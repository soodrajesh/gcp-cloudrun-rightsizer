"""Search for the cheapest configuration that meets the SLO, by simulation.

Coordinate descent over (billing mode, concurrency, CPU/memory, min instances): each step re-simulates the
actual traffic, so a candidate is only ever recommended with its own predicted cold starts, latency and
cost. It is a heuristic, not a global optimum; `alternatives` shows what else was tried."""

from __future__ import annotations

import math
import multiprocessing as mp
import os
from concurrent.futures import ProcessPoolExecutor

from .models import SLO, Candidate, Finding, ServiceConfig, Snapshot
from .simulator import SimParams, Workload, calibrate, simulate

CPU_OPTIONS = [1.0, 2.0, 4.0, 8.0]
MEM_OPTIONS = [0.5, 1.0, 2.0, 4.0, 8.0, 16.0]
CONC_OPTIONS = [1, 2, 4, 8, 16, 32, 64, 80, 120, 250]
MIN_OPTIONS = [0, 1, 2, 3, 5]
MAX_OPTIONS = [2, 5, 10, 20, 50, 100]


def min_mem(cpu: float) -> float:
    return (
        0.5 * cpu
    )  # Cloud Run requires at least 0.5 GiB per vCPU for 2+ CPUs; kept for all sizes as a safe floor


def valid(cfg: ServiceConfig) -> bool:
    return cfg.memory_gib >= min_mem(cfg.cpu) and (cfg.cpu >= 1 or cfg.concurrency == 1)


def evaluate(
    snap: Snapshot,
    cfg: ServiceConfig,
    slo: SLO,
    demand: float,
    params: SimParams,
    workload: Workload | None = None,
) -> Candidate:
    res = simulate(snap.traffic, snap.observed, cfg, demand, params, workload)
    p95_cap = slo.p95_latency_ms or snap.observed.latency_p95_ms * 1.25
    viol = []
    if res.cold_start_fraction > slo.max_cold_start_fraction:
        viol.append(
            f"cold starts {res.cold_start_fraction:.2%} > {slo.max_cold_start_fraction:.2%}"
        )
    if res.p95_ms > p95_cap:
        viol.append(f"p95 {res.p95_ms:.0f} ms > {p95_cap:.0f} ms")
    if res.drop_fraction > slo.max_drop_fraction:
        viol.append(f"{res.drop_fraction:.2%} of requests dropped")
    mem_need = memory_needed(snap, cfg)
    if cfg.memory_gib < mem_need:
        viol.append(f"memory {cfg.memory_gib} GiB < estimated need {mem_need:.2f} GiB")
    return Candidate(
        config=cfg, result=res, feasible=not viol, violations=viol, monthly_usd=res.cost_month.total
    )


def memory_needed(snap: Snapshot, cfg: ServiceConfig) -> float:
    """Peak memory used today, scaled up if concurrency rises (conservative: all of it scales with concurrency)."""
    used = snap.observed.mem_util_p99 * snap.config.memory_gib
    grow = (
        max(1.0, min(cfg.concurrency, 1000) / max(snap.observed.concurrency_mean, 1.0))
        if cfg.concurrency > snap.config.concurrency
        else 1.0
    )
    return used * grow / 0.85  # keep memory under 85% utilisation


_CTX: dict = {}


def _init_worker(snap, slo, demand, params, workload) -> None:
    _CTX.update(snap=snap, slo=slo, demand=demand, params=params, workload=workload)


def _eval_one(cfg_dict: dict) -> Candidate:
    c = _CTX
    return evaluate(
        c["snap"], ServiceConfig(**cfg_dict), c["slo"], c["demand"], c["params"], c["workload"]
    )


def _parallel(pool: ProcessPoolExecutor, cfgs: list[ServiceConfig]) -> list[Candidate]:
    return list(pool.map(_eval_one, [c.model_dump() for c in cfgs]))


