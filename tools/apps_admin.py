"""MCP tools for publishing self-contained HTML apps.

An agent designs a self-contained HTML app, then creates + publishes it here.
Apps live forever at a permanent, Azure-AD-gated URL and appear in the catalog.

The app's HTML must NOT contain credentials or connection strings. To read data
at runtime, call the injected client from the app's own JavaScript:

    const { columns, rows } = await AppData.query(
      "SELECT col_a, col_b FROM schema.table WHERE col_a > $1",
      [someValue]
    );

`AppData.query` posts to this app's scoped, validated, read-only SQL proxy
(bound to the datasource chosen at create time). Constraints to design around:
  * read-only SELECT only, schema-qualified tables, row-capped results
  * positional params $1..$n (passed as the second arg array)
  * strict CSP: attach event listeners in <script> blocks, NOT inline on* handlers
"""
import re
from pathlib import Path

import appcheck
import datasources
import icons
import registry
import s3source
import tool_access
from auth import CURRENT_USER_EMAIL
from config import settings

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_GUIDE_PATH = Path(__file__).resolve().parent.parent / "docs" / "authoring-guide.md"
_THEME_PATH = Path(__file__).resolve().parent.parent / "static" / "dashboard_blank.html"
# Logo served from this origin; safe under the app CSP (img-src 'self').
# Drop your own logo at static/logo.png (a neutral placeholder ships in the repo).
_LOGO_URL = "/static/logo.png"


def _caller(passed: str | None) -> str:
    return (CURRENT_USER_EMAIL.get() or passed or "ai-agent")


def _owns_or_admin(app: dict, caller: str) -> bool:
    """True if `caller` is the app's owner or an admin.

    Per-app authorization for the mutating tools, mirroring the /manage web
    layer's `_owned_or_admin`. The tool-access middleware only gates by ROLE
    (author vs admin); this stops one author from editing another's app.
    """
    if tool_access.is_admin(caller):
        return True
    return (app.get("created_by") or "").strip().lower() == caller.strip().lower()


def _app_url(slug: str) -> str:
    return f"{settings.public_base}/a/{slug}"


