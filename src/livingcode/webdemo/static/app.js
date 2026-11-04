// The live demo page: pick a bug, start a run, and watch every line of it as it happens.
// Every URL here is relative, so the page works under any path prefix, such as /livingcode/.
(function () {
  "use strict";
  var $ = function (selector) { return document.querySelector(selector); };
  var screen = $("[data-screen]");
  var statusBox = $("[data-status]");
  var cards = $("[data-cards]");
  var title = $("[data-title]");
  var timer = $("[data-timer]");
  var monitor = $("[data-monitor]");
  var result = $("[data-result]");
  var state = { scenarios: [], busy: false };
  var started = 0;
  var ticking = null;
  var calm = null;
  var PERIOD = 220;

  // The same heartbeat as the site, one beat from x on a baseline at y.
  function beat(x, y, h) {
    return " L" + (x + 78) + " " + y + " Q" + (x + 86) + " " + (y - 5) + " " + (x + 94) + " " + y +
      " L" + (x + 104) + " " + y + " L" + (x + 110) + " " + (y + 6) + " L" + (x + 118) + " " + (y - h) +
      " L" + (x + 126) + " " + (y + 10) + " L" + (x + 132) + " " + y +
      " Q" + (x + 150) + " " + (y - 9) + " " + (x + 166) + " " + y + " L" + (x + PERIOD) + " " + y;
  }

  function drawTrace() {
    var svg = $("[data-trace]");
    var width = Math.max(320, Math.round(svg.clientWidth));
    var height = svg.clientHeight || 54;
    var y = Math.round(height * 0.62);
    var ok = "M0 " + y;
    for (var x = 0; x < width + PERIOD * 2; x += PERIOD) ok += beat(x, y, height * 0.5);
    var bad = "M0 " + y;
    var seed = 11;
    for (var bx = 0; bx < width + 20;) {
      seed = (seed * 9301 + 49297) % 233280;
      var step = 12 + (seed / 233280) * 20;
      bad += " L" + (bx + step / 2).toFixed(1) + " " + (y + ((seed / 233280) - 0.5) * height * 0.8).toFixed(1);
      bx += step;
      bad += " L" + bx.toFixed(1) + " " + y;
    }
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.querySelector(".ok").setAttribute("d", ok);
    svg.querySelector(".bad").setAttribute("d", bad);
    svg.querySelector(".flow").style.setProperty("--period", PERIOD + "px");
  }

  function pulse(mood) {
    clearTimeout(calm);
    monitor.classList.remove("fault", "idle");
    if (mood === "fault") {
      monitor.classList.add("fault");
      calm = setTimeout(function () { monitor.classList.remove("fault"); }, 1600);
    } else if (mood === "idle") {
      monitor.classList.add("idle");
    }
  }

  // Same colouring rules as the replay on the site.
  function kind(event) {
    if (event.t === "step") return "step";
    if (event.t === "ai") return "ai";
    var text = event.text || "";
    if (/^\$ /.test(text)) return "cmd";
    if (/^\s+\+(?!\+\+)/.test(text)) return "add";
    if (/^\s+-(?!--)/.test(text)) return "del";
    if (/-> 5\d\d|-> no answer|WARN|RolledBack|failed|no_fix/.test(text)) return "bad";
    if (/^\s+OK\b|-> [234]\d\d|Settled|Doctor passed|flag is on|proposed:/.test(text)) return "ok";
    if (/^\s+NOTE|^\s+\(|^\s+@@|^\s+(---|\+\+\+)/.test(text)) return "dim";
    return "";
  }

  function write(text, className) {
    var nearEnd = screen.scrollHeight - screen.scrollTop - screen.clientHeight < 80;
    var span = document.createElement("span");
    if (className) span.className = className;
    span.textContent = text + "\n";
    screen.appendChild(span);
    if (nearEnd) screen.scrollTop = screen.scrollHeight;
  }

  function clock() {
    timer.textContent = started ? Math.round((Date.now() - started) / 1000) + "s" : "";
  }

  function onRun(event) {
    if (event.t === "start") {
      screen.textContent = "";
      result.hidden = true;
      title.textContent = event.title + (event.llm === "fake" ? " (scripted fake AI)" : "");
      started = Date.now();
      clearInterval(ticking);
      ticking = setInterval(clock, 1000);
      clock();
      pulse("run");
      return;
    }
    if (event.t === "done") {
      clearInterval(ticking);
      started = 0;
      timer.textContent = event.seconds ? event.seconds + "s" : "";
      result.hidden = false;
      result.classList.toggle("no", !event.ok);
      $("[data-result-title]").textContent = event.ok ? "Fixed, deployed and watched." : "No fix this time.";
      $("[data-result-text]").textContent = event.summary || "";
      var meta = [];
      if (event.tokens) meta.push(event.tokens.toLocaleString() + " AI tokens");
      if (event.seconds) meta.push(event.seconds + " seconds");
      $("[data-result-meta]").textContent = meta.join(", ");
      pulse("idle");
      return;
    }
    var className = kind(event);
    if (className === "bad") pulse("fault");
    write((event.t === "ai" ? "  AI " : "") + event.text, className);
  }

  function renderCards() {
    cards.textContent = "";
    var blocked = state.busy || !!state.message || state.your_runs_left === 0;
    state.scenarios.forEach(function (item) {
      var card = document.createElement("article");
      card.className = "card" + (state.busy && state.scenario === item.id ? " live" : "");
      var h = document.createElement("h3");
      h.textContent = item.title;
      var p = document.createElement("p");
      p.textContent = item.story;
      var code = document.createElement("code");
      code.textContent = "GET " + item.path;
      var button = document.createElement("button");
      button.className = "btn btn-primary";
      button.type = "button";
      button.textContent = state.busy && state.scenario === item.id ? "Running now" : "Run this one";
      button.disabled = blocked;
      button.addEventListener("click", function () { start(item.id, button); });
      card.append(h, p, code, button);
      cards.appendChild(card);
    });
  }

  function say(text, warn) {
    statusBox.textContent = text;
    statusBox.classList.toggle("warn", !!warn);
  }

  function describe() {
    if (state.busy) return say("A run is going: " + state.title + ". You can watch it below.");
    if (state.message) return say(state.message, true);
    if (typeof state.your_runs_left === "number") {
      say("Ready. You can start " + state.your_runs_left + " more run" + (state.your_runs_left === 1 ? "" : "s") + " today.");
    }
  }

  function apply(next) {
    for (var key in next) state[key] = next[key];
    $("[data-llm-note]").hidden = state.llm !== "fake";
    renderCards();
    describe();
  }

  function refresh() {
    fetch("api/state", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(apply)
      .catch(function () { say("The demo can't be reached right now.", true); });
  }

  function start(id, button) {
    button.disabled = true;
    fetch("api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scenario: id }) })
      .then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); })
      .then(function (answer) {
        if (!answer.ok) say(answer.data.message || "That didn't work, please try again.", true);
        refresh();
        if (answer.ok) screen.focus({ preventScroll: true });
      })
      .catch(function () { say("The demo can't be reached right now.", true); refresh(); });
  }

  function listen() {
    var source = new EventSource("api/events");
    source.addEventListener("state", function (message) {
      apply(JSON.parse(message.data));
      refresh();
    });
    source.addEventListener("run", function (message) { onRun(JSON.parse(message.data)); });
    source.onerror = function () { if (source.readyState !== EventSource.OPEN) say("Lost the live stream, trying again...", true); };
  }

  drawTrace();
  pulse("idle");
  window.addEventListener("resize", drawTrace);
  refresh();
  listen();
})();
