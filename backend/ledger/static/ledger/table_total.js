// The total line under a table of amounts (docs/qa/PLAN-012d D9, D10): the setup
// checklist's prices on the new lease form and the transfer's incoming step, and the
// lease end's deductions. Every "span.js-total-out[data-total-for=<table id>]" shows the
// sum of the table's "input.js-total" amounts as they are typed; an empty or invalid
// amount counts as nothing. The server renders the same line, so the page reads right
// without this script; nothing is sent from here.
(function () {
  "use strict";
  function cents(text) {
    var value = Number(String(text || "").trim());
    return String(text || "").trim() !== "" && isFinite(value) && value > 0
      ? Math.round(value * 100)
      : 0;
  }
  function money(value) {
    return "$" + (value / 100).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }
  document.querySelectorAll("span.js-total-out[data-total-for]").forEach(function (out) {
    var table = document.getElementById(out.dataset.totalFor);
    if (!table) {
      return;
    }
    function update() {
      var sum = 0;
      table.querySelectorAll("input.js-total").forEach(function (input) {
        var row = input.closest("tr");
        if (!row || !row.hidden) {
          sum += cents(input.value);
        }
      });
      out.textContent = money(sum);
    }
    table.addEventListener("input", update);
    table.addEventListener("change", update);
    update();
  });
})();
