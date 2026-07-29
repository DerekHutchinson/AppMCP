# AppMCP — publish self-contained HTML apps backed by live data

AppMCP lets an AI agent build a **self-contained HTML app** against live data and
**publish** it to a permanent, authenticated catalog at a stable URL. It is a
sibling of KPIMCP and reuses the same data-source layer, Azure AD auth, and
role model.

The agent never sees credentials. It discovers the data catalog, writes SQL, and
the published app calls back to **server-side, validated proxies** that hold the
secrets. Beyond SQL, apps can also read files from S3, pull U.S. Census
statistics, send email, call Microsoft Graph as the signed-in user, run LLM chat
completions, and analyze images — all through the same injected `AppData` client,
with the credentials kept on the server.

**Data sources:** Redshift / PostgreSQL, MySQL, MSSQL, and BigQuery (GoogleSQL).
**Extra integrations (all optional):** S3 object storage, the U.S. Census Bureau
Data API, SendGrid email, Microsoft Graph, LLM chat (OpenAI / Anthropic), and
Google Cloud Vision.

## How it works

1. **Discover** — `list_datasources`, `list_schemas`, `list_tables`,
   `list_columns` expose the full catalog the app is allowed to read. `list_s3_sources`
   / `list_s3_objects` do the same for bound S3 buckets.
2. **Design** — `run_query` runs read-only SELECTs and `census_query` previews
   Census data, so the agent can validate against the real path before building.
3. **Author** — the agent starts from the default dashboard theme (`get_app_theme`)
   and writes a complete HTML document. To read data at runtime, its JavaScript
   calls the injected client:

   ```js
   const { columns, rows } = await AppData.query(
     "SELECT rep, SUM(total) AS total FROM sales.invoice WHERE day > $1 GROUP BY rep",
     ["2025-01-01"]
   );
   ```

4. **Publish** — `create_app` (draft) → `publish_app` (live in the catalog).
   Authors publish directly; there is no separate approval gate. Apps are tagged
   with a `category` (`list_categories`) so they land in the right catalog section.
5. **View** — users browse the catalog at `/` and open apps at `/a/{slug}`.
   Viewing requires an Azure AD sign-in; any authenticated org user can open any
   published app (unless the author restricts it via `set_app_access`).

## What apps can do at runtime (`AppData`)

Every published app is served with an injected `AppData` client. Each method
posts to the app's own origin (allowed by `connect-src 'self'`) and is handled by
a server-side proxy that holds the credentials and enforces guardrails:

| Client API | Backing proxy | What it does |
| --- | --- | --- |
| `AppData.query(sql, params, {maxRows})` | `/a/{slug}/sql` | Read-only SELECT against the app's datasource. Auto-paginates (`QUERY_PAGE_SIZE`) up to `MAX_QUERY_ROWS`. |
| `AppData.queryPages(sql, params)` | `/a/{slug}/sql` | Async iterator yielding one page at a time for large result sets. |
| `AppData.s3.list(prefix, opts)` / `AppData.s3.get(key)` | `/a/{slug}/s3` | List / fetch objects from the bound S3 source, confined to its bucket + prefix. |
| `AppData.census(opts)` | `/a/{slug}/census` | Query the U.S. Census Bureau Data API (public, read-only statistics). |
| `AppData.sendEmail({to, subject, html, text})` | `/a/{slug}/email` | Send notification email via SendGrid; recipients restricted to the org domain. |
| `AppData.graph(path, opts)` | `/a/{slug}/graph` | Call Microsoft Graph as the **signed-in user** (`/me`-scoped, method+path allowlist). |
| `AppData.llm({system, prompt\|messages, model, images, files})` | `/a/{slug}/llm` | Run a chat completion via OpenAI or Anthropic (model allowlist; supports images + PDFs). |
| `AppData.vision({image, features})` | `/a/{slug}/vision` | Analyze an image via Google Cloud Vision (labels, OCR, faces, etc.). |

Each integration is only available when configured on the server; unconfigured
endpoints return `503`. See `docs/authoring-guide.md` for the full contract.

## MCP tools

- **Explore:** `list_datasources`, `list_schemas`, `list_tables`, `list_columns`,
  `run_query`, `list_s3_sources`, `list_s3_objects`, `census_query`.
- **Author/publish:** `get_authoring_guide`, `get_app_theme`, `list_categories`,
  `list_icons`, `check_app`, `create_app`, `update_app`, `preview_app`,
  `set_app_access`, `transfer_app`, `publish_app`, `unpublish_app`, `list_apps`,
  `get_app`, `get_app_url`.

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
  (BigQuery adds a per-query `BIGQUERY_MAX_BYTES_BILLED` cost cap.)
- **Every integration is a locked-down proxy.** The app never holds a secret:
  - **S3** — pinned to one bucket and optional key prefix; read-only (`list` + `get`);
    use a least-privilege key. Object size and list counts are capped.
  - **Census** — the API key lives only on the server; the data is public and read-only.
  - **Email (SendGrid)** — the From address is fixed server-side and recipients are
    restricted to `ALLOWED_EMAIL_DOMAIN` (anti-relay), with per-send and per-minute caps.
  - **Microsoft Graph** — forwarded with the **signed-in user's** delegated token
    (stored encrypted at rest, keyed to app_secret), pinned to `/me` and a
    method+path allowlist, with a response-size cap and rate limit.
  - **LLM (OpenAI / Anthropic)** — the provider keys live only on the server; the
    requested model must be on an allowlist, output tokens and input size are
    clamped (cost guard), and any image URLs are fetched SSRF-guarded before being
    inlined. Per-session rate limit and response-size cap apply.
  - **Vision** — the API key lives only on the server; inline image bytes only (no
    remote URL fetches), with batch, request-size, and response-size caps.
