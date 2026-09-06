"""Remote (streamable HTTP) deployment of the Apiguru MCP server.

Two MCP endpoints on one host:

* `/mcp`      keyless. Anyone can call it; the gateway serves a small free
              probe budget per caller, then answers 402. Send `X-API-KEY`
              to bill an existing account instead.
* `/account`  OAuth 2.1 protected. For clients that cannot send headers and
              call from shared egress addresses (claude.ai, Claude Desktop
              connectors, ChatGPT connectors). The client discovers the
              authorization server, registers itself, sends the user to our
              login page, and every tool call then carries a bearer token
              that maps to one Apiguru account. Only mounted when
              `APIGURU_OAUTH_ENABLED=true`.

A remote server serves many callers at once, so the API key cannot come from
an environment variable the way it does in stdio mode. The middleware here
lifts `X-API-KEY` off each incoming request into a ContextVar that client.py
reads for the duration of that request, and does the same for the caller's
address so the gateway can key free probes on the real client.

The transport runs STATELESS by default. In the SDK's stateful mode the tool
handlers run inside a per-session task that is spawned by the request which
created the session (`initialize`), and a task's ContextVars are a snapshot
taken at spawn time -- so a key, address or bearer token sent on a later
request would never be seen. Stateless mode spawns the handler from each
request, so per-request context propagates exactly. Nothing here needs
session state: there are no subscriptions, resources or sampling.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from . import client as client_module
from . import oauth
from . import server as server_module
from .client import request_api_key, request_client_ip, request_route, request_user_agent
from .server import build_server
from .spec import api_info, endpoints, load_spec, price_label

logger = logging.getLogger(__name__)

ACCOUNT_PATH = os.environ.get("APIGURU_MCP_ACCOUNT_PATH", "/account").strip() or "/account"


def _client_address(request: Request) -> str | None:
    """The end client's address as our own nginx reports it.

    nginx sets X-Real-IP from $remote_addr (never from anything the client
    sent), and the container is only reachable through nginx or from the
    private Docker network, so the header can be taken at face value here.
    """
    real = (request.headers.get("X-Real-IP") or "").strip()
    if real:
        return real
    return request.client.host if request.client else None


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Scope the caller's API key and address to their own request."""

    async def dispatch(self, request: Request, call_next):
        key = request.headers.get("X-API-KEY") or request.query_params.get("api_key")

        authorization = request.headers.get("Authorization", "")
        # A bearer token that is one of OUR OAuth tokens is handled by the
        # SDK's auth middleware on /account, not treated as an API key.
        if not key and authorization.lower().startswith("bearer "):
            candidate = authorization[7:].strip()
            if candidate and not candidate.startswith("apg_"):
                key = candidate

        key_token = request_api_key.set(key)
        ip_token = request_client_ip.set(_client_address(request))
        route_token = request_route.set(request.url.path)
        ua_token = request_user_agent.set(request.headers.get("User-Agent"))
        try:
            return await call_next(request)
        finally:
            # Always reset: ContextVars leak across requests otherwise, which
            # would let one caller's key bill another caller's account.
            request_user_agent.reset(ua_token)
            request_route.reset(route_token)
            request_client_ip.reset(ip_token)
            request_api_key.reset(key_token)


# Kept under its old name for anything that imported it.
ApiKeyForwardMiddleware = RequestContextMiddleware


def public_url() -> str:
    """Origin the world reaches this server at; the OAuth issuer."""
    configured = os.environ.get("APIGURU_MCP_PUBLIC_URL", "").strip().rstrip("/")
    if configured:
        return configured
    parsed = urlparse(api_info()["mcp_url"])
    return f"{parsed.scheme}://{parsed.netloc}"


def _transport_security() -> TransportSecuritySettings:
    """Hosts and origins the MCP transport will accept.

    The SDK's DNS-rebinding protection rejects any Host it was not told
    about with 421. Behind nginx the Host is the public domain, not the bind
    address, so the public hostname must be listed explicitly or every real
    request fails. APIGURU_MCP_ALLOWED_HOSTS overrides for other deployments.
    """
    public_host = urlparse(api_info()["mcp_url"]).hostname or "mcp.apiguru.app"
    own_host = urlparse(public_url()).hostname
    configured = os.environ.get("APIGURU_MCP_ALLOWED_HOSTS", "")
    hosts = [h.strip() for h in configured.split(",") if h.strip()] or [
        public_host,
        "localhost",
        "127.0.0.1",
    ]
    if own_host and own_host not in hosts:
        hosts.append(own_host)
    # Hosts may arrive with an explicit port; accept both forms.
    hosts = sorted({h for base in hosts for h in (base, f"{base}:*")})

    origins = [f"https://{public_host}", public_url(), "http://localhost", "http://127.0.0.1"]
    configured_origins = os.environ.get("APIGURU_MCP_ALLOWED_ORIGINS", "")
    if configured_origins:
        origins = [o.strip() for o in configured_origins.split(",") if o.strip()]

    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=sorted(set(origins)))


def _stateless() -> bool:
    return os.environ.get("APIGURU_MCP_STATELESS", "true").strip().lower() != "false"


