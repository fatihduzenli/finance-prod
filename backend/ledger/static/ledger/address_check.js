// "Check address" in the New / Edit store dialog (docs/qa/PLAN-012f-b-staging-review.md D13;
// ledger/templates/ledger/stores/_form_dialog.html). A click posts Street, City, State and
// ZIP with the form's CSRF token to the route named by the button's data-url, then shows in
// #address-check the address USPS standardised, with "Use this" (fills Street, City, State,
// by choosing the state's option, and ZIP as zip5-zip4) and "Keep mine" (clears the box);
// when USPS has no answer, the reason as a muted line. Every text is set with textContent.
// Nothing here runs on Save, and Save never waits for it.
(function () {
  "use strict";
  var button = document.getElementById("check-address");
  var box = document.getElementById("address-check");
  if (!button || !box || !button.form) { return; }
  var form = button.form;
  var url = button.getAttribute("data-url");
  var asked = 0;
  function field(name) { return form.elements.namedItem(name); }
  function value(name) {
    var element = field(name);
    return element && "value" in element ? element.value : "";
  }
  function line(text, className) {
    var item = document.createElement("p");
    if (className) { item.className = className; }
    item.textContent = text;
    return item;
  }
  function action(text, onClick) {
    var control = document.createElement("button");
    control.type = "button";
    control.className = "btn btn-sm";
    control.textContent = text;
    control.addEventListener("click", onClick);
    return control;
  }
  function chooseState(code) {
    var select = field("state");
    if (!select || !select.options) { return; }
    for (var i = 0; i < select.options.length; i += 1) {
      if (select.options[i].value === code) { select.selectedIndex = i; return; }
    }
  }
  function use(address) {
    var street = field("address_line1");
    var city = field("city");
    var zip = field("postal_code");
    if (street) { street.value = address.street; }
    if (city) { city.value = address.city; }
    chooseState(address.state);
    if (zip) { zip.value = address.zip4 ? address.zip5 + "-" + address.zip4 : address.zip5; }
    box.textContent = "";
  }
  function show(body) {
    box.textContent = "";
    if (!body || body.verified !== true || !body.address) {
      box.appendChild(line((body && body.reason) || "The address service did not answer.", "muted"));
      return;
    }
    var address = body.address;
    var zip = address.zip4 ? address.zip5 + "-" + address.zip4 : address.zip5;
    box.appendChild(line("USPS suggests:", "muted"));
    box.appendChild(line(address.street));
    box.appendChild(line(address.city + ", " + address.state + " " + zip));
    if (!address.dpv_confirmed) {
      box.appendChild(line("USPS could not confirm every part of this address.", "muted"));
    }
    var actions = document.createElement("div");
    actions.className = "address-check-actions";
    actions.appendChild(action("Use this", function () { use(address); }));
    actions.appendChild(action("Keep mine", function () { box.textContent = ""; }));
    box.appendChild(actions);
  }
  button.addEventListener("click", function () {
    var ticket = ++asked;
    var token = field("csrfmiddlewaretoken");
    var data = new URLSearchParams();
    data.append("street", value("address_line1"));
    data.append("city", value("city"));
    data.append("state", value("state"));
    data.append("zip", value("postal_code"));
    box.textContent = "";
    box.appendChild(line("Checking…", "muted"));
    fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Accept": "application/json",
        "X-CSRFToken": token && "value" in token ? token.value : ""
      },
      body: data
    }).then(function (response) {
      return response.json().catch(function () { return null; });
    }).then(function (body) {
      if (ticket === asked) { show(body); }
    }).catch(function () {
      if (ticket === asked) { show(null); }
    });
  });
})();
