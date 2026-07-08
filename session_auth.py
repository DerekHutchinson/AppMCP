"""Browser session login for the web UI (Azure AD auth-code flow).

Gates the catalog, the app viewer, the runtime SQL proxy, and the manage UI.
A human signs in via the standard server-side authorization-code flow; we then
issue an HMAC-signed session cookie (signed with app_secret). Any authenticated
org user may VIEW published apps; authoring/manage is further gated by role
(see tool_access.py).

NOTE: like auth.py, id_token claims are read without signature verification.
The code is exchanged directly with Azure over TLS, so the token is trusted as
coming from Azure; switch to JWKS verification if the threat model changes.

Azure app registration must include the redirect URI:
    {public_base}/auth/callback
"""
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from auth import decode_jwt_payload, extract_email
from config import settings

logger = logging.getLogger("appmcp.session_auth")

COOKIE_NAME = "appmcp_session"
CALLBACK_PATH = "/auth/callback"
STATE_TTL_SECONDS = 600


def _sign(raw_b64: str) -> str:
    return hmac.new(
        settings.app_secret.encode(), raw_b64.encode(), hashlib.sha256
    ).hexdigest()


def _make_token(data: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
    return f"{raw}.{_sign(raw)}"


def _read_token(token: str) -> dict | None:
    try:
        raw, sig = token.split(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(raw)):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(raw.encode()))
    except Exception:  # noqa: BLE001
        return None
    if int(data.get("exp", 0)) < int(time.time()):
        return None
    return data


def session_email(request: Request) -> str | None:
    """Return the verified, non-expired session email, or None.

    In local dev (LOCAL_MODE=true, AUTH_ENABLED=false) a fixed dev identity is
    assumed so the web UI works without any Azure sign-in.
    """
    if settings.local_dev_bypass:
        return settings.local_dev_email
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    data = _read_token(token)
    if not data or data.get("k") != "sess":
        return None
    return data.get("email")


def _clear_session_cookie(response) -> None:
    response.set_cookie(
        COOKIE_NAME, "", max_age=0, expires=0,
        path="/", httponly=True, secure=True, samesite="lax",
    )


def _redirect_uri() -> str:
    return f"{settings.public_base}{CALLBACK_PATH}"


def _ms_endpoint(kind: str) -> str:
    return (f"https://login.microsoftonline.com/"
            f"{settings.azure_tenant_id}/oauth2/v2.0/{kind}")


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def _notice_page(title: str, message: str, status: int = 400) -> HTMLResponse:
    body = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>{t}</title><link rel='stylesheet' href='/static/appmcp.css'>"
        "</head><body><div class='notice'><h1>{t}</h1>"
        "<p>{m}</p><p class='muted'><a href='/login'>Try signing in again</a></p>"
        "</div></body></html>"
    ).format(t=title, m=message)
    return HTMLResponse(body, status_code=status)


def register_routes(app: FastAPI) -> None:
    async def login(request: Request):
        if not (settings.azure_tenant_id and settings.azure_client_id):
            return _notice_page("Login unavailable",
                                "Azure AD is not configured on this server.", 503)
        state = _make_token({
            "k": "state",
            "n": secrets.token_urlsafe(8),
            "next": _safe_next(request.query_params.get("next")),
            "exp": int(time.time()) + STATE_TTL_SECONDS,
        })
        params = {
            "client_id": settings.azure_client_id,
            "response_type": "code",
            "redirect_uri": _redirect_uri(),
            "scope": "openid profile email",
            "state": state,
            "response_mode": "query",
        }
        return RedirectResponse(f"{_ms_endpoint('authorize')}?{urlencode(params)}")

    async def callback(request: Request):
        q = request.query_params
        if q.get("error"):
            return _notice_page("Sign-in failed",
                                q.get("error_description", q.get("error")), 400)

        code = q.get("code")
        state = _read_token(q.get("state", ""))
        if not code or not state or state.get("k") != "state":
            return _notice_page("Sign-in failed", "Invalid or expired login state.")

        data = {
            "client_id": settings.azure_client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _redirect_uri(),
            "scope": "openid profile email",
        }
        if settings.azure_client_secret:
            data["client_secret"] = settings.azure_client_secret

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(_ms_endpoint("token"), data=data, timeout=30)
        except httpx.HTTPError as exc:
            logger.warning("[session-auth] token request failed: %s", exc)
            return _notice_page("Sign-in failed", "Could not reach the identity provider.", 502)

        if resp.status_code != 200:
            logger.warning("[session-auth] token exchange %s: %s",
                           resp.status_code, resp.text[:200])
            return _notice_page("Sign-in failed", "Token exchange was rejected.", 401)

        tokens = resp.json()
        id_token = tokens.get("id_token") or tokens.get("access_token")
        email = (extract_email(decode_jwt_payload(id_token)) or "").lower() if id_token else ""
        if not email:
            return _notice_page("Sign-in failed", "No email claim in the token.", 401)
        if not email.endswith(settings.allowed_email_domain):
            return _notice_page("Access denied",
                                f"Only {settings.allowed_email_domain} accounts may sign in.", 403)
        allowlist = settings.allowed_email_set
        if allowlist and email not in allowlist:
            return _notice_page("Access denied", "Your account is not on the allow list.", 403)

        ttl = settings.session_ttl_seconds
        session = _make_token({"k": "sess", "email": email,
                               "exp": int(time.time()) + ttl})
        redirect = RedirectResponse(_safe_next(state.get("next")), status_code=303)
        redirect.set_cookie(
            COOKIE_NAME, session, max_age=ttl, httponly=True, secure=True,
            samesite="lax", path="/",
        )
        logger.info("[session-auth] signed in: %s", email)
        return redirect

    async def logout(request: Request):
        if settings.local_dev_bypass or not (
            settings.azure_tenant_id and settings.azure_client_id
        ):
            target = "/login"
        else:
            params = {}
            if settings.azure_post_logout_redirect_url:
                params["post_logout_redirect_uri"] = settings.azure_post_logout_redirect_url
            target = _ms_endpoint("logout")
            if params:
                target += "?" + urlencode(params)

        redirect = RedirectResponse(target, status_code=303)
        _clear_session_cookie(redirect)
        return redirect

    app.add_api_route("/login", login, methods=["GET"])
    app.add_api_route(CALLBACK_PATH, callback, methods=["GET"])
    app.add_api_route("/logout", logout, methods=["GET", "POST"])
