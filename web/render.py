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

# Injected into every served app. Defines:
#   window.AppData.query(sql, params, opts) -> {columns, rows, truncated}
#       (auto-paginates: fetches pages until exhausted or opts.maxRows / the
#        server ceiling; backward compatible with the old {columns, rows} shape)
#   window.AppData.queryPages(sql, params, opts) -> async iterator of pages
#       (yields {columns, rows, page, page_size, has_more}; use for huge sets so
#        the app renders incrementally and never holds everything in memory)
#   window.AppData.sendEmail({to, subject, html, text}) -> {sent, recipients}
#   window.AppData.graph(path, {method, query, body}) -> Graph JSON (as the user)
#   window.AppData.census({dataset, year, get, for, in, ...}) -> {columns, rows}
#   window.AppData.s3.list(prefix, {maxKeys}) -> [{key, size, last_modified}]
#   window.AppData.s3.get(key) -> {key, size, content_type, encoding, body}
# All post to this app's own scoped, server-side endpoints (same origin).
APP_DATA_JS = """
window.AppData = {
  async _fetchPage(sql, params, page, pageSize) {
    const body = { sql: sql, params: params || [], page: page };
    if (pageSize) body.page_size = pageSize;
    const res = await fetch('/a/' + window.APP_SLUG + '/sql', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(body)
    });
    const data = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(data.error || ('Query failed (HTTP ' + res.status + ')'));
    return data;
  },
  async query(sql, params, opts) {
    opts = opts || {};
    const pageSize = opts.pageSize || 0;         // 0 => server default page size
    const maxRows = opts.maxRows || window.APP_MAX_ROWS || Infinity;
    let page = 0, columns = [], rows = [], hasMore = true, truncated = false;
    while (hasMore) {
      const data = await this._fetchPage(sql, params, page, pageSize);
      if (data.columns && data.columns.length) columns = data.columns;
      rows = rows.concat(data.rows || []);
      hasMore = !!data.has_more;
      page += 1;
      if (rows.length >= maxRows) { truncated = hasMore; rows = rows.slice(0, maxRows); break; }
    }
    return { columns: columns, rows: rows, truncated: truncated };
  },
  async *queryPages(sql, params, opts) {
    opts = opts || {};
    const pageSize = opts.pageSize || 0;
    let page = 0, hasMore = true;
    while (hasMore) {
      const data = await this._fetchPage(sql, params, page, pageSize);
      yield data;
      hasMore = !!data.has_more;
      page += 1;
    }
  },
  async sendEmail(opts) {
    opts = opts || {};
    const res = await fetch('/a/' + window.APP_SLUG + '/email', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        to: opts.to,
        subject: opts.subject,
        html: opts.html,
        text: opts.text || opts.body
      })
    });
    const data = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(data.error || ('Email failed (HTTP ' + res.status + ')'));
    return data;
  },
  async graph(path, opts) {
    opts = opts || {};
    const res = await fetch('/a/' + window.APP_SLUG + '/graph', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        method: opts.method || 'GET',
        path: path,
        query: opts.query || null,
        body: opts.body || null
      })
    });
    const data = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(data.error || ('Graph failed (HTTP ' + res.status + ')'));
    return data;
  },
  async census(opts) {
    opts = opts || {};
    const res = await fetch('/a/' + window.APP_SLUG + '/census', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        dataset: opts.dataset,
        year: opts.year,
        get: opts.get,
        for: opts['for'] || opts.forGeo || null,
        in: opts['in'] || opts.inGeo || null,
        ucgid: opts.ucgid || null,
        predicates: opts.predicates || null,
        descriptive: opts.descriptive || false
      })
    });
    const data = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(data.error || ('Census failed (HTTP ' + res.status + ')'));
    return data;
  },
  s3: {
    async _post(payload) {
      const res = await fetch('/a/' + window.APP_SLUG + '/s3', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify(payload)
      });
      const data = await res.json().catch(function () { return {}; });
      if (!res.ok) throw new Error(data.error || ('S3 failed (HTTP ' + res.status + ')'));
      return data;
    },
    async list(prefix, opts) {
      opts = opts || {};
      const data = await this._post({ op: 'list', prefix: prefix || '', max_keys: opts.maxKeys || null });
      return data.objects || [];
    },
    async get(key) {
      return await this._post({ op: 'get', key: key });
    }
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
        f'window.APP_MAX_ROWS={int(settings.max_query_rows)};'
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
      <div class="brand">App Catalog <span class="pill">v{{ version }}</span></div>
      <h1>Published apps</h1>
    </div>
    <div class="userbox">
      <span class="who">{{ email }}</span>
      {% if can_author %}<a class="chip" href="/manage">Manage</a>{% endif %}
      <a class="chip" href="/logout">Sign out</a>
    </div>
  </header>
  <main class="catalog">
    {% if not sections %}
    <p class="muted">No published apps yet.</p>
    {% else %}
    <div class="catalog-controls">
      <input id="catalog-search" type="search" placeholder="Search apps by name, description, or category…" autocomplete="off">
      <div class="chips" id="category-chips">
        <button type="button" class="chip chip-cat active" data-cat="">All</button>
        {% for c in categories %}
        <button type="button" class="chip chip-cat" data-cat="{{ c }}">{{ c }}</button>
        {% endfor %}
      </div>
    </div>
    {% for s in sections %}
    <section class="cat-section" data-category="{{ s.name }}">
      <button type="button" class="cat-head" aria-expanded="true">
        <span class="cat-caret" aria-hidden="true">&#9662;</span>
        <span class="cat-name">{{ s.name }}</span>
        <span class="cat-count">{{ s.apps | length }}</span>
      </button>
      <div class="grid cat-grid">
        {% for a in s.apps %}
        <a class="card" href="/a/{{ a.slug }}"
           data-category="{{ a.category }}"
           data-search="{{ (a.title ~ ' ' ~ a.description ~ ' ' ~ a.category) | lower }}">
          <h2>{{ a.title }}</h2>
          <p class="muted">{{ a.description }}</p>
          <div class="meta"><span class="badge">{{ a.category }}</span>{% if a.datasource %} <code>{{ a.datasource }}</code>{% endif %} &middot; {{ a.published_at or a.updated_at }}</div>
        </a>
        {% endfor %}
      </div>
    </section>
    {% endfor %}
    <p class="muted" id="no-results" hidden>No apps match your search.</p>
    {% endif %}
  </main>
  <script src="/static/catalog.js"></script>
</body>
</html>""",
    autoescape=True,
)


