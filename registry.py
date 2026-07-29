"""App registry: a small SQLite store of agent-authored HTML apps.

Each row is a self-contained HTML app bound to ONE datasource, with a lifecycle
status: 'draft' -> 'published'. Authors create drafts and publish directly (no
approval gate). Published apps appear in the catalog for any authenticated user;
drafts are visible only to their author + admins.

The HTML is stored inline in the row. That is fine at app scale; swap in object
storage later behind the same async API if apps grow large.
"""
import asyncio
import json
import sqlite3
from datetime import datetime, timezone

import apphistory
from config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
    slug         TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    datasource   TEXT NOT NULL,
    html         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft',
    created_by   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    published_by TEXT,
    published_at TEXT,
    access_list  TEXT NOT NULL DEFAULT '[]',
    s3_source    TEXT NOT NULL DEFAULT '',
    category     TEXT NOT NULL DEFAULT 'Other',
    icon         TEXT NOT NULL DEFAULT ''
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.registry_db)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    # Migrate pre-existing DBs that predate newer columns.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(apps)")}
    if "access_list" not in cols:
        conn.execute("ALTER TABLE apps ADD COLUMN access_list TEXT NOT NULL DEFAULT '[]'")
    if "s3_source" not in cols:
        conn.execute("ALTER TABLE apps ADD COLUMN s3_source TEXT NOT NULL DEFAULT ''")
    if "category" not in cols:
        conn.execute("ALTER TABLE apps ADD COLUMN category TEXT NOT NULL DEFAULT 'Other'")
    if "icon" not in cols:
        conn.execute("ALTER TABLE apps ADD COLUMN icon TEXT NOT NULL DEFAULT ''")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _norm_access(access_list) -> list[str]:
    """Normalize an access list to sorted, de-duped, lowercased emails."""
    if not access_list:
        return []
    return sorted({str(e).strip().lower() for e in access_list if str(e).strip()})


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    raw = d.get("access_list")
    try:
        d["access_list"] = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        d["access_list"] = []
    return d


# ---- sync core (run in a thread from async callers) ----
def _create(slug, title, description, datasource, html, created_by,
            access_list=None, s3_source="", category="Other", icon="") -> None:
    with _connect() as conn:
        if conn.execute("SELECT 1 FROM apps WHERE slug=?", (slug,)).fetchone():
            raise ValueError(f"App slug '{slug}' already exists")
        now = _now()
        conn.execute(
            "INSERT INTO apps (slug, title, description, datasource, html, status, "
            "created_by, created_at, updated_at, access_list, s3_source, category, icon) "
            "VALUES (?,?,?,?,?, 'draft', ?, ?, ?, ?, ?, ?, ?)",
            (slug, title, description, datasource, html, created_by, now, now,
             json.dumps(_norm_access(access_list)), s3_source or "",
             category or "Other", icon or ""),
        )


def _update(slug, title, description, datasource, html, access_list,
            s3_source="", category="Other", icon="") -> None:
    with _connect() as conn:
        if not conn.execute("SELECT 1 FROM apps WHERE slug=?", (slug,)).fetchone():
            raise ValueError(f"App slug '{slug}' not found")
        conn.execute(
            "UPDATE apps SET title=?, description=?, datasource=?, html=?, "
            "access_list=?, s3_source=?, category=?, icon=?, updated_at=? WHERE slug=?",
            (title, description, datasource, html,
             json.dumps(_norm_access(access_list)), s3_source or "",
             category or "Other", icon or "", _now(), slug),
        )


def _set_icon(slug, icon) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE apps SET icon=?, updated_at=? WHERE slug=?",
            (icon or "", _now(), slug),
        )
        return cur.rowcount > 0


def _set_category(slug, category) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE apps SET category=?, updated_at=? WHERE slug=?",
            (category or "Other", _now(), slug),
        )
        return cur.rowcount > 0


def _set_access(slug, access_list) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE apps SET access_list=?, updated_at=? WHERE slug=?",
            (json.dumps(_norm_access(access_list)), _now(), slug),
        )
        return cur.rowcount > 0


def _set_owner(slug, email) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE apps SET created_by=?, updated_at=? WHERE slug=?",
            ((email or "").strip().lower(), _now(), slug),
        )
        return cur.rowcount > 0


def _set_status(slug, status, by) -> bool:
    with _connect() as conn:
        if status == "published":
            cur = conn.execute(
                "UPDATE apps SET status=?, published_by=?, published_at=?, updated_at=? "
                "WHERE slug=?",
                (status, by, _now(), _now(), slug),
            )
        else:
            cur = conn.execute(
                "UPDATE apps SET status=?, updated_at=? WHERE slug=?",
                (status, _now(), slug),
            )
        return cur.rowcount > 0


def _get(slug, status=None):
    with _connect() as conn:
        if status:
            row = conn.execute(
                "SELECT * FROM apps WHERE slug=? AND status=?", (slug, status)
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM apps WHERE slug=?", (slug,)).fetchone()
        return _row_to_dict(row) if row else None


def _list(status=None):
    with _connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM apps WHERE status=? ORDER BY updated_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM apps ORDER BY updated_at DESC"
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


def _delete(slug) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM apps WHERE slug=?", (slug,))
        return cur.rowcount > 0


# ---- async wrappers ----
async def _record_history(action: str, slug: str) -> None:
    """Mirror the current app state to git history (best-effort, never raises)."""
    if not settings.git_history_enabled:
        return
    app = None if action == "delete" else await asyncio.to_thread(_get, slug)
    await asyncio.to_thread(apphistory.record, action, slug, app)


async def init_registry() -> None:
    await asyncio.to_thread(lambda: _connect().close())


async def create_app(slug, title, description, datasource, html, created_by,
                     access_list=None, s3_source="", category="Other", icon=""):
    result = await asyncio.to_thread(
        _create, slug, title, description, datasource, html, created_by,
        access_list, s3_source, category, icon
    )
    await _record_history("create", slug)
    return result


async def update_app(slug, title, description, datasource, html, access_list,
                     s3_source="", category="Other", icon=""):
    result = await asyncio.to_thread(
        _update, slug, title, description, datasource, html, access_list,
        s3_source, category, icon
    )
    await _record_history("update", slug)
    return result


async def set_category(slug, category) -> bool:
    ok = await asyncio.to_thread(_set_category, slug, category)
    if ok:
        await _record_history("update", slug)
    return ok


async def set_icon(slug, icon) -> bool:
    ok = await asyncio.to_thread(_set_icon, slug, icon)
    if ok:
        await _record_history("update", slug)
    return ok


async def set_access(slug, access_list) -> bool:
    ok = await asyncio.to_thread(_set_access, slug, access_list)
    if ok:
        await _record_history("update", slug)
    return ok


async def set_owner(slug, email) -> bool:
    ok = await asyncio.to_thread(_set_owner, slug, email)
    if ok:
        await _record_history("update", slug)
    return ok


async def set_status(slug, status, by):
    ok = await asyncio.to_thread(_set_status, slug, status, by)
    if ok:
        action = "publish" if status == "published" else "unpublish"
        await _record_history(action, slug)
    return ok


async def get(slug, status=None):
    return await asyncio.to_thread(_get, slug, status)


async def list_apps(status=None):
    return await asyncio.to_thread(_list, status)


async def delete_app(slug) -> bool:
    ok = await asyncio.to_thread(_delete, slug)
    if ok:
        await _record_history("delete", slug)
    return ok
