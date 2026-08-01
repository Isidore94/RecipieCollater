/* Progress feedback for the slow forms — the four that make a blocking AI call (chat turn,
   AI draft, photo draft, receipt scan). Each is a plain POST, so the browser gives no hint that
   anything is happening for the 10-30s the model takes, and the page reads as broken. Marking
   the form [data-busy-form] disables its submit, swaps in a waiting label, and reveals the
   matching [data-busy-note]. Double-submit is blocked too, which stops double-billing the AI. */
(function () {
  "use strict";
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || !form.matches || !form.matches("[data-busy-form]")) return;
    if (form.hasAttribute("data-busy-active")) {
      event.preventDefault(); // already in flight
      return;
    }
    if (typeof form.checkValidity === "function" && !form.checkValidity()) return;
    form.setAttribute("data-busy-active", "1");

    var button = form.querySelector("button[type=submit], button:not([type])");
    if (button) {
      var label = form.getAttribute("data-busy-label") || "Working…";
      button.setAttribute("data-busy-original", button.innerHTML);
      button.innerHTML = label;
      // Disabling before submit would drop the button's own name/value from the post, and
      // some of these forms carry it, so defer until the request is already on its way.
      setTimeout(function () { button.disabled = true; }, 0);
    }
    var noteId = form.getAttribute("data-busy-note-for");
    var note = noteId
      ? document.getElementById(noteId)
      : (form.parentNode && form.parentNode.querySelector("[data-busy-note]"));
    if (note) note.hidden = false;
  });

  // Restoring from the bfcache (iOS back-swipe) re-shows a form frozen mid-submit; reset it.
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) return;
    document.querySelectorAll("[data-busy-active]").forEach(function (form) {
      form.removeAttribute("data-busy-active");
      var button = form.querySelector("button[data-busy-original]");
      if (button) {
        button.disabled = false;
        button.innerHTML = button.getAttribute("data-busy-original");
        button.removeAttribute("data-busy-original");
      }
    });
    document.querySelectorAll("[data-busy-note]").forEach(function (n) { n.hidden = true; });
  });
})();
