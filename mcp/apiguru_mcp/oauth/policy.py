"""Which redirect URIs an authorization code may be sent to.

Dynamic registration (RFC 7591) stays open, but the operator decides where
codes go, never the registering client. A callback is approved when it is

* an HTTPS URI listed exactly in the policy file (hosted clients),
* plain HTTP on a loopback address with an explicit port (desktop clients,
  RFC 8252 section 7.3), or
* a private-use scheme the policy names, which the user's own device
  routes to an installed app (RFC 8252 section 7.1).

The policy is a JSON file (`APIGURU_OAUTH_POLICY_FILE`, default
`/app/oauth-policy.json`). A missing file approves nothing, and a malformed
one stops the server from starting, so neither can fall back to open
registration.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Schemes a browser handles itself (or that reach the network) can never be
# a private-use scheme, whatever the policy file says.
NOT_PRIVATE_USE = frozenset({
    "http", "https", "ws", "wss", "ftp", "file", "data", "blob", "javascript",
    "vbscript", "about", "filesystem", "view-source", "intent", "content",
})
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*$")


@dataclass(frozen=True)
class RedirectPolicy:
    redirect_uris: frozenset[str] = frozenset()
    allow_loopback: bool = False
    native_schemes: frozenset[str] = frozenset()

    @classmethod
    def from_environment(cls) -> RedirectPolicy:
        path = Path(os.environ.get("APIGURU_OAUTH_POLICY_FILE", "/app/oauth-policy.json"))
        # Missing configuration must not silently enable open registration.
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        uris = data.get("redirect_uris", [])
        loopback = data.get("allow_loopback", False)
        schemes = data.get("native_schemes", [])
        if not isinstance(uris, list) or not all(isinstance(uri, str) for uri in uris):
            raise ValueError("OAuth redirect_uris must be a list of strings")
        if not isinstance(loopback, bool):
            raise ValueError("OAuth allow_loopback must be a boolean")
        if not isinstance(schemes, list) or not all(
            isinstance(s, str) and _SCHEME.match(s) and s not in NOT_PRIVATE_USE for s in schemes
        ):
            raise ValueError("OAuth native_schemes must be a list of lower-case private-use schemes")
        return cls(frozenset(uris), loopback, frozenset(schemes))

    def allows(self, uri: str) -> bool:
        # Reject parser ambiguities before either exact or loopback matching.
        if not uri or "\\" in uri or any(ord(c) <= 32 or ord(c) == 127 for c in uri):
            return False
        try:
            parsed = urlsplit(uri)
            port = parsed.port
        except ValueError:
            return False
        if parsed.username is not None or parsed.password is not None or "#" in uri:
            return False
        if parsed.scheme == "https" and parsed.hostname and uri in self.redirect_uris:
            return True
        if parsed.scheme in self.native_schemes:
            return True
        return (
            self.allow_loopback
            and parsed.scheme == "http"
            and parsed.hostname in LOOPBACK_HOSTS
            and port is not None
            and port > 0
        )

    def approved(self, uris) -> list:
        """The approved subset, in the client's order."""
        return [uri for uri in (uris or []) if self.allows(str(uri))]

    def restrict(self, client):
        """The client limited to its approved callbacks, or None if it has
        none. RFC 7591 section 3.2.1 lets the server replace requested
        metadata; a client that also lists an unapproved callback (VS Code
        registers four, two of them portless or remote) keeps working
        through the approved ones, and the rest can never receive a code."""
        uris = self.approved(client.redirect_uris)
        if not uris:
            return None
        if len(uris) == len(client.redirect_uris):
            return client
        return client.model_copy(update={"redirect_uris": uris})

    def allows_client(self, client) -> bool:
        return self.restrict(client) is not None
