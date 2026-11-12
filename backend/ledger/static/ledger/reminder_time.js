// The reminder's time beside its switch (PLAN-015h; ledger/calendar/_reminder_row.html, on the
// New calendar entry dialog and the edit page): the time shows only while the switch is on. On
// load it follows the switch as the browser restored it, except that a time the server refused
// stays shown with its error.
(function () {
  "use strict";
  Array.prototype.forEach.call(document.querySelectorAll(".entry-morning"), function (row) {
    var toggle = row.querySelector("input[type=checkbox]");
    var wrap = row.querySelector(".entry-time");
    if (!toggle || !wrap) { return; }
    toggle.addEventListener("change", function () { wrap.hidden = !toggle.checked; });
    wrap.hidden = !toggle.checked && !wrap.querySelector(".errorlist");
  });
})();
