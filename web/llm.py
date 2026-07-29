"""App LLM chat endpoint (POST /a/{slug}/llm).

The published app's JS posts {system, messages|prompt, model, temperature,
max_tokens, json}. We require a valid session (any authenticated org user who may
view the app), then forward a chat completion to OpenAI/Anthropic with the API
key held only server-side. Like the email/Graph/Census/Vision proxies this is a
shared capability any app may use (not bound to a per-app source). Mirrors
web/census.py.
"""
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import registry
import tool_access
from config import settings
from llmsource import LLMError, llm_complete
from session_auth import session_email

logger = logging.getLogger("appmcp.llm")


def register(router: APIRouter) -> None:
    @router.post("/a/{slug}/llm")
    async def app_llm(slug: str, request: Request):
        if not settings.llm_configured:
            return JSONResponse(
                {"error": "The LLM proxy is not configured on this server."},
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

        # Cap the raw body before buffering/parsing (multimodal payloads are large).
        raw = await request.body()
        cap = settings.llm_max_request_bytes
        if cap > 0 and len(raw) > cap:
            return JSONResponse(
                {"error": f"Request too large (max {cap} bytes)."},
                status_code=413,
            )
        try:
            payload = json.loads(raw)
        except ValueError:
            return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

        try:
            result = await llm_complete(
                email,
                system=payload.get("system"),
                prompt=payload.get("prompt"),
                messages=payload.get("messages"),
                model=payload.get("model"),
                temperature=payload.get("temperature"),
                max_tokens=payload.get("max_tokens"),
                json_mode=bool(payload.get("json")),
                images=payload.get("images"),
                files=payload.get("files"),
            )
        except LLMError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[llm] %s call failed on '%s': %s", email, slug, exc)
            return JSONResponse({"error": "LLM call failed."}, status_code=502)

        logger.info("[llm] %s ran %s on '%s'", email, result.get("model"), slug)
        return JSONResponse(result)
