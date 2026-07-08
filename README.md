# AppMCP — publish self-contained HTML apps backed by live data

AppMCP lets an AI agent build a **self-contained HTML app** against live data and
**publish** it to a permanent, authenticated catalog at a stable URL. It is a
sibling of KPIMCP and reuses the same data-source layer, Azure AD auth, and
role model.

The agent never sees database credentials. It discovers the data catalog, writes
SQL, and the published app calls back to a **server-side, validated, read-only
SQL proxy** that holds the credentials.

## How it works

1. **Discover** — `list_datasources`, `list_schemas`, `list_tables`,
   `list_columns` expose the full catalog the app is allowed to read.
2. **Design** — `run_query` runs read-only SELECTs so the agent can validate its
   SQL and preview results before building anything.
3. **Author** — the agent writes a complete HTML document. To read data at
   runtime, its JavaScript calls the injected client:

   ```js
   const { columns, rows } = await AppData.query(
     "SELECT rep, SUM(total) AS total FROM sales.invoice WHERE day > $1 GROUP BY rep",
     ["2025-01-01"]
   );
   ```

4. **Publish** — `create_app` (draft) → `publish_app` (live in the catalog).
   Authors publish directly; there is no separate approval gate.
5. **View** — users browse the catalog at `/` and open apps at `/a/{slug}`.
   Viewing requires an Azure AD sign-in; any authenticated org user can open any
   published app.

## Security model

- **No credentials in the browser.** Apps call `/a/{slug}/sql`, which is pinned
  to the app's chosen datasource and runs on a read-only connection.
- **Every proxy request is validated** regardless of what SQL arrives: single
  statement, SELECT-only (no DDL/DML), schema-qualified tables in the allowlist,
  forced row cap, statement timeout.
- **The real boundary is the database account.** Point each datasource at a
  **least-privilege, read-only** user; `allowed_schemas` further constrains what
  any app can read. The proxy is, by design, a read-only SQL gateway reachable
  by authenticated employees, so what that account can read is what apps can read.
- **Sandboxed serving.** App HTML is served from an isolated origin with a strict
  CSP and a per-render nonce on inline scripts; `connect-src 'self'` means a
  compromised app can only talk to its own data proxy, not exfiltrate. Attach
  event listeners in `<script>` blocks (inline `on*=` handlers are blocked by CSP).
- **Identity + audit.** Browser session via Azure AD; every proxy query is logged
  against the signed-in user.

## Roles (`tool_access.json`)

- `admin` — all MCP tools; sees/manages all apps.
- `author` — explore data, create/publish/manage their own apps.
- `viewer` — no MCP tools; can browse and open published apps in the catalog.

## Run locally

```bash
cp .env.example .env                        # fill in DATASOURCES_JSON (read-only DSNs)
cp .env.local.example .env.local
cp tool_access.example.json tool_access.json  # map your users to roles (gitignored)
pip install -r requirements.txt
uvicorn server:app --reload --port 8000
```

With `LOCAL_MODE=true` and `AUTH_ENABLED=false`, a fixed local admin identity is
assumed, so the catalog/manage UI works without any Azure configuration.

## App history (git audit trail)

With `GIT_HISTORY_ENABLED=true`, every app lifecycle change
(create / update / publish / unpublish / delete) is mirrored into a dedicated
git repo, giving a reviewable history of what the AI agent has built over time.

Each change writes to the clone and commits + pushes it:

```
<GIT_HISTORY_SUBDIR>/<slug>/index.html   # the app HTML
<GIT_HISTORY_SUBDIR>/<slug>/app.json     # title/datasource/status/who/when
```

Commit message: `create app: <title> (<slug>) by <author>`. With
`GIT_HISTORY_PUSH=true` it pushes to `GIT_HISTORY_BRANCH` on `GIT_HISTORY_REMOTE`.

Setup (GitHub, token-in-URL):

1. Create a GitHub PAT with **Contents: read & write** on the history repo
   (`https://github.com/your-org/AppMCP-History`).
2. In `.env`, set `GIT_HISTORY_REPO_URL` (already filled in `.env.example`) and
   `GIT_HISTORY_TOKEN=<PAT>`.
3. Run `./deploy.sh`. On first deploy it clones the repo into `./data/app-history`
   (mounted into the container at `/app/data/app-history`) with the token baked
   into the remote URL, so the container can push.

AppMCP commits/pushes to `app-history`; the first push creates that branch.
**Git failures are logged and swallowed** — they can never block an app
create/publish. `git` is installed in the image, and `data/` is gitignored so
the history clone is never tracked by the parent repo.

## Deploy

`./deploy.sh` builds the image and runs it on `127.0.0.1:8016` behind your
reverse proxy. Serve it from the isolated subdomain (`appmcp.example.com`) and
register `https://appmcp.example.com/auth/callback` as a redirect URI on the
Azure app.

## Layout

| Path | Purpose |
| --- | --- |
| `config.py` | Settings (datasources, auth, CSP, session). |
| `datasources.py` / `db.py` | Read-only data-source layer (Redshift/PG, MySQL, MSSQL). |
| `validation.py` / `appsql.py` | SQL guardrails + the shared validated runner. |
| `catalog.py` | `information_schema` introspection (allowlist-aware). |
| `registry.py` | SQLite store of apps (HTML + datasource + lifecycle). |
| `apphistory.py` | Optional git audit trail of app changes (best-effort). |
| `auth.py` | Azure AD OAuth proxy + bearer auth for `/mcp`. |
| `session_auth.py` | Browser session login for the web UI. |
| `tool_access.py` | Role-based access (MCP tools + author/admin gates). |
| `web/` | Catalog, sandboxed app serving, SQL proxy, manage UI. |
| `tools/` | MCP tools: `data_explore` + `apps_admin`. |
| `server.py` | FastMCP + FastAPI wiring. |
