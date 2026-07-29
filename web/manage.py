"""Manage UI (author/admin): list, publish/unpublish, and remove apps.

Authors see their own apps; admins see all. Creation/editing of the HTML itself
happens through the MCP tools (create_app / update_app); this UI is the
lifecycle + housekeeping surface.
"""
import html
import re

from fastapi import APIRouter, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

import appcheck
import icons
import registry
import tool_access
from appsql import run_validated
from config import settings
from session_auth import session_email
from validation import SQLValidationError
from web.render import render_manage, render_notice

# AppData.* helpers an app can call; the diagnostics tool reports which ones a
# given app's HTML actually references.
_CAPABILITY_MARKERS = {
    "query": "AppData.query",
    "queryPages": "AppData.queryPages",
    "sendEmail": "AppData.sendEmail",
    "graph": "AppData.graph",
    "census": "AppData.census",
    "vision": "AppData.vision",
    "llm": "AppData.llm",
    "s3": "AppData.s3",
}


def _capabilities_used(html_text: str) -> list[str]:
    text = html_text or ""
    return [name for name, marker in _CAPABILITY_MARKERS.items() if marker in text]

# SQL extraction for the manage "Inspect" action: recover the base SQL statements
# an app runs, straight from its stored source (no DB, no runtime capture).
#
# Strategy: it is call-site-aware. We locate each AppData.query()/queryPages()
# call, read its FIRST argument expression (balanced, string-literal aware), and
# reduce that expression to SQL text -- concatenating adjacent string literals,
# keeping template-literal bodies verbatim (so `${expr}` interpolation shows as a
# placeholder), and marking any other spliced sub-expression as /*expr*/. If the
# whole first argument is just a variable, we resolve that one variable's
# assignment. This yields the "root" statement even when SELECT and FROM live in
# different fragments -- the case a plain page-wide SELECT...FROM scan misses.
# Runtime *values* are intentionally NOT resolved; the placeholders show where
# they go. If no call sites are found we fall back to a page-wide literal scan.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# A real statement starts with SELECT or WITH (read-only apps). Used to reject
# incidental "select ... from" matches inside HTML/markup or prose.
_SQL_START_RE = re.compile(r"^\s*(?:with|select)\b", re.IGNORECASE)
# If any of these appear, the candidate is markup, not SQL -- so we drop it.
_HTML_MARKERS = (
    "<!doctype", "<html", "<head", "<body", "<div", "<span", "<table",
    "<main", "<aside", "<section", "<header", "<footer", "<nav", "<ul",
    "<li>", "<li ", "<script", "<style", "<button", "<input", "<label",
    "<select", "<canvas", "<svg", "</",
)
# One regex, three alternatives, so matches come back in source order.
_LITERAL_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|`([^`]*)`')
_QUERY_CALL_RE = re.compile(r"AppData\s*\.\s*(?:query|queryPages)\s*\(")
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")
_ASSIGN_TMPL = r"\b(?:const|let|var)\s+{name}\s*=\s*"


def _literal_value(m: "re.Match") -> str:
    return m.group(1) or m.group(2) or m.group(3) or ""


def _unescape_js(s: str) -> str:
    return (s.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "")
             .replace('\\"', '"').replace("\\'", "'"))


def _strip_sql_comment_prefix(sql: str) -> str:
    """Drop leading whitespace and SQL/reduction comments so the first real token
    is exposed. Handles `-- line`, `/* block */`, and our `/*expr*/` placeholders."""
    s = sql.lstrip()
    while True:
        if s.startswith("--"):
            nl = s.find("\n")
            s = "" if nl == -1 else s[nl + 1:].lstrip()
        elif s.startswith("/*"):
            end = s.find("*/")
            s = "" if end == -1 else s[end + 2:].lstrip()
        else:
            return s


def _is_sql(candidate: str | None) -> bool:
    """True only for text that is really a SQL statement (starts with SELECT/WITH)
    and contains no HTML markup -- the guard that keeps the SQL view SQL-only."""
    if not candidate:
        return False
    low = candidate.lower()
    if any(marker in low for marker in _HTML_MARKERS):
        return False
    return bool(_SQL_START_RE.match(_strip_sql_comment_prefix(candidate)))


def _consume_string(text: str, i: int) -> int:
    """Return the index just past the string/template literal starting at `text[i]`.

    Handles backslash escapes; for template literals, skips over `${ ... }`
    interpolations (with brace tracking and nested strings) so an embedded `}` or
    backtick inside an expression doesn't close the literal prematurely.
    """
    n = len(text)
    q = text[i]
    j = i + 1
    if q == "`":
        while j < n:
            c = text[j]
            if c == "\\":
                j += 2
                continue
            if c == "`":
                return j + 1
            if c == "$" and j + 1 < n and text[j + 1] == "{":
                j += 2
                depth = 1
                while j < n and depth:
                    cc = text[j]
                    if cc == "\\":
                        j += 2
                        continue
                    if cc in "\"'`":
                        j = _consume_string(text, j)
                        continue
                    if cc == "{":
                        depth += 1
                    elif cc == "}":
                        depth -= 1
                    j += 1
                continue
            j += 1
        return n
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == q:
            return j + 1
        j += 1
    return n


