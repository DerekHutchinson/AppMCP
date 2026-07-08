"""Catalog page (GET /): published apps, visible to any authenticated user."""
import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import registry
import tool_access
from session_auth import session_email
from web.render import render_catalog


def register(router: APIRouter) -> None:
    @router.get("/", response_class=HTMLResponse)
    async def catalog(request: Request):
        email = session_email(request)
        if not email:
            return RedirectResponse("/login?next=/", status_code=303)
        apps = await registry.list_apps("published")
        return HTMLResponse(render_catalog(
            email=html.escape(email),
            can_author=tool_access.can_author(email),
            apps=apps,
        ))
