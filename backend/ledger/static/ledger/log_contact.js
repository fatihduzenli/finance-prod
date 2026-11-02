// The Log contact dialog (owner, 2026-11-01; ledger/contacts/_log_contact_dialog.html). The
// kind's dependent fields are ledger/shown_when.js's; this script keeps the rest:
// - the day under Date as the page reads it ("Nov 1, 2026") while the date changes;
// - choosing Promise to pay fills an empty Amount with the open balance of the store chosen
//   (#promise-amounts, by lease; "" for no particular store), and a change of store moves a
//   suggestion the user has not edited;
// - the supersede warning (#supersede-warnings, by lease) shows for a promise only;
// - a chosen outcome chip is cleared by a second click (a call may have none);
// - "Rescheduled" on the open promise chooses Promise to pay and fills the promise's date
//   and amount from the old promise (#promise-values).
// Without the script every field shows and the server checks every rule.
(function () {
  "use strict";
  var form = document.getElementById("contact-form");
  if (!form) { return; }
  function data(id) {
    var node = document.getElementById(id);
    return node ? JSON.parse(node.textContent) : {};
  }
  var amounts = data("promise-amounts");
  var warnings = data("supersede-warnings");
  var promises = data("promise-values");
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var date = form.querySelector("[name=happened_on]");
  var day = document.getElementById("log-contact-day");
  var amount = form.querySelector("[name=promised_amount]");
  var promisedDate = form.querySelector("[name=promised_date]");
  var lease = form.querySelector("[name=lease]");
  var warning = document.getElementById("promise-supersede");
  var suggested = "";

  function kind() {
    var chosen = form.querySelector("input[name=kind]:checked");
    return chosen ? chosen.value : "";
  }
  function leaseKey() { return lease ? lease.value : ""; }
  function showDay() {
    if (!date || !day) { return; }
    var parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date.value);
    day.textContent = parts ? MONTHS[Number(parts[2]) - 1] + " " + Number(parts[3]) + ", " + parts[1] : "";
  }
  function suggest() {
    if (!amount || kind() !== "promise") { return; }
    var value = amounts[leaseKey()] || "";
    if (amount.value === "" || amount.value === suggested) {
      amount.value = value;
      suggested = value;
    }
  }
  function showWarning() {
    if (!warning) { return; }
    var text = kind() === "promise" ? warnings[leaseKey()] || "" : "";
    warning.textContent = text;
    warning.hidden = !text;
  }
  function choose(value) {
    var radio = form.querySelector("input[name=kind][value=" + value + "]");
    if (!radio || radio.checked) { return; }
    radio.checked = true;
    radio.dispatchEvent(new Event("change", { bubbles: true }));
  }

  if (date) { date.addEventListener("input", showDay); date.addEventListener("change", showDay); }
  form.querySelectorAll("input[name=kind]").forEach(function (radio) {
    radio.addEventListener("change", function () { suggest(); showWarning(); });
  });
  if (lease) { lease.addEventListener("change", function () { suggest(); showWarning(); }); }
  form.querySelectorAll("input[name=call_outcome]").forEach(function (radio) {
    var was = false;
    var chip = radio.closest("label") || radio;
    chip.addEventListener("pointerdown", function () { was = radio.checked; });
    radio.addEventListener("keydown", function () { was = false; });
    radio.addEventListener("click", function () {
      if (was) { radio.checked = false; radio.dispatchEvent(new Event("change", { bubbles: true })); }
      was = false;
    });
  });
  form.querySelectorAll("input[name=outcome]").forEach(function (radio) {
    radio.addEventListener("change", function () {
      if (radio.value !== "rescheduled" || !radio.checked) { return; }
      var previous = form.querySelector("[name=previous]");
      var old = previous ? promises[previous.value] : null;
      if (old && promisedDate && !promisedDate.value) { promisedDate.value = old.date; }
      if (old && amount && (!amount.value || amount.value === suggested)) {
        amount.value = old.amount;
        suggested = "";
      }
      choose("promise");
    });
  });
  showDay();
  showWarning();
})();
