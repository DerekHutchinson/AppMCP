"""Schema introspection over the read-only connections (information_schema).

Trusted, server-issued SQL (NOT agent input), so it bypasses the SQL guardrails.
Results are restricted to each datasource's allowed_schemas when that allowlist
is configured, so agents only ever see what a published app could actually read.
"""
import datasources
import db


def _allowed(source: datasources.DataSource, schema: str) -> bool:
    return (not source.allowed_schemas) or (schema in source.allowed_schemas)


async def list_schemas(datasource: str | None = None) -> list[str]:
    source = datasources.get_source(datasource)
    rows = await db.fetch_ro(
        "SELECT schema_name AS schema_name FROM information_schema.schemata",
        datasource=source.name,
    )
    names = sorted({str(r["schema_name"]) for r in rows})
    return [s for s in names if _allowed(source, s)]


async def list_tables(datasource: str | None = None,
                      schema: str | None = None) -> list[dict]:
    source = datasources.get_source(datasource)
    if schema:
        rows = await db.fetch_ro(
            "SELECT table_schema AS table_schema, table_name AS table_name, "
            "table_type AS table_type FROM information_schema.tables "
            "WHERE table_schema = $1",
            schema,
            datasource=source.name,
        )
    else:
        rows = await db.fetch_ro(
            "SELECT table_schema AS table_schema, table_name AS table_name, "
            "table_type AS table_type FROM information_schema.tables",
            datasource=source.name,
        )
    out = [
        {
            "schema": str(r["table_schema"]),
            "name": str(r["table_name"]),
            "type": str(r.get("table_type") or ""),
        }
        for r in rows
        if _allowed(source, str(r["table_schema"]))
    ]
    out.sort(key=lambda t: (t["schema"], t["name"]))
    return out


async def list_columns(datasource: str | None, schema: str, table: str) -> list[dict]:
    source = datasources.get_source(datasource)
    if not _allowed(source, schema):
        raise ValueError(f"Schema '{schema}' is not allowed on '{source.name}'.")
    rows = await db.fetch_ro(
        "SELECT column_name AS column_name, data_type AS data_type, "
        "is_nullable AS is_nullable, ordinal_position AS ordinal_position "
        "FROM information_schema.columns WHERE table_schema = $1 AND table_name = $2",
        schema, table,
        datasource=source.name,
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
