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

  document.addEventListener("click", async function (e) {
    var btn = e.target.closest && e.target.closest("[data-action]");
    if (!btn) return;
    var action = btn.getAttribute("data-action");
    var slug = btn.getAttribute("data-slug");
    if (!slug) return;

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
