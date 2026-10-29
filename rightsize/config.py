from __future__ import annotations

import os
from dataclasses import dataclass, field


def _list(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(x.strip() for x in os.environ.get(name, default).split(",") if x.strip())


@dataclass
class Settings:
    provider: str = field(default_factory=lambda: os.environ.get("RIGHTSIZE_LLM", "mock"))
    model: str | None = field(default_factory=lambda: os.environ.get("RIGHTSIZE_MODEL") or None)
    allowed_providers: tuple[str, ...] = field(
        default_factory=lambda: _list("RIGHTSIZE_ALLOWED_PROVIDERS", "mock,none")
    )
    live_projects: tuple[str, ...] = field(default_factory=lambda: _list("RIGHTSIZE_LIVE_PROJECTS"))
    daily_budget_usd: float = field(
        default_factory=lambda: float(os.environ.get("RIGHTSIZE_DAILY_BUDGET_USD", 0.5))
    )
    review_budget_usd: float = 0.05
    max_llm_calls: int = 6
    max_body_bytes: int = 4_000_000
