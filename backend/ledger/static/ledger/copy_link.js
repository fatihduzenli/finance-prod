// "Copy invite link" on the operator page's PORTAL card (owner, 2026-11-01): a button carrying
// data-copy-from="<a read-only field's id>" is shown, and a press copies the field's text with
// the Clipboard API ("Copied" for two seconds). Without the script, or without the Clipboard
// API, the button stays hidden and the field's text is selected and copied by hand.
(function () {
  "use strict";
  if (!navigator.clipboard || !window.isSecureContext) { return; }
  var buttons = document.querySelectorAll("button[data-copy-from]");
  Array.prototype.forEach.call(buttons, function (button) {
    var field = document.getElementById(button.getAttribute("data-copy-from"));
    if (!field) { return; }
    button.hidden = false;
    button.addEventListener("click", function () {
      field.select();
      navigator.clipboard.writeText(field.value).then(function () {
        button.textContent = "Copied";
        window.setTimeout(function () { button.textContent = "Copy"; }, 2000);
      }, function () {
        button.textContent = "Select the link and copy it";
      });
    });
  });
})();
