// The dialog component (docs/qa/PLAN-012f-b-staging-review.md D2; the {% dialog %} tag in
// finance/templatetags/components.py). A dialog[data-open] (opened by the server) carries the
// open attribute too, so it shows with scripts off; on load it is closed and shown again as a
// modal (its close event during that switch is ignored: no return-url navigation); without
// showModal it keeps the open attribute. The close button,
// Escape and a click on the backdrop close a dialog; a server-opened dialog with a
// data-return-url goes there once closed. A click on an element carrying
// data-dialog-open="<id>" opens that dialog, and its link is not followed only when the
// dialog exists and showModal works: without the script the link still leads to a page.
(function () {
  "use strict";
  function modal(dialog) { return typeof dialog.showModal === "function"; }
  function show(dialog) {
    if (dialog.open) { return; }
    if (modal(dialog)) { dialog.showModal(); } else { dialog.setAttribute("open", ""); }
  }
  function hide(dialog) {
    if (typeof dialog.close === "function") { dialog.close(); return; }
    if (!dialog.hasAttribute("open")) { return; }
    dialog.removeAttribute("open");
    closed(dialog);
  }
  function closed(dialog) {
    var target = dialog.getAttribute("data-return-url");
    if (dialog.hasAttribute("data-open") && target) { window.location.assign(target); }
  }
  function outside(dialog, event) {
    var box = dialog.getBoundingClientRect();
    return event.clientX < box.left || event.clientX > box.right ||
      event.clientY < box.top || event.clientY > box.bottom;
  }
  var dialogs = Array.prototype.slice.call(document.querySelectorAll("dialog.dialog"));
  dialogs.forEach(function (dialog) {
    var button = dialog.querySelector(".dialog-close");
    if (button) { button.addEventListener("click", function () { hide(dialog); }); }
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog && outside(dialog, event)) { hide(dialog); }
    });
    var switching = false;
    dialog.addEventListener("close", function () {
      if (switching) { switching = false; return; }
      closed(dialog);
    });
    if (dialog.hasAttribute("data-open")) {
      if (dialog.open && modal(dialog)) { switching = true; dialog.close(); }
      show(dialog);
    }
  });
  document.addEventListener("keydown", function (event) {
    // A modal dialog closes itself on Escape; this covers the open attribute's fallback.
    if (event.key !== "Escape") { return; }
    dialogs.forEach(function (dialog) { if (!modal(dialog)) { hide(dialog); } });
  });
  document.addEventListener("click", function (event) {
    var opener = event.target.closest ? event.target.closest("[data-dialog-open]") : null;
    if (!opener) { return; }
    var dialog = document.getElementById(opener.getAttribute("data-dialog-open"));
    if (!dialog || dialog.tagName !== "DIALOG" || !modal(dialog)) { return; }
    event.preventDefault();
    show(dialog);
  });
})();
