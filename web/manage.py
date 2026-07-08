"""Manage UI (author/admin): list, publish/unpublish, and remove apps.

Authors see their own apps; admins see all. Creation/editing of the HTML itself
happens through the MCP tools (create_app / update_app); this UI is the
lifecycle + housekeeping surface.
"""
import html
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import registry
import tool_access
from session_auth import session_email
from web.render import render_manage, render_notice


def _require_author(request: Request):
    email = session_email(request)
    if not email:
        return None, RedirectResponse("/login?next=/manage", status_code=303)
    if not tool_access.can_author(email):
        return None, HTMLResponse(
            render_notice("Not authorized", "Only authors can manage apps."),
            status_code=403,
        )
    return email, None


def _visible(apps: list[dict], email: str) -> list[dict]:
    if tool_access.is_admin(email):
        return apps
    return [a for a in apps if (a.get("created_by") or "").lower() == email.lower()]


def register(router: APIRouter) -> None:
    @router.get("/manage", response_class=HTMLResponse)
    async def manage_page(request: Request):
        email, err = _require_author(request)
        if err:
            return err
        apps = _visible(await registry.list_apps(), email)
        return HTMLResponse(render_manage(email=html.escape(email), apps=apps))

    async def _owned_or_admin(slug: str, email: str):
        app = await registry.get(slug)
        if app is None:
            return None, JSONResponse({"ok": False, "error": f"No app '{slug}'."},
                                      status_code=404)
        if not tool_access.is_admin(email) and \
                (app.get("created_by") or "").lower() != email.lower():
            return None, JSONResponse({"ok": False, "error": "Not your app."},
                                      status_code=403)
        return app, None

    def _auth_json(request: Request):
        email = session_email(request)
        if not email:
            return None, JSONResponse({"ok": False, "error": "Not signed in."},
                                      status_code=401)
        if not tool_access.can_author(email):
            return None, JSONResponse({"ok": False, "error": "Not authorized."},
                                      status_code=403)
        return email, None

    @router.post("/manage/{slug}/publish")
    async def publish(slug: str, request: Request):
        email, err = _auth_json(request)
        if err:
            return err
        _, err = await _owned_or_admin(slug, email)
        if err:
            return err
        ok = await registry.set_status(slug, "published", by=email)
        return JSONResponse({"ok": ok, "slug": slug, "status": "published"})

    @router.post("/manage/{slug}/unpublish")
    async def unpublish(slug: str, request: Request):
        email, err = _auth_json(request)
        if err:
            return err
        _, err = await _owned_or_admin(slug, email)
        if err:
            return err
        ok = await registry.set_status(slug, "draft", by=email)
        return JSONResponse({"ok": ok, "slug": slug, "status": "draft"})

    @router.post("/manage/{slug}/delete")
    async def delete(slug: str, request: Request):
        email, err = _auth_json(request)
        if err:
            return err
        _, err = await _owned_or_admin(slug, email)
        if err:
            return err
        ok = await registry.delete_app(slug)
        return JSONResponse({"ok": ok, "slug": slug})

    @router.post("/manage/{slug}/access")
    async def set_access(slug: str, request: Request):
        email, err = _auth_json(request)
        if err:
            return err
        _, err = await _owned_or_admin(slug, email)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "Invalid JSON body."},
                                status_code=400)
        # Accept either {"access_list": [...]} or {"emails": "a@x, b@x"}.
        entries = body.get("access_list")
        if entries is None:
            raw = body.get("emails") or ""
            entries = re.split(r"[,\s;]+", raw)
        if not isinstance(entries, list):
            return JSONResponse({"ok": False, "error": "access_list must be a list."},
                                status_code=400)
        ok = await registry.set_access(slug, entries)
        return JSONResponse({"ok": ok, "slug": slug})
