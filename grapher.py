"""Server-side Microsoft Graph calls for the app /a/{slug}/graph endpoint.

Apps never hold a token; they post {method, path, query, body} to
/a/{slug}/graph and this module forwards the call to Microsoft Graph using the
SIGNED-IN USER's delegated token (captured at login by session_auth, stored
encrypted in graph_tokens, refreshed here when stale).

Guardrails (defense-in-depth alongside the endpoint):

  * every call is pinned to `/me` and matched against a method+path allowlist,
    so an app can only ever touch the current user's own mailbox/calendar/etc.
  * writes are limited to the specific operations the granted scopes permit
    (send mail, create/update/delete the user's own calendar events)
  * a per-session per-minute cap guards against runaway agent loops
  * the forwarded response body is size-capped

Uses httpx directly against the Graph v1.0 API (no extra SDK dep).
"""
import asyncio
import logging
import re
import time
from collections import defaultdict, deque

import httpx

import graph_tokens
from config import settings

logger = logging.getLogger("appmcp.grapher")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Graph/Exchange occasionally returns these for transient reasons (including the
# infamous "Cannot query rows in a table" ErrorInternalServerTransientError on
# otherwise-valid mailbox queries). Microsoft's guidance is to retry with a short
# backoff, so we do a few attempts before surfacing the error to the app.
_RETRY_STATUSES = {429, 503, 504}
_MAX_ATTEMPTS = 3

# Per-session call timestamps for the sliding-window rate limit (per process).
_CALL_LOG: dict[str, deque] = defaultdict(deque)

# (METHOD, compiled path regex) allowlist. Paths are always /me-scoped so an app
# can only reach the current user's data. Reads cover mail/calendar/profile;
# writes cover exactly what Mail.Send + Calendars.ReadWrite allow.
_ALLOW = [
    ("GET", r"/me/?"),
    ("GET", r"/me/messages(/[^/]+)?"),
    ("GET", r"/me/mailFolders(/.*)?"),
    ("GET", r"/me/events(/[^/]+)?"),
    ("GET", r"/me/calendar(/.*)?"),
    ("GET", r"/me/calendars(/.*)?"),
    ("GET", r"/me/calendarView"),
    ("GET", r"/me/contacts(/[^/]+)?"),
    ("GET", r"/me/people"),
    ("GET", r"/me/manager"),
    ("GET", r"/me/directReports"),
    ("GET", r"/me/drive(/.*)?"),
    ("GET", r"/me/drives(/.*)?"),
    ("GET", r"/me/chats(/.*)?"),
    ("POST", r"/me/sendMail"),
    ("POST", r"/me/events"),
    ("PATCH", r"/me/events/[^/]+"),
    ("DELETE", r"/me/events/[^/]+"),
]
_ALLOW_COMPILED = [(m, re.compile(rf"^{pat}$", re.IGNORECASE)) for m, pat in _ALLOW]

_ALLOWED_METHODS = {"GET", "POST", "PATCH", "DELETE"}


class GraphError(Exception):
    """Raised when a Graph request is invalid, unauthorized, or fails."""


def _normalize_path(path: str) -> str:
    """Validate the caller-supplied path and return it without a query string."""
    if not isinstance(path, str) or not path.strip():
        raise GraphError("A Graph 'path' is required, e.g. '/me/messages'.")
    p = path.strip()
    if "://" in p or p.startswith("//"):
        raise GraphError("Absolute URLs are not allowed; use a '/me/...' path.")
    if not p.startswith("/"):
        p = "/" + p
    # Drop any embedded query string; query params come via the 'query' field.
    p = p.split("?", 1)[0]
    if ".." in p:
        raise GraphError("Invalid path.")
    return p.rstrip("/") or "/"


def _check_allowed(method: str, path: str) -> None:
    for m, rx in _ALLOW_COMPILED:
        if m == method and rx.match(path):
            return
    raise GraphError(
        f"{method} {path} is not permitted. Allowed: read /me mail, calendar, "
        f"profile; send mail; create/update/delete your own calendar events."
    )


def _check_rate(sid: str) -> None:
    limit = settings.graph_rate_per_min
    if limit <= 0:
        return
    now = time.monotonic()
    log = _CALL_LOG[sid]
    while log and now - log[0] > 60:
        log.popleft()
    if len(log) >= limit:
        raise GraphError("Graph rate limit exceeded for this session; try again shortly.")
    log.append(now)


def _retry_delay(resp, attempt: int) -> float:
    """Backoff before retrying a transient Graph response.

    Honors a small Retry-After if present (throttling), else exponential backoff.
    """
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            return min(float(retry_after), 5.0)
        except ValueError:
            pass
    return min(0.5 * (2 ** (attempt - 1)), 4.0)


def _token_endpoint() -> str:
    return (f"https://login.microsoftonline.com/"
            f"{settings.azure_tenant_id}/oauth2/v2.0/token")


async def _refresh(rec: dict) -> dict:
    """Exchange the stored refresh token for a fresh access token; persist it."""
    refresh_token = rec.get("refresh_token")
    if not refresh_token:
        raise GraphError("Your Graph session has expired; sign out and sign in again.")
    data = {
        "client_id": settings.azure_client_id,
        "client_secret": settings.azure_client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": settings.graph_refresh_scope,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(_token_endpoint(), data=data)
    except httpx.HTTPError as exc:
        raise GraphError(f"Could not reach the identity provider: {exc}") from exc
    if resp.status_code != 200:
        logger.info("[grapher] refresh rejected %s: %s", resp.status_code, resp.text[:200])
        raise GraphError("Your Graph session has expired; sign out and sign in again.")

    tokens = resp.json()
    access = tokens.get("access_token")
    if not access:
        raise GraphError("Your Graph session has expired; sign out and sign in again.")
    # Azure may or may not rotate the refresh token; keep the newest one we have.
    new_refresh = tokens.get("refresh_token") or refresh_token
    expires_at = int(time.time()) + int(tokens.get("expires_in", 3600))
    await graph_tokens.save(
        rec["sid"], rec["email"], access, new_refresh, expires_at, rec["session_exp"],
    )
    rec = dict(rec)
    rec["access_token"] = access
    rec["refresh_token"] = new_refresh
    rec["expires_at"] = expires_at
    return rec


async def _valid_access_token(sid: str) -> str:
    rec = await graph_tokens.load(sid)
    if not rec or not rec.get("access_token"):
        raise GraphError(
            "No Graph session found; sign out and sign in again to grant access."
        )
    # Refresh a minute early to avoid using an about-to-expire token.
    if rec["expires_at"] - int(time.time()) <= 60:
        rec = await _refresh(rec)
    return rec["access_token"]


async def graph_request(sid: str, method, path, query=None, body=None) -> dict:
    """Validate and forward a Graph call as the signed-in user.

    Returns the parsed Graph JSON response body (dict). Raises GraphError on any
    validation problem or a non-2xx Graph response.
    """
    if not settings.graph_configured:
        raise GraphError("Microsoft Graph is not configured on this server.")
    if not sid:
        raise GraphError("No Graph session; sign out and sign in again to grant access.")

    method = (method or "GET").upper()
    if method not in _ALLOWED_METHODS:
        raise GraphError(f"Method {method} is not allowed.")
    path = _normalize_path(path)
    _check_allowed(method, path)

    if query is not None and not isinstance(query, dict):
        raise GraphError("'query' must be an object of query parameters.")
    if body is not None and not isinstance(body, dict):
        raise GraphError("'body' must be a JSON object.")

    _check_rate(sid)
    token = await _valid_access_token(sid)

    url = GRAPH_BASE + path
    headers = {"Authorization": f"Bearer {token}"}
    resp = None
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = await client.request(
                    method, url, headers=headers, params=query or None,
                    json=body if body is not None else None,
                )
            except httpx.HTTPError as exc:
                raise GraphError(f"Could not reach Microsoft Graph: {exc}") from exc
            if resp.status_code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
                delay = _retry_delay(resp, attempt)
                logger.info(
                    "[grapher] transient %s on %s (attempt %d/%d); retrying in %.1fs",
                    resp.status_code, path, attempt, _MAX_ATTEMPTS, delay,
                )
                await asyncio.sleep(delay)
                continue
            break

    cap = settings.graph_max_response_bytes
    if cap > 0 and len(resp.content) > cap:
        raise GraphError(
            f"Graph response too large ({len(resp.content)} bytes); "
            f"narrow the query (use $select/$top)."
        )

    if resp.status_code >= 400:
        detail = resp.text[:300]
        raise GraphError(f"Graph rejected the request ({resp.status_code}): {detail}")

    if resp.status_code == 204 or not resp.content:
        return {"ok": True, "status": resp.status_code}
    try:
        return resp.json()
    except ValueError:
        raise GraphError("Graph returned a non-JSON response.")
