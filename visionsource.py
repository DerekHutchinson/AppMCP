"""Server-side Google Cloud Vision API calls for the /a/{slug}/vision endpoint.

Apps never hold the API key; they post an image (inline base64 content) plus the
features they want, and this module forwards a `images:annotate` request to
https://vision.googleapis.com/v1/images:annotate with the key held ONLY here
(VISION_API_KEY). Mirrors the Census/Graph/S3 proxies.

Guardrails (the key is powerful and billable, so keep the proxy well-behaved):

  * only POST, and the URL is pinned to vision.googleapis.com/v1/images:annotate
    (built here, never a caller-supplied absolute URL)
  * inline image content only — remote `source`/`imageUri` fetches are rejected,
    so the request is exactly "send image bytes, get results", with no
    server-side URL fetching surprises
  * the request body the app may post is size-capped and the batch is limited to
    VISION_MAX_REQUESTS images
  * a per-session per-minute cap guards against runaway agent loops
  * the forwarded response body is size-capped

Uses httpx directly (already a dependency; no Google SDK / GCP auth needed).
"""
import logging
import time
from collections import defaultdict, deque

import httpx

from config import settings

logger = logging.getLogger("appmcp.vision")

VISION_URL = "https://vision.googleapis.com/v1/images:annotate"

# Per-session call timestamps for the sliding-window rate limit (per process).
_CALL_LOG: dict[str, deque] = defaultdict(deque)

# Standard Vision feature types. Validated up front so apps get a clear error
# instead of an opaque 400 from Google.
_FEATURE_TYPES = {
    "LABEL_DETECTION",
    "TEXT_DETECTION",
    "DOCUMENT_TEXT_DETECTION",
    "FACE_DETECTION",
    "LANDMARK_DETECTION",
    "LOGO_DETECTION",
    "SAFE_SEARCH_DETECTION",
    "IMAGE_PROPERTIES",
    "CROP_HINTS",
    "WEB_DETECTION",
    "OBJECT_LOCALIZATION",
    "PRODUCT_SEARCH",
}


class VisionError(Exception):
    """Raised when a Vision request is invalid or the API call fails."""


def _check_rate(session_key: str) -> None:
    limit = settings.vision_rate_per_min
    if limit <= 0:
        return
    now = time.monotonic()
    log = _CALL_LOG[session_key]
    while log and now - log[0] > 60:
        log.popleft()
    if len(log) >= limit:
        raise VisionError(
            "Vision rate limit exceeded for this session; try again shortly."
        )
    log.append(now)


def _clean_content(image) -> str:
    """Return the base64 image content from a string or {content:...} object.

    Accepts a raw base64 string, a data URL ("data:image/png;base64,AAAA"), or an
    object with a `content` field. Rejects remote sources (source/imageUri).
    """
    if isinstance(image, dict):
        if image.get("source") or image.get("imageUri"):
            raise VisionError(
                "Remote image sources are not allowed; send inline base64 content."
            )
        content = image.get("content")
    else:
        content = image
    if not isinstance(content, str) or not content.strip():
        raise VisionError("An image is required (base64 'content').")
    content = content.strip()
    # Tolerate a data URL prefix; the API wants bare base64.
    if content.startswith("data:"):
        comma = content.find(",")
        if comma == -1:
            raise VisionError("Malformed data URL for the image.")
        content = content[comma + 1:].strip()
    return content


def _clean_features(features) -> list[dict]:
    """Normalize features into [{type, maxResults?}] and validate the types.

    Accepts a single string, a list of strings, or a list of
    {type, maxResults} objects.
    """
    if features is None:
        raise VisionError("At least one 'feature' (e.g. 'LABEL_DETECTION') is required.")
    if isinstance(features, str):
        features = [features]
    if not isinstance(features, list) or not features:
        raise VisionError("'features' must be a non-empty array.")

    out: list[dict] = []
    for f in features:
        if isinstance(f, str):
            ftype, max_results = f.strip(), None
        elif isinstance(f, dict):
            ftype = str(f.get("type", "")).strip()
            max_results = f.get("maxResults")
        else:
            raise VisionError("Each feature must be a string or {type, maxResults}.")
        ftype = ftype.upper()
        if ftype not in _FEATURE_TYPES:
            raise VisionError(
                f"Unknown feature '{ftype}'. Valid: {', '.join(sorted(_FEATURE_TYPES))}."
            )
        entry = {"type": ftype}
        if max_results is not None:
            try:
                entry["maxResults"] = int(max_results)
            except (TypeError, ValueError):
                raise VisionError("'maxResults' must be an integer.")
        out.append(entry)
    return out


def _build_requests(*, requests, image, features, image_context) -> list[dict]:
    """Build the Vision `requests` array from either a full passthrough list or
    the convenience {image, features, image_context} shape."""
    if requests is not None:
        if not isinstance(requests, list) or not requests:
            raise VisionError("'requests' must be a non-empty array.")
        built = []
        for r in requests:
            if not isinstance(r, dict):
                raise VisionError("Each entry in 'requests' must be an object.")
            entry = {
                "image": {"content": _clean_content(r.get("image"))},
                "features": _clean_features(r.get("features")),
            }
            if r.get("imageContext") is not None:
                if not isinstance(r["imageContext"], dict):
                    raise VisionError("'imageContext' must be an object.")
                entry["imageContext"] = r["imageContext"]
            built.append(entry)
    else:
        entry = {
            "image": {"content": _clean_content(image)},
            "features": _clean_features(features),
        }
        if image_context is not None:
            if not isinstance(image_context, dict):
                raise VisionError("'imageContext' must be an object.")
            entry["imageContext"] = image_context
        built = [entry]

    if len(built) > settings.vision_max_requests:
        raise VisionError(
            f"Too many images ({len(built)}); max {settings.vision_max_requests} per call."
        )
    return built


async def vision_request(session_key: str, *, requests=None, image=None,
                         features=None, image_context=None) -> dict:
    """Validate and forward one Vision annotate call; return {responses:[...]}.

    Raises VisionError on any validation problem or a non-2xx API response.
    """
    if not settings.vision_configured:
        raise VisionError("The Vision API is not configured on this server.")

    body = {"requests": _build_requests(
        requests=requests, image=image, features=features,
        image_context=image_context,
    )}

    _check_rate(session_key)

    try:
        async with httpx.AsyncClient(timeout=settings.vision_timeout) as client:
            resp = await client.post(
                VISION_URL,
                params={"key": settings.vision_api_key.strip()},
                json=body,
            )
    except httpx.HTTPError as exc:
        raise VisionError(f"Could not reach the Vision API: {exc}") from exc

    cap = settings.vision_max_response_bytes
    if cap > 0 and len(resp.content) > cap:
        raise VisionError(
            f"Vision response too large ({len(resp.content)} bytes); "
            f"request fewer features or a smaller image."
        )

    if resp.status_code >= 400:
        detail = resp.text[:300].strip()
        raise VisionError(f"Vision API rejected the request ({resp.status_code}): {detail}")

    try:
        return resp.json()
    except ValueError:
        raise VisionError(
            f"Vision returned a non-JSON response: {resp.text[:200].strip()}"
        )
