"""Shared validated read-only query runner.

Used by both the design-time `run_query` MCP tool and the runtime `/a/{slug}/sql`
proxy, so the exact same guardrails apply whether the agent is authoring or a
published app is serving data to a logged-in viewer.
"""
import datetime
from decimal import Decimal

import datasources
import db
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
    return str(value)


async def run_validated(
    datasource: str | None,
    sql: str,
    params: list | None = None,
) -> dict:
    """Validate + run a read-only SELECT. Returns {"columns": [...], "rows": [...]}.

    Raises SQLValidationError for guardrail failures and KeyError for an unknown
    datasource. The result row cap is enforced by the data-source layer. Row
    values are coerced to JSON-safe types so both the MCP tool and the runtime
    proxy can serialize them.
    """
    params = list(params or [])
    source = datasources.get_source(datasource)  # raises KeyError if unknown
    validate_sql(
        sql,
        source.allowed_schemas,
        declared_params=len(params),
        dialect=source.dialect,
    )
    rows = await db.fetch_ro(sql, *params, datasource=source.name)
    columns = list(rows[0].keys()) if rows else []
    safe_rows = [{k: _json_safe(v) for k, v in r.items()} for r in rows]
    return {"columns": columns, "rows": safe_rows}
