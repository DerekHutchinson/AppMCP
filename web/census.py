"""App U.S. Census Bureau Data API endpoint (POST /a/{slug}/census).

The published app's JS posts {dataset, year, get, for, in, ucgid, predicates,
descriptive}. We require a valid session (any authenticated org user who may view
the app), then forward the call to the public Census Data API with the API key
held only server-side. Census data is public and read-only, so — like the email
and Graph proxies — this is a shared capability any app may use (it is not bound
to a per-app source). Mirrors web/proxy.py, web/email.py, web/graph.py.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import registry
import tool_access
from censussource import CensusError, census_request
from config import settings
from session_auth import session_email

logger = logging.getLogger("appmcp.census")


def register(router: APIRouter) -> None:
    @router.post("/a/{slug}/census")
    async def app_census(slug: str, request: Request):
        if not settings.census_configured:
            return JSONResponse(
                {"error": "The Census Data API is not configured on this server."},
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
            result = await census_request(
                email,
                dataset=payload.get("dataset"),
                year=payload.get("year"),
                get=payload.get("get"),
                for_=payload.get("for"),
                in_=payload.get("in"),
                ucgid=payload.get("ucgid"),
                predicates=payload.get("predicates"),
                descriptive=bool(payload.get("descriptive")),
            )
        except CensusError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[census] %s call failed on '%s': %s", email, slug, exc)
            return JSONResponse({"error": "Census call failed."}, status_code=502)

        logger.info("[census] %s queried %s (%s) on '%s'",
                    email, payload.get("dataset"), payload.get("year"), slug)
        return JSONResponse(result)
