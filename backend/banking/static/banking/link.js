// Admin › Settings › Bank deposits' "Connect a bank account" (docs/qa/PLAN-014 I2;
// ledger/templates/ledger/settings/index.html). Plaid Link's script, loaded from Plaid's CDN
// just before this file (https://plaid.com/docs/link/web/), opens Plaid's own window, where the
// admin signs in to the business's bank and chooses one account; this page never sees the
// bank's credentials. The button asks the server for a link token (banking:link_token, POST,
// CSRF), opens Link with it and, on success, puts only the public token, the chosen account's
// id and the institution's name in the form's three hidden fields and posts the form to
// banking:link_exchange, where the server exchanges the public token. A failure shows the fixed
// sentence in #bank-link-error; nothing from Plaid is shown.
(function () {
  "use strict";
  var form = document.getElementById("bank-link-form");
  var button = document.getElementById("bank-connect");
  var error = document.getElementById("bank-link-error");
  var url = form ? form.getAttribute("data-token-url") : "";
  if (!form || !button || !url || !window.Plaid || typeof window.Plaid.create !== "function") {
    return;
  }
  var csrf = form.querySelector("input[name=csrfmiddlewaretoken]");
  function failed() {
    button.disabled = false;
    if (error) { error.hidden = false; }
  }
  function put(id, value) {
    var field = document.getElementById(id);
    if (field) { field.value = typeof value === "string" ? value : ""; }
  }
  button.addEventListener("click", function () {
    button.disabled = true;
    if (error) { error.hidden = true; }
    window.fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrf ? csrf.value : "" }
    }).then(function (response) {
      return response.ok ? response.json() : Promise.reject(new Error("refused"));
    }).then(function (data) {
      if (!data || typeof data.link_token !== "string") { failed(); return; }
      var handler = window.Plaid.create({
        token: data.link_token,
        onSuccess: function (publicToken, metadata) {
          var accounts = metadata && metadata.accounts ? metadata.accounts : [];
          put("bank-public-token", publicToken);
          put("bank-account-id", accounts.length === 1 ? accounts[0].id : "");
          put("bank-institution", metadata && metadata.institution ? metadata.institution.name : "");
          form.submit();
        },
        onExit: function () { button.disabled = false; }
      });
      handler.open();
    }).catch(failed);
  });
})();
