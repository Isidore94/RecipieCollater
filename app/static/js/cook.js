/* Cook mode: one-step-at-a-time navigation, a persistent ingredient checklist, multiple
   recoverable timers with an alarm, and best-effort screen-wake. All state is device-local
   (localStorage). With JavaScript off, every step is simply visible - nothing breaks. */
(function () {
  "use strict";
  var root = document.querySelector(".cook");
  if (!root) return;
  var ns = "rc-cook:" + (root.getAttribute("data-recipe") || "recipe") + ":";

  function load(key, fallback) {
    try {
      var v = localStorage.getItem(ns + key);
      return v === null ? fallback : JSON.parse(v);
    } catch (e) {
      return fallback;
    }
  }
  function save(key, value) {
    try {
      localStorage.setItem(ns + key, JSON.stringify(value));
    } catch (e) {}
  }

  // --- one step at a time -------------------------------------------------------------
  var steps = Array.prototype.slice.call(root.querySelectorAll(".cook-step"));
  var progress = root.querySelector("[data-cook-progress]");
  var current = 0;
  function showStep(i) {
    if (!steps.length) return;
    current = Math.min(Math.max(0, i), steps.length - 1);
    steps.forEach(function (el, idx) { el.hidden = idx !== current; });
    if (progress) progress.textContent = (current + 1) + " / " + steps.length;
    save("step", current);
  }
  var prevBtn = root.querySelector("[data-cook-prev]");
  var nextBtn = root.querySelector("[data-cook-next]");
  if (prevBtn) prevBtn.addEventListener("click", function () { showStep(current - 1); });
  if (nextBtn) nextBtn.addEventListener("click", function () { showStep(current + 1); });
  if (steps.length) showStep(parseInt(load("step", 0), 10) || 0);

  // --- ingredient checklist -----------------------------------------------------------
  var checkState = load("checks", {}) || {};
  root.querySelectorAll(".cook-check").forEach(function (cb) {
    var key = cb.getAttribute("data-key");
    if (checkState[key]) cb.checked = true;
    cb.addEventListener("change", function () {
      checkState[key] = cb.checked;
      save("checks", checkState);
    });
  });

  // --- deviation quick-capture (skip / sub) -------------------------------------------
  // Stored per recipe; the after-cook form reads the same key to pre-fill its questions.
  var devState = load("devs", {}) || {};
  root.querySelectorAll(".cook-ing").forEach(function (row) {
    var key = row.getAttribute("data-devkey");
    var textInput = row.querySelector(".dev-text");
    function apply() {
      var d = devState[key] || {};
      row.querySelectorAll(".dev-btn").forEach(function (b) {
        b.classList.toggle("is-on", b.getAttribute("data-dev") === d.kind);
      });
      if (textInput) {
        textInput.hidden = d.kind !== "substituted";
        if (d.text && !textInput.value) textInput.value = d.text;
      }
      row.classList.toggle("is-skipped", d.kind === "omitted");
    }
    row.querySelectorAll(".dev-btn").forEach(function (b) {
      b.addEventListener("click", function () {
        var kind = b.getAttribute("data-dev");
        var current = devState[key] || {};
        if (current.kind === kind) {
          delete devState[key]; // second tap clears the mark
        } else {
          devState[key] = { kind: kind, text: current.text || "" };
        }
        save("devs", devState);
        apply();
        if (kind === "substituted" && devState[key] && textInput) textInput.focus();
      });
    });
    if (textInput) {
      textInput.addEventListener("input", function () {
        if (devState[key]) {
          devState[key].text = textInput.value;
          save("devs", devState);
        }
      });
    }
    apply();
  });

  // --- timers -------------------------------------------------------------------------
  var tray = root.querySelector("[data-timer-tray]");
  var timers = load("timers", []) || [];
  function persist() { save("timers", timers); }
  function fmt(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    var m = Math.floor(s / 60);
    var r = s % 60;
    return m + ":" + (r < 10 ? "0" : "") + r;
  }
  function beep() {
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      var ctx = new Ctx();
      var osc = ctx.createOscillator();
      osc.frequency.value = 880;
      osc.connect(ctx.destination);
      osc.start();
      setTimeout(function () { osc.stop(); ctx.close(); }, 700);
    } catch (e) {}
  }
  function remove(id) {
    timers = timers.filter(function (t) { return t.id !== id; });
    persist();
    render();
    renderAlert();
  }
  function render() {
    if (!tray) return;
    tray.innerHTML = "";
    timers.forEach(function (t) {
      var chip = document.createElement("div");
      chip.className = "timer-chip" + (t.done ? " is-done" : "");
      var label = document.createElement("span");
      label.textContent = t.label;
      var time = document.createElement("strong");
      time.setAttribute("data-remaining", t.id);
      time.textContent = t.done ? "done" : fmt(t.endsAt - Date.now());
      var x = document.createElement("button");
      x.type = "button";
      x.className = "timer-cancel";
      x.textContent = "×";
      x.addEventListener("click", function () { remove(t.id); });
      chip.appendChild(label);
      chip.appendChild(time);
      chip.appendChild(x);
      tray.appendChild(chip);
    });
  }
  function add(seconds, label) {
    if (seconds <= 0) return;
    if ("Notification" in window && Notification.permission === "default") {
      try { Notification.requestPermission(); } catch (e) {}
    }
    timers.push({
      id: String(Date.now()) + "-" + timers.length,
      label: label,
      endsAt: Date.now() + seconds * 1000,
      done: false,
    });
    persist();
    render();
  }
  root.querySelectorAll(".timer-btn").forEach(function (b) {
    b.addEventListener("click", function () {
      add(parseInt(b.getAttribute("data-seconds"), 10) || 0, b.getAttribute("data-label") || "Timer");
    });
  });
  // A timer that finishes while the phone is asleep or the tab is backgrounded cannot beep
  // (no service worker over plain HTTP), so every finished timer stays "unacknowledged" until
  // she actually sees it. The banner is what greets her when she picks the phone back up.
  var alertEl = root.querySelector("[data-timer-alert]");
  function ago(ms) {
    var mins = Math.floor(ms / 60000);
    if (mins < 1) return "just now";
    if (mins === 1) return "1 minute ago";
    if (mins < 60) return mins + " minutes ago";
    var hrs = Math.floor(mins / 60);
    return hrs === 1 ? "1 hour ago" : hrs + " hours ago";
  }
  function renderAlert() {
    if (!alertEl) return;
    var pending = timers.filter(function (t) { return t.done && !t.acked; });
    if (!pending.length) {
      alertEl.hidden = true;
      alertEl.innerHTML = "";
      return;
    }
    alertEl.innerHTML = "";
    var list = document.createElement("div");
    list.className = "timer-alert-text";
    pending.forEach(function (t) {
      var line = document.createElement("p");
      line.className = "timer-alert-line";
      line.textContent = "⏱ " + t.label + " finished " + ago(Date.now() - t.endsAt);
      list.appendChild(line);
    });
    var dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "btn-primary timer-alert-ok";
    dismiss.textContent = "Got it";
    dismiss.addEventListener("click", function () {
      timers = timers.filter(function (t) { return !(t.done && !t.acked); });
      persist();
      render();
      renderAlert();
    });
    alertEl.appendChild(list);
    alertEl.appendChild(dismiss);
    alertEl.hidden = false;
  }

  function tick() {
    var now = Date.now();
    var changed = false;
    timers.forEach(function (t) {
      var el = tray && tray.querySelector("[data-remaining='" + t.id + "']");
      if (!t.done && t.endsAt <= now) {
        t.done = true;
        changed = true;
        beep();
        try { if (navigator.vibrate) navigator.vibrate([200, 100, 200]); } catch (e) {}
        if ("Notification" in window && Notification.permission === "granted") {
          try { new Notification("Timer done: " + t.label); } catch (e) {}
        }
        if (el) el.textContent = "done";
        if (el && el.parentNode) el.parentNode.classList.add("is-done");
      } else if (el && !t.done) {
        el.textContent = fmt(t.endsAt - now);
      }
    });
    if (changed) {
      persist();
      renderAlert();
    }
  }
  setInterval(tick, 500);
  // Coming back to the page is the moment a missed timer must be announced.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") {
      tick();
      renderAlert();
    }
  });
  render();
  renderAlert();

  // --- keep the screen awake ----------------------------------------------------------
  // navigator.wakeLock needs a secure context, which the default LAN deployment (plain HTTP)
  // does not have. So the looping muted video is the workhorse and wakeLock is the bonus:
  // iOS Safari keeps the screen lit while a video plays, HTTP or not.
  var wakeLock = null;
  var video = root.querySelector("[data-keep-awake]");
  var statusEl = root.querySelector("[data-wake-status]");

  function setWakeStatus(on) {
    if (!statusEl) return;
    statusEl.textContent = on ? "Screen stays on" : "Screen may sleep — tap here";
    statusEl.classList.toggle("is-on", !!on);
    statusEl.hidden = false;
  }

  function playVideo() {
    if (!video) return Promise.reject();
    var p = video.play();
    return p && p.then ? p : Promise.resolve();
  }

  function wake() {
    try {
      if (navigator.wakeLock && navigator.wakeLock.request) {
        navigator.wakeLock
          .request("screen")
          .then(function (wl) {
            wakeLock = wl;
            setWakeStatus(true);
            wl.addEventListener("release", function () { wakeLock = null; });
          })
          .catch(function () { videoWake(); });
        return;
      }
    } catch (e) {}
    videoWake();
  }

  function videoWake() {
    playVideo().then(function () { setWakeStatus(true); }).catch(function () {
      // Autoplay was refused; the next deliberate tap counts as the required gesture.
      setWakeStatus(false);
    });
  }

  wake();
  // Any tap in cook mode is a user gesture, so retry a refused autoplay off the first one.
  root.addEventListener("click", function retry() {
    if (wakeLock || (video && !video.paused)) return;
    videoWake();
  });
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") wake();
  });
})();
