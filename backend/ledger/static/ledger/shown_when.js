// Dependent fields (docs/qa/PLAN-012c-design-pass.md D8): a control carrying
// data-shown-when is shown, with its .field wrapper, only while one of its "|"-separated
// conditions holds: "<id>" (a ticked box or a chosen radio, or an input with a value) or
// "<id>=<value>" (a select or input with that value); "&" joins conditions that must all
// hold ("collect-autopay&id_autopay_end_rule=count"). Hidden, it is also disabled, so it
// is not sent: what is saved is what the page shows. A field with an error from the
// server stays shown.
// Controls marked data-reveal (a tick that only drives this script) appear only when the
// script runs. Without the script every field stays visible and nothing is hidden; the
// server checks every rule either way.
// Dependent labels (docs/qa/PLAN-012e3-check-method.md D3): a control carrying
// data-label-when="<id>=<value>" and data-label="<text>" has its <label for> read that
// text while the condition holds, and its own text otherwise (a payment's reference reads
// "Check number" while Check is chosen). Without the script the label keeps its own text.
(function () {
  "use strict";
  function holds(condition, missing) {
    var parts = condition.split("=");
    var control = document.getElementById(parts[0]);
    if (!control) { return missing; }
    if (parts.length > 1) { return control.value === parts.slice(1).join("="); }
    if (control.type === "checkbox" || control.type === "radio") { return control.checked; }
    return String(control.value || "").trim() !== "";
  }
  var dependents = Array.prototype.slice.call(document.querySelectorAll("[data-shown-when]"));
  var labelled = [];
  document.querySelectorAll("[data-label-when]").forEach(function (control) {
    var label = control.id ? document.querySelector('label[for="' + control.id + '"]') : null;
    var text = label && label.firstChild;
    if (!text || text.nodeType !== Node.TEXT_NODE) { return; }
    labelled.push({ control: control, text: text, own: text.nodeValue });
  });
  if (!dependents.length && !labelled.length) { return; }
  document.querySelectorAll("[data-reveal]").forEach(function (element) {
    element.hidden = false;
  });
  function update() {
    dependents.forEach(function (control) {
      var wrapper = control.closest(".field") || control;
      var shown = wrapper.classList.contains("has-error") ||
        control.getAttribute("data-shown-when").split("|").some(function (alternative) {
          return alternative.split("&").every(function (condition) {
            return holds(condition, true);
          });
        });
      wrapper.hidden = !shown;
      control.disabled = !shown;
    });
    labelled.forEach(function (entry) {
      var switched = holds(entry.control.getAttribute("data-label-when"), false);
      entry.text.nodeValue = switched ? entry.control.getAttribute("data-label") : entry.own;
    });
  }
  document.addEventListener("input", update);
  document.addEventListener("change", update);
  update();
})();
