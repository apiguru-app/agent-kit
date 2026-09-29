"""What arrives on the MCP endpoints: protocol-era routing and one log line
per exchange.

Two jobs, done in one pass over the request body:

1. **Route by what the client sent, not only by its header.** The SDK (mcp
   2.2) picks the protocol era from `MCP-Protocol-Version` alone: any value
   outside the initialize-handshake set goes to the 2026-07-28 per-request
   envelope path, which then refuses a body without the envelope with a
   -32602 that names `_meta` keys the client never heard of. The SDK marks
   this itself ("header-only era-routing for now; body-primary
   classification is a follow-up"). A Node client did exactly that on
   2026-09-25: it listed our tools, then 34 calls in a row came back 400 and
   it never reached one. When the header claims a version we do not handshake
   but the body carries no modern envelope, the body is a handshake-era
   request and is served as one. A body that DOES carry the envelope is left
   alone, so a real 2026-07-28 client, and the SDK's own negotiation for
   versions newer than it knows (-32022 listing what is supported), are
   untouched.

2. **Log every exchange with enough to debug it from the log alone:** the
   caller, client, protocol version, JSON-RPC method and tool, status,
   latency, and for anything that failed the first part of the error the
   caller was shown. Until this, a 400 on /mcp was logged as a bare status
   line, and finding out why meant reproducing it by hand.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

try:  # the registry lives in mcp_types from mcp 2.x on
    from mcp_types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY
    from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, LATEST_MODERN_VERSION
except ImportError:  # older SDK: there is no modern era to misroute into
    CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
    PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
    HANDSHAKE_PROTOCOL_VERSIONS = ()
    LATEST_MODERN_VERSION = None
try:  # the routing-header rung of the 2026-07-28 path
    from mcp.shared.inbound import NAME_BEARING_METHODS, encode_header_value
except ImportError:  # older SDK: nothing checks the headers
    NAME_BEARING_METHODS = {}

    def encode_header_value(value: str) -> str:
        return value

logger = logging.getLogger("apiguru_mcp.traffic")

PROTOCOL_HEADER = b"mcp-protocol-version"
METHOD_HEADER = b"mcp-method"
NAME_HEADER = b"mcp-name"
# `server/discover` exists only in the 2026-07-28 protocol, but clients try it
# first whatever they speak -- without the envelope, or without any version
# header -- and the handshake path answers -32601. 34 of those in a day, from
# directories and from a user behind our npm bridge.
DISCOVER = "server/discover"
# The 2026-07-28 replacement for the standing GET stream. A chat client behind
# our npm bridge sent it every 15 s for 16 hours (2,504 times, 2026-09-28): the
# bridge never learns the negotiated version, so no request carries the
# header, every one landed on the handshake path, and each -32601 was retried.
LISTEN = "subscriptions/listen"
# Methods that exist only on the 2026-07-28 wire. The SDK routes on the
# MCP-Protocol-Version header alone, so without it these can never be served.
MODERN_ONLY = (DISCOVER, LISTEN)
ALLOWED_METHODS = "GET, POST, DELETE, OPTIONS, HEAD"
# Bodies larger than this are passed through without inspection.
MAX_INSPECTED_BODY = 256 * 1024
# How much of a response is kept to explain a failure in the log.
ERROR_SNIPPET = 400
# How much of a successful response is sniffed for an in-band error.
SNIFF_BYTES = 4096

TOP_LEVEL_ERROR = re.compile(r'\{\s*"jsonrpc"\s*:\s*"2\.0"\s*,\s*"id"\s*:\s*[^,{]*,\s*(?P<err>"error"\s*:)')
TOOL_ERROR = re.compile(r'"isError"\s*:\s*true')


def needs_legacy_routing(version: str | None, body: Any) -> bool:
    """True when the header says "modern" but the body is a handshake-era
    request that the SDK would refuse.

    Only a single JSON-RPC request or notification (a dict with `method`)
    qualifies. Batches, responses and anything unparseable keep the SDK's
    answer, whatever it is.
    """
    if version is None or not HANDSHAKE_PROTOCOL_VERSIONS:
        return False
    if version in HANDSHAKE_PROTOCOL_VERSIONS:
        return False
    if not isinstance(body, dict) or "method" not in body:
        return False
    if body["method"] in MODERN_ONLY:  # handled by modern_only_as_modern instead
        return False
    params = body.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    return not (isinstance(meta, dict) and PROTOCOL_VERSION_META_KEY in meta)


def modern_only_as_modern(version: str | None, body: Any) -> tuple[dict, str] | None:
    """A 2026-07-28-only request (see MODERN_ONLY) made servable.

    Returns (body, version for the MCP-Protocol-Version header), or None to
    leave the request alone. Two shapes reach the handshake path and get
    -32601 there:

    * no envelope in the body (a bare `server/discover`): the envelope is
      added;
    * a complete envelope but no header (heldfast, 9 times in 30 h): the body
      is kept and the header is set from the version it names.

    A client whose header names a version we do not know still gets the
    SDK's -32022 listing what we support, which is how it is meant to find
    out. A `subscriptions/listen` without `notifications` asks for nothing
    and is served as that -- an acknowledged, quiet stream -- rather than a
    -32602 its client would retry.
    """
    if LATEST_MODERN_VERSION is None or not isinstance(body, dict):
        return None
    method = body.get("method")
    if method not in MODERN_ONLY:
        return None
    if version is not None and version not in HANDSHAKE_PROTOCOL_VERSIONS:
        if version != LATEST_MODERN_VERSION:
            return None
    params = dict(body["params"]) if isinstance(body.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    changed = False
    if method == LISTEN and not isinstance(params.get("notifications"), dict):
        params["notifications"] = {}
        changed = True
    if PROTOCOL_VERSION_META_KEY in meta and CLIENT_CAPABILITIES_META_KEY in meta:
        claimed = str(meta[PROTOCOL_VERSION_META_KEY])
        if version == claimed and not changed:
            return None  # a well-formed modern request; the SDK serves it
        return {**body, "params": params}, claimed
    meta = {
        PROTOCOL_VERSION_META_KEY: LATEST_MODERN_VERSION,
        CLIENT_CAPABILITIES_META_KEY: {},
        **meta,
    }
    params["_meta"] = meta
    return {**body, "params": params}, str(meta[PROTOCOL_VERSION_META_KEY])


def missing_routing_headers(version: str | None, body: Any,
                            sent: dict[bytes, str | None]) -> dict[bytes, bytes] | None:
    """The Mcp-Method / Mcp-Name headers a modern request left out, from its body.

    The SDK's 2026-07-28 path wants Mcp-Method (and Mcp-Name on tools/call,
    prompts/get, resources/read) equal to the body, and counts an ABSENT
    header as a mismatch: -32020. heldfast, which pins a server's tools
    before a user approves it, sends the version header and the `_meta`
    envelope but never Mcp-Method, so every tools/list it made was refused
    (5 of 5 runs, 2026-09-29) and it reported the server as broken.

    Only a header that is absent (or blank) is filled; one that names
    something else is a real contradiction and keeps the SDK's -32020. Only
    envelope requests whose body version matches the header qualify:
    anything else is not on the modern path, or is refused for its version
    first. `sent` holds the request's own values for both headers.
    """
    if LATEST_MODERN_VERSION is None or version is None or version in HANDSHAKE_PROTOCOL_VERSIONS:
        return None
    if not isinstance(body, dict):
        return None
    method = body.get("method")
    if not isinstance(method, str) or not method.isascii() or not method.isprintable():
        return None
    params = body.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    if not isinstance(meta, dict) or meta.get(PROTOCOL_VERSION_META_KEY) != version:
        return None
    fill: dict[bytes, bytes] = {}
    if not (sent.get(METHOD_HEADER) or "").strip():
        fill[METHOD_HEADER] = method.encode()
    name_key = NAME_BEARING_METHODS.get(method)
    name = params.get(name_key) if name_key else None
    if isinstance(name, str) and not (sent.get(NAME_HEADER) or "").strip():
        fill[NAME_HEADER] = encode_header_value(name).encode()
    return fill or None


def discover_as_modern(version: str | None, body: Any) -> dict | None:
    """The rebuilt body alone (the 1.1.31 name, kept for callers and tests)."""
    rescued = modern_only_as_modern(version, body)
    return rescued[0] if rescued else None


def _with_headers(scope, replace: dict[bytes, bytes]):
    """A copy of `scope` with these headers set (and nothing else touched)."""
    scope = dict(scope)
    scope["headers"] = [
        (k, v) for k, v in scope["headers"] if k not in replace
    ] + list(replace.items())
    return scope


class OpenOriginMiddleware:
    """Let any website's MCP client reach the public endpoints.

    The SDK's DNS-rebinding guard compares `Origin` against a fixed list and
    has no wildcard, so a browser-based client on any other site got 403 (and
    its preflight 400) -- three did in a day. That guard exists for servers on
    localhost or a private network, which a browser page could otherwise
    reach; this one is public, keyless, and never relies on cookies (a key or
    bearer token has to be sent explicitly), so there is nothing for it to
    protect. It sits INSIDE the CORS middleware: CORS still sees the Origin
    and answers the browser; only the SDK's check below it does not. The Host
    check stays on.
    """

    def __init__(self, app, paths: tuple[str, ...]):
        self.app = app
        self.paths = tuple(paths)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") in self.paths:
            scope = dict(scope)
            scope["headers"] = [(k, v) for k, v in scope["headers"] if k != b"origin"]
        await self.app(scope, receive, send)


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key == name:
            return value.decode("latin-1")
    return None


def _client_ip(scope) -> str:
    real = _header(scope, b"x-real-ip")
    if real:
        return real.strip()
    client = scope.get("client")
    return client[0] if client else "-"


def _rpc_summary(body: Any) -> str:
    if isinstance(body, list):
        return f"batch[{len(body)}]"
    if not isinstance(body, dict):
        return "-"
    method = body.get("method")
    if method is None:
        return "response" if "result" in body or "error" in body else "-"
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    name = params.get("name") or params.get("uri")
    return f"{method}({name})" if name else str(method)


def _client_info(body: Any) -> str | None:
    """clientInfo from an initialize, or from a modern request's envelope."""
    if not isinstance(body, dict) or not isinstance(body.get("params"), dict):
        return None
    params = body["params"]
    info = params.get("clientInfo")
    if info is None and isinstance(params.get("_meta"), dict):
        info = params["_meta"].get("io.modelcontextprotocol/clientInfo")
    if isinstance(info, dict):
        return f"{info.get('name', '?')}/{info.get('version', '?')}"
    return None


