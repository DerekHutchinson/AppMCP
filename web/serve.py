"""Serve a published app (GET /a/{slug}).

Any authenticated user may open a PUBLISHED app. Drafts are visible only to
their author + admins. The HTML is served sandboxed (strict CSP + nonce) with
the AppData client injected (see web/render.render_app).
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import registry
import tool_access
from session_auth import session_email
from web.render import render_app, render_notice


def register(router: APIRouter) -> None:
    @router.get("/a/{slug}", response_class=HTMLResponse)
    async def serve_app(slug: str, request: Request):
        email = session_email(request)
        if not email:
            return RedirectResponse(f"/login?next=/a/{slug}", status_code=303)

        app = await registry.get(slug)
        if app is None:
            return HTMLResponse(render_notice("Not found", f"No app '{slug}'."),
                                status_code=404)

        if app["status"] != "published":
            is_owner = (app.get("created_by") or "").lower() == email.lower()
            if not (is_owner or tool_access.is_admin(email)):
                return HTMLResponse(
                    render_notice("Not available",
                                  "This app is a draft and not published yet."),
                    status_code=403,
                )

        if not tool_access.viewer_allowed(app, email):
            return HTMLResponse(
                render_notice("Permission denied",
                              "You don't have access to this app."),
                status_code=403,
            )

        body, csp = render_app(slug, app["html"])
        return HTMLResponse(body, headers={
            "Content-Security-Policy": csp,
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "SAMEORIGIN",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
        })
