"""Azure AD OAuth proxy + bearer-token identity for the /mcp endpoint.

Adapted from KPIMCP. The server proxies the OAuth flow to Azure AD so the MCP
client obtains a bearer token, then a lightweight ASGI middleware decodes the
token's email claim and stashes it in a ContextVar (CURRENT_USER_EMAIL) that the
tool-access layer reads to decide which tools the caller may see/use.

Only `/mcp` is protected here. The web UI (catalog, app viewer, manage) is
gated separately by a browser session (see session_auth.py).

NOTE: the JWT is decoded WITHOUT signature verification (Azure AD is the sole
issuer reachable through this proxy). Switch to JWKS verification if the threat
model changes.
"""
import base64
import json
import logging
from contextvars import ContextVar
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from config import settings

logger = logging.getLogger("appmcp.auth")

CURRENT_USER_EMAIL: ContextVar[str | None] = ContextVar("CURRENT_USER_EMAIL", default=None)


def decode_jwt_payload(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * ((4 - len(payload) % 4) % 4)
    return json.loads(base64.urlsafe_b64decode(payload.encode()))


def extract_email(claims: dict) -> str | None:
    return (
        claims.get("email")
        or claims.get("preferred_username")
        or claims.get("upn")
        or claims.get("unique_name")
    )


def get_user_email_from_token(token: str) -> str | None:
    try:
        email = extract_email(decode_jwt_payload(token))
        return email.lower() if email else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[auth] token decode failed: %s", exc)
        return None


class BearerTokenMiddleware:
    """Authenticate the caller on `/mcp` only and set CURRENT_USER_EMAIL."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("utf-8", "replace")
        token = (
            auth_header.removeprefix("Bearer ").strip()
            if auth_header.startswith("Bearer ")
            else None
        )

        async def send_json(body: dict, status: int, extra_headers: dict | None = None):
            data = json.dumps(body).encode()
            hdrs = [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(data)).encode()),
            ]
            for k, v in (extra_headers or {}).items():
                hdrs.append((k.encode(), v.encode()))
            await send({"type": "http.response.start", "status": status, "headers": hdrs})
            await send({"type": "http.response.body", "body": data})

        if not token:
            await send_json(
                {"error": "unauthorized", "detail": "Missing Bearer token"}, 401,
                {"www-authenticate": (
                    'Bearer resource_metadata='
                    f'"{settings.public_base}/.well-known/oauth-protected-resource"'
                )},
            )
            return

        email = get_user_email_from_token(token)
        if not email:
            await send_json({"error": "unauthorized", "detail": "Invalid token"}, 401)
            return
        if not email.endswith(settings.allowed_email_domain):
            await send_json(
                {"error": "forbidden",
                 "detail": f"Only {settings.allowed_email_domain} users are allowed"},
                403,
            )
            return
        allowlist = settings.allowed_email_set
        if allowlist and email not in allowlist:
            await send_json(
                {"error": "forbidden", "detail": "User is not in the allowed email list"},
                403,
            )
            return

        ctx = CURRENT_USER_EMAIL.set(email)
        try:
            await self.app(scope, receive, send)
        finally:
            CURRENT_USER_EMAIL.reset(ctx)


def register_oauth_routes(app: FastAPI) -> None:
    async def oauth_protected_resource(request: Request):
        return JSONResponse({
            "resource": f"{settings.public_base}/mcp",
            "authorization_servers": [settings.public_base],
        })

    async def oauth_metadata(request: Request):
        return JSONResponse({
            "issuer": settings.public_base,
            "authorization_endpoint": f"{settings.public_base}/authorize",
            "token_endpoint": f"{settings.public_base}/token",
            "scopes_supported": ["openid", "profile", "email", "User.Read"],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "registration_endpoint": f"{settings.public_base}/register",
            "client_id": settings.azure_client_id,
        })

    async def register(request: Request):
        return JSONResponse({
            "client_id": settings.azure_client_id,
            "redirect_uris": [
                "https://claude.ai/api/mcp/auth_callback",
                "https://claude.ai/api/mcp/auth/callback",
            ],
        })

    async def authorize(request: Request):
        params = dict(request.query_params)
        params["client_id"] = settings.azure_client_id
        params.setdefault("scope", "openid profile email User.Read")
        for key in ("resource", "response_mode"):
            params.pop(key, None)
        ms = (f"https://login.microsoftonline.com/"
              f"{settings.azure_tenant_id}/oauth2/v2.0/authorize")
        return RedirectResponse(url=f"{ms}?{urlencode(params)}")

    async def token(request: Request):
        form = await request.form()
        data = dict(form)
        data["client_id"] = settings.azure_client_id
        if settings.azure_client_secret:
            data["client_secret"] = settings.azure_client_secret
        data.pop("resource", None)
        ms = (f"https://login.microsoftonline.com/"
              f"{settings.azure_tenant_id}/oauth2/v2.0/token")
        async with httpx.AsyncClient() as client:
            resp = await client.post(ms, data=data, timeout=30)
        return JSONResponse(resp.json(), status_code=resp.status_code)

    app.add_api_route("/.well-known/oauth-authorization-server", oauth_metadata, methods=["GET"])
    app.add_api_route("/.well-known/oauth-protected-resource", oauth_protected_resource, methods=["GET"])
    app.add_api_route("/.well-known/oauth-protected-resource/mcp", oauth_protected_resource, methods=["GET"])
    app.add_api_route("/authorize", authorize, methods=["GET"])
    app.add_api_route("/token", token, methods=["POST"])
    app.add_api_route("/register", register, methods=["POST", "GET"])
