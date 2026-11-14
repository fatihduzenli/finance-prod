// The New lease page (docs/qa/PLAN-014b item 1, Fable's decisions 3 to 5), in the browser only;
// the server checks every rule and nothing is sent from here.
// - Operator: #operator-search filters the options of the operator select #id_operator (an
//   option's text holds the search's words); the select opens as a list while searching, stays
//   in the page as one line once chosen, and the chosen operator fills #operator-card
//   (initials, name, then "phone · e-mail · <n> other stores: #…" or "No other store", from
//   the option's data-* attributes), on the
//   select's change (whatever chose it) and on load (?operator=). The Billing details' fields
//   follow the select through ledger/shown_when.js.
// - Terms: #prorate-line reads "Nov 15–30: 16 of 30 days = $1,066.67" from the start date and
//   the rent: the days from the start through the month's last day over the days in the month,
//   times the rent, rounded half up to the cent (ledger/services/proration.py, in whole cents);
//   hidden when the start is the 1st, blank, the rent is not an amount or the switch is off.
//   #deposit-mirror shows the Deposit typed in Terms in the Move-in charges list.
// - "+ Add item" (#add-item) reveals the next hidden row of the other fee types (rows marked
//   data-extra, hidden here; without the script every row shows).
// - The Summary (aside#lease-summary): operator, start, rent, who collects ("Autopay · 1st"),
//   the first month (prorated or the rent), the move-in charges (every priced row, the deposit
//   included) and the first invoice's total; its last line #summary-after reads "Starts Nov 15,
//   2026, activated on that day." for a start after the aside's data-today (PLAN-015b §9.1:
//   the lease saves prospective), else its data-default. The first month's free rent
//   (#summary-free-rent, PLAN-015d: ledger/services/free_rent.first_month_concession) is the
//   free days of the days the first month bills over those days times its amount, rounded half
//   up to the cent, the whole amount when every billed day is free, none when the days start
//   the month after; the total is net of it.
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
  var card = byId("operator-card");
  function chosenOption() {
    if (!select || select.selectedIndex < 0) { return null; }
    var option = select.options[select.selectedIndex];
    return option && option.value ? option : null;
  }
  function fillCard() {
    var option = chosenOption();
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
  // The select stays in the page (a keyboard or any other path chooses through it): one line,
  // the browser's own select, whenever the search is empty (on load, after choosing, after
  // clearing); the filtered list only while the search holds text.
  function closeList() {
    select.size = 0;
    Array.prototype.forEach.call(select.options, function (option) { option.hidden = false; });
  }
  function filterOptions() {
    var words = search.value.trim().toLowerCase().split(/\s+/).filter(function (word) { return word; });
    if (!words.length) { closeList(); return; }
    var shown = 0;
    Array.prototype.forEach.call(select.options, function (option) {
      var text = option.textContent.toLowerCase();
      var match = !option.value || words.every(function (word) { return text.indexOf(word) !== -1; });
      option.hidden = !match;
      if (match && option.value) { shown += 1; }
    });
    select.size = Math.min(shown + 1, 8);
  }
  if (select && search && searchField) {
    searchField.hidden = false;
    var chosen = chosenOption();
    search.value = chosen ? chosen.textContent.trim() : "";
    closeList();
    search.addEventListener("input", filterOptions);
    search.addEventListener("focus", function () { search.select(); });
    search.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown" && select.size > 1) { event.preventDefault(); select.focus(); }
    });
    select.addEventListener("change", function () {
      var option = chosenOption();
      search.value = option ? option.textContent.trim() : "";
      closeList();
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
  var summary = byId("lease-summary");
  var rent = byId("id_rent");
  var prorate = byId("id_prorate_first_month");
  var line = byId("prorate-line");
  var debitDay = byId("id_debit_day");
  var secondDay = byId("id_second_debit_day");
  var freeDays = byId("id_free_rent_days");
  var freeFrom = byId("id_free_rent_from");

  function freeRent(first) {
    if (first.amount === null || !freeDays || !freeFrom) { return 0; }
    var days = /^\d+$/.test(freeDays.value.trim()) ? Number(freeDays.value.trim()) : 0;
    var day = parseDay(start && start.value);
    if (!day || days <= 0 || freeFrom.value === "next_month") { return 0; }
    var billedFrom = prorate && prorate.checked && day.day > 1 ? day.day : 1;
    var billed = day.last - billedFrom + 1;
    var free = Math.min(day.day + days - 1, day.last) - day.day + 1;
    if (free >= billed) { return first.amount; }
    return prorated(first.amount, free, billed);
  }

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
  // PLAN-015j §4: a row due on a date or in installments is not on the first invoice.
  function offInvoice(row) {
    var when = row ? row.querySelector('select[name$="_when"]') : null;
    return Boolean(when && when.value && when.value !== "with_invoice");
  }
  function moveIn() {
    var sum = 0;
    form.querySelectorAll("input.js-total").forEach(function (input) {
      var row = input.closest(".movein-row");
      if (input.disabled || (row && row.hidden)) { return; }
      if (offInvoice(row)) { return; }
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
    var option = chosenOption();
    setText("summary-operator", option ? option.textContent.trim() : NONE);
    var day = parseDay(start && start.value);
    setText("summary-starts", day ? MONTHS[day.month - 1] + " " + day.day + ", " + day.year : NONE);
    var after = byId("summary-after");
    if (after) {
      var later = day && summary && start.value > summary.getAttribute("data-today");
      after.textContent = later
        ? "Starts " + MONTHS[day.month - 1] + " " + day.day + ", " + day.year + ", activated on that day."
        : after.getAttribute("data-default");
    }
    var monthly = cents(rent && rent.value);
    setText("summary-rent", monthly === null ? NONE : money(monthly));
    setText("summary-collected", collectedBy());
    setText("summary-first-month-label", first.text ? "First month (prorated)" : "First month");
    setText("summary-first-month", first.amount === null ? NONE : money(first.amount));
    var charges = moveIn();
    var free = freeRent(first);
    var freeRow = byId("summary-free-rent-row");
    if (freeRow) { freeRow.hidden = free <= 0; freeRow.style.display = free > 0 ? "" : "none"; }
    setText("summary-free-rent", free > 0 ? "-" + money(free) : "");
    setText("setup-total", money(charges));
    setText("summary-total", first.amount === null ? NONE : money(first.amount - free + charges));
  }
  form.addEventListener("input", update);
  form.addEventListener("change", update);
  if (addItem) { addItem.addEventListener("click", update); }
  update();
})();
