"""App email endpoint (POST /a/{slug}/email).

The published app's JS posts {to, subject, html|text}. We require a valid
session (any authenticated org user who may view the app), restrict recipients
to the org domain (no open relay), send via SendGrid server-side (the app never
holds the key or sets the From address), and attribute every send to the
logged-in user. Mirrors web/proxy.py.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import registry
import tool_access
from config import settings
from emailer import EmailError, send_app_email
from session_auth import session_email

logger = logging.getLogger("appmcp.email")


def register(router: APIRouter) -> None:
    @router.post("/a/{slug}/email")
    async def app_email(slug: str, request: Request):
        if not settings.email_configured:
            return JSONResponse(
                {"error": "Email is not configured on this server."},
                status_code=503,
            )

        email = session_email(request)
        if not email:
            return JSONResponse({"error": "Not signed in."}, status_code=401)

        app = await registry.get(slug)
        if app is None:
            return JSONResponse({"error": f"No app '{slug}'."}, status_code=404)

        if app["status"] != "published":
            is_owner = (app.get("created_by") or "").lower() == email.lower()
            if not (is_owner or tool_access.is_admin(email)):
                return JSONResponse({"error": "App is not published."}, status_code=403)

        if not tool_access.viewer_allowed(app, email):
            return JSONResponse({"error": "Permission denied."}, status_code=403)

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

        try:
            result = await send_app_email(
                slug=slug,
                sender=email,
                to=payload.get("to"),
                subject=payload.get("subject") or "",
                html=payload.get("html"),
                text=payload.get("text") or payload.get("body"),
            )
        except EmailError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[email] %s send failed on '%s': %s", email, slug, exc)
            return JSONResponse({"error": "Email send failed."}, status_code=502)

        logger.info("[email] %s sent from '%s' to %d recipient(s)",
                    email, slug, result["sent"])
        return JSONResponse({"ok": True, **result})
