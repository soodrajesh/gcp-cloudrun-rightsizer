from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ServiceConfig(BaseModel):
    name: str
    region: str = "us-central1"
    cpu: float = 1.0  # vCPU per instance
    memory_gib: float = 0.5
    concurrency: int = Field(default=80, ge=1, le=1000)
    min_instances: int = Field(default=0, ge=0)
    max_instances: int = Field(default=100, ge=1)
    cpu_idle: bool = True  # True = request-based billing (CPU only while handling requests); False = instance-based
    startup_cpu_boost: bool = False

    @model_validator(mode="after")
    def _check(self):
        if self.min_instances > self.max_instances:
            raise ValueError("min_instances must not exceed max_instances")
        if self.cpu < 1 and self.concurrency != 1:
            raise ValueError("a service with less than 1 vCPU must use concurrency 1")
        return self


class Traffic(BaseModel):
    start: str = ""
    step_s: int = Field(default=60, ge=1)
    requests: list[int]  # request count per step

    @property
    def window_s(self) -> int:
        return self.step_s * len(self.requests)

    @property
    def total(self) -> int:
        return sum(self.requests)


class Observed(BaseModel):
    """What was measured on the current configuration (from Cloud Monitoring, or supplied)."""

    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float = 0.0
    startup_s: float = 2.0  # median container startup latency
    startup_quantiles_s: list[float] = Field(
        default_factory=list
    )  # startup latency at p5,p50,p95,p99 (optional, ascending)
    cpu_util_mean: float = (
        0.3  # mean CPU utilisation of an instance while it is handling requests (0-1)
    )
    cpu_util_p95: float = 0.6
    mem_util_p99: float = 0.5
    concurrency_mean: float = 1.0  # mean in-flight requests per busy instance
    instance_peak: int = 1
    cold_starts_observed: int | None = None


class Snapshot(BaseModel):
    config: ServiceConfig
    traffic: Traffic
    observed: Observed
    source: str = ""
    notes: list[str] = Field(default_factory=list)


class SLO(BaseModel):
    max_cold_start_fraction: float = 0.01
    p95_latency_ms: float | None = None  # default: 1.25 x observed p95
    max_drop_fraction: float = 0.001


class Cost(BaseModel):
    cpu_usd: float = 0.0
    memory_usd: float = 0.0
    idle_min_instances_usd: float = 0.0
    requests_usd: float = 0.0

    @property
    def total(self) -> float:
        return round(
            self.cpu_usd + self.memory_usd + self.idle_min_instances_usd + self.requests_usd, 4
        )


class SimResult(BaseModel):
    requests: int
    cold_starts: int
    cold_start_fraction: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    dropped: int
    drop_fraction: float
    peak_instances: int
    instances_started: int = 0
    mean_instances: float
    instance_seconds: float
    busy_seconds: float
    mean_concurrency: float = 1.0  # service-seconds per busy second
    window_s: int
    cost_month: Cost  # window scaled to 30 days; free tier ignored


class Candidate(BaseModel):
    config: ServiceConfig
    result: SimResult
    feasible: bool
    violations: list[str] = Field(default_factory=list)
    monthly_usd: float


class Finding(BaseModel):
    id: str
    severity: Literal["info", "low", "medium", "high"]
    title: str
    detail: str = ""


class TraceStep(BaseModel):
    agent: str
    action: str
    status: Literal["ok", "skipped", "error"] = "ok"
    start_ms: int = 0
    duration_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    note: str = ""


class Recommendation(BaseModel):
    id: str
    created_at: str
    service: str
    source: str
    provider: str
    model: str
    summary: str
    current: Candidate
    best: Candidate
    saving_month_usd: float
    saving_pct: float
    changed: list[str]
    alternatives: list[Candidate] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    terraform: str = ""
    slo: SLO
    note: str = ""  # validated model explanation
    dropped: list[dict[str, str]] = Field(default_factory=list)
    degraded: list[str] = Field(default_factory=list)
    trace: list[TraceStep] = Field(default_factory=list)
    traffic_preview: list[int] = Field(default_factory=list)
    duration_ms: int = 0
    assumptions: dict[str, Any] = Field(default_factory=dict)
