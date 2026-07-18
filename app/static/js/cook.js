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
  function tick() {
    var now = Date.now();
    var changed = false;
    timers.forEach(function (t) {
      var el = tray && tray.querySelector("[data-remaining='" + t.id + "']");
      if (!t.done && t.endsAt <= now) {
        t.done = true;
        changed = true;
        beep();
        if ("Notification" in window && Notification.permission === "granted") {
          try { new Notification("Timer done: " + t.label); } catch (e) {}
        }
        if (el) el.textContent = "done";
        if (el && el.parentNode) el.parentNode.classList.add("is-done");
      } else if (el && !t.done) {
        el.textContent = fmt(t.endsAt - now);
      }
    });
    if (changed) persist();
  }
  setInterval(tick, 500);
  render();

  // --- keep the screen awake (secure context only; harmless otherwise) ----------------
  var wakeLock = null;
  function wake() {
    if (!("wakeLock" in navigator)) return;
    navigator.wakeLock.request("screen").then(function (wl) { wakeLock = wl; }).catch(function () {});
  }
  wake();
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") wake();
  });
})();
