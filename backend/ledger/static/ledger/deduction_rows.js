// The lease end's deduction rows (docs/qa/PLAN-012f-a-design-follow-up.md D8): the eight
// rows of table#deduction-rows are rendered by the server. With this script the empty rows
// after the last filled one are hidden (the first row always shows), the dashed
// "+ Add deduction" button (#add-deduction) shows the next hidden row, and each row's trash
// button (.deduction-clear) clears that row's fields. Without the script every row shows
// and both controls stay hidden. Nothing is sent from here; the server checks every rule.
(function () {
  "use strict";
  var table = document.getElementById("deduction-rows");
  var add = document.getElementById("add-deduction");
  if (!table || !add) {
    return;
  }
  var rows = Array.prototype.slice.call(table.querySelectorAll("tbody tr.deduction-row"));
  function fields(row) {
    return Array.prototype.slice.call(row.querySelectorAll("input, select, textarea"));
  }
  function filled(row) {
    return fields(row).some(function (field) {
      return String(field.value || "").trim() !== "";
    }) || row.querySelector(".errorlist") !== null;
  }
  function refresh() {
    add.hidden = !rows.some(function (row) { return row.hidden; });
  }
  var last = 0;
  rows.forEach(function (row, index) {
    if (filled(row)) {
      last = index;
    }
  });
  rows.forEach(function (row, index) {
    row.hidden = index > last;
    var clear = row.querySelector("button.deduction-clear");
    if (!clear) {
      return;
    }
    clear.hidden = false;
    clear.addEventListener("click", function () {
      fields(row).forEach(function (field) {
        field.value = "";
      });
      table.dispatchEvent(new Event("input"));
    });
  });
  add.addEventListener("click", function () {
    var next = rows.filter(function (row) { return row.hidden; })[0];
    if (next) {
      next.hidden = false;
      var first = fields(next)[0];
      if (first) {
        first.focus();
      }
    }
    refresh();
  });
  refresh();
})();
