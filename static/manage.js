// Manage page: publish / unpublish / remove apps + edit access list.
(function () {
  async function post(path, body) {
    var opts = { method: "POST", credentials: "same-origin" };
    if (body !== undefined) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    var res = await fetch(path, opts);
    var data = await res.json().catch(function () { return {}; });
    return { ok: res.ok && data.ok, data: data };
  }

  // Build an empty modal shell with a title + Close button; returns the pieces
  // callers fill in. Reused by Extract SQL, diagnostics, and the query runner.
  function makeModal(titleText) {
    var overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    var modal = document.createElement("div");
    modal.className = "modal";
    var head = document.createElement("div");
    head.className = "modal-head";
    var title = document.createElement("h2");
    title.textContent = titleText;
    var close = document.createElement("button");
    close.type = "button";
    close.className = "chip tiny";
    close.textContent = "Close";
    head.appendChild(title);
    head.appendChild(close);
    modal.appendChild(head);

    function dismiss() { if (overlay.parentNode) document.body.removeChild(overlay); }
    close.addEventListener("click", dismiss);
    overlay.addEventListener("click", function (e) { if (e.target === overlay) dismiss(); });
    overlay.appendChild(modal);
    return { overlay: overlay, modal: modal, dismiss: dismiss };
  }

  function renderResults(out, data) {
    out.textContent = "";
    var cols = data.columns || [];
    var rows = data.rows || [];
    var meta = document.createElement("div");
    meta.className = "muted";
    meta.textContent = rows.length + " row" + (rows.length === 1 ? "" : "s") +
      (data.has_more ? " (first page; more available)" : "");
    out.appendChild(meta);
    if (!rows.length) return;
    var table = document.createElement("table");
    table.className = "table";
    var thead = document.createElement("thead");
    var trh = document.createElement("tr");
    cols.forEach(function (c) {
      var th = document.createElement("th");
      th.textContent = c;
      trh.appendChild(th);
    });
    thead.appendChild(trh);
    table.appendChild(thead);
    var tb = document.createElement("tbody");
    rows.forEach(function (row) {
      var tr = document.createElement("tr");
      cols.forEach(function (c) {
        var td = document.createElement("td");
        var v = row[c];
        td.textContent = (v === null || v === undefined) ? "" : String(v);
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
    table.appendChild(tb);
    out.appendChild(table);
  }

  function showQueryModal(slug) {
    var m = makeModal("Query runner · " + slug);
    var hint = document.createElement("div");
    hint.className = "muted";
    hint.textContent = "Read-only SELECT against this app's datasource. " +
      "Use $1..$n placeholders and supply a JSON array of params below.";
    m.modal.appendChild(hint);

    var ta = document.createElement("textarea");
    ta.className = "debug-input";
    ta.rows = 6;
    ta.placeholder = "SELECT ... FROM schema.table WHERE ...";
    m.modal.appendChild(ta);

    var pin = document.createElement("input");
    pin.className = "debug-input";
    pin.type = "text";
    pin.placeholder = 'params JSON (optional), e.g. ["US", 2025]';
    m.modal.appendChild(pin);

    var runBtn = document.createElement("button");
    runBtn.type = "button";
    runBtn.className = "chip tiny primary";
    runBtn.textContent = "Run";
    m.modal.appendChild(runBtn);

    var out = document.createElement("div");
    out.className = "debug-out";
    m.modal.appendChild(out);

    runBtn.addEventListener("click", async function () {
      var sql = ta.value.trim();
      if (!sql) { out.textContent = "Enter a SELECT statement."; return; }
      var params = [];
      if (pin.value.trim()) {
        try { params = JSON.parse(pin.value); }
        catch (e) { out.textContent = "params must be a valid JSON array."; return; }
        if (!Array.isArray(params)) { out.textContent = "params must be a JSON array."; return; }
      }
      runBtn.disabled = true;
      out.textContent = "Running…";
      var r = await post("/manage/" + encodeURIComponent(slug) + "/run-query",
        { sql: sql, params: params });
      runBtn.disabled = false;
      if (!r.ok) { out.textContent = (r.data && r.data.error) || "Query failed."; return; }
      renderResults(out, r.data);
    });

    document.body.appendChild(m.overlay);
    ta.focus();
  }

  async function runDiagnostics(btn, slug) {
    btn.disabled = true;
    var res = await fetch("/manage/" + encodeURIComponent(slug) + "/diagnostics",
      { credentials: "same-origin" });
    var d = await res.json().catch(function () { return {}; });
    btn.disabled = false;
    if (!res.ok || !d.ok) {
      alert((d && d.error) || "Could not run diagnostics.");
      return;
    }
    var m = makeModal("Diagnostics · " + slug);
    var body = document.createElement("div");
    body.className = "diag";

    function row(label, value) {
      var r = document.createElement("div");
      r.className = "diag-row";
      var k = document.createElement("span");
      k.className = "diag-k";
      k.textContent = label;
      var v = document.createElement("span");
      v.className = "diag-v";
      v.textContent = value;
      r.appendChild(k);
      r.appendChild(v);
      body.appendChild(r);
    }

    row("Status", d.status || "—");
    row("Datasource", d.datasource || "(none)");
    if (d.s3_source) row("S3 source", d.s3_source);
    row("Category", d.category || "—");
    row("Icon", d.icon || "—");
    row("Capabilities used",
      (d.capabilities_used && d.capabilities_used.length)
        ? d.capabilities_used.join(", ") : "none detected");
    row("HTML size", (d.html_bytes || 0).toLocaleString() + " bytes");
    row("Created", (d.created_at || "—") + (d.created_by ? (" by " + d.created_by) : ""));
    row("Updated", d.updated_at || "—");
    row("Published", d.published_at
      ? (d.published_at + (d.published_by ? (" by " + d.published_by) : "")) : "—");
    row("Access", (d.access_list && d.access_list.length)
      ? ("Restricted (" + d.access_list.join(", ") + ")") : "Public");
    if (d.datasource_ping) {
      row("Datasource ping",
        (d.datasource_ping.ok ? "OK" : "FAILED") + " — " + (d.datasource_ping.detail || ""));
    }

    var lint = d.lint || {};
    row("Static check", lint.ok ? "OK (no errors)" : "Has errors");
    (lint.errors || []).forEach(function (e) {
      var p = document.createElement("div");
      p.className = "diag-issue err";
      p.textContent = "ERROR: " + e;
      body.appendChild(p);
    });
    (lint.warnings || []).forEach(function (w) {
      var p = document.createElement("div");
      p.className = "diag-issue warn";
      p.textContent = "WARN: " + w;
      body.appendChild(p);
    });

    m.modal.appendChild(body);
    document.body.appendChild(m.overlay);
  }

  function showSqlModal(slug, datasource, queries) {
    var m = makeModal("Extracted SQL · " + slug);
    var overlay = m.overlay;
    var modal = m.modal;

    var sub = document.createElement("div");
    sub.className = "muted";
    sub.textContent = datasource
      ? ("Datasource: " + datasource + " · " + queries.length + " quer" + (queries.length === 1 ? "y" : "ies") + " found")
      : "This app has no datasource.";
    modal.appendChild(sub);

    if (!queries.length) {
      var none = document.createElement("p");
      none.className = "muted";
      none.textContent = "No SQL found. The app may use no datasource or build SQL dynamically.";
      modal.appendChild(none);
    } else {
      queries.forEach(function (q) {
        var pre = document.createElement("pre");
        pre.className = "sql-block";
        pre.textContent = q; // textContent avoids any HTML injection
        modal.appendChild(pre);
      });
    }

    document.body.appendChild(overlay);
  }

  async function extractSql(btn, slug) {
    btn.disabled = true;
    var res = await fetch("/manage/" + encodeURIComponent(slug) + "/sql", { credentials: "same-origin" });
    var data = await res.json().catch(function () { return {}; });
    btn.disabled = false;
    if (!res.ok || !data.ok) {
      alert((data && data.error) || "Could not extract SQL from app.");
      return;
    }
    showSqlModal(slug, data.datasource, data.queries || []);
  }

  function showSourceModal(slug, source) {
    var m = makeModal("Source code · " + slug);
    var sub = document.createElement("div");
    sub.className = "muted";
    sub.textContent = (source ? source.length.toLocaleString() : "0") +
      " chars · stored source (as authored, before AppData/CSP injection)";
    m.modal.appendChild(sub);

    var pre = document.createElement("pre");
    pre.className = "sql-block source-block";
    pre.textContent = source || "(empty)"; // textContent avoids any HTML injection
    m.modal.appendChild(pre);

    document.body.appendChild(m.overlay);
  }

  async function inspectSource(btn, slug) {
    btn.disabled = true;
    var res = await fetch("/manage/" + encodeURIComponent(slug) + "/source", { credentials: "same-origin" });
    var data = await res.json().catch(function () { return {}; });
    btn.disabled = false;
    if (!res.ok || !data.ok) {
      alert((data && data.error) || "Could not load source code.");
      return;
    }
    showSourceModal(slug, data.html || "");
  }

  async function editAccess(btn, slug) {
    var row = btn.closest("tr");
    var current = (row && row.getAttribute("data-access")) || "";
    var msg =
      "Access for '" + slug + "'.\n\n" +
      "Leave blank = public (any signed-in user).\n" +
      "Or enter a comma-separated list of emails allowed to open it\n" +
      "(admins and the creator always have access).";
    var value = window.prompt(msg, current);
    if (value === null) return; // cancelled
    btn.disabled = true;
    var r = await post("/manage/" + encodeURIComponent(slug) + "/access", { emails: value });
    btn.disabled = false;
    if (!r.ok) {
      alert((r.data && r.data.error) || "Could not update access.");
      return;
    }
    window.location.reload();
  }

  async function transferOwner(btn, slug) {
    var row = btn.closest("tr");
    var current = (row && row.getAttribute("data-owner")) || "";
    var msg =
      "Transfer ownership of '" + slug + "'.\n\n" +
      "Enter the new owner's email. They must already be an author or admin so\n" +
      "they can manage/edit it. You'll lose owner access unless you're an admin.";
    var value = window.prompt(msg, "");
    if (value === null) return; // cancelled
    value = value.trim();
    if (!value || value.toLowerCase() === current.toLowerCase()) return;
    if (!confirm("Transfer '" + slug + "' to " + value + "?")) return;
    btn.disabled = true;
    var r = await post("/manage/" + encodeURIComponent(slug) + "/transfer", { new_owner: value });
    btn.disabled = false;
    if (!r.ok) {
      alert((r.data && r.data.error) || "Could not transfer ownership.");
      return;
    }
    window.location.reload();
  }

  function editIcon(btn, slug) {
    var row = btn.closest("tr");
    var current = (row && row.getAttribute("data-icon")) || "";
    var main = document.querySelector(".manage");
    var names = ((main && main.getAttribute("data-icons")) || "")
      .split(",").filter(function (n) { return n; });

    var m = makeModal("Icon · " + slug);
    var hint = document.createElement("div");
    hint.className = "muted";
    hint.textContent = "Pick an icon for the catalog card, or choose Auto to " +
      "let the app's category decide.";
    m.modal.appendChild(hint);

    var grid = document.createElement("div");
    grid.className = "icon-picker";
    m.modal.appendChild(grid);

    async function choose(name) {
      var r = await post("/manage/" + encodeURIComponent(slug) + "/icon", { icon: name });
      if (!r.ok) { alert((r.data && r.data.error) || "Could not set icon."); return; }
      window.location.reload();
    }

    function tile(name, label, imgSrc) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "icon-opt" + (name === current ? " active" : "");
      b.title = label;
      if (imgSrc) {
        var img = document.createElement("img");
        img.src = imgSrc;
        img.alt = "";
        img.width = 24;
        img.height = 24;
        b.appendChild(img);
      }
      var cap = document.createElement("span");
      cap.textContent = label;
      b.appendChild(cap);
      b.addEventListener("click", function () { choose(name); });
      return b;
    }

    grid.appendChild(tile("", "Auto", null));
    names.forEach(function (n) {
      grid.appendChild(tile(n, n, "/static/icons/" + encodeURIComponent(n) + ".svg"));
    });

    document.body.appendChild(m.overlay);
  }

  async function editCategory(btn, slug) {
    var row = btn.closest("tr");
    var current = (row && row.getAttribute("data-category")) || "";
    var main = document.querySelector(".manage");
    var cats = ((main && main.getAttribute("data-categories")) || "").split(",");
    var msg =
      "Category for '" + slug + "'.\n\n" +
      "Choose one of: " + cats.join(", ") + "\n" +
      "(anything else falls back to 'Other').";
    var value = window.prompt(msg, current);
    if (value === null) return; // cancelled
    btn.disabled = true;
    var r = await post("/manage/" + encodeURIComponent(slug) + "/category", { category: value });
    btn.disabled = false;
    if (!r.ok) {
      alert((r.data && r.data.error) || "Could not update category.");
      return;
    }
    window.location.reload();
  }

  document.addEventListener("click", async function (e) {
    var btn = e.target.closest && e.target.closest("[data-action]");
    if (!btn) return;
    var action = btn.getAttribute("data-action");
    var slug = btn.getAttribute("data-slug");
    if (!slug) return;

    if (action === "inspect-source") {
      await inspectSource(btn, slug);
      return;
    }

    if (action === "extract-sql") {
      await extractSql(btn, slug);
      return;
    }

    if (action === "diagnostics") {
      await runDiagnostics(btn, slug);
      return;
    }

    if (action === "query") {
      showQueryModal(slug);
      return;
    }

    if (action === "category") {
      await editCategory(btn, slug);
      return;
    }

    if (action === "icon") {
      editIcon(btn, slug);
      return;
    }

    if (action === "access") {
      await editAccess(btn, slug);
      return;
    }

    if (action === "transfer") {
      await transferOwner(btn, slug);
      return;
    }

    if (action === "delete" && !confirm("Remove app '" + slug + "'? This cannot be undone.")) {
      return;
    }

    btn.disabled = true;
    var r = await post("/manage/" + encodeURIComponent(slug) + "/" + action);
    btn.disabled = false;
    if (!r.ok) {
      alert((r.data && r.data.error) || "Action failed.");
      return;
    }
    window.location.reload();
  });
})();
