"""Request-level simulation of a Cloud Run service.

Model (deliberately simple, every assumption is listed in docs/architecture.md):
  * arrivals: the per-step request counts are spread uniformly at random within each step (seeded)
  * service time: lognormal fitted to the observed p50/p95 latency
  * routing: least-loaded warm instance with a free slot, else wait on an instance that is still starting,
    else start a new one (cold start) while below max instances, else queue briefly, else drop (429)
  * CPU contention: a request needs `d` vCPU-seconds per second; an instance running n requests slows each
    by max(1, n*d/cpu). `d` is derived from the observed CPU utilisation and concurrency
  * instances that stay idle for `idle_timeout_s` are removed; min instances never are
  * billing: request-based (cpu_idle) bills busy + startup time and idle min instances at the idle rate,
    instance-based bills the whole instance lifetime; the window is scaled to 30 days"""

from __future__ import annotations

import heapq
import math
import random
from dataclasses import dataclass, field

from . import pricing
from .models import Cost, Observed, ServiceConfig, SimResult, Traffic

Z95 = 1.645


@dataclass
class SimParams:
    idle_timeout_s: float = 900.0
    queue_timeout_s: float = 10.0
    startup_billed: float = 1.0  # fraction of container startup time billed as active time (see docs/validation-results.md)
    pack: bool = True  # route to the busiest instance that still has room (keeps other instances idle, which request-based billing does not charge for)
    scale_target: float = (
        0.6  # Cloud Run scales out when concurrency utilisation exceeds ~60% of the maximum
    )
    seed: int = 7


@dataclass
class _Inst:
    ready: float
    created: float
    min_pinned: bool
    ends: list[float] = field(default_factory=list)  # heap of in-flight completion times
    pending: int = 0  # requests assigned but not started (waiting for startup)
    last_end: float = 0.0
    busy_until: float = 0.0
    busy: float = 0.0
    closed_at: float | None = None

    def inflight(self, t: float) -> int:
        while self.ends and self.ends[0] <= t:
            heapq.heappop(self.ends)
        return len(self.ends) + self.pending

    def add_busy(self, s: float, e: float) -> None:
        if s >= self.busy_until:
            self.busy += e - s
        elif e > self.busy_until:
            self.busy += e - self.busy_until
        self.busy_until = max(self.busy_until, e)


QPROBS = (0.05, 0.5, 0.95, 0.99)  # the percentiles Cloud Monitoring can compute


def startup_time(obs: Observed, u: float) -> float:
    """Container startup latency for one instance. Startup is far from constant (a cold image pull can take
    10+ s while a warm host takes ~1 s), so when the quantiles are known they are sampled rather than using one number."""
    q = obs.startup_quantiles_s
    if len(q) != len(QPROBS):
        return obs.startup_s
    xs = [0.0, *QPROBS, 1.0]
    ys = [q[0] * 0.5, *q, q[-1] * 1.2]
    for i in range(1, len(xs)):
        if u <= xs[i]:
            f = (u - xs[i - 1]) / (xs[i] - xs[i - 1])
            return ys[i - 1] + f * (ys[i] - ys[i - 1])
    return ys[-1]


def cpu_demand(obs: Observed, cfg: ServiceConfig, mean_concurrency: float | None = None) -> float:
    """vCPU a single in-flight request consumes while running: observed CPU utilisation of a busy instance
    divided by its concurrency. The concurrency is the one the traffic implies on the current settings
    (calibrated by `calibrate`), because the metric-derived figure can disagree with the traffic."""
    k = mean_concurrency if mean_concurrency is not None else obs.concurrency_mean
    return max(1e-4, obs.cpu_util_mean * cfg.cpu / max(k, 1.0))


def calibrate(
    traffic: Traffic,
    obs: Observed,
    cfg: ServiceConfig,
    params: SimParams | None = None,
    workload: Workload | None = None,
) -> float:
    """Per-request vCPU demand, using the mean concurrency a contention-free run of the current config produces."""
    r = simulate(traffic, obs, cfg, demand=1e-4, params=params, workload=workload)
    return cpu_demand(obs, cfg, r.mean_concurrency)


