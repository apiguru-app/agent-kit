"""HTTP transport to the Apiguru API.

Three routes to the same data, decided per call:

* **OAuth** — the request carries one of our bearer tokens (hosted server,
  `/account` endpoint). The token's subject is a `users.id`; its API key is
  looked up and the call bills that account.
* **Keyed** — an `X-API-KEY` is available (env var, or forwarded from the
  incoming HTTP request when running as a remote server). Calls go straight
  to the normal API and bill the owning account.
* **Keyless** — no key anywhere. Calls go to the agent gateway, which serves
  a small free probe budget and then answers 402 with a payment challenge.

When this server is the HOSTED one (mcp.apiguru.app) it sits in the same
Docker network as the gateway and the backend. It then calls both directly
and tells the gateway who the real end client is, authenticated with a
shared secret. Without that, every keyless call arrived at the gateway from
the MCP container's own address and all remote users shared one free-probe
budget.

Two guards live here as well:

* a short-lived response cache, so an agent that retries or re-asks the
  same question within a few minutes does not pay twice;
* an optional per-process spend ceiling (`APIGURU_SESSION_BUDGET_USD`) for
  local installs, so a runaway loop stops at a number the operator chose.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from contextvars import ContextVar
from decimal import Decimal
from typing import Any, Awaitable, Callable

import httpx

from .errors import ApiguruError
from .spec import api_info, endpoints

# Set by the ASGI middleware in http_app.py for the duration of one request.
# Empty in stdio mode, where the env var is the only source of a key.
request_api_key: ContextVar[str | None] = ContextVar("request_api_key", default=None)

# The end client's address, also set per request by the middleware. Passed to
# the gateway so free probes are keyed on the caller, not on this container.
request_client_ip: ContextVar[str | None] = ContextVar("request_client_ip", default=None)

# Which HTTP path the tool call arrived on (/mcp or /account) and the caller's
# User-Agent. Only for usage telemetry.
request_route: ContextVar[str | None] = ContextVar("request_route", default=None)
request_user_agent: ContextVar[str | None] = ContextVar("request_user_agent", default=None)

# Installed by http_app.py when OAuth is enabled: users.id -> api_key.
oauth_key_resolver: Callable[[str], Awaitable[str | None]] | None = None

USER_AGENT = "apiguru-mcp/1.1 (+https://apiguru.app)"
TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)

INTERNAL_TOKEN_HEADER = "X-Apiguru-Internal-Token"
INTERNAL_CLIENT_HEADER = "X-Apiguru-Client-IP"

DASH = "https://dash.apiguru.app"
OAUTH_URL = "https://mcp.apiguru.app/account"


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------

def oauth_subject() -> str | None:
    """The user id behind the current request's bearer token, if any."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
    except Exception:  # SDK without the auth extras
        return None
    token = get_access_token()
    return getattr(token, "subject", None) if token else None


def oauth_client_id() -> str | None:
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
    except Exception:
        return None
    token = get_access_token()
    return getattr(token, "client_id", None) if token else None


def resolve_api_key_sync() -> str | None:
    """Key from the request header or the environment. Does not see OAuth."""
    return request_api_key.get() or os.environ.get("APIGURU_API_KEY") or None


async def resolve_api_key() -> str | None:
    """The key this call should be made with, from any of the three routes.

    Raises when the caller is signed in via OAuth but the account can no
    longer be used: silently falling back to the keyless gateway would hand
    them somebody else's probe budget.
    """
    key = resolve_api_key_sync()
    if key:
        return key
    subject = oauth_subject()
    if subject and oauth_key_resolver is not None:
        key = await oauth_key_resolver(subject)
        if not key:
            raise ApiguruError(
                "Your Apiguru account is disabled or its API key was removed.",
                http_status=403,
                next_step=f"Sign in again from the client, or check the account at {DASH}.",
            )
        return key
    return None


def access_mode_sync() -> str:
    """keyless | apikey | oauth -- for telemetry and list_capabilities."""
    if oauth_subject():
        return "oauth"
    if resolve_api_key_sync():
        return "apikey"
    return "keyless"


