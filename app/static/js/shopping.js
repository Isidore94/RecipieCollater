// Copy the shopping list for pasting into Apple Reminders (one item per line).
//
// The app is served over plain HTTP on the LAN, which is not a "secure context", so
// navigator.clipboard is unavailable in most browsers (incl. iOS Safari). The reliable path there
// is selecting a <textarea> and document.execCommand('copy'). We reveal the textarea first so that,
// if even that is blocked, the user can select it by hand and use the native Copy menu.
(function () {
  "use strict";

  function setStatus(msg) {
    var el = document.getElementById("reminders-status");
    if (el) {
      el.textContent = msg;
    }
  }

  function copyReminders() {
    var ta = document.getElementById("reminders-text");
    if (!ta) {
      return;
    }
    ta.hidden = false; // must be visible/selectable, and doubles as a manual fallback
    ta.focus();
    ta.select();
    try {
      ta.setSelectionRange(0, ta.value.length); // iOS Safari needs an explicit range
    } catch (e) {
      /* older browsers: select() is enough */
    }

    var copied = false;
    try {
      copied = document.execCommand("copy");
    } catch (e) {
      copied = false;
    }
    if (copied) {
      setStatus("Copied — open Reminders and paste.");
      return;
    }

    // Secure contexts (HTTPS / localhost) can use the async Clipboard API.
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(ta.value).then(
        function () {
          setStatus("Copied — open Reminders and paste.");
        },
        function () {
          setStatus("Select the text below, then Copy.");
        }
      );
      return;
    }

    setStatus("Select the text below, then Copy.");
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("copy-reminders");
    if (btn) {
      btn.addEventListener("click", copyReminders);
    }
  });
})();
