// The help pages' topic filter (PLAN-012f-b E-3, DESIGN-SPEC §4 Help): the search field of the
// topic card (#help-search, hidden in the markup) shows itself, and as one types it hides the
// topics whose title does not hold the typed words (each word, any case). It reads and
// compares textContent only, writes nothing but the hidden flags, and sends nothing. Without
// the script the field stays hidden and every topic shows.
(function () {
  "use strict";
  var field = document.getElementById("help-search");
  var list = document.getElementById("help-topics");
  var none = document.getElementById("help-no-match");
  if (!field || !list) { return; }
  var items = Array.prototype.slice.call(list.querySelectorAll("li"));
  function filter() {
    var words = field.value.toLowerCase().split(/\s+/).filter(Boolean);
    var shown = 0;
    items.forEach(function (item) {
      var title = item.textContent.toLowerCase();
      var match = words.every(function (word) { return title.indexOf(word) !== -1; });
      item.hidden = !match;
      if (match) { shown += 1; }
    });
    if (none) { none.hidden = shown !== 0; }
  }
  field.hidden = false;
  field.addEventListener("input", filter);
  filter();
})();
