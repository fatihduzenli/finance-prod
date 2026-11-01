// The sign-in page's "Show" button (PLAN-014 I-b; accounts/templates/two_factor/core/login.html):
// a button carrying data-password-toggle and aria-controls="<the password field's id>" is
// shown, and a press switches the field between hidden and readable text ("Show" / "Hide",
// aria-pressed and the button's label follow). It is a button, so the keyboard reaches it and
// Enter or Space presses it; it never submits the form. Without the script the button stays
// hidden and the field stays a password field.
(function () {
  "use strict";
  var buttons = document.querySelectorAll("button[data-password-toggle]");
  Array.prototype.forEach.call(buttons, function (button) {
    var field = document.getElementById(button.getAttribute("aria-controls"));
    if (!field) { return; }
    button.hidden = false;
    button.addEventListener("click", function () {
      var showing = field.type === "password";
      field.type = showing ? "text" : "password";
      button.textContent = showing ? "Hide" : "Show";
      button.setAttribute("aria-pressed", showing ? "true" : "false");
      button.setAttribute("aria-label", showing ? "Hide password" : "Show password");
    });
  });
})();