# --------------------------------------------------------------------------
# where to call
# --------------------------------------------------------------------------

def _internal_gateway() -> tuple[str, str] | None:
    """(url, token) for the gateway on the private network, when configured.

    Both must be present: the URL says where the gateway is, the token is
    what lets this server vouch for its callers' addresses.
    """
    url = os.environ.get("APIGURU_AGENT_INTERNAL_URL", "").strip().rstrip("/")
    token = os.environ.get("AGENT_INTERNAL_TOKEN", "").strip()
    if url and token:
        return url, token
    return None


def _base_url(keyed: bool) -> str:
    override = os.environ.get("APIGURU_BASE_URL")
    if override:
        return override.rstrip("/")
    if keyed:
        internal_api = os.environ.get("APIGURU_API_INTERNAL_URL", "").strip().rstrip("/")
        if internal_api:
            return internal_api
    else:
        internal = _internal_gateway()
        if internal:
            return internal[0]
    info = api_info()
    return (info["base_url"] if keyed else info["agent_base_url"]).rstrip("/")


def _encode(params: dict[str, Any]) -> dict[str, str]:
    """Drop unset params and encode the rest the way the backend parses them.

    Booleans must be the lowercase strings "true"/"false" — the endpoints
    compare with `request.args.get(...).lower() == 'true'`, so Python's
    default "True" would silently read as false.
    """
    out: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        else:
            out[key] = str(value)
    return out


# --------------------------------------------------------------------------
# cost estimate + session budget
# --------------------------------------------------------------------------

_BY_PATH: dict[str, dict[str, Any]] = {}


def _endpoint_for(path: str) -> dict[str, Any] | None:
    if not _BY_PATH:
        for ep in endpoints():
            _BY_PATH[ep["path"]] = ep
            for alias in ep.get("aliases", []):
                _BY_PATH[alias] = ep
    return _BY_PATH.get(path)


def estimate_cost_usd(path: str, params: dict[str, Any]) -> Decimal:
    """What this call will cost at list price, before making it."""
    ep = _endpoint_for(path)
    if ep is None:
        return Decimal("0")
    if ep["price_model"] != "per_item":
        return Decimal(ep["price_usd"])
    raw = str(params.get(ep["count_param"]) or "")
    items = [p.strip() for p in raw.split(ep["count_separator"]) if p.strip()]
    if ep.get("count_dedup", True):
        items = list(dict.fromkeys(items))
    count = max(1, min(len(items), ep["max_items"]))
    return Decimal(ep["unit_price_usd"]) * count


