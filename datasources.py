"""Pluggable data-source layer (adapted from KPIMCP).

Each engine has a DataSource implementation that knows how to run queries, page
result rows (LIMIT/OFFSET wrapped so any author SELECT paginates without being
written for it), and which sqlglot dialect to validate against.

  * Postgres / Redshift -> asyncpg  ($n placeholders, native async)
  * MySQL / MariaDB      -> aiomysql (%s placeholders, native async)
  * MSSQL / SQL Server   -> pymssql  (sync; wrapped in asyncio.to_thread)

Published apps and the design-time run_query use the read-only path (fetch_ro),
authored in canonical $1..$n and translated per driver. A module-level registry
is built from settings.sources at startup.
"""
import asyncio
import re
from urllib.parse import unquote, urlparse

from config import settings

_PARAM_RE = re.compile(r"\$(\d+)")


def translate_params(sql: str, args, style: str):
    """Translate canonical $n placeholders to the driver's placeholder style.

    - "numeric"  -> leave $n as-is (asyncpg)
    - "pyformat" -> %s, literal % escaped to %% (aiomysql / pymssql)
    - "qmark"    -> ? (BigQuery positional parameters)
    Reorders args to match placeholder order for the positional styles.
    """
    args = list(args)
    if style == "numeric":
        return sql, args

    placeholder = "?" if style == "qmark" else "%s"
    src = sql if style == "qmark" else sql.replace("%", "%%")
    new_args = []

    def _repl(m):
        new_args.append(args[int(m.group(1)) - 1])
        return placeholder

    return _PARAM_RE.sub(_repl, src), new_args


def _parse_dsn(dsn: str) -> dict:
    u = urlparse(dsn)
    return {
        "host": u.hostname,
        "port": u.port,
        "user": unquote(u.username) if u.username else None,
        "password": unquote(u.password) if u.password else None,
        "db": (u.path or "").lstrip("/") or None,
    }


