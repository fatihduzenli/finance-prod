// Drag and drop for the document upload box (docs/qa/PLAN-010c D7).
// Only moves one dropped file into the form's file input; the form is a plain multipart
// post and works without this script. No file content is read here.
(function () {
  "use strict";
  document.querySelectorAll(".js-document-drop").forEach(function (zone) {
    var input = zone.querySelector("input[type=file]");
    var hint = zone.querySelector(".js-document-drop-hint");
    if (!input) {
      return;
    }
    if (hint) {
      hint.hidden = false;
    }
    zone.addEventListener("dragover", function (event) {
      event.preventDefault();
      zone.style.background = "#eef4ff";
    });
    zone.addEventListener("dragleave", function () {
      zone.style.background = "";
    });
    zone.addEventListener("drop", function (event) {
      event.preventDefault();
      zone.style.background = "";
      var files = event.dataTransfer ? event.dataTransfer.files : null;
      if (!files || files.length !== 1) {
        if (hint) {
          hint.textContent = "Drop one file at a time.";
        }
        return;
      }
      input.files = files;
      if (hint) {
        hint.textContent = "Selected: " + files[0].name;
      }
    });
  });
})();
