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
from config import settings
from session_auth import session_email
from web.render import render_manage, render_notice

# SQL extraction for the manage "Inspect" action: pull string literals that look
# like SQL out of the stored HTML so a reviewer can read what an app queries. A
# heuristic (not a JS parser): find '..' / ".." / `..` literals, stitch together
# adjacent literals joined by string concatenation (`"a" + "b"`, common for
# multi-line SQL), then keep the ones containing SELECT ... FROM. Catches inline
# AppData.query("..."), template-literal SQL, and concatenated SQL. Apps that
# splice in runtime expressions are shown with a /*…*/ placeholder for the gap.
_SQL_LOOKS = re.compile(r"\bselect\b[\s\S]*?\bfrom\b", re.IGNORECASE)
# One regex, three alternatives, so matches come back in source order.
_LITERAL_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|`([^`]*)`')


def _literal_value(m: "re.Match") -> str:
    return m.group(1) or m.group(2) or m.group(3) or ""


def _unescape_js(s: str) -> str:
    return (s.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "")
             .replace('\\"', '"').replace("\\'", "'"))


def extract_sql(html_text: str) -> list[str]:
    """Return distinct SQL-looking strings found in the app HTML.

    Merges concatenation runs (literals separated only by `+`, optionally with a
    runtime expression between them) so multi-line SQL isn't truncated.
    """
    text = html_text or ""
    lits = [(m.start(), m.end(), _literal_value(m))
            for m in _LITERAL_RE.finditer(text)]

    merged: list[str] = []
    i, n = 0, len(lits)
    while i < n:
        _, end, combined = lits[i]
        j = i + 1
        while j < n:
            gap = text[end:lits[j][0]].strip()
            if gap == "+":                                  # plain concatenation
                combined += lits[j][2]
            elif gap.startswith("+") and gap.endswith("+"):  # expr spliced in
                combined += " /*…*/ " + lits[j][2]
            else:
                break
            end = lits[j][1]
            j += 1
        merged.append(combined)
        i = j

    found: list[str] = []
    seen: set[str] = set()
    for raw in merged:
        if not _SQL_LOOKS.search(raw):
            continue
        sql = _unescape_js(raw).strip()
        key = " ".join(sql.split()).lower()
        if key not in seen:
            seen.add(key)
            found.append(sql)
    return found


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

    @router.get("/manage/{slug}/sql")
    async def inspect_sql(slug: str, request: Request):
        email, err = _auth_json(request)
        if err:
            return err
        app, err = await _owned_or_admin(slug, email)
        if err:
            return err
        return JSONResponse({
            "ok": True,
            "slug": slug,
            "datasource": app.get("datasource") or None,
            "queries": extract_sql(app.get("html") or ""),
        })

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

    @router.post("/manage/{slug}/category")
    async def set_category(slug: str, request: Request):
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
        cat = settings.match_category(body.get("category"))
        ok = await registry.set_category(slug, cat)
        return JSONResponse({"ok": ok, "slug": slug, "category": cat})