class DataSource:
    """Common async interface implemented by every engine."""

    dialect = "redshift"
    param_style = "numeric"

    def __init__(self, name, dsn, ro_dsn=None, allowed_schemas=None,
                 dialect=None, description=""):
        self.name = name
        self.dsn = dsn
        self.ro_dsn = ro_dsn or dsn
        self.allowed_schemas = set(allowed_schemas or [])
        self.description = description or ""
        if dialect:
            self.dialect = dialect

    async def connect(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        pass

    async def fetch_ro(self, query: str, *args, limit: int | None = None,
                       offset: int = 0) -> list[dict]:
        raise NotImplementedError

    def _page_sql(self, query: str, limit: int, offset: int) -> str:
        """Wrap an arbitrary SELECT so it returns one page (LIMIT/OFFSET).

        limit/offset are always server-controlled ints, so inlining them is safe
        and keeps the author's own $n params untouched.
        """
        inner = query.rstrip().rstrip(";")
        return f"SELECT * FROM (\n{inner}\n) _p LIMIT {int(limit)} OFFSET {int(offset)}"

    # ---- schema introspection (trusted, server-issued; bypasses guardrails) ----
    # Defaults query information_schema over the read-only connection. Engines
    # without a standard information_schema (BigQuery) override these. A large
    # explicit limit avoids the default page cap truncating the catalog.
    _INTROSPECT_LIMIT = 100000

    async def get_schemas(self) -> list[str]:
        rows = await self.fetch_ro(
            "SELECT schema_name AS schema_name FROM information_schema.schemata",
            limit=self._INTROSPECT_LIMIT,
        )
        return sorted({str(r["schema_name"]) for r in rows})

    async def get_tables(self, schema: str | None = None) -> list[dict]:
        if schema:
            rows = await self.fetch_ro(
                "SELECT table_schema AS table_schema, table_name AS table_name, "
                "table_type AS table_type FROM information_schema.tables "
                "WHERE table_schema = $1",
                schema, limit=self._INTROSPECT_LIMIT,
            )
        else:
            rows = await self.fetch_ro(
                "SELECT table_schema AS table_schema, table_name AS table_name, "
                "table_type AS table_type FROM information_schema.tables",
                limit=self._INTROSPECT_LIMIT,
            )
        return [
            {
                "schema": str(r["table_schema"]),
                "name": str(r["table_name"]),
                "type": str(r.get("table_type") or ""),
            }
            for r in rows
        ]

    async def get_columns(self, schema: str, table: str) -> list[dict]:
        rows = await self.fetch_ro(
            "SELECT column_name AS column_name, data_type AS data_type, "
            "is_nullable AS is_nullable, ordinal_position AS ordinal_position "
            "FROM information_schema.columns WHERE table_schema = $1 AND table_name = $2",
            schema, table, limit=self._INTROSPECT_LIMIT,
        )
        rows.sort(key=lambda r: int(r.get("ordinal_position") or 0))
        return [
            {
                "name": str(r["column_name"]),
                "type": str(r.get("data_type") or ""),
                "nullable": str(r.get("is_nullable") or "").upper() == "YES",
            }
            for r in rows
        ]


class PostgresDataSource(DataSource):
    """Redshift and PostgreSQL via asyncpg."""

    dialect = "redshift"
    param_style = "numeric"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._ro_pool = None

    async def connect(self) -> None:
        import asyncpg

        self._ro_pool = await asyncpg.create_pool(
            dsn=self.ro_dsn, min_size=1, max_size=5,
            command_timeout=settings.query_timeout,
        )

    async def close(self) -> None:
        if self._ro_pool:
            await self._ro_pool.close()

    async def fetch_ro(self, query: str, *args, limit: int | None = None,
                       offset: int = 0) -> list[dict]:
        size = int(limit or settings.query_page_size)
        async with self._ro_pool.acquire() as conn:
            rows = await conn.fetch(self._page_sql(query, size, offset), *args)
            return [dict(r) for r in rows]


class MySQLDataSource(DataSource):
    """MySQL / MariaDB via aiomysql (pyformat %s placeholders)."""

    dialect = "mysql"
    param_style = "pyformat"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._ro_pool = None
        self._aiomysql = None

    @staticmethod
    def _conn_kwargs(dsn: str) -> dict:
        cfg = _parse_dsn(dsn)
        return {
            "host": cfg["host"] or "localhost",
            "port": cfg["port"] or 3306,
            "user": cfg["user"],
            "password": cfg["password"] or "",
            "db": cfg["db"],
        }

    async def connect(self) -> None:
        import aiomysql

        self._aiomysql = aiomysql
        self._ro_pool = await aiomysql.create_pool(
            minsize=1, maxsize=5, autocommit=True, **self._conn_kwargs(self.ro_dsn),
        )

    async def close(self) -> None:
        if self._ro_pool:
            self._ro_pool.close()
            await self._ro_pool.wait_closed()

    async def _run(self, query: str, args) -> list[dict]:
        q, a = translate_params(query, args, self.param_style)
        async with self._ro_pool.acquire() as conn:
            async with conn.cursor(self._aiomysql.DictCursor) as cur:
                await cur.execute(q, a)
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def fetch_ro(self, query: str, *args, limit: int | None = None,
                       offset: int = 0) -> list[dict]:
        size = int(limit or settings.query_page_size)
        return await self._run(self._page_sql(query, size, offset), args)


class MSSQLDataSource(DataSource):
    """MSSQL / SQL Server via pymssql (synchronous; wrapped in a thread)."""

    dialect = "tsql"
    param_style = "pyformat"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._pymssql = None

    async def connect(self) -> None:
        import pymssql

        self._pymssql = pymssql

    def _page_sql(self, query: str, limit: int, offset: int) -> str:
        # SQL Server's OFFSET/FETCH requires an ORDER BY; (SELECT NULL) is the
        # accepted no-op ordering so we can paginate an arbitrary SELECT.
        inner = query.rstrip().rstrip(";")
        return (f"SELECT * FROM (\n{inner}\n) _p "
                f"ORDER BY (SELECT NULL) "
                f"OFFSET {int(offset)} ROWS FETCH NEXT {int(limit)} ROWS ONLY")

    def _query_sync(self, query: str, args) -> list[dict]:
        cfg = _parse_dsn(self.ro_dsn)
        q, a = translate_params(query, args, self.param_style)
        conn = self._pymssql.connect(
            server=cfg["host"] or "localhost",
            port=str(cfg["port"] or 1433),
            user=cfg["user"],
            password=cfg["password"] or "",
            database=cfg["db"] or "",
            timeout=settings.query_timeout,
            login_timeout=settings.query_timeout,
        )
        try:
            cur = conn.cursor(as_dict=True)
            cur.execute(q, tuple(a))
            rows = cur.fetchall() or []
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def fetch_ro(self, query: str, *args, limit: int | None = None,
                       offset: int = 0) -> list[dict]:
        size = int(limit or settings.query_page_size)
        return await asyncio.to_thread(
            self._query_sync, self._page_sql(query, size, offset), list(args)
        )


class BigQueryDataSource(DataSource):
    """Google BigQuery via google-cloud-bigquery (synchronous; wrapped in a thread).

    Auth is a service account (not a DSN): credentials come from an inline
    base64-encoded key (credentials_b64), a mounted key file (credentials_path),
    or Application Default Credentials. "Schemas" are BigQuery datasets, so
    allowed_schemas is the allowed-dataset list and tables are dataset-qualified
    (dataset.table). Every query carries a maximum_bytes_billed cost ceiling.
    """

    dialect = "bigquery"
    param_style = "qmark"

    def __init__(self, name, *, project, credentials_b64=None,
                 credentials_path=None, location=None, allowed_schemas=None,
                 description="", max_bytes_billed=None):
        if not project:
            raise ValueError(f"BigQuery source '{name}' is missing 'project'")
        self.name = name
        self.dsn = None
        self.ro_dsn = None
        self.allowed_schemas = set(allowed_schemas or [])
        self.description = description or ""
        self.project = project
        self.location = location or None
        self.credentials_b64 = credentials_b64
        self.credentials_path = credentials_path
        self.max_bytes_billed = int(max_bytes_billed) if max_bytes_billed else 0
        self._client = None

    def _credentials(self):
        if self.credentials_b64:
            import base64
            import json

            from google.oauth2 import service_account
            info = json.loads(base64.b64decode(self.credentials_b64))
            return service_account.Credentials.from_service_account_info(info)
        if self.credentials_path:
            from google.oauth2 import service_account
            return service_account.Credentials.from_service_account_file(
                self.credentials_path
            )
        return None  # Application Default Credentials

    async def connect(self) -> None:
        from google.cloud import bigquery

        self._client = bigquery.Client(
            project=self.project,
            credentials=self._credentials(),
            location=self.location,
        )

    async def close(self) -> None:
        if self._client:
            self._client.close()

    def _bq_type(self, value) -> str:
        # Params arrive as JSON scalars; map to BigQuery scalar types.
        if isinstance(value, bool):
            return "BOOL"
        if isinstance(value, int):
            return "INT64"
        if isinstance(value, float):
            return "FLOAT64"
        return "STRING"

    def _query_sync(self, query: str, args) -> list[dict]:
        from google.cloud import bigquery

        q, a = translate_params(query, args, self.param_style)
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(None, self._bq_type(v), v) for v in a
            ],
            use_legacy_sql=False,
        )
        if self.max_bytes_billed > 0:
            job_config.maximum_bytes_billed = self.max_bytes_billed
        job = self._client.query(q, job_config=job_config, location=self.location)
        result = job.result(timeout=settings.query_timeout)
        return [dict(row.items()) for row in result]

    async def fetch_ro(self, query: str, *args, limit: int | None = None,
                       offset: int = 0) -> list[dict]:
        size = int(limit or settings.query_page_size)
        return await asyncio.to_thread(
            self._query_sync, self._page_sql(query, size, offset), list(args)
        )

    # ---- introspection via the client metadata API (no information_schema) ----
    def _datasets(self, schema: str | None):
        if schema:
            return [schema]
        if self.allowed_schemas:
            return sorted(self.allowed_schemas)
        return [ds.dataset_id for ds in self._client.list_datasets()]

    async def get_schemas(self) -> list[str]:
        # If the source is scoped to specific datasets, return those without
        # calling list_datasets (the SA may lack project-level dataset:list).
        if self.allowed_schemas:
            return sorted(self.allowed_schemas)
        return await asyncio.to_thread(
            lambda: sorted(ds.dataset_id for ds in self._client.list_datasets())
        )

    def _tables_sync(self, schema: str | None) -> list[dict]:
        out = []
        for ds in self._datasets(schema):
            for t in self._client.list_tables(ds):
                out.append({
                    "schema": ds,
                    "name": t.table_id,
                    "type": str(t.table_type or ""),
                })
        return out

    async def get_tables(self, schema: str | None = None) -> list[dict]:
        return await asyncio.to_thread(self._tables_sync, schema)

    def _columns_sync(self, schema: str, table: str) -> list[dict]:
        tbl = self._client.get_table(f"{self.project}.{schema}.{table}")
        return [
            {
                "name": f.name,
                "type": f.field_type,
                "nullable": str(f.mode or "NULLABLE").upper() != "REQUIRED",
            }
            for f in tbl.schema
        ]

    async def get_columns(self, schema: str, table: str) -> list[dict]:
        return await asyncio.to_thread(self._columns_sync, schema, table)


