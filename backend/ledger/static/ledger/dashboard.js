// The dashboard's two small behaviours (docs/qa/PLAN-012f-b-staging-review.md D3, D6). Each
// Cashflow card's range select (the wide card's and, without ?range=, the phone's; PLAN-012g
// C2) submits its GET form on change, except Custom, which shows the From and To selects and
// their Show button. A tap on a mini calendar day toggles its popover (hover and
// focus show it through the style sheet); one popover is open at a time. Without the script
// the form has its Show button and the popover opens on hover and focus.
(function () {
  "use strict";
  Array.prototype.slice.call(document.querySelectorAll("form.range-form")).forEach(function (form) {
    var select = form.querySelector('select[name="range"]');
    if (!select) { return; }
    select.addEventListener("change", function () {
      if (select.value === "custom") { form.classList.add("is-custom"); return; }
      form.submit();
    });
  });
  var days = Array.prototype.slice.call(document.querySelectorAll(".mini-day.has-items"));
  days.forEach(function (day) {
    day.addEventListener("click", function (event) {
      if (event.target.closest && event.target.closest(".mini-popover")) { return; }
      var opening = !day.classList.contains("is-open");
      days.forEach(function (other) { other.classList.remove("is-open"); });
      if (opening) { day.classList.add("is-open"); }
    });
  });
})();
