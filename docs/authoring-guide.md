# AppMCP authoring guide (for agents building apps)

You are building a **single, self-contained HTML document** that will be served
at a permanent, authenticated URL. Follow these rules and it will work.

## The data contract

Do **not** put any database connection string, credentials, or secrets in the
HTML. To read data at runtime, call the injected client from your JavaScript:

```js
const result = await AppData.query(sql, params);
// result.columns -> ["rep", "total"]
// result.rows    -> [{ rep: "...", total: 123 }, ...]
```

- `sql` is a single **read-only SELECT**. Tables must be **schema-qualified**
  (`schema.table`) and within the datasource's allowed schemas.
- Use **positional params** `$1..$n` and pass their values as the `params`
  array (in order). Never string-concatenate user input into SQL.
- Results are **row-capped** by the server. Aggregate/limit in SQL for big tables.
- Validate your SQL with the `run_query` MCP tool while you build.

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

## Checklist before publishing

- [ ] If the app uses a datasource: data is loaded live via `AppData.query()` on
      load and on every filter change — **no result rows baked into the HTML**.
      (Static apps with no datasource are exempt.)
- [ ] Ran `check_app(html)` and cleared its errors (and reviewed warnings).
- [ ] All SQL is a single read-only SELECT with schema-qualified tables.
- [ ] User inputs flow through `params` (`$1..$n`), never string-concatenated.
- [ ] No inline `on*=` handlers; listeners attached via `addEventListener`.
- [ ] Loading + error states handled (`AppData.query` throws on failure).
- [ ] Verified each query with the `run_query` tool.
- [ ] For maps: geometry comes from this origin (`/static/us-states-10m.json`)
      or is inlined — never fetched from an external CDN; no Web Workers / Mapbox
      GL / MapLibre; libraries come from the allowed CDNs.