_KIND_CLASS = {
    "redshift": (PostgresDataSource, "redshift"),
    "postgres": (PostgresDataSource, "postgres"),
    "postgresql": (PostgresDataSource, "postgres"),
    "mysql": (MySQLDataSource, "mysql"),
    "mariadb": (MySQLDataSource, "mysql"),
    "mssql": (MSSQLDataSource, "tsql"),
    "sqlserver": (MSSQLDataSource, "tsql"),
}

_SOURCES: dict[str, DataSource] = {}


def _norm_allowed(cfg: dict):
    allowed = cfg.get("allowed_schemas")
    if isinstance(allowed, str):
        allowed = {s.strip() for s in allowed.split(",") if s.strip()}
    return allowed


def _build(name: str, cfg: dict) -> DataSource:
    kind = str(cfg.get("kind", "redshift")).lower()

    if kind == "bigquery":
        return BigQueryDataSource(
            name=name,
            project=cfg.get("project"),
            credentials_b64=cfg.get("credentials_b64"),
            credentials_path=cfg.get("credentials_path"),
            location=cfg.get("location"),
            allowed_schemas=_norm_allowed(cfg),
            description=cfg.get("description", ""),
            max_bytes_billed=cfg.get(
                "max_bytes_billed", settings.bigquery_max_bytes_billed
            ),
        )

    if kind not in _KIND_CLASS:
        raise ValueError(f"Unknown datasource kind '{kind}' for source '{name}'")
    cls, dialect = _KIND_CLASS[kind]

    return cls(
        name=name,
        dsn=cfg["dsn"],
        ro_dsn=cfg.get("ro_dsn"),
        allowed_schemas=_norm_allowed(cfg),
        dialect=dialect,
        description=cfg.get("description", ""),
    )


async def init_sources() -> None:
    global _SOURCES
    _SOURCES = {}
    for name, cfg in settings.sources.items():
        source = _build(name, cfg)
        await source.connect()
        _SOURCES[name] = source


async def close_sources() -> None:
    for source in _SOURCES.values():
        await source.close()
    _SOURCES.clear()


def get_source(name: str | None = None) -> DataSource:
    key = name or settings.default_datasource
    try:
        return _SOURCES[key]
    except KeyError:
        raise KeyError(f"Unknown datasource '{key}'. Configured: {sorted(_SOURCES)}")


def all_sources() -> list[DataSource]:
    return list(_SOURCES.values())
