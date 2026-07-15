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

  function showSqlModal(slug, datasource, queries) {
    var overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    var modal = document.createElement("div");
    modal.className = "modal";

    var head = document.createElement("div");
    head.className = "modal-head";
    var title = document.createElement("h2");
    title.textContent = "SQL in '" + slug + "'";
    var close = document.createElement("button");
    close.type = "button";
    close.className = "chip tiny";
    close.textContent = "Close";
    head.appendChild(title);
    head.appendChild(close);
    modal.appendChild(head);

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

    function dismiss() { document.body.removeChild(overlay); }
    close.addEventListener("click", dismiss);
    overlay.addEventListener("click", function (e) { if (e.target === overlay) dismiss(); });

    overlay.appendChild(modal);
    document.body.appendChild(overlay);
  }

  async function inspectSql(btn, slug) {
    btn.disabled = true;
    var res = await fetch("/manage/" + encodeURIComponent(slug) + "/sql", { credentials: "same-origin" });
    var data = await res.json().catch(function () { return {}; });
    btn.disabled = false;
    if (!res.ok || !data.ok) {
      alert((data && data.error) || "Could not inspect app.");
      return;
    }
    showSqlModal(slug, data.datasource, data.queries || []);
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

    if (action === "inspect") {
      await inspectSql(btn, slug);
      return;
    }

    if (action === "category") {
      await editCategory(btn, slug);
      return;
    }

    if (action === "access") {
      await editAccess(btn, slug);
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
