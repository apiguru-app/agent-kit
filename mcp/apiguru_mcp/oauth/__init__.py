"""OAuth 2.1 for the hosted MCP server.

Why this exists: claude.ai, Claude Desktop connectors and ChatGPT connectors
cannot send custom headers, so there is no way for them to present an API
key. OAuth is the only door those clients can walk through: they discover
the authorization server, register themselves, send the user to our login
page, and from then on every tool call carries a bearer token that maps to
one Apiguru account and bills it.

The keyless `/mcp` endpoint is untouched; this adds a second, protected
endpoint (`/account` by default) plus the authorization-server routes.

Only active in the hosted deployment (`APIGURU_OAUTH_ENABLED=true`, needs
`DATABASE_URL`). The stdio server never imports any of this.
"""

from __future__ import annotations

import os


def enabled() -> bool:
    return os.environ.get("APIGURU_OAUTH_ENABLED", "false").strip().lower() == "true"


__all__ = ["enabled"]