def _group_by_category(apps: list[dict]) -> list[dict]:
    """Group apps into ordered, non-empty sections by category.

    Canonical categories (settings order, 'Other' last) come first; any stray
    category not in the canonical list is appended alphabetically. Order within
    a section follows the input order (list_apps sorts by updated_at DESC).
    """
    by_cat: dict[str, list[dict]] = {}
    for a in apps:
        by_cat.setdefault(a.get("category") or "Other", []).append(a)
    ordered = [c for c in settings.category_list if c in by_cat]
    extras = sorted(c for c in by_cat if c not in settings.category_list)
    return [{"name": c, "apps": by_cat[c]} for c in ordered + extras]

MANAGE = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>""" + _HEAD + """
  <title>Manage Apps</title>
</head>
<body>
  <header class="topbar">
    <div>
      <div class="brand">App Catalog <span class="pill">v{{ version }}</span></div>
      <h1>Manage apps</h1>
    </div>
    <div class="userbox">
      <span class="who">{{ email }}</span>
      <a class="chip" href="/">Catalog</a>
      <a class="chip" href="/logout">Sign out</a>
    </div>
  </header>
  <main class="manage" data-categories="{{ categories | join(',') }}">
    {% if not apps %}
    <p class="muted">No apps yet. Create one via the <code>create_app</code> MCP tool.</p>
    {% endif %}
    <table class="table">
      <thead>
        <tr><th>Title</th><th>Slug</th><th>Category</th><th>Source</th><th>Status</th><th>Access</th><th>By</th><th>Actions</th></tr>
      </thead>
      <tbody>
        {% for a in apps %}
        <tr data-slug="{{ a.slug }}" data-access="{{ a.access_list | join(', ') }}" data-category="{{ a.category }}">
          <td><strong>{{ a.title }}</strong><div class="muted">{{ a.description }}</div></td>
          <td><code>{{ a.slug }}</code></td>
          <td><span class="badge">{{ a.category }}</span></td>
          <td>{{ a.datasource }}{% if a.s3_source %}{% if a.datasource %} + {% endif %}s3:{{ a.s3_source }}{% endif %}</td>
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
            <button type="button" class="chip tiny" data-action="inspect" data-slug="{{ a.slug }}">Inspect</button>
            <button type="button" class="chip tiny" data-action="category" data-slug="{{ a.slug }}">Category</button>
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
    sections = _group_by_category(apps)
    return CATALOG.render(email=email, can_author=can_author, sections=sections,
                          categories=[s["name"] for s in sections],
                          version=APP_VERSION)


def render_manage(email: str, apps: list[dict]) -> str:
    return MANAGE.render(email=email, apps=apps, version=APP_VERSION,
                         categories=settings.category_list)


def render_notice(title: str, message: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title><link rel='stylesheet' href='/static/appmcp.css'>"
        f"</head><body><div class='notice'><h1>{title}</h1><p>{message}</p>"
        "<p class='muted'><a href='/'>Back to catalog</a></p></div></body></html>"
    )
