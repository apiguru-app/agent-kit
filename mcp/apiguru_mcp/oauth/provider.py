"""The authorization-server half of the MCP SDK's auth contract.

The SDK owns the wire protocol: `/authorize`, `/token`, `/register`,
`/revoke`, the metadata documents, PKCE and client authentication. This
class supplies what only we can: where users log in, how codes and tokens
are minted and stored, and which Apiguru account a token stands for.

Flow, in the SDK's terms:

    /authorize  -> provider.authorize()          -> redirect to our login page
    login page  -> provider.complete_login()     -> auth code -> client redirect
    /token      -> load_authorization_code()     -> exchange_authorization_code()
    tool call   -> load_access_token()           -> subject (users.id)
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .store import Store, new_token, utcnow

logger = logging.getLogger(__name__)

# Codes are single use and short lived; the user has just clicked "Allow".
CODE_TTL_SECONDS = 300
# A login page that sits open longer than this is abandoned.
PENDING_TTL_SECONDS = 600

DEFAULT_SCOPE = "apiguru"


@dataclass
class PendingLogin:
    """An /authorize request waiting for the user to sign in."""

    txn: str
    client_id: str
    client_name: str
    params: AuthorizationParams
    created: float


class ApiguruOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(
        self,
        store: Store,
        *,
        login_url: str,
        access_ttl: int,
        refresh_ttl: int,
    ) -> None:
        self.store = store
        self.login_url = login_url.rstrip("/")
        self.access_ttl = access_ttl
        self.refresh_ttl = refresh_ttl
        # In memory on purpose: a pending login is worthless after a
        # restart, and the user simply clicks "connect" again.
        self._pending: dict[str, PendingLogin] = {}

    # -- clients (dynamic registration, RFC 7591) ---------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        document = await self.store.load_client(client_id)
        if document is None:
            return None
        return OAuthClientInformationFull.model_validate_json(document)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await self.store.save_client(client_info.client_id, client_info.model_dump_json())
        logger.info(
            "Registered OAuth client %s (%s) redirect_uris=%s",
            client_info.client_id,
            client_info.client_name,
            [str(u) for u in (client_info.redirect_uris or [])],
        )

    # -- authorization ------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self._expire_pending()
        txn = secrets.token_urlsafe(24)
        self._pending[txn] = PendingLogin(
            txn=txn,
            client_id=client.client_id,
            client_name=client.client_name or client.client_id,
            params=params,
            created=time.monotonic(),
        )
        return f"{self.login_url}?txn={txn}"

    def pending(self, txn: str) -> PendingLogin | None:
        self._expire_pending()
        return self._pending.get(txn)

    async def complete_login(self, txn: str, subject: str) -> str:
        """The user proved who they are. Mint the code and build the URL that
        sends them back to the client. Returns that URL."""
        pending = self._pending.pop(txn, None)
        if pending is None:
            raise AuthorizeError(error="invalid_request", error_description="Login session expired; retry.")

        params = pending.params
        scopes = params.scopes or [DEFAULT_SCOPE]
        code = new_token("apg_code")
        await self.store.save_code(
            code=code,
            client_id=pending.client_id,
            subject=subject,
            scopes=scopes,
            code_challenge=params.code_challenge,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_explicit=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            ttl_seconds=CODE_TTL_SECONDS,
        )
        logger.info("Issued authorization code for user %s to client %s", subject, pending.client_id)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def abandon(self, txn: str) -> None:
        self._pending.pop(txn, None)

    def _expire_pending(self) -> None:
        cutoff = time.monotonic() - PENDING_TTL_SECONDS
        for key in [k for k, v in self._pending.items() if v.created < cutoff]:
            del self._pending[key]

    # -- codes ---------------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        stored = await self.store.load_code(authorization_code)
        if stored is None or stored.used or stored.client_id != client.client_id:
            return None
        if stored.expires_at < utcnow():
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=stored.scopes,
            expires_at=stored.expires_at.timestamp(),
            client_id=stored.client_id,
            code_challenge=stored.code_challenge,
            redirect_uri=stored.redirect_uri,  # type: ignore[arg-type]
            redirect_uri_provided_explicitly=stored.redirect_uri_explicit,
            resource=stored.resource,
            subject=stored.subject,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        stored = await self.store.load_code(authorization_code.code)
        if stored is None or not await self.store.consume_code(stored.code_hash):
            raise TokenError(error="invalid_grant", error_description="Authorization code already used.")
        return await self._issue(
            client_id=client.client_id,
            subject=stored.subject,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
        )

    # -- refresh --------------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        stored = await self.store.load_token(refresh_token, "refresh")
        if stored is None or stored.revoked or stored.client_id != client.client_id:
            return None
        if stored.expires_at < utcnow():
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=stored.client_id,
            scopes=stored.scopes,
            expires_at=int(stored.expires_at.timestamp()),
            subject=stored.subject,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        stored = await self.store.load_token(refresh_token.token, "refresh")
        if stored is None or stored.revoked:
            raise TokenError(error="invalid_grant", error_description="Refresh token is no longer valid.")
        # Rotate: the old pair dies with this exchange (RFC 6819 §5.2.2.3).
        await self.store.revoke_pair(stored.pair_id)
        granted = scopes or stored.scopes
        if any(s not in stored.scopes for s in granted):
            raise TokenError(error="invalid_scope", error_description="Cannot widen scopes on refresh.")
        return await self._issue(
            client_id=client.client_id,
            subject=stored.subject,
            scopes=granted,
            resource=stored.resource,
        )

    # -- access ---------------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        stored = await self.store.load_token(token, "access")
        if stored is None or stored.revoked or stored.expires_at < utcnow():
            return None
        return AccessToken(
            token=token,
            client_id=stored.client_id,
            scopes=stored.scopes,
            expires_at=int(stored.expires_at.timestamp()),
            resource=stored.resource,
            subject=stored.subject,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        kind = "refresh" if isinstance(token, RefreshToken) else "access"
        stored = await self.store.load_token(token.token, kind)
        if stored is not None:
            await self.store.revoke_pair(stored.pair_id)

    # -- helpers --------------------------------------------------------------

    async def _issue(self, *, client_id: str, subject: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access = new_token("apg_at")
        refresh = new_token("apg_rt")
        await self.store.save_token_pair(
            access_token=access,
            refresh_token=refresh,
            client_id=client_id,
            subject=subject,
            scopes=scopes,
            resource=resource,
            access_ttl=self.access_ttl,
            refresh_ttl=self.refresh_ttl,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.access_ttl,
            refresh_token=refresh,
            scope=" ".join(scopes) if scopes else None,
        )


def describe(params: AuthorizationParams) -> dict[str, Any]:
    """What the consent page shows about the request."""
    return {
        "redirect_uri": str(params.redirect_uri),
        "scopes": params.scopes or [DEFAULT_SCOPE],
        "resource": params.resource,
    }
