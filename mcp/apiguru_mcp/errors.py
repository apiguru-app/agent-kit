"""Errors an agent can act on.

The MCP SDK only surfaces the message of `ToolError` subclasses; any other
exception reaches the model as the bare string "Error executing tool X".
That is how a 402 turned into an opaque failure in an external review. So
every failure this server produces is a `ToolError` whose message is a
small JSON document with the same shape every time:

    {
      "error":       human-readable explanation,
      "http_status": upstream status when there was one,
      "billed":      whether the failed call cost money,
      "retryable":   whether retrying unchanged can succeed,
      "next_step":   what to do instead,
      ...            optional extras (payment_challenge, ...)
    }
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError


class ApiguruError(ToolError):
    """A structured, model-readable tool failure."""

    def __init__(
        self,
        error: str,
        *,
        http_status: int | None = None,
        billed: bool = False,
        retryable: bool = False,
        next_step: str | None = None,
        **extra: Any,
    ) -> None:
        self.detail: dict[str, Any] = {
            "error": error,
            "http_status": http_status,
            "billed": billed,
            "retryable": retryable,
            "next_step": next_step,
        }
        self.detail.update({k: v for k, v in extra.items() if v is not None})
        super().__init__(json.dumps({k: v for k, v in self.detail.items() if v is not None}))

    @property
    def http_status(self) -> int | None:
        return self.detail.get("http_status")


logger = logging.getLogger("apiguru_mcp.errors")


def hosted() -> bool:
    """True on the hosted server, which reaches the gateway and the API over
    a private network (the compose file sets both URLs)."""
    return bool(os.environ.get("APIGURU_AGENT_INTERNAL_URL") or os.environ.get("APIGURU_API_INTERNAL_URL"))


def exception_text(exc: BaseException, context: str) -> str:
    """The exception, as a suffix for a caller-facing message.

    A local install shows it: it is the user's own process and network.
    The hosted server logs it and shows nothing -- its exceptions name our
    private network (owner's rule 2026-09-28: no exception text to callers).
    """
    if hosted():
        logger.warning("%s: %s: %s", context, type(exc).__name__, exc)
        return ""
    return f": {exc}"


def structured(exc: BaseException) -> ToolError:
    """Wrap anything that is not already structured."""
    if isinstance(exc, ToolError):
        return exc
    detail = exception_text(exc, "unexpected tool failure")
    return ApiguruError(
        "Unexpected failure inside the tool" + (f": {type(exc).__name__}{detail}" if detail else "."),
        retryable=True,
        next_step="Retry once; if it persists, report it at support@apiguru.app.",
    )
