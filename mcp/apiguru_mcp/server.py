"""Build the Apiguru MCP server from the bundled spec.

Every tool is generated from endpoints.json, so adding an endpoint there is
the only step needed to expose it over MCP.

What the tools add on top of a plain proxy, all learned from watching
agents use the first version:

* **Compact payloads.** `product_details` is ~75 KB raw; the compact
  projection is ~4 KB and lists what it left out. `fields=` fetches any of
  it by name; `compact=false` returns everything.
* **Structured errors.** Every failure is JSON with `http_status`,
  `billed`, `retryable` and `next_step` (see errors.py). A 402 carries the
  payment challenge instead of vanishing into "Error executing tool".
* **Output schemas.** Each tool advertises a loose `outputSchema` naming
  the top-level keys it returns.
* **Cache and budget.** Identical calls within a few minutes are served
  from memory; `APIGURU_SESSION_BUDGET_USD` caps a local session's spend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import client as client_module
from .client import (
    ApiguruError,
    GITHUB_ISSUES,
    FEEDBACK_WALL,
    post_feedback,
    access_mode_sync,
    call_endpoint,
    oauth_client_id,
    oauth_subject,
    resolve_api_key,
)
from .errors import structured
from .shaping import (
    COMPACT_LIST_FIELDS,
    COMPACT_PRODUCT_FIELDS,
    DEFAULT_LIST_LIMIT,
    LIST_KEYS,
    KNOWN_LARGE_FIELDS,
    output_model_for,
    shape_list_payload,
    shape_product_payload,
    shape_reviews_payload,
)
from .schema import typed_function
from .spec import (
    api_info,
    conventions,
    endpoints,
    load_spec,
    price_label,
    tool_description,
)

logger = logging.getLogger(__name__)

# Every tool is a read: it fetches Amazon data and changes nothing on the
# caller's side. Clients use these hints for auto-approval (a read-only tool
# can run without a per-call confirmation) and directories require them --
# the Anthropic Connectors Directory flags any tool missing a title and a
# readOnlyHint/destructiveHint. openWorldHint is true because the answer
# comes from the live web, not a closed dataset.
READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

INSTRUCTIONS = """\
Apiguru returns live, structured Amazon marketplace data across 20 country
marketplaces. Data is fetched at request time, not served from a cache.

Choosing a tool:
- One ASIN, full record            -> product_details
- Many ASINs (up to 20)            -> product_details_batch (cheaper per item)
- Reviews and rating breakdown     -> product_reviews
- Keyword discovery                -> search
- Offers, buy box, live stock      -> offers_stock
- Category rankings                -> best_sellers
- Discounted items                 -> deals
- Seller storefront / reputation   -> seller_products, seller_reviews,
                                      seller_profile_batch

Rules that will save you money and failed calls:
- ASINs must be 10 UPPERCASE alphanumeric characters. Normalise before calling.
- Prefer the batch tools over looping single-item tools.
- product_details and product_details_batch return a COMPACT record by
  default and list what they left out under `_omitted_fields`. Ask for more
  with fields="tech_specs,product_information" or compact=false. The full
  record is ~75 KB; do not request it unless you need it.
- Every error is a JSON document with http_status, billed, retryable and
  next_step. Retry only when retryable is true. A 404 IS billed; a 503 is
  not. A 402 means pay, add a key, or sign in.
- Identical calls within a few minutes are answered from a short cache
  (`_cache` marks such answers) and cost nothing extra.
- Call list_capabilities first if you need prices, the marketplace list or
  your access mode; it is free and makes no network request.
- Bullet points and specs describe what Amazon shows for the listing; on
  multi-variant listings they can describe the product family rather than
  the exact variant. A null field means Amazon did not show that field.
- `search` takes ten filters besides the query -- page, sort_by, brand,
  seller_id, category_id, min_price, max_price, product_condition, geo,
  today_deals. `list_capabilities` lists them per tool under `optional`.

