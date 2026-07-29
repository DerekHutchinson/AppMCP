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

    # Icon shown on each catalog card. Authors/agents pick one of the SVG names
    # discovered from static/icons/ (see icons.py). When an app has no icon set,
    # the catalog falls back to the icon mapped to its category, and finally to
    # `app_default_icon`. Both must name a file that exists in static/icons/.
    app_default_icon: str = "grid"

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

    # --- Google Cloud Vision API proxy for /a/{slug}/vision ---
    # Apps call AppData.vision(opts) which posts here; the server forwards the
    # image bytes to https://vision.googleapis.com/v1/images:annotate with the API
    # key held ONLY here (never in the app). Apps send inline image content
    # (base64) and get annotations back (labels, text/OCR, objects, faces, etc.).
    # Empty key or VISION_ENABLED=false disables the endpoint (returns 503).
    vision_enabled: bool = False
    vision_api_key: str = ""
    # Per-session per-minute cap on Vision calls (runaway-loop guard).
    vision_rate_per_min: int = 60
    # Max annotate requests (images) in a single batch call.
    vision_max_requests: int = 16
    # Hard cap on the request body the app may post (bytes; base64 inflates ~33%).
    vision_max_request_bytes: int = 12_000_000
    # Hard cap on a single Vision response body forwarded back to the app (bytes).
    vision_max_response_bytes: int = 5_000_000
    # Per-request timeout talking to the Vision API (seconds).
    vision_timeout: int = 30

    # --- LLM chat proxy for /a/{slug}/llm (OpenAI + Anthropic) ---
    # Apps call AppData.llm(opts) which posts here; the server forwards the chat
    # completion to the provider with the API key held ONLY here (never in the
    # app). Apps send {system, messages|prompt, model?, temperature?, maxTokens?,
    # json?, images?, files?} and get {text, model, usage} back. The requested
    # model must be on one of the allowlists below; the provider is inferred from
    # which list it is in. Images (any vision-capable model) and PDF files are
    # forwarded as inline base64. Set LLM_ENABLED=false or leave both keys empty
    # to disable (returns 503).
    llm_enabled: bool = False
    llm_openai_api_key: str = ""
    llm_anthropic_api_key: str = ""
    llm_openai_base_url: str = "https://api.openai.com/v1"
    llm_anthropic_base_url: str = "https://api.anthropic.com/v1"
    llm_anthropic_version: str = "2023-06-01"
    # Comma-separated allowlist of models an app may request, per provider. Only
    # models listed here are accepted; anything else is rejected with a clear error.
    llm_openai_models: str = "gpt-4o-mini,gpt-4o,gpt-5.2"
    llm_anthropic_models: str = "claude-3-5-haiku-latest,claude-3-5-sonnet-latest,claude-sonnet-5"
    # Default model used when the app doesn't specify one (must be on an allowlist).
    llm_default_model: str = "gpt-4o-mini"
    # Ceiling on output tokens per call (cost guard); an app's maxTokens is clamped
    # to this, and it's the default when the app doesn't ask for one. Kept generous
    # because reasoning models (claude-sonnet-5, gpt-5.x) spend output tokens on
    # internal reasoning before the visible answer; too low a cap yields empty text.
    llm_max_output_tokens: int = 32768
    # Per-model output-token caps (comma-separated model:tokens). Providers 400 if
    # max_tokens exceeds a model's real limit, so the effective cap for a call is
    # min(requested-or-default, this model's cap here, llm_max_output_tokens). Models
    # not listed fall back to llm_max_output_tokens. Keep the smaller/older models
    # here so the global ceiling can stay high for the capable ones.
    llm_model_max_output: str = (
        "gpt-4o-mini:16384,gpt-4o:16384,"
        "claude-3-5-haiku-latest:8192,claude-3-5-sonnet-latest:8192"
    )
    # Reasoning models spend output tokens on internal reasoning before the answer,
    # so a small maxTokens starves the visible text. OpenAI reasoning models
    # (gpt-5*, o1/o3/o4*) are detected automatically; list any others here (e.g.
    # Anthropic reasoning models). Comma-separated exact model names.
    llm_reasoning_models: str = "claude-sonnet-5"
    # Minimum output-token budget forced for reasoning models (clamped to the
    # model's own ceiling), applied even when an app requests fewer.
    llm_reasoning_min_output_tokens: int = 8192
    # Hard cap on total input characters (system + every message) accepted per call.
    llm_max_input_chars: int = 100_000
    # Per-session per-minute cap on LLM calls (runaway-loop guard).
    llm_rate_per_min: int = 30
    # Hard cap on the provider response body forwarded back to the app (bytes).
    llm_max_response_bytes: int = 2_000_000
    # Max attachments (images + files) across a single call.
    llm_max_attachments: int = 8
    # Hard cap on the raw request body the app may post (bytes; base64 inflates
    # ~33%). Guards multimodal payloads before they're buffered/parsed.
    llm_max_request_bytes: int = 20_000_000
    # Allow apps to pass image URLs (images only); the server fetches them and
    # forwards inline base64. SSRF-guarded (public hosts only, redirects revalidated,
    # content-type + size checked). Set false to require inline base64 only.
    llm_allow_image_urls: bool = True
    # Per-fetch timeout when downloading an image URL (seconds).
    llm_image_fetch_timeout: int = 15
    # Hard cap on a single fetched image (bytes) before it's base64-encoded.
    llm_max_image_bytes: int = 8_000_000
    # Per-request timeout talking to the provider (seconds).
    llm_timeout: int = 60

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
    def category_icon_defaults(self) -> dict[str, str]:
        """Best-effort category -> default icon name (used when an app has none).

        Only takes effect if the named icon exists in static/icons/; otherwise
        icons.resolve() falls back to `app_default_icon`.
        """
        return {
            "Sales": "sales",
            "Inventory": "inventory",
            "Customers": "customers",
            "Rewards": "rewards",
            "Ecommerce": "ecommerce",
            "Finance": "finance",
            "Operations": "operations",
            "Marketing": "marketing",
            "Reports": "report",
            "Tools": "tools",
            "Demo": "star",
            "Other": "grid",
        }

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
    def vision_configured(self) -> bool:
        """True when the Google Vision proxy can operate (needs an API key)."""
        return bool(self.vision_enabled and self.vision_api_key.strip())

    @property
    def llm_openai_model_list(self) -> list[str]:
        """Allowlisted OpenAI model names (empty if none configured)."""
        return [m.strip() for m in self.llm_openai_models.split(",") if m.strip()]

    @property
    def llm_anthropic_model_list(self) -> list[str]:
        """Allowlisted Anthropic model names (empty if none configured)."""
        return [m.strip() for m in self.llm_anthropic_models.split(",") if m.strip()]

    @property
    def llm_models(self) -> list[str]:
        """Every allowlisted model (OpenAI first, then Anthropic), de-duped."""
        seen: set[str] = set()
        out: list[str] = []
        for m in self.llm_openai_model_list + self.llm_anthropic_model_list:
            if m not in seen:
                seen.add(m)
                out.append(m)
        return out

    def llm_provider_for(self, model: str) -> str | None:
        """Which provider serves `model`, or None if it isn't allowlisted."""
        if model in self.llm_openai_model_list:
            return "openai"
        if model in self.llm_anthropic_model_list:
            return "anthropic"
        return None

    @property
    def llm_model_max_output_map(self) -> dict[str, int]:
        """Parse LLM_MODEL_MAX_OUTPUT ('model:tokens,...') into {model: tokens}."""
        out: dict[str, int] = {}
        for pair in self.llm_model_max_output.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            name, _, tok = pair.rpartition(":")
            name = name.strip()
            try:
                n = int(tok.strip())
            except ValueError:
                continue
            if name and n > 0:
                out[name] = n
        return out

    def llm_output_ceiling_for(self, model: str) -> int:
        """Effective output-token ceiling for `model`.

        The global cap, further lowered to the model's own limit when one is known,
        so we never send a max_tokens a provider will reject.
        """
        ceiling = self.llm_max_output_tokens
        per_model = self.llm_model_max_output_map.get(model)
        if per_model and per_model > 0:
            return min(ceiling, per_model) if ceiling > 0 else per_model
        return ceiling

    @property
    def llm_reasoning_model_list(self) -> list[str]:
        """Explicitly-configured reasoning model names (beyond the auto-detected ones)."""
        return [m.strip() for m in self.llm_reasoning_models.split(",") if m.strip()]

    def llm_is_reasoning(self, model: str) -> bool:
        """True for models that reason before answering (need a min output budget)."""
        m = (model or "").lower()
        if m.startswith(("gpt-5", "o1", "o3", "o4")):
            return True
        return model in self.llm_reasoning_model_list

    @property
    def llm_configured(self) -> bool:
        """True when the LLM proxy can operate (enabled + at least one usable provider)."""
        if not self.llm_enabled:
            return False
        has_openai = bool(self.llm_openai_api_key.strip() and self.llm_openai_model_list)
        has_anthropic = bool(
            self.llm_anthropic_api_key.strip() and self.llm_anthropic_model_list
        )
        return has_openai or has_anthropic

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