def _discovery_routes(server) -> None:
    """Unauthenticated discovery routes. Everything an agent needs to decide
    whether to engage is free; only real data costs money."""

    @server.custom_route("/health", methods=["GET"])
    async def health(_request: Request):
        return JSONResponse({"status": "ok", "service": "apiguru-mcp"})

    @server.custom_route("/llms.txt", methods=["GET"])
    async def llms(_request: Request):
        info = api_info()
        lines = [
            f"# {info['name']} - MCP server",
            "",
            f"> {info['description']}",
            "",
            f"MCP endpoint (keyless): {info['mcp_url']}",
        ]
        if oauth.enabled():
            lines.append(
                f"MCP endpoint (sign in with your Apiguru account, OAuth 2.1): "
                f"{public_url()}{ACCOUNT_PATH}"
            )
        lines += [
            "Local: uvx apiguru-mcp (PyPI)",
            "",
            "## Tools",
            "",
        ]
        lines += [
            f"- `{ep['name']}` - {ep['summary']}. {price_label(ep)}."
            for ep in endpoints()
        ]
        lines += [
            "- `list_capabilities` - endpoints, prices and marketplaces. Free.",
            "",
            "No API key required on the keyless endpoint: unkeyed callers get a "
            "free probe budget, then an HTTP 402 payment challenge payable via "
            "x402 (USDC on Base). Send X-API-KEY to bill an existing Apiguru "
            "account instead, or connect the OAuth endpoint from claude.ai / "
            "ChatGPT and sign in.",
            "",
        ]
        return PlainTextResponse("\n".join(lines))

    @server.custom_route("/spec.json", methods=["GET"])
    async def spec(_request: Request):
        return JSONResponse(load_spec())


def create_app(streamable_http_path: str = "/mcp", host: str = "127.0.0.1"):
    security = _transport_security()

    open_server = build_server()
    _discovery_routes(open_server)
    open_app = open_server.streamable_http_app(
        streamable_http_path=streamable_http_path,
        host=host,
        stateless_http=_stateless(),
        transport_security=security,
    )

    if not oauth.enabled():
        open_app.add_middleware(RequestContextMiddleware)
        return open_app

    # ---- OAuth-protected endpoint -------------------------------------------
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions

    from .oauth.login import LOGIN_PATH, create_login_routes
    from .oauth.provider import DEFAULT_SCOPE, ApiguruOAuthProvider
    from .oauth.store import Store

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("APIGURU_OAUTH_ENABLED=true needs DATABASE_URL for the token store.")

    issuer = public_url()
    store = Store(database_url)
    provider = ApiguruOAuthProvider(
        store,
        login_url=f"{issuer}{LOGIN_PATH}",
        access_ttl=int(os.environ.get("APIGURU_OAUTH_ACCESS_TTL", str(24 * 3600))),
        refresh_ttl=int(os.environ.get("APIGURU_OAUTH_REFRESH_TTL", str(30 * 24 * 3600))),
    )

    account_server = build_server(
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=issuer,
            resource_server_url=f"{issuer}{ACCOUNT_PATH}",
            service_documentation_url=api_info()["docs_url"],
            # Clients register with whatever scope string they like (claude.ai
            # sends its own); we grant our single scope by default and never
            # require one, so no client is turned away on vocabulary.
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=None, default_scopes=[DEFAULT_SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=None,
        ),
    )
    account_app = account_server.streamable_http_app(
        streamable_http_path=ACCOUNT_PATH,
        host=host,
        stateless_http=True,
        transport_security=security,
    )
    # Tool calls made with a bearer token run as that user.
    client_module.oauth_key_resolver = store.api_key_for_user
    # Every tool call is recorded for the admin dashboard's channel view.
    server_module.usage_logger = store.log_request

    @asynccontextmanager
    async def lifespan(_app):
        async with open_app.router.lifespan_context(open_app):
            async with account_app.router.lifespan_context(account_app):
                await store.start()
                try:
                    yield
                finally:
                    await store.stop()

    # The SDK puts bearer authentication on the APPLICATION, not on the
    # route: `AuthenticationMiddleware` resolves the token into scope["user"]
    # and `AuthContextMiddleware` exposes it to tool handlers; only the
    # "must be signed in" check (`RequireAuthMiddleware`) sits on /account.
    # Merging the two SDK apps' routes into one app therefore has to carry
    # that stack too, or every token is rejected as absent. Applying it
    # app-wide also lets a token work on the keyless /mcp endpoint, which
    # simply bills the signed-in account there as well.
    from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
    from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
    from mcp.server.auth.provider import ProviderTokenVerifier
    from starlette.middleware import Middleware
    from starlette.middleware.authentication import AuthenticationMiddleware

    app = Starlette(
        routes=[
            *create_login_routes(provider, store),
            *account_app.routes,
            *open_app.routes,
        ],
        middleware=[
            Middleware(RequestContextMiddleware),
            Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(ProviderTokenVerifier(provider))),
            Middleware(AuthContextMiddleware),
        ],
        lifespan=lifespan,
    )
    logger.info("OAuth endpoint mounted at %s%s (issuer %s)", issuer, ACCOUNT_PATH, issuer)
    return app


__all__ = ["create_app", "ACCOUNT_PATH", "public_url"]
