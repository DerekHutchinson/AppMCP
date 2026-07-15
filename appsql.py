"""Shared validated read-only query runner.

Used by both the design-time `run_query` MCP tool and the runtime `/a/{slug}/sql`
proxy, so the exact same guardrails apply whether the agent is authoring or a
published app is serving data to a logged-in viewer.
"""
import datetime
from decimal import Decimal

import datasources
import db
from config import settings
from validation import SQLValidationError, validate_sql


def _json_safe(value):
    """Coerce a DB value to something json.dumps (Starlette/JSON) can render.

    Drivers return types like Decimal (numerics), date/datetime/time, and bytes
    that the stdlib JSON encoder rejects. Without this the runtime proxy fails
    during response serialization (a 500 outside the request try/except).
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", "replace")
    # Nested structures (e.g. BigQuery STRUCT/ARRAY) may hold non-serializable
    # leaves, so recurse rather than stringifying the whole thing.
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


async def run_validated(
    datasource: str | None,
    sql: str,
    params: list | None = None,
    page: int = 0,
    page_size: int | None = None,
) -> dict:
    """Validate + run one page of a read-only SELECT.

    Returns {"columns", "rows", "page", "page_size", "has_more"}. The author's
    SELECT is wrapped with LIMIT/OFFSET by the data-source layer, so any query
    paginates without being written for it. We fetch page_size+1 rows to detect
    has_more cheaply (no COUNT). Row values are coerced to JSON-safe types.

    Raises SQLValidationError for guardrail failures and KeyError for an unknown
    datasource.
    """
    params = list(params or [])
    source = datasources.get_source(datasource)  # raises KeyError if unknown
    validate_sql(
        sql,
        source.allowed_schemas,
        declared_params=len(params),
        dialect=source.dialect,
    )
    size = int(page_size or settings.query_page_size)
    size = max(1, min(size, settings.query_page_size))  # never exceed the max page
    page = max(0, int(page))
    offset = page * size

    # Over-fetch by one row to know whether another page exists.
    rows = await db.fetch_ro(
        sql, *params, datasource=source.name, limit=size + 1, offset=offset
    )
    has_more = len(rows) > size
    rows = rows[:size]
    columns = list(rows[0].keys()) if rows else []
    safe_rows = [{k: _json_safe(v) for k, v in r.items()} for r in rows]
    return {
        "columns": columns,
        "rows": safe_rows,
        "page": page,
        "page_size": size,
        "has_more": has_more,
    }
