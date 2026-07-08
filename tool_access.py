"""Per-user access control (role-based, fail-closed).

Maps an authenticated email to a role, and the role to a set of allowed MCP
tool names (see tool_access.json). A FastMCP middleware filters `tools/list`
and blocks `tools/call` for disallowed tools.

The same roles also gate the web authoring/manage UI:
  * is_admin(email)  -> role "admin" ("*")
  * can_author(email) -> "admin" or "author"

Policy:
  * Role "*" (or a role whose tool list contains "*") means "all tools".
  * Unknown caller -> default_role; if missing (e.g. "deny"), no access.
  * When auth_enabled is False, MCP tool enforcement is bypassed (all visible),
    and local_dev_bypass treats the local dev identity as admin.
"""
import json
import logging
from pathlib import Path

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

from auth import CURRENT_USER_EMAIL
from config import settings

logger = logging.getLogger("appmcp.tool_access")

ALL = object()

_ACCESS: dict | None = None


def _load() -> dict:
    path = Path(settings.tool_access_path)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    roles = {
        role: {str(t) for t in tools}
        for role, tools in data.get("roles", {}).items()
    }
    users = {
        str(email).strip().lower(): role
        for email, role in data.get("users", {}).items()
    }
    return {
        "roles": roles,
        "users": users,
        "default_role": data.get("default_role", "deny"),
    }


def _access() -> dict:
    global _ACCESS
    if _ACCESS is None:
        _ACCESS = _load()
    return _ACCESS


def role_for(email: str | None) -> str:
    access = _access()
    return access["users"].get((email or "").strip().lower(), access["default_role"])


def allowed_tools_for(email: str | None) -> set[str]:
    access = _access()
    return access["roles"].get(role_for(email), set())


def is_admin(email: str | None) -> bool:
    if settings.local_dev_bypass:
        return True
    return "*" in allowed_tools_for(email)


def can_author(email: str | None) -> bool:
    """True for admins and authors (anyone whose role can create/publish apps)."""
    if settings.local_dev_bypass:
        return True
    return role_for(email) in ("admin", "author") or is_admin(email)


def viewer_allowed(app: dict, email: str | None) -> bool:
    """Whether `email` may view a published app given its per-app access list.

    An empty access_list means the app is public (any authenticated org user).
    A non-empty list restricts viewing to those emails, but admins and the app's
    creator always retain access.
    """
    allow = app.get("access_list") or []
    if not allow:
        return True
    e = (email or "").strip().lower()
    if e in {str(a).strip().lower() for a in allow}:
        return True
    if is_admin(email):
        return True
    return e == (app.get("created_by") or "").strip().lower()


def current_allowed():
    if not settings.auth_enabled:
        return ALL
    email = CURRENT_USER_EMAIL.get()
    if not email:
        return set()  # fail closed
    tools = allowed_tools_for(email)
    return ALL if "*" in tools else tools


class ToolAccessMiddleware(Middleware):
    """FastMCP middleware: filter the tool list and gate tool calls per caller."""

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        allowed = current_allowed()
        if allowed is ALL:
            return tools
        return [t for t in tools if getattr(t, "name", None) in allowed]

    async def on_call_tool(self, context, call_next):
        allowed = current_allowed()
        name = getattr(context.message, "name", None)
        if allowed is not ALL and name not in allowed:
            email = CURRENT_USER_EMAIL.get() or "<anonymous>"
            logger.warning("[tool-access] %s denied tool '%s'", email, name)
            raise ToolError(f"You are not authorized to use the '{name}' tool.")
        return await call_next(context)


if settings.auth_enabled:
    _access()
