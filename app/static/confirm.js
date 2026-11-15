/* The delete confirmation of the job page (v0.99.0): a form marked data-confirm="text" asks with the browser's own confirm() before it is sent.
   OK: a hidden field confirm=yes is added and the form is sent; Cancel: nothing is sent. Without this script the server answers a delete without
   confirm=yes with its own confirmation page. The text is read as plain text; nothing is evaluated. */
(function () {
  "use strict";
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || !form.hasAttribute || !form.hasAttribute("data-confirm")) { return; }
    event.preventDefault();
    if (!window.confirm(form.getAttribute("data-confirm"))) { return; }
    var field = document.createElement("input");
    field.type = "hidden";
    field.name = "confirm";
    field.value = "yes";
    form.appendChild(field);
    HTMLFormElement.prototype.submit.call(form);
  });
})();
