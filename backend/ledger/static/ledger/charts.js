// The bar charts' hover tooltip (PLAN-015 Batch 4 §3.3; the dashboard's Collections chart of
// PLAN-015 Batch 1 and the Profit and loss chart, finance/templatetags/charts.py). Each
// .collections-chart with data-tooltip-fields ("collected:Collected,due:Due": the bar's
// data-* attribute, then the word written before it) and a .chart-tooltip: hovering a bar
// (.collection-bar) shows the tooltip over it with the bar's data-label, then one line per
// field, the other bars dimmed (.is-hovering, .is-hover); leaving the chart hides it. Every
// text is set with textContent; a field's name is lower-case letters only, anything else is
// skipped. Without the script each bar's <title> is the tooltip.
(function () {
  "use strict";
  function line(word, value) {
    var row = document.createElement("span");
    row.appendChild(document.createTextNode(word));
    var figure = document.createElement("b");
    figure.textContent = value;
    row.appendChild(figure);
    return row;
  }
  function fieldsOf(chart) {
    var fields = [];
    (chart.getAttribute("data-tooltip-fields") || "").split(",").forEach(function (pair) {
      var at = pair.indexOf(":");
      var key = pair.slice(0, at);
      if (at > 0 && /^[a-z]+$/.test(key)) { fields.push({ key: key, word: pair.slice(at + 1) }); }
    });
    return fields;
  }
  function enhance(chart) {
    var tooltip = chart.querySelector(".chart-tooltip");
    var fields = fieldsOf(chart);
    if (!tooltip || !fields.length) { return; }
    Array.prototype.slice.call(chart.querySelectorAll(".collection-bar")).forEach(function (bar) {
      bar.addEventListener("mouseenter", function () {
        while (tooltip.firstChild) { tooltip.removeChild(tooltip.firstChild); }
        var head = document.createElement("strong");
        head.textContent = bar.getAttribute("data-label") || "";
        tooltip.appendChild(head);
        fields.forEach(function (field) {
          tooltip.appendChild(line(field.word, bar.getAttribute("data-" + field.key) || ""));
        });
        tooltip.hidden = false;
        var frame = chart.getBoundingClientRect();
        var box = bar.getBoundingClientRect();
        var left = box.left - frame.left + chart.scrollLeft + box.width / 2 - tooltip.offsetWidth / 2;
        var widest = chart.scrollWidth - tooltip.offsetWidth;
        tooltip.style.left = Math.max(0, Math.min(left, widest)) + "px";
        tooltip.style.top = Math.max(0, box.top - frame.top - tooltip.offsetHeight - 6) + "px";
        chart.classList.add("is-hovering");
        bar.classList.add("is-hover");
      });
      bar.addEventListener("mouseleave", function () { bar.classList.remove("is-hover"); });
    });
    chart.addEventListener("mouseleave", function () {
      tooltip.hidden = true;
      chart.classList.remove("is-hovering");
    });
  }
  Array.prototype.slice.call(document.querySelectorAll(".collections-chart[data-tooltip-fields]"))
    .forEach(enhance);
})();
