// Catalog page: live search, category chip filter, and collapsible sections.
(function () {
  var search = document.getElementById("catalog-search");
  var chips = document.getElementById("category-chips");
  var noResults = document.getElementById("no-results");
  var sections = Array.prototype.slice.call(document.querySelectorAll(".cat-section"));
  var query = "";
  var activeCat = "";

  function cardMatches(card) {
    if (!query) return true;
    return (card.getAttribute("data-search") || "").indexOf(query) !== -1;
  }

  function apply() {
    var totalVisible = 0;
    sections.forEach(function (sec) {
      if (activeCat && sec.getAttribute("data-category") !== activeCat) {
        sec.hidden = true;
        return;
      }
      var cards = sec.querySelectorAll(".card");
      var shown = 0;
      Array.prototype.forEach.call(cards, function (card) {
        var vis = cardMatches(card);
        card.style.display = vis ? "" : "none";
        if (vis) shown++;
      });
      sec.hidden = shown === 0;
      totalVisible += shown;
    });
    if (noResults) noResults.hidden = totalVisible !== 0;
  }

  if (search) {
    search.addEventListener("input", function () {
      query = search.value.trim().toLowerCase();
      // Expand every section while searching so matches are never hidden.
      if (query) {
        sections.forEach(function (s) { s.classList.remove("collapsed"); });
      }
      apply();
    });
  }

  if (chips) {
    chips.addEventListener("click", function (e) {
      var b = e.target.closest && e.target.closest("[data-cat]");
      if (!b) return;
      activeCat = b.getAttribute("data-cat") || "";
      Array.prototype.forEach.call(chips.querySelectorAll(".chip-cat"), function (c) {
        c.classList.toggle("active", c === b);
      });
      apply();
    });
  }

  sections.forEach(function (sec) {
    var head = sec.querySelector(".cat-head");
    if (!head) return;
    head.addEventListener("click", function () {
      var collapsed = sec.classList.toggle("collapsed");
      head.setAttribute("aria-expanded", collapsed ? "false" : "true");
    });
  });
})();
