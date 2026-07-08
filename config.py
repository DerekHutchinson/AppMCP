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
    # Hard cap on rows returned by the runtime SQL proxy / run_query.
    max_query_rows: int = 5000
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

    # --- web session ---
    # Lifetime of the browser session cookie, seconds (default 8h).
    session_ttl_seconds: int = 28800

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


settings = Settings()
