/* v0.89.0: draws the charts of /analytics and /health. Loaded with `defer` after the vendored Chart.js; no inline script, no eval.
   The data is the JSON block #chart-data (one entry per canvas id, built by app/analytics.py); colours are token names that
   are read from the CSS custom properties, so the palette lives in style.css only. */
(function () {
  "use strict";
  var holder = document.getElementById("chart-data");
  if (!holder || typeof Chart === "undefined") { return; }
  var charts = JSON.parse(holder.textContent || "{}");
  var root = getComputedStyle(document.documentElement);
  var css = function (name) { return root.getPropertyValue(name).trim(); };
  var fontFamily = getComputedStyle(document.body).fontFamily;
  var tick = css("--label-2"), grid = css("--sep");
  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var nf = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
  var colour = function (token) { return css("--" + token) || css("--grey"); };

  Chart.defaults.font.family = fontFamily;
  Chart.defaults.font.size = 12;
  Chart.defaults.color = tick;
  Chart.defaults.borderColor = grid;

  Object.keys(charts).forEach(function (id) {
    var spec = charts[id], canvas = document.getElementById(id);
    if (!canvas) { return; }
    // Chart.js sizes a chart to its parent: a box carries the height class (.h-s, .h-m, .h-l of style.css)
    var box = document.createElement("div");
    box.className = "chartbox";
    canvas.className.split(/\s+/).forEach(function (c) {
      if (/^h-[sml]$/.test(c)) { box.classList.add(c); canvas.classList.remove(c); }
    });
    canvas.parentNode.insertBefore(box, canvas);
    box.appendChild(canvas);

    var horizontal = !!spec.horizontal, stacked = !!spec.stacked;
    var whole = spec.datasets.every(function (d) {
      return d.data.every(function (v) { return v === null || Number.isInteger(v); });
    });
    var datasets = spec.datasets.map(function (d) {
      var fill = d.colors ? d.colors.map(colour) : colour(d.color);
      return { label: d.label, data: d.data, backgroundColor: fill, borderWidth: 0, borderRadius: stacked ? 0 : 4, maxBarThickness: 28 };
    });
    var value = { stacked: stacked, beginAtZero: true, grid: { color: grid }, border: { color: grid },
                  ticks: { color: tick, precision: whole ? 0 : undefined, callback: function (v) { return nf.format(v); } } };
    if (spec.max) { value.max = spec.max; }
    var category = { stacked: stacked, grid: { display: false }, border: { color: grid }, ticks: { color: tick, autoSkip: false } };
    new Chart(canvas, {
      type: spec.type,
      data: { labels: spec.labels, datasets: datasets },
      options: {
        indexAxis: horizontal ? "y" : "x",
        responsive: true,
        maintainAspectRatio: false,
        animation: reduced ? false : undefined,
        scales: horizontal ? { x: value, y: category } : { x: category, y: value },
        plugins: {
          legend: { display: datasets.length > 1, labels: { color: tick, boxWidth: 12 } },
          tooltip: { callbacks: { label: function (c) {
            var v = horizontal ? c.parsed.x : c.parsed.y;
            return c.dataset.label + ": " + nf.format(v);
          } } }
        }
      }
    });
  });
})();
