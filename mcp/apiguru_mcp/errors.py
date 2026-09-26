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


def structured(exc: BaseException) -> ToolError:
    """Wrap anything that is not already structured."""
    if isinstance(exc, ToolError):
        return exc
    return ApiguruError(
        f"Unexpected failure inside the tool: {type(exc).__name__}: {exc}",
        retryable=True,
        next_step="Retry once; if it persists, report it at support@apiguru.app.",
    )
