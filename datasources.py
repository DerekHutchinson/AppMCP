"""Pluggable data-source layer (adapted from KPIMCP).

Each engine has a DataSource implementation that knows how to run queries, cap
result rows, dry-run, and which sqlglot dialect to validate against.

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
    """Translate canonical $n placeholders to the driver's placeholder style."""
    args = list(args)
    if style == "numeric":
        return sql, args

    escaped = sql.replace("%", "%%")
    new_args = []

    def _repl(m):
        new_args.append(args[int(m.group(1)) - 1])
        return "%s"

    return _PARAM_RE.sub(_repl, escaped), new_args


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

    async def fetch_ro(self, query: str, *args, limit: int | None = None) -> list[dict]:
        raise NotImplementedError

    def _cap_limit(self, query: str, cap: int) -> str:
        inner = query.rstrip().rstrip(";")
        return f"SELECT * FROM (\n{inner}\n) _capped LIMIT {cap}"


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

    async def fetch_ro(self, query: str, *args, limit: int | None = None) -> list[dict]:
        cap = int(limit or settings.max_query_rows)
        async with self._ro_pool.acquire() as conn:
            rows = await conn.fetch(self._cap_limit(query, cap), *args)
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

    async def fetch_ro(self, query: str, *args, limit: int | None = None) -> list[dict]:
        cap = int(limit or settings.max_query_rows)
        return await self._run(self._cap_limit(query, cap), args)


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

    def _cap_top(self, query: str, cap: int) -> str:
        inner = query.rstrip().rstrip(";")
        return f"SELECT TOP ({cap}) * FROM (\n{inner}\n) _capped"

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

    async def fetch_ro(self, query: str, *args, limit: int | None = None) -> list[dict]:
        cap = int(limit or settings.max_query_rows)
        return await asyncio.to_thread(
            self._query_sync, self._cap_top(query, cap), list(args)
        )


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


def _build(name: str, cfg: dict) -> DataSource:
    kind = str(cfg.get("kind", "redshift")).lower()
    if kind not in _KIND_CLASS:
        raise ValueError(f"Unknown datasource kind '{kind}' for source '{name}'")
    cls, dialect = _KIND_CLASS[kind]

    allowed = cfg.get("allowed_schemas")
    if isinstance(allowed, str):
        allowed = {s.strip() for s in allowed.split(",") if s.strip()}

    return cls(
        name=name,
        dsn=cfg["dsn"],
        ro_dsn=cfg.get("ro_dsn"),
        allowed_schemas=allowed,
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
