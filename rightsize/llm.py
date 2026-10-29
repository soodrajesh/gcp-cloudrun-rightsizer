"""Model providers behind one tiny interface: ask for JSON, get JSON text plus usage.

mock    deterministic, offline; used by tests and local demos
vertex  Gemini on Vertex AI via Application Default Credentials (the deployed provider)
openai  test/eval-only second vendor; needs OPENAI_API_KEY and the `openai` extra
none    no model: rules only"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass


@dataclass
class LLMResult:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    pass


class Budget:
    """Per-review ceiling on model calls and spend. Shared by every agent in one review."""

    def __init__(self, max_calls: int = 12, max_cost_usd: float = 0.05):
        self.max_calls, self.max_cost_usd = max_calls, max_cost_usd
        self.calls, self.cost_usd = 0, 0.0
        self._lock = asyncio.Lock()

    async def reserve(self) -> None:
        async with self._lock:
            if self.calls >= self.max_calls:
                raise BudgetExceeded(f"per-review limit of {self.max_calls} model calls reached")
            if self.cost_usd >= self.max_cost_usd:
                raise BudgetExceeded(f"per-review spend limit ${self.max_cost_usd:.3f} reached")
            self.calls += 1

    async def record(self, usd: float) -> None:
        async with self._lock:
            self.cost_usd += usd


class LLM:
    name = "none"
    model = "none"

    async def complete_json(self, system: str, user: str, max_tokens: int = 1200) -> LLMResult:
        raise LLMError("no model configured")


class NullLLM(LLM):
    pass


class MockLLM(LLM):
    """Deterministic stand-in: explains with numbers taken from the input, proposes a modest change."""

    name, model = "mock", "mock-1"

    async def complete_json(self, system: str, user: str, max_tokens: int = 1200) -> LLMResult:
        p = json.loads(user)
        if p.get("task") == "explain":
            c, b = p["current"], p["best"]
            return LLMResult(
                json.dumps(
                    {
                        "note": f"{p['service']} costs ${c['monthly_usd']} a month today and ${b['monthly_usd']} on the recommended settings."
                    }
                ),
                100,
                30,
            )
        if p.get("task") == "propose":
            keys = (
                "cpu",
                "memory_gib",
                "concurrency",
                "min_instances",
                "max_instances",
                "cpu_idle",
                "startup_cpu_boost",
            )
            cfg = {k: p["current_config"][k] for k in keys}
            cfg["min_instances"] = 0
            return LLMResult(
                json.dumps({"config": cfg, "rationale": "Scale to zero overnight."}), 100, 40
            )
        if p.get("task") == "summarize":
            return LLMResult(json.dumps({"summary": p["fallback"]}), 50, 20)
        return LLMResult("{}", 10, 2)


class VertexLLM(LLM):
    name = "vertex"
    # USD per 1M tokens (input, output); an estimate used only for budget guards.
    PRICES = {"gemini-2.5-flash": (0.30, 2.50), "gemini-2.5-flash-lite": (0.10, 0.40)}

    def __init__(
        self, project: str, location: str = "us-central1", model: str = "gemini-2.5-flash"
    ):
        from google import genai

        self.model = model
        self._client = genai.Client(vertexai=True, project=project, location=location)

    async def complete_json(self, system: str, user: str, max_tokens: int = 1200) -> LLMResult:
        from google.genai import types

        try:
            r = await self._client.aio.models.generate_content(
                model=self.model,
                contents=user,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    temperature=0.0,
                    max_output_tokens=max_tokens,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
        except Exception as e:  # SDK raises a family of API errors; callers only need one type
            raise LLMError(f"vertex: {type(e).__name__}: {str(e)[:200]}") from e
        u = r.usage_metadata
        tin, tout = (u.prompt_token_count or 0, u.candidates_token_count or 0) if u else (0, 0)
        pin, pout = self.PRICES.get(self.model, (0.30, 2.50))
        return LLMResult(r.text or "", tin, tout, (tin * pin + tout * pout) / 1e6)


class OpenAILLM(LLM):
    name = "openai"
    PRICES = {"gpt-5-nano": (0.05, 0.40), "gpt-5-mini": (0.25, 2.00)}

    def __init__(self, model: str = "gpt-5-nano"):
        from openai import AsyncOpenAI

        self.model = model
        self._client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])

    async def complete_json(self, system: str, user: str, max_tokens: int = 1200) -> LLMResult:
        try:
            r = await self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={"type": "json_object"},
                max_completion_tokens=max(max_tokens, 2000),  # reasoning tokens count against it
                reasoning_effort="minimal",
            )
        except Exception as e:
            raise LLMError(f"openai: {type(e).__name__}: {str(e)[:200]}") from e
        u = r.usage
        tin, tout = (u.prompt_tokens, u.completion_tokens) if u else (0, 0)
        pin, pout = self.PRICES.get(self.model, (0.25, 2.0))
        return LLMResult(
            r.choices[0].message.content or "", tin, tout, (tin * pin + tout * pout) / 1e6
        )


def make_llm(provider: str, model: str | None = None) -> LLM:
    provider = (provider or "none").lower()
    if provider == "none":
        return NullLLM()
    if provider == "mock":
        return MockLLM()
    if provider == "vertex":
        project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("RIGHTSIZE_GCP_PROJECT")
        if not project:
            raise LLMError("set GOOGLE_CLOUD_PROJECT for the vertex provider")
        return VertexLLM(
            project,
            os.environ.get("RIGHTSIZE_GCP_LOCATION", "us-central1"),
            model or "gemini-2.5-flash",
        )
    if provider == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            raise LLMError("set OPENAI_API_KEY for the openai provider")
        return OpenAILLM(model or "gpt-5-nano")
    raise LLMError(f"unknown provider '{provider}'")
