"""App S3 object endpoint (POST /a/{slug}/s3).

The published app's JS posts {op, prefix|key}. We require a valid session (any
authenticated org user who may view the app), pin execution to the APP's bound
S3 source (the request cannot choose another bucket), and forward a read-only
list/get to Amazon S3 with credentials held only server-side. Mirrors
web/proxy.py, web/email.py, and web/graph.py.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import registry
import s3source
import tool_access
from config import settings
from session_auth import session_email

logger = logging.getLogger("appmcp.s3")


def register(router: APIRouter) -> None:
    @router.post("/a/{slug}/s3")
    async def app_s3(slug: str, request: Request):
        if not settings.s3_configured:
            return JSONResponse(
                {"error": "S3 sources are not configured on this server."},
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

        source = (app.get("s3_source") or "").strip()
        if not source:
            return JSONResponse(
                {"error": "This app has no S3 source; AppData.s3 is unavailable."},
                status_code=400,
            )

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

        op = (payload.get("op") or "").strip().lower()
        try:
            if op == "list":
                objects = await s3source.list_objects(
                    source,
                    prefix=payload.get("prefix") or "",
                    max_keys=payload.get("max_keys"),
                    session_key=email,
                )
                result = {"objects": objects, "count": len(objects)}
            elif op == "get":
                key = (payload.get("key") or "").strip()
                if not key:
                    return JSONResponse({"error": "Missing 'key'."}, status_code=400)
                result = await s3source.get_object(source, key, session_key=email)
            else:
                return JSONResponse(
                    {"error": "'op' must be 'list' or 'get'."}, status_code=400
                )
        except s3source.S3Error as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[s3] %s op '%s' failed on '%s': %s", email, op, slug, exc)
            return JSONResponse({"error": "S3 request failed."}, status_code=502)

        logger.info("[s3] %s %s on '%s' (source=%s)", email, op, slug, source)
        return JSONResponse(result)
