// The New calendar entry dialog (owner, 2026-11-06; ledger/calendar/_entry_dialog.html), opened
// by "+ New entry" through finance/dialog.js. This script keeps the rest:
// - an opener with data-day ("+ New entry", the day panel's "+ Add entry", a day's "+") sets
//   that day as the Date (finance/dialog.js opens the dialog; without the script each is a
//   link to the old URL with ?on=). A click on a day itself selects it for the panel;
// - on a touch screen a day held for 500 ms (a pointerdown not moved or lifted) shows its "+"
//   (.show-add) and the click that ends the hold is not followed; a plain tap selects the day;
// - the day beside Date as the page reads it ("Nov 6, 2026") while the date changes;
// - the link's type-ahead: typing in #entry-link asks ledger:calendar:search (150 ms after the
//   last key) and lists the stores, operators and leases found, each a button; the arrow keys
//   move the highlight and Enter chooses it. A choice fills the hidden link_kind and link_id
//   and shows the chip in place of the input; the chip's × clears both and shows the input.
//   Every text is set with textContent;
// - Cancel closes a dialog the page opened itself (one the server opened goes to its
//   return URL, as its link does).
(function () {
  "use strict";
  var dialog = document.getElementById("calendar-entry");
  var form = document.getElementById("calendar-entry-form");
  if (!dialog || !form) { return; }
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var KINDS = {store: "Store", operator: "Operator", lease: "Lease"};
  var date = form.querySelector("[name=on]");
  var day = document.getElementById("entry-day");
  var input = document.getElementById("entry-link");
  var list = document.getElementById("entry-link-results");
  var status = document.getElementById("entry-link-status");
  var chip = document.getElementById("entry-link-chip");
  var chipText = document.getElementById("entry-link-text");
  var remove = document.getElementById("entry-link-remove");
  var linkKind = form.querySelector("[name=link_kind]");
  var linkId = form.querySelector("[name=link_id]");
  var cancel = document.getElementById("cancel-calendar-entry");
  var month = document.getElementById("calendar-month");
  var timer = null;
  var asked = 0;
  var active = -1;

  function modal() { return typeof dialog.showModal === "function"; }
  function showDay() {
    if (!date || !day) { return; }
    var parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date.value);
    day.textContent = parts ? MONTHS[Number(parts[2]) - 1] + " " + Number(parts[3]) + ", " + parts[1] : "";
  }

  if (date) { date.addEventListener("input", showDay); date.addEventListener("change", showDay); }
  document.addEventListener("click", function (event) {
    var opener = event.target.closest ? event.target.closest("[data-dialog-open=calendar-entry][data-day]") : null;
    if (opener && date && modal()) {
      date.value = opener.getAttribute("data-day");
      showDay();
    }
  });
  if (month) {
    var HOLD_MS = 500;
    var held = null;
    var holdTimer = null;
    var start = null;
    var swallow = false;
    function stopHold() { window.clearTimeout(holdTimer); holdTimer = null; }
    month.addEventListener("pointerdown", function (event) {
      swallow = false;
      if (event.pointerType !== "touch") { return; }
      var cell = event.target.closest(".calendar-day");
      if (!cell || !cell.querySelector(".day-add")) { return; }
      start = {x: event.clientX, y: event.clientY};
      stopHold();
      holdTimer = window.setTimeout(function () {
        holdTimer = null;
        if (held && held !== cell) { held.classList.remove("show-add"); }
        held = cell;
        cell.classList.add("show-add");
        swallow = true;
      }, HOLD_MS);
    });
    month.addEventListener("pointermove", function (event) {
      if (holdTimer && start && Math.abs(event.clientX - start.x) + Math.abs(event.clientY - start.y) > 10) { stopHold(); }
    });
    month.addEventListener("pointerup", stopHold);
    month.addEventListener("pointercancel", stopHold);
    month.addEventListener("contextmenu", function (event) {
      if (swallow || holdTimer) { event.preventDefault(); }
    });
    month.addEventListener("click", function (event) {
      if (!swallow) { return; }
      swallow = false;
      if (!event.target.closest(".day-add")) { event.preventDefault(); event.stopPropagation(); }
    }, true);
  }
  if (cancel) {
    cancel.addEventListener("click", function (event) {
      if (dialog.hasAttribute("data-open") || !modal()) { return; }
      event.preventDefault();
      dialog.close();
    });
  }

  if (!input || !list || !status || !chip || !chipText || !remove || !linkKind || !linkId) { return; }
  var url = input.getAttribute("data-url");
  function rows() { return Array.prototype.slice.call(list.querySelectorAll("li")); }
  function highlight(index) {
    var items = rows();
    active = items.length ? Math.max(-1, Math.min(index, items.length - 1)) : -1;
    items.forEach(function (item, position) { item.classList.toggle("is-active", position === active); });
  }
  function choose(row) {
    linkKind.value = row.kind;
    linkId.value = row.id;
    chipText.textContent = KINDS[row.kind] + ": " + row.label;
    chip.hidden = false;
    input.hidden = true;
    input.value = "";
    list.textContent = "";
    status.textContent = "";
    remove.focus();
  }
  function render(results) {
    list.textContent = "";
    active = -1;
    results.forEach(function (row) {
      if (!KINDS[row.kind]) { return; }
      var button = document.createElement("button");
      button.type = "button";
      button.textContent = row.label;
      var sub = document.createElement("span");
      sub.className = "muted";
      sub.textContent = " · " + KINDS[row.kind] + (row.sub ? " · " + row.sub : "");
      button.appendChild(sub);
      button.addEventListener("click", function () { choose(row); });
      var item = document.createElement("li");
      item.appendChild(button);
      list.appendChild(item);
    });
    status.textContent = results.length ? "" : "Nothing matches";
  }
  function search() {
    var query = input.value.trim();
    var ticket = ++asked;
    if (!query) { list.textContent = ""; status.textContent = ""; return; }
    fetch(url + "?q=" + encodeURIComponent(query), {
      credentials: "same-origin", headers: {"Accept": "application/json"}
    }).then(function (response) {
      if (!response.ok) { throw new Error("status " + response.status); }
      return response.json();
    }).then(function (body) {
      if (ticket === asked) { render(body.results || []); }
    }).catch(function () {
      if (ticket === asked) { list.textContent = ""; status.textContent = "The search did not answer; try again."; }
    });
  }
  input.addEventListener("input", function () {
    window.clearTimeout(timer);
    timer = window.setTimeout(search, 150);
  });
  input.addEventListener("keydown", function (event) {
    if (event.key === "ArrowDown") { event.preventDefault(); highlight(active + 1); }
    else if (event.key === "ArrowUp") { event.preventDefault(); highlight(active - 1); }
    else if (event.key === "Enter") {
      // Enter never submits the entry from the search box.
      event.preventDefault();
      var button = active >= 0 ? rows()[active].querySelector("button") : null;
      if (button) { button.click(); }
    }
  });
  remove.addEventListener("click", function () {
    linkKind.value = "";
    linkId.value = "";
    chipText.textContent = "";
    chip.hidden = true;
    input.hidden = false;
    input.focus();
  });
})();
