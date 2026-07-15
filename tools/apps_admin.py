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
import registry
import s3source
from auth import CURRENT_USER_EMAIL
from config import settings

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")
_GUIDE_PATH = Path(__file__).resolve().parent.parent / "docs" / "authoring-guide.md"
_THEME_PATH = Path(__file__).resolve().parent.parent / "static" / "dashboard_blank.html"
# Logo served from this origin; safe under the app CSP (img-src 'self').
# Drop your own logo at static/logo.png (not included in the repo).
_LOGO_URL = "/static/logo.png"


def _caller(passed: str | None) -> str:
    return (CURRENT_USER_EMAIL.get() or passed or "ai-agent")


def _app_url(slug: str) -> str:
    return f"{settings.public_base}/a/{slug}"


def register(mcp) -> None:
    @mcp.tool
    async def get_authoring_guide() -> dict:
        """Read this BEFORE building an app.

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
        created_by: str | None = None,
        access_list: list[str] | None = None,
        allow_inline_data: bool = False,
    ) -> dict:
        """Create a new app as a DRAFT (not yet in the catalog).

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

        try:
            await registry.create_app(slug, title, description, ds, html,
                                      _caller(created_by), access_list, s3, cat)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        return {
            "ok": True,
            "status": "draft",
            "slug": slug,
            "datasource": ds or None,
            "s3_source": s3 or None,
            "category": cat,
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
        access_list: list[str] | None = None,
        allow_inline_data: bool = False,
    ) -> dict:
        """Update an existing app's HTML / title / datasource. Only sent fields change.

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
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "slug": slug, "status": app["status"],
                "category": cat, "preview_url": _app_url(slug),
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
        ok = await registry.set_status(slug, "draft", by=_caller(None))
        if not ok:
            return {"ok": False, "error": f"No app '{slug}'."}
        return {"ok": True, "slug": slug, "status": "draft"}

    @mcp.tool
    async def list_apps(status: str | None = None) -> dict:
        """List apps, optionally filtered by status ('draft' or 'published')."""
        rows = await registry.list_apps(status)
        return {
            "count": len(rows),
            "apps": [
                {
                    "slug": r["slug"],
                    "title": r["title"],
                    "status": r["status"],
                    "datasource": r["datasource"],
                    "s3_source": r.get("s3_source") or None,
                    "category": r.get("category") or "Other",
                    "created_by": r.get("created_by"),
                    "access": "public" if not r.get("access_list") else "restricted",
                    "access_list": r.get("access_list") or [],
                    "url": _app_url(r["slug"]),
                }
                for r in rows
            ],
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
