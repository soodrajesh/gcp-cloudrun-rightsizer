"""A tiny service used by validation/ to produce real Cloud Run metrics with a known shape:
GET /work?ms=N waits N milliseconds without using CPU (so latency is predictable and CPU contention is nil)."""

import asyncio
import os
import time

from fastapi import FastAPI

app = FastAPI()
STARTED = time.time()


@app.get("/work")
async def work(ms: int = 100):
    await asyncio.sleep(min(max(ms, 0), 10_000) / 1000)
    return {"ok": True, "pid": os.getpid(), "uptime_s": round(time.time() - STARTED, 1)}
