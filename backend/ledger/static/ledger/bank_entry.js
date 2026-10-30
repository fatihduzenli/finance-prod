// The bank-entry dialog's hosted fields (docs/qa/PLAN-014 C7;
// ledger/templates/ledger/leases/bank_entry.html). Stax.js, loaded from Stax's CDN just
// before this file, shows the account holder, routing and account fields inside its own
// frames in #bank-holder-field, #bank-routing-field and #bank-account-field; this page never
// renders or reads those values (Rule 1). On Save, Stax.js tokenizes the bank account under
// the lease's customer (data-customer) and returns the new payment method; only its id is
// put in the form's one field, payment_method_id, and the form is posted. A refusal shows the
// fixed sentence in #bank-entry-error; nothing from the processor is shown.
//
// TODO-VERIFY-066: the constructor's name (StaxJs, formerly FattJs), its field options for a
// bank account and the tokenize details are taken from Stax's Stax.js documentation as
// understood, not verified here; the sandbox cannot tokenize a bank account.
(function () {
  "use strict";
  var form = document.getElementById("bank-entry-form");
  var fields = document.getElementById("bank-entry-fields");
  var save = document.getElementById("bank-entry-save");
  var error = document.getElementById("bank-entry-error");
  var method = document.getElementById("bank-entry-method");
  var Hosted = window.StaxJs || window.FattJs;
  if (!form || !fields || !save || !method || typeof Hosted !== "function") { return; }
  var hosted = new Hosted(fields.getAttribute("data-token"), {
    method: "bank",
    person_name: { id: "bank-holder-field" },
    bank_routing: { id: "bank-routing-field" },
    bank_account: { id: "bank-account-field" }
  });
  var busy = false;
  function failed() {
    busy = false;
    save.disabled = false;
    if (error) { error.hidden = false; }
  }
  Promise.resolve(hosted.showCardForm ? hosted.showCardForm() : null).then(function () {
    save.disabled = false;
  }, failed);
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (busy) { return; }
    busy = true;
    save.disabled = true;
    if (error) { error.hidden = true; }
    hosted.tokenize({
      method: "bank",
      customer_id: fields.getAttribute("data-customer")
    }).then(function (paymentMethod) {
      var id = paymentMethod && typeof paymentMethod.id === "string" ? paymentMethod.id : "";
      if (!id) { failed(); return; }
      method.value = id;
      form.submit();
    }, failed);
  });
})();
