// The New lease page (docs/qa/PLAN-014b item 1, Fable's decisions 3 to 5), in the browser only;
// the server checks every rule and nothing is sent from here.
// - Operator: #operator-search filters the options of the operator select #id_operator (an
//   option's text holds the search's words); the select shows as a list while searching and
//   gives way to the chosen operator's #operator-card (initials, name, then "phone · e-mail ·
//   <n> other stores: #…" or "No other store", from the option's data-* attributes). The
//   Billing details card (#billing-card) shows once an operator is chosen.
// - Terms: #prorate-line reads "Nov 15–30: 16 of 30 days = $1,066.67" from the start date and
//   the rent: the days from the start through the month's last day over the days in the month,
//   times the rent, rounded half up to the cent (ledger/services/proration.py, in whole cents);
//   hidden when the start is the 1st, blank, the rent is not an amount or the switch is off.
//   #deposit-mirror shows the Deposit typed in Terms in the Move-in charges list.
// - "+ Add item" (#add-item) reveals the next hidden row of the other fee types (rows marked
//   data-extra, hidden here; without the script every row shows).
// - The Summary (aside#lease-summary): operator, start, rent, who collects ("Autopay · 1st"),
//   the first month (prorated or the rent), the move-in charges (every priced row, the deposit
//   included) and the first invoice's total.
(function () {
  "use strict";
  var form = document.getElementById("lease-form");
  if (!form) { return; }
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var NONE = "—";
  function byId(id) { return document.getElementById(id); }
  function setText(id, text) { var node = byId(id); if (node) { node.textContent = text; } }

  // Money in whole cents: "2000", "2000.5" and "2,000.00" read as cents; anything else is null.
  function cents(text) {
    var value = String(text || "").trim().replace(/,/g, "");
    var match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(value);
    if (!match) { return null; }
    return Number(match[1]) * 100 + Number(((match[2] || "") + "00").slice(0, 2));
  }
  function grouped(whole) { return String(whole).replace(/\B(?=(\d{3})+(?!\d))/g, ","); }
  function plain(value) { return grouped(Math.floor(value / 100)) + "." + String(value % 100).padStart(2, "0"); }
  function money(value) { return "$" + plain(value); }
  function ordinal(day) {
    var tens = day % 100;
    if (tens >= 11 && tens <= 13) { return day + "th"; }
    return day + ({ 1: "st", 2: "nd", 3: "rd" }[day % 10] || "th");
  }
  function parseDay(text) {
    var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(text || ""));
    if (!match) { return null; }
    var year = Number(match[1]), month = Number(match[2]), day = Number(match[3]);
    var last = new Date(Date.UTC(year, month, 0)).getUTCDate();
    if (month < 1 || month > 12 || day < 1 || day > last) { return null; }
    return { year: year, month: month, day: day, last: last };
  }
  // proration.prorated: rent × used ÷ days, rounded half up to the cent, in integers.
  function prorated(rent, used, days) {
    var numerator = rent * used;
    var whole = Math.floor(numerator / days);
    return 2 * (numerator - whole * days) >= days ? whole + 1 : whole;
  }

  var select = byId("id_operator");
  var search = byId("operator-search");
  var searchField = byId("operator-search-field");
  var listField = byId("operator-list-field");
  var card = byId("operator-card");
  var billing = byId("billing-card");
  function chosenOption() {
    if (!select || select.selectedIndex < 0) { return null; }
    var option = select.options[select.selectedIndex];
    return option && option.value ? option : null;
  }
  function fillCard() {
    var option = chosenOption();
    if (billing) { billing.hidden = !option; }
    if (!card) { return; }
    card.hidden = !option;
    if (!option) { return; }
    var count = Number(option.getAttribute("data-store-count") || "0");
    var stores = option.getAttribute("data-stores") || "";
    var parts = [option.getAttribute("data-phone-text"), option.getAttribute("data-email")]
      .filter(function (part) { return part; });
    parts.push(count ? count + (count === 1 ? " other store: " : " other stores: ") + stores : "No other store");
    setText("operator-card-initials", option.getAttribute("data-initials") || "");
    setText("operator-card-name", option.textContent.trim());
    setText("operator-card-line", parts.join(" · "));
  }
  function showList(shown) {
    if (!listField || !search) { return; }
    listField.hidden = !shown;
  }
  function filterOptions() {
    var words = search.value.trim().toLowerCase().split(/\s+/).filter(function (word) { return word; });
    var shown = 0;
    Array.prototype.forEach.call(select.options, function (option) {
      var text = option.textContent.toLowerCase();
      var match = !option.value || words.every(function (word) { return text.indexOf(word) !== -1; });
      option.hidden = !match;
      if (match && option.value) { shown += 1; }
    });
    select.size = Math.min(Math.max(shown + 1, 2), 8);
  }
  if (select && search && searchField) {
    searchField.hidden = false;
    var chosen = chosenOption();
    search.value = chosen ? chosen.textContent.trim() : "";
    showList(!chosen);
    if (!chosen) { filterOptions(); }
    search.addEventListener("input", function () { filterOptions(); showList(true); });
    search.addEventListener("focus", function () { search.select(); });
    search.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") { event.preventDefault(); showList(true); select.focus(); }
    });
    select.addEventListener("change", function () {
      var option = chosenOption();
      if (option) { search.value = option.textContent.trim(); showList(false); }
    });
  }
  if (select) { select.addEventListener("change", fillCard); }
  fillCard();

  var addItem = byId("add-item");
  var extras = Array.prototype.slice.call(form.querySelectorAll(".movein-row[data-extra]"));
  extras.forEach(function (row) { row.hidden = true; });
  if (addItem && extras.length) {
    addItem.hidden = false;
    addItem.addEventListener("click", function () {
      var next = extras.filter(function (row) { return row.hidden; })[0];
      if (next) {
        next.hidden = false;
        var input = next.querySelector("input");
        if (input) { input.focus(); }
      }
      addItem.hidden = !extras.some(function (row) { return row.hidden; });
    });
  }

  var start = byId("id_started_on");
  var rent = byId("id_rent");
  var prorate = byId("id_prorate_first_month");
  var line = byId("prorate-line");
  var mirror = byId("deposit-mirror");
  var deposit = mirror ? byId(mirror.getAttribute("for")) : null;
  var debitDay = byId("id_debit_day");
  var secondDay = byId("id_second_debit_day");

  function firstMonth() {
    var amount = cents(rent && rent.value);
    if (amount === null || amount <= 0) { return { amount: null, text: "" }; }
    var day = parseDay(start && start.value);
    if (!day || day.day === 1 || !(prorate && prorate.checked)) { return { amount: amount, text: "" }; }
    var used = day.last - day.day + 1;
    var value = prorated(amount, used, day.last);
    var span = day.day === day.last ? String(day.day) : day.day + "–" + day.last;
    return {
      amount: value,
      text: MONTHS[day.month - 1] + " " + span + ": " + used + " of " + day.last + " days = " + money(value)
    };
  }
  function moveIn() {
    var sum = 0;
    form.querySelectorAll("input.js-total").forEach(function (input) {
      var row = input.closest(".movein-row");
      if (input.disabled || (row && row.hidden)) { return; }
      var value = cents(input.value);
      if (value !== null) { sum += value; }
    });
    return sum;
  }
  function collectedBy() {
    var channel = form.querySelector('input[name="payment_channel"]:checked');
    var days = [];
    if (debitDay && debitDay.value) { days.push(ordinal(Number(debitDay.value))); }
    if (secondDay && !secondDay.disabled && /^\d+$/.test(secondDay.value.trim())) {
      days.push(ordinal(Number(secondDay.value)));
    }
    var parts = [];
    if (channel) { parts.push(channel.getAttribute("data-short")); }
    if (days.length) { parts.push(days.join(" and ")); }
    return parts.join(" · ") || NONE;
  }
  function update() {
    var first = firstMonth();
    if (line) {
      line.textContent = first.text;
      line.hidden = !first.text;
    }
    if (mirror) {
      var typed = cents(deposit && !deposit.disabled ? deposit.value : "");
      mirror.textContent = typed === null ? NONE : plain(typed);
    }
    var option = chosenOption();
    setText("summary-operator", option ? option.textContent.trim() : NONE);
    var day = parseDay(start && start.value);
    setText("summary-starts", day ? MONTHS[day.month - 1] + " " + day.day + ", " + day.year : NONE);
    var monthly = cents(rent && rent.value);
    setText("summary-rent", monthly === null ? NONE : money(monthly));
    setText("summary-collected", collectedBy());
    setText("summary-first-month-label", first.text ? "First month (prorated)" : "First month");
    setText("summary-first-month", first.amount === null ? NONE : money(first.amount));
    var charges = moveIn();
    setText("setup-total", money(charges));
    setText("summary-total", first.amount === null ? NONE : money(first.amount + charges));
  }
  form.addEventListener("input", update);
  form.addEventListener("change", update);
  if (addItem) { addItem.addEventListener("click", update); }
  update();
})();
