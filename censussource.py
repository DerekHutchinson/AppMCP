"""Server-side U.S. Census Bureau Data API calls for the /a/{slug}/census endpoint.

Apps never hold the API key; they post a small request object to
/a/{slug}/census and this module forwards it to the public Census Data API
(https://api.census.gov/data) with the key held ONLY here. This is the same
"connection method" USCensusMCP uses: the Census Data API keyed by CENSUS_API_KEY
(see USCensusMCP/mcp-server fetch-aggregate-data).

The Census Data API is read-only public data, so the guardrails here are about
keeping the proxy well-behaved rather than access control:

  * only GET, and the host is pinned to api.census.gov (we build the URL)
  * the dataset path is validated (no traversal / injection) and the URL is
    assembled from typed pieces, never a caller-supplied absolute URL
  * a per-session per-minute cap guards against runaway agent loops
  * the forwarded response body is size-capped

Uses httpx directly (already a dependency; no Census SDK needed).
"""
import logging
import re
import time
from collections import defaultdict, deque

import httpx

from config import settings

logger = logging.getLogger("appmcp.census")

CENSUS_BASE = "https://api.census.gov/data"

# Per-session call timestamps for the sliding-window rate limit (per process).
_CALL_LOG: dict[str, deque] = defaultdict(deque)

# A dataset is a slash-separated path of alnum/hyphen segments, e.g. "acs/acs1",
# "acs/acs5/subject", "dec/pl", "cbp", "timeseries/eits/marts". No dots, no
# traversal, no query string — those are supplied via the typed fields.
_DATASET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*(?:/[A-Za-z0-9][A-Za-z0-9-]*)*$")


class CensusError(Exception):
    """Raised when a Census request is invalid or the API call fails."""


def _check_rate(session_key: str) -> None:
    limit = settings.census_rate_per_min
    if limit <= 0:
        return
    now = time.monotonic()
    log = _CALL_LOG[session_key]
    while log and now - log[0] > 60:
        log.popleft()
    if len(log) >= limit:
        raise CensusError(
            "Census rate limit exceeded for this session; try again shortly."
        )
    log.append(now)


def _clean_dataset(dataset) -> str:
    if not isinstance(dataset, str) or not dataset.strip():
        raise CensusError("A 'dataset' is required, e.g. 'acs/acs1'.")
    ds = dataset.strip().strip("/")
    if not _DATASET_RE.match(ds):
        raise CensusError(
            f"Invalid dataset '{dataset}'. Use a path like 'acs/acs1' or 'dec/pl'."
        )
    return ds


def _clean_year(year, dataset: str) -> str | None:
    # Timeseries datasets carry no year in the path; everything else needs one.
    is_timeseries = dataset.startswith("timeseries")
    if year is None or (isinstance(year, str) and not year.strip()):
        if is_timeseries:
            return None
        raise CensusError("A 4-digit 'year' is required for this dataset.")
    try:
        y = int(year)
    except (TypeError, ValueError):
        raise CensusError("'year' must be a 4-digit number, e.g. 2022.")
    if not (1000 <= y <= 9999):
        raise CensusError("'year' must be a 4-digit number, e.g. 2022.")
    return str(y)


def _build_get(get) -> str:
    """Normalize the caller's `get` into the Census 'get=' string.

    Accepts a string ("NAME,B01001_001E"), a list of variables, or an object
    {variables: [...], group: "B01001"}.
    """
    if get is None:
        raise CensusError("'get' is required (variables and/or a group).")
    if isinstance(get, str):
        parts = [get.strip()] if get.strip() else []
    elif isinstance(get, list):
        parts = [str(v).strip() for v in get if str(v).strip()]
    elif isinstance(get, dict):
        parts = []
        variables = get.get("variables")
        if isinstance(variables, str):
            variables = [variables]
        if variables:
            parts += [str(v).strip() for v in variables if str(v).strip()]
        group = get.get("group")
        if group:
            parts.append(f"group({str(group).strip()})")
    else:
        raise CensusError("'get' must be a string, an array, or an object.")
    if not parts:
        raise CensusError("'get' must name at least one variable or a group.")
    return ",".join(parts)


def _rows_from_matrix(data) -> dict:
    """Turn the Census 2-D array [[headers],[row],...] into columns + row dicts."""
    if not isinstance(data, list) or not data:
        raise CensusError("Census returned an unexpected response shape.")
    headers = [str(h) for h in data[0]]
    rows = [dict(zip(headers, r)) for r in data[1:]]
    return {"columns": headers, "rows": rows, "count": len(rows)}


async def census_request(session_key: str, *, dataset, year=None, get=None,
                         for_=None, in_=None, ucgid=None, predicates=None,
                         descriptive=False) -> dict:
    """Validate and forward one Census Data API call; return columns + rows.

    Raises CensusError on any validation problem or a non-2xx API response.
    """
    if not settings.census_configured:
        raise CensusError("The Census Data API is not configured on this server.")

    ds = _clean_dataset(dataset)
    y = _clean_year(year, ds)
    get_param = _build_get(get)

    if predicates is not None and not isinstance(predicates, dict):
        raise CensusError("'predicates' must be an object of query parameters.")

    _check_rate(session_key)

    params = {"get": get_param}
    if for_:
        params["for"] = str(for_)
    if in_:
        params["in"] = str(in_)
    if ucgid:
        params["ucgid"] = str(ucgid)
    if predicates:
        for k, v in predicates.items():
            params[str(k)] = str(v)
    params["descriptive"] = "true" if descriptive else "false"
    params["key"] = settings.census_api_key.strip()

    url = f"{CENSUS_BASE}/{ds}" if y is None else f"{CENSUS_BASE}/{y}/{ds}"

    try:
        async with httpx.AsyncClient(timeout=settings.census_timeout) as client:
            resp = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise CensusError(f"Could not reach the Census Data API: {exc}") from exc

    cap = settings.census_max_response_bytes
    if cap > 0 and len(resp.content) > cap:
        raise CensusError(
            f"Census response too large ({len(resp.content)} bytes); "
            f"narrow the request (fewer variables or a smaller geography)."
        )

    if resp.status_code >= 400:
        detail = resp.text[:300].strip()
        raise CensusError(f"Census API rejected the request ({resp.status_code}): {detail}")

    try:
        data = resp.json()
    except ValueError:
        # The API returns plain-text (not JSON) for some invalid queries.
        raise CensusError(
            f"Census returned a non-JSON response: {resp.text[:200].strip()}"
        )
    return _rows_from_matrix(data)
