"""Read-only collection of a Cloud Run service's configuration and metrics (Admin API v2 and Cloud
Monitoring) with Application Default Credentials. Needs roles/run.viewer and roles/monitoring.viewer."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import Observed, ServiceConfig, Snapshot, Traffic

RUN = "https://run.googleapis.com/v2"
MON = "https://monitoring.googleapis.com/v3"
MAX_HOURS = 168


class CollectError(RuntimeError):
    pass


def _session():
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(creds)


def parse_memory(s: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(Mi|Gi|M|G)?", s.strip())
    if not m:
        raise CollectError(f"unrecognised memory '{s}'")
    v, u = float(m.group(1)), m.group(2) or "Mi"
    return {"Mi": v / 1024, "Gi": v, "M": v * 0.000931, "G": v * 0.931}[u]


def parse_config(service: dict[str, Any], name: str, region: str) -> ServiceConfig:
    t = service.get("template", {})
    c = (t.get("containers") or [{}])[0]
    lim = (c.get("resources") or {}).get("limits", {})
    sc = t.get("scaling", {})
    cpu = float(str(lim.get("cpu", "1")).rstrip("m")) / (
        1000 if str(lim.get("cpu", "1")).endswith("m") else 1
    )
    return ServiceConfig(
        name=name,
        region=region,
        cpu=cpu,
        memory_gib=parse_memory(lim.get("memory", "512Mi")),
        concurrency=int(t.get("maxInstanceRequestConcurrency") or 80),
        min_instances=int(sc.get("minInstanceCount") or 0),
        max_instances=int(sc.get("maxInstanceCount") or 100),
        cpu_idle=bool((c.get("resources") or {}).get("cpuIdle", True)),
        startup_cpu_boost=bool((c.get("resources") or {}).get("startupCpuBoost", False)),
    )


def _points(series: list[dict], key: str = "doubleValue") -> list[tuple[datetime, float]]:
    out = []
    for s in series:
        for p in s.get("points", []):
            v = p["value"]
            val = float(v.get("int64Value", v.get(key, 0)))
            ts = datetime.fromisoformat(p["interval"]["endTime"].replace("Z", "+00:00"))
            out.append((ts, val))
    return sorted(out)


def _startup_quantiles(dist) -> list[float]:
    qs = [dist("container/startup_latencies", p) / 1000 for p in ("05", "50", "95", "99")]
    return sorted(qs) if all(q > 0 for q in qs) else []


def collect(
    project: str, service: str, region: str = "us-central1", hours: int = 24, session=None
) -> Snapshot:
    if not 1 <= hours <= MAX_HOURS:
        raise CollectError(f"hours must be between 1 and {MAX_HOURS}")
    s = session or _session()

    def get(url: str, **kw) -> dict[str, Any]:
        r = s.get(url, timeout=60, **kw)
        if r.status_code != 200:
            raise CollectError(f"GET {url.split('/v')[-1][:90]} -> {r.status_code}: {r.text[:160]}")
        return r.json()

    cfg = parse_config(
        get(f"{RUN}/projects/{project}/locations/{region}/services/{service}"), service, region
    )
    end = datetime.now(UTC).replace(second=0, microsecond=0)
    start = end - timedelta(hours=hours)

    def ts(
        metric: str,
        aligner: str,
        period: int,
        reducer: str = "REDUCE_NONE",
        extra: str = "",
        group: str = "",
    ) -> list[dict]:
        flt = f'metric.type="run.googleapis.com/{metric}" AND resource.labels.service_name="{service}"{extra}'
        params = {
            "filter": flt,
            "interval.startTime": start.isoformat().replace("+00:00", "Z"),
            "interval.endTime": end.isoformat().replace("+00:00", "Z"),
            "aggregation.alignmentPeriod": f"{period}s",
            "aggregation.perSeriesAligner": aligner,
            "aggregation.crossSeriesReducer": reducer,
            "view": "FULL",
        }
        if group:
            params["aggregation.groupByFields"] = group
        return get(f"{MON}/projects/{project}/timeSeries", params=params).get("timeSeries", [])

    counts = _points(ts("request_count", "ALIGN_DELTA", 60, "REDUCE_SUM"), "int64Value")
    steps = int((end - start).total_seconds() // 60)
    arr = [0] * steps
    for t, v in counts:
        i = int((t - start).total_seconds() // 60) - 1
        if 0 <= i < steps:
            arr[i] += int(v)

    def dist(metric: str, pct: str) -> float:
        pts = _points(ts(metric, f"ALIGN_PERCENTILE_{pct}", 3600, "REDUCE_MEAN"))
        return sum(v for _, v in pts) / len(pts) if pts else 0.0

    def mean_of(metric: str) -> float:
        pts = _points(ts(metric, "ALIGN_DELTA", 3600, "REDUCE_MEAN"))
        return sum(v for _, v in pts) / len(pts) if pts else 0.0

    p50, p95, p99 = (dist("request_latencies", q) for q in ("50", "95", "99"))
    inst = _points(ts("container/instance_count", "ALIGN_MAX", 60, "REDUCE_SUM"))
    notes = [
        "latency/utilisation percentiles are hourly percentiles averaged over the window (an approximation)"
    ]
    if not any(arr):
        notes.append("no requests in the window")
    obs = Observed(
        latency_p50_ms=p50 or 100.0,
        latency_p95_ms=max(p95, p50 * 1.2) or 300.0,
        latency_p99_ms=p99,
        startup_s=(dist("container/startup_latencies", "50") / 1000) or 2.0,
        startup_quantiles_s=_startup_quantiles(dist),
        cpu_util_mean=mean_of("container/cpu/utilizations") or 0.3,
        cpu_util_p95=dist("container/cpu/utilizations", "95") or 0.6,
        mem_util_p99=dist("container/memory/utilizations", "99") or 0.5,
        concurrency_mean=max(1.0, mean_of("container/max_request_concurrencies") or 1.0),
        instance_peak=int(max((v for _, v in inst), default=1)),
    )
    return Snapshot(
        config=cfg,
        traffic=Traffic(start=start.isoformat(), step_s=60, requests=arr),
        observed=obs,
        source=f"live:{project}/{region}/{service} last {hours}h",
        notes=notes,
    )
