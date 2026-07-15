# AppMCP authoring guide (for agents building apps)

You are building a **single, self-contained HTML document** that will be served
at a permanent, authenticated URL. Follow these rules and it will work.

## Start from the default theme

**By default, base every new app on the default dashboard theme.** Call the
`get_app_theme` MCP tool to fetch it — a CSP-clean template (sidebar +
topbar + card sections, light theme, Chart.js already wired). Replace its
`{{ PLACEHOLDER }}` markers and demo-data constants with the app's real title,
labels, and live `AppData.query(...)` data; add/remove sections as needed.

Only depart from this theme when the **user explicitly asks for a different or
custom look** (their own layout, colors, or branding). In that case, build to
their spec.

**Always include the logo.** The logo is served from this origin
at `/static/logo.png` — reference it with a plain `<img>` (allowed by the app
CSP, `img-src 'self'`):

```html
<img src="/static/logo.png" alt="Logo" style="max-width:280px;height:auto" />
```

The default theme already places it in the sidebar; if you build a custom layout,
still add the logo (e.g. in the header/sidebar) unless the user says otherwise.

## The data contract

Do **not** put any database connection string, credentials, or secrets in the
HTML. To read data at runtime, call the injected client from your JavaScript:

```js
const result = await AppData.query(sql, params);
// result.columns   -> ["rep", "total"]
// result.rows      -> [{ rep: "...", total: 123 }, ...]
// result.truncated -> true if the row ceiling was hit (there was more data)
```

- `sql` is a single **read-only SELECT**. Tables must be **schema-qualified**
  (`schema.table`) and within the datasource's allowed schemas.
- Use **positional params** `$1..$n` and pass their values as the `params`
  array (in order). Never string-concatenate user input into SQL.
- Validate your SQL with the `run_query` MCP tool while you build.

