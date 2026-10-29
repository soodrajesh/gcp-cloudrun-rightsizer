from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

SECONDS_PER_MONTH = 30 * 86400


@lru_cache
def table() -> dict[str, Any]:
    return json.loads((resources.files("rightsize") / "data" / "pricing.json").read_text())


def for_region(region: str) -> dict[str, Any]:
    regions = table()["regions"]
    if region not in regions:
        raise KeyError(
            f"no pricing for region '{region}'; run scripts/fetch_pricing.py or add it to data/pricing.json"
        )
    return regions[region]
