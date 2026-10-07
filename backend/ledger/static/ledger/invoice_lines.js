// Add and remove the line rows of an invoice draft (docs/qa/PLAN-012 D6). The form is a
// plain post and works without this script: it shows a fixed number of blank rows and a
// Remove box on each row. Here "Add line" copies the last row, blank, under the next
// index of the management form (at most its MAX_NUM_FORMS), and ticking Remove hides
// the row (its box stays ticked, so the server drops it). Nothing is sent from here.
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

  function watchRemove(row) {
    var box = row.querySelector("input[type=checkbox][name$='-DELETE']");
    if (!box) {
      return;
    }
    box.addEventListener("change", function () {
      row.hidden = box.checked;
    });
  }

  Array.prototype.forEach.call(body.querySelectorAll("tr.draft-line"), watchRemove);
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
    row.querySelectorAll("input, select, textarea").forEach(function (field) {
      ["name", "id"].forEach(function (attribute) {
        var value = field.getAttribute(attribute);
        if (value) {
          field.setAttribute(attribute, value.replace(/lines-\d+-/, "lines-" + count + "-"));
        }
      });
      if (field.type === "checkbox") {
        field.checked = false;
      } else if (field.tagName === "SELECT") {
        field.selectedIndex = 0;
      } else {
        field.value = "";
      }
    });
    body.appendChild(row);
    watchRemove(row);
    total.value = String(count + 1);
  });
})();
