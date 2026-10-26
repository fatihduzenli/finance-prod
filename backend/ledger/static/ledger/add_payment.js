// Add payment's SUMMARY (PLAN-012f-b D14): "This payment", the "After this payment" line
// and the amount in the primary button follow the amount typed in the card chosen (the
// radio "how": the bank debit's #id_debit_amount, the record card's #id_amount). Which
// words the button shows is ledger/shown_when.js's. The server renders the same figures,
// so the page reads right without this script; nothing is sent from here.
(function () {
  "use strict";
  var summary = document.getElementById("payment-summary");
  if (!summary) {
    return;
  }
  var form = summary.closest("form");
  var balance = cents(summary.getAttribute("data-balance"));
  var tile = document.querySelector("#tile-this-payment .kpi-value");
  var after = document.getElementById("after-payment-value");
  function cents(text) {
    var value = Number(String(text || "").trim());
    return String(text || "").trim() !== "" && isFinite(value) && value > 0
      ? Math.round(value * 100)
      : 0;
  }
  function money(value) {
    return "$" + (value / 100).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }
  function update() {
    var chosen = form.querySelector('input[name="how"]:checked');
    var input = document.getElementById(
      chosen && chosen.value === "debit" ? "id_debit_amount" : "id_amount"
    );
    var amount = input ? cents(input.value) : 0;
    var text = money(amount);
    if (tile) {
      tile.textContent = text;
    }
    summary.querySelectorAll(".payment-amount").forEach(function (span) {
      span.textContent = text;
    });
    if (after) {
      after.textContent = amount > 0 && amount >= balance
        ? after.getAttribute("data-paid")
        : money(Math.max(balance - amount, 0)) + after.getAttribute("data-owed");
    }
  }
  form.addEventListener("input", update);
  form.addEventListener("change", update);
  update();
})();
