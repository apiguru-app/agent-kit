"""Access to the bundled API specification.

The JSON here is written by agent-kit/spec/generate.py from the canonical
endpoints.json, so it is never edited by hand and cannot drift from the
OpenAPI document or llms.txt.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_FILE = Path(__file__).parent / "data" / "endpoints.json"


@lru_cache(maxsize=1)
def load_spec() -> dict[str, Any]:
    with DATA_FILE.open(encoding="utf-8") as fh:
        return json.load(fh)


def api_info() -> dict[str, Any]:
    return load_spec()["api"]


def conventions() -> dict[str, Any]:
    return load_spec()["conventions"]


def endpoints() -> list[dict[str, Any]]:
    """Every callable endpoint. Deprecated path aliases are not included."""
    return load_spec()["endpoints"]


def price_label(ep: dict[str, Any]) -> str:
    if ep["price_model"] == "per_item":
        return f"${ep['unit_price_usd']} per item (max {ep['max_items']})"
    return f"${ep['price_usd']} per call"


def tool_description(ep: dict[str, Any]) -> str:
    """The text the model reads when deciding whether to call this tool.

    Deliberately includes price and the billing-relevant error semantics:
    an agent choosing between tools should know that a 404 costs money and
    a 503 does not.
    """
    parts = [ep["description"], f"Price: {price_label(ep)}."]
    if ep.get("notes"):
        parts.append(ep["notes"])
    return " ".join(parts)
