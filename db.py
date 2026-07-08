"""Thin facade over the data-source registry (read-only paths only)."""
import datasources


async def init_db() -> None:
    await datasources.init_sources()


async def close_db() -> None:
    await datasources.close_sources()


async def fetch_ro(query: str, *args, datasource: str | None = None,
                   limit: int | None = None) -> list[dict]:
    return await datasources.get_source(datasource).fetch_ro(query, *args, limit=limit)
