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
    from mcp_types import PROTOCOL_VERSION_META_KEY
    from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS
except ImportError:  # older SDK: there is no modern era to misroute into
    PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
    HANDSHAKE_PROTOCOL_VERSIONS = ()

logger = logging.getLogger("apiguru_mcp.traffic")

PROTOCOL_HEADER = b"mcp-protocol-version"
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
    params = body.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    return not (isinstance(meta, dict) and PROTOCOL_VERSION_META_KEY in meta)


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
        rerouted = False

        if scope.get("method") == "POST":
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

            if needs_legacy_routing(version, parsed):
                scope = dict(scope)
                scope["headers"] = [
                    (k, v) for k, v in scope["headers"] if k != PROTOCOL_HEADER
                ]
                rerouted = True

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
            self._log(scope, version, parsed, rerouted, status, captured, started)

    @staticmethod
    def _log(scope, version, parsed, rerouted, status, captured, started):
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
        if rerouted:
            parts.append("era=legacy-by-body")

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
