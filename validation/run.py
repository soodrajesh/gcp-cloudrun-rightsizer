"""Validate the simulator against a REAL Cloud Run service.

1. `load`    sends a scripted burst pattern to rs-target (a service with known settings) and records the schedule
2. `compare` (a few minutes later, when Cloud Monitoring has caught up) reads the measured metrics for that window,
             runs the simulator on the same schedule with the same settings, and writes docs/validation-results.md

  .venv/bin/python validation/run.py load    --url https://rs-target-xxxx.run.app --token "$(gcloud auth print-identity-token)"
  .venv/bin/python validation/run.py compare --project <p> --service rs-target
"""

import argparse
import asyncio
import json
import random
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# (start second, number of requests, burst length in seconds). Pattern A was used while building the model;
# pattern B (different shape, size and seed) was run afterwards as a held-out check.
PATTERNS = {
    "A": {
        "bursts": [(0, 40, 3), (180, 120, 6), (420, 240, 8), (660, 80, 4)],
        "work_ms": 400,
        "seed": 5,
    },
    "B": {
        "bursts": [(0, 25, 2), (150, 60, 5), (330, 160, 5), (540, 320, 10), (840, 45, 3)],
        "work_ms": 250,
        "seed": 9,
    },
    # C: run after the simulator's routing, scale-out and billing assumptions were fixed using A and B: a true holdout
    "C": {
        "bursts": [(0, 30, 10), (120, 100, 60), (330, 600, 120), (600, 200, 30)],
        "work_ms": 800,
        "seed": 13,
    },
}
PATTERN = "A"
BURSTS, WORK_MS, SEED = (PATTERNS["A"]["bursts"], PATTERNS["A"]["work_ms"], PATTERNS["A"]["seed"])


def results_path() -> Path:
    return ROOT / "validation" / f"results-{PATTERN}.json"


def use_pattern(name: str) -> None:
    global PATTERN, BURSTS, WORK_MS, SEED
    PATTERN = name
    BURSTS, WORK_MS, SEED = (
        PATTERNS[name]["bursts"],
        PATTERNS[name]["work_ms"],
        PATTERNS[name]["seed"],
    )


def schedule() -> list[float]:
    rng = random.Random(SEED)
    return sorted(s + rng.random() * length for s, n, length in BURSTS for _ in range(n))


async def load(url: str, token: str):
    sched = schedule()
    t0 = time.time()
    start_iso = datetime.fromtimestamp(t0, UTC).isoformat()
    lat: list[float] = []
    errors = 0
    async with httpx.AsyncClient(timeout=30, limits=httpx.Limits(max_connections=400)) as c:

        async def one(at: float):
            nonlocal errors
            await asyncio.sleep(max(0.0, t0 + at - time.time()))
            s = time.perf_counter()
            try:
                r = await c.get(
                    f"{url}/work",
                    params={"ms": WORK_MS},
                    headers={"Authorization": f"Bearer {token}"},
                )
                if r.status_code != 200:
                    errors += 1
                    return
                lat.append((time.perf_counter() - s) * 1000)
            except httpx.HTTPError:
                errors += 1

        await asyncio.gather(*(one(a) for a in sched))
    lat.sort()
    out = {
        "start": start_iso,
        "end": datetime.now(UTC).isoformat(),
        "schedule": sched,
        "bursts": BURSTS,
        "work_ms": WORK_MS,
        "sent": len(sched),
        "ok": len(lat),
        "errors": errors,
        "client_p50_ms": lat[len(lat) // 2],
        "client_p95_ms": lat[int(len(lat) * 0.95)],
        "client_p99_ms": lat[int(len(lat) * 0.99)],
    }
    results_path().write_text(json.dumps(out))
    print({k: v for k, v in out.items() if k != "schedule"})


def monitoring(
    project: str,
    service: str,
    metric: str,
    aligner: str,
    period: int,
    reducer: str,
    start,
    end,
    extra="",
):
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform.read-only"]
    )
    s = AuthorizedSession(creds)
    params = {
        "filter": f'metric.type="run.googleapis.com/{metric}" AND resource.labels.service_name="{service}"{extra}',
        "interval.startTime": start.isoformat().replace("+00:00", "Z"),
        "interval.endTime": end.isoformat().replace("+00:00", "Z"),
        "aggregation.alignmentPeriod": f"{period}s",
        "aggregation.perSeriesAligner": aligner,
        "aggregation.crossSeriesReducer": reducer,
    }
    r = s.get(
        f"https://monitoring.googleapis.com/v3/projects/{project}/timeSeries",
        params=params,
        timeout=60,
    )
    r.raise_for_status()
    return r.json().get("timeSeries", [])


def values(series, kind="doubleValue"):
    out = []
    for s in series:
        for p in s.get("points", []):
            v = p["value"]
            out.append(
                float(v["int64Value"])
                if "int64Value" in v
                else float(v.get("doubleValue", 0))
                if "doubleValue" in v
                else v
            )
    return out