- **Sandboxed serving.** App HTML is served from an isolated origin with a strict
  CSP and a per-render nonce on inline scripts; `connect-src 'self'` means a
  compromised app can only talk to its own proxies, not exfiltrate. Attach
  event listeners in `<script>` blocks (inline `on*=` handlers are blocked by CSP).
- **Identity + audit.** Browser session via Azure AD; every proxy call (SQL, S3,
  Census, email, Graph) is logged against the signed-in user.

## Roles (`tool_access.json`)

- `admin` — all MCP tools; sees/manages all apps.
- `author` — explore data, create/publish/manage their own apps.
- `viewer` — no MCP tools; can browse and open published apps in the catalog.

## Run locally

```bash
cp .env.example .env                          # fill in DATASOURCES_JSON (read-only DSNs)
cp .env.local.example .env.local
cp tool_access.example.json tool_access.json  # map your users to roles (gitignored)
pip install -r requirements.txt
uvicorn server:app --reload --port 8000
```

With `LOCAL_MODE=true` and `AUTH_ENABLED=false`, a fixed local admin identity is
assumed, so the catalog/manage UI works without any Azure configuration.

Add your own logo at `static/logo.png` — the default theme and catalog header
reference it (a neutral placeholder ships in the repo).

## Configuration

All settings come from `.env` (see `.env.example` for the annotated list). Core
settings: `APP_SECRET`, `DATASOURCES_JSON`, `DEFAULT_DATASOURCE`, and the Azure
AD group (`AUTH_ENABLED`, `AZURE_*`, `ALLOWED_EMAIL_DOMAIN`).

Each extra integration is **off unless configured**, and its endpoint returns
`503` until then:

| Integration | Enable with | Notes |
| --- | --- | --- |
| BigQuery source | a `"kind":"bigquery"` entry in `DATASOURCES_JSON` | Auth'd by a base64 service-account key; `BIGQUERY_MAX_BYTES_BILLED` caps cost. |
| S3 objects | `S3_SOURCES_JSON` | One bucket + optional prefix per source; least-privilege key. |
| Census | `CENSUS_ENABLED=true` + `CENSUS_API_KEY` | Free key from the Census Bureau. |
| Email | `SENDGRID_API_KEY` + `EMAIL_FROM` | Verified SendGrid sender; internal recipients only. |
| Microsoft Graph | `GRAPH_ENABLED=true` + `GRAPH_SCOPES` | Requires admin consent on the Azure app registration. |
| LLM chat | `LLM_ENABLED=true` + `LLM_OPENAI_API_KEY` and/or `LLM_ANTHROPIC_API_KEY` | Model allowlists (`LLM_OPENAI_MODELS` / `LLM_ANTHROPIC_MODELS`); token/cost caps. |
| Vision | `VISION_ENABLED=true` + `VISION_API_KEY` | Google Cloud API key restricted to the Vision API. |

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

`./deploy.sh` builds the image and runs it on `127.0.0.1:8017` behind your
reverse proxy. Serve it from the isolated subdomain (`appmcp.example.com`) and
register `https://appmcp.example.com/auth/callback` as a redirect URI on the
Azure app.

## Layout

| Path | Purpose |
| --- | --- |
| `config.py` | Settings (datasources, auth, integrations, CSP, session). |
| `datasources.py` / `db.py` | Read-only data-source layer (Redshift/PG, MySQL, MSSQL, BigQuery). |
| `validation.py` / `appsql.py` | SQL guardrails + the shared validated runner. |
| `catalog.py` | Schema introspection per source (allowlist-aware). |
| `s3source.py` | Read-only S3 object source layer (bucket/prefix-pinned). |
| `censussource.py` | U.S. Census Bureau Data API client. |
| `emailer.py` | SendGrid email sender (domain-restricted). |
| `grapher.py` / `graph_tokens.py` | Microsoft Graph proxy + encrypted delegated-token store. |
| `llmsource.py` | LLM chat proxy (OpenAI + Anthropic; allowlist + cost caps; multimodal). |
| `visionsource.py` | Google Cloud Vision proxy (inline images only). |
| `icons.py` | Catalog icon set auto-discovered from `static/icons/`. |
| `registry.py` | SQLite store of apps (HTML + datasource + lifecycle). |
| `apphistory.py` | Optional git audit trail of app changes (best-effort). |
| `auth.py` | Azure AD OAuth proxy + bearer auth for `/mcp`. |
| `session_auth.py` | Browser session login + delegated-token capture. |
| `tool_access.py` | Role-based access (MCP tools + author/admin gates). |
| `web/` | Catalog, sandboxed app serving, and the SQL/S3/census/email/graph/llm/vision proxies. |
| `tools/` | MCP tools: `data_explore` + `apps_admin`. |
| `static/dashboard_blank.html` | Default dashboard theme returned by `get_app_theme`. |
| `server.py` | FastMCP + FastAPI wiring. |
