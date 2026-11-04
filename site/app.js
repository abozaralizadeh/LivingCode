// Small things for the site: the health trace, scroll reveals, copy buttons and the theme switch.
(function () {
  var root = document.documentElement;
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var PERIOD = 220;

  // One heartbeat, starting at x, on a baseline at y.
  function beat(x, y, height) {
    return " L" + (x + 78) + " " + y +
      " Q" + (x + 86) + " " + (y - 7) + " " + (x + 94) + " " + y +
      " L" + (x + 104) + " " + y +
      " L" + (x + 110) + " " + (y + 8) +
      " L" + (x + 118) + " " + (y - height) +
      " L" + (x + 126) + " " + (y + 16) +
      " L" + (x + 132) + " " + y +
      " Q" + (x + 150) + " " + (y - 13) + " " + (x + 166) + " " + y +
      " L" + (x + PERIOD) + " " + y;
  }

  function steady(width, y, height) {
    var d = "M0 " + y;
    for (var x = 0; x < width + PERIOD * 2; x += PERIOD) d += beat(x, y, height);
    return d;
  }

  // A fixed, made-up fault: a few normal beats, then the line goes wild and drops.
  function broken(width, y) {
    var d = "M0 " + y;
    var x = 0;
    while (x < width * 0.34) { d += beat(x, y, 40); x += PERIOD; }
    var seed = 7;
    function next() { seed = (seed * 9301 + 49297) % 233280; return seed / 233280; }
    while (x < width * 0.8) {
      var step = 14 + next() * 22;
      var up = (next() - 0.5) * 84;
      d += " L" + (x + step / 2).toFixed(1) + " " + (y + up).toFixed(1);
      x += step;
      d += " L" + x.toFixed(1) + " " + (y + (next() - 0.5) * 20).toFixed(1);
    }
    d += " L" + (x + 10) + " " + (y + 30) + " L" + (width + 20) + " " + (y + 30);
    return d;
  }

  function drawTrace(svg) {
    var width = Math.max(320, Math.round(svg.clientWidth));
    var height = svg.clientHeight || 120;
    var y = Math.round(height * 0.62);
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.querySelector(".ok").setAttribute("d", steady(width, y, height * 0.48));
    svg.querySelector(".bad").setAttribute("d", broken(width, y));
    var flow = svg.querySelector(".flow");
    flow.style.setProperty("--period", PERIOD + "px");
    flow.style.animationDuration = "1.35s";
  }

  function drawDivider(svg) {
    var width = Math.max(320, Math.round(svg.clientWidth));
    var height = svg.clientHeight || 64;
    var y = Math.round(height * 0.6);
    var many = svg.classList.contains("closing-trace");
    var d = "M0 " + y;
    if (many) {
      for (var x = 0; x < width; x += PERIOD * 1.6) d += beat(x, y, height * 0.5) + " L" + (x + PERIOD * 1.6) + " " + y;
    } else {
      var mid = Math.round(width / 2 - PERIOD / 2);
      d += " L" + mid + " " + y + beat(mid, y, height * 0.55) + " L" + width + " " + y;
    }
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.querySelector("path").setAttribute("d", d);
  }

  function drawAll() {
    document.querySelectorAll("[data-trace]").forEach(drawTrace);
    document.querySelectorAll("[data-divider]").forEach(drawDivider);
  }

  function monitor() {
    var box = document.querySelector("[data-monitor]");
    if (!box) return;
    var button = box.querySelector("[data-break]");
    var status = box.querySelector("[data-status]");
    var timers = [];
    function later(ms, fn) { timers.push(window.setTimeout(fn, ms)); }
    button.addEventListener("click", function () {
      timers.forEach(window.clearTimeout);
      timers = [];
      button.disabled = true;
      box.classList.remove("recovered");
      status.textContent = "Deploying r0004...";
      later(700, function () {
        box.classList.add("fault");
        status.textContent = "r0004 failed its smoke check: /checkout gave 500";
      });
      later(2500, function () {
        box.classList.remove("fault");
        box.classList.add("recovered");
        status.textContent = "The guard rolled back to r0003. No AI, no network.";
      });
      later(3500, function () {
        box.classList.remove("recovered");
        button.disabled = false;
        button.textContent = "Ship another bad version";
      });
      later(7000, function () { status.textContent = "r0003 is live. Health checks pass."; });
    });
  }

  function reveals() {
    var items = document.querySelectorAll(".reveal");
    if (reduce || !("IntersectionObserver" in window)) {
      items.forEach(function (item) { item.classList.add("in"); });
      return;
    }
    var seen = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("in");
        seen.unobserve(entry.target);
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -40px 0px" });
    items.forEach(function (item) { seen.observe(item); });
  }

  function copyButtons() {
    document.querySelectorAll("[data-copy]").forEach(function (button) {
      button.addEventListener("click", function () {
        var code = button.parentNode.querySelector("[data-copy-text]");
        var text = code.getAttribute("data-copy-text");
        function done() {
          button.textContent = "Copied";
          button.classList.add("done");
          window.setTimeout(function () { button.textContent = "Copy"; button.classList.remove("done"); }, 1600);
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(done, function () {});
        } else {
          var area = document.createElement("textarea");
          area.value = text;
          document.body.appendChild(area);
          area.select();
          try { document.execCommand("copy"); done(); } catch (e) {}
          document.body.removeChild(area);
        }
      });
    });
  }

  function themeToggle() {
    var button = document.querySelector("[data-theme-toggle]");
    if (!button) return;
    button.addEventListener("click", function () {
      var current = root.getAttribute("data-theme") ||
        (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      var next = current === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("lc-theme", next); } catch (e) {}
    });
  }

  function header() {
    var bar = document.querySelector("[data-header]");
    if (!bar) return;
    function update() { bar.classList.toggle("scrolled", window.scrollY > 8); }
    window.addEventListener("scroll", update, { passive: true });
    update();
  }

  document.addEventListener("DOMContentLoaded", function () {
    drawAll();
    monitor();
    reveals();
    copyButtons();
    themeToggle();
    header();
    var resizing;
    window.addEventListener("resize", function () {
      window.clearTimeout(resizing);
      resizing = window.setTimeout(drawAll, 150);
    });
  });
})();
