"""Persistence for OAuth clients, codes and tokens, plus the user lookup.

Everything the authorization server needs to survive a container restart
lives here, in three small tables the MCP server owns (`mcp_oauth_*`). Users
are read from the backend's `users` table; only the columns needed to turn a
token into an API key are mapped, and nothing there is ever written.

Tokens and codes are stored as SHA-256 hashes. A database read never yields
a usable credential.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    delete,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

logger = logging.getLogger(__name__)

Base = declarative_base()


# --- owned tables ----------------------------------------------------------

class OAuthClient(Base):
    __tablename__ = "mcp_oauth_clients"

    client_id = Column(String(64), primary_key=True)
    # The full OAuthClientInformationFull document, as the SDK produced it.
    data = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class OAuthCode(Base):
    __tablename__ = "mcp_oauth_codes"

    code_hash = Column(String(64), primary_key=True)
    client_id = Column(String(64), nullable=False, index=True)
    subject = Column(String(64), nullable=False)  # users.id
    scopes = Column(Text, nullable=False, default="")
    code_challenge = Column(String(128), nullable=False)
    redirect_uri = Column(Text, nullable=False)
    redirect_uri_explicit = Column(Boolean, nullable=False, default=True)
    resource = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, nullable=False, default=False)


class OAuthTokenRow(Base):
    __tablename__ = "mcp_oauth_tokens"

    token_hash = Column(String(64), primary_key=True)
    kind = Column(String(8), nullable=False)  # access | refresh
    pair_id = Column(String(32), nullable=False, index=True)
    client_id = Column(String(64), nullable=False, index=True)
    subject = Column(String(64), nullable=False, index=True)
    scopes = Column(Text, nullable=False, default="")
    resource = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class McpRequest(Base):
    """One row per tool call on the hosted server, for the channel view."""

    __tablename__ = "mcp_requests"

    id = Column(Integer, primary_key=True)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)
    route = Column(String(32), nullable=False)       # /mcp | /account
    auth_mode = Column(String(16), nullable=False)   # keyless | apikey | oauth
    client = Column(String(120), nullable=True)      # OAuth client name, else User-Agent
    tool = Column(String(64), nullable=False)
    ok = Column(Boolean, nullable=False)
    http_status = Column(Integer, nullable=True)
    cached = Column(Boolean, nullable=False, default=False)
    duration_ms = Column(Integer, nullable=True)
    subject = Column(String(64), nullable=True)
    client_ip = Column(String(64), nullable=True)


# --- the backend's users table, read only ---------------------------------

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(120))
    api_key = Column(String)
    is_active = Column(Boolean)


OWNED_TABLES = [OAuthClient.__table__, OAuthCode.__table__, OAuthTokenRow.__table__, McpRequest.__table__]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; Postgres aware ones."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_token(prefix: str) -> str:
    """Opaque credential with 256 bits of entropy and a recognisable prefix."""
    return f"{prefix}_{secrets.token_urlsafe(32)}"


@dataclass
class StoredToken:
    token_hash: str
    kind: str
    pair_id: str
    client_id: str
    subject: str
    scopes: list[str]
    resource: str | None
    expires_at: datetime
    revoked: bool


@dataclass
class StoredCode:
    code_hash: str
    client_id: str
    subject: str
    scopes: list[str]
    code_challenge: str
    redirect_uri: str
    redirect_uri_explicit: bool
    resource: str | None
    expires_at: datetime
    used: bool


class Store:
    def __init__(self, database_url: str) -> None:
        self._engine = create_async_engine(database_url, pool_pre_ping=True, pool_size=3, max_overflow=3)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        # user_id -> (api_key or None, fetched_at)
        self._key_cache: dict[str, tuple[str | None, float]] = {}
        self.key_cache_ttl = 60.0
        self._client_names: dict[str, str] = {}

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        async with self._engine.begin() as conn:
            # Only the three tables this module owns; `users` belongs to the
            # backend and is never created or altered from here.
            await conn.run_sync(Base.metadata.create_all, tables=OWNED_TABLES)
        await self.sweep()
        logger.info("OAuth store ready.")

    async def stop(self) -> None:
        await self._engine.dispose()

    async def sweep(self) -> None:
        """Drop expired codes and tokens that are more than a week past expiry."""
        cutoff = utcnow() - timedelta(days=7)
        async with self._sessions() as session:
            async with session.begin():
                await session.execute(delete(OAuthCode).where(OAuthCode.expires_at < cutoff))
                await session.execute(delete(OAuthTokenRow).where(OAuthTokenRow.expires_at < cutoff))
                await session.execute(delete(McpRequest).where(McpRequest.ts < utcnow() - timedelta(days=90)))

    # -- usage telemetry --------------------------------------------------

    async def client_name(self, client_id: str | None) -> str | None:
        if not client_id:
            return None
        if client_id in self._client_names:
            return self._client_names[client_id]
        document = await self.load_client(client_id)
        name = None
        if document:
            try:
                name = json.loads(document).get("client_name")
            except ValueError:
                name = None
        self._client_names[client_id] = name or client_id[:12]
        return self._client_names[client_id]

    async def log_request(self, event: dict) -> None:
        """One tool call. Called fire-and-forget by the server; must not raise."""
        label = await self.client_name(event.get("oauth_client_id")) or (event.get("user_agent") or None)
        async with self._sessions() as session:
            async with session.begin():
                session.add(
                    McpRequest(
                        ts=utcnow(),
                        route=(event.get("route") or "?")[:32],
                        auth_mode=(event.get("auth_mode") or "keyless")[:16],
                        client=(label or "")[:120] or None,
                        tool=(event.get("tool") or "?")[:64],
                        ok=bool(event.get("ok")),
                        http_status=event.get("http_status"),
                        cached=bool(event.get("cached")),
                        duration_ms=event.get("duration_ms"),
                        subject=(event.get("subject") or None),
                        client_ip=(event.get("client_ip") or None),
                    )
                )

    # -- clients ----------------------------------------------------------

    async def save_client(self, client_id: str, document: str) -> None:
        async with self._sessions() as session:
            async with session.begin():
                session.add(OAuthClient(client_id=client_id, data=document, created_at=utcnow()))

    async def load_client(self, client_id: str) -> str | None:
        async with self._sessions() as session:
            row = await session.get(OAuthClient, client_id)
            return row.data if row else None

    # -- authorization codes ---------------------------------------------

    async def save_code(
        self,
        *,
        code: str,
        client_id: str,
        subject: str,
        scopes: list[str],
        code_challenge: str,
        redirect_uri: str,
        redirect_uri_explicit: bool,
        resource: str | None,
        ttl_seconds: int,
    ) -> None:
        async with self._sessions() as session:
            async with session.begin():
                session.add(
                    OAuthCode(
                        code_hash=hash_secret(code),
                        client_id=client_id,
                        subject=subject,
                        scopes=" ".join(scopes),
                        code_challenge=code_challenge,
                        redirect_uri=redirect_uri,
                        redirect_uri_explicit=redirect_uri_explicit,
                        resource=resource,
                        expires_at=utcnow() + timedelta(seconds=ttl_seconds),
                        used=False,
                    )
                )

    async def load_code(self, code: str) -> StoredCode | None:
        async with self._sessions() as session:
            row = await session.get(OAuthCode, hash_secret(code))
            if row is None:
                return None
            return StoredCode(
                code_hash=row.code_hash,
                client_id=row.client_id,
                subject=row.subject,
                scopes=row.scopes.split() if row.scopes else [],
                code_challenge=row.code_challenge,
                redirect_uri=row.redirect_uri,
                redirect_uri_explicit=bool(row.redirect_uri_explicit),
                resource=row.resource,
                expires_at=as_utc(row.expires_at),
                used=bool(row.used),
            )

    async def consume_code(self, code_hash: str) -> bool:
        """Mark a code used. False if it was already used -- a replayed code
        must never mint a second token pair."""
        async with self._sessions() as session:
            async with session.begin():
                result = await session.execute(
                    update(OAuthCode)
                    .where(OAuthCode.code_hash == code_hash, OAuthCode.used.is_(False))
                    .values(used=True)
                )
                return (result.rowcount or 0) == 1

    # -- tokens -----------------------------------------------------------

    async def save_token_pair(
        self,
        *,
        access_token: str,
        refresh_token: str,
        client_id: str,
        subject: str,
        scopes: list[str],
        resource: str | None,
        access_ttl: int,
        refresh_ttl: int,
    ) -> str:
        pair_id = secrets.token_hex(12)
        now = utcnow()
        async with self._sessions() as session:
            async with session.begin():
                session.add(
                    OAuthTokenRow(
                        token_hash=hash_secret(access_token), kind="access", pair_id=pair_id,
                        client_id=client_id, subject=subject, scopes=" ".join(scopes),
                        resource=resource, expires_at=now + timedelta(seconds=access_ttl),
                        revoked=False, created_at=now,
                    )
                )
                session.add(
                    OAuthTokenRow(
                        token_hash=hash_secret(refresh_token), kind="refresh", pair_id=pair_id,
                        client_id=client_id, subject=subject, scopes=" ".join(scopes),
                        resource=resource, expires_at=now + timedelta(seconds=refresh_ttl),
                        revoked=False, created_at=now,
                    )
                )
        return pair_id

    async def load_token(self, token: str, kind: str) -> StoredToken | None:
        async with self._sessions() as session:
            row = await session.get(OAuthTokenRow, hash_secret(token))
            if row is None or row.kind != kind:
                return None
            return StoredToken(
                token_hash=row.token_hash, kind=row.kind, pair_id=row.pair_id,
                client_id=row.client_id, subject=row.subject,
                scopes=row.scopes.split() if row.scopes else [],
                resource=row.resource, expires_at=as_utc(row.expires_at), revoked=bool(row.revoked),
            )

    async def revoke_pair(self, pair_id: str) -> None:
        async with self._sessions() as session:
            async with session.begin():
                await session.execute(
                    update(OAuthTokenRow).where(OAuthTokenRow.pair_id == pair_id).values(revoked=True)
                )

    async def revoke_subject(self, subject: str) -> int:
        """Everything issued to one user, e.g. when the account is disabled."""
        async with self._sessions() as session:
            async with session.begin():
                result = await session.execute(
                    update(OAuthTokenRow)
                    .where(OAuthTokenRow.subject == subject, OAuthTokenRow.revoked.is_(False))
                    .values(revoked=True)
                )
                return result.rowcount or 0

    # -- users (read only) ------------------------------------------------

    async def user_by_api_key(self, api_key: str) -> tuple[str, str] | None:
        """(user_id, email) for a live account holding this key, else None."""
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(User.id, User.email, User.is_active).where(User.api_key == api_key)
                )
            ).first()
        if row is None or row.is_active is False:
            return None
        return str(row.id), row.email or ""

    async def api_key_for_user(self, subject: str) -> str | None:
        """The key a token's tool calls are made with. Cached briefly; a
        disabled account or rotated key takes effect within the TTL."""
        cached = self._key_cache.get(subject)
        now = time.monotonic()
        if cached and now - cached[1] < self.key_cache_ttl:
            return cached[0]

        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(User.api_key, User.is_active).where(User.id == int(subject))
                )
            ).first()
        key = row.api_key if row and row.is_active is not False else None
        self._key_cache[subject] = (key, now)
        if len(self._key_cache) > 10_000:
            self._key_cache = {k: v for k, v in self._key_cache.items() if now - v[1] < self.key_cache_ttl}
        return key

    async def ping(self) -> bool:
        async with self._sessions() as session:
            await session.execute(text("select 1"))
        return True


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))