> **BigQuery sources:** the "schema" is a **dataset**, so qualify tables as
> `dataset.table` (the dataset must be in the source's allowed schemas). Write
> GoogleSQL; keep using `$1..$n` params. Everything else — `AppData.query()`,
> pagination, `queryPages()` — works identically. Prefer aggregating in SQL:
> each query is capped by a bytes-scanned ceiling, so `SELECT *` on a huge table
> may be rejected — add `WHERE`/`GROUP BY`/`LIMIT` and select only needed columns.

### Large result sets & pagination

You don't write pagination into your SQL — the server paginates **any** SELECT
for you. `AppData.query()` transparently fetches page after page and concatenates
them, so a plain `SELECT ...` returns everything up to a safety **ceiling** (the
server's `MAX_QUERY_ROWS`, default 50,000). If that ceiling is hit,
`result.truncated` is `true` and you should tell the user the view is partial.

- **For dashboards, aggregate in SQL** (`GROUP BY`, `SUM`, `COUNT`, `LIMIT`) —
  don't pull tens of thousands of raw rows into the browser just to reduce them
  client-side. This is faster and avoids hitting the ceiling.
- **For genuinely large sets** (big tables, exports, virtualized grids), use the
  streaming iterator so you render page-by-page and never hold everything at
  once:

```js
for await (const page of AppData.queryPages(sql, params)) {
  // page.rows is one chunk (page.page, page.page_size, page.has_more available)
  appendRows(page.rows);           // render incrementally
}
```

- Pagination uses `LIMIT/OFFSET` under the hood. For **stable** pages across
  fetches, add an `ORDER BY` on a unique/stable column (e.g. an id or timestamp);
  without one, row order between pages isn't guaranteed. (On SQL Server, keep the
  `ORDER BY` out of the query — the server adds paging itself.)
- You can tune per call: `AppData.query(sql, params, { maxRows: 5000 })` to cap
  the auto-fetch, or `{ pageSize: 500 }` to change the chunk size.

`AppData` is injected by the server at serve time — you do **not** define it, and
you do **not** know or need the datasource name inside the app (when a datasource
is set, the server pins the app to the one chosen in `create_app`). If the app
has no datasource, `AppData.query()` is unavailable — don't call it.

### Datasource is optional; but if you use one, keep data live

A `datasource` is **optional**. Omit it in `create_app` to build a purely static
page (docs, a calculator, a form) — those have no live data and no restrictions
on inline content.

When you **do** bind a datasource, the app's data must be fetched **live** with
`AppData.query()` every time the app loads and every time a filter changes. Do
**not** paste query results into the HTML as literal arrays — e.g.
`const rows = [{...}, {...}, ...]`. Baked data goes stale, breaks filtering, and
bypasses per-user access/attribution.

For datasource-bound apps, `create_app`/`update_app` run a static check and
**reject** HTML that contains a large inline array of row objects (baked
results). Run `check_app(html)` first to see the errors/warnings and fix them.
The only legitimate inline data is small **reference** data (lookup maps) or map
**geometry** (TopoJSON) — for those rare cases pass `allow_inline_data=true`.

## Categorize your app

Pass a `category` to `create_app` so the app lands in the right catalog section
(the catalog groups apps into collapsible sections by category instead of one
long list). Call `list_categories` for the current canonical list — defaults are
**Sales, Inventory, Customers, Rewards, Ecommerce, Finance, Operations,
Marketing, Reports, Tools, Demo**, with **Other** as the fallback. Pick the closest
match; an unrecognized value is normalized to "Other". You can change it later
with `update_app(category=...)` (or an author can edit it in `/manage`).

## Who can view the app

By default a published app is visible to any signed-in org user. To restrict it,
pass `access_list` (a list of emails) to `create_app`/`update_app`, or call
`set_app_access(slug, [...])` later. An empty list means public; when set, only
those users can open it (admins and the creator always retain access). This is a
publish-time setting, not something you handle in the HTML.

## Hard constraints (Content-Security-Policy)

The page is served under a strict CSP. Design around it:

1. **No inline event handlers.** `onclick="..."`, `onchange="..."`, etc. are
   blocked. Attach listeners in a `<script>` block with `addEventListener`.
2. **No `eval` / `new Function`.**
3. **Scripts** run from your inline `<script>` blocks or these CDNs only:
   `cdn.jsdelivr.net`, `cdnjs.cloudflare.com`, `unpkg.com`. Native HTML controls
   (`<select>`, `<input type="date">`) need no library.
4. **Network:** the page may only call back to its own origin (i.e. `AppData`).
   It cannot fetch arbitrary external URLs.
5. Inline `<style>` and `style="..."` are allowed.

## Filters, dropdowns, and date pickers

Filters are just normal HTML inputs plus a listener that re-queries. Two patterns:

- **Server-side params (preferred for large data):** put the filter values into
  the `params` array and re-run `AppData.query(...)` with a `WHERE` clause using
  `$1..$n`. Re-render with the new rows.
- **Client-side (small data):** query once, keep the rows in a variable, and
  filter/sort them in JavaScript on change.

Use native controls — `<select>` for a dropdown, `<input type="date">` for a
calendar picker — so you need no external library. Wire them with
`addEventListener` (never inline `onchange`).

## Minimal working template

A complete app with a dropdown + date-range filter that re-queries on demand:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sales by rep</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 24px; color: #1a1f2e; }
    .filters { display: flex; gap: 12px; align-items: end; flex-wrap: wrap; margin-bottom: 16px; }
    label { display: flex; flex-direction: column; font-size: .8rem; color: #5b6477; gap: 4px; }
    input, select, button { padding: 7px 10px; font: inherit; }
    table { border-collapse: collapse; width: 100%; }
    th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eee; }
    .status { color: #8a93a6; font-size: .85rem; }
  </style>
</head>
<body>
  <h1>Sales by rep</h1>

  <div class="filters">
    <label>From<input type="date" id="from" value="2025-01-01"></label>
    <label>To<input type="date" id="to" value="2025-12-31"></label>
    <label>Region
      <select id="region">
        <option value="">All</option>
        <option value="East">East</option>
        <option value="West">West</option>
      </select>
    </label>
    <button id="run" type="button">Run</button>
    <span class="status" id="status"></span>
  </div>

  <table>
    <thead><tr><th>Rep</th><th>Region</th><th>Total</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>

  <script>
    var $ = function (id) { return document.getElementById(id); };

    function esc(v) {
      return String(v == null ? "" : v)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    async function load() {
      $("status").textContent = "Loading\u2026";
      // $3 = region; '' means "all" via the OR ($3 = '') trick.
      var sql =
        "SELECT rep, region, SUM(total_sales) AS total " +
        "FROM sales.invoice " +
        "WHERE document_date BETWEEN $1 AND $2 " +
        "AND ($3 = '' OR region = $3) " +
        "GROUP BY rep, region ORDER BY total DESC";
      var params = [$("from").value, $("to").value, $("region").value];
      try {
        var data = await AppData.query(sql, params);
        $("rows").innerHTML = data.rows.map(function (r) {
          return "<tr><td>" + esc(r.rep) + "</td><td>" + esc(r.region) +
                 "</td><td>" + esc(r.total) + "</td></tr>";
        }).join("");
        $("status").textContent = data.rows.length + " rows";
      } catch (err) {
        $("status").textContent = err.message || "Query failed.";
      }
    }

    $("run").addEventListener("click", load);
    $("region").addEventListener("change", load);
    load(); // initial render
  </script>
</body>
</html>
```

## Maps & geography (e.g. plotting data on a US map)

Maps are possible, but the CSP shapes how you do it — get this wrong and the map
silently fails:

- **Libraries load from the allowed CDNs.** D3 (`d3`), `topojson-client`,
  Leaflet, Plotly via `cdn.jsdelivr.net` / `cdnjs.cloudflare.com` / `unpkg.com`
  are fine (they're `script-src`).
- **Fetch geometry from THIS origin, not an external CDN.** `connect-src 'self'`
  blocks `d3.json("https://.../states-10m.json")`, but it **allows same-origin
  requests** — the same rule that lets `AppData.query` work. The server hosts the
  US states TopoJSON for you at **`/static/us-states-10m.json`**, so
  `d3.json("/static/us-states-10m.json")` works and you do **not** inline or
  paste any geometry. (Inlining a ~100 KB topology also works but is brittle to
  author — prefer the hosted file.)
- **No Web Workers**, so **Mapbox GL JS / MapLibre GL do not work** here
  (`default-src 'none'`). Use SVG-based rendering (D3) instead.
- Leaflet with raster tiles can render (tiles are `<img>`, allowed by
  `img-src https:`), but for a data choropleth prefer the D3 approach below.

**Hosted geometry files** (all under `/static/`, fetch with `d3.json`):

| File | `topojson.feature(topo, topo.objects.X)` | Join id | Notes |
|------|------------------------------------------|---------|-------|
| `us-states-10m.json` | `states` | 2-digit state FIPS | also has `nation`; use `geoAlbersUsa()` |
| `us-counties-10m.json` | `counties` | 5-digit county FIPS (state+county) | also has `states`, `nation`; ~820 KB |
| `world-countries-110m.json` | `countries` | numeric ISO 3166-1 (`d.id`); name in `d.properties.name` | low detail, fast; also has `land` |
| `world-countries-50m.json` | `countries` | numeric ISO 3166-1; name in `d.properties.name` | medium detail; ~740 KB |

Pick the coarsest file that looks good (smaller = faster). For world maps use a
world projection (e.g. `d3.geoNaturalEarth1()` / `d3.geoMercator()`), not
`geoAlbersUsa()`. Join your data on the id column shown above.

**Recommended pattern — D3 choropleth:** aggregate to the geographic key in SQL
(return `{state, value}`), fetch the hosted TopoJSON from `/static/us-states-10m.json`,
and join on the state's FIPS id. `d3.geoAlbersUsa()` handles the Alaska/Hawaii
insets automatically. (The hosted file keys states under `objects.states` by
2-digit FIPS id; it also contains PR/territories, which `geoAlbersUsa` does not
place — that's expected.)

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sales by state</title>
  <script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
  <script src="https://cdn.jsdelivr.net/npm/topojson-client@3"></script>
  <style>
    body { font-family: system-ui, sans-serif; margin: 24px; color: #1a1f2e; }
    svg { width: 100%; height: auto; }
    .status { color: #8a93a6; font-size: .85rem; }
    path { stroke: #fff; stroke-width: .5; }
  </style>
</head>
<body>
  <h1>Sales by state</h1>
  <div id="map"></div>
  <span class="status" id="status"></span>

  <script>
    var $ = function (id) { return document.getElementById(id); };

    // us-atlas keys states by numeric FIPS id; map your 2-letter codes to it.
    var USPS_TO_FIPS = {
      AL:"01", AK:"02", AZ:"04", AR:"05", CA:"06", CO:"08", CT:"09", DE:"10",
      DC:"11", FL:"12", GA:"13", HI:"15", ID:"16", IL:"17", IN:"18", IA:"19",
      KS:"20", KY:"21", LA:"22", ME:"23", MD:"24", MA:"25", MI:"26", MN:"27",
      MS:"28", MO:"29", MT:"30", NE:"31", NV:"32", NH:"33", NJ:"34", NM:"35",
      NY:"36", NC:"37", ND:"38", OH:"39", OK:"40", OR:"41", PA:"42", RI:"44",
      SC:"45", SD:"46", TN:"47", TX:"48", UT:"49", VT:"50", VA:"51", WA:"53",
      WV:"54", WI:"55", WY:"56"
    };

    async function render() {
      $("status").textContent = "Loading\u2026";
      try {
        // Fetch geometry from THIS origin (allowed by connect-src 'self') and the
        // data from the SQL proxy, in parallel.
        var results = await Promise.all([
          d3.json("/static/us-states-10m.json"),
          AppData.query(
            "SELECT ship_state AS state, SUM(total_sales) AS value " +
            "FROM sales.invoice WHERE ship_country = $1 " +
            "GROUP BY ship_state", ["US"]
          )
        ]);
        var topo = results[0], data = results[1];

        var byFips = {};
        data.rows.forEach(function (r) {
          var fips = USPS_TO_FIPS[String(r.state || "").toUpperCase()];
          if (fips) byFips[fips] = Number(r.value) || 0;
        });

        var states = topojson.feature(topo, topo.objects.states);
        var color = d3.scaleSequential(d3.interpolateBlues)
          .domain([0, d3.max(Object.values(byFips)) || 1]);

        var width = 960, height = 600;
        var path = d3.geoPath(d3.geoAlbersUsa().fitSize([width, height], states));
        var svg = d3.select("#map").append("svg")
          .attr("viewBox", "0 0 " + width + " " + height);

        svg.append("g").selectAll("path")
          .data(states.features).enter().append("path")
          .attr("d", path)
          .attr("fill", function (d) {
            var v = byFips[d.id];
            return v == null ? "#eee" : color(v);
          })
          .append("title")
          .text(function (d) {
            var v = byFips[d.id], name = d.properties && d.properties.name;
            return name + ": " + (v == null ? "no data" : v);
          });

        $("status").textContent = data.rows.length + " states";
      } catch (err) {
        $("status").textContent = err.message || "Failed to render map.";
      }
    }
    render();
  </script>
</body>
</html>
```

For a non-US or point map the same rules apply: fetch geometry from this origin
if the server hosts it, otherwise inline a small projection + geometry, or plot
bubbles at an inline lat/long lookup, and color/size by values from `AppData.query`.

## Sending email

Apps can send email through a server-side SendGrid integration via
`AppData.sendEmail(...)`. Like `AppData.query`, this posts to the app's own
origin (`/a/{slug}/email`) — the app never holds credentials or sets the sender.

```js
const res = await AppData.sendEmail({
  to: "jane.doe@example.com",            // or an array of addresses
  subject: "Weekly rewards summary",
  html: "<h1>Summary</h1><p>…</p>",       // html and/or text
  text: "Summary…"
});
// res -> { ok: true, sent: 1, recipients: [...] }
```

Constraints (enforced server-side):

- **Internal recipients only.** Every address must be on the org domain
  (`@example.com`); external recipients are rejected. This is an anti-relay
  guard, not something you can override from the app.
- **The From address is fixed** by the server; replies go to the signed-in user.
- **Provide `html` and/or `text`,** plus a non-empty `subject`. Build the body
  from data you already fetched with `AppData.query` (escape user/data values).
- **Caps apply:** a max number of recipients per send and a per-app per-minute
  rate limit; `AppData.sendEmail` throws on any violation, so handle errors.
- If email isn't configured on the server, the call fails with a clear error —
  treat sending as best-effort and surface failures in the UI.

### Two ways to send mail — which to use

There are two send paths and they do **not** conflict; pick based on who the mail
should come from:

- **`AppData.sendEmail(...)` (SendGrid) — use for system/notification-style mail.**
  Sent from a fixed service address (replies go to the signed-in user),
  **internal recipients only**, and it does **not** appear in anyone's mailbox
  Sent Items. No Graph sign-in needed. Best for "the app is notifying people"
  (alerts, digests, reports) to `@example.com` addresses.
- **`AppData.graph("/me/sendMail", ...)` (Microsoft Graph) — use for "send as me".**
  Sent from the **signed-in user's own mailbox** (lands in their Sent Items,
  threads naturally) and **may go to external recipients**. Requires the user to
  have signed in with Graph enabled. Best when the message should genuinely come
  from the person using the app, or must reach someone outside the org.

Rule of thumb: notifications *from the app* → `sendEmail`; a message *from the
user* (or to an external address) → Graph `/me/sendMail`.

## Microsoft Graph (the signed-in user's mail & calendar)

Apps can query Microsoft Graph **as the person viewing the app** through
`AppData.graph(path, opts)`. Like the other helpers it posts to the app's own
origin (`/a/{slug}/graph`); the server attaches the viewer's delegated token —
the app never sees a token, and every call is automatically scoped to that
user's own data (`/me`).

```js
// Read the 10 most recent inbox messages
const mail = await AppData.graph("/me/messages", {
  query: { $top: 10, $select: "subject,from,receivedDateTime", $orderby: "receivedDateTime desc" }
});
mail.value.forEach(m => console.log(m.subject));

// Read today's calendar events
const events = await AppData.graph("/me/calendarView", {
  query: { startDateTime: "2026-07-08T00:00:00", endDateTime: "2026-07-08T23:59:59" }
});

// Send mail as the signed-in user
await AppData.graph("/me/sendMail", {
  method: "POST",
  body: { message: {
    subject: "Hi",
    body: { contentType: "Text", content: "Sent from an app." },
    toRecipients: [{ emailAddress: { address: "jane.doe@example.com" } }]
  } }
});

// Create a calendar event
await AppData.graph("/me/events", {
  method: "POST",
  body: { subject: "Sync", start: { dateTime: "2026-07-09T15:00:00", timeZone: "Eastern Standard Time" },
          end: { dateTime: "2026-07-09T15:30:00", timeZone: "Eastern Standard Time" } }
});

// List OneDrive files in the root folder
const files = await AppData.graph("/me/drive/root/children", {
  query: { $select: "name,size,lastModifiedDateTime,webUrl", $top: 25 }
});
files.value.forEach(f => console.log(f.name, f.size));

// List recent Teams chats, then read messages in one
const chats = await AppData.graph("/me/chats", { query: { $top: 20 } });
const msgs = await AppData.graph(`/me/chats/${chats.value[0].id}/messages`, {
  query: { $top: 20 }
});
```

`opts`: `method` (default `GET`), `query` (object → OData query params like
`$select`, `$top`, `$filter`, `$orderby`), and `body` (object, for writes). The
call returns the parsed Graph JSON (e.g. `{ value: [...] }` for collections).

Constraints (enforced server-side):

- **`/me` only.** Paths must target the current user; you cannot read other
  users or the directory at large. The path is matched against an allowlist.
- **Allowed operations:** read `/me` profile, `/me/messages`, `/me/mailFolders`,
  `/me/events`, `/me/calendar(s)`, `/me/calendarView`, `/me/contacts`,
  `/me/people`, `/me/manager`, `/me/directReports`, `/me/drive(s)` (OneDrive
  files & folders), `/me/chats` (Teams chats & messages); **write:**
  `POST /me/sendMail` and create/update/delete your own calendar events
  (`/me/events`). Anything else is rejected — don't try to move/delete mail,
  write to files/chats, or read `/users/{other}`.
- **Narrow your reads.** Use `$select` and `$top`; oversized responses are
  rejected. A per-session per-minute rate limit applies.
- **Drive & chats are read-only and JSON-only.** You can list/inspect OneDrive
  items and read Teams chats/messages, but you can't upload/modify files, post
  chat messages, or download raw binary file content (`/content`) — the proxy
  returns JSON only and caps the response size. Use `webUrl` to link a user to a
  file rather than streaming it through the app.
- **This is the viewer's personal mailbox/calendar.** Only request what the app
  genuinely needs, escape any values you render, and handle errors (the promise
  throws on any rejection).
- If Graph isn't configured, or the user's Graph session has expired, the call
  fails with a clear error asking them to sign out and back in.
- **Transient errors are retried automatically.** Exchange occasionally returns a
  503 `ErrorInternalServerTransientError` (e.g. "Cannot query rows in a table")
  on otherwise-valid mailbox queries; the server retries a few times with backoff
  before surfacing it. Keep queries simple (avoid combining `$filter` with an
  `$orderby` on a different property — sort or filter client-side instead).

## S3 files (buckets as a data source)

Apps can read files from an S3 bucket through `AppData.s3`. Like the other
helpers it posts to the app's own origin (`/a/{slug}/s3`); the server holds the
AWS key/secret and the app never sees them. This is a **separate binding** from a
SQL `datasource` — an app can use a SQL source, an S3 source, both, or neither.
Bind one at create time with `create_app(..., s3_source="reports")`; discover the
available names with `list_s3_sources` and browse keys with `list_s3_objects`.

```js
// List objects under a folder (keys are RELATIVE to the source's own prefix)
const files = await AppData.s3.list("2026/", { maxKeys: 200 });
files.forEach(f => console.log(f.key, f.size, f.last_modified));

// Fetch one object's contents
const obj = await AppData.s3.get("2026/q2-summary.csv");
if (obj.encoding === "text") {
  const rows = obj.body.trim().split("\n").map(line => line.split(","));
  // ...render rows...
} else {
  // Binary objects come back base64-encoded in obj.body (obj.encoding === "base64").
}
```

`AppData.s3.list(prefix, opts)` returns an array of `{key, size, last_modified}`;
`AppData.s3.get(key)` returns `{key, size, content_type, encoding, body}` where
`encoding` is `"text"` (utf-8) or `"base64"` (binary).

Constraints (enforced server-side):

- **Read-only.** Only list + get; there is no upload/write/delete.
- **Confined to the source.** Each source is pinned to one bucket and an optional
  key prefix. Keys you pass are **relative** to that prefix — you cannot escape it
  (no `..`, no absolute keys, no other bucket).
- **Size cap.** A single `get` is capped (default 5 MiB); larger objects are
  rejected — split big files or store pre-aggregated extracts. `list` returns a
  capped number of keys per call.
- **Parse client-side.** The server returns raw bytes; parse CSV/JSON/text in the
  app. A per-session per-minute rate limit applies, and `AppData.s3` throws on any
  error, so handle failures in the UI.
- If S3 isn't configured on the server, or the app has no `s3_source` bound, the
  call fails with a clear error.

## U.S. Census Bureau data (population, demographics, economy)

Apps can pull official statistics from the **U.S. Census Bureau Data API** through
`AppData.census(opts)`. Like the other helpers it posts to the app's own origin
(`/a/{slug}/census`); the server holds the Census API key and the app never sees
it. This is a **shared capability** (like email) — it needs no per-app binding and
works in any app. Validate a query first with the `census_query` MCP tool.

```js
// Total population per state (ACS 1-year, 2022)
const res = await AppData.census({
  dataset: "acs/acs1",
  year: 2022,
  get: { variables: ["NAME", "B01001_001E"] },  // or get: ["NAME","B01001_001E"]
  for: "state:*"
});
// res.columns -> ["NAME","B01001_001E","state"]
// res.rows    -> [{ NAME: "New York", B01001_001E: "19677151", state: "36" }, ...]

// A whole variable group, restricted to counties in New York (state FIPS 36)
const counties = await AppData.census({
  dataset: "acs/acs5",
  year: 2022,
  get: { group: "B19013" },        // median household income
  for: "county:*",
  in: "state:36"
});

// Decennial redistricting counts for one place
const place = await AppData.census({
  dataset: "dec/pl", year: 2020,
  get: ["NAME", "P1_001N"], ucgid: "1600000US3651000"
});
```

`opts`: `dataset` (path like `acs/acs1`, `acs/acs5/subject`, `dec/pl`), `year`
(4-digit vintage; omit only for `timeseries/*` datasets), `get` (a string, an
array of variables, or `{variables, group}`), `for`/`in` (geography, e.g.
`"state:*"`, `"county:*"` + `"state:36"`), `ucgid`, `predicates` (extra filters),
and `descriptive` (add variable labels). It returns `{columns, rows, count}` with
each row a plain object keyed by the returned columns (values are **strings** — the
Census API returns everything as text, so `Number(...)` before charting).

Finding the right dataset/variables/geography: the sibling **USCensusMCP** server
exposes `list-datasets`, `search-data-tables`, `fetch-dataset-geography`, and
`resolve-geography-fips` to discover dataset ids, variable codes, and FIPS codes.

Constraints (enforced server-side):

- **Read-only public data.** No key or credentials in the app; the server adds them.
- **Narrow your request.** Responses are size-capped — prefer specific variables
  over huge groups across `for=*:*`, and filter with `for`/`in`. A per-session
  per-minute rate limit applies.
- **Values are strings.** Cast to `Number` before math/plotting; missing values may
  come back as sentinels (e.g. negative codes) — validate before charting.
- `AppData.census` **throws** on any error (bad dataset/variable, geography not
  supported by the dataset, rate limit), so handle failures in the UI.
- If Census isn't configured on the server, the call fails with a clear error.

## Checklist before publishing

- [ ] Started from the default theme (`get_app_theme`) unless the user asked for a
      custom look, and the logo (`/static/logo.png`) is included.
- [ ] If the app uses a datasource: data is loaded live via `AppData.query()` on
      load and on every filter change — **no result rows baked into the HTML**.
      (Static apps with no datasource are exempt.)
- [ ] Ran `check_app(html)` and cleared its errors (and reviewed warnings).
- [ ] All SQL is a single read-only SELECT with schema-qualified tables.
- [ ] User inputs flow through `params` (`$1..$n`), never string-concatenated.
- [ ] No inline `on*=` handlers; listeners attached via `addEventListener`.
- [ ] Loading + error states handled (`AppData.query`/`AppData.sendEmail` throw
      on failure).
- [ ] If sending email: recipients are internal (`@example.com`), subject +
      body set, and data values are escaped in the message.
- [ ] If using Graph: calls are `/me`-scoped and on the allowlist, reads use
      `$select`/`$top`, and errors are handled.
- [ ] If using S3: an `s3_source` is bound, keys are relative to the source
      prefix, objects are within the size cap, and errors are handled.
- [ ] If using Census: dataset/year/variables are valid (checked with
      `census_query`), the request is narrowed with `for`/`in`, values are cast
      from strings with `Number(...)`, and errors are handled.
- [ ] Verified each query with the `run_query` tool.
- [ ] For maps: geometry comes from this origin (`/static/us-states-10m.json`)
      or is inlined — never fetched from an external CDN; no Web Workers / Mapbox
      GL / MapLibre; libraries come from the allowed CDNs.
