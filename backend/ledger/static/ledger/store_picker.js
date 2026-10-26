// The "Which store?" picker (docs/qa/PLAN-012f-b-staging-review.md D7;
// ledger/templates/ledger/_store_picker.html). Typing in #store-picker-q asks the JSON route
// named by its data-url (150 ms after the last key) and lists at most ten stores, each a link
// to its Create invoice page: "#number Name · operator", "closed" added for a closed store.
// The arrow keys move the highlight and Enter follows the highlighted row; with no row
// highlighted Enter submits the form, the search page for a new invoice. Every text is set
// with textContent. The input takes the focus when the dialog opens.
(function () {
  "use strict";
  var dialog = document.getElementById("store-picker");
  var input = document.getElementById("store-picker-q");
  var list = document.getElementById("store-picker-results");
  var status = document.getElementById("store-picker-status");
  if (!dialog || !input || !list || !status) { return; }
  var url = input.getAttribute("data-url");
  var timer = null;
  var asked = 0;
  var active = -1;
  function rows() { return Array.prototype.slice.call(list.querySelectorAll("li")); }
  function highlight(index) {
    var items = rows();
    active = items.length ? Math.max(-1, Math.min(index, items.length - 1)) : -1;
    items.forEach(function (item, position) {
      item.classList.toggle("is-active", position === active);
    });
  }
  function muted(text) {
    var span = document.createElement("span");
    span.className = "muted";
    span.textContent = text;
    return span;
  }
  function render(stores) {
    list.textContent = "";
    active = -1;
    stores.slice(0, 10).forEach(function (store) {
      var link = document.createElement("a");
      link.href = store.url;
      var number = store.number === null || store.number === undefined ? "" : "#" + store.number + " ";
      link.textContent = number + store.name;
      if (store.operator) { link.appendChild(muted(" · " + store.operator)); }
      if (store.closed) { link.appendChild(muted(" · closed")); }
      var item = document.createElement("li");
      item.appendChild(link);
      list.appendChild(item);
    });
    status.textContent = stores.length ? "" : "No store matches";
  }
  function search() {
    var query = input.value.trim();
    var ticket = ++asked;
    if (!query) { render([]); status.textContent = "Type to search"; return; }
    fetch(url + "?q=" + encodeURIComponent(query), {
      credentials: "same-origin", headers: {"Accept": "application/json"}
    }).then(function (response) {
      if (!response.ok) { throw new Error("status " + response.status); }
      return response.json();
    }).then(function (body) {
      if (ticket === asked) { render(body.stores || []); }
    }).catch(function () {
      if (ticket === asked) { list.textContent = ""; status.textContent = "Press Enter to search"; }
    });
  }
  input.addEventListener("input", function () {
    window.clearTimeout(timer);
    timer = window.setTimeout(search, 150);
  });
  input.addEventListener("keydown", function (event) {
    if (event.key === "ArrowDown") { event.preventDefault(); highlight(active + 1); }
    else if (event.key === "ArrowUp") { event.preventDefault(); highlight(active - 1); }
    else if (event.key === "Enter" && active >= 0) {
      var link = rows()[active].querySelector("a");
      if (link) { event.preventDefault(); window.location.assign(link.href); }
    }
  });
  new MutationObserver(function () {
    if (dialog.open) { input.focus(); }
  }).observe(dialog, {attributes: true, attributeFilter: ["open"]});
})();