def session_budget_usd() -> Decimal | None:
    raw = os.environ.get("APIGURU_SESSION_BUDGET_USD", "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except Exception:
        return None


session_spent_usd = Decimal("0")


def _check_budget(estimate: Decimal) -> None:
    budget = session_budget_usd()
    if budget is None:
        return
    if session_spent_usd + estimate > budget:
        raise ApiguruError(
            f"Session budget reached: ${session_spent_usd} spent of the "
            f"${budget} allowed by APIGURU_SESSION_BUDGET_USD; this call would add ${estimate}.",
            billed=False,
            retryable=False,
            next_step="Raise APIGURU_SESSION_BUDGET_USD, restart the server, or stop here.",
            session_spent_usd=str(session_spent_usd),
            session_budget_usd=str(budget),
        )


# --------------------------------------------------------------------------
# response cache
# --------------------------------------------------------------------------

def cache_ttl_seconds() -> float:
    try:
        return float(os.environ.get("APIGURU_MCP_CACHE_TTL", "300"))
    except ValueError:
        return 300.0


_cache: dict[str, tuple[float, Any]] = {}
_CACHE_MAX = 2000


def _cache_key(identity: str, path: str, params: dict[str, str]) -> str:
    material = json.dumps([identity, path, sorted(params.items())], separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()


def _cache_get(key: str) -> tuple[Any, float] | None:
    ttl = cache_ttl_seconds()
    if ttl <= 0:
        return None
    hit = _cache.get(key)
    if hit is None:
        return None
    stored_at, payload = hit
    age = time.monotonic() - stored_at
    if age > ttl:
        _cache.pop(key, None)
        return None
    return payload, age


def _cache_put(key: str, payload: Any) -> None:
    if cache_ttl_seconds() <= 0:
        return
    if len(_cache) >= _CACHE_MAX:
        oldest = sorted(_cache.items(), key=lambda kv: kv[1][0])[: _CACHE_MAX // 4]
        for k, _ in oldest:
            _cache.pop(k, None)
    _cache[key] = (time.monotonic(), payload)


def cache_clear() -> None:
    _cache.clear()


# --------------------------------------------------------------------------
# the call
# --------------------------------------------------------------------------

def _payment_challenge(response: httpx.Response) -> str | None:
    return (
        response.headers.get("PAYMENT-REQUIRED")
        or response.headers.get("payment-required")
        or response.headers.get("WWW-Authenticate")
    )


async def call_endpoint(path: str, params: dict[str, Any], *, use_cache: bool = True) -> Any:
    """GET one API endpoint and return parsed JSON.

    Every failure is an ApiguruError whose message is a JSON document the
    model can act on (see errors.py).
    """
    global session_spent_usd

    api_key = await resolve_api_key()
    keyed = bool(api_key)
    url = f"{_base_url(keyed=keyed)}{path}"
    encoded = _encode(params)

    identity = (
        f"key:{hashlib.sha256(api_key.encode()).hexdigest()[:16]}" if api_key
        else f"anon:{request_client_ip.get() or 'local'}"
    )
    key = _cache_key(identity, path, encoded)
    if use_cache:
        hit = _cache_get(key)
        if hit is not None:
            payload, age = hit
            if isinstance(payload, dict):
                return {**payload, "_cache": {"hit": True, "age_seconds": int(age), "ttl_seconds": int(cache_ttl_seconds())}}
            return payload

    estimate = estimate_cost_usd(path, params)
    _check_budget(estimate)

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    client_ip = request_client_ip.get()
    if api_key:
        headers["X-API-KEY"] = api_key
        if client_ip:
            # So the backend's request log records the person, not this box.
            headers["X-Forwarded-For"] = client_ip
    else:
        internal = _internal_gateway()
        if internal and not os.environ.get("APIGURU_BASE_URL"):
            headers[INTERNAL_TOKEN_HEADER] = internal[1]
            if client_ip:
                headers[INTERNAL_CLIENT_HEADER] = client_ip

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        try:
            response = await client.get(url, params=encoded, headers=headers)
        except httpx.TimeoutException as exc:
            raise ApiguruError(
                f"Request to {path} timed out after {TIMEOUT.read:.0f}s. This endpoint fetches "
                "live data from Amazon and can be slow under load.",
                billed=False, retryable=True,
                next_step="Retry; nothing was charged for a timeout.",
            ) from exc
        except httpx.HTTPError as exc:
            raise ApiguruError(
                f"Could not reach the Apiguru API: {exc}",
                billed=False, retryable=True, next_step="Retry shortly.",
            ) from exc

    status = response.status_code
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None

    def detail() -> str:
        if isinstance(payload, dict):
            return str(payload.get("message") or payload.get("error") or "")
        return response.text[:300]

    if status == 402:
        if keyed:
            raise ApiguruError(
                "Payment required: this Apiguru account has no balance or trial calls left. "
                + detail(),
                http_status=402, billed=False, retryable=False,
                next_step=f"Top up at {DASH}, or sign in with a funded account.",
            )
        raise ApiguruError(
            "Payment required: the free probe budget for this caller is spent. " + detail(),
            http_status=402, billed=False, retryable=False,
            next_step=(
                "Either pay this request with an x402-capable HTTP client (USDC on Base) using the "
                f"payment_challenge below, set APIGURU_API_KEY from {DASH}, or connect the OAuth "
                f"endpoint {OAUTH_URL} and sign in."
            ),
            payment_challenge=_payment_challenge(response),
            free_probes_remaining=response.headers.get("X-Free-Probes-Remaining"),
        )
    if status == 401:
        raise ApiguruError(
            "Unauthorized: the API key was rejected.",
            http_status=401, billed=False, retryable=False,
            next_step=f"Check the key at {DASH}; unset APIGURU_API_KEY to use the keyless gateway.",
        )
    if status == 403:
        raise ApiguruError(
            "Forbidden: " + (detail() or "the account is disabled or has no active plan."),
            http_status=403, billed=False, retryable=False,
            next_step=f"Check the account at {DASH}.",
        )
    if status == 413:
        raise ApiguruError(
            "Too many items in one call. " + detail(),
            http_status=413, billed=False, retryable=False,
            next_step="Split the list (20 ASINs for product_details_batch, 10 for offers_stock and seller_profile_batch) and retry.",
        )
    if status == 429:
        raise ApiguruError(
            "Rate limited: too many requests per second for this plan.",
            http_status=429, billed=False, retryable=True,
            next_step="Back off for a second and retry.",
        )
    if status == 404:
        raise ApiguruError(
            "Not found: " + (detail() or "the item does not exist on that marketplace") + ". This fetch WAS billed.",
            http_status=404, billed=True, retryable=False,
            next_step="Retrying will not help; try a different geo or move on.",
        )
    if status == 400:
        raise ApiguruError(
            "Bad input: " + detail(),
            http_status=400, billed=False, retryable=False,
            next_step="Fix the parameters (ASINs are 10 uppercase alphanumerics; geo is one of the 20 codes) and retry.",
        )
    if status >= 500:
        raise ApiguruError(
            f"Upstream fetch failed (HTTP {status}). This is an Apiguru-side failure. " + detail(),
            http_status=status, billed=False, retryable=True,
            next_step="Retry with a short backoff; nothing was charged.",
        )
    if status >= 400:
        raise ApiguruError(
            f"HTTP {status} from {path}: {detail()}",
            http_status=status, billed=False, retryable=False,
        )
    if payload is None:
        raise ApiguruError(
            f"Expected JSON from {path} but got HTTP {status}: {response.text[:200]}",
            http_status=status, billed=False, retryable=True, next_step="Retry once.",
        )

    session_spent_usd += estimate
    if use_cache:
        _cache_put(key, payload)
    return payload


# --- feedback --------------------------------------------------------------

GITHUB_ISSUES = "https://github.com/apiguru-app/agent-kit/issues"
FEEDBACK_WALL = "https://dash.apiguru.app/feedback"


def feedback_url() -> str:
    """Where feedback is POSTed.

    Always the keyed base: the x402 gateway proxies GETs only, and this is
    free anyway, so it never goes through the paid rail.
    """
    override = os.environ.get("APIGURU_FEEDBACK_URL")
    if override:
        return override.rstrip("/")
    base = os.environ.get("APIGURU_BASE_URL") or api_info()["base_url"]
    return f"{base.rstrip('/')}/feedback"


async def post_feedback(payload: dict[str, Any]) -> Any:
    """Send one feedback entry. Free, unauthenticated, never billed."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    client_ip = request_client_ip.get()
    if client_ip:
        headers["X-Forwarded-For"] = client_ip
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        try:
            response = await client.post(feedback_url(), json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ApiguruError(
                f"Could not reach the feedback wall: {exc}",
                billed=False, retryable=True,
                next_step=f"Open an issue at {GITHUB_ISSUES} instead.",
            ) from exc

    try:
        body: Any = response.json()
    except ValueError:
        body = {"raw": response.text[:300]}

    if response.status_code == 429:
        raise ApiguruError(
            "Feedback rate limit reached for this address.",
            http_status=429, billed=False, retryable=True,
            next_step=f"Wait an hour, or open an issue at {GITHUB_ISSUES}.",
        )
    if response.status_code >= 400:
        raise ApiguruError(
            f"The feedback wall returned HTTP {response.status_code}: "
            + str(body.get("error") if isinstance(body, dict) else body),
            http_status=response.status_code, billed=False,
            retryable=response.status_code >= 500,
            next_step=f"Open an issue at {GITHUB_ISSUES} instead.",
        )
    return body
