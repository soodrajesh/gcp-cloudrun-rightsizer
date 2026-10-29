"""CLI.  Exit codes for `recommend`: 0 current settings are optimal, 1 a cheaper config exists, 2 the current config misses the SLO, 3 error.

rightsize collect my-project my-service > snapshot.json
rightsize recommend snapshot.json --llm none
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from .collector import CollectError, collect
from .llm import Budget, LLMError, make_llm
from .models import SLO, Recommendation, Snapshot
from .orchestrator import SnapshotError, recommend


def render(r: Recommendation) -> str:
    def line(label, c):
        x, k = c.config, c.result
        return (
            f"{label:12} ${c.monthly_usd:>8.2f}/mo  cpu {x.cpu:g} mem {x.memory_gib:g}Gi conc {x.concurrency} min {x.min_instances} "
            f"max {x.max_instances} {'request-based' if x.cpu_idle else 'instance-based'}{' boost' if x.startup_cpu_boost else ''}\n"
            f"{'':12} cold starts {k.cold_start_fraction:.2%}  p95 {k.p95_ms:.0f} ms  peak instances {k.peak_instances}  "
            f"dropped {k.drop_fraction:.2%}  {'meets SLO' if c.feasible else 'MISSES SLO: ' + '; '.join(c.violations)}"
        )

    out = [f"{r.service}: {r.summary}", "", line("current", r.current), line("recommended", r.best)]
    if r.alternatives:
        out += ["", "Other feasible options:"] + [
            f"  ${a.monthly_usd:.2f}/mo  " + ", ".join(rd for rd in _short(a))
            for a in r.alternatives[:3]
        ]
    if r.findings:
        out += ["", "Findings:"] + [f"  [{f.severity}] {f.id} {f.title}" for f in r.findings]
    if r.commands:
        out += ["", "Apply with:"] + [f"  {c}" for c in r.commands]
    if r.note:
        out += ["", f"note: {r.note}"]
    return "\n".join(out + [f"! {d}" for d in r.degraded])


def _short(c) -> list[str]:
    x = c.config
    return [
        f"cpu {x.cpu:g}",
        f"{x.memory_gib:g}Gi",
        f"conc {x.concurrency}",
        f"min {x.min_instances}",
        "request-based" if x.cpu_idle else "instance-based",
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rightsize")
    sub = ap.add_subparsers(dest="cmd", required=True)
    co = sub.add_parser(
        "collect", help="read a service's config and metrics (read-only) and print a snapshot"
    )
    co.add_argument("project")
    co.add_argument("service")
    co.add_argument("--region", default="us-central1")
    co.add_argument("--hours", type=int, default=24)
    rc = sub.add_parser("recommend", help="recommend settings for a snapshot file (or - for stdin)")
    rc.add_argument("snapshot")
    rc.add_argument("--llm", default="none", choices=["none", "mock", "vertex", "openai"])
    rc.add_argument("--max-cold-start", type=float, default=0.01)
    rc.add_argument("--p95-ms", type=float)
    rc.add_argument("--format", default="text", choices=["text", "json"])
    a = ap.parse_args(argv)
    try:
        if a.cmd == "collect":
            print(collect(a.project, a.service, a.region, a.hours).model_dump_json(indent=1))
            return 0
        raw = sys.stdin.read() if a.snapshot == "-" else Path(a.snapshot).read_text()
        snap = Snapshot.model_validate(json.loads(raw))
        rec = asyncio.run(
            recommend(
                snap,
                make_llm(a.llm),
                SLO(max_cold_start_fraction=a.max_cold_start, p95_latency_ms=a.p95_ms),
                budget=Budget(),
            )
        )
    except (OSError, ValueError, ValidationError, SnapshotError, LLMError, CollectError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    print(rec.model_dump_json(indent=1) if a.format == "json" else render(rec))
    return 2 if not rec.current.feasible else 1 if rec.changed else 0


if __name__ == "__main__":
    sys.exit(main())
