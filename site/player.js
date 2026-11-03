// Replays a recorded run of scripts/demo.py in a terminal box. The lines are exactly what the
// demo printed (see demo-cast.js). Only the pacing is changed, so it can be read: commands are
// typed out, and the replay pauses on each step and on each result.
(function () {
  var TYPE = 0.028;
  var LINE = 0.06;
  var AFTER_COMMAND = 0.45;
  var STEP_PAUSE = 1.5;
  var RESULT_PAUSE = 1.1;
  var END_PAUSE = 4;

  function kind(text) {
    if (text.indexOf("== ") === 0) return "step";
    if (text.indexOf("$ ") === 0) return "cmd";
    if (/^\s+\+/.test(text)) return "add";
    if (/-> 5\d\d|WARN|RolledBack|failed/.test(text)) return "bad";
    if (/^\s+OK\b|-> 200|Settled|Doctor passed|flag is on/.test(text)) return "ok";
    if (/^\s+NOTE|^\s+\(/.test(text)) return "dim";
    return "";
  }

  function isResult(text) {
    return /-> \d{3}|Settled:|RolledBack:|Doctor passed|proposed:/.test(text);
  }

  function timeline(lines) {
    var events = [];
    var at = 0.6;
    lines.forEach(function (entry) {
      var text = entry[1];
      var k = kind(text);
      if (k === "step") {
        at += 0.4;
        events.push({ at: at, text: text, kind: k, typing: 0 });
        at += STEP_PAUSE;
      } else if (k === "cmd") {
        var typing = (text.length - 2) * TYPE;
        events.push({ at: at, text: text, kind: k, typing: typing });
        at += typing + AFTER_COMMAND;
      } else {
        events.push({ at: at, text: text, kind: k, typing: 0 });
        at += isResult(text) ? RESULT_PAUSE : LINE;
      }
    });
    return { events: events, length: at + END_PAUSE };
  }

  function escape(text) {
    return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function Player(box, cast) {
    this.box = box;
    this.screen = box.querySelector(".screen");
    this.plan = timeline(cast.lines);
    this.started = null;
    this.timer = null;
    this.lastKey = "";
  }

  // Draws the terminal as it looks t seconds into the replay. Returns a key that changes
  // whenever the picture changes, so a recorder can skip identical frames.
  Player.prototype.seek = function (t) {
    var html = [];
    var key = 0;
    this.plan.events.forEach(function (event) {
      if (event.at > t) return;
      var text = event.text;
      var cursor = "";
      if (event.kind === "cmd" && t < event.at + event.typing) {
        var shown = Math.floor((t - event.at) / TYPE);
        text = text.slice(0, 2 + shown);
        cursor = '<span class="cursor"> </span>';
      }
      key += text.length + 1;
      html.push('<span class="' + event.kind + '">' + escape(text) + "</span>" + cursor);
    });
    if (t >= this.plan.length - END_PAUSE) key += 1;
    var joined = html.join("\n");
    if (joined !== this.lastKey) {
      this.screen.innerHTML = joined;
      this.screen.scrollTop = this.screen.scrollHeight;
      this.lastKey = joined;
    }
    return key;
  };

  Player.prototype.play = function () {
    var self = this;
    window.clearInterval(this.timer);
    this.started = performance.now();
    this.timer = window.setInterval(function () {
      var t = (performance.now() - self.started) / 1000;
      self.seek(t);
      if (t > self.plan.length) {
        window.clearInterval(self.timer);
        self.box.classList.add("done");
      }
    }, 33);
    this.box.classList.remove("done");
  };

  window.LivingCodeDemo = { Player: Player };

  document.addEventListener("DOMContentLoaded", function () {
    var box = document.querySelector("[data-demo]");
    if (!box || !window.DEMO_CAST) return;
    var player = new Player(box, window.DEMO_CAST);
    window.LivingCodeDemo.player = player;
    var replay = box.querySelector("[data-replay]");
    if (replay) replay.addEventListener("click", function () { player.play(); });
    if (/[?&]still\b/.test(location.search)) return;
    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce) {
      player.seek(player.plan.length);
    } else {
      player.play();
    }
  });
})();
