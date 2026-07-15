"""Server-side store for delegated Microsoft Graph tokens.

The browser session cookie only carries {email, sid}; the actual Graph access +
refresh tokens live here, keyed by session id (sid), in a small SQLite table on
the persistent data volume (next to the app registry). Tokens are encrypted at
rest with a key derived from app_secret, so the raw DB file never exposes usable
credentials. Used by grapher.py to call Graph as the signed-in user.

The lifecycle mirrors the browser session: a row is written at login and deleted
at logout. Rows are self-expiring lazily (see purge_expired), keyed to the
session TTL, so abandoned sessions don't accumulate tokens indefinitely.
"""
import asyncio
import base64
import hashlib
import os
import sqlite3
import time
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

from config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_tokens (
    sid           TEXT PRIMARY KEY,
    email         TEXT NOT NULL,
    access_token  TEXT NOT NULL,
    refresh_token TEXT,
    expires_at    INTEGER NOT NULL,
    session_exp   INTEGER NOT NULL,
    updated_at    TEXT NOT NULL
);
"""


def _db_path() -> str:
    """Store alongside the registry DB so it lands on the same data volume."""
    reg = settings.registry_db
    d = os.path.dirname(reg)
    return os.path.join(d, "graph_tokens.sqlite") if d else "graph_tokens.sqlite"


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.app_secret.encode()).digest())
    return Fernet(key)


def _encrypt(value: str | None) -> str | None:
    if not value:
        return None
    return _fernet().encrypt(value.encode()).decode()


def _decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


# ---- sync core (run in a thread from async callers) ----
def _save(sid, email, access_token, refresh_token, expires_at, session_exp) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO graph_tokens "
            "(sid, email, access_token, refresh_token, expires_at, session_exp, updated_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(sid) DO UPDATE SET "
            "email=excluded.email, access_token=excluded.access_token, "
            "refresh_token=excluded.refresh_token, expires_at=excluded.expires_at, "
            "session_exp=excluded.session_exp, updated_at=excluded.updated_at",
            (sid, email, _encrypt(access_token), _encrypt(refresh_token),
             int(expires_at), int(session_exp), _now()),
        )


def _load(sid):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM graph_tokens WHERE sid=?", (sid,)
        ).fetchone()
    if not row:
        return None
    return {
        "sid": row["sid"],
        "email": row["email"],
        "access_token": _decrypt(row["access_token"]),
        "refresh_token": _decrypt(row["refresh_token"]),
        "expires_at": row["expires_at"],
        "session_exp": row["session_exp"],
    }


def _delete(sid) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM graph_tokens WHERE sid=?", (sid,))


def _purge_expired() -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM graph_tokens WHERE session_exp < ?", (int(time.time()),))


# ---- async wrappers ----
async def init() -> None:
    await asyncio.to_thread(lambda: _connect().close())


async def save(sid, email, access_token, refresh_token, expires_at, session_exp) -> None:
    await asyncio.to_thread(
        _save, sid, email, access_token, refresh_token, expires_at, session_exp
    )


async def load(sid):
    return await asyncio.to_thread(_load, sid)


async def delete(sid) -> None:
    await asyncio.to_thread(_delete, sid)


async def purge_expired() -> None:
    await asyncio.to_thread(_purge_expired)