def _percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


@dataclass
class Workload:
    """Arrival times and unit-normal service draws, generated once and reused for every candidate
    configuration (common random numbers): differences between candidates are then due to the
    configuration, not to simulation noise."""

    arrivals: list[float]
    normals: list[float]
    startup_u: list[
        float
    ]  # uniform draws for container startup times (one per potential instance start)

    @classmethod
    def build(cls, traffic: Traffic, seed: int = 7) -> Workload:
        rng = random.Random(seed)
        arr: list[float] = []
        for i, n in enumerate(traffic.requests):
            base = i * traffic.step_s
            arr += [base + rng.random() * traffic.step_s for _ in range(n)]
        arr.sort()
        return cls(arr, [rng.gauss(0.0, 1.0) for _ in arr], [rng.random() for _ in arr])


def simulate(
    traffic: Traffic,
    obs: Observed,
    cfg: ServiceConfig,
    demand: float | None = None,
    params: SimParams | None = None,
    workload: Workload | None = None,
) -> SimResult:
    p = params or SimParams()
    wl = workload or Workload.build(traffic, p.seed)
    d = demand if demand is not None else cpu_demand(obs, cfg)
    median = max(obs.latency_p50_ms, 1.0) / 1000
    sigma = max(
        0.05,
        math.log(max(obs.latency_p95_ms, obs.latency_p50_ms * 1.01) / max(obs.latency_p50_ms, 1.0))
        / Z95,
    )
    boost = 0.6 if cfg.startup_cpu_boost else 1.0
    window = float(traffic.window_s)
    arrivals, normals, startup_u = wl.arrivals, wl.normals, wl.startup_u

    insts: list[_Inst] = [
        _Inst(ready=0.0, created=0.0, min_pinned=True) for _ in range(cfg.min_instances)
    ]
    retired: list[_Inst] = []
    lat: list[float] = []
    cold = dropped = peak = 0
    service_total = 0.0
    started = 0
    inst_area = (
        0.0  # integral of instance count, for mean_instances (computed from lifetimes below)
    )

    for idx, t in enumerate(arrivals):
        # one pass over the instances: retire idle ones, pick the best warm slot, remember starting ones
        keep: list[_Inst] = []
        total_inflight = 0
        best_warm = None
        best_warm_key = None
        best_start = None
        best_start_key = None
        for ins in insts:
            n = ins.inflight(t)
            if (
                not ins.min_pinned
                and n == 0
                and t - max(ins.last_end, ins.ready) > p.idle_timeout_s
            ):
                ins.closed_at = min(max(ins.last_end, ins.ready) + p.idle_timeout_s, window)
                retired.append(ins)
                continue
            keep.append(ins)
            total_inflight += n
            if n < cfg.concurrency:
                if ins.ready <= t:
                    k = (-n if p.pack else n, ins.ready)
                    if best_warm_key is None or k < best_warm_key:
                        best_warm, best_warm_key = ins, k
                else:
                    k = (ins.ready, n)
                    if best_start_key is None or k < best_start_key:
                        best_start, best_start_key = ins, k
        insts = keep

        target = None
        start_at = t
        is_cold = False
        if best_warm is not None:
            target = best_warm
        elif best_start is not None:
            target = best_start
            start_at = target.ready
            is_cold = True  # it waits for an instance that another request already triggered
        elif len(insts) < cfg.max_instances:
            startup = startup_time(obs, startup_u[started % len(startup_u)]) * boost
            target = _Inst(ready=t + startup, created=t, min_pinned=False)
            started += 1
            insts.append(target)
            start_at, is_cold = target.ready, True
        else:  # saturated: wait for the earliest completion
            best = min(insts, key=lambda i: i.ends[0] if i.ends else math.inf)
            free_at = best.ends[0] if best.ends else math.inf
            if free_at - t > p.queue_timeout_s:
                dropped += 1
                continue
            target, start_at = best, free_at
        n_now = target.inflight(start_at) + 1
        slow = max(1.0, n_now * d / cfg.cpu)
        service = math.exp(math.log(median) + sigma * normals[idx]) * slow
        end = start_at + service
        service_total += service
        if is_cold:
            cold += 1
            if target.ready > t and target.busy == 0 and not target.ends:
                if p.startup_billed > 0:
                    target.add_busy(
                        target.created,
                        target.created + (target.ready - target.created) * p.startup_billed,
                    )
        target.add_busy(start_at, end)
        heapq.heappush(target.ends, end)
        target.last_end = max(target.last_end, end)
        lat.append((end - t) * 1000)
        # background scale-out: Cloud Run adds instances when utilisation passes the target, before requests have to wait
        if (
            total_inflight + 1 > p.scale_target * cfg.concurrency * len(insts)
            and len(insts) < cfg.max_instances
        ):
            su = startup_time(obs, startup_u[started % len(startup_u)]) * boost
            extra = _Inst(ready=t + su, created=t, min_pinned=False)
            extra.last_end = t + su
            if p.startup_billed > 0:
                extra.add_busy(t, t + su * p.startup_billed)  # startup is billed as active time
            insts.append(extra)
            started += 1
        peak = max(peak, len(insts))

    for ins in insts:
        ins.closed_at = (
            window
            if ins.min_pinned
            else min(max(ins.last_end, ins.ready) + p.idle_timeout_s, window)
        )
    all_i = retired + insts
    lifetimes = [(i, max(0.0, (i.closed_at or window) - i.created)) for i in all_i]
    inst_seconds = sum(life for _, life in lifetimes)
    inst_area = inst_seconds / window if window else 0.0
    busy_seconds = sum(min(i.busy, life) for i, life in lifetimes)
    min_idle_s = sum(max(0.0, life - min(i.busy, life)) for i, life in lifetimes if i.min_pinned)

    k = pricing.SECONDS_PER_MONTH / window if window else 0.0
    pr = pricing.for_region(cfg.region)
    cost = Cost()
    if cfg.cpu_idle:
        rb, idle = pr["request_based"], pr["min_instance_idle"]
        cost.cpu_usd = busy_seconds * cfg.cpu * rb["cpu_per_vcpu_s"] * k
        cost.memory_usd = busy_seconds * cfg.memory_gib * rb["mem_per_gib_s"] * k
        cost.idle_min_instances_usd = (
            min_idle_s
            * (cfg.cpu * idle["cpu_per_vcpu_s"] + cfg.memory_gib * idle["mem_per_gib_s"])
            * k
        )
        cost.requests_usd = (
            max(0.0, len(arrivals) * k - pr["free_requests_per_month"])
            * pr["per_million_requests"]
            / 1e6
        )
    else:
        ib = pr["instance_based"]
        cost.cpu_usd = inst_seconds * cfg.cpu * ib["cpu_per_vcpu_s"] * k
        cost.memory_usd = inst_seconds * cfg.memory_gib * ib["mem_per_gib_s"] * k
    n = len(arrivals)
    return SimResult(
        requests=n,
        cold_starts=cold,
        cold_start_fraction=round(cold / n, 5) if n else 0.0,
        p50_ms=round(_percentile(lat, 0.5), 1),
        p95_ms=round(_percentile(lat, 0.95), 1),
        p99_ms=round(_percentile(lat, 0.99), 1),
        dropped=dropped,
        drop_fraction=round(dropped / n, 5) if n else 0.0,
        peak_instances=peak,
        instances_started=started,
        mean_instances=round(inst_area, 3),
        instance_seconds=round(inst_seconds, 1),
        busy_seconds=round(busy_seconds, 1),
        mean_concurrency=round(service_total / busy_seconds, 3) if busy_seconds else 1.0,
        window_s=int(window),
        cost_month=Cost(**{k_: round(v, 4) for k_, v in cost.model_dump().items()}),
    )
