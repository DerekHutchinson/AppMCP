"""Runtime SQL proxy (POST /a/{slug}/sql).

The published app's JS posts {sql, params}. We require a valid session (any
authenticated org user), pin execution to the APP's bound datasource (the
request cannot choose another), validate the SQL read-only + schema-allowlisted,
cap rows, and return {columns, rows}. Every query is attributable to the
logged-in user.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import registry
import tool_access
from appsql import run_validated
from session_auth import session_email
from validation import SQLValidationError

logger = logging.getLogger("appmcp.proxy")


def register(router: APIRouter) -> None:
    @router.post("/a/{slug}/sql")
    async def app_sql(slug: str, request: Request):
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

        if not (app.get("datasource") or "").strip():
            return JSONResponse(
                {"error": "This app has no datasource; AppData.query is unavailable."},
                status_code=400,
            )

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

        sql = (payload.get("sql") or "").strip()
        params = payload.get("params") or []
        if not sql:
            return JSONResponse({"error": "Missing 'sql'."}, status_code=400)
        if not isinstance(params, list):
            return JSONResponse({"error": "'params' must be an array."}, status_code=400)

        try:
            result = await run_validated(app["datasource"], sql, params)
        except SQLValidationError as exc:
            return JSONResponse({"error": f"Rejected: {exc}"}, status_code=400)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[proxy] %s query failed on '%s': %s", email, slug, exc)
            return JSONResponse({"error": f"Query failed: {exc}"}, status_code=400)

        logger.info("[proxy] %s ran query on '%s' (%d rows)",
                    email, slug, len(result["rows"]))
        return JSONResponse(result)