def _with_required_memory(snap: Snapshot, cfg: ServiceConfig) -> float:
    """Smallest allowed memory size that covers what this configuration needs (never less than it has)."""
    need = max(memory_needed(snap, cfg), min_mem(cfg.cpu))
    fits = [m for m in MEM_OPTIONS if m >= need]
    return max(cfg.memory_gib, fits[0]) if fits else cfg.memory_gib


def _size_options(snap: Snapshot) -> list[tuple[float, float]]:
    floor = memory_needed(snap, snap.config)
    cpus = [c for c in CPU_OPTIONS if c <= max(2 * snap.config.cpu, 2.0)]
    mems = [m for m in MEM_OPTIONS if floor <= m <= 4 * max(snap.config.memory_gib, 1.0)] or [
        snap.config.memory_gib
    ]
    out = [(c, m) for c in cpus for m in mems if m >= min_mem(c)]
    return sorted({*out, (snap.config.cpu, snap.config.memory_gib)})


def search(
    snap: Snapshot, slo: SLO | None = None, params: SimParams | None = None, rounds: int = 2
) -> tuple[Candidate, Candidate, list[Candidate], float]:
    slo = slo or SLO()
    params = params or SimParams()
    workload = Workload.build(snap.traffic, params.seed)
    demand = calibrate(snap.traffic, snap.observed, snap.config, params, workload)
    workers = int(os.environ.get("RIGHTSIZE_WORKERS", os.cpu_count() or 1))
    pool: ProcessPoolExecutor | None = None
    if workers > 1 and snap.traffic.total > 20_000:
        # Spawn (not fork), so this is safe from a server thread; the workload is shipped once per worker.
        pool = ProcessPoolExecutor(
            max_workers=workers,
            mp_context=mp.get_context("spawn"),
            initializer=_init_worker,
            initargs=(snap, slo, demand, params, workload),
        )
    try:
        return _search(snap, slo, params, rounds, workload, demand, pool)
    finally:
        if pool is not None:
            pool.shutdown(cancel_futures=True)


def _search(
    snap: Snapshot,
    slo: SLO,
    params: SimParams,
    rounds: int,
    workload: Workload,
    demand: float,
    pool: ProcessPoolExecutor | None,
) -> tuple[Candidate, Candidate, list[Candidate], float]:
    seen: dict[tuple, Candidate] = {}

    def key_of(cfg: ServiceConfig) -> tuple:
        return (
            cfg.cpu,
            cfg.memory_gib,
            cfg.concurrency,
            cfg.min_instances,
            cfg.max_instances,
            cfg.cpu_idle,
            cfg.startup_cpu_boost,
        )

    def ev(cfg: ServiceConfig) -> Candidate:
        key = key_of(cfg)
        if key not in seen:
            seen[key] = evaluate(snap, cfg, slo, demand, params, workload)
        return seen[key]

    current = ev(snap.config)

    def better(a: Candidate, b: Candidate) -> bool:
        if a.feasible != b.feasible:
            return a.feasible
        if not a.feasible:
            return len(a.violations) < len(b.violations) or (
                len(a.violations) == len(b.violations) and a.monthly_usd < b.monthly_usd
            )
        if abs(a.monthly_usd - b.monthly_usd) <= 0.01 * max(
            b.monthly_usd, 0.01
        ):  # a tie: prefer the smaller change
            return len(diff_fields(snap.config, a.config)) < len(diff_fields(snap.config, b.config))
        return a.monthly_usd < b.monthly_usd

    best = current
    for _ in range(rounds):
        improved = False
        steps = [
            ("cpu_idle", [True, False]),
            ("concurrency", sorted({*CONC_OPTIONS, snap.config.concurrency})),
            ("size", _size_options(snap)),
            ("min_instances", sorted({*MIN_OPTIONS, snap.config.min_instances})),
            ("max_instances", sorted({snap.config.max_instances, *MAX_OPTIONS})),
            ("startup_cpu_boost", [False, True]),
        ]
        for dim, options in steps:
            if (
                dim == "concurrency"
                and best.result.peak_instances <= 1
                and best.result.drop_fraction == 0
            ):
                continue  # a single instance never saturates: concurrency cannot matter
            if dim == "min_instances":
                options = [
                    o
                    for o in options
                    if o <= 3 or o == snap.config.min_instances or not best.feasible
                ]
            batch: list[ServiceConfig] = []
            for opt in options:
                cfg = best.config.model_copy()
                if dim == "size":
                    cfg.cpu, cfg.memory_gib = opt
                else:
                    setattr(cfg, dim, opt)
                if dim in ("concurrency", "size"):
                    cfg.memory_gib = _with_required_memory(
                        snap, cfg
                    )  # a move carries the memory it needs
                if cfg.min_instances > cfg.max_instances or not valid(cfg) or cfg.cpu < 1:
                    continue
                batch.append(cfg)
            todo = [c for c in batch if key_of(c) not in seen]
            if pool is not None and len(todo) >= 3:
                for c, cand in zip(todo, _parallel(pool, todo), strict=True):
                    seen[key_of(c)] = cand
            for cfg in batch:
                cand = ev(cfg)
                if better(cand, best):
                    best, improved = cand, True
        if not improved:
            break
    alts = sorted(
        (c for c in seen.values() if c.feasible and c is not best), key=lambda c: c.monthly_usd
    )[:4]
    return current, best, alts, demand


