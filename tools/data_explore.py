"""MCP tools for exploring data sources and testing SQL at design time.

These let an agent discover the full catalog (all schemas/tables/columns it is
allowed to read) and validate its SQL before baking it into an app. The same
read-only guardrails apply here as in the published app's runtime proxy.
"""
import catalog
import censussource
import datasources
import s3source
from appsql import run_validated
from auth import CURRENT_USER_EMAIL
from config import settings
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
    async def list_s3_sources() -> dict:
        """List configured S3 object sources (name, bucket, region, prefix, desc).

        No credentials are ever returned. Bind one to an app via create_app's
        `s3_source`; the app then reads files with AppData.s3.list()/get(). Each
        source is confined to its bucket and (optional) prefix.
        """
        out = []
        for s in s3source.all_sources():
            out.append({
                "name": s.name,
                "bucket": s.bucket,
                "region": s.region,
                "prefix": s.prefix or None,
                "description": s.description,
            })
        return {"count": len(out), "s3_sources": out}

    @mcp.tool
    async def list_s3_objects(source: str, prefix: str = "",
                              max_keys: int | None = None) -> dict:
        """List objects in an S3 source under `prefix` (relative to its prefix).

        Returns {objects: [{key, size, last_modified}]} with keys RELATIVE to the
        source, ready to pass to AppData.s3.get(). Read-only; capped per config.
        """
        try:
            objects = await s3source.list_objects(
                source, prefix=prefix or "", max_keys=max_keys,
                session_key=CURRENT_USER_EMAIL.get() or "mcp",
            )
        except s3source.S3Error as exc:
            return {"ok": False, "error": str(exc)}
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}
        return {"source": source, "prefix": prefix or "",
                "count": len(objects), "objects": objects}

    @mcp.tool
    async def census_query(dataset: str, get: list[str] | str, year: int | None = None,
                           group: str | None = None, for_geo: str | None = None,
                           in_geo: str | None = None, ucgid: str | None = None,
                           descriptive: bool = False) -> dict:
        """Query the U.S. Census Bureau Data API to validate/preview before building.

        This is the same path published apps use at runtime via AppData.census().
        The Census Data API is public, read-only data (population, demographics,
        income, housing, employment, economics).

        Args:
            dataset: Dataset path, e.g. 'acs/acs1', 'acs/acs5/subject', 'dec/pl'.
            get: Variable(s) to fetch, e.g. ['NAME','B01001_001E'] (or a string).
            year: 4-digit vintage, e.g. 2022 (omit only for timeseries datasets).
            group: Optional variable group to fetch instead of/with variables,
                e.g. 'B01001'.
            for_geo: Geography to fetch, e.g. 'state:*' or 'county:*'.
            in_geo: Parent geography restriction, e.g. 'state:36'.
            ucgid: Restrict by Uniform Census Geography Identifier.
            descriptive: Include variable labels in the response.

        Returns {"columns": [...], "rows": [{col: val}, ...], "count": n}.
        """
        if not settings.census_configured:
            return {"ok": False, "error": "The Census Data API is not configured."}
        get_arg: dict = {"variables": get}
        if group:
            get_arg["group"] = group
        try:
            return await censussource.census_request(
                CURRENT_USER_EMAIL.get() or "mcp",
                dataset=dataset, year=year, get=get_arg,
                for_=for_geo, in_=in_geo, ucgid=ucgid, descriptive=descriptive,
            )
        except censussource.CensusError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"Census query failed: {exc}"}

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
