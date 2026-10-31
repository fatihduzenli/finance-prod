// "Copy from operator" on the Billing details of a new lease (docs/qa/PLAN-014 C4): fills
// the e-mail, phone and address fields from the operator's contact details, in the browser
// only. The details are data-* attributes (data-email, data-phone, data-address-line1, ...)
// of the operator select's chosen option when the button names that select in data-from
// (New lease), else of the button itself (the transfer's incoming operator). Nothing is
// copied until the button is pressed, and the server never copies anything: a form posted
// with empty fields keeps them empty. Without the script the button stays hidden.
(function () {
  "use strict";
  var button = document.getElementById("copy-from-operator");
  if (!button) { return; }
  var FIELDS = ["email", "phone", "address_line1", "address_line2", "city", "state", "postal_code"];
  function source() {
    var from = button.getAttribute("data-from");
    if (!from) { return button; }
    var select = document.getElementById(from);
    if (!select || select.selectedIndex < 0) { return null; }
    return select.options[select.selectedIndex];
  }
  button.addEventListener("click", function () {
    var holder = source();
    if (!holder || !holder.hasAttribute("data-email")) { return; }
    FIELDS.forEach(function (name) {
      var field = document.getElementById("id_" + name);
      if (!field || field.disabled) { return; }
      field.value = holder.getAttribute("data-" + name.replace(/_/g, "-")) || "";
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
  });
})();