def _in_band_error(chunk: bytes, rpc: str) -> str | None:
    """A JSON-RPC error or a tool error inside a 200, from the first bytes.

    Only a TOP-LEVEL error counts: a tools/list answer legitimately contains
    `"error": {...}` inside output schemas.
    """
    text = chunk.decode("utf-8", "replace")
    match = TOP_LEVEL_ERROR.search(text)
    if match:
        return "rpc_error " + text[match.start("err"):match.start("err") + ERROR_SNIPPET].replace("\n", " ")
    if rpc.startswith("tools/call") and TOOL_ERROR.search(text):
        return "tool_error"
    return None


class McpTrafficMiddleware:
    """Pure ASGI, so a streamed (SSE) response is never buffered."""

    def __init__(self, app, paths: tuple[str, ...]):
        self.app = app
        self.paths = tuple(paths)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") not in self.paths:
            await self.app(scope, receive, send)
            return

        started = time.monotonic()
        version = _header(scope, PROTOCOL_HEADER)
        body_bytes = b""
        parsed: Any = None
        era = None

        method = scope.get("method")
        # Uptime checkers probe with HEAD (28 in a day, all 405 -- "down" on
        # their boards), and some clients send a bare OPTIONS to see what is
        # allowed. A real CORS preflight carries Access-Control-Request-Method
        # and still goes to the CORS middleware.
        if method == "HEAD" or (
            method == "OPTIONS" and _header(scope, b"access-control-request-method") is None
        ):
            status = 200 if method == "HEAD" else 204
            await send({"type": "http.response.start", "status": status, "headers": [
                (b"allow", ALLOWED_METHODS.encode()),
                (b"content-type", b"application/json"),
                (b"content-length", b"0"),
            ]})
            await send({"type": "http.response.body", "body": b""})
            self._log(scope, version, None, None, status, bytearray(), started)
            return

        if method == "POST":
            chunks: list[bytes] = []
            more = True
            while more:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunks.append(message.get("body", b""))
                more = message.get("more_body", False)
            body_bytes = b"".join(chunks)
            if len(body_bytes) <= MAX_INSPECTED_BODY:
                try:
                    parsed = json.loads(body_bytes)
                except ValueError:
                    parsed = None

            rescued = modern_only_as_modern(version, parsed)
            if rescued is not None:
                rebuilt, claimed = rescued
                body_bytes = json.dumps(rebuilt).encode()
                scope = _with_headers(scope, {
                    PROTOCOL_HEADER: claimed.encode(),
                    METHOD_HEADER: rebuilt["method"].encode(),
                    b"content-length": str(len(body_bytes)).encode(),
                })
                # era=modern-for-discover / era=modern-for-listen
                era = f"modern-for-{rebuilt['method'].rsplit('/', 1)[-1]}"
            elif needs_legacy_routing(version, parsed):
                scope = dict(scope)
                scope["headers"] = [
                    (k, v) for k, v in scope["headers"] if k != PROTOCOL_HEADER
                ]
                era = "legacy-by-body"
            else:
                filled = missing_routing_headers(version, parsed, {
                    METHOD_HEADER: _header(scope, METHOD_HEADER),
                    NAME_HEADER: _header(scope, NAME_HEADER),
                })
                if filled:
                    scope = _with_headers(scope, filled)
                    # era=modern-filled-mcp-method / modern-filled-mcp-method+mcp-name
                    era = "modern-filled-" + "+".join(k.decode() for k in filled)

            replayed = False

            async def receive_replay():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": body_bytes, "more_body": False}
                return await receive()

            downstream_receive = receive_replay
        else:
            downstream_receive = receive

        status = 0
        captured = bytearray()

        async def send_capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                limit = ERROR_SNIPPET if status >= 400 else SNIFF_BYTES
                if len(captured) < limit:
                    captured.extend(message.get("body", b"")[: limit - len(captured)])
            await send(message)

        try:
            await self.app(scope, downstream_receive, send_capture)
        finally:
            self._log(scope, version, parsed, era, status, captured, started)

    @staticmethod
    def _log(scope, version, parsed, era, status, captured, started):
        elapsed = int((time.monotonic() - started) * 1000)
        rpc = _rpc_summary(parsed)
        parts = [
            f"{scope.get('method')} {scope.get('path')} {status or 'aborted'} {elapsed}ms",
            f"ip={_client_ip(scope)}",
            f"ua={(_header(scope, b'user-agent') or '-')[:80]!r}",
            f"pv={version or '-'}",
            f"rpc={rpc}",
        ]
        client = _client_info(parsed)
        if client:
            parts.append(f"client={client}")
        if era:
            parts.append(f"era={era}")
        origin = _header(scope, b"origin")
        if origin:
            parts.append(f"origin={origin[:80]}")

        if status >= 400:
            snippet = bytes(captured).decode("utf-8", "replace").replace("\n", " ")
            parts.append(f"err={snippet!r}")
            logger.warning("mcp %s", " ".join(parts))
            return
        problem = _in_band_error(bytes(captured), rpc) if status == 200 else None
        if problem:
            parts.append(problem)
            logger.warning("mcp %s", " ".join(parts))
            return
        logger.info("mcp %s", " ".join(parts))
