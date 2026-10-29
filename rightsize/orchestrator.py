"""snapshot -> [search] simulate and pick the cheapest feasible config   deterministic
-> [rules]  findings and commands                               deterministic
-> parallel [explainer] note (numbers validated)  [proposer] alternative config (verified by simulation)
-> [judge]  summary (template, optionally model-phrased but guarded)"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime

from . import recommend as rc
from .agents import explain, propose
from .llm import LLM, Budget
from .models import SLO, Candidate, Recommendation, Snapshot, TraceStep
from .simulator import SimParams, Workload

MAX_REQUESTS = 400_000
MIN_SAVING_USD = 1.0
MIN_SAVING_PCT = 0.03
MAX_WINDOW_S = 14 * 86400


class SnapshotError(ValueError):
    pass


def validate(snap: Snapshot) -> None:
    if snap.traffic.total > MAX_REQUESTS:
        raise SnapshotError(
            f"{snap.traffic.total} requests in the window; limit is {MAX_REQUESTS:,} (use a shorter window)"
        )
    if snap.traffic.window_s > MAX_WINDOW_S or snap.traffic.window_s < 600:
        raise SnapshotError("window must be between 10 minutes and 14 days")


def summary(
    rec_cur: Candidate, best: Candidate, saving: float, pct: float, changed: list[str]
) -> str:
    if not best.feasible:
        return (
            f"No tested configuration meets the SLO ({'; '.join(best.violations)}). Closest: ${best.monthly_usd:.2f} per month; "
            "consider a larger instance, a higher max-instances, or relaxing the SLO."
        )
    if not changed:
        return f"Current settings are already the cheapest that meet the SLO (${rec_cur.monthly_usd:.2f} per month)."
    if not rec_cur.feasible:
        return (
            f"Current settings miss the SLO ({'; '.join(rec_cur.violations)}). The recommended settings meet it at "
            f"${best.monthly_usd:.2f} per month (current ${rec_cur.monthly_usd:.2f})."
        )
    return f"Save about ${saving:.2f} per month ({pct:.0f}%): {'; '.join(changed)}."


async def recommend(
    snap: Snapshot,
    llm: LLM,
    slo: SLO | None = None,
    *,
    budget: Budget | None = None,
    timeout: float = 45.0,
    params: SimParams | None = None,
) -> Recommendation:
    t0 = time.perf_counter()
    validate(snap)
    slo, budget, params = slo or SLO(), budget or Budget(), params or SimParams()
    trace: list[TraceStep] = []
    cur, best, alts, demand = await asyncio.to_thread(rc.search, snap, slo, params)
    trace.append(
        TraceStep(
            agent="search",
            action="simulate",
            duration_ms=int((time.perf_counter() - t0) * 1000),
            note=f"{snap.traffic.total} requests simulated per candidate",
        )
    )
    # A feasible current config that is within noise of the best is left alone: churn has its own cost and risk.
    material = max(MIN_SAVING_USD, MIN_SAVING_PCT * cur.monthly_usd)
    if cur.feasible and cur.monthly_usd - best.monthly_usd < material:
        best = cur
    saving = round(cur.monthly_usd - best.monthly_usd, 2)
    pct = round(100 * saving / cur.monthly_usd, 1) if cur.monthly_usd else 0.0
    changed = rc.diff_fields(snap.config, best.config)
    rec = Recommendation(
        id=uuid.uuid4().hex[:12],
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        service=snap.config.name,
        source=snap.source,
        provider=llm.name,
        model=llm.model,
        summary=summary(cur, best, saving, pct, changed),
        current=cur,
        best=best,
        saving_month_usd=saving,
        saving_pct=pct,
        changed=changed,
        alternatives=alts,
        findings=rc.rule_findings(snap, cur),
        commands=rc.commands(snap.config, best.config),
        terraform=rc.terraform_snippet(best.config) if changed else "",
        slo=slo,
        traffic_preview=_preview(snap),
        trace=trace,
        assumptions={
            "idle_timeout_s": params.idle_timeout_s,
            "cpu_demand_vcpu_per_request": round(demand, 4),
            "free_tier_ignored": True,
            "window_s": snap.traffic.window_s,
            "notes": snap.notes,
        },
    )
    if llm.name != "none":
        (note, estep, why), (prop, pstep) = await asyncio.gather(
            explain(rec, llm, budget, t0, timeout), propose(rec, llm, budget, t0, timeout)
        )
        trace += [estep, pstep]
        if note:
            rec.note = note
        elif why:
            rec.dropped.append({"agent": "explainer", "item": "explanation", "reason": why})
        elif estep.status == "error":
            rec.degraded.append(f"explainer: {estep.note}")
        if pstep.status == "error":
            rec.degraded.append(f"proposer: {pstep.note}")
        if pstep.note.startswith("invalid configuration"):
            rec.dropped.append({"agent": "proposer", "item": "configuration", "reason": pstep.note})
        if prop:
            cfg, why_p = prop
            if not rc.valid(cfg) or cfg.min_instances > cfg.max_instances:
                rec.dropped.append(
                    {
                        "agent": "proposer",
                        "item": "configuration",
                        "reason": "not a valid Cloud Run configuration",
                    }
                )
            else:
                wl = Workload.build(snap.traffic, params.seed)
                cand = await asyncio.to_thread(rc.evaluate, snap, cfg, slo, demand, params, wl)
                if not cand.feasible:
                    rec.dropped.append(
                        {
                            "agent": "proposer",
                            "item": "configuration",
                            "reason": "misses the SLO: " + "; ".join(cand.violations),
                        }
                    )
                elif cand.monthly_usd >= cur.monthly_usd:
                    rec.dropped.append(
                        {
                            "agent": "proposer",
                            "item": "configuration",
                            "reason": "not cheaper than the current settings",
                        }
                    )
                else:
                    cand.violations = [f"model-proposed: {why_p}"] if why_p else ["model-proposed"]
                    rec.alternatives = sorted(
                        [*rec.alternatives, cand], key=lambda c: c.monthly_usd
                    )[:5]
    else:
        trace.append(
            TraceStep(
                agent="explainer", action="explain", status="skipped", note="no model configured"
            )
        )
    rec.trace = trace  # the model rebinds the list on validation, so assign the finished one
    rec.duration_ms = int((time.perf_counter() - t0) * 1000)
    return rec


def _preview(snap: Snapshot, n: int = 96) -> list[int]:
    r, k = snap.traffic.requests, max(1, len(snap.traffic.requests) // 96)
    return [sum(r[i : i + k]) for i in range(0, len(r), k)][:n]
