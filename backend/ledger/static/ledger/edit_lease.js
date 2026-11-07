// The Edit lease page (docs/qa/15/PLAN-015a-edit-lease.md), in the browser only; the server
// checks every rule and nothing is sent from here.
// - "Change rent" (#change-rent) shows and hides #rent-change-fields; they start hidden unless
//   a rent is typed or the server refused one. Without the script the fields stay visible.
// - The Changes card (aside#lease-changes): one li per control whose value differs from its
//   data-initial, worded "<data-label> <old> → <new>" ("Debit day 1st → 5th"; a select by its
//   options' text, a channel by its card's data-short, the switch On/Off, the rent from the
//   input's data-current-text with "from <date>"). Nothing changed: #changes-empty shows and
//   Save (#save-lease) is disabled; reverting a change disables it again. A hidden control
//   (ledger/shown_when.js disables it, so it is not sent) counts as empty.
// - #autopay-warning (only in the page when the lease has a live provider schedule) shows
//   while a debit day or the rent changes: those are what mark the autopay Needs review.
(function () {
  "use strict";
  var form = document.getElementById("lease-form");
  var list = document.getElementById("changes-list");
  if (!form || !list) { return; }
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var NONE = "—";
  var ARROW = " → ";
  var REVIEWED = { debit_day: true, second_debit_day: true, rent: true };
  var empty = document.getElementById("changes-empty");
  var save = document.getElementById("save-lease");
  var warning = document.getElementById("autopay-warning");
  var button = document.getElementById("change-rent");
  var fields = document.getElementById("rent-change-fields");
  var rent = document.getElementById("id_rent");
  var from = document.getElementById("id_rent_effective_from");

  function cents(text) {
    var value = String(text || "").trim().replace(/[$,]/g, "");
    var match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(value);
    if (!match) { return null; }
    return Number(match[1]) * 100 + Number(((match[2] || "") + "00").slice(0, 2));
  }
  function money(value) {
    var whole = String(Math.floor(value / 100)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return "$" + whole + "." + String(value % 100).padStart(2, "0");
  }
  // "2026-11-01" as "Nov 1, 2026" (finance.dates.day_text); anything else as typed.
  function dayText(text) {
    var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(text || ""));
    if (!match || Number(match[2]) < 1 || Number(match[2]) > 12) { return String(text || ""); }
    return MONTHS[Number(match[2]) - 1] + " " + Number(match[3]) + ", " + match[1];
  }
  function optionText(select, value) {
    var found = Array.prototype.find.call(select.options, function (option) {
      return option.value === value;
    });
    return found && found.value ? found.textContent.trim() : NONE;
  }
  function current(control) { return control.disabled ? "" : String(control.value || ""); }

  // Each change: { field, label, text }.
  function changes() {
    var found = [];
    var seenRadios = {};
    form.querySelectorAll("[data-initial][data-label]").forEach(function (control) {
      var name = control.name;
      var label = control.getAttribute("data-label");
      var initial = control.getAttribute("data-initial");
      if (control === rent || control === from) { return; }
      if (control.type === "radio") {
        if (seenRadios[name]) { return; }
        seenRadios[name] = true;
        var radios = form.querySelectorAll('input[type=radio][name="' + name + '"]');
        var chosen = Array.prototype.find.call(radios, function (radio) { return radio.checked; });
        var before = Array.prototype.find.call(radios, function (radio) { return radio.value === initial; });
        if (chosen && chosen.value !== initial) {
          found.push({ field: name, label: label, text: (before ? before.getAttribute("data-short") : NONE) + ARROW + chosen.getAttribute("data-short") });
        }
        return;
      }
      if (control.type === "checkbox") {
        var on = initial === "true";
        if (control.checked !== on) {
          found.push({ field: name, label: label, text: (on ? "On" : "Off") + ARROW + (control.checked ? "On" : "Off") });
        }
        return;
      }
      if (control.tagName === "SELECT" && current(control) !== initial) {
        found.push({ field: name, label: label, text: optionText(control, initial) + ARROW + optionText(control, current(control)) });
      }
    });
    if (rent && current(rent).trim() !== "") {
      var typed = cents(rent.value);
      var stored = cents(rent.getAttribute("data-current"));
      if (typed === null || typed !== stored) {
        var text = (rent.getAttribute("data-current-text") || NONE) + ARROW + (typed === null ? rent.value.trim() : money(typed));
        if (from && current(from)) { text += " from " + dayText(from.value); }
        found.push({ field: "rent", label: rent.getAttribute("data-label"), text: text });
      }
    }
    return found;
  }

  function render() {
    var found = changes();
    list.textContent = "";
    found.forEach(function (change) {
      var item = document.createElement("li");
      item.setAttribute("data-field", change.field);
      var label = document.createElement("span");
      label.className = "change-label";
      label.textContent = change.label;
      var text = document.createElement("span");
      text.className = "change-text";
      text.textContent = change.text;
      item.appendChild(label);
      item.appendChild(document.createTextNode(" "));
      item.appendChild(text);
      list.appendChild(item);
    });
    if (empty) { empty.hidden = found.length > 0; }
    if (save) { save.disabled = found.length === 0; }
    if (warning) {
      warning.hidden = !found.some(function (change) { return REVIEWED[change.field]; });
    }
  }

  if (button && fields && rent) {
    var open = rent.value.trim() !== "" || fields.querySelector(".has-error") !== null;
    var show = function (shown) {
      fields.hidden = !shown;
      button.setAttribute("aria-expanded", shown ? "true" : "false");
      button.textContent = shown ? "Keep the rent" : "Change rent";
    };
    button.hidden = false;
    show(open);
    button.addEventListener("click", function () {
      var shown = fields.hidden;
      if (!shown) { rent.value = ""; }
      show(shown);
      if (shown) { rent.focus(); }
      render();
    });
  }
  document.addEventListener("input", render);
  document.addEventListener("change", render);
  render();
})();