def _scan_balanced(text: str, start: int, stop: set[str]) -> int:
    """Index of the first top-level char in `stop` at/after `start`.

    Skips balanced (), [], {} and string/template literals so a comma inside a
    nested call/array or a `;` inside a string doesn't end the scan early.
    """
    n = len(text)
    i = start
    depth = 0
    while i < n:
        c = text[i]
        if c in "\"'`":
            i = _consume_string(text, i)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0 and c in stop:
                return i
            depth = max(0, depth - 1)
        elif depth == 0 and c in stop:
            return i
        i += 1
    return n


def _short(expr: str, limit: int = 60) -> str:
    expr = " ".join(expr.split())
    return expr if len(expr) <= limit else expr[: limit - 1] + "…"


def _reduce_expr_to_sql(expr: str) -> str | None:
    """Reduce a JS expression to SQL text, or None if it holds no string literal.

    Concatenates string/template literals in order; drops the `+` between them;
    renders any other spliced sub-expression (a variable, a call) as `/*expr*/`.
    """
    lits = list(_LITERAL_RE.finditer(expr))
    if not lits:
        return None
    parts: list[str] = []
    prev_end: int | None = None
    for m in lits:
        if prev_end is not None:
            gap = expr[prev_end:m.start()].strip()
            gap = gap.strip("+").strip()
            if gap:
                parts.append(" /*" + _short(gap) + "*/ ")
        is_tmpl = m.group(3) is not None
        val = _literal_value(m)
        parts.append(val if is_tmpl else _unescape_js(val))
        prev_end = m.end()
    return "".join(parts).strip()


def _resolve_sql_var(text: str, name: str) -> str | None:
    """Reduce the last `const/let/var name = <expr>;` assignment to SQL text."""
    assign = re.compile(_ASSIGN_TMPL.format(name=re.escape(name)))
    last = None
    for m in assign.finditer(text):
        last = m
    if last is None:
        return None
    # Stop at the top-level ';' only -- newlines are common inside multi-line
    # concatenations, so stopping on them would truncate before FROM.
    end = _scan_balanced(text, last.end(), {";"})
    return _reduce_expr_to_sql(text[last.end():end])


def _extract_sql_fallback(text: str) -> list[str]:
    """Page-wide literal scan (used only when no query() call sites are found)."""
    lits = [(m.start(), m.end(), _literal_value(m))
            for m in _LITERAL_RE.finditer(text)]
    merged: list[str] = []
    i, n = 0, len(lits)
    while i < n:
        _, end, combined = lits[i]
        j = i + 1
        while j < n:
            gap = text[end:lits[j][0]].strip()
            if gap == "+":
                combined += lits[j][2]
            elif gap.startswith("+") and gap.endswith("+"):
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
        sql = _unescape_js(raw).strip()
        if not _is_sql(sql):
            continue
        key = " ".join(sql.split()).lower()
        if key not in seen:
            seen.add(key)
            found.append(sql)
    return found


