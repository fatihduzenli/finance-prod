// The store page's payment form (docs/qa/PLAN-012d D6): shows the unallocated amount
// live (the amount received less the allocations) and names it on the keep-credit tick.
// With exactly one open charge, typing the amount fills that charge's allocation with
// the smaller of the amount and its balance, until the allocation is typed by hand. The
// keep-credit tick is shown only while something is unallocated (PLAN-012c D8): hidden,
// it is disabled and not sent; a tick the server refused stays shown. The server checks
// everything again; the form works without this script, with the tick always shown.
(function () {
  "use strict";
  document.querySelectorAll("form.js-allocation").forEach(function (form) {
    var amount = form.querySelector("input[name=amount]");
    var out = form.querySelector("#unallocated");
    var keep = form.querySelector("#keep-credit-amount");
    var keepField = form.querySelector("#keep-credit-field");
    var keepTick = keepField ? keepField.querySelector("input[type=checkbox]") : null;
    var allocations = Array.prototype.slice.call(form.querySelectorAll("input.js-alloc"));
    var prefill = allocations.length === 1 && allocations[0].dataset.prefill ? allocations[0] : null;
    var touched = prefill !== null && prefill.value !== "";
    function cents(text) {
      var value = Number(String(text || "").trim());
      return isFinite(value) ? Math.round(value * 100) : 0;
    }
    function money(value) {
      var sign = value < 0 ? "-" : "";
      var whole = Math.abs(value);
      return sign + "$" + (whole / 100).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    }
    function update() {
      if (prefill !== null && !touched) {
        var received = cents(amount.value);
        var balance = cents(prefill.dataset.prefill);
        prefill.value = received > 0 ? (Math.min(received, balance) / 100).toFixed(2) : "";
      }
      var left = cents(amount.value);
      allocations.forEach(function (input) { left -= cents(input.value); });
      out.textContent = money(left);
      if (keep) {
        keep.textContent = left > 0 ? money(left) : "the unallocated amount";
      }
      if (keepField && keepTick) {
        keepField.hidden = left <= 0 && !keepField.classList.contains("has-error");
        keepTick.disabled = keepField.hidden;
      }
    }
    if (prefill !== null) {
      prefill.addEventListener("input", function () { touched = true; });
    }
    form.addEventListener("input", update);
    update();
  });
})();
