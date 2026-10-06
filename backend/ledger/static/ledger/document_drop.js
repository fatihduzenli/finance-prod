// Drag and drop, and the "Take photo" input, for the document upload box
// (docs/qa/PLAN-010c D7, PLAN-011 D9). A dropped file goes into the file input. The box
// has two inputs (a file, or a photo from the camera) and the server takes one file per
// submit, so choosing in one input clears the other. The form is a plain multipart post
// and works without this script. No file content is read here.
(function () {
  "use strict";
  document.querySelectorAll(".js-document-drop").forEach(function (zone) {
    var inputs = Array.prototype.slice.call(zone.querySelectorAll("input[type=file]"));
    var input = zone.querySelector("input.js-document-file") || inputs[0];
    var hint = zone.querySelector(".js-document-drop-hint");
    if (!input) {
      return;
    }
    if (hint) {
      hint.hidden = false;
    }
    function keepOnly(chosen) {
      inputs.forEach(function (other) {
        if (other !== chosen) {
          other.value = "";
        }
      });
      if (hint && chosen.files && chosen.files.length === 1) {
        hint.textContent = "Selected: " + chosen.files[0].name;
      }
    }
    inputs.forEach(function (each) {
      each.addEventListener("change", function () {
        keepOnly(each);
      });
    });
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
      keepOnly(input);
    });
  });
})();