def register(mcp) -> None:
    @mcp.tool
    async def get_authoring_guide() -> dict:
        """Read this BEFORE building an app — only when asked to build one.

        Use only when the user explicitly asked you to create/build/make/publish
        an app or dashboard. Do NOT read this (or start building) for a plain data
        question — answer those with the data-exploration tools and reply in chat.

        Returns the app authoring guide: the AppData data contract, the strict-CSP
        constraints (no inline on* handlers, allowed CDNs), how to wire dropdown /
        date-picker filters, and a complete minimal HTML template to start from.
        """
        try:
            return {"ok": True, "guide": _GUIDE_PATH.read_text(encoding="utf-8")}
        except OSError as exc:
            return {"ok": False, "error": f"Guide unavailable: {exc}"}

    @mcp.tool
    async def get_app_theme() -> dict:
        """Get the DEFAULT dashboard theme to start a new app from.

        Only relevant when the user explicitly asked you to build/publish an app
        or dashboard; don't fetch this for a plain data question.

        Use this as the basis for every new app UNLESS the user asks for a
        different / custom look. Returns the full HTML of the template
        (sidebar + topbar + card sections, light theme, Chart.js) with
        `{{ PLACEHOLDER }}` markers and demo-data constants to replace. It is
        already CSP-clean (no inline on* handlers). Keep the logo — it
        loads from `/static/logo.png` (served from this origin). Swap in your
        real title/labels and wire the sections to `AppData.query(...)`.
        """
        try:
            return {
                "ok": True,
                "theme": _THEME_PATH.read_text(encoding="utf-8"),
                "logo_url": _LOGO_URL,
            }
        except OSError as exc:
            return {"ok": False, "error": f"Theme unavailable: {exc}"}

    @mcp.tool
    async def list_categories() -> dict:
        """List the canonical catalog categories to pass to create_app/update_app.

        Pick the closest match for an app's `category` so it lands in the right
        catalog section; unrecognized values fall back to "Other".
        """
        return {"categories": settings.category_list}

    @mcp.tool
    async def list_icons() -> dict:
        """List the catalog icons you can pass as `icon` to create_app/update_app.

        Each app shows one small icon on its catalog card. Pick the name that
        best fits the app's purpose (e.g. "chart-line" for a trend dashboard,
        "gem" for a product app, "truck" for logistics). Unrecognized or omitted
        values fall back to an icon chosen from the app's category. Returns the
        available icon names plus a preview URL template.
        """
        return {
            "icons": list(icons.available()),
            "preview_url_template": "/static/icons/{name}.svg",
            "note": "Omit `icon` to auto-pick one based on the app's category.",
        }

    @mcp.tool
    async def check_app(html: str, has_datasource: bool = True) -> dict:
        """Static-check app HTML BEFORE create_app/publish (no side effects).

        For apps bound to a datasource, catches the common failure of baking
        query results into the HTML instead of loading them at view time via
        AppData.query(). Also flags CSP pitfalls (inline on* handlers, eval).
        Set has_datasource=false for a purely static app (no datasource) to skip
        the anti-baked-data checks. Returns {ok, errors, warnings}: errors block
        create_app unless you pass allow_inline_data=true.
        """
        return appcheck.check_html(html or "", has_datasource=has_datasource)

    @mcp.tool
    async def create_app(
        slug: str,
        title: str,
        html: str,
        datasource: str | None = None,
        s3_source: str | None = None,
        description: str = "",
        category: str | None = None,
        icon: str | None = None,
        created_by: str | None = None,
        access_list: list[str] | None = None,
        allow_inline_data: bool = False,
    ) -> dict:
        """Create a new app as a DRAFT (not yet in the catalog).

        Call this ONLY when the user explicitly asked you to create/build/make or
        publish an app or dashboard. For a plain data question (a number, table,
        or analysis), do NOT create an app — answer with the data-exploration
        tools and reply in chat. If it's unclear whether the user wants a one-off
        answer or a durable published app, ask first.

        FIRST call list_apps to check an equivalent app doesn't already exist. If
        one does, point the user to it (or update it with update_app) rather than
        publishing a duplicate.

        Call get_authoring_guide() first for the AppData contract, CSP rules
        (no inline on* handlers), filter/date-picker patterns, and a template.

        Args:
            slug: URL id, lowercase letters/digits/hyphens, 3-40 chars.
            title: Human-readable app title (shown in the catalog).
            html: A complete, self-contained HTML document. If the app shows data,
                use AppData.query() from your JavaScript to fetch it at runtime
                (see module doc). Do NOT embed credentials, connection strings, or
                query results.
            datasource: SQL source name the app reads from via AppData.query().
                OPTIONAL. When set, the runtime SQL proxy is pinned to this source
                and the app's data must be loaded live (not baked in).
            s3_source: S3 object source the app reads files from via
                AppData.s3.list()/get(). OPTIONAL and independent of `datasource`
                — an app may use a SQL source, an S3 source, both, or neither.
                See list_s3_sources for available names.
            description: Short description for the catalog card.
            category: Which catalog section the app appears under. Choose one of
                the canonical categories (see list_categories / the authoring
                guide); anything unrecognized falls back to "Other". Keeps the
                catalog organized instead of a flat wall of cards.
            icon: Name of the icon shown on the catalog card (see list_icons).
                OPTIONAL. If omitted or unrecognized, an icon is auto-picked from
                the app's category.
            created_by: Optional author identity (defaults to the caller).
            access_list: Optional list of emails allowed to VIEW the published
                app. Omit or pass [] to make it public (any signed-in org user).
                When set, only these users can open it; admins and the creator
                always retain access.
            allow_inline_data: Escape hatch for the anti-baked-data checks (only
                relevant when a datasource is set). Set true for apps whose inline
                data is genuinely static/reference (e.g. a lookup table or map
                geometry).

        Returns a status dict with a preview_url. Publish it with publish_app.
        """
        if not SLUG_RE.match(slug):
            return {"ok": False, "error": "Invalid slug (use a-z, 0-9, -; 3-40 chars)."}
        if not (html or "").strip():
            return {"ok": False, "error": "html is required."}

        # datasource / s3_source are both optional and independent. Validate each
        # only when explicitly provided.
        ds = (datasource or "").strip()
        if ds:
            try:
                datasources.get_source(ds)
            except KeyError as exc:
                return {"ok": False, "error": str(exc)}

        s3 = (s3_source or "").strip()
        if s3:
            try:
                s3source.get_source(s3)
            except KeyError as exc:
                return {"ok": False, "error": str(exc)}

        check = appcheck.check_html(html, has_datasource=bool(ds))
        if check["errors"] and not allow_inline_data:
            return {
                "ok": False,
                "error": "App failed static checks; fix these or pass "
                         "allow_inline_data=true if the data is genuinely static.",
                "errors": check["errors"],
                "warnings": check["warnings"],
            }

        cat = settings.match_category(category)

        ico = (icon or "").strip().lower()
        if ico and not icons.exists(ico):
            return {"ok": False,
                    "error": f"Unknown icon '{ico}'. See list_icons for valid names.",
                    "icons": list(icons.available())}

        try:
            await registry.create_app(slug, title, description, ds, html,
                                      _caller(created_by), access_list, s3, cat, ico)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        return {
            "ok": True,
            "status": "draft",
            "slug": slug,
            "datasource": ds or None,
            "s3_source": s3 or None,
            "category": cat,
            "icon": icons.resolve(ico, cat),
            "preview_url": _app_url(slug),
            "warnings": check["warnings"],
            "message": (
                f"Draft '{slug}' created. Open preview_url to review (you must be "
                f"signed in), then call publish_app to add it to the catalog."
            ),
        }

    @mcp.tool
    async def update_app(
        slug: str,
        title: str | None = None,
        html: str | None = None,
        datasource: str | None = None,
        s3_source: str | None = None,
        description: str | None = None,
        category: str | None = None,
        icon: str | None = None,
        access_list: list[str] | None = None,
        allow_inline_data: bool = False,
    ) -> dict:
        """Update an existing app's HTML / title / datasource. Only sent fields change.

        Only the app's owner (its creator) or an admin may update it; another
        author cannot modify an app they didn't create.

        If you don't already have the app's HTML, call get_app(slug) first.

        datasource: omit to keep the current SQL source; pass a name to rebind;
        pass "" to clear it (no live SQL data).

        s3_source: omit to keep the current S3 source; pass a name to rebind;
        pass "" to clear it. Independent of datasource.

        access_list, when provided, replaces the app's viewer allow list ([] =
        public). Omit it to leave the current access unchanged. To edit only the
        access list, prefer set_app_access.

        When html is sent it is re-checked for baked-in data (only if the app has
        a datasource); pass allow_inline_data=true to bypass those checks for
        static/reference data.
        """
        app = await registry.get(slug)
        if app is None:
            return {"ok": False, "error": f"No app '{slug}'."}
        if not _owns_or_admin(app, _caller(None)):
            return {"ok": False,
                    "error": "Only the app's owner or an admin can modify it."}

        # Keep the existing datasource unless a new one is sent. An empty string
        # explicitly clears it (converts the app to a static, no-data app).
        ds = (app["datasource"] if datasource is None else datasource).strip()
        if ds:
            try:
                datasources.get_source(ds)
            except KeyError as exc:
                return {"ok": False, "error": str(exc)}

        s3 = ((app.get("s3_source") or "") if s3_source is None else s3_source).strip()
        if s3:
            try:
                s3source.get_source(s3)
            except KeyError as exc:
                return {"ok": False, "error": str(exc)}

        cat = (app.get("category") or "Other") if category is None \
            else settings.match_category(category)

        # icon: omit to keep current; "" clears it (auto-pick at render).
        if icon is None:
            ico = app.get("icon") or ""
        else:
            ico = icon.strip().lower()
            if ico and not icons.exists(ico):
                return {"ok": False,
                        "error": f"Unknown icon '{ico}'. See list_icons for valid names.",
                        "icons": list(icons.available())}

        check = {"warnings": []}
        if html is not None:
            check = appcheck.check_html(html, has_datasource=bool(ds))
            if check["errors"] and not allow_inline_data:
                return {
                    "ok": False,
                    "error": "App failed static checks; fix these or pass "
                             "allow_inline_data=true if the data is genuinely static.",
                    "errors": check["errors"],
                    "warnings": check["warnings"],
                }

        try:
            await registry.update_app(
                slug,
                title if title is not None else app["title"],
                description if description is not None else app["description"],
                ds,
                html if html is not None else app["html"],
                access_list if access_list is not None else app["access_list"],
                s3,
                cat,
                ico,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "slug": slug, "status": app["status"],
                "category": cat, "icon": icons.resolve(ico, cat),
                "preview_url": _app_url(slug),
                "warnings": check["warnings"]}

    @mcp.tool
    async def set_app_access(slug: str, access_list: list[str]) -> dict:
        """Set who may VIEW a published app.

        Args:
            slug: The app's slug.
            access_list: Emails allowed to open the app. Pass [] to make it
                public (any signed-in org user). Admins and the app's creator
                always retain access regardless of this list.
        """
        app = await registry.get(slug)
        if app is None:
            return {"ok": False, "error": f"No app '{slug}'."}
        if not _owns_or_admin(app, _caller(None)):
            return {"ok": False,
                    "error": "Only the app's owner or an admin can change access."}
        ok = await registry.set_access(slug, access_list or [])
        if not ok:
            return {"ok": False, "error": "Could not update access."}
        updated = await registry.get(slug)
        allow = updated["access_list"] if updated else []
        return {
            "ok": True,
            "slug": slug,
            "access": "public" if not allow else "restricted",
            "access_list": allow,
        }

    @mcp.tool
    async def transfer_app(slug: str, new_owner: str) -> dict:
        """Transfer ownership of an app to another author.

        Use this to hand an app you built off to another person so they (and their
        own AI agent) can make further changes: it sets the app's owner
        (`created_by`) to `new_owner`. Only the current owner or an admin may
        transfer, and the new owner must already be an author or admin (so they
        can actually manage/edit it) — grant them the author role first if needed.

        After transfer, the new owner sees the app in /manage and retains view
        access; the previous owner loses owner-only access unless they're an admin
        or on the app's access_list.

        Args:
            slug: The app's slug.
            new_owner: Email of the new owner (must be an author or admin).
        """
        app = await registry.get(slug)
        if app is None:
            return {"ok": False, "error": f"No app '{slug}'."}

        caller = _caller(None)
        current_owner = (app.get("created_by") or "").strip().lower()
        if not _owns_or_admin(app, caller):
            return {"ok": False,
                    "error": "Only the app's owner or an admin can transfer it."}

        target = (new_owner or "").strip().lower()
        if not EMAIL_RE.match(target):
            return {"ok": False, "error": "new_owner must be a valid email address."}
        if target == current_owner:
            return {"ok": False, "error": f"App is already owned by {target}."}
        if not tool_access.can_author(target):
            return {"ok": False,
                    "error": f"{target} is not an author/admin, so they couldn't "
                             "manage the app. Grant them the author role first."}

        ok = await registry.set_owner(slug, target)
        if not ok:
            return {"ok": False, "error": "Transfer failed."}
        return {
            "ok": True,
            "slug": slug,
            "previous_owner": current_owner or None,
            "new_owner": target,
            "message": f"'{slug}' is now owned by {target}.",
        }

    @mcp.tool
    async def preview_app(slug: str) -> dict:
        """Get the (signed-in) URL to preview an app, published or draft."""
        app = await registry.get(slug)
        if app is None:
            return {"ok": False, "error": f"No app '{slug}'."}
        return {"ok": True, "slug": slug, "status": app["status"],
                "url": _app_url(slug)}

    @mcp.tool
    async def publish_app(slug: str, published_by: str | None = None) -> dict:
        """Publish a draft app to the catalog (live for any authenticated user)."""
        app = await registry.get(slug)
        if app is None:
            return {"ok": False, "error": f"No app '{slug}'."}
        if not _owns_or_admin(app, _caller(None)):
            return {"ok": False,
                    "error": "Only the app's owner or an admin can publish it."}
        ok = await registry.set_status(slug, "published", by=_caller(published_by))
        if not ok:
            return {"ok": False, "error": "Publish failed."}
        return {
            "action": "open_in_browser",
            "label": app["title"],
            "url": _app_url(slug),
            "status": "published",
            "message": f"'{slug}' is live in the catalog.",
        }

    @mcp.tool
    async def unpublish_app(slug: str) -> dict:
        """Remove an app from the catalog (back to draft)."""
        app = await registry.get(slug)
        if app is None:
            return {"ok": False, "error": f"No app '{slug}'."}
        if not _owns_or_admin(app, _caller(None)):
            return {"ok": False,
                    "error": "Only the app's owner or an admin can unpublish it."}
        ok = await registry.set_status(slug, "draft", by=_caller(None))
        if not ok:
            return {"ok": False, "error": f"No app '{slug}'."}
        return {"ok": True, "slug": slug, "status": "draft"}

    @mcp.tool
    async def list_apps(status: str | None = None) -> dict:
        """List existing apps (slug, title, description, category, owner, URL).

        CALL THIS BEFORE BUILDING A NEW APP. Check whether an equivalent app
        already exists so you don't recreate one someone already published — if a
        close match exists, point the user to its `url` (or offer to update it via
        update_app) instead of creating a duplicate. Match on intent, not just an
        exact title: compare title + description + category + datasource.

        To edit an existing app without prior context, call get_app(slug) to fetch
        its full HTML, then update_app.

        Args:
            status: Optional filter — "published" (live in the catalog) or "draft".
                Omit to list everything. Pass "published" to see what users can
                already open today.
        """
        rows = await registry.list_apps(status)
        return {
            "count": len(rows),
            "apps": [
                {
                    "slug": r["slug"],
                    "title": r["title"],
                    "description": r.get("description") or "",
                    "status": r["status"],
                    "datasource": r["datasource"],
                    "s3_source": r.get("s3_source") or None,
                    "category": r.get("category") or "Other",
                    "icon": icons.resolve(r.get("icon"), r.get("category")),
                    "created_by": r.get("created_by"),
                    "access": "public" if not r.get("access_list") else "restricted",
                    "access_list": r.get("access_list") or [],
                    "url": _app_url(r["slug"]),
                }
                for r in rows
            ],
        }

    @mcp.tool
    async def get_app(slug: str) -> dict:
        """Fetch an app's full source HTML + metadata so you can edit it.

        Use this when the user asks you to change an existing app and you don't
        already have its HTML in context (e.g. a fresh chat, or another agent
        built it). Flow: list_apps → get_app(slug) → edit the `html` → update_app.

        Only the app's owner or an admin may read the source (same gate as
        update_app / the /manage Export button). Returns the stored HTML as
        authored — not the served variant with AppData/CSP injection.

        Args:
            slug: The app's slug (from list_apps).
        """
        app = await registry.get(slug)
        if app is None:
            return {"ok": False, "error": f"No app '{slug}'."}
        if not _owns_or_admin(app, _caller(None)):
            return {"ok": False,
                    "error": "Only the app's owner or an admin can read its source."}
        return {
            "ok": True,
            "slug": app["slug"],
            "title": app["title"],
            "description": app.get("description") or "",
            "status": app["status"],
            "datasource": app.get("datasource") or None,
            "s3_source": app.get("s3_source") or None,
            "category": app.get("category") or "Other",
            "icon": icons.resolve(app.get("icon"), app.get("category")),
            "created_by": app.get("created_by"),
            "access": "public" if not app.get("access_list") else "restricted",
            "access_list": app.get("access_list") or [],
            "url": _app_url(slug),
            "html": app.get("html") or "",
            "message": (
                "Edit the `html` (and any metadata fields), then call update_app "
                "with the same slug to save. Re-publish only if status is draft "
                "or you need a fresh publish."
            ),
        }

    @mcp.tool
    async def get_app_url(slug: str) -> dict:
        """Get the permanent catalog URL for a PUBLISHED app."""
        app = await registry.get(slug, status="published")
        if app is None:
            return {"ok": False, "error": f"No published app '{slug}'."}
        return {
            "action": "open_in_browser",
            "label": app["title"],
            "url": _app_url(slug),
        }
