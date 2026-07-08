"""Shared HTML rendering: catalog/manage pages + sandboxed app serving.

Published apps are untrusted, agent-authored HTML. We serve them from this
isolated origin with a strict Content-Security-Policy and a per-render nonce on
inline scripts, and we inject a small `AppData` client so the app fetches data
from its own `/a/{slug}/sql` proxy (connect-src 'self') instead of ever holding
credentials.
"""
import json
import re
import secrets

from jinja2 import Template

from config import settings
from version import APP_VERSION

# Injected into every served app. Defines window.AppData.query(sql, params)
# -> {columns, rows}, posting to this app's own scoped, validated SQL proxy.
APP_DATA_JS = """
window.AppData = {
  async query(sql, params) {
    const res = await fetch('/a/' + window.APP_SLUG + '/sql', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ sql: sql, params: params || [] })
    });
    const data = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(data.error || ('Query failed (HTTP ' + res.status + ')'));
    return data;
  }
};
"""

_SCRIPT_TAG_RE = re.compile(r"<script(?![^>]*\bnonce=)", re.IGNORECASE)
_BODY_OPEN_RE = re.compile(r"(<body[^>]*>)", re.IGNORECASE)


def render_app(slug: str, html: str) -> tuple[str, str]:
    """Return (html, csp_header) for serving a published app.

    Stamps a nonce onto inline <script> tags and injects the AppData
    bootstrap so the app can call its data proxy under a strict CSP.
    """
    nonce = secrets.token_urlsafe(16)

    stamped = _SCRIPT_TAG_RE.sub(f'<script nonce="{nonce}"', html or "")

    bootstrap = (
        f'<script nonce="{nonce}">window.APP_SLUG={json.dumps(slug)};'
        f'{APP_DATA_JS}</script>'
    )
    stamped, n = _BODY_OPEN_RE.subn(r"\1" + bootstrap, stamped, count=1)
    if n == 0:
        stamped = bootstrap + stamped

    extra = settings.csp_script_src_extra.strip()
    script_src = f"'self' 'nonce-{nonce}'" + (f" {extra}" if extra else "")
    csp = "; ".join([
        "default-src 'none'",
        f"script-src {script_src}",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: https:",
        "font-src 'self' https: data:",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'self'",
    ])
    return stamped, csp


_HEAD = """
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/static/appmcp.css">
"""

CATALOG = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>""" + _HEAD + """
  <title>App Catalog</title>
</head>
<body>
  <header class="topbar">
    <div>
      <div class="brand">Gabriel &amp; Co. &middot; App Catalog <span class="pill">v{{ version }}</span></div>
      <h1>Published apps</h1>
    </div>
    <div class="userbox">
      <span class="who">{{ email }}</span>
      {% if can_author %}<a class="chip" href="/manage">Manage</a>{% endif %}
      <a class="chip" href="/logout">Sign out</a>
    </div>
  </header>
  <main class="grid">
    {% if not apps %}
    <p class="muted">No published apps yet.</p>
    {% endif %}
    {% for a in apps %}
    <a class="card" href="/a/{{ a.slug }}">
      <h2>{{ a.title }}</h2>
      <p class="muted">{{ a.description }}</p>
      <div class="meta"><code>{{ a.datasource }}</code> &middot; {{ a.published_at or a.updated_at }}</div>
    </a>
    {% endfor %}
  </main>
</body>
</html>""",
    autoescape=True,
)

MANAGE = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>""" + _HEAD + """
  <title>Manage Apps</title>
</head>
<body>
  <header class="topbar">
    <div>
      <div class="brand">Gabriel &amp; Co. &middot; App Catalog <span class="pill">v{{ version }}</span></div>
      <h1>Manage apps</h1>
    </div>
    <div class="userbox">
      <span class="who">{{ email }}</span>
      <a class="chip" href="/">Catalog</a>
      <a class="chip" href="/logout">Sign out</a>
    </div>
  </header>
  <main class="manage">
    {% if not apps %}
    <p class="muted">No apps yet. Create one via the <code>create_app</code> MCP tool.</p>
    {% endif %}
    <table class="table">
      <thead>
        <tr><th>Title</th><th>Slug</th><th>Source</th><th>Status</th><th>Access</th><th>By</th><th>Actions</th></tr>
      </thead>
      <tbody>
        {% for a in apps %}
        <tr data-slug="{{ a.slug }}" data-access="{{ a.access_list | join(', ') }}">
          <td><strong>{{ a.title }}</strong><div class="muted">{{ a.description }}</div></td>
          <td><code>{{ a.slug }}</code></td>
          <td>{{ a.datasource }}</td>
          <td><span class="status status-{{ a.status }}">{{ a.status }}</span></td>
          <td>
            {% if a.access_list %}
            <span class="status status-restricted" title="{{ a.access_list | join(', ') }}">Restricted ({{ a.access_list | length }})</span>
            {% else %}
            <span class="muted">Public</span>
            {% endif %}
          </td>
          <td class="muted">{{ a.created_by or '—' }}</td>
          <td class="actions">
            <a class="chip tiny" href="/a/{{ a.slug }}" target="_blank" rel="noopener">Open</a>
            <button type="button" class="chip tiny" data-action="access" data-slug="{{ a.slug }}">Access</button>
            {% if a.status == 'published' %}
            <button type="button" class="chip tiny" data-action="unpublish" data-slug="{{ a.slug }}">Unpublish</button>
            {% else %}
            <button type="button" class="chip tiny primary" data-action="publish" data-slug="{{ a.slug }}">Publish</button>
            {% endif %}
            <button type="button" class="chip tiny danger" data-action="delete" data-slug="{{ a.slug }}">Remove</button>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </main>
  <script src="/static/manage.js"></script>
</body>
</html>""",
    autoescape=True,
)


def render_catalog(email: str, can_author: bool, apps: list[dict]) -> str:
    return CATALOG.render(email=email, can_author=can_author, apps=apps,
                          version=APP_VERSION)


def render_manage(email: str, apps: list[dict]) -> str:
    return MANAGE.render(email=email, apps=apps, version=APP_VERSION)


def render_notice(title: str, message: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title><link rel='stylesheet' href='/static/appmcp.css'>"
        f"</head><body><div class='notice'><h1>{title}</h1><p>{message}</p>"
        "<p class='muted'><a href='/'>Back to catalog</a></p></div></body></html>"
    )
