"""App Microsoft Graph endpoint (POST /a/{slug}/graph).

The published app's JS posts {method, path, query, body}. We require a valid
session (any authenticated org user who may view the app), then forward the call
to Microsoft Graph using the SIGNED-IN USER's delegated token (never app-held).
Calls are pinned to /me and a method+path allowlist in grapher.py, so an app can
only ever touch the current user's own mailbox/calendar/profile. Mirrors
web/proxy.py and web/email.py.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import registry
import tool_access
from config import settings
from grapher import GraphError, graph_request
from session_auth import session_email, session_sid

logger = logging.getLogger("appmcp.graph")


def register(router: APIRouter) -> None:
    @router.post("/a/{slug}/graph")
    async def app_graph(slug: str, request: Request):
        if not settings.graph_configured:
            return JSONResponse(
                {"error": "Microsoft Graph is not configured on this server."},
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

        sid = session_sid(request)
        if not sid:
            return JSONResponse(
                {"error": "No Graph session; sign out and sign in again to grant access."},
                status_code=401,
            )

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

        try:
            result = await graph_request(
                sid,
                method=payload.get("method"),
                path=payload.get("path"),
                query=payload.get("query"),
                body=payload.get("body"),
            )
        except GraphError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[graph] %s call failed on '%s': %s", email, slug, exc)
            return JSONResponse({"error": "Graph call failed."}, status_code=502)

        logger.info("[graph] %s called %s %s on '%s'",
                    email, payload.get("method", "GET"), payload.get("path"), slug)
        return JSONResponse(result)
