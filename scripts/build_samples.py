"""Generate synthetic but realistic snapshots in rightsize/samples/ (24h, per-minute requests).
They are illustrations: the validation experiment (validation/) is what compares the simulator to a real service.

    .venv/bin/python scripts/build_samples.py
"""

import json
import math
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "rightsize" / "samples"
R = random.Random(11)


def diurnal(base, amp, peak_min=840):
    return [
        max(
            0,
            int(
                base
                + amp * max(0.0, math.cos((m - peak_min) / 1440 * 2 * math.pi)) ** 2
                + R.gauss(0, base * 0.1)
            ),
        )
        for m in range(1440)
    ]


def snap(name, cfg, req, obs, note):
    return {
        "config": {"name": name, "region": "us-central1", **cfg},
        "traffic": {"start": "2026-10-28T00:00:00+00:00", "step_s": 60, "requests": req},
        "observed": obs,
        "source": f"synthetic: {note}",
        "notes": ["synthetic illustration, not measured"],
    }


S = {}
S["web_overprovisioned"] = snap(
    "web",
    {
        "cpu": 2,
        "memory_gib": 2,
        "concurrency": 80,
        "min_instances": 2,
        "max_instances": 50,
        "cpu_idle": False,
    },
    diurnal(40, 260),
    {
        "latency_p50_ms": 120,
        "latency_p95_ms": 400,
        "startup_s": 3,
        "cpu_util_mean": 0.15,
        "cpu_util_p95": 0.3,
        "mem_util_p99": 0.3,
        "concurrency_mean": 3,
        "instance_peak": 2,
    },
    "a web app on 2 vCPU with 2 always-on instances and a day/night traffic curve",
)
bursty = [0] * 1440
for h in range(24):
    for k in range(3):  # a 3-minute burst at the top of every hour
        bursty[h * 60 + k] = R.randint(900, 1400)
for m in range(1440):
    bursty[m] += R.randint(0, 4)
S["api_hourly_bursts"] = snap(
    "partner-api",
    {
        "cpu": 1,
        "memory_gib": 0.5,
        "concurrency": 4,
        "min_instances": 0,
        "max_instances": 20,
        "cpu_idle": True,
    },
    bursty,
    {
        "latency_p50_ms": 80,
        "latency_p95_ms": 220,
        "startup_s": 4,
        "cpu_util_mean": 0.55,
        "cpu_util_p95": 0.85,
        "mem_util_p99": 0.45,
        "concurrency_mean": 3,
        "instance_peak": 20,
    },
    "a partner API hit by a burst at the top of every hour; cold starts matter",
)
idle = [1 if m % 97 == 0 else 0 for m in range(1440)]
S["internal_tool_idle"] = snap(
    "admin-tool",
    {
        "cpu": 1,
        "memory_gib": 1,
        "concurrency": 80,
        "min_instances": 1,
        "max_instances": 3,
        "cpu_idle": False,
    },
    idle,
    {
        "latency_p50_ms": 200,
        "latency_p95_ms": 500,
        "startup_s": 5,
        "cpu_util_mean": 0.05,
        "cpu_util_p95": 0.1,
        "mem_util_p99": 0.2,
        "concurrency_mean": 1,
        "instance_peak": 1,
    },
    "an internal tool used a handful of times a day with an always-on instance",
)
batch = [0] * 1440
for m in range(120, 150):
    batch[m] = R.randint(300, 500)
S["nightly_batch"] = snap(
    "etl-trigger",
    {
        "cpu": 2,
        "memory_gib": 4,
        "concurrency": 10,
        "min_instances": 1,
        "max_instances": 30,
        "cpu_idle": False,
    },
    batch,
    {
        "latency_p50_ms": 900,
        "latency_p95_ms": 2500,
        "startup_s": 6,
        "cpu_util_mean": 0.6,
        "cpu_util_p95": 0.9,
        "mem_util_p99": 0.55,
        "concurrency_mean": 8,
        "instance_peak": 30,
    },
    "a nightly 30-minute burst of heavy requests, otherwise silent",
)
S["checkout_underprovisioned"] = snap(
    "checkout",
    {
        "cpu": 1,
        "memory_gib": 0.5,
        "concurrency": 20,
        "min_instances": 0,
        "max_instances": 1,
        "cpu_idle": True,
    },
    diurnal(40, 650),
    {
        "latency_p50_ms": 1500,
        "latency_p95_ms": 3000,
        "startup_s": 2.5,
        "cpu_util_mean": 0.55,
        "cpu_util_p95": 0.9,
        "mem_util_p99": 0.9,
        "concurrency_mean": 15,
        "instance_peak": 1,
    },
    "a slow checkout service capped at one instance that runs close to its memory limit",
)

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for n, d in S.items():
        (OUT / f"{n}.json").write_text(json.dumps(d) + "\n")
    print(f"wrote {len(S)} samples")
