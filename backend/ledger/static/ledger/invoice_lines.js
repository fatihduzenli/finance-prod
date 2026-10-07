// Add and remove the line rows of an invoice draft (docs/qa/PLAN-012 D6). The form is a
// plain post and works without this script: it shows a fixed number of blank rows and a
// Save as catalogue item box and a Remove box on each row. Here "Add line" copies the
// last row, blank, under the next index of the management form (at most its
// MAX_NUM_FORMS); the copy keeps the Item box's list attribute, so every row offers the
// form's one list of catalogue names (PLAN-012b D2). Ticking Remove hides the row (its
// box stays ticked, so the server drops it). The Save box is shown only while the Item
// box holds a name that is not on the list (case and surrounding spaces ignored); the
// server ignores it on a listed name either way. Nothing is sent from here.
(function () {
  "use strict";
  var table = document.getElementById("draft-lines");
  var button = document.getElementById("add-line");
  var total = document.getElementById("id_lines-TOTAL_FORMS");
  var max = document.getElementById("id_lines-MAX_NUM_FORMS");
  if (!table || !button || !total) {
    return;
  }
  var body = table.querySelector("tbody");

  function key(text) {
    return text.trim().toLowerCase();
  }

  function listed(input) {
    var list = input.list;
    if (!list) {
      return false;
    }
    var wanted = key(input.value);
    return Array.prototype.some.call(list.options, function (option) {
      return key(option.value) === wanted;
    });
  }

  function watchSave(row) {
    var input = row.querySelector("input[name$='-item']");
    var box = row.querySelector("input[type=checkbox][name$='-save_item']");
    if (!input || !box) {
      return;
    }
    var cell = box.closest("td") || box;
    function update() {
      var typed = key(input.value) !== "" && !listed(input);
      cell.style.visibility = typed ? "" : "hidden";
      if (!typed) {
        box.checked = false;
      }
    }
    input.addEventListener("input", update);
    input.addEventListener("change", update);
    update();
  }

  function watchRemove(row) {
    var box = row.querySelector("input[type=checkbox][name$='-DELETE']");
    if (!box) {
      return;
    }
    box.addEventListener("change", function () {
      row.hidden = box.checked;
    });
  }

  function watch(row) {
    watchRemove(row);
    watchSave(row);
  }

  Array.prototype.forEach.call(body.querySelectorAll("tr.draft-line"), watch);
  button.hidden = false;
  button.addEventListener("click", function () {
    var rows = body.querySelectorAll("tr.draft-line");
    var count = parseInt(total.value, 10);
    var limit = max ? parseInt(max.value, 10) : Infinity;
    if (!rows.length || !(count < limit)) {
      return;
    }
    var row = rows[rows.length - 1].cloneNode(true);
    row.hidden = false;
    row.querySelectorAll("input, textarea").forEach(function (field) {
      ["name", "id"].forEach(function (attribute) {
        var value = field.getAttribute(attribute);
        if (value) {
          field.setAttribute(attribute, value.replace(/lines-\d+-/, "lines-" + count + "-"));
        }
      });
      if (field.type === "checkbox") {
        field.checked = false;
      } else {
        field.value = "";
      }
    });
    body.appendChild(row);
    watch(row);
    total.value = String(count + 1);
  });
})();
