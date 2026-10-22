// The line rows of an invoice draft (docs/qa/PLAN-012 D6; PLAN-012b D2; PLAN-012d D10).
// The form is a plain post and works without this script: it shows its rows with a Save
// as catalogue item box and a Remove box on each. Here:
// - one Item box per row on the form's one list: "Rent", then the lease's existing open
//   charges, then the catalogue items; any other text is a typed item;
// - Rent shows the row's lease and month selects; once both are set, the unit amount is
//   filled at once from the rent-amount endpoint (read-only, same origin) and stays
//   editable; its proration text, or why the month cannot be billed, shows under the box;
// - an existing open charge names the charge in the row's hidden field, fills the unit
//   amount with its balance and makes it read-only (the charge is billed at its balance;
//   PLAN-012d D10 amended; the server refuses any other amount without this script); any
//   other text clears the field and the amount is editable again;
// - the Save box shows only on a typed item that is not on the list (case and surrounding
//   spaces ignored); the server ignores it on a listed name either way;
// - the trash button ticks the row's Remove box and hides the row; the visible rows are
//   numbered 1, 2, ... (each control's hidden label says its line: "Line 2, quantity",
//   "Removed line, quantity") and the invoice total under the table is kept live;
// - "Add line" copies the last row, blank, under the next index of the management form
//   (at most its MAX_NUM_FORMS), its labels' for renamed with its controls' ids;
// - the "How to collect" buttons name the live total (.collect-amount) and the Schedule
//   button the debit date (.collect-date; PLAN-012e-2 D3).
// Nothing is sent from here but the read of the rent amount.
(function () {
  "use strict";
  var table = document.getElementById("draft-lines");
  var button = document.getElementById("add-line");
  var total = document.getElementById("id_lines-TOTAL_FORMS");
  var max = document.getElementById("id_lines-MAX_NUM_FORMS");
  var totalOut = document.getElementById("lines-total-value");
  if (!table || !button || !total) {
    return;
  }
  var body = table.querySelector("tbody");
  var amountUrl = table.dataset.rentAmountUrl || "";

  function key(text) {
    return String(text || "").trim().toLowerCase();
  }

  function option(input) {
    var list = input.list;
    if (!list) {
      return null;
    }
    var wanted = key(input.value);
    return Array.prototype.find.call(list.options, function (entry) {
      return key(entry.value) === wanted;
    }) || null;
  }

  function cents(text) {
    var value = Number(String(text || "").trim());
    return String(text || "").trim() !== "" && isFinite(value) && value > 0 ? value : 0;
  }

  function money(value) {
    return "$" + value.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function rows() {
    return Array.prototype.slice.call(body.querySelectorAll("tr.draft-line"));
  }

  function name(row, text) {
    row.querySelectorAll(".line-name").forEach(function (span) {
      span.textContent = text;
    });
  }

  function refresh() {
    var number = 0;
    var sum = 0;
    rows().forEach(function (row) {
      var cell = row.querySelector("td.line-number");
      if (row.hidden) {
        if (cell) {
          cell.textContent = "";
        }
        name(row, "Removed line");
        return;
      }
      number += 1;
      if (cell) {
        cell.textContent = String(number);
      }
      name(row, "Line " + number);
      var unit = cents((row.querySelector("input[name$='-unit_amount']") || {}).value);
      var quantityInput = row.querySelector("input[name$='-quantity']");
      var quantity = quantityInput && quantityInput.value.trim() !== "" ? cents(quantityInput.value) : 1;
      sum += Math.round(unit * quantity * 100) / 100;
    });
    if (totalOut) {
      totalOut.textContent = money(sum);
    }
    document.querySelectorAll(".collect-amount").forEach(function (span) {
      span.textContent = money(sum);
    });
  }

  function field(row, suffix) {
    return row.querySelector("[name$='-" + suffix + "']");
  }

  function fetchRent(row) {
    var lease = field(row, "rent_lease");
    var month = field(row, "rent_month");
    var unit = field(row, "unit_amount");
    var note = row.querySelector(".rent-text");
    if (!amountUrl || !lease || !month || !unit || !lease.value || !month.value) {
      return;
    }
    var url = amountUrl + (amountUrl.indexOf("?") === -1 ? "?" : "&") +
      "lease=" + encodeURIComponent(lease.value) + "&month=" + encodeURIComponent(month.value);
    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (data) {
        if (!data) {
          return;
        }
        if (data.amount !== null && data.amount !== undefined) {
          unit.value = data.amount;
        }
        if (note) {
          note.textContent = data.amount !== null ? (data.text || "") : (data.message || "");
        }
        refresh();
      })
      .catch(function () { /* the server fills an empty amount on save */ });
  }

  function watch(row) {
    var input = field(row, "item");
    var hidden = field(row, "existing_charge");
    var save = row.querySelector("input[type=checkbox][name$='-save_item']");
    var remove = row.querySelector("input[type=checkbox][name$='-DELETE']");
    var trash = row.querySelector("button.line-trash");
    var plain = row.querySelector(".no-js-remove");
    var unit = field(row, "unit_amount");
    var note = row.querySelector(".rent-text");
    var lastExisting = hidden ? hidden.value : "";

    function update(fromTyping) {
      var chosen = input ? option(input) : null;
      var kind = chosen ? chosen.dataset.kind || "item" : (input && key(input.value) !== "" ? "typed" : "");
      row.querySelectorAll("td.rent-field").forEach(function (cell) {
        cell.style.visibility = kind === "rent" ? "" : "hidden";
      });
      if (save) {
        var cell = save.closest("td") || save;
        cell.style.visibility = kind === "typed" ? "" : "hidden";
        if (kind !== "typed") {
          save.checked = false;
        }
      }
      if (hidden) {
        var charge = kind === "existing" ? chosen.dataset.charge : "";
        if (kind === "existing" || fromTyping) {
          hidden.value = charge;
        }
        if (unit) {
          if (kind === "existing") {
            unit.value = chosen.dataset.balance;
            unit.readOnly = true;
          } else if (unit.readOnly) {
            unit.readOnly = false;
            if (fromTyping && lastExisting !== "") {
              unit.value = "";
            }
          }
        }
        lastExisting = hidden.value;
      }
      if (note && kind !== "rent") {
        note.textContent = "";
      }
      if (fromTyping && kind === "rent") {
        fetchRent(row);
      }
      refresh();
    }

    if (input) {
      input.addEventListener("input", function () { update(true); });
      input.addEventListener("change", function () { update(true); });
    }
    ["rent_lease", "rent_month"].forEach(function (suffix) {
      var select = field(row, suffix);
      if (select) {
        select.addEventListener("change", function () { fetchRent(row); });
      }
    });
    row.addEventListener("input", refresh);
    if (remove && trash) {
      if (plain) {
        plain.hidden = true;
      }
      trash.hidden = false;
      trash.addEventListener("click", function () {
        remove.checked = true;
        row.hidden = true;
        refresh();
      });
    } else if (remove) {
      remove.addEventListener("change", function () {
        row.hidden = remove.checked;
        refresh();
      });
    }
    update(false);
  }

  var debitOn = document.getElementById("id_schedule_on");
  if (debitOn) {
    debitOn.addEventListener("input", function () {
      document.querySelectorAll(".collect-date").forEach(function (span) {
        span.textContent = debitOn.value || span.dataset.empty || "";
      });
    });
  }

  rows().forEach(watch);
  refresh();
  button.hidden = false;
  button.addEventListener("click", function () {
    var all = rows();
    var count = parseInt(total.value, 10);
    var limit = max ? parseInt(max.value, 10) : Infinity;
    if (!all.length || !(count < limit)) {
      return;
    }
    var row = all[all.length - 1].cloneNode(true);
    row.hidden = false;
    row.querySelectorAll("input, textarea, select").forEach(function (input) {
      ["name", "id"].forEach(function (attribute) {
        var value = input.getAttribute(attribute);
        if (value) {
          input.setAttribute(attribute, value.replace(/lines-\d+-/, "lines-" + count + "-"));
        }
      });
      if (input.type === "checkbox") {
        input.checked = false;
      } else if (input.tagName === "SELECT") {
        // The month starts empty; the lease keeps the row's choice (the draft's lease).
        if (/-rent_month$/.test(input.name)) {
          input.value = "";
        }
      } else {
        input.value = "";
        input.readOnly = false;
      }
    });
    row.querySelectorAll("label[for]").forEach(function (label) {
      label.setAttribute("for", label.getAttribute("for").replace(/lines-\d+-/, "lines-" + count + "-"));
    });
    var note = row.querySelector(".rent-text");
    if (note) {
      note.textContent = "";
    }
    body.appendChild(row);
    watch(row);
    total.value = String(count + 1);
    refresh();
  });
})();