def compare(project: str, service: str):
    from rightsize.models import Observed, ServiceConfig, Traffic
    from rightsize.simulator import simulate

    run = json.loads(results_path().read_text())
    t0 = datetime.fromisoformat(run["start"])
    start, end = t0 - timedelta(minutes=2), t0 + timedelta(minutes=17)
    win = int((end - start).total_seconds())

    reqs = sum(
        values(
            monitoring(
                project, service, "request_count", "ALIGN_DELTA", 60, "REDUCE_SUM", start, end
            )
        )
    )
    pcts = {
        q: values(
            monitoring(
                project,
                service,
                "request_latencies",
                f"ALIGN_PERCENTILE_{q}",
                win,
                "REDUCE_NONE",
                start,
                end,
            )
        )
        for q in ("50", "95", "99")
    }
    startup = monitoring(
        project,
        service,
        "container/startup_latencies",
        "ALIGN_DELTA",
        60,
        "REDUCE_NONE",
        start,
        end,
    )
    starts = sum(
        int(p["value"]["distributionValue"].get("count", 0))
        for s in startup
        for p in s.get("points", [])
    )
    startup_p50 = values(
        monitoring(
            project,
            service,
            "container/startup_latencies",
            "ALIGN_PERCENTILE_50",
            win,
            "REDUCE_NONE",
            start,
            end,
        )
    )
    startup_q = []
    for q in ("05", "50", "95", "99"):
        v = values(
            monitoring(
                project,
                service,
                "container/startup_latencies",
                f"ALIGN_PERCENTILE_{q}",
                win,
                "REDUCE_NONE",
                start,
                end,
            )
        )
        startup_q.append((v[0] if v else 0.0) / 1000)
    inst = values(
        monitoring(
            project, service, "container/instance_count", "ALIGN_MAX", 60, "REDUCE_SUM", start, end
        )
    )
    billable = sum(
        values(
            monitoring(
                project,
                service,
                "container/billable_instance_time",
                "ALIGN_DELTA",
                60,
                "REDUCE_SUM",
                start,
                end,
            )
        )
    )

    measured = {
        "requests": int(reqs),
        "p50_ms": pcts["50"][0] if pcts["50"] else None,
        "p95_ms": pcts["95"][0] if pcts["95"] else None,
        "p99_ms": pcts["99"][0] if pcts["99"] else None,
        "instances_started": starts,
        "peak_instances": int(max(inst, default=0)),
        "billable_s": round(billable, 1),
        "startup_p50_ms": startup_p50[0] if startup_p50 else None,
        "startup_quantiles_s": [round(x, 2) for x in startup_q],
    }

    # simulate the same schedule (1-second buckets) on the same settings
    secs = int(max(run["schedule"])) + 1
    counts = [0] * secs
    for a in run["schedule"]:
        counts[int(a)] += 1
    warm = run["work_ms"] + 12  # measured warm overhead is small and constant; the sleep is exact
    obs = Observed(
        latency_p50_ms=warm,
        latency_p95_ms=warm * 1.05,
        startup_s=(measured["startup_p50_ms"] or 2500) / 1000,
        startup_quantiles_s=sorted(startup_q) if all(startup_q) else [],
        cpu_util_mean=0.02,
        cpu_util_p95=0.05,
        concurrency_mean=4,
    )
    cfg = ServiceConfig(
        name=service,
        cpu=1,
        memory_gib=0.5,
        concurrency=4,
        min_instances=0,
        max_instances=10,
        cpu_idle=True,
    )
    sim = simulate(Traffic(step_s=1, requests=counts), obs, cfg)
    # a window longer than the schedule so billing tails are comparable: instances idle-wait up to 15 min; billable time excludes idle
    predicted = {
        "requests": sim.requests,
        "p50_ms": sim.p50_ms,
        "p95_ms": sim.p95_ms,
        "p99_ms": sim.p99_ms,
        "instances_started": sim.instances_started,
        "peak_instances": sim.peak_instances,
        "billable_s": sim.busy_seconds,
        "cold_start_requests": sim.cold_starts,
    }
    out = {
        "run": {k: v for k, v in run.items() if k != "schedule"},
        "measured": measured,
        "predicted": predicted,
    }
    (ROOT / "validation" / f"comparison-{PATTERN}.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("load")
    a.add_argument("--url", required=True)
    a.add_argument("--token")
    a.add_argument("--pattern", default="A", choices=sorted(PATTERNS))
    b = sub.add_parser("compare")
    b.add_argument("--project", required=True)
    b.add_argument("--service", default="rs-target")
    b.add_argument("--pattern", default="A", choices=sorted(PATTERNS))
    args = ap.parse_args()
    use_pattern(args.pattern)
    if args.cmd == "load":
        tok = (
            args.token
            or subprocess.check_output(
                ["gcloud", "auth", "print-identity-token"], text=True
            ).strip()
        )
        asyncio.run(load(args.url, tok))
    else:
        compare(args.project, args.service)
