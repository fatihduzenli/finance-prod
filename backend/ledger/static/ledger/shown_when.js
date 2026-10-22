// Dependent fields (docs/qa/PLAN-012c-design-pass.md D8): a control carrying
// data-shown-when is shown, with its .field wrapper, only while one of its "|"-separated
// conditions holds: "<id>" (a ticked box or a chosen radio, or an input with a value) or
// "<id>=<value>" (a select or input with that value). Hidden, it is also disabled, so it
// is not sent: what is saved is what the page shows. A field with an error from the
// server stays shown.
// Controls marked data-reveal (a tick that only drives this script) appear only when the
// script runs. Without the script every field stays visible and nothing is hidden; the
// server checks every rule either way.
(function () {
  "use strict";
  function holds(condition) {
    var parts = condition.split("=");
    var control = document.getElementById(parts[0]);
    if (!control) { return true; }
    if (parts.length > 1) { return control.value === parts.slice(1).join("="); }
    if (control.type === "checkbox" || control.type === "radio") { return control.checked; }
    return String(control.value || "").trim() !== "";
  }
  var dependents = Array.prototype.slice.call(document.querySelectorAll("[data-shown-when]"));
  if (!dependents.length) { return; }
  document.querySelectorAll("[data-reveal]").forEach(function (element) {
    element.hidden = false;
  });
  function update() {
    dependents.forEach(function (control) {
      var wrapper = control.closest(".field") || control;
      var shown = wrapper.classList.contains("has-error") ||
        control.getAttribute("data-shown-when").split("|").some(holds);
      wrapper.hidden = !shown;
      control.disabled = !shown;
    });
  }
  document.addEventListener("input", update);
  document.addEventListener("change", update);
  update();
})();
