"""The login and consent page.

Two ways to prove an Apiguru account:

* **Dashboard e-mail + password.** Forwarded to the backend's own `/login`
  route over the private network, so credential checking, lockouts and
  rate limits stay in one place and this server never sees a password hash.
* **API key.** Looked up directly. Covers Google-sign-in accounts, which
  have no password, and is what an API customer has to hand anyway.

Either way the result is a `users.id`, which becomes the token's subject.

A pending sign-in is bound to the browser that first opens the page (an
HttpOnly SameSite cookie), so knowing a `txn` is not enough to complete it
from elsewhere, and a cross-site form post never carries the cookie.
"""

from __future__ import annotations

import hmac
import html
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from .provider import PENDING_TTL_SECONDS, ApiguruOAuthProvider, describe
from .store import Store, hash_secret

logger = logging.getLogger(__name__)

LOGIN_PATH = "/oauth/login"

# Every response of the sign-in page: never framed (clickjacking), never
# cached, never leaks the txn in a Referer to another site. same-origin, not
# no-referrer: the latter makes browsers send `Origin: null` on our own form.
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
}

# Attempts per client address per window. Brute force goes nowhere at this
# rate; a person mistyping a password is not inconvenienced.
ATTEMPT_LIMIT = 10
ATTEMPT_WINDOW = 600.0


class _Attempts:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and hits[0] <= now - ATTEMPT_WINDOW:
            hits.popleft()
        if len(hits) >= ATTEMPT_LIMIT:
            return False
        hits.append(now)
        return True


attempts = _Attempts()


def _client_ip(request: Request) -> str:
    real = (request.headers.get("X-Real-IP") or "").strip()
    if real:
        return real
    return request.client.host if request.client else "unknown"


def _api_base() -> str:
    """Where the backend's /login lives. Private network in the hosted
    deployment; the public API otherwise."""
    internal = os.environ.get("APIGURU_API_INTERNAL_URL", "").strip().rstrip("/")
    if internal:
        return internal
    from ..spec import api_info

    return api_info()["base_url"].rstrip("/")


async def _login_with_password(email: str, password: str, client_ip: str) -> tuple[str, str] | None:
    """(user_id, email) via the backend, or None."""
    url = f"{_api_base()}/login"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                url,
                json={"email": email, "password": password},
                headers={"X-Forwarded-For": client_ip, "User-Agent": "apiguru-mcp-oauth/1.0"},
            )
    except httpx.HTTPError as exc:
        logger.warning("Backend login unreachable: %s", exc)
        return None
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    user_id = body.get("user_id")
    if not user_id:
        return None
    return str(user_id), email


def _page(
    *,
    client_name: str,
    txn: str,
    error: str | None = None,
    request_info: dict | None = None,
) -> str:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    host = ""
    if request_info and request_info.get("redirect_uri"):
        host = urlparse(request_info["redirect_uri"]).hostname or ""
    where = f" ({html.escape(host)})" if host else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect Apiguru</title>
