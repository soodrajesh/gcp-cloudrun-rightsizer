import json
from importlib import resources

from rightsize.llm import LLM, LLMResult
from rightsize.models import Observed, ServiceConfig, Snapshot, Traffic


def sample(name: str) -> dict:
    return json.loads((resources.files("rightsize") / "samples" / f"{name}.json").read_text())


def small(cfg=None, req=None, obs=None) -> Snapshot:
    """A small, fast snapshot: 3 hours, a 3-minute burst each hour."""
    req = req or ([300, 300, 300] + [0] * 57) * 3
    return Snapshot(
        config=ServiceConfig(
            name="svc",
            **(
                cfg
                or {
                    "cpu": 1,
                    "memory_gib": 0.5,
                    "concurrency": 10,
                    "min_instances": 0,
                    "max_instances": 20,
                    "cpu_idle": False,
                }
            ),
        ),
        traffic=Traffic(requests=req),
        observed=Observed(
            **(
                obs
                or {
                    "latency_p50_ms": 100,
                    "latency_p95_ms": 250,
                    "startup_s": 3,
                    "cpu_util_mean": 0.3,
                    "mem_util_p99": 0.4,
                    "concurrency_mean": 2,
                }
            )
        ),
    )


class ScriptedLLM(LLM):
    name, model = "scripted", "scripted-1"

    def __init__(self, reply=None, *, raises=None):
        self.reply, self.raises, self.calls = reply, raises, 0

    async def complete_json(self, system, user, max_tokens=1200):
        self.calls += 1
        if self.raises:
            raise self.raises
        r = self.reply(json.loads(user)) if callable(self.reply) else self.reply
        return LLMResult(r if isinstance(r, str) else json.dumps(r), 50, 20, 0.0005)