BILLING = {
    True: "request-based (CPU only while handling requests)",
    False: "instance-based (CPU always allocated)",
}
LABELS = {
    "cpu": "vCPU",
    "memory_gib": "memory (GiB)",
    "concurrency": "concurrency",
    "min_instances": "min instances",
    "max_instances": "max instances",
    "startup_cpu_boost": "startup CPU boost",
}


def diff_fields(a: ServiceConfig, b: ServiceConfig) -> list[str]:
    """Human-readable changes. Billing mode is spelled out because `cpu_idle: true` reads backwards."""
    out = []
    for f, label in LABELS.items():
        if getattr(a, f) != getattr(b, f):
            va, vb = getattr(a, f), getattr(b, f)
            if isinstance(va, bool):
                va, vb = ("on" if va else "off"), ("on" if vb else "off")
            out.append(f"{label}: {va} -> {vb}")
    if a.cpu_idle != b.cpu_idle:
        out.append(f"billing: {BILLING[a.cpu_idle]} -> {BILLING[b.cpu_idle]}")
    return out


def commands(cur: ServiceConfig, new: ServiceConfig, project: str = "<project>") -> list[str]:
    parts = []
    if new.cpu != cur.cpu:
        parts.append(f"--cpu={new.cpu:g}")
    if new.memory_gib != cur.memory_gib:
        mem = f"{int(new.memory_gib * 1024)}Mi" if new.memory_gib < 1 else f"{new.memory_gib:g}Gi"
        parts.append(f"--memory={mem}")
    if new.concurrency != cur.concurrency:
        parts.append(f"--concurrency={new.concurrency}")
    if new.min_instances != cur.min_instances:
        parts.append(f"--min-instances={new.min_instances}")
    if new.max_instances != cur.max_instances:
        parts.append(f"--max-instances={new.max_instances}")
    if new.cpu_idle != cur.cpu_idle:
        parts.append("--cpu-throttling" if new.cpu_idle else "--no-cpu-throttling")
    if new.startup_cpu_boost != cur.startup_cpu_boost:
        parts.append("--cpu-boost" if new.startup_cpu_boost else "--no-cpu-boost")
    if not parts:
        return []
    return [
        f"gcloud run services update {cur.name} --region={cur.region} --project={project} "
        + " ".join(parts)
    ]