<style>
 body{{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:#f6f7f9;color:#1c2330;margin:0}}
 main{{max-width:440px;margin:6vh auto;background:#fff;border:1px solid #e3e6eb;border-radius:12px;padding:28px 30px}}
 h1{{font-size:20px;margin:0 0 6px}} p{{margin:8px 0}} .muted{{color:#5c6675;font-size:13px}}
 label{{display:block;font-size:13px;font-weight:600;margin:14px 0 4px}}
 input{{width:100%;box-sizing:border-box;padding:10px 12px;border:1px solid #cfd5dd;border-radius:8px;font-size:15px}}
 button{{margin-top:16px;width:100%;padding:11px;border:0;border-radius:8px;background:#1f6feb;color:#fff;font-size:15px;font-weight:600;cursor:pointer}}
 .or{{text-align:center;color:#8a93a1;font-size:12px;margin:18px 0 4px}} .err{{background:#fdecec;color:#a12626;border-radius:8px;padding:9px 12px}}
 form+form{{border-top:1px solid #eceff3;margin-top:8px;padding-top:4px}}
</style></head><body><main>
<h1>Connect Apiguru</h1>
<p><strong>{html.escape(client_name)}</strong>{where} wants to call the Apiguru Amazon Data API on your behalf.
Calls made through it bill <em>your</em> Apiguru account at your plan's rates.</p>
<p class="muted">The app chose that name itself; Apiguru has not verified it. Continue only if you just asked this app to connect.</p>
{err}
<form method="post" action="{LOGIN_PATH}">
 <input type="hidden" name="txn" value="{html.escape(txn)}">
 <input type="hidden" name="mode" value="password">
 <label for="email">Dashboard e-mail</label><input id="email" name="email" type="email" autocomplete="username" required>
 <label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required>
 <button type="submit">Sign in and allow</button>
</form>
<p class="or">or</p>
<form method="post" action="{LOGIN_PATH}">
 <input type="hidden" name="txn" value="{html.escape(txn)}">
 <input type="hidden" name="mode" value="apikey">
 <label for="api_key">API key <span class="muted">(dashboard &rarr; API key; works for Google sign-in accounts)</span></label>
 <input id="api_key" name="api_key" type="password" autocomplete="off" required>
 <button type="submit">Use this key and allow</button>
</form>
<p class="muted">No account? <a href="https://dash.apiguru.app/register">Create one</a>. You can revoke this connection any time from the client that asked for it.</p>
</main></body></html>"""


def _html(content: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(content, status_code=status_code, headers=SECURITY_HEADERS)


def create_login_routes(provider: ApiguruOAuthProvider, store: Store) -> list[Route]:
    parts = urlparse(provider.login_url)
    own_origin = f"{parts.scheme}://{parts.netloc}".lower()
    secure = parts.scheme == "https"
    # __Host- pins the cookie to this exact host over HTTPS; plain HTTP
    # (local development) cannot carry the prefix.
    bind_cookie = "__Host-apg_oauth" if secure else "apg_oauth"

    def expired(message: str) -> Response:
        return _html(_page(client_name="This client", txn="", error=message), status_code=400)

    async def get_login(request: Request) -> Response:
        txn = request.query_params.get("txn", "")
        pending = provider.pending(txn) if txn else None
        if pending is None:
            return expired("This sign-in link has expired. Go back to the app and connect again.")
        browser = request.cookies.get(bind_cookie, "")
        if not (16 <= len(browser) <= 64):
            browser = secrets.token_urlsafe(24)
        if pending.browser is None:
            pending.browser = hash_secret(browser)
        elif not hmac.compare_digest(pending.browser, hash_secret(browser)):
            logger.warning("OAuth login page for client %s opened in a second browser from %s",
                           pending.client_id, _client_ip(request))
            return expired("This sign-in link is already open in another browser. Go back to the app and connect again.")
        response = _html(_page(client_name=pending.client_name, txn=txn, request_info=describe(pending.params)))
        # Refreshed on every view so a cookie shared by several sign-ins in
        # one browser outlives the newest of them.
        response.set_cookie(bind_cookie, browser, max_age=PENDING_TTL_SECONDS, path="/",
                            secure=secure, httponly=True, samesite="lax")
        return response

    async def post_login(request: Request) -> Response:
        origin = (request.headers.get("origin") or "").rstrip("/").lower()
        if origin and origin != "null" and origin != own_origin:
            logger.warning("OAuth login post from foreign origin %r refused (%s)", origin, _client_ip(request))
            return _html(_page(client_name="This client", txn="",
                               error="This form can only be sent from the Apiguru sign-in page."), status_code=403)
        form = await request.form()
        txn = str(form.get("txn", ""))
        pending = provider.pending(txn) if txn else None
        if pending is None:
            return expired("This sign-in session has expired. Go back to the app and connect again.")
        browser = request.cookies.get(bind_cookie, "")
        if pending.browser is None or not browser or not hmac.compare_digest(pending.browser, hash_secret(browser)):
            logger.warning("OAuth login post for client %s without this sign-in's browser cookie (%s)",
                           pending.client_id, _client_ip(request))
            return expired("This browser did not open this sign-in. Go back to the app and connect again "
                           "(cookies must be allowed for this page).")

        ip = _client_ip(request)
        if not attempts.allow(ip):
            return _html(
                _page(client_name=pending.client_name, txn=txn, error="Too many attempts. Wait ten minutes and try again.",
                      request_info=describe(pending.params)),
                status_code=429,
            )

        mode = str(form.get("mode", "password"))
        identity: tuple[str, str] | None = None
        if mode == "apikey":
            key = str(form.get("api_key", "")).strip()
            if key:
                identity = await store.user_by_api_key(key)
            failure = "That API key was not recognised, or the account is disabled."
        else:
            email = str(form.get("email", "")).strip().lower()
            password = str(form.get("password", ""))
            if email and password:
                identity = await _login_with_password(email, password, ip)
            failure = "E-mail or password did not match. Google sign-in accounts: use your API key instead."

        if identity is None:
            logger.info("OAuth login failed (%s) from %s for client %s", mode, ip, pending.client_id)
            return _html(
                _page(client_name=pending.client_name, txn=txn, error=failure, request_info=describe(pending.params)),
                status_code=401,
            )

        subject, _email = identity
        target = await provider.complete_login(txn, subject)
        logger.info("OAuth login ok: user %s -> client %s", subject, pending.client_id)
        # The code is in this Location; it must not be cached either.
        return RedirectResponse(target, status_code=302,
                                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    return [
        Route(LOGIN_PATH, get_login, methods=["GET"]),
        Route(LOGIN_PATH, post_login, methods=["POST"]),
    ]
