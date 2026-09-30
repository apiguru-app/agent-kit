"""Errors an agent can act on.

The MCP SDK only surfaces the message of `ToolError` subclasses; any other
exception reaches the model as the bare string "Error executing tool X".
That is how a 402 turned into an opaque failure in an external review. So
every failure this server produces is a `ToolError` whose message is a
small JSON document with the same shape every time:

    {
      "error":       human-readable explanation,
      "http_status": upstream status when there was one,
      "billed":      whether the failed call cost money: true, false, or
                     "unknown" when we stopped waiting before the API answered,
      "retryable":   whether retrying unchanged can succeed,
      "next_step":   what to do instead,
      ...            optional extras
    }

One failure is not raised but returned: a keyless 402 is the x402 MCP
transport's PaymentRequired result (see PaymentRequiredError), so that an
x402-capable client can pay it in-band.
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
        billed: bool | str = False,
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


class PaymentRequiredError(ApiguruError):
    """A keyless 402, carrying the x402 PaymentRequired document.

    The tool does not let this propagate: it returns `document` as an
    `isError` result with the same JSON in `structuredContent` and in the
    first text block, which is where x402 MCP clients look for `x402Version`
    and `accepts`. Raised as a ToolError, the SDK would prefix the JSON with
    "Error executing tool ..." and no client could parse it.
    """

    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document
        super().__init__(
            str(document.get("error") or "Payment required."),
            http_status=402,
            billed=False,
            retryable=False,
            next_step=document.get("next_step"),
        )


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
