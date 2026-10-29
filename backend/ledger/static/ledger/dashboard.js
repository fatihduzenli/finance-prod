// The dashboard's small behaviours (docs/qa/PLAN-012f-b-staging-review.md D3, D6; PLAN-012g
// C2, C3). The root gets the class "js" first, so that the style sheet hides the mini
// calendar's script-free list. Each Cashflow card's range select (the wide card's and,
// without ?range=, the phone's) submits its GET form on change, except Custom, which shows the
// From and To selects and their Show button. A tap on a mini calendar day toggles its popover
// and rings the day (is-selected; hover and focus show the popover through the style sheet);
// one is open at a time. At 768 px and under the popover is not shown: the tapped day's list
// is moved (not copied) into #mini-panel under the grid, headed "Wed, Oct 28 · tapped", and
// moved back when another day or the same one is tapped. Without the script the form has its
// Show button, the popover opens on hover and focus, and a phone lists the days in details.
document.documentElement.classList.add("js");
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
  var panel = document.getElementById("mini-panel");
  var phone = window.matchMedia ? window.matchMedia("(max-width: 768px)") : null;
  var moved = null;
  function restore() {
    if (moved) { moved.day.appendChild(moved.list); moved = null; }
    if (panel) {
      while (panel.firstChild) { panel.removeChild(panel.firstChild); }
      panel.hidden = true;
    }
  }
  function intoPanel(day) {
    var list = day.querySelector(".mini-popover");
    if (!panel || !list) { return; }
    var head = document.createElement("p");
    head.className = "mini-panel-head";
    var label = document.createElement("strong");
    label.textContent = day.getAttribute("data-label") || "";
    head.appendChild(label);
    head.appendChild(document.createTextNode(" · tapped"));
    panel.appendChild(head);
    panel.appendChild(list);
    panel.hidden = false;
    moved = { day: day, list: list };
  }
  var days = Array.prototype.slice.call(document.querySelectorAll(".mini-day.has-items"));
  function closeAll() {
    days.forEach(function (other) { other.classList.remove("is-open", "is-selected"); });
    restore();
  }
  days.forEach(function (day) {
    day.addEventListener("click", function (event) {
      if (event.target.closest && event.target.closest(".mini-popover")) { return; }
      var opening = !day.classList.contains("is-open");
      closeAll();
      if (!opening) { return; }
      day.classList.add("is-open");
      day.classList.add("is-selected");
      if (phone && phone.matches) { intoPanel(day); }
    });
  });
  if (phone && phone.addEventListener) { phone.addEventListener("change", closeAll); }
})();
