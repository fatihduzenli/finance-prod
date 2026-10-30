// The list pages' search and filters (PLAN-014 A5; finance/_filters.html). Without this script
// the panel is open and the form submits through Enter and Apply. With it: the "Filters" button
// shows and toggles the panel (aria-expanded; on a phone, 768 px and under, the panel is a
// bottom sheet over #filters-backdrop); Escape and a tap on the backdrop close it. Typing in
// the search field fetches the same page with the form's values after 300 ms without a key,
// replaces #results (and the chips, the counts line and the spreadsheet link, which carry the
// query) with the response's own, and writes the address with history.replaceState; any error
// falls back to submitting the form.
(function () {
  "use strict";
  var form = document.getElementById("filters");
  if (!form) { return; }
  var toggle = document.getElementById("filters-toggle");
  var panel = document.getElementById("filters-panel");
  var backdrop = document.getElementById("filters-backdrop");
  var search = form.querySelector(".filters-search");
  var swapped = ["filters-chips", "list-counts", "export-csv"];
  var phone = window.matchMedia("(max-width: 768px)");

  form.classList.add("is-enhanced");
  toggle.hidden = false;

  function setOpen(open) {
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    panel.classList.toggle("is-open", open);
    backdrop.hidden = !(open && phone.matches);
    if (open) {
      var first = panel.querySelector("select, input:not([type=hidden]), textarea, button");
      if (first) { first.focus(); }
    }
  }
  toggle.addEventListener("click", function () {
    setOpen(toggle.getAttribute("aria-expanded") !== "true");
  });
  backdrop.addEventListener("click", function () { setOpen(false); });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && toggle.getAttribute("aria-expanded") === "true") {
      setOpen(false);
      toggle.focus();
    }
  });

  if (!search) { return; }
  var timer = null;
  var latest = 0;

  function address() {
    var params = new URLSearchParams(new FormData(form));
    var query = params.toString();
    return form.action.split("?")[0] + (query ? "?" + query : "");
  }

  function replaceFrom(page, id) {
    var mine = document.getElementById(id);
    var theirs = page.getElementById(id);
    if (mine && theirs) { mine.replaceWith(document.importNode(theirs, true)); }
  }

  function refresh() {
    var url = address();
    var ticket = ++latest;
    fetch(url, {headers: {"X-Requested-With": "fetch"}, credentials: "same-origin"})
      .then(function (response) {
        if (!response.ok) { throw new Error("status " + response.status); }
        return response.text();
      })
      .then(function (text) {
        if (ticket !== latest) { return; }
        var page = new DOMParser().parseFromString(text, "text/html");
        var results = document.getElementById("results");
        var fresh = page.getElementById("results");
        if (!results || !fresh) { throw new Error("no results region"); }
        results.replaceChildren.apply(results, Array.prototype.map.call(fresh.childNodes, function (node) {
          return document.importNode(node, true);
        }));
        swapped.forEach(function (id) { replaceFrom(page, id); });
        history.replaceState(null, "", url);
        document.dispatchEvent(new CustomEvent("filters:updated"));
      })
      .catch(function () { form.submit(); });
  }

  search.addEventListener("input", function () {
    window.clearTimeout(timer);
    timer = window.setTimeout(refresh, 300);
  });
})();
