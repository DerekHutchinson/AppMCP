"""S3 object sources for the app /a/{slug}/s3 endpoint.

Apps never hold AWS credentials; they post {op, prefix|key} to /a/{slug}/s3 and
this module talks to Amazon S3 (or an S3-compatible endpoint) using credentials
stored ONLY here (parsed from settings.s3_sources, same "creds live on the
proxy" model as the SQL datasources).

Read-only by design — exactly two operations:

  * list  -> ListObjectsV2, returns [{key, size, last_modified}] (keys are
             RELATIVE to the source prefix, so the app can pass one back to get)
  * get   -> GetObject, returns the bytes as text (utf-8) or base64, size-capped

Guardrails (defense-in-depth alongside the endpoint):

  * every source is pinned to ONE bucket and an optional key prefix; the app can
    never escape that prefix (no '..', no absolute keys, no other bucket)
  * object bytes and list page size are hard-capped from config
  * a per-session per-minute cap guards against runaway agent loops

boto3 is synchronous, so calls are wrapped in asyncio.to_thread (same pattern as
the pymssql datasource). One client per source is created at startup and reused.
"""
import asyncio
import base64
import time
from collections import defaultdict, deque

from config import settings

# Per-session call timestamps for the sliding-window rate limit (per process).
_CALL_LOG: dict[str, deque] = defaultdict(deque)


class S3Error(Exception):
    """Raised when an S3 request is invalid, forbidden, or fails."""


class S3Source:
    """One configured, credential-bearing S3 bucket/prefix (read-only)."""

    def __init__(self, name, bucket, region=None, access_key_id=None,
                 secret_access_key=None, prefix="", endpoint_url=None,
                 description=""):
        if not bucket:
            raise ValueError(f"S3 source '{name}' is missing 'bucket'")
        self.name = name
        self.bucket = bucket
        self.region = region or "us-east-1"
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        # Confinement boundary: everything the app reaches is under this prefix.
        # Normalize to no leading slash (S3 keys have no leading slash).
        self.prefix = (prefix or "").lstrip("/")
        self.endpoint_url = endpoint_url or None
        self.description = description or ""
        self._client = None

    def connect(self) -> None:
        import boto3

        self._client = boto3.client(
            "s3",
            region_name=self.region,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            endpoint_url=self.endpoint_url,
        )

    # ---- key confinement ----
    def _clean_rel(self, rel: str) -> str:
        """Validate an app-supplied key/prefix (relative to this source)."""
        rel = "" if rel is None else str(rel)
        if "\\" in rel or "://" in rel or "\x00" in rel:
            raise S3Error("Invalid key.")
        rel = rel.lstrip("/")
        # Reject any parent-directory traversal component.
        if rel == ".." or rel.startswith("../") or "/../" in rel or rel.endswith("/.."):
            raise S3Error("Invalid key (path traversal is not allowed).")
        return rel

    def _full_key(self, rel: str) -> str:
        return self.prefix + self._clean_rel(rel)

    def _to_rel(self, full_key: str) -> str | None:
        """Strip the source prefix; None if the key escapes it (defensive)."""
        if self.prefix and not full_key.startswith(self.prefix):
            return None
        return full_key[len(self.prefix):]

    # ---- operations (sync; called via asyncio.to_thread) ----
    def _list_sync(self, rel_prefix: str, max_keys: int) -> list[dict]:
        full_prefix = self._full_key(rel_prefix)
        paginator = self._client.get_paginator("list_objects_v2")
        out: list[dict] = []
        for page in paginator.paginate(
            Bucket=self.bucket, Prefix=full_prefix,
            PaginationConfig={"MaxItems": max_keys, "PageSize": min(max_keys, 1000)},
        ):
            for obj in page.get("Contents", []):
                rel = self._to_rel(obj["Key"])
                if rel is None or rel == "":
                    continue
                lm = obj.get("LastModified")
                out.append({
                    "key": rel,
                    "size": int(obj.get("Size", 0)),
                    "last_modified": lm.isoformat() if lm else None,
                })
                if len(out) >= max_keys:
                    return out
        return out

    def _get_sync(self, rel_key: str, max_bytes: int) -> dict:
        clean = self._clean_rel(rel_key)
        if not clean:
            raise S3Error("A key is required.")
        full = self.prefix + clean
        resp = self._client.get_object(Bucket=self.bucket, Key=full)
        size = int(resp.get("ContentLength") or 0)
        if max_bytes > 0 and size > max_bytes:
            raise S3Error(
                f"Object is too large ({size} bytes; max {max_bytes}). "
                f"Fetch a smaller object or a range."
            )
        # Read one byte past the cap to catch unknown-length streams too.
        raw = resp["Body"].read(max_bytes + 1 if max_bytes > 0 else None)
        if max_bytes > 0 and len(raw) > max_bytes:
            raise S3Error(f"Object is too large (>{max_bytes} bytes).")
        content_type = resp.get("ContentType") or "application/octet-stream"
        try:
            body = raw.decode("utf-8")
            encoding = "text"
        except UnicodeDecodeError:
            body = base64.b64encode(raw).decode("ascii")
            encoding = "base64"
        return {
            "key": clean,
            "size": len(raw),
            "content_type": content_type,
            "encoding": encoding,
            "body": body,
        }


