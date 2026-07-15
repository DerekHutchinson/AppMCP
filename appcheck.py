"""Static checks on agent-authored app HTML (publish-time + the check_app tool).

Apps may be created WITHOUT a datasource (purely static/reference pages); those
have no data to keep live, so the anti-baked-data checks do not apply to them.
Only when an app is bound to a datasource do we enforce that its data is loaded
live via ``AppData.query()`` rather than baked into the HTML as literal arrays —
baked data goes stale, breaks filtering, and bypasses per-user attribution.

We cannot force arbitrary JavaScript to fetch data at runtime, but we CAN
reliably catch the common failure (pasting a query result set into the HTML).
These heuristics run inside ``create_app``/``update_app`` and are exposed via the
``check_app`` MCP tool so agents self-correct before publishing.

The signals are intentionally conservative to avoid false positives on
legitimate inline reference data (lookup maps, map geometry/TopoJSON), which are
nested-number arrays or single objects rather than large arrays of row objects.
"""
import re

# A sanctioned runtime data path: AppData.query / queryPages / s3 (any spacing).
_APPDATA_RE = re.compile(r"\bAppData\s*\.\s*(?:query|queryPages|s3)\b")

# Object boundary inside an array literal: `},{` ignoring whitespace/newlines.
# A baked result set of N rows produces N-1 of these; TopoJSON arcs and lookup
# maps (arrays of numbers / single objects) do not.
_OBJ_SEP_RE = re.compile(r"\}\s*,\s*\{")

# Inline event handlers (onclick=, onchange=, ...) — blocked by the app CSP.
_INLINE_HANDLER_RE = re.compile(r"<[^>]+\son[a-z]+\s*=\s*[\"']", re.IGNORECASE)

# eval / new Function — blocked by the app CSP.
_EVAL_RE = re.compile(r"\b(?:eval|new\s+Function)\s*\(", re.IGNORECASE)

# Estimated row count (object-separator count + 1) thresholds for a single doc.
WARN_BAKED_ROWS = 15
FAIL_BAKED_ROWS = 60


def check_html(html: str, has_datasource: bool = True) -> dict:
    """Return {"ok", "errors", "warnings"} for a candidate app HTML document.

    ``has_datasource`` controls the anti-baked-data checks: they apply only to
    apps bound to a datasource. A datasource-less app is treated as a static page
    and only gets advisory CSP warnings.

    ``errors`` block publishing (unless the caller opts into inline data);
    ``warnings`` are advisory. ``ok`` is True when there are no errors.
    """
    html = html or ""
    errors: list[str] = []
    warnings: list[str] = []

    # Anti-baked-data checks only make sense when the app has a datasource to
    # keep live. Static (no-datasource) apps may legitimately inline their data.
    if has_datasource:
        has_appdata = bool(_APPDATA_RE.search(html))
        seps = len(_OBJ_SEP_RE.findall(html))
        est_rows = seps + 1 if seps else 0

        if est_rows >= FAIL_BAKED_ROWS:
            errors.append(
                f"Found a large inline array of ~{est_rows} objects, which looks "
                "like baked-in query results. This app is bound to a datasource, "
                "so load its data with AppData.query() instead. If it is genuine "
                "reference data (a lookup table or map geometry), set "
                "allow_inline_data=true."
            )
        elif est_rows >= WARN_BAKED_ROWS:
            warnings.append(
                f"Found an inline array of ~{est_rows} objects. If this is query "
                "data, fetch it with AppData.query() instead of embedding it "
                "(reference lookups and map geometry are fine)."
            )

        if not has_appdata:
            warnings.append(
                "This app is bound to a datasource but never calls AppData.query(). "
                "If it displays data, load it live via AppData.query() rather than "
                "baking it into the HTML. (Ignore this if the app has no data or "
                "was not meant to use a datasource.)"
            )

    if _INLINE_HANDLER_RE.search(html):
        warnings.append(
            "Inline on* event handlers detected; these are blocked by the CSP. "
            "Attach listeners with addEventListener in a <script> block."
        )
    if _EVAL_RE.search(html):
        warnings.append(
            "eval()/new Function() detected; these are blocked by the CSP."
        )

    return {"ok": not errors, "errors": errors, "warnings": warnings}
