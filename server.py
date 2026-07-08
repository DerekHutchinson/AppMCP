"""FastMCP entry point for AppMCP.

Builds the MCP server (data-exploration + app-publishing tools) and mounts the
web app on the same FastAPI application:

  * MCP streamable-HTTP endpoint     ->  /mcp
  * App catalog (auth-gated)         ->  /
  * Published app (sandboxed)        ->  /a/{slug}
  * App data proxy (validated SQL)   ->  /a/{slug}/sql
  * Author/admin manage UI           ->  /manage
  * Browser session login            ->  /login, /auth/callback, /logout
  * Shared static assets             ->  /static/*

Agents discover data sources/schema, validate SQL with run_query, author a
self-contained HTML app (whose JS calls AppData.query), then create + publish it
to the permanent, Azure-AD-gated catalog.

Run:  uvicorn server:app --host 0.0.0.0 --port 8000
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastmcp import FastMCP

import auth
import db
import registry
import session_auth
import tool_access
from config import settings
from tools import apps_admin, data_explore
from web import home, manage, proxy, serve

# ---- MCP server + tool registration ----
mcp = FastMCP("Gabriel App Publisher")
data_explore.register(mcp)
apps_admin.register(mcp)

# Per-user tool gating (no-op while auth_enabled is False).
mcp.add_middleware(tool_access.ToolAccessMiddleware())

mcp_app = mcp.http_app(path="/mcp")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    await registry.init_registry()
    async with mcp_app.lifespan(app):
        yield
    await db.close_db()


app = FastAPI(title="Gabriel App Publisher", lifespan=lifespan)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok"})


# The web UI needs an authenticated identity. It mounts when real auth is on, OR
# in local dev (LOCAL_MODE + auth off), where session_auth assumes a local admin
# identity so no Azure sign-in / redirect URI is needed.
if settings.auth_enabled or settings.local_dev_bypass:
    session_auth.register_routes(app)
    web_router = APIRouter(tags=["web"])
    home.register(web_router)
    serve.register(web_router)
    proxy.register(web_router)
    manage.register(web_router)
    app.include_router(web_router)

# Azure bearer enforcement + OAuth proxy attach only when auth is enabled.
if settings.auth_enabled:
    auth.register_oauth_routes(app)
    app.add_middleware(auth.BearerTokenMiddleware)

# Mount the MCP app last as the catch-all (serves /mcp).
app.mount("/", mcp_app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