def extract_sql(html_text: str) -> list[str]:
    """Return the distinct base SQL statements an app runs, in source order.

    Reads the first argument of each AppData.query()/queryPages() call and
    reduces it to SQL text (see the module comment). Runtime values are left as
    `${expr}` / `/*expr*/` placeholders. Falls back to a page-wide literal scan
    when no call sites are present.
    """
    text = html_text or ""
    found: list[str] = []
    seen: set[str] = set()
    for call in _QUERY_CALL_RE.finditer(text):
        end = _scan_balanced(text, call.end(), {",", ")"})
        arg = text[call.end():end].strip()
        sql = _reduce_expr_to_sql(arg)
        if sql is None:                       # first arg is a bare variable
            ident = _IDENT_RE.fullmatch(arg)
            if ident:
                sql = _resolve_sql_var(text, arg)
        if not _is_sql(sql):
            continue
        key = " ".join(sql.split()).lower()
        if key not in seen:
            seen.add(key)
            found.append(sql)
    if not found:
        return _extract_sql_fallback(text)
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
        return HTMLResponse(render_manage(
            email=html.escape(email), apps=apps,
            is_admin=tool_access.is_admin(email)))

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

    @router.get("/manage/{slug}/export")
    async def export_app(slug: str, request: Request):
        email, err = _require_author(request)
        if err:
            return err
        app, err = await _owned_or_admin(slug, email)
        if err:
            return err
        # Download the stored source (what the author/agent wrote), not the
        # served variant (which has the AppData bootstrap + CSP nonce injected).
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", slug) or "app"
        return Response(
            content=app.get("html") or "",
            media_type="text/html; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{safe}.html"'},
        )

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

    @router.get("/manage/{slug}/source")
    async def inspect_source(slug: str, request: Request):
        email, err = _auth_json(request)
        if err:
            return err
        app, err = await _owned_or_admin(slug, email)
        if err:
            return err
        # The stored source (what the author/agent wrote), not the served variant
        # that has the AppData bootstrap + CSP nonce injected.
        return JSONResponse({
            "ok": True,
            "slug": slug,
            "html": app.get("html") or "",
        })

    def _auth_admin_json(request: Request):
        email, err = _auth_json(request)
        if err:
            return None, err
        if not tool_access.is_admin(email):
            return None, JSONResponse(
                {"ok": False, "error": "Admins only."}, status_code=403)
        return email, None

    @router.get("/manage/{slug}/diagnostics")
    async def diagnostics(slug: str, request: Request):
        email, err = _auth_admin_json(request)
        if err:
            return err
        app, err = await _owned_or_admin(slug, email)
        if err:
            return err
        html_text = app.get("html") or ""
        ds = app.get("datasource") or None
        lint = appcheck.check_html(html_text, has_datasource=bool(ds))
        ping = None
        if ds:
            try:
                await run_validated(ds, "SELECT 1 AS ok", [])
                ping = {"ok": True, "detail": "SELECT 1 succeeded"}
            except Exception as exc:  # noqa: BLE001 - report any failure verbatim
                ping = {"ok": False, "detail": str(exc)[:400]}
        return JSONResponse({
            "ok": True,
            "slug": slug,
            "status": app.get("status"),
            "datasource": ds,
            "s3_source": app.get("s3_source") or None,
            "category": app.get("category"),
            "icon": icons.resolve(app.get("icon"), app.get("category")),
            "created_by": app.get("created_by"),
            "created_at": app.get("created_at"),
            "updated_at": app.get("updated_at"),
            "published_by": app.get("published_by"),
            "published_at": app.get("published_at"),
            "access_list": app.get("access_list") or [],
            "html_bytes": len(html_text),
            "capabilities_used": _capabilities_used(html_text),
            "lint": {
                "ok": bool(lint.get("ok")),
                "errors": lint.get("errors", []),
                "warnings": lint.get("warnings", []),
            },
            "datasource_ping": ping,
        })

    @router.post("/manage/{slug}/run-query")
    async def run_query_debug(slug: str, request: Request):
        email, err = _auth_admin_json(request)
        if err:
            return err
        app, err = await _owned_or_admin(slug, email)
        if err:
            return err
        ds = app.get("datasource") or None
        if not ds:
            return JSONResponse(
                {"ok": False, "error": "This app has no datasource."},
                status_code=400)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "Invalid JSON body."},
                                status_code=400)
        sql = (body.get("sql") or "").strip()
        params = body.get("params") or []
        if not sql:
            return JSONResponse({"ok": False, "error": "Provide a SELECT statement."},
                                status_code=400)
        if not isinstance(params, list):
            return JSONResponse({"ok": False, "error": "params must be a list."},
                                status_code=400)
        try:
            result = await run_validated(ds, sql, params)
        except SQLValidationError as exc:
            return JSONResponse({"ok": False, "error": f"Rejected: {exc}"},
                                status_code=400)
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": f"Unknown datasource: {exc}"},
                                status_code=400)
        except Exception as exc:  # noqa: BLE001 - surface the DB/driver error text
            return JSONResponse({"ok": False, "error": f"Query failed: {exc}"},
                                status_code=400)
        return JSONResponse({
            "ok": True,
            "datasource": ds,
            "columns": result.get("columns", []),
            "rows": result.get("rows", []),
            "has_more": bool(result.get("has_more")),
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

    @router.post("/manage/{slug}/transfer")
    async def transfer(slug: str, request: Request):
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
        target = (body.get("new_owner") or "").strip().lower()
        if not _EMAIL_RE.match(target):
            return JSONResponse({"ok": False, "error": "Enter a valid email address."},
                                status_code=400)
        if not tool_access.can_author(target):
            return JSONResponse(
                {"ok": False, "error": f"{target} is not an author/admin, so they "
                 "couldn't manage the app. Grant them the author role first."},
                status_code=400)
        ok = await registry.set_owner(slug, target)
        return JSONResponse({"ok": ok, "slug": slug, "new_owner": target})

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

    @router.post("/manage/{slug}/icon")
    async def set_icon(slug: str, request: Request):
        email, err = _auth_json(request)
        if err:
            return err
        app, err = await _owned_or_admin(slug, email)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "Invalid JSON body."},
                                status_code=400)
        # "" clears the icon (auto-pick from category); any other value must exist.
        raw = (body.get("icon") or "").strip().lower()
        if raw and not icons.exists(raw):
            return JSONResponse({"ok": False, "error": f"Unknown icon '{raw}'."},
                                status_code=400)
        ok = await registry.set_icon(slug, raw)
        resolved = icons.resolve(raw, app.get("category"))
        return JSONResponse({"ok": ok, "slug": slug, "icon": resolved})