_SOURCES: dict[str, S3Source] = {}


def _build(name: str, cfg: dict) -> S3Source:
    return S3Source(
        name=name,
        bucket=cfg.get("bucket"),
        region=cfg.get("region"),
        access_key_id=cfg.get("access_key_id"),
        secret_access_key=cfg.get("secret_access_key"),
        prefix=cfg.get("prefix", ""),
        endpoint_url=cfg.get("endpoint_url"),
        description=cfg.get("description", ""),
    )


def init_sources() -> None:
    global _SOURCES
    _SOURCES = {}
    for name, cfg in settings.s3_sources.items():
        source = _build(name, cfg)
        source.connect()
        _SOURCES[name] = source


def get_source(name: str) -> S3Source:
    try:
        return _SOURCES[name]
    except KeyError:
        raise KeyError(f"Unknown S3 source '{name}'. Configured: {sorted(_SOURCES)}")


def all_sources() -> list[S3Source]:
    return list(_SOURCES.values())


def _check_rate(session_key: str) -> None:
    limit = settings.s3_rate_per_min
    if limit <= 0:
        return
    now = time.monotonic()
    log = _CALL_LOG[session_key]
    while log and now - log[0] > 60:
        log.popleft()
    if len(log) >= limit:
        raise S3Error("S3 rate limit exceeded for this session; try again shortly.")
    log.append(now)


async def list_objects(name: str, prefix: str = "", max_keys: int | None = None,
                       session_key: str = "") -> list[dict]:
    """List objects under `prefix` (relative to the source's own prefix)."""
    _check_rate(session_key)
    source = get_source(name)
    cap = settings.s3_max_list_keys
    n = cap if not max_keys else min(int(max_keys), cap)
    try:
        return await asyncio.to_thread(source._list_sync, prefix or "", n)
    except S3Error:
        raise
    except Exception as exc:  # noqa: BLE001
        raise S3Error(f"S3 list failed: {_reason(exc)}") from exc


async def get_object(name: str, key: str, session_key: str = "") -> dict:
    """Fetch one object's bytes (relative key), size-capped and text/base64."""
    _check_rate(session_key)
    source = get_source(name)
    try:
        return await asyncio.to_thread(
            source._get_sync, key, settings.s3_max_object_bytes
        )
    except S3Error:
        raise
    except Exception as exc:  # noqa: BLE001
        raise S3Error(f"S3 get failed: {_reason(exc)}") from exc


def _reason(exc: Exception) -> str:
    """Best-effort short reason from a botocore ClientError (or any exception)."""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        err = resp.get("Error") or {}
        code = err.get("Code")
        if code == "NoSuchKey":
            return "no such object"
        if code in ("AccessDenied", "403"):
            return "access denied"
        if code == "NoSuchBucket":
            return "no such bucket"
        if code:
            return str(code)
    return str(exc)
