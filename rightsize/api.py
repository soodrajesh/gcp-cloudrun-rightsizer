"""HTTP API and viewer. Auth is Cloud Run IAM. Recommendations are in memory only."""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ValidationError

from . import __version__
from .collector import CollectError, collect
from .config import Settings
from .llm import Budget, LLMError, make_llm
from .models import SLO, Recommendation, Snapshot
from .orchestrator import SnapshotError, recommend

SAMPLES = resources.files("rightsize") / "samples"


class Live(BaseModel):
    project: str
    service: str
    region: str = "us-central1"
    hours: int = 24


class Req(BaseModel):
    snapshot: dict | None = None
    sample: str | None = None
    live: Live | None = None
    slo: SLO = SLO()
    llm: str | None = None


class _Mem:
    def __init__(self, cap: int = 40):
        self._d: OrderedDict[str, Recommendation] = OrderedDict()
        self._cap, self._lock, self._spend = cap, threading.Lock(), {}

    def save(self, r):
        with self._lock:
            self._d[r.id] = r
            while len(self._d) > self._cap:
                self._d.popitem(last=False)

    def get(self, i):
        return self._d.get(i)

    def recent(self, n):
        return list(reversed(self._d.values()))[:n]

    def spend(self):
        return self._spend.get(datetime.now(UTC).strftime("%Y%m%d"), 0.0)

    def add(self, usd):
        k = datetime.now(UTC).strftime("%Y%m%d")
        with self._lock:
            self._spend[k] = self._spend.get(k, 0.0) + usd


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg, mem = settings or Settings(), _Mem()
    app = FastAPI(title="rightsize", version=__version__)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (resources.files("rightsize") / "ui.html").read_text()

    @app.get("/api/config")
    def config():
        return {
            "version": __version__,
            "provider": cfg.provider,
            "allowed_providers": cfg.allowed_providers,
            "live_projects": cfg.live_projects,
            "daily_budget_usd": cfg.daily_budget_usd,
            "spend_today_usd": round(mem.spend(), 6),
        }

    @app.get("/api/samples")
    def samples():
        return sorted(p.name.removesuffix(".json") for p in Path(str(SAMPLES)).glob("*.json"))

    @app.get("/api/samples/{name}")
    def sample(name: str):
        f = SAMPLES / f"{name}.json"
        if not re.fullmatch(r"[a-z0-9_]+", name) or not f.is_file():
            raise HTTPException(404, "no such sample")
        return json.loads(f.read_text())

    @app.post("/api/recommendations", response_model=Recommendation)
    async def create(req: Request):
        raw = await req.body()
        if len(raw) > cfg.max_body_bytes:
            raise HTTPException(413, "too large")
        try:
            body = Req.model_validate_json(raw)
        except ValueError as e:
            raise HTTPException(422, f"invalid request: {str(e)[:300]}") from e
        if sum(x is not None for x in (body.snapshot, body.sample, body.live)) != 1:
            raise HTTPException(422, "send exactly one of 'snapshot', 'sample' or 'live'")
        provider = body.llm or cfg.provider
        if provider not in cfg.allowed_providers:
            raise HTTPException(
                403, f"provider '{provider}' not allowed here ({', '.join(cfg.allowed_providers)})"
            )
        notes = []
        if provider not in ("none", "mock") and mem.spend() >= cfg.daily_budget_usd:
            provider = "none"
            notes.append(
                f"daily model budget ${cfg.daily_budget_usd:.2f} reached; no model commentary"
            )
        try:
            if body.live:
                if body.live.project not in cfg.live_projects:
                    raise HTTPException(
                        403, f"live analysis of '{body.live.project}' is not enabled"
                    )
                snap = collect(
                    body.live.project, body.live.service, body.live.region, body.live.hours
                )
            elif body.sample:
                f = SAMPLES / f"{body.sample}.json"
                if not re.fullmatch(r"[a-z0-9_]+", body.sample) or not f.is_file():
                    raise HTTPException(404, "no such sample")
                snap = Snapshot.model_validate_json(f.read_text())
            else:
                snap = Snapshot.model_validate(body.snapshot)
        except CollectError as e:
            raise HTTPException(502, str(e)) from e
        except ValidationError as e:
            raise HTTPException(422, f"invalid snapshot: {str(e)[:300]}") from e
        try:
            llm = make_llm(provider, cfg.model if provider == cfg.provider else None)
        except LLMError as e:
            raise HTTPException(503, str(e)) from e
        try:
            rec = await recommend(
                snap, llm, body.slo, budget=Budget(cfg.max_llm_calls, cfg.review_budget_usd)
            )
        except SnapshotError as e:
            raise HTTPException(422, str(e)) from e
        rec.degraded = notes + rec.degraded
        mem.add(sum(s.cost_usd for s in rec.trace))
        mem.save(rec)
        return rec

    @app.get("/api/recommendations", response_model=list[Recommendation])
    def listing(limit: int = 20):
        return mem.recent(max(1, min(limit, 40)))

    @app.get("/api/recommendations/{rid}", response_model=Recommendation)
    def get(rid: str):
        r = mem.get(rid)
        if r is None:
            raise HTTPException(404, "no such recommendation")
        return r

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):
        logging.getLogger("rightsize").exception("unhandled error", exc_info=exc)
        return JSONResponse({"detail": "internal error"}, status_code=500)

    return app


def app_factory() -> FastAPI:
    return create_app()
