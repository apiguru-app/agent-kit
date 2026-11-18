"""Build the Apiguru MCP server from the bundled spec.

Every tool is generated from endpoints.json, so adding an endpoint there is
the only step needed to expose it over MCP.

What the tools add on top of a plain proxy, all learned from watching
agents use the first version:

* **Compact payloads.** `product_details` is ~75 KB raw; the compact
  projection is ~4 KB and lists what it left out. `fields=` fetches any of
  it by name; `compact=false` returns everything.
* **Structured errors.** Every failure is JSON with `http_status`,
  `billed`, `retryable` and `next_step` (see errors.py).
* **Payment in-band.** A keyless 402 is the x402 MCP transport's
  PaymentRequired result; the same call resent with `_meta["x402/payment"]`
  is paid through the gateway and answers with
  `_meta["x402/payment-response"]`. A caller never has to leave MCP to pay.
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

import pydantic_core
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from . import __version__
from . import client as client_module
from . import identifiers
from .client import (
    ApiguruError,
    GITHUB_ISSUES,
    FEEDBACK_WALL,
    post_feedback,
    access_mode_sync,
    fetch_endpoint,
    oauth_client_id,
    oauth_subject,
    resolve_api_key,
)
from .errors import PaymentRequiredError, structured

# The x402 MCP transport's keys (x402 transports-v2/mcp).
PAYMENT_META_KEY = "x402/payment"
PAYMENT_RESPONSE_META_KEY = "x402/payment-response"
from .shaping import (
    COMPACT_LIST_FIELDS,
    COMPACT_BATCH_FIELDS,
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
Apiguru returns live, structured Amazon marketplace data across 23 country
marketplaces. Data is fetched at request time, not served from a cache.

Choosing a tool:
- One ASIN, full record            -> product_details
- Many ASINs (up to 20)            -> product_details_batch (no ranks,
                                      category path or merchant id)
- Reviews and rating breakdown     -> product_reviews
- Keyword discovery                -> search
- Offers, buy box, live stock      -> offers_stock
- Category rankings                -> best_sellers
- Discounted items                 -> deals
- Seller storefront / reputation   -> seller_products, seller_reviews,
                                      seller_profile_batch
- Prices, marketplaces, access mode -> list_capabilities (free, makes no
                                      network request)

Rules that will save you money and failed calls:
- Pass an ASIN (10 characters) or an Amazon product URL; lower case is
  fine. A URL's domain sets the marketplace unless `geo` says otherwise;
  what was rewritten is listed under `_input_interpreted`.
- Every error is a JSON document with http_status, billed, retryable and
  next_step. Retry only when retryable is true. A 404 IS billed; a 503 is
  not.
- Prefer the batch tools over looping single-item tools: fewer calls,
  and cheaper per item without an account.
- When the free calls run out, a tool returns an x402 PaymentRequired
  result (`x402Version`, `accepts`). An x402-capable MCP client pays it and
  resends the same call with the payment in `_meta["x402/payment"]`.
  Without a wallet, ask the user to connect
  https://mcp.apiguru.app/account and sign in, then repeat the same call.
- Identical calls within a few minutes are answered from a short cache
  (`_cache` marks such answers) and cost nothing extra.

Keeping answers small:
- product_details and product_details_batch return a COMPACT record by
  default and list what they left out under `_omitted_fields`. Ask for
  more with fields= or compact=false; the full record is ~75 KB.
- Variants come as `variation_summary`. The ASIN-keyed maps (`variation`,
  `product_variations`) can be 50-130 KB on big families; ask for one by
  name only when you must resolve a value to a sibling ASIN.
- search, best_sellers, deals, seller_products and seller_reviews return
  the first 10 rows as LIGHT records by default. Raise `limit` (0 = the
  whole page), set `compact=false`, or name what you want with
  `fields="asin,product_title,product_price"`. `_truncated` gives the real
  row count.
- Filtering beats paging. Each tool's description lists its filters, and
  every list answer says which took effect (`filters_applied`), which the
  marketplace could not apply (`filters_ignored`) and what it offers
  (`available_filters`).

Amazon's own data will mislead you if you assume otherwise:
- Rating counts are per listing family, not per ASIN: variants share one
  review pool. Never read a count as "this variant has that many reviews".
- A search row's title can be the parent's. When the exact variant
  matters, call product_details on that ASIN.
- `badges` is the source of truth for Amazon's Choice / Best Seller /
  Overall Pick.
- Bullet points and specs can describe the product family rather than the
  exact variant. A null field means Amazon did not show that field.

Found something wrong? Open an issue at
https://github.com/apiguru-app/agent-kit/issues, or call the free
`send_feedback` tool. Neither is billed.
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
    # ASIN parameters also take an Amazon URL or a lower-case ASIN; the
    # tool normalises them (identifiers.py) and validates the result.
    props = schema.get("properties", {})
    for name in identifiers.ASIN_PARAMS:
        if name in props:
            props[name] = identifiers.relaxed_schema(name, props[name])
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


def _payment_from(context: Any) -> Any:
    """`_meta["x402/payment"]` of the tools/call being served, if any.

    A tool called directly (tests, a nested call) has no request context.
    """
    try:
        params = context.request_context.params
    except Exception:
        return None
    meta = params.get("_meta") if isinstance(params, dict) else None
    return meta.get(PAYMENT_META_KEY) if isinstance(meta, dict) else None


def _payment_required_result(document: dict[str, Any]) -> CallToolResult:
    """The x402 MCP transport's PaymentRequired: the same JSON as
    structuredContent and as the first text block."""
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(document))],
        structured_content=document,
        is_error=True,
    )


def _paid_result(shaped: Any, receipt: dict[str, Any], structured_output: bool) -> CallToolResult:
    """What the SDK would have built from `shaped`, plus the settlement
    receipt, which only a CallToolResult can carry."""
    text = pydantic_core.to_json(shaped, fallback=str, indent=2).decode()
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=shaped if structured_output and isinstance(shaped, dict) else None,
        _meta={PAYMENT_RESPONSE_META_KEY: receipt},
    )


def _make_impl(ep: dict[str, Any]):
    path = ep["path"]
    local_names = set(_mcp_only_params(ep))
    structured_output = output_model_for(ep["name"], ep.get("output_example")) is not None

    async def impl(arguments: dict[str, Any], context: Any = None) -> Any:
        local = {k: arguments.pop(k) for k in list(arguments) if k in local_names}
        if ep.get("upstream_limit"):
            # Let the upstream fetch only what will be shown (best-sellers:
            # <= 30 rows skips a second, sequential fetch of ranks 31-50).
            wanted = local.get("limit", DEFAULT_LIST_LIMIT)
            if isinstance(wanted, int) and wanted > 0:
                arguments["limit"] = wanted
        payment = _payment_from(context)
        started = time.monotonic()
        outcome: dict[str, Any] = {"tool": ep["name"], "ok": False, "http_status": None, "cached": False}
        if payment is not None:
            outcome["payment"] = "presented"
        try:
            interpreted = identifiers.normalise(arguments, load_spec()["geos"])
            fetched = await fetch_endpoint(path, arguments, payment=payment)
            payload = fetched.payload
            outcome["ok"] = True
            outcome["cached"] = isinstance(payload, dict) and bool(payload.get("_cache"))
            shaped = _shape(ep, payload, local)
            if interpreted and isinstance(shaped, dict):
                shaped["_input_interpreted"] = interpreted
            if fetched.payment_response is None:
                return shaped
            outcome["payment"] = "settled"
            return _paid_result(shaped, fetched.payment_response, structured_output)
        except PaymentRequiredError as exc:
            outcome["http_status"] = 402
            outcome["error"] = str(exc.document.get("error", ""))[:200]
            if payment is not None:
                outcome["payment"] = "refused"
            return _payment_required_result(exc.document)
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
        # A call paid in-band is its own mode: it is revenue, not a free call.
        "auth_mode": "x402" if outcome.get("payment") == "settled" else access_mode_sync(),
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
            meta={"price": price_label(ep)},
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
        supported marketplaces, plus your current access mode and cache.
        Free: answers locally with no network request and no charge. Call
        this before paid tools if you need to plan."""
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
                "keyless (free probe budget, then an x402 PaymentRequired result, payable "
                "in-band over MCP via _meta[\"x402/payment\"] in USDC on Base, Polygon, "
                "Arbitrum or Avalanche)"
            )
        budget = client_module.session_budget_usd()
        # The HTTP middleware sets the route on every hosted request; over
        # stdio it stays unset.
        hosted = client_module.request_route.get() is not None
        if hosted:
            # A hosted caller was told to "use the hosted server, which is
            # always current" while already on it.
            version_note = (
                "You are on the hosted server (mcp.apiguru.app), which always runs the latest "
                f"release: apiguru-mcp {info['version']}. There is nothing to upgrade."
            )
            # One process serves every caller here, so the spend counter is
            # everyone's total since the last restart: a signed-in caller saw
            # 0.1560 before making a single call. It is not their number and
            # not theirs to see. A session budget is a local-install setting.
            session: dict[str, Any] = {}
        else:
            version_note = (
                f"This catalogue ships inside apiguru-mcp {info['version']} and is answered "
                "offline, so it is exactly as current as this install. If it is behind the "
                "latest release (https://github.com/apiguru-app/agent-kit/releases), upgrade "
                "with uvx apiguru-mcp==<version> or npx apiguru-mcp@<version>, or use the "
                "hosted server https://mcp.apiguru.app/mcp, which is always current."
            )
            session = {
                "session_budget_usd": str(budget) if budget is not None else None,
                "session_spent_usd": str(client_module.session_spent_usd),
            }
        return json.dumps(
            {
                "api": info["name"],
                "version": info["version"],
                # An agent read a stale catalogue and concluded `search` took
                # no filters. The catalogue is bundled with the package, so it
                # is only as new as the package: say which one is answering and
                # how to move off it.
                "version_note": version_note,
                "authenticated": bool(key),
                "access_mode": access_mode,
                "marketplaces": list(load_spec()["geos"].keys()),
                "asin_format": conv["asin_pattern"],
                "sample_asin": conv["sample_asin"],
                "retry_policy": conv["retry_policy"],
                "error_semantics": conv["error_semantics"],
                "error_format": {
                    "error": "text", "http_status": "int|null",
                    "billed": "bool, or \"unknown\" when the fetch outlived the tool call",
                    "retryable": "bool", "next_step": "text",
                },
                "payment_in_band": {
                    "on_402": "an isError result whose structuredContent is an x402 PaymentRequired "
                              "(x402Version, accepts, resource)",
                    "pay_with": "resend the same tools/call with the PaymentPayload in "
                                "params._meta[\"x402/payment\"]",
                    "receipt": "result _meta[\"x402/payment-response\"]",
                    "without_a_wallet": client_module.OAUTH_URL,
                },
                "compact_product_fields": list(COMPACT_PRODUCT_FIELDS),
                "compact_batch_fields": list(COMPACT_BATCH_FIELDS),
                "compact_list_fields": list(COMPACT_LIST_FIELDS),
                "list_default_limit": DEFAULT_LIST_LIMIT,
                "large_optional_fields": list(KNOWN_LARGE_FIELDS),
                "cache_ttl_seconds": int(client_module.cache_ttl_seconds()),
                **session,
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
                  parameters, the field, what you expected, what you got, and
                  the request_id from the answer. One issue per entry -- a
                  five-point list cannot be closed point by point.
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
                    # The server version rides along: an install can lag the
                    # release, and a report reads differently against old text.
                    "agent": f"{(agent or '').strip() or 'mcp-client'} (apiguru-mcp {__version__})",
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