def terraform_snippet(new: ServiceConfig) -> str:
    mem = f"{int(new.memory_gib * 1024)}Mi" if new.memory_gib < 1 else f"{new.memory_gib:g}Gi"
    return (
        f"  # google_cloud_run_v2_service.{new.name.replace('-', '_')}\n"
        "  template {\n"
        f"    max_instance_request_concurrency = {new.concurrency}\n"
        "    scaling {\n"
        f"      min_instance_count = {new.min_instances}\n"
        f"      max_instance_count = {new.max_instances}\n"
        "    }\n"
        "    containers {\n"
        "      resources {\n"
        f'        limits   = {{ cpu = "{new.cpu:g}", memory = "{mem}" }}\n'
        f"        cpu_idle = {str(new.cpu_idle).lower()}\n"
        f"        startup_cpu_boost = {str(new.startup_cpu_boost).lower()}\n"
        "      }\n"
        "    }\n"
        "  }\n"
    )


def rule_findings(snap: Snapshot, current: Candidate) -> list[Finding]:
    c, o, r = snap.config, snap.observed, current.result
    out: list[Finding] = []

    def add(i, sev, title, detail=""):
        out.append(Finding(id=i, severity=sev, title=title, detail=detail))

    if snap.traffic.total == 0 and c.min_instances > 0:
        add(
            "RS001",
            "high",
            "Minimum instances with no traffic",
            f"{c.min_instances} instance(s) are billed with zero requests in the window.",
        )
    if o.cpu_util_p95 < 0.25 and c.cpu > 1:
        add(
            "RS002",
            "medium",
            f"CPU is mostly idle (p95 {o.cpu_util_p95:.0%} of {c.cpu:g} vCPU)",
            "A smaller instance would serve the same load.",
        )
    if o.mem_util_p99 > 0.85:
        add(
            "RS003",
            "high",
            f"Memory is near its limit (p99 {o.mem_util_p99:.0%})",
            "Risk of out-of-memory restarts; increase memory before anything else.",
        )
    if c.concurrency == 1 and o.cpu_util_mean < 0.3:
        add(
            "RS004",
            "medium",
            "Concurrency 1 with low CPU use",
            "Raising concurrency lets one instance serve several requests and cuts instance count and cold starts.",
        )
    if r.peak_instances >= 0.9 * c.max_instances:
        add(
            "RS005",
            "high",
            f"Peak instances ({r.peak_instances}) reach max-instances ({c.max_instances})",
            "Requests can be queued or rejected during bursts.",
        )
    if not c.cpu_idle and r.busy_seconds < 0.3 * r.instance_seconds:
        add(
            "RS006",
            "medium",
            "CPU always allocated but busy under 30% of the time",
            "Request-based billing usually costs less for this traffic shape.",
        )
    if o.startup_s > 5 and r.cold_start_fraction > 0.01 and not c.startup_cpu_boost:
        add(
            "RS007",
            "low",
            f"Slow startup ({o.startup_s:.1f}s) with {r.cold_start_fraction:.1%} cold starts",
            "Enable startup CPU boost or trim container start-up work.",
        )
    if r.drop_fraction > 0:
        add(
            "RS008",
            "high",
            f"{r.drop_fraction:.2%} of requests would be dropped on the current settings",
        )
    if c.min_instances > 0 and r.cold_starts == 0 and snap.traffic.total > 0:
        add(
            "RS009",
            "info",
            "Minimum instances are fully warm",
            "No cold starts in the window; check whether they are all needed (see recommendation).",
        )
    return out


def sizing_hint(snap: Snapshot) -> dict[str, float]:
    o = snap.observed
    cpu_target = snap.config.cpu * max(o.cpu_util_p95, 0.05) / 0.6
    return {
        "cpu_for_60pct_p95": round(max(1.0, math.ceil(cpu_target)), 1),
        "memory_floor_gib": round(memory_needed(snap, snap.config), 2),
    }
