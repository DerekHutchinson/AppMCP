import json

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # HMAC secret for the browser session cookie (min ~32 chars).
    app_secret: str
    # Public origin where published apps live (the isolated subdomain).
    app_base_url: str = "https://appmcp.example.com"

    # --- local instance switch ---
    # Set LOCAL_MODE=true (typically in a gitignored .env.local) to run a local
    # instance: advertised URLs and OAuth metadata point at local_base_url.
    #
    # When LOCAL_MODE=true AND AUTH_ENABLED=false, the web UI is mounted and a
    # fixed local dev identity (local_dev_email, treated as admin) is assumed —
    # no Azure sign-in / redirect URI registration is needed. Hard-gated to
    # local + auth-off so it can never apply in production.
    local_mode: bool = False
    local_base_url: str = "http://localhost:8000"
    local_dev_email: str = "local-admin@localhost"

    # --- app registry ---
    # SQLite file holding published/draft app definitions (HTML + datasource).
    registry_db: str = "apps.sqlite"
    # Rows returned per page by the runtime SQL proxy / run_query (and the hard
    # max any single proxy request may fetch).
    query_page_size: int = 1000
    # Overall safety ceiling for AppData.query()'s transparent auto-pagination:
    # the client fetches pages until exhausted or this many rows, then reports
    # truncated=true. Apps needing more should use AppData.queryPages() and render
    # incrementally. Raise/lower to taste (protects the BROWSER from OOM).
    max_query_rows: int = 50000
    # Statement timeout (seconds) for proxy / design-time queries.
    query_timeout: int = 20

    # --- data sources (same shape as KPIMCP) ---
    # JSON object keyed by source name (or a JSON array of objects each with a
    # "name"). Each entry: {"kind": "redshift|postgres|mysql|mssql",
    # "dsn": "...", "ro_dsn": "..."?, "allowed_schemas": "a,b" | ["a","b"],
    # "description": "..."?}. ro_dsn defaults to dsn. allowed_schemas is the
    # real boundary for what any published app can ever read — point each
    # datasource at a LEAST-PRIVILEGE read-only DB account.
    datasources_json: str = ""
    default_datasource: str = "redshift"
    # Default per-query cost ceiling for BigQuery sources (bytes scanned/billed);
    # a query that would exceed it fails fast instead of running up a bill. Each
    # BigQuery datasource may override this with its own "max_bytes_billed".
    # Default 1 GiB. Set 0 to disable (not recommended).
    bigquery_max_bytes_billed: int = 1_073_741_824

    # --- MCP endpoint auth (Azure AD bearer) ---
    # When False (default) /mcp is open and all tools are visible (local/dev).
    # When True, /mcp requires an Azure AD bearer token and tools are filtered
    # per caller via tool_access.json.
    auth_enabled: bool = False
    public_base_url: str = ""
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""
    # Where Azure returns the browser after a federated sign-out (optional).
    azure_post_logout_redirect_url: str = ""
    allowed_email_domain: str = "@example.com"
    # Optional comma-separated hard allowlist; empty = any domain match.
    allowed_emails: str = ""
    # Role/user -> allowed MCP tools map (see tool_access.json).
    tool_access_path: str = "tool_access.json"

    # --- catalog organization ---
    # Canonical category list authors/agents choose from when creating an app, so
    # the catalog groups into a few stable sections instead of fragmenting into
    # near-duplicates ("Sales" vs "sales"). "Other" is always allowed as the
    # fallback. Comma-separated; edit to taste.
    app_categories: str = (
        "Sales,Inventory,Customers,Rewards,Ecommerce,Finance,"
        "Operations,Marketing,Reports,Tools,Demo"
    )

    # --- web session ---
    # Lifetime of the browser session cookie, seconds (default 8h).
    session_ttl_seconds: int = 28800

    # --- email (SendGrid) for the app-triggered /a/{slug}/email endpoint ---
    # Apps call AppData.sendEmail(...) which posts here; the server sends via
    # SendGrid. Recipients are restricted to allowed_email_domain (internal
    # only) to prevent apps becoming an open relay. The From address is fixed
    # server-side (never app-supplied). Empty api key/from disables the endpoint.
    sendgrid_api_key: str = ""
    email_from: str = ""
    email_from_name: str = "Example Apps"
    # Max recipients per send and a per-app per-minute cap (runaway-loop guard).
    email_max_recipients: int = 25
    email_rate_per_min: int = 30

    # --- Microsoft Graph proxy for the app-triggered /a/{slug}/graph endpoint ---
    # Apps call AppData.graph(path, opts) which posts here; the server forwards to
    # Microsoft Graph using the SIGNED-IN USER's delegated token (captured at
    # login, refreshed as needed, stored server-side). Calls are pinned to /me and
    # a method+path allowlist. Enabling requires the delegated Graph permissions in
    # GRAPH_SCOPES to be granted (admin consent) on the Azure app registration.
    graph_enabled: bool = False
    # Space-separated delegated Graph scopes requested at login (offline_access is
    # added automatically for refresh tokens). Keep to the least privilege needed.
    graph_scopes: str = (
        "User.Read Mail.Read Mail.Send Calendars.ReadWrite Files.Read Chat.Read"
    )
    # Per-session per-minute cap on Graph calls (runaway-loop guard).
    graph_rate_per_min: int = 60
    # Hard cap on a single Graph response body forwarded back to the app (bytes).
    graph_max_response_bytes: int = 2_000_000

    # --- U.S. Census Bureau Data API proxy for /a/{slug}/census ---
    # Apps call AppData.census(opts) which posts here; the server forwards to the
    # public Census Data API (https://api.census.gov/data) with the API key held
    # ONLY here (never in the app). Read-only public data — the same "connection
    # method" USCensusMCP uses (the Census Data API + a key). Empty key or
    # CENSUS_ENABLED=false disables the endpoint (returns 503).
    census_enabled: bool = False
    census_api_key: str = ""
    # Per-session per-minute cap on Census calls (runaway-loop guard).
    census_rate_per_min: int = 60
    # Hard cap on a single Census response body forwarded back to the app (bytes).
    census_max_response_bytes: int = 5_000_000
    # Per-request timeout talking to the Census API (seconds).
    census_timeout: int = 30

    # --- S3 object sources for the app-triggered /a/{slug}/s3 endpoint ---
    # Apps call AppData.s3.list()/get() which post here; the server fetches from
    # Amazon S3 (or an S3-compatible endpoint) using credentials stored ONLY here
    # (never in the app). Read-only: list + get objects. Each source is pinned to
    # one bucket and an optional key prefix that confines everything the app can
    # ever reach — point each at a LEAST-PRIVILEGE key (s3:GetObject/ListBucket on
    # just that bucket/prefix). JSON object keyed by source name (or a JSON array
    # of objects each with a "name"). Each entry: {"bucket": "...",
    # "region": "us-east-1", "access_key_id": "...", "secret_access_key": "...",
    # "prefix": "reports/"?, "endpoint_url": "https://..."?, "description": "..."?}.
    # Empty = the /s3 endpoint is disabled (returns 503).
    s3_sources_json: str = ""
    # Hard cap on a single object's bytes forwarded back to an app (5 MiB).
    s3_max_object_bytes: int = 5_242_880
    # Max keys returned by one AppData.s3.list() page.
    s3_max_list_keys: int = 1000
    # Per-session per-minute cap on S3 calls (runaway-loop guard).
    s3_rate_per_min: int = 120

    # --- git history (optional audit trail of apps as agents build them) ---
    # When enabled, every app create/update/publish/unpublish/delete writes the
    # app's HTML + metadata to a git working tree and commits it (optionally
    # pushing). Failures are logged and NEVER block app operations. Point
    # git_history_repo_path at an existing clone whose remote already carries the
    # push credentials (deploy token in the URL, or a mounted SSH key).
    git_history_enabled: bool = False
    git_history_repo_path: str = ""
    git_history_branch: str = "app-history"
    git_history_subdir: str = "apps"
    git_history_push: bool = False
    git_history_remote: str = "origin"
    git_history_author_name: str = "AppMCP Bot"
    git_history_author_email: str = "appmcp-bot@example.com"

    # --- content security policy for served apps ---
    # Extra script origins allowed in published apps (space-separated). Common
    # CDNs are allowed by default so artifacts that pull libraries still work;
    # connect-src stays 'self', so even a CDN-loaded lib cannot exfiltrate.
    csp_script_src_extra: str = (
        "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://unpkg.com"
    )

    class Config:
        # .env.local (gitignored) overrides .env. Later file wins.
        env_file = (".env", ".env.local")
        # Some .env keys are consumed by deploy.sh only (e.g. GIT_HISTORY_TOKEN,
        # GIT_HISTORY_REPO_URL) and are not Settings fields. Ignore unknown keys
        # so their presence in .env can never crash app startup.
        extra = "ignore"

    @property
    def local_dev_bypass(self) -> bool:
        """True when running locally with auth off: assume a local admin."""
        return self.local_mode and not self.auth_enabled

    @property
    def base_url(self) -> str:
        if self.local_mode:
            return self.local_base_url.rstrip("/")
        return self.app_base_url.rstrip("/")

    @property
    def public_base(self) -> str:
        if self.local_mode:
            return self.local_base_url.rstrip("/")
        return (self.public_base_url or self.app_base_url).rstrip("/")

    @property
    def allowed_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.allowed_emails.split(",") if e.strip()}

    @property
    def category_list(self) -> list[str]:
        """Canonical categories (order preserved) with 'Other' always last."""
        cats = [c.strip() for c in self.app_categories.split(",") if c.strip()]
        cats = [c for c in cats if c.lower() != "other"]
        cats.append("Other")
        return cats

    def match_category(self, value: str | None) -> str:
        """Map free input to a canonical category (case-insensitive); else 'Other'."""
        v = (value or "").strip()
        if not v:
            return "Other"
        for c in self.category_list:
            if c.lower() == v.lower():
                return c
        return "Other"

    @property
    def email_configured(self) -> bool:
        """True when the SendGrid email endpoint can operate."""
        return bool(self.sendgrid_api_key.strip() and self.email_from.strip())

    @property
    def graph_configured(self) -> bool:
        """True when the Microsoft Graph proxy can operate (needs Azure creds)."""
        return bool(
            self.graph_enabled
            and self.azure_tenant_id
            and self.azure_client_id
            and self.azure_client_secret
        )

    @property
    def graph_login_scope(self) -> str:
        """Full scope string requested at login when Graph is enabled."""
        parts = ["openid", "profile", "email"]
        if self.graph_configured:
            parts.append("offline_access")
            parts += [s for s in self.graph_scopes.split() if s]
        # De-dupe while preserving order.
        seen, out = set(), []
        for p in parts:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return " ".join(out)

    @property
    def graph_refresh_scope(self) -> str:
        """Scopes sent on a refresh_token grant (Graph delegated + offline)."""
        parts = ["offline_access"] + [s for s in self.graph_scopes.split() if s]
        seen, out = set(), []
        for p in parts:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return " ".join(out)

    @property
    def census_configured(self) -> bool:
        """True when the Census Data API proxy can operate (needs an API key)."""
        return bool(self.census_enabled and self.census_api_key.strip())

    @property
    def sources(self) -> dict[str, dict]:
        """All configured data sources, keyed by name (parsed from DATASOURCES_JSON)."""
        raw = self.datasources_json.strip()
        if not raw:
            raise ValueError("DATASOURCES_JSON must define at least one data source")

        data = json.loads(raw)
        out: dict[str, dict] = {}
        if isinstance(data, list):
            for entry in data:
                entry = dict(entry)
                name = entry.pop("name")
                out[name] = entry
        elif isinstance(data, dict):
            for name, cfg in data.items():
                out[name] = dict(cfg)
        else:
            raise ValueError("DATASOURCES_JSON must be a JSON object or array")

        if self.default_datasource not in out:
            raise ValueError(
                f"DEFAULT_DATASOURCE '{self.default_datasource}' is not declared in "
                f"DATASOURCES_JSON (configured: {sorted(out)})"
            )
        return out

    @property
    def s3_sources(self) -> dict[str, dict]:
        """All configured S3 object sources, keyed by name (from S3_SOURCES_JSON).

        Unlike SQL sources this is optional: an empty/unset value simply means no
        S3 sources are configured and the /a/{slug}/s3 endpoint is disabled.
        """
        raw = self.s3_sources_json.strip()
        if not raw:
            return {}

        data = json.loads(raw)
        out: dict[str, dict] = {}
        if isinstance(data, list):
            for entry in data:
                entry = dict(entry)
                name = entry.pop("name")
                out[name] = entry
        elif isinstance(data, dict):
            for name, cfg in data.items():
                out[name] = dict(cfg)
        else:
            raise ValueError("S3_SOURCES_JSON must be a JSON object or array")
        return out

    @property
    def s3_configured(self) -> bool:
        """True when at least one S3 source is declared (endpoint can operate)."""
        return bool(self.s3_sources)


settings = Settings()
