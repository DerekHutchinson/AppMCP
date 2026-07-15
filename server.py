"""FastMCP entry point for AppMCP.

Builds the MCP server (data-exploration + app-publishing tools) and mounts the
web app on the same FastAPI application:

  * MCP streamable-HTTP endpoint     ->  /mcp
  * App catalog (auth-gated)         ->  /
  * Published app (sandboxed)        ->  /a/{slug}
  * App data proxy (validated SQL)   ->  /a/{slug}/sql
  * App email (SendGrid)             ->  /a/{slug}/email
  * App Microsoft Graph (delegated)  ->  /a/{slug}/graph
  * App U.S. Census Data API         ->  /a/{slug}/census
  * App S3 objects (list/get)        ->  /a/{slug}/s3
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
import graph_tokens
import registry
import s3source
import session_auth
import tool_access
from config import settings
from tools import apps_admin, data_explore
from web import census, email, graph, home, manage, proxy, s3, serve

# ---- MCP server + tool registration ----
mcp = FastMCP("AppMCP App Publisher")
data_explore.register(mcp)
apps_admin.register(mcp)

# Per-user tool gating (no-op while auth_enabled is False).
mcp.add_middleware(tool_access.ToolAccessMiddleware())

mcp_app = mcp.http_app(path="/mcp")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    await registry.init_registry()
    if settings.s3_configured:
        s3source.init_sources()
    if settings.graph_configured:
        await graph_tokens.init()
        await graph_tokens.purge_expired()
    async with mcp_app.lifespan(app):
        yield
    await db.close_db()


app = FastAPI(title="AppMCP App Publisher", lifespan=lifespan)

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
    email.register(web_router)
    graph.register(web_router)
    census.register(web_router)
    s3.register(web_router)
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