Reading a page of results without drowning in it:
- search, best_sellers, deals, seller_products and seller_reviews return
  the first 10 rows as LIGHT records by default. A full page is up to 48
  rows and ~54 KB, which most clients spill to a file. Raise `limit` (0 =
  the whole page), set `compact=false` for every field, or name what you
  want with `fields="asin,product_title,product_price"`. Answers carry
  `_truncated` and `_omitted_fields` so nothing is hidden.
- Filtering beats paging: brand, price range and sort_by cut the page down
  before it is fetched, so the answer is small AND relevant.

Two things about Amazon's own data that will mislead you if you assume
otherwise:
- **Rating counts are per listing family, not per ASIN.** Variants share a
  review pool, so the same 488,443 appears on every colour of one shoe.
  Never read it as "this variant has that many reviews".
- **A variant's title can be the parent's.** A search row's ASIN and title
  can disagree (the URL slug often shows the real variant). When the exact
  variant matters, call product_details on that ASIN -- it is authoritative
  for the ASIN you passed.
- `badges` is the source of truth for Amazon's Choice / Best Seller /
  Overall Pick. `is_amazon_choice` and `is_best_seller` are conveniences
  derived from it.

Found something wrong? Say so -- this API has been fixed more than once
because a caller reported a bad field. Open an issue at
https://github.com/apiguru-app/agent-kit/issues if you have a GitHub
account; otherwise call the free `send_feedback` tool. Neither is billed.
"""

# Called after every tool invocation when the hosted server has somewhere to
# write usage; installed by http_app.py.
usage_logger: Callable[[dict[str, Any]], Awaitable[None]] | None = None


def _mcp_only_params(ep: dict[str, Any]) -> dict[str, Any]:
    """Parameters the MCP layer consumes itself and never forwards upstream."""
    return ep.get("mcp_params", {}) or {}


def _merged_input_schema(ep: dict[str, Any]) -> dict[str, Any]:
    schema = json.loads(json.dumps(ep["input_schema"]))
    extra = _mcp_only_params(ep)
    if extra:
        schema.setdefault("properties", {}).update(extra)
    return schema


def _shape(ep: dict[str, Any], payload: Any, local: dict[str, Any]) -> Any:
    name = ep["name"]
    if name in ("product_details", "product_details_batch"):
        return shape_product_payload(payload, compact=bool(local.get("compact", True)), fields=local.get("fields"))
    if name == "product_reviews":
        return shape_reviews_payload(payload, max_reviews=local.get("max_reviews"))
    if name in LIST_KEYS:
        return shape_list_payload(
            payload,
            tool=name,
            compact=bool(local.get("compact", True)),
            fields=local.get("fields"),
            limit=local.get("limit", DEFAULT_LIST_LIMIT),
        )
    return payload


def _make_impl(ep: dict[str, Any]):
    path = ep["path"]
    local_names = set(_mcp_only_params(ep))

    async def impl(arguments: dict[str, Any]) -> Any:
        local = {k: arguments.pop(k) for k in list(arguments) if k in local_names}
        started = time.monotonic()
        outcome: dict[str, Any] = {"tool": ep["name"], "ok": False, "http_status": None, "cached": False}
        try:
            payload = await call_endpoint(path, arguments)
            outcome["ok"] = True
            outcome["cached"] = isinstance(payload, dict) and bool(payload.get("_cache"))
            return _shape(ep, payload, local)
        except ApiguruError as exc:
            outcome["http_status"] = exc.http_status
            outcome["error"] = exc.detail.get("error", "")[:200]
            raise
        except Exception as exc:  # noqa: BLE001 -- surfaced as structured JSON, never masked
            wrapped = structured(exc)
            outcome["error"] = str(exc)[:200]
            raise wrapped from exc
        finally:
            outcome["duration_ms"] = int((time.monotonic() - started) * 1000)
            _log_usage(outcome)

    return impl


def _log_usage(outcome: dict[str, Any]) -> None:
    if usage_logger is None:
        return
    event = {
        **outcome,
        "route": client_module.request_route.get(),
        "auth_mode": access_mode_sync(),
        "subject": oauth_subject(),
        "oauth_client_id": oauth_client_id(),
        "user_agent": (client_module.request_user_agent.get() or "")[:160],
        "client_ip": client_module.request_client_ip.get(),
    }
    try:
        asyncio.get_running_loop().create_task(_swallow(usage_logger(event)))
    except RuntimeError:
        pass


async def _swallow(coro: Awaitable[None]) -> None:
    try:
        await coro
    except Exception:  # telemetry must never affect a tool call
        logger.debug("usage log failed", exc_info=True)


def build_server(**server_kwargs: Any) -> MCPServer:
    """One MCP server. `server_kwargs` pass straight to MCPServer -- the hosted
    deployment uses them to attach the OAuth authorization server."""
    info = api_info()

    server = MCPServer(
        name="apiguru",
        title="Apiguru Amazon Data",
        version=info["version"],
        website_url="https://apiguru.app",
        instructions=INSTRUCTIONS,
        **server_kwargs,
    )

    for ep in endpoints():
        server.add_tool(
            typed_function(
                name=ep["name"],
                doc=tool_description(ep),
                input_schema=_merged_input_schema(ep),
                impl=_make_impl(ep),
                return_model=output_model_for(ep["name"], ep.get("output_example")),
            ),
            name=ep["name"],
            title=ep["summary"],
            annotations=READ_ONLY,
            meta={"price": price_label(ep), "source": ep["source"]},
        )

    _add_capabilities_tool(server)
    _add_feedback_tool(server)
    return server


def _add_capabilities_tool(server: MCPServer) -> None:
    """A free, offline catalogue tool.

    Agents evaluating whether this API fits their task should not have to
    spend money to find out what it costs or which marketplaces it covers.
    """
    info = api_info()
    conv = conventions()

    async def list_capabilities() -> str:
        """List every Apiguru endpoint with its price, required parameters and
        supported marketplaces, plus your current access mode, cache and
        session budget. Free: answers locally with no network request and no
        charge. Call this before paid tools if you need to plan."""
        catalogue = [
            {
                "tool": ep["name"],
                "summary": ep["summary"],
                "price": price_label(ep),
                "required": ep["input_schema"].get("required", []),
                # Without this an agent reading the catalogue sees only
                # `required` and `mcp_options` and concludes the tool takes
                # nothing else -- one reported exactly that about `search`,
                # which has ten filters.
                "optional": sorted(
                    set(ep["input_schema"].get("properties", {}))
                    - set(ep["input_schema"].get("required", []))
                ),
                "mcp_options": sorted(_mcp_only_params(ep)),
            }
            for ep in endpoints()
        ]
        try:
            key = await resolve_api_key()
            unusable = None
        except ApiguruError as exc:
            key = None
            unusable = exc.detail.get("error")
        if unusable:
            access_mode = f"signed in, but unusable: {unusable}"
        elif key and oauth_subject():
            access_mode = "signed in with an Apiguru account (OAuth); billed to that account"
        elif key:
            access_mode = "keyed (X-API-KEY present; billed to that account)"
        else:
            access_mode = (
                "keyless (free probe budget, then HTTP 402 payment "
                "challenge payable via x402, USDC on Base)"
            )
        budget = client_module.session_budget_usd()
        return json.dumps(
            {
                "api": info["name"],
                "version": info["version"],
                # An agent read a stale catalogue and concluded `search` took
                # no filters. The catalogue is bundled with the package, so it
                # is only as new as the package: say which one is answering and
                # how to move off it.
                "version_note": (
                    f"This catalogue ships inside apiguru-mcp {info['version']} and is answered "
                    "offline, so it is exactly as current as this install. If it is behind the "
                    "latest release (https://github.com/apiguru-app/agent-kit/releases), upgrade "
                    "with uvx apiguru-mcp==<version> or npx apiguru-mcp@<version>, or use the "
                    "hosted server https://mcp.apiguru.app/mcp, which is always current."
                ),
                "authenticated": bool(key),
                "access_mode": access_mode,
                "marketplaces": list(load_spec()["geos"].keys()),
                "asin_format": conv["asin_pattern"],
                "sample_asin": conv["sample_asin"],
                "retry_policy": conv["retry_policy"],
                "error_semantics": conv["error_semantics"],
                "error_format": {
                    "error": "text", "http_status": "int|null", "billed": "bool",
                    "retryable": "bool", "next_step": "text", "payment_challenge": "on 402, keyless",
                },
                "compact_product_fields": list(COMPACT_PRODUCT_FIELDS),
                "compact_list_fields": list(COMPACT_LIST_FIELDS),
                "list_default_limit": DEFAULT_LIST_LIMIT,
                "large_optional_fields": list(KNOWN_LARGE_FIELDS),
                "cache_ttl_seconds": int(client_module.cache_ttl_seconds()),
                "session_budget_usd": str(budget) if budget is not None else None,
                "session_spent_usd": str(client_module.session_spent_usd),
                "sign_in_url": client_module.OAUTH_URL,
                # An agent on a stale plugin could not find anywhere to report
                # a bug: the plugin manifest only carries a repo URL and an
                # email, and its bundled SKILL.md predated the wall. This tool
                # is free, always present and version-independent, so the
                # channels belong here too.
                "feedback": {
                    "preferred": GITHUB_ISSUES,
                    "wall": FEEDBACK_WALL,
                    "wall_post": "POST https://dash.apiguru.app/api/v1/feedback "
                                 "with {\"message\": \"...\", \"category\": "
                                 "\"bug|wish|praise|question|other\"} - no key, never billed",
                    "tool": "send_feedback (free)",
                },
                "tools": catalogue,
            },
            indent=2,
        )

    server.add_tool(
        list_capabilities,
        name="list_capabilities",
        title="List endpoints, prices and marketplaces (free)",
        # Answers from the bundled spec; nothing leaves the machine.
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )


def _add_feedback_tool(server: MCPServer) -> None:
    """Somewhere for the caller to say what is wrong with this API.

    Agents were finding real defects -- brand-only titles, null rating
    counts, welded delivery strings -- and had nowhere to put them. GitHub is
    the better channel because an issue can hold a reply, but an agent with no
    GitHub account still needs a wall. This is that wall.
    """

    async def send_feedback(
        message: str,
        category: str = "other",
        endpoint: str = "",
        agent: str = "",
        contact: str = "",
    ) -> str:
        """Report a bug, ask for a field, or say what would make this API more
        useful. Free: never billed, no API key needed.

        Prefer GitHub if you have an account -- an issue at
        https://github.com/apiguru-app/agent-kit/issues gets a reply on the
        thread, this wall does not. Use this tool when you have no GitHub
        account or nothing to attach one to.

        message:  what happened or what you want. Be specific: the tool, the
                  parameters, the field, what you expected, what you got.
        category: bug | wish | praise | question | other
        endpoint: which tool or path it is about, e.g. "search".
        agent:    what you are, e.g. "acme-pricing-bot/2.1". Optional.
        contact:  a GitHub handle or email if you want a reply. Shown
                  publicly on the wall. Optional.
        """
        if not message or not message.strip():
            return json.dumps(
                {
                    "error": "message is required",
                    "github_issues": GITHUB_ISSUES,
                    "wall": FEEDBACK_WALL,
                },
                indent=2,
            )
        try:
            result = await post_feedback(
                {
                    "message": message.strip(),
                    "category": (category or "other").strip().lower(),
                    "endpoint": (endpoint or "").strip() or None,
                    "agent": (agent or "").strip() or None,
                    "contact": (contact or "").strip() or None,
                    "source": "mcp",
                }
            )
        except ApiguruError as exc:
            return json.dumps(exc.detail, indent=2)
        return json.dumps(result, indent=2)

    server.add_tool(
        send_feedback,
        name="send_feedback",
        title="Report a bug or request a feature (free)",
        annotations=ToolAnnotations(
            # It writes to a public wall, so it is not a read -- but it
            # destroys nothing and costs nothing.
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )


__all__ = ["build_server", "ApiguruError", "usage_logger"]
