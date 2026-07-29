"""Catalog icon set: auto-discovered from static/icons/.

Icons are plain SVG files dropped into `static/icons/`. The available set is the
list of `*.svg` file stems in that directory, so adding a new option is just a
matter of adding a file (a server restart picks it up). Each app stores an
optional `icon` name; the catalog resolves it to a real file, falling back to a
per-category default and finally to `settings.app_default_icon`.

The SVGs render via `<img src="/static/icons/<name>.svg">`, which is safe under
the app/catalog CSP (`img-src 'self'`).
"""
import functools
from pathlib import Path

from config import settings

_ICON_DIR = Path(__file__).resolve().parent / "static" / "icons"


@functools.lru_cache(maxsize=1)
def available() -> tuple[str, ...]:
    """Sorted icon names discovered in static/icons/ (cached for process life)."""
    if not _ICON_DIR.is_dir():
        return ()
    return tuple(sorted(p.stem for p in _ICON_DIR.glob("*.svg") if p.stem))


def exists(name: str | None) -> bool:
    return bool(name) and name in available()


def url_for(name: str) -> str:
    return f"/static/icons/{name}.svg"


def default_for_category(category: str | None) -> str:
    """Icon to use when an app has no icon of its own (best-effort)."""
    mapped = settings.category_icon_defaults.get(category or "Other")
    if exists(mapped):
        return mapped
    if exists(settings.app_default_icon):
        return settings.app_default_icon
    av = available()
    return av[0] if av else ""


def resolve(name: str | None, category: str | None = None) -> str:
    """Map a stored/requested icon name to a real icon file.

    Returns the name itself when it exists, else the category default, else the
    global default. May be "" only if static/icons/ is empty.
    """
    n = (name or "").strip().lower()
    if exists(n):
        return n
    return default_for_category(category)
