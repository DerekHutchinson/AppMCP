"""Schema introspection over the read-only connections.

Trusted, server-issued metadata (NOT agent input). Each datasource knows how to
enumerate its own schemas/tables/columns (information_schema for SQL engines, the
client metadata API for BigQuery); this module just delegates and restricts the
results to each datasource's allowed_schemas, so agents only ever see what a
published app could actually read.
"""
import datasources


def _allowed(source: datasources.DataSource, schema: str) -> bool:
    return (not source.allowed_schemas) or (schema in source.allowed_schemas)


async def list_schemas(datasource: str | None = None) -> list[str]:
    source = datasources.get_source(datasource)
    names = await source.get_schemas()
    return [s for s in sorted(set(names)) if _allowed(source, s)]


async def list_tables(datasource: str | None = None,
                      schema: str | None = None) -> list[dict]:
    source = datasources.get_source(datasource)
    tables = await source.get_tables(schema)
    out = [t for t in tables if _allowed(source, t["schema"])]
    out.sort(key=lambda t: (t["schema"], t["name"]))
    return out


async def list_columns(datasource: str | None, schema: str, table: str) -> list[dict]:
    source = datasources.get_source(datasource)
    if not _allowed(source, schema):
        raise ValueError(f"Schema '{schema}' is not allowed on '{source.name}'.")
    return await source.get_columns(schema, table)
