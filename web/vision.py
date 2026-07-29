"""App Google Cloud Vision endpoint (POST /a/{slug}/vision).

The published app's JS posts an image (inline base64 content) plus the features
it wants ({image, features, imageContext} or a full {requests:[...]} batch). We
require a valid session (any authenticated org user who may view the app), cap
the upload size, then forward the annotate call to the Vision API with the API
key held only server-side. Like the email/Graph/Census proxies this is a shared
capability any app may use (not bound to a per-app source). Mirrors
web/census.py.
"""
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import registry
import tool_access
from config import settings
from session_auth import session_email
from visionsource import VisionError, vision_request

logger = logging.getLogger("appmcp.vision")


def register(router: APIRouter) -> None:
    @router.post("/a/{slug}/vision")
    async def app_vision(slug: str, request: Request):
        if not settings.vision_configured:
            return JSONResponse(
                {"error": "The Vision API is not configured on this server."},
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

        # Cap the upload before buffering/parsing it (images are large).
        raw = await request.body()
        cap = settings.vision_max_request_bytes
        if cap > 0 and len(raw) > cap:
            return JSONResponse(
                {"error": f"Image payload too large (max {cap} bytes)."},
                status_code=413,
            )

        try:
            payload = json.loads(raw)
        except ValueError:
            return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

        try:
            result = await vision_request(
                email,
                requests=payload.get("requests"),
                image=payload.get("image"),
                features=payload.get("features"),
                image_context=payload.get("imageContext"),
            )
        except VisionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[vision] %s call failed on '%s': %s", email, slug, exc)
            return JSONResponse({"error": "Vision call failed."}, status_code=502)

        logger.info("[vision] %s ran annotate on '%s'", email, slug)
        return JSONResponse(result)
