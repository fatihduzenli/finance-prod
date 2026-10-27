// Split into installments' live preview (PLAN-012f-c D13). The Plan card's fields (#split-form:
// the count, its − / + stepper, the first date, the cadence) are read again from the preview
// endpoint (ledger:plans:split_preview, read-only, same origin; its address and the charge in
// the form's data-preview-url and data-parent) a moment after they change; a success rebuilds
// the table of the confirm form (#confirm-form: one row per installment, its date and amount
// editable, the last marked when it carries the rounding cent), the plan's hidden values, the
// Summary ("n × share", the last payment date, the primary's count) and the large-debit tick;
// a refusal shows its sentence, empties the table and disables the primary. Editing a row's
// date or amount keeps the last date and the tick in step. Without this script the Preview
// button reloads the page with the table the server renders. Nothing is sent from here but
// the preview's reads; the confirm is the form's own POST.
(function () {
  "use strict";
  var form = document.getElementById("split-form");
  var confirmForm = document.getElementById("confirm-form");
  if (!form || !confirmForm) {
    return;
  }
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var count = document.getElementById("id_count");
  var first = document.getElementById("id_first_due");
  var echo = document.getElementById("first-due-text");
  var less = document.getElementById("count-less");
  var more = document.getElementById("count-more");
  var rows = document.getElementById("split-rows");
  var message = document.getElementById("split-preview-message");
  var tile = document.querySelector("#split-installments .kpi-value");
  var last = document.getElementById("split-last-value");
  var button = document.getElementById("split-confirm");
  var large = document.getElementById("split-large");
  var labels = Array.prototype.map.call(
    document.querySelectorAll("#split-lines thead th"),
    function (th) { return th.textContent.trim(); }
  );
  var min = parseInt(count.getAttribute("min"), 10);
  var max = parseInt(count.getAttribute("max"), 10);
  var timer = null;
  var asked = 0;
  // After a failed read the Preview button is back and reloads the page as without the script.
  var failed = false;

  function dayText(iso) {
    var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ""));
    if (!match) {
      return "";
    }
    return MONTHS[parseInt(match[2], 10) - 1] + " " + parseInt(match[3], 10) + ", " + match[1];
  }
  function money(text) {
    var value = Number(text);
    return "$" + value.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }
  function cadence() {
    var chosen = form.querySelector('input[name="cadence"]:checked');
    return chosen ? chosen.value : "";
  }
  function stepper() {
    var value = parseInt(count.value, 10);
    less.disabled = !isNaN(value) && value <= min;
    more.disabled = !isNaN(value) && value >= max;
  }
  function step(delta) {
    var value = parseInt(count.value, 10);
    value = isNaN(value) ? min : Math.min(max, Math.max(min, value + delta));
    count.value = String(value);
    count.dispatchEvent(new Event("input", { bubbles: true }));
  }
  function setHidden(id, value) {
    var input = document.getElementById(id);
    if (input) {
      input.value = value;
    }
  }
  function cell(row, label) {
    var td = document.createElement("td");
    td.setAttribute("data-label", label || "");
    row.appendChild(td);
    return td;
  }
  function input(type, name, value, label) {
    var field = document.createElement("input");
    field.type = type;
    field.name = name;
    field.value = value;
    field.required = true;
    field.setAttribute("aria-label", label);
    return field;
  }
  function largeCheck() {
    if (!large) {
      return;
    }
    var threshold = Number(large.getAttribute("data-threshold"));
    var over = Array.prototype.some.call(
      rows.querySelectorAll('input[name^="amount_"]'),
      function (field) { return Number(field.value) > threshold; }
    );
    large.hidden = !over;
  }
  function lastDate() {
    var dates = rows.querySelectorAll('input[name^="due_"]');
    var text = dates.length ? dayText(dates[dates.length - 1].value) : "";
    last.textContent = text || "—";
  }
  function show(data, chosen) {
    var lines = data.lines || [];
    var total = lines.length;
    rows.textContent = "";
    lines.forEach(function (line) {
      var row = document.createElement("tr");
      var rounding = line.number === total && data.cent_on_last;
      row.className = rounding ? "split-line is-rounding" : "split-line";
      cell(row, labels[0]).textContent = line.number + " of " + total;
      cell(row, labels[1]).appendChild(
        input("date", "due_" + line.number, line.due, "Installment " + line.number + " due date")
      );
      var amount = cell(row, labels[2]);
      var field = input("text", "amount_" + line.number, line.amount, "Installment " + line.number + " amount");
      field.inputMode = "decimal";
      field.maxLength = 10;
      amount.appendChild(field);
      if (rounding) {
        var note = document.createElement("span");
        note.className = "help muted split-rounding";
        note.id = "split-rounding";
        note.textContent = "Carries the rounding cent";
        amount.appendChild(document.createTextNode(" "));
        amount.appendChild(note);
      }
      rows.appendChild(row);
    });
    setHidden("confirm-count", String(total));
    setHidden("confirm-first-due", lines.length ? lines[0].due : "");
    setHidden("confirm-cadence", chosen);
    if (tile) {
      tile.textContent = total + " × " + money(data.share);
    }
    last.textContent = dayText(data.last_due) || "—";
    document.querySelectorAll(".split-count").forEach(function (span) {
      span.textContent = total + " ";
    });
    message.textContent = "";
    // The server's field errors were about the values before this answer.
    form.querySelectorAll(".errorlist").forEach(function (list) { list.remove(); });
    form.querySelectorAll(".has-error").forEach(function (field) {
      field.classList.remove("has-error");
    });
    button.disabled = total === 0;
    largeCheck();
  }
  function refuse(text) {
    rows.textContent = "";
    if (tile) {
      tile.textContent = "—";
    }
    last.textContent = "—";
    document.querySelectorAll(".split-count").forEach(function (span) {
      span.textContent = "";
    });
    message.textContent = text || "";
    button.disabled = true;
    if (large) {
      large.hidden = true;
    }
  }
  function preview() {
    if (echo) {
      echo.textContent = dayText(first ? first.value : "");
    }
    stepper();
    window.clearTimeout(timer);
    timer = window.setTimeout(function () {
      var chosen = cadence();
      var url = form.getAttribute("data-preview-url") +
        "?parent=" + encodeURIComponent(form.getAttribute("data-parent") || "") +
        "&count=" + encodeURIComponent(count.value) +
        "&first=" + encodeURIComponent(first ? first.value : "") +
        "&cadence=" + encodeURIComponent(chosen);
      var mine = ++asked;
      fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (response) { return response.json(); })
        .then(function (data) {
          if (mine !== asked) {
            return;  // a later change asked again
          }
          if (data && data.ok) {
            show(data, chosen);
          } else {
            refuse(data && data.message);
          }
        })
        .catch(function () {
          if (mine === asked) {
            failed = true;
            refuse("The preview could not be read. Use Preview to reload it.");
            document.getElementById("split-preview-row").hidden = false;
          }
        });
    }, 300);
  }

  less.hidden = false;
  more.hidden = false;
  less.addEventListener("click", function () { step(-1); });
  more.addEventListener("click", function () { step(1); });
  document.getElementById("split-preview-row").hidden = true;
  form.addEventListener("input", preview);
  form.addEventListener("change", preview);
  form.addEventListener("submit", function (event) {
    if (failed) {
      return;
    }
    event.preventDefault();
    preview();
  });
  rows.addEventListener("input", function () {
    lastDate();
    largeCheck();
  });
  stepper();
})();
