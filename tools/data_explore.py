"""MCP tools for exploring data sources and testing SQL at design time.

These let an agent discover the full catalog (all schemas/tables/columns it is
allowed to read) and validate its SQL before baking it into an app. The same
read-only guardrails apply here as in the published app's runtime proxy.
"""
import catalog
import datasources
from appsql import run_validated
from validation import SQLValidationError


def register(mcp) -> None:
    @mcp.tool
    async def list_datasources() -> dict:
        """List configured data sources (name, engine, description, allowed schemas).

        No credentials are ever returned. Pick a source name to use as an app's
        `datasource`.
        """
        out = []
        for s in datasources.all_sources():
            out.append({
                "name": s.name,
                "kind": s.dialect,
                "description": s.description,
                "allowed_schemas": sorted(s.allowed_schemas) or None,
            })
        return {"count": len(out), "datasources": out}

    @mcp.tool
    async def list_schemas(datasource: str | None = None) -> dict:
        """List schemas available on a data source (restricted to allowed schemas)."""
        try:
            schemas = await catalog.list_schemas(datasource)
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}
        return {"datasource": datasource, "schemas": schemas}

    @mcp.tool
    async def list_tables(datasource: str | None = None,
                          schema: str | None = None) -> dict:
        """List tables/views on a data source, optionally filtered to one schema."""
        try:
            tables = await catalog.list_tables(datasource, schema)
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}
        return {"datasource": datasource, "schema": schema,
                "count": len(tables), "tables": tables}

    @mcp.tool
    async def list_columns(schema: str, table: str,
                           datasource: str | None = None) -> dict:
        """List columns (name, type, nullable) for a table."""
        try:
            cols = await catalog.list_columns(datasource, schema, table)
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"datasource": datasource, "schema": schema, "table": table,
                "columns": cols}

    @mcp.tool
    async def run_query(sql: str, datasource: str | None = None,
                        params: list | None = None) -> dict:
        """Run a read-only SELECT to validate/preview results before building an app.

        Args:
            sql: A single read-only SELECT. Tables must be schema-qualified and
                in the source's allowed schemas. Use positional params $1..$n.
            datasource: Source name (defaults to the configured default).
            params: Values for $1..$n, in order.

        Returns {"columns": [...], "rows": [...]} (row-capped). This is the same
        execution path published apps use at runtime via AppData.query().
        """
        try:
            return await run_validated(datasource, sql, params)
        except SQLValidationError as exc:
            return {"ok": False, "error": f"Rejected: {exc}"}
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"Query failed: {exc}"}
