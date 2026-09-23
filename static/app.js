"use strict";

/* battviz frontend. No dependencies: every chart is laid out with CSS
   percentages against the container, so the page works offline and reflows
   without a redraw step. */

var report = null;
var rawText = "";

/* Channel colours mirror style.css. A subsystem keeps its colour in every
   chart, so the legend only has to be read once. */
var CHANNEL = {
  cpu: "var(--cpu)",
  wakelock: "var(--wakelock)",
  wifi: "var(--wifi)",
  sensors: "var(--sensors)",
  screen: "var(--screen)",
  idle: "var(--idle)",
  bluetooth: "var(--radio)",
  cell: "var(--radio)",
  radio: "var(--radio)",
  gps: "var(--sensors)",
  camera: "var(--sensors)",
  flashlight: "var(--alarm)",
  audio: "var(--alarm)",
  video: "var(--alarm)",
  memory: "var(--idle)",
  overcounted: "var(--idle)",
  unaccounted: "var(--idle)"
};

var LANE_COLOUR = {
  power_state_active: "var(--screen)",
  power_state_idle_awake: "var(--wakelock)",
  power_state_suspended: "var(--idle)",
  doze: "var(--radio)",
  tmpwhitelist: "var(--radio)",
  screen: "var(--screen)",
  running: "var(--cpu)",
  wake_lock: "var(--wakelock)",
  job: "var(--cpu)",
  sync: "var(--sensors)",
  audio: "var(--alarm)",
  video: "var(--alarm)",
  wifi_radio: "var(--wifi)",
  wifi_scan: "var(--wifi)",
  wifi_running: "var(--wifi)",
  mobile_radio: "var(--radio)",
  phone_scanning: "var(--radio)",
  gps: "var(--sensors)",
  sensor: "var(--sensors)",
  camera: "var(--sensors)",
  flashlight: "var(--alarm)",
  top: "var(--idle)",
  fg: "var(--idle)",
  plugged: "var(--wifi)",
  usb_data: "var(--wifi)",
  bluetooth_scan_on: "var(--radio)",
  package_inst: "var(--idle)",
  wifi_full_lock: "var(--wifi)"
};

var LANE_NAME = {
  power_state: "Power state (derived)",
  doze: "Doze",
  tmpwhitelist: "Doze attempt",
  screen: "Screen",
  running: "CPU running",
  wake_lock: "Wakelock",
  job: "Jobs",
  sync: "Sync",
  audio: "Audio",
  video: "Video",
  wifi_radio: "Wifi radio",
  wifi_scan: "Wifi scan",
  wifi_running: "Wifi on",
  mobile_radio: "Mobile radio",
  phone_scanning: "Cell scanning",
  gps: "GPS",
  sensor: "Sensors",
  camera: "Camera",
  flashlight: "Flashlight",
  top: "Foreground app",
  fg: "Foreground",
  plugged: "Charger",
  usb_data: "USB data",
  bluetooth_scan_on: "Bluetooth scan",
  package_inst: "Package install",
  wifi_full_lock: "Wifi lock"
};

/* --------------------------------------------------------------- utils -- */

function $(sel, root) { return (root || document).querySelector(sel); }
function el(tag, cls, text) {
  var n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function dur(ms) {
  if (ms === null || ms === undefined) return "\u2013";
  ms = Math.round(ms);
  if (ms === 0) return "0";
  if (ms < 1000) return ms + "ms";
  var s = Math.floor(ms / 1000), m, h, d, out = [];
  d = Math.floor(s / 86400); s -= d * 86400;
  h = Math.floor(s / 3600); s -= h * 3600;
  m = Math.floor(s / 60); s -= m * 60;
  if (d) out.push(d + "d");
  if (h) out.push(h + "h");
  if (m) out.push(m + "m");
  if (s || !out.length) out.push(s + "s");
  return out.slice(0, 2).join(" ");
}

function clock(ms) {
  var s = Math.max(0, Math.round(ms / 1000));
  var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  var pad = function (n) { return (n < 10 ? "0" : "") + n; };
  return (h ? h + ":" : "") + pad(m) + ":" + pad(r);
}

function mah(v) {
  if (v === null || v === undefined) return "\u2013";
  if (v === 0) return "0";
  if (v >= 100) return v.toFixed(0);
  if (v >= 1) return v.toFixed(2);
  if (v >= 0.01) return v.toFixed(3);
  return v.toExponential(1);
}

function pct(n, total) {
  if (!total) return 0;
  return Math.max(0, Math.min(100, (n / total) * 100));
}

function toast(msg) {
  var t = $("#toast");
  t.textContent = msg;
  t.classList.add("on");
  clearTimeout(toast._h);
  toast._h = setTimeout(function () { t.classList.remove("on"); }, 2600);
}

function emptyNote(text) {
  var d = el("div", "empty", text);
  return d;
}

/* Build a table from column specs. Sorting is handled here so every view
   gets it for free. */
function buildTable(cols, rows, opts) {
  opts = opts || {};
  var state = { key: opts.sortKey || null, dir: opts.sortDir || -1 };
  var wrap = el("div");
  var table = el("table");
  var thead = el("thead");
  var htr = el("tr");

  cols.forEach(function (c) {
    var th = el("th", (c.right ? "r " : "") + (c.sort === false ? "" : "sortable"));
    th.appendChild(document.createTextNode(c.label));
    if (c.sort !== false) {
      var arrow = el("span", "arrow", "");
      th.appendChild(document.createTextNode(" "));
      th.appendChild(arrow);
      th.addEventListener("click", function () {
        if (state.key === c.key) state.dir = -state.dir;
        else { state.key = c.key; state.dir = -1; }
        draw();
      });
    }
    htr.appendChild(th);
  });
  thead.appendChild(htr);
  table.appendChild(thead);
  var tbody = el("tbody");
  table.appendChild(tbody);
  wrap.appendChild(table);

  function draw() {
    var data = rows.slice();
    if (state.key) {
      var col = cols.filter(function (c) { return c.key === state.key; })[0];
      var get = (col && col.value) || function (r) { return r[state.key]; };
      data.sort(function (a, b) {
        var x = get(a), y = get(b);
        if (typeof x === "string" || typeof y === "string") {
          return String(x).localeCompare(String(y)) * -state.dir;
        }
        return ((x || 0) - (y || 0)) * state.dir;
      });
    }
    htr.querySelectorAll("th").forEach(function (th, i) {
      var arrow = th.querySelector(".arrow");
      if (arrow) arrow.textContent = cols[i].key === state.key
        ? (state.dir === -1 ? "\u25be" : "\u25b4") : "";
    });
    tbody.textContent = "";
    if (!data.length) {
      var tr = el("tr");
      var td = el("td");
      td.colSpan = cols.length;
      td.appendChild(emptyNote(opts.empty || "Nothing recorded."));
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }
    data.forEach(function (row) {
      var tr = el("tr");
      if (opts.expand) tr.className = "clickable";
      cols.forEach(function (c) {
        var td = el("td", c.right ? "r" : "");
        var out = c.render ? c.render(row) : row[c.key];
        if (out instanceof Node) td.appendChild(out);
        else td.innerHTML = out === undefined || out === null ? "" : String(out);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);

      if (opts.expand) {
        var open = false, detail = null;
        tr.addEventListener("click", function () {
          if (open) {
            detail.remove();
            open = false;
            return;
          }
          detail = el("tr", "detail");
          var td = el("td");
          td.colSpan = cols.length;
          td.appendChild(opts.expand(row));
          detail.appendChild(td);
          tr.after(detail);
          open = true;
        });
      }
    });
  }

  draw();
  return wrap;
}

/* Horizontal proportion bar, optionally segmented by component. */
function segBar(parts, total) {
  var bar = el("div", "bar");
  parts.forEach(function (p) {
    if (!p.value) return;
    var s = el("span");
    s.style.width = pct(p.value, total) + "%";
    s.style.background = p.colour;
    s.title = p.label + ": " + p.text;
    bar.appendChild(s);
  });
  return bar;
}

/* ------------------------------------------------------------ overview -- */

function renderOverview() {
  var s = report.summary || {};
  var intro = $("#ov-intro");
  var onBat = s.time_on_battery_ms;
  intro.textContent = "Captured " + (s.start_clock || "unknown time") + ", " +
    dur(onBat) + " on battery across " + report.sections.length +
    " parsed sections.";

  var metrics = [
    ["Time on battery", dur(onBat), s.time_on_battery_pct != null
      ? s.time_on_battery_pct.toFixed(1) + "% of run time" : ""],
    ["Screen on", s.screen_on_pct != null ? s.screen_on_pct.toFixed(1) + "%" : "\u2013",
      dur(s.screen_on_ms) + (s.screen_on_count ? ", " + s.screen_on_count + " wakes" : "")],
    ["Idle time", s.light_idle_pct != null ? s.light_idle_pct.toFixed(1) + "%" : "\u2013",
      dur(s.light_idle_ms) + " light doze"],
    ["Awake on wakelocks", dur(s.total_partial_wakelock_ms),
      report.partial_wakelocks.length + " distinct locks"],
    ["Modelled drain", mah(report.power.total_uid_mah) + " mAh",
      "capacity " + (s.learned_capacity_mah || s.capacity_mah || "?") + " mAh"],
    ["Measured drain", (s.actual_drain_mah != null ? s.actual_drain_mah : "\u2013") + " mAh",
      s.actual_drain_mah === 0 ? "battery level did not move" : "since last charge"]
  ];
  var box = $("#ov-metrics");
  box.textContent = "";
  metrics.forEach(function (m) {
    var c = el("div", "metric");
    c.appendChild(el("div", "label", m[0]));
    c.appendChild(el("div", "value", m[1]));
    c.appendChild(el("div", "foot", m[2]));
    box.appendChild(c);
  });

  // Power budget strip.
  var glob = (report.power.global || []).filter(function (g) { return g.mah > 0; })
    .sort(function (a, b) { return b.mah - a.mah; });
  var total = glob.reduce(function (a, g) { return a + g.mah; }, 0);
  var strip = $("#ov-budget");
  var legend = $("#ov-budget-legend");
  strip.textContent = "";
  legend.textContent = "";
  if (!total) {
    strip.style.display = "none";
    legend.appendChild(emptyNote("No global power estimates in this dump."));
  } else {
    strip.style.display = "flex";
    glob.forEach(function (g) {
      var colour = CHANNEL[g.component] || "var(--idle)";
      var seg = el("span");
      seg.style.width = pct(g.mah, total) + "%";
      seg.style.background = colour;
      seg.title = g.component + " " + mah(g.mah) + " mAh";
      strip.appendChild(seg);

      var item = el("span");
      var sw = el("i");
      sw.style.background = colour;
      item.appendChild(sw);
      item.appendChild(document.createTextNode(g.component + " "));
      var b = el("b", null, mah(g.mah) + " mAh");
      item.appendChild(b);
      var share = el("span", "faint", " " + pct(g.mah, total).toFixed(0) + "%");
      share.style.marginLeft = "2px";
      item.appendChild(share);
      legend.appendChild(item);
    });
  }

  // Top culprits, as component-segmented bars.
  var top = (report.culprits || []).slice(0, 8);
  var maxMah = Math.max.apply(null, top.map(function (c) { return c.total_mah; }).concat([0.0001]));
  var host = $("#ov-top");
  host.textContent = "";
  if (!top.length) {
    host.appendChild(emptyNote("No per-uid power estimates in this dump."));
  } else {
    top.forEach(function (c) {
      var row = el("div");
      row.style.marginBottom = "11px";
      var head = el("div");
      head.style.cssText = "display:flex;gap:10px;align-items:baseline;font-size:13.5px;margin-bottom:4px";
      var name = el("span", null, c.label);
      name.style.fontWeight = "550";
      head.appendChild(name);
      var uid = el("span", "mono faint", c.uid);
      uid.style.fontSize = "11.5px";
      head.appendChild(uid);
      var right = el("span", "dim num");
      right.style.marginLeft = "auto";
      right.textContent = mah(c.total_mah) + " mAh" +
        (c.wakelock_ms ? "  \u00b7  " + dur(c.wakelock_ms) + " held" : "");
      head.appendChild(right);
      row.appendChild(head);

      var parts = Object.keys(c.components).map(function (k) {
        return {
          value: c.components[k], colour: CHANNEL[k] || "var(--idle)",
          label: k, text: mah(c.components[k]) + " mAh"
        };
      });
      var bar = segBar(parts, maxMah);
      bar.style.height = "13px";
      row.appendChild(bar);
      host.appendChild(row);
    });

    var lg = el("div", "legend");
    ["cpu", "wakelock", "wifi", "sensors"].forEach(function (k) {
      var item = el("span");
      var sw = el("i");
      sw.style.background = CHANNEL[k];
      item.appendChild(sw);
      item.appendChild(document.createTextNode(k));
      lg.appendChild(item);
    });
    host.appendChild(lg);
  }

  // Findings.
  var fbox = $("#ov-findings");
  fbox.textContent = "";
  if (!report.findings.length) {
    fbox.appendChild(emptyNote("Nothing unusual stood out in this dump."));
  }
  report.findings.forEach(function (f) {
    var card = el("div", "finding" + (f.level === "warn" ? " warn" : ""));
    card.appendChild(el("div", "mark"));
    var body = el("div");
    body.appendChild(el("h3", null, f.title));
    body.appendChild(el("p", null, f.body));
    card.appendChild(body);
    if (f.goto && f.goto !== "overview") {
      var btn = el("button", null, "Open");
      btn.type = "button";
      btn.addEventListener("click", function () { show(f.goto); });
      card.appendChild(btn);
    }
    fbox.appendChild(card);
  });
}

/* ------------------------------------------------------------ culprits -- */

function renderCulprits() {
  var rows = report.culprits || [];
  var maxScore = Math.max.apply(null, rows.map(function (r) { return r.score; }).concat([1]));

  var cols = [
    {
      key: "label", label: "App or service", value: function (r) { return r.label; },
      render: function (r) {
        var box = el("div");
        var top = el("div");
        top.appendChild(document.createTextNode(r.label));
        if (r.system) {
          var tag = el("span", "tag-sys", "system");
          top.appendChild(tag);
        }
        box.appendChild(top);
        if (r.packages.length && r.packages[0] !== r.label) {
          box.appendChild(el("div", "pkg", r.packages[0]));
        }
        return box;
      }
    },
    { key: "uid", label: "uid", render: function (r) { return el("span", "mono faint", r.uid); } },
    {
      key: "score", label: "Score", right: true,
      render: function (r) {
        var box = el("div");
        box.style.cssText = "display:flex;align-items:center;gap:8px;justify-content:flex-end";
        box.appendChild(el("span", null, r.score.toFixed(1)));
        var bar = el("div", "bar");
        bar.style.width = "54px";
        var s = el("span");
        s.style.width = pct(r.score, maxScore) + "%";
        s.style.background = "var(--wakelock)";
        bar.appendChild(s);
        box.appendChild(bar);
        return box;
      }
    },
    { key: "total_mah", label: "Power", right: true, render: function (r) { return mah(r.total_mah) + " <span class='faint'>mAh</span>"; } },
    { key: "wakelock_ms", label: "Wakelock", right: true, render: function (r) { return dur(r.wakelock_ms || 0); } },
    { key: "wakelock_count", label: "Acquires", right: true },
    { key: "cpu_total_ms", label: "CPU", right: true, render: function (r) { return dur(r.cpu_total_ms); } },
    {
      key: "mix", label: "Mix", sort: false,
      render: function (r) {
        var parts = Object.keys(r.components).map(function (k) {
          return { value: r.components[k], colour: CHANNEL[k] || "var(--idle)", label: k, text: mah(r.components[k]) + " mAh" };
        });
        var totalC = parts.reduce(function (a, p) { return a + p.value; }, 0);
        var bar = segBar(parts, totalC);
        bar.style.width = "72px";
        return bar;
      }
    }
  ];

  $("#cul-table").textContent = "";
  $("#cul-table").appendChild(buildTable(cols, rows, {
    sortKey: "score",
    empty: "No per-uid data in this dump.",
    expand: culpritDetail
  }));
}

function culpritDetail(row) {
  var info = (report.uids || {})[row.uid] || {};
  var box = el("div");

  var dl = el("dl");
  function pair(k, v) {
    dl.appendChild(el("dt", null, k));
    dl.appendChild(el("dd", null, v));
  }
  pair("cpu user", dur(info.cpu_user_ms));
  pair("cpu system", dur(info.cpu_sys_ms));
  pair("screen-off cpu", dur(row.screen_off_cpu_ms));
  pair("foreground", dur(info.foreground_ms));
  pair("running", dur(info.running_ms));
  if (info.audio_ms) pair("audio", dur(info.audio_ms));
  if (info.vibrator_ms) pair("vibrator", dur(info.vibrator_ms));
  if (info.user_activity) pair("user activity", info.user_activity);
  box.appendChild(dl);

  function list(title, items, fmt) {
    if (!items || !items.length) return;
    box.appendChild(el("h4", null, title));
    var ul = el("ul");
    items.slice(0, 12).forEach(function (it) { ul.appendChild(el("li", null, fmt(it))); });
    if (items.length > 12) ul.appendChild(el("li", null, "and " + (items.length - 12) + " more"));
    box.appendChild(ul);
  }

  list("Wakelocks", (info.wakelocks || []).slice().sort(function (a, b) { return b.ms - a.ms; }),
    function (w) { return w.tag + "  " + dur(w.ms) + " \u00d7" + w.count; });
  list("Sensors", info.sensors,
    function (s) { return "sensor " + s.sensor + "  " + dur(s.ms) + " \u00d7" + s.count; });
  list("Processes", (info.procs || []).slice().sort(function (a, b) {
    return (b.cpu_user_ms + b.cpu_sys_ms) - (a.cpu_user_ms + a.cpu_sys_ms);
  }), function (p) { return p.name + "  " + dur(p.cpu_user_ms + p.cpu_sys_ms); });
  list("Services", (info.services || []).slice().sort(function (a, b) { return b.starts - a.starts; }),
    function (s) { return s.name.split(".").pop() + "  " + s.starts + " starts, " + dur(s.created_ms) + " alive"; });
  list("Jobs", info.jobs, function (j) { return j.name + "  " + dur(j.ms) + " \u00d7" + j.count; });
  list("Syncs", info.syncs, function (j) { return j.name + "  " + dur(j.ms) + " \u00d7" + j.count; });
  if (row.packages.length > 1) list("Packages", row.packages.map(function (p) { return { p: p }; }), function (o) { return o.p; });

  if (!dl.children.length && !box.querySelector("ul")) {
    box.appendChild(emptyNote("No detail block for this uid."));
  }
  return box;
}

/* ------------------------------------------------------------ timeline -- */

var tl = { start: 0, end: 0, full: 0, drag: null, wallClock: false };

function tlAnchor() {
  var iso = report.history && report.history.wall_clock_anchor;
  return iso ? new Date(iso) : null;
}

// Elapsed-ms since capture start -> a wall-clock HH:MM:SS string, using the
// anchor parsed from RESET:TIME or Start clock time. Falls back to elapsed
// formatting if no anchor was parseable, so callers never need to branch.
function tlTime(ms) {
  if (!tl.wallClock) return clock(ms);
  var anchor = tlAnchor();
  if (!anchor) return clock(ms);
  var d = new Date(anchor.getTime() + ms);
  var pad = function (n) { return (n < 10 ? "0" : "") + n; };
  return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
}

function renderTimeline() {
  var h = report.history || {};
  tl.full = h.duration_ms || 0;
  tl.start = 0;
  tl.end = tl.full;
  tl.wallClock = false;

  var s = report.summary || {};
  var banner = $("#tl-gap-banner");
  var totalMs = s.time_on_battery_ms || 0;
  if (totalMs && tl.full && totalMs - tl.full >= 20 * 60 * 1000 && totalMs / tl.full >= 3) {
    banner.hidden = false;
    banner.innerHTML = "<b>This covers " + dur(tl.full) + " of " + dur(totalMs) + " on battery.</b> " +
      "Android's history log is a fixed-size buffer that overwrites its oldest events, so only the " +
      "most recent window is scrubbable here even though the totals elsewhere on this page span the full session.";
  } else {
    banner.hidden = true;
  }

  drawTimeline();

  var scroller = $("#tl-scroller");
  scroller.onwheel = function (e) {
    if (!tl.full) return;
    e.preventDefault();
    var rect = scroller.getBoundingClientRect();
    var frac = Math.max(0, Math.min(1, (e.clientX - rect.left - 124) / (rect.width - 124)));
    var span = tl.end - tl.start;
    var factor = e.deltaY > 0 ? 1.25 : 0.8;
    var newSpan = Math.max(500, Math.min(tl.full, span * factor));
    var focus = tl.start + span * frac;
    tl.start = Math.max(0, focus - newSpan * frac);
    tl.end = Math.min(tl.full, tl.start + newSpan);
    tl.start = Math.max(0, tl.end - newSpan);
    drawTimeline();
  };
  scroller.onmousedown = function (e) {
    tl.drag = { x: e.clientX, start: tl.start, end: tl.end, w: scroller.clientWidth - 124 };
    e.preventDefault();
  };
  window.addEventListener("mousemove", function (e) {
    if (!tl.drag) return;
    var span = tl.drag.end - tl.drag.start;
    var shift = ((tl.drag.x - e.clientX) / tl.drag.w) * span;
    var s = Math.max(0, Math.min(tl.full - span, tl.drag.start + shift));
    tl.start = s;
    tl.end = s + span;
    drawTimeline();
  });
  window.addEventListener("mouseup", function () { tl.drag = null; });

  $("#tl-out").onclick = function () {
    var span = Math.min(tl.full, (tl.end - tl.start) * 2);
    var mid = (tl.start + tl.end) / 2;
    tl.start = Math.max(0, mid - span / 2);
    tl.end = Math.min(tl.full, tl.start + span);
    drawTimeline();
  };
  $("#tl-reset").onclick = function () {
    tl.start = 0; tl.end = tl.full; drawTimeline();
  };

  var hasAnchor = !!(h.wall_clock_anchor);
  var wallBtn = $("#tl-mode-wall");
  wallBtn.disabled = !hasAnchor;
  wallBtn.title = hasAnchor ? "" : "No parseable start time in this dump";
  $("#tl-mode-elapsed").onclick = function () {
    tl.wallClock = false;
    $("#tl-mode-elapsed").setAttribute("aria-current", "true");
    wallBtn.setAttribute("aria-current", "false");
    drawTimeline();
  };
  wallBtn.onclick = function () {
    if (wallBtn.disabled) return;
    tl.wallClock = true;
    wallBtn.setAttribute("aria-current", "true");
    $("#tl-mode-elapsed").setAttribute("aria-current", "false");
    drawTimeline();
  };
}

// Candidate tick spacings, in ms, chosen to look like a real clock rather
// than arbitrary fractions of whatever window happens to be visible.
var NICE_INTERVALS = [
  1000, 2000, 5000, 10000, 15000, 30000,
  60000, 2 * 60000, 5 * 60000, 10 * 60000, 15 * 60000, 30 * 60000,
  3600000, 2 * 3600000, 3 * 3600000, 6 * 3600000, 12 * 3600000
];

function niceInterval(spanMs) {
  var target = spanMs / 5;
  for (var i = 0; i < NICE_INTERVALS.length; i++) {
    if (NICE_INTERVALS[i] >= target) return NICE_INTERVALS[i];
  }
  return NICE_INTERVALS[NICE_INTERVALS.length - 1];
}

// Tick positions, elapsed ms from capture start, each a round number on
// whichever clock is showing: multiples of the interval from t=0 for
// elapsed mode, or from the real epoch for wall-clock mode, so the tick
// labels read 18:05 / 18:10 rather than 18:11:37 / 18:23:13.
function tlTicks() {
  var span = tl.end - tl.start;
  var step = niceInterval(span);
  var first;
  if (tl.wallClock) {
    var anchor = tlAnchor();
    if (anchor) {
      // Align in real epoch space, then convert back to elapsed ms - folding
      // the anchor offset through ceil() on a negated value instead gives
      // the wrong boundary because ceil() is not symmetric around zero.
      var wallStart = anchor.getTime() + tl.start;
      first = Math.ceil(wallStart / step) * step - anchor.getTime();
    } else {
      first = Math.ceil(tl.start / step) * step;
    }
  } else {
    first = Math.ceil(tl.start / step) * step;
  }
  var ticks = [];
  for (var t = first; t <= tl.end + 1 && ticks.length < 14; t += step) {
    if (t >= tl.start) ticks.push(t);
  }
  return ticks;
}

function drawTimeline() {
  var body = $("#tl-body");
  body.textContent = "";
  var spans = (report.history && report.history.spans) || [];
  var points = (report.history && report.history.points) || [];
  var span = Math.max(1, tl.end - tl.start);

  $("#tl-window").textContent = tlTime(tl.start) + " to " + tlTime(tl.end) +
    "  (" + dur(span) + " of " + dur(tl.full) + ")";

  // The derived power-state lane comes first: it is the closest thing to a
  // single answer to "what mode was the device in", built from screen+running
  // for dumps with no explicit doze=light/deep tokens (see parser.py). It is
  // rendered separately from the flag-based lanes below because its segments
  // key on `state`, not a lane name, and it is always labelled as derived.
  var powerState = (report.history && report.history.power_state) || [];
  if (powerState.length) {
    var pslabel = el("div", "tl-label", LANE_NAME.power_state);
    pslabel.title = "Approximated from screen + cpu-running signals, not a measured doze state";
    body.appendChild(pslabel);
    var pstrack = el("div", "tl-lane");
    powerState.forEach(function (s) {
      if (s.end < tl.start || s.start > tl.end) return;
      var seg = el("div", "seg");
      var left = pct(s.start - tl.start, span);
      var width = pct(Math.max(s.end - s.start, span * 0.0012), span);
      seg.style.left = left + "%";
      seg.style.width = Math.min(width, 100 - left) + "%";
      seg.style.background = LANE_COLOUR["power_state_" + s.state] || "var(--idle)";
      seg.dataset.info = JSON.stringify({
        lane: "power_state", start: s.start, end: s.end,
        tag: s.state.replace("_", " "), duration: s.duration
      });
      pstrack.appendChild(seg);
    });
    body.appendChild(pstrack);
  }

  var byLane = {};
  spans.forEach(function (s) {
    (byLane[s.lane] = byLane[s.lane] || []).push(s);
  });

  var order = ["screen", "doze", "tmpwhitelist", "top", "running", "wake_lock",
    "job", "sync", "audio", "wifi_radio", "wifi_scan", "mobile_radio",
    "phone_scanning", "gps", "sensor", "camera", "flashlight", "plugged",
    "usb_data"];
  var lanes = order.filter(function (k) { return byLane[k]; })
    .concat(Object.keys(byLane).filter(function (k) { return order.indexOf(k) < 0; }));

  if (!lanes.length) {
    body.appendChild(emptyNote("No battery history in this dump. Some OEM builds strip it."));
    return;
  }

  lanes.forEach(function (lane) {
    var label = el("div", "tl-label", LANE_NAME[lane] || lane);
    label.title = lane;
    body.appendChild(label);

    var track = el("div", "tl-lane");
    byLane[lane].forEach(function (s) {
      if (s.end < tl.start || s.start > tl.end) return;
      var seg = el("div", "seg");
      var left = pct(s.start - tl.start, span);
      var width = pct(Math.max(s.end - s.start, span * 0.0012), span);
      seg.style.left = left + "%";
      seg.style.width = Math.min(width, 100 - left) + "%";
      seg.style.background = LANE_COLOUR[lane] || "var(--idle)";
      seg.dataset.info = JSON.stringify({
        lane: lane, start: s.start, end: s.end, uid: s.uid, tag: s.tag,
        duration: s.duration
      });
      track.appendChild(seg);
    });
    body.appendChild(track);
  });

  // Wake reasons get their own marker lane.
  var wakes = points.filter(function (p) { return p.name === "wake_reason" || p.name === "screenwake"; });
  if (wakes.length) {
    body.appendChild(el("div", "tl-label", "Wake reasons"));
    var wtrack = el("div", "tl-lane");
    wakes.forEach(function (p) {
      if (p.t < tl.start || p.t > tl.end) return;
      var m = el("div", "pt");
      m.style.left = pct(p.t - tl.start, span) + "%";
      m.dataset.info = JSON.stringify({ lane: p.name, start: p.t, end: p.t, tag: p.value });
      wtrack.appendChild(m);
    });
    body.appendChild(wtrack);
  }

  var axis = $("#tl-axis");
  axis.textContent = "";
  tlTicks().forEach(function (t) {
    var tick = el("span", "tick", tlTime(t));
    tick.style.left = pct(t - tl.start, span) + "%";
    axis.appendChild(tick);
  });

  body.onmousemove = function (e) {
    var t = e.target;
    if (!t.dataset || !t.dataset.info) return;
    showReadout(JSON.parse(t.dataset.info));
  };
}

function showReadout(info) {
  var box = $("#tl-readout");
  box.textContent = "";
  var t = el("div", "t", tlTime(info.start) +
    (info.end > info.start ? " \u2192 " + tlTime(info.end) + "   (" + dur(info.end - info.start) + ")" : ""));
  box.appendChild(t);
  var title = el("div");
  title.style.fontWeight = "550";
  title.textContent = LANE_NAME[info.lane] || info.lane;
  box.appendChild(title);
  var parts = [];
  if (info.uid) {
    var label = (report.uid_names[info.uid] || {}).label || info.uid;
    parts.push(label + " (" + info.uid + ")");
  }
  if (info.tag) parts.push(info.tag);
  if (parts.length) {
    var sub = el("div", "dim");
    sub.style.fontSize = "13px";
    sub.textContent = parts.join("  \u00b7  ");
    box.appendChild(sub);
  }
  if (info.lane === "power_state") {
    var note = el("div", "faint");
    note.style.cssText = "font-size:11.5px;margin-top:2px";
    note.textContent = "Derived from screen + cpu-running signals, not a measured doze state.";
    box.appendChild(note);
  }
}

/* ----------------------------------------------------------- wakelocks -- */

function renderWakelocks() {
  var partial = report.partial_wakelocks || [];
  var maxMs = Math.max.apply(null, partial.map(function (w) { return w.ms; }).concat([1]));

  var cols = [
    {
      key: "tag", label: "Tag",
      render: function (w) {
        var box = el("div");
        box.appendChild(el("div", "mono", w.tag));
        var who = (report.uid_names[w.uid] || {}).label || w.uid;
        box.appendChild(el("div", "pkg", who + "  \u00b7  " + w.uid));
        return box;
      }
    },
    {
      key: "ms", label: "Held", right: true,
      render: function (w) {
        var box = el("div");
        box.style.cssText = "display:flex;align-items:center;gap:8px;justify-content:flex-end";
        box.appendChild(el("span", null, dur(w.ms)));
        var bar = el("div", "bar");
        bar.style.width = "70px";
        var s = el("span");
        s.style.width = pct(w.ms, maxMs) + "%";
        s.style.background = "var(--wakelock)";
        bar.appendChild(s);
        box.appendChild(bar);
        return box;
      }
    },
    { key: "count", label: "Acquires", right: true },
    {
      key: "avg", label: "Avg hold", right: true,
      value: function (w) { return w.count ? w.ms / w.count : w.ms; },
      render: function (w) { return dur(w.count ? w.ms / w.count : w.ms); }
    },
    { key: "max_ms", label: "Longest", right: true, render: function (w) { return dur(w.max_ms); } },
    { key: "actual_ms", label: "Wall time", right: true, render: function (w) { return dur(w.actual_ms); } }
  ];
  $("#wl-partial").textContent = "";
  $("#wl-partial").appendChild(buildTable(cols, partial, {
    sortKey: "ms", empty: "No app wakelocks recorded."
  }));

  var kernel = report.kernel_wakelocks || [];
  var kMax = Math.max.apply(null, kernel.map(function (w) { return w.ms; }).concat([1]));
  var kcols = [
    { key: "name", label: "Kernel lock", render: function (w) { return el("span", "mono", w.name); } },
    {
      key: "ms", label: "Held", right: true,
      render: function (w) {
        var box = el("div");
        box.style.cssText = "display:flex;align-items:center;gap:8px;justify-content:flex-end";
        box.appendChild(el("span", null, dur(w.ms)));
        var bar = el("div", "bar");
        bar.style.width = "70px";
        var s = el("span");
        s.style.width = pct(w.ms, kMax) + "%";
        s.style.background = "var(--radio)";
        bar.appendChild(s);
        box.appendChild(bar);
        return box;
      }
    },
    { key: "count", label: "Times", right: true },
    {
      key: "avg", label: "Avg", right: true,
      value: function (w) { return w.count ? w.ms / w.count : w.ms; },
      render: function (w) { return dur(w.count ? w.ms / w.count : w.ms); }
    }
  ];
  $("#wl-kernel").textContent = "";
  $("#wl-kernel").appendChild(buildTable(kcols, kernel, {
    sortKey: "ms", empty: "No kernel wakelocks in this dump."
  }));
}

/* ------------------------------------------------------------- wakeups -- */

function renderWakeups() {
  var rows = report.wakeup_reasons || [];
  var maxMs = Math.max.apply(null, rows.map(function (r) { return r.ms; }).concat([1]));

  var cols = [
    {
      key: "reason", label: "Reason",
      render: function (r) {
        var box = el("div");
        box.appendChild(el("div", "mono", r.reason));
        if (/abort/i.test(r.reason)) {
          box.appendChild(el("div", "pkg", "suspend attempt rejected"));
        }
        return box;
      }
    },
    {
      key: "ms", label: "Awake", right: true,
      render: function (r) {
        var box = el("div");
        box.style.cssText = "display:flex;align-items:center;gap:8px;justify-content:flex-end";
        box.appendChild(el("span", null, dur(r.ms)));
        var bar = el("div", "bar");
        bar.style.width = "70px";
        var s = el("span");
        s.style.width = pct(r.ms, maxMs) + "%";
        s.style.background = /abort/i.test(r.reason) ? "var(--alarm)" : "var(--radio)";
        bar.appendChild(s);
        box.appendChild(bar);
        return box;
      }
    },
    { key: "count", label: "Times", right: true },
    {
      key: "avg", label: "Avg awake", right: true,
      value: function (r) { return r.count ? r.ms / r.count : 0; },
      render: function (r) { return r.count ? dur(r.ms / r.count) : "\u2013"; }
    }
  ];
  $("#wu-table").textContent = "";
  $("#wu-table").appendChild(buildTable(cols, rows, {
    sortKey: "ms", empty: "No wakeup reasons recorded."
  }));

  var pids = report.per_pid || [];
  var pMax = Math.max.apply(null, pids.map(function (p) { return p.ms; }).concat([1]));
  var pcols = [
    { key: "pid", label: "pid", render: function (p) { return el("span", "mono", p.pid); } },
    {
      key: "ms", label: "Wake time", right: true,
      render: function (p) {
        var box = el("div");
        box.style.cssText = "display:flex;align-items:center;gap:8px;justify-content:flex-end";
        box.appendChild(el("span", null, dur(p.ms)));
        var bar = el("div", "bar");
        bar.style.width = "70px";
        var s = el("span");
        s.style.width = pct(p.ms, pMax) + "%";
        s.style.background = "var(--cpu)";
        bar.appendChild(s);
        box.appendChild(bar);
        return box;
      }
    },
    { key: "count", label: "Episodes", right: true }
  ];
  $("#wu-pid").textContent = "";
  $("#wu-pid").appendChild(buildTable(pcols, pids, {
    sortKey: "ms", empty: "No per-pid section in this dump."
  }));
}

/* --------------------------------------------------------------- daily -- */

function renderDaily() {
  var days = (report.daily || []).filter(function (d) { return d.steps > 0; });
  var chart = $("#dl-chart");
  chart.textContent = "";

  if (!days.length) {
    chart.appendChild(emptyNote("No daily discharge steps in this dump."));
    $("#dl-table").textContent = "";
    return;
  }

  var maxRate = Math.max.apply(null, days.map(function (d) {
    return Math.max(d.rate_screen_on || 0, d.rate_screen_off || 0, d.rate_all || 0);
  }).concat([1]));

  days.slice().reverse().forEach(function (d) {
    var row = el("div");
    row.style.marginBottom = "10px";
    var head = el("div");
    head.style.cssText = "display:flex;gap:10px;font-size:13px;margin-bottom:3px";
    head.appendChild(el("span", "mono", d.label));
    var r = el("span", "dim num");
    r.style.marginLeft = "auto";
    r.textContent = (d.rate_all != null ? d.rate_all.toFixed(2) : "\u2013") + " %/hr overall";
    head.appendChild(r);
    row.appendChild(head);

    [["screen on", d.rate_screen_on, "var(--screen)"],
     ["screen off", d.rate_screen_off, "var(--idle)"]].forEach(function (pair) {
      if (pair[1] == null) return;
      var line = el("div");
      line.style.cssText = "display:flex;align-items:center;gap:8px;margin-bottom:2px";
      var lbl = el("span", "faint", pair[0]);
      lbl.style.cssText = "font-size:11.5px;flex:0 0 66px";
      line.appendChild(lbl);
      var bar = el("div", "bar");
      bar.style.flex = "1";
      var s = el("span");
      s.style.width = pct(pair[1], maxRate) + "%";
      s.style.background = pair[2];
      bar.appendChild(s);
      line.appendChild(bar);
      var v = el("span", "num");
      v.style.cssText = "font-size:11.5px;flex:0 0 58px;text-align:right";
      v.textContent = pair[1].toFixed(2) + " %/hr";
      line.appendChild(v);
      row.appendChild(line);
    });
    chart.appendChild(row);
  });

  var lg = el("div", "legend");
  [["screen on", "var(--screen)"], ["screen off", "var(--idle)"]].forEach(function (p) {
    var item = el("span");
    var sw = el("i");
    sw.style.background = p[1];
    item.appendChild(sw);
    item.appendChild(document.createTextNode(p[0]));
    lg.appendChild(item);
  });
  chart.appendChild(lg);

  var cols = [
    { key: "label", label: "Day", render: function (d) { return el("span", "mono", d.label); } },
    { key: "steps", label: "Steps", right: true },
    { key: "total_ms", label: "On battery", right: true, render: function (d) { return dur(d.total_ms); } },
    { key: "screen_on_ms", label: "Screen on", right: true, render: function (d) { return dur(d.screen_on_ms); } },
    { key: "rate_screen_on", label: "%/hr on", right: true, render: function (d) { return d.rate_screen_on != null ? d.rate_screen_on.toFixed(2) : "\u2013"; } },
    { key: "rate_screen_off", label: "%/hr off", right: true, render: function (d) { return d.rate_screen_off != null ? d.rate_screen_off.toFixed(2) : "\u2013"; } },
    { key: "rate_all", label: "%/hr all", right: true, render: function (d) { return d.rate_all != null ? d.rate_all.toFixed(2) : "\u2013"; } }
  ];
  $("#dl-table").textContent = "";
  $("#dl-table").appendChild(buildTable(cols, days, { sortKey: null, empty: "No daily data." }));
}

/* ----------------------------------------------------------------- raw -- */

var rawTimer = null;

function renderRaw() {
  if (rawText) { filterRaw(); return; }
  fetch("/api/raw").then(function (r) { return r.text(); }).then(function (t) {
    rawText = t;
    filterRaw();
  });
}

function filterRaw() {
  var q = $("#raw-search").value.trim().toLowerCase();
  var lines = rawText.split("\n");
  var out = [];
  var shown = 0;
  var LIMIT = 3000;

  for (var i = 0; i < lines.length && shown < LIMIT; i++) {
    var line = lines[i];
    if (q && line.toLowerCase().indexOf(q) < 0) continue;
    shown++;
    var body = esc(line);
    if (q) {
      var idx = line.toLowerCase().indexOf(q);
      body = esc(line.slice(0, idx)) + "<mark>" + esc(line.slice(idx, idx + q.length)) +
        "</mark>" + esc(line.slice(idx + q.length));
    }
    out.push('<div class="raw-line"><span class="ln">' + (i + 1) + "</span><span>" + body + "</span></div>");
  }

  $("#raw-out").innerHTML = out.join("") || '<div class="empty">No lines match.</div>';
  var total = q ? lines.filter(function (l) { return l.toLowerCase().indexOf(q) >= 0; }).length : lines.length;
  $("#raw-count").textContent = q
    ? total + " matching lines" + (total > LIMIT ? ", showing first " + LIMIT : "")
    : lines.length + " lines" + (lines.length > LIMIT ? ", showing first " + LIMIT : "");
}


/* ---------------------------------------------------------------- live -- */

/* The browser polls the server; the server polls the device on its own
   adaptive cadence. These are deliberately decoupled, so a page left open
   never increases the load on the phone. */

var live = {
  timer: null, since: 0, selected: null, focus: null,
  starting: false, lastSeen: 0
};

var EV_COLOUR = {
  anr: "var(--alarm)", crash: "var(--alarm)", cpu: "var(--cpu)",
  wakelock: "var(--wakelock)", level: "var(--screen)", screen: "var(--idle)",
  doze: "var(--wifi)", job: "var(--cpu)", alarm: "var(--sensors)",
  jank: "var(--wakelock)", info: "var(--idle)"
};

function liveFetchDevices() {
  var box = $("#live-devices");
  box.className = "empty";
  box.textContent = "Looking for devices.";
  fetch("/api/live/devices").then(function (r) {
    return r.json().then(function (j) { return { ok: r.ok, body: j }; });
  }).then(function (res) {
    if (!res.ok) {
      box.className = "empty";
      box.textContent = res.body.error || "Could not list devices.";
      return;
    }
    var devices = res.body.devices || [];
    box.className = "";
    box.textContent = "";
    if (!devices.length) {
      box.className = "empty";
      box.textContent = "No device found. Connect one and enable usb debugging, " +
        "or pair over wireless adb, then rescan.";
      return;
    }
    devices.forEach(function (d, i) {
      var row = el("div", "dev-row");
      row.setAttribute("aria-selected", String(i === 0));
      if (i === 0) live.selected = d.serial;
      var name = el("span", null, d.model || d.serial);
      name.style.fontWeight = "550";
      row.appendChild(name);
      row.appendChild(el("span", "mono faint", d.serial));
      row.appendChild(el("span", "state", d.state));
      row.addEventListener("click", function () {
        live.selected = d.serial;
        box.querySelectorAll(".dev-row").forEach(function (r2) {
          r2.setAttribute("aria-selected", String(r2 === row));
        });
      });
      box.appendChild(row);
    });
  }).catch(function (err) {
    box.className = "empty";
    box.textContent = "Could not reach the server: " + err;
  });
}

function liveStart() {
  if (live.starting) return;
  var err = $("#live-error");
  err.textContent = "";
  if (!live.selected) { err.textContent = "Select a device first."; return; }
  live.starting = true;
  $("#live-start").textContent = "Connecting";
  fetch("/api/live/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      serial: live.selected,
      unplug: $("#live-unplug").checked,
      logcat: $("#live-logcat").checked
    })
  }).then(function (r) {
    return r.json().then(function (j) { return { ok: r.ok, body: j }; });
  }).then(function (res) {
    live.starting = false;
    $("#live-start").textContent = "Start session";
    if (!res.ok) { err.textContent = res.body.error || "Could not start."; return; }
    live.since = 0;
    $("#live-setup").hidden = true;
    $("#live-running").hidden = false;
    livePoll();
    live.timer = setInterval(livePoll, 2000);
  }).catch(function (e) {
    live.starting = false;
    $("#live-start").textContent = "Start session";
    err.textContent = "Could not start: " + e;
  });
}

function liveStop() {
  clearInterval(live.timer);
  live.timer = null;
  fetch("/api/live/stop", { method: "POST" }).then(function () {
    $("#live-running").hidden = true;
    $("#live-setup").hidden = false;
    $("#n-live").textContent = "";
    toast("Session stopped, charging restored");
    liveFetchDevices();
  });
}

function livePoll() {
  fetch("/api/live/state?since=" + live.since).then(function (r) { return r.json(); })
    .then(function (s) {
      if (!s.running) {
        clearInterval(live.timer);
        live.timer = null;
        $("#live-running").hidden = true;
        $("#live-setup").hidden = false;
        return;
      }
      live.since = s.seq;
      liveRender(s);
    }).catch(function () { /* transient, next tick retries */ });
}

function liveRender(s) {
  var d = s.device || {};
  $("#live-device").textContent = (d.model || d.serial || "device") +
    (d.android ? "  \u00b7  Android " + d.android : "");
  $("#live-mode").textContent = (s.dozing ? "dozing" : s.screen_on ? "screen on" : "screen off") +
    "  \u00b7  polling every " + s.interval + "s" +
    (s.unplug_applied ? "  \u00b7  charging masked" : "");
  $("#live-uptime").textContent = clock(s.uptime_s * 1000);
  $("#n-live").textContent = "\u25cf";

  var dot = $("#live-dot");
  dot.className = "live-dot" + (s.error ? " dead" : s.dozing ? " stale" : "");

  var l = s.latest || {};
  var metrics = [
    ["Current draw", l.current_ma != null ? Math.round(l.current_ma) + " mA" : "\u2013",
      s.current_source ? "from battery gauge" : "gauge not readable"],
    ["Drain rate", s.drain_pct_hr != null ? s.drain_pct_hr.toFixed(2) + " %/hr" : "measuring",
      s.drain_pct_hr == null ? "needs a level drop" : "observed"],
    ["Level", l.level != null ? l.level + "%" : "\u2013", l.status || ""],
    ["Battery temp", l.temp_c != null ? l.temp_c.toFixed(1) + "\u00b0C" : "\u2013",
      l.voltage_mv ? (l.voltage_mv / 1000).toFixed(2) + " V" : ""]
  ];
  var mbox = $("#live-metrics");
  mbox.textContent = "";
  metrics.forEach(function (m) {
    var c = el("div", "metric");
    c.appendChild(el("div", "label", m[0]));
    c.appendChild(el("div", "value", m[1]));
    c.appendChild(el("div", "foot", m[2]));
    mbox.appendChild(c);
  });

  liveTrace(s.trend || []);
  liveProcs(s.culprits || []);
  liveLocks(l.wakelocks || []);
  $("#live-top-sub").textContent = "Ranked by accumulated cpu time over " +
    clock(s.accum_seconds * 1000) + ", not a single noisy snapshot. Select one to filter logcat to it.";
  liveEvents(s.events || []);
  liveOverhead(s.overhead || {});
}

function liveTrace(trend) {
  var host = $("#live-trace");
  host.textContent = "";
  var pts = trend.filter(function (p) { return p.current_ma != null; });
  if (pts.length < 2) {
    host.appendChild(emptyNote("Waiting for a second reading."));
    return;
  }
  var W = 640, H = 110, pad = 6;
  var max = Math.max.apply(null, pts.map(function (p) { return p.current_ma; }));
  var min = Math.min.apply(null, pts.map(function (p) { return p.current_ma; }));
  var span = Math.max(1, max - min);
  var t0 = pts[0].t, t1 = pts[pts.length - 1].t;
  var tspan = Math.max(1, t1 - t0);

  var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Current draw over the session");
  svg.style.cssText = "width:100%;height:110px;display:block";

  // Shade screen-on stretches so spikes can be read in context.
  var runStart = null;
  pts.forEach(function (p, i) {
    var x = pad + ((p.t - t0) / tspan) * (W - pad * 2);
    if (p.screen_on && runStart === null) runStart = x;
    if ((!p.screen_on || i === pts.length - 1) && runStart !== null) {
      var rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect.setAttribute("x", runStart);
      rect.setAttribute("y", 0);
      rect.setAttribute("width", Math.max(1, x - runStart));
      rect.setAttribute("height", H - 14);
      rect.setAttribute("fill", "var(--screen)");
      rect.setAttribute("opacity", "0.10");
      svg.appendChild(rect);
      runStart = null;
    }
  });

  var dstr = pts.map(function (p, i) {
    var x = pad + ((p.t - t0) / tspan) * (W - pad * 2);
    var y = (H - 18) - ((p.current_ma - min) / span) * (H - 30);
    return (i ? "L" : "M") + x.toFixed(1) + "," + y.toFixed(1);
  }).join(" ");
  var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", dstr);
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "var(--cpu)");
  path.setAttribute("stroke-width", "1.8");
  svg.appendChild(path);
  host.appendChild(svg);

  var foot = el("div", "faint");
  foot.style.cssText = "display:flex;justify-content:space-between;font-size:11px;font-family:var(--mono)";
  foot.appendChild(el("span", null, Math.round(min) + " mA"));
  foot.appendChild(el("span", null, "peak " + Math.round(max) + " mA"));
  host.appendChild(foot);
}

function liveCpuSeconds(s) {
  if (s < 60) return s.toFixed(1) + "s";
  var m = Math.floor(s / 60), rem = s - m * 60;
  return m + "m " + Math.round(rem) + "s";
}

function liveProcs(culprits) {
  var host = $("#live-top");
  host.textContent = "";
  if (!culprits.length) {
    host.appendChild(emptyNote("No process samples yet."));
    return;
  }
  var max = Math.max.apply(null, culprits.map(function (c) { return c.cpu_seconds; }).concat([0.01]));
  culprits.forEach(function (c) {
    var row = el("div", "live-proc" + (c.stale ? " stale" : ""));
    row.setAttribute("aria-selected", String(live.focus === c.name));

    var left = el("div");
    left.style.cssText = "display:flex;flex-direction:column;gap:1px;min-width:0";
    left.appendChild(el("span", "name", c.name));
    var cur = el("span", "cur", "now " + c.last_cpu.toFixed(0) + "%  ·  peak " +
      c.peak_cpu.toFixed(0) + "%  ·  " + c.samples + " samples");
    left.appendChild(cur);
    row.appendChild(left);

    var right = el("div");
    right.style.cssText = "margin-left:auto;text-align:right;display:flex;flex-direction:column;gap:1px;flex:0 0 auto";
    right.appendChild(el("span", "accum", liveCpuSeconds(c.cpu_seconds)));
    var bar = el("div", "bar");
    bar.style.width = "84px";
    var seg = el("span");
    seg.style.width = pct(c.cpu_seconds, max) + "%";
    seg.style.background = "var(--cpu)";
    bar.appendChild(seg);
    right.appendChild(bar);
    row.appendChild(right);

    row.style.cssText = "display:flex;align-items:center;gap:10px;cursor:pointer;border-radius:var(--radius)";
    row.addEventListener("click", function () { liveFocus(c.name); });
    host.appendChild(row);
  });
}

function liveFocus(pkg) {
  var next = live.focus === pkg ? null : pkg;
  fetch("/api/live/focus", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ package: next })
  }).then(function (r) { return r.json(); }).then(function (j) {
    live.focus = next;
    toast(next ? (j.pid ? "Logcat filtered to " + next + " (pid " + j.pid + ")"
                        : next + " is not running, filter cleared")
               : "Logcat filter cleared");
  });
}

function liveLocks(locks) {
  var host = $("#live-locks");
  host.textContent = "";
  var title = el("div", "faint");
  title.style.cssText = "font-size:12px;margin-bottom:6px";
  title.textContent = locks.length ? "Wakelocks held right now" : "No wakelocks held right now";
  host.appendChild(title);
  locks.forEach(function (w) {
    var row = el("div");
    row.style.cssText = "display:flex;gap:10px;font-size:12.5px;padding:3px 0";
    var tag = el("span", "mono", w.tag);
    row.appendChild(tag);
    if (w.package) {
      var pk = el("span", "faint");
      pk.style.marginLeft = "auto";
      pk.textContent = w.package;
      row.appendChild(pk);
    }
    host.appendChild(row);
  });
}

function liveEvents(events) {
  var host = $("#live-events");
  if (!events.length && host.children.length) return;
  events.slice().reverse().forEach(function (e) {
    var row = el("div", "ev");
    var when = new Date(e.t * 1000);
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    row.appendChild(el("span", "when",
      pad(when.getHours()) + ":" + pad(when.getMinutes()) + ":" + pad(when.getSeconds())));
    var pip = el("span", "pip");
    pip.style.background = EV_COLOUR[e.kind] || "var(--idle)";
    row.appendChild(pip);
    var body = el("span", "body");
    var lbl = el("b", null, e.label);
    lbl.style.fontWeight = "550";
    body.appendChild(lbl);
    body.appendChild(document.createTextNode("  " + (e.text || "")));
    row.appendChild(body);
    host.insertBefore(row, host.firstChild);
  });
  while (host.children.length > 200) host.removeChild(host.lastChild);
  if (!host.children.length) host.appendChild(emptyNote("Nothing yet."));
}

function liveOverhead(o) {
  var host = $("#live-overhead");
  host.textContent = "";
  var items = [
    ["Shell commands", (o.commands || 0).toLocaleString(), (o.commands_per_min || 0) + " per minute"],
    ["Device shell time", (o.shell_ms_per_min || 0) + " ms/min", "time the shell was busy"],
    ["Duty cycle", (o.duty_pct || 0).toFixed(2) + "%", "of session spent polling"],
    ["Total", (o.shell_seconds || 0).toFixed(1) + "s", "since session start"]
  ];
  items.forEach(function (m) {
    var c = el("div", "metric");
    c.appendChild(el("div", "label", m[0]));
    c.appendChild(el("div", "value", m[1]));
    c.appendChild(el("div", "foot", m[2]));
    host.appendChild(c);
  });
}

function renderLive() {
  liveFetchDevices();
}


/* ------------------------------------------------------------- control -- */

/* Ranking prefers real trace measurements when a capture exists, and falls
   back to the live accumulator otherwise. The source is always labelled, so
   it is clear whether a number is measured or sampled. */

var ctl = { rows: [], selected: {}, source: "none", pkgInfo: {}, pending: null,
            net: {}, signals: {}, showNet: false };

function ctlRefreshState() {
  fetch("/api/trace/state").then(function (r) { return r.json(); }).then(function (s) {
    $("#ctl-tp").textContent = s.trace_processor
      ? "trace analysis ready" : "run: pip install perfetto --break-system-packages";
    if (s.analysis && s.analysis.culprits) {
      ctl.source = "trace";
      ctl.rows = s.analysis.culprits.map(function (c) {
        return { name: c.name, cpu: c.cpu_ms, wakeups: c.wakeups,
                 threads: c.threads, isPkg: c.is_package, unit: "ms cpu" };
      });
      $("#ctl-source").textContent = "measured from a " +
        (s.capture ? s.capture.duration_s + "s" : "") + " perfetto trace";
    }
    ctlRender();
  }).catch(function () {});
}

function ctlLoadFallback() {
  fetch("/api/live/state?since=0").then(function (r) { return r.json(); })
    .then(function (s) {
      if (ctl.source === "trace") return;
      if (!s.running || !s.culprits) return;
      ctl.source = "live";
      ctl.rows = s.culprits.map(function (c) {
        return { name: c.name, cpu: c.cpu_seconds * 1000, wakeups: null,
                 isPkg: /^[a-z][\w]*(\.[\w]+){2,}$/.test(c.name), unit: "ms cpu" };
      });
      $("#ctl-source").textContent = "sampled from the live session, capture a trace for measured data";
      ctlRender();
    }).catch(function () {});
}

function ctlBytes(n) {
  if (!n) return "0";
  if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " GB";
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(0) + " KB";
  return n + " B";
}

/* Network bytes are a separate signal from cpu time, not a component of it:
   an app can be near-invisible to a cpu ranking while steadily sending data.
   Shown as its own column rather than folded into the score. */
function ctlLoadNetwork() {
  fetch("/api/control/network").then(function (r) {
    if (!r.ok) return null;
    return r.json();
  }).then(function (j) {
    if (!j) return;
    ctl.net = {};
    (j.rows || []).forEach(function (row) {
      ctl.net[row.package] = row;
    });
    ctl.showNet = true;
    ctlRender();
  }).catch(function () {});
}

function ctlLoadPackages() {
  fetch("/api/control/packages").then(function (r) {
    if (!r.ok) return null;
    return r.json();
  }).then(function (p) {
    if (!p) return;
    ctl.pkgInfo = {};
    (p.system || []).forEach(function (n) { ctl.pkgInfo[n] = { system: true }; });
    (p.third_party || []).forEach(function (n) { ctl.pkgInfo[n] = { system: false }; });
    (p.disabled || []).forEach(function (n) {
      ctl.pkgInfo[n] = ctl.pkgInfo[n] || {};
      ctl.pkgInfo[n].disabled = true;
    });
    ctlRender();
  }).catch(function () {});
}

function ctlRender() {
  var host = $("#ctl-list");
  host.textContent = "";
  var hideSystem = $("#ctl-hide-system").checked;

  // The cpu ranking cannot see an app that transfers data without burning
  // cpu - exactly the case worth catching. Any package with network traffic
  // is merged in, so it appears with a real network figure and a zero cpu
  // figure rather than being absent entirely.
  var merged = ctl.rows.slice();
  var seen = {};
  merged.forEach(function (r) { seen[r.name] = true; });
  if (ctl.showNet) {
    Object.keys(ctl.net).forEach(function (pkg) {
      if (seen[pkg] || !ctl.net[pkg].total) return;
      if (!/^[a-z][\w]*(\.[\w]+){2,}$/.test(pkg)) return;
      merged.push({ name: pkg, cpu: 0, wakeups: null, isPkg: true, netOnly: true });
    });
  }

  var rows = merged.filter(function (r) {
    if (!r.isPkg) return false;
    var info = ctl.pkgInfo[r.name];
    if (hideSystem && info && info.system) return false;
    return true;
  });

  // Sort by whichever signal is larger relative to its own column max, so a
  // network-only entry is not stranded at the bottom by a zero cpu figure.
  var maxCpu = Math.max.apply(null, rows.map(function (r) { return r.cpu || 0; }).concat([1]));
  var maxNet = Math.max.apply(null, rows.map(function (r) {
    var n = ctl.net[r.name]; return n ? n.total : 0;
  }).concat([1]));
  rows.sort(function (a, b) {
    var an = ctl.net[a.name], bn = ctl.net[b.name];
    var as = Math.max((a.cpu || 0) / maxCpu, (an ? an.total : 0) / maxNet);
    var bs = Math.max((b.cpu || 0) / maxCpu, (bn ? bn.total : 0) / maxNet);
    return bs - as;
  });

  if (!rows.length) {
    host.appendChild(emptyNote(ctl.rows.length
      ? "No user packages in the ranking. Uncheck hide system packages to see everything."
      : "Nothing ranked yet. Start a live session, or capture a trace for measured data."));
    ctlUpdateBar();
    return;
  }

  var max = maxCpu;
  rows.forEach(function (r) {
    var info = ctl.pkgInfo[r.name] || {};
    var row = el("div", "ctl-row");

    var cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!ctl.selected[r.name];
    cb.addEventListener("change", function () {
      if (cb.checked) ctl.selected[r.name] = true;
      else delete ctl.selected[r.name];
      ctlUpdateBar();
    });
    row.appendChild(cb);

    var name = el("span", "pkg-name", r.name);
    name.style.flex = "1";
    row.appendChild(name);

    if (r.cpu != null) {
      row.appendChild(el("span", "metric-s", (r.cpu >= 1000
        ? (r.cpu / 1000).toFixed(1) + "s" : Math.round(r.cpu) + "ms") + " cpu"));
    }
    if (r.wakeups != null) {
      row.appendChild(el("span", "metric-s", r.wakeups + " wakeups"));
    }

    var net = ctl.net[r.name];
    if (ctl.showNet) {
      var netCell = el("span", "metric-s", net ? ctlBytes(net.total) + " net" : "\u2013");
      if (net && net.total) {
        netCell.title = "rx " + ctlBytes(net.rx) + " / tx " + ctlBytes(net.tx) +
          (net.shared_uid ? "  (shared uid " + net.uid + ", not attributable to one package)" : "");
      }
      row.appendChild(netCell);
    }

    var bar = el("div", "bar");
    bar.style.width = "70px";
    var seg = el("span");
    seg.style.width = pct(r.cpu || 0, max) + "%";
    seg.style.background = "var(--cpu)";
    bar.appendChild(seg);
    row.appendChild(bar);

    if (info.disabled) row.appendChild(el("span", "ctl-tag off", "disabled"));
    else if (info.system) row.appendChild(el("span", "ctl-tag sys", "system"));
    else if (info.system === false) row.appendChild(el("span", "ctl-tag", "user app"));

    host.appendChild(row);
  });
  ctlUpdateBar();
}

function ctlUpdateBar() {
  var names = Object.keys(ctl.selected);
  $("#ctl-actionbar").hidden = names.length === 0;
  $("#ctl-selcount").textContent = names.length + " selected";
}

function ctlPlanAll(action) {
  var names = Object.keys(ctl.selected);
  if (!names.length) return;
  Promise.all(names.map(function (pkg) {
    return fetch("/api/control/plan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ package: pkg, action: action })
    }).then(function (r) { return r.json().then(function (j) {
      return r.ok ? j : { package: pkg, action: action, blocked: j.error }; }); });
  })).then(function (plans) { ctlShowModal(action, plans); });
}

function ctlShowModal(action, plans) {
  ctl.pending = { action: action, plans: plans };
  var runnable = plans.filter(function (p) { return !p.blocked; });
  var blocked = plans.filter(function (p) { return p.blocked; });

  $("#ctl-modal-title").textContent = (plans[0] && plans[0].label ? plans[0].label : action)
    + " " + runnable.length + (runnable.length === 1 ? " app" : " apps");

  var body = $("#ctl-modal-body");
  body.textContent = "";

  if (runnable.length) {
    body.appendChild(el("p", "dim", runnable[0].effect));
    var cmds = el("div", "cmd-block");
    cmds.textContent = runnable.map(function (p) { return p.command; }).join("\n");
    cmds.style.whiteSpace = "pre-wrap";
    body.appendChild(cmds);

    runnable.forEach(function (p) {
      if (p.warning) {
        var w = el("div", "ctl-warn");
        w.textContent = p.package + ": " + p.warning;
        body.appendChild(w);
      }
    });

    if (runnable[0].undo_command) {
      var undo = el("p", "faint");
      undo.style.fontSize = "12.5px";
      undo.textContent = "Reversible: " + runnable[0].undo_label.toLowerCase() +
        " with " + runnable[0].undo_command.replace(runnable[0].package, "<package>");
      body.appendChild(undo);
    }
  }

  blocked.forEach(function (p) {
    var b = el("div", "ctl-block");
    b.textContent = p.blocked;
    body.appendChild(b);
  });

  $("#ctl-modal-run").disabled = runnable.length === 0;
  $("#ctl-modal-run").textContent = "Run on device";
  $("#ctl-modal").hidden = false;
}

function ctlRunPending() {
  if (!ctl.pending) return;
  var runnable = ctl.pending.plans.filter(function (p) { return !p.blocked; });
  var action = ctl.pending.action;
  $("#ctl-modal-run").disabled = true;
  $("#ctl-modal-run").textContent = "Running";

  var done = 0, failed = 0;
  Promise.all(runnable.map(function (p) {
    return fetch("/api/control/apply", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ package: p.package, action: action })
    }).then(function (r) { return r.json().then(function (j) {
      if (r.ok && j.ok) done++; else failed++; }); })
      .catch(function () { failed++; });
  })).then(function () {
    $("#ctl-modal").hidden = true;
    ctl.pending = null;
    ctl.selected = {};
    toast(done + " applied" + (failed ? ", " + failed + " failed" : ""));
    ctlLoadPackages();
  });
}

function ctlCapture() {
  var btn = $("#ctl-capture");
  var dur = parseInt($("#ctl-duration").value, 10) || 30;
  btn.disabled = true;
  btn.textContent = "Capturing " + dur + "s";
  var status = $("#ctl-capture-status");
  status.className = "";
  status.textContent = "Recording. Use the phone normally so the trace has something in it.";

  fetch("/api/trace/capture", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ duration: dur })
  }).then(function (r) { return r.json().then(function (j) {
    return { ok: r.ok, body: j }; }); })
    .then(function (res) {
      btn.disabled = false;
      btn.textContent = "Capture trace";
      if (!res.ok) { status.className = "empty"; status.textContent = res.body.error; return; }
      var cap = res.body.capture || {};
      var msg = "Captured " + Math.round((cap.bytes || 0) / 1024) + " KB over " +
        cap.duration_s + "s.";
      if (res.body.analysis_error) {
        msg += " Analysis unavailable: " + res.body.analysis_error;
        status.className = "empty";
      } else {
        msg += " Trace kept at " + cap.path;
        status.className = "";
      }
      status.textContent = msg;
      ctlRefreshState();
    }).catch(function (e) {
      btn.disabled = false;
      btn.textContent = "Capture trace";
      status.className = "empty";
      status.textContent = "Capture failed: " + e;
    });
}

function renderControl() {
  ctlRefreshState();
  ctlLoadFallback();
  ctlLoadPackages();
  ctlLoadNetwork();
}

/* -------------------------------------------------------------- routing -- */

var RENDERED = {};
var RENDERERS = {
  overview: renderOverview,
  culprits: renderCulprits,
  timeline: renderTimeline,
  wakelocks: renderWakelocks,
  wakeups: renderWakeups,
  daily: renderDaily,
  raw: renderRaw,
  live: renderLive,
  control: renderControl
};

function show(view) {
  document.querySelectorAll(".view").forEach(function (v) {
    v.classList.toggle("active", v.id === "view-" + view);
  });
  document.querySelectorAll("#nav button").forEach(function (b) {
    b.setAttribute("aria-current", b.dataset.view === view ? "true" : "false");
  });
  if (view === "live") {
    if (!RENDERED.live) { renderLive(); RENDERED.live = true; }
  } else if (view === "control") {
    renderControl();
  } else if (report && RENDERERS[view] && !RENDERED[view]) {
    RENDERERS[view]();
    RENDERED[view] = true;
  } else if (report && view === "raw") {
    renderRaw();
  }
  if (location.hash !== "#" + view) history.replaceState(null, "", "#" + view);
  window.scrollTo(0, 0);
}

function counts() {
  $("#n-culprits").textContent = (report.culprits || []).length || "";
  $("#n-timeline").textContent = ((report.history || {}).spans || []).length || "";
  $("#n-wakelocks").textContent = (report.partial_wakelocks || []).length || "";
  $("#n-wakeups").textContent = (report.wakeup_reasons || []).length || "";
  $("#n-daily").textContent = (report.daily || []).filter(function (d) { return d.steps; }).length || "";

  document.querySelectorAll("#nav button").forEach(function (b) {
    var v = b.dataset.view;
    var disabled =
      (v === "timeline" && !((report.history || {}).spans || []).length) ||
      (v === "wakelocks" && !(report.partial_wakelocks || []).length && !(report.kernel_wakelocks || []).length) ||
      (v === "wakeups" && !(report.wakeup_reasons || []).length && !(report.per_pid || []).length) ||
      (v === "daily" && !(report.daily || []).filter(function (d) { return d.steps; }).length) ||
      (v === "culprits" && !(report.culprits || []).length);
    b.disabled = !!disabled;
  });
}

function applyReport(data) {
  report = data;
  rawText = "";
  RENDERED = {};
  $("#source").textContent = (report.meta && report.meta.source) || "unknown source";
  $("#meta").textContent = report.meta
    ? (report.meta.lines.toLocaleString() + " lines \u00b7 " +
       Math.round(report.meta.bytes / 1024) + " KB")
    : "";
  counts();
  var view = (location.hash || "#overview").slice(1);
  if (!RENDERERS[view]) view = "overview";
  RENDERED = {};
  show(view);
}

function load() {
  fetch("/api/report").then(function (r) {
    if (r.status === 503) {
      $("#ov-intro").textContent =
        "No dump loaded. Drop a batterystats file onto this page, or restart " +
        "battviz with a file path.";
      return null;
    }
    return r.json();
  }).then(function (data) {
    if (data) applyReport(data);
  }).catch(function (err) {
    $("#ov-intro").textContent = "Could not reach the battviz server: " + err;
  });
}

function upload(file) {
  toast("Parsing " + file.name);
  file.text().then(function (text) {
    return fetch("/api/upload", {
      method: "POST",
      headers: { "Content-Type": "text/plain", "X-Filename": file.name },
      body: text
    });
  }).then(function (r) {
    return r.json().then(function (j) { return { ok: r.ok, body: j }; });
  }).then(function (res) {
    if (!res.ok) { toast(res.body.error || "Could not parse that file"); return; }
    location.hash = "#overview";
    load();
    toast("Loaded " + res.body.source);
  }).catch(function (err) { toast("Upload failed: " + err); });
}

/* ---------------------------------------------------------------- init -- */

document.addEventListener("DOMContentLoaded", function () {
  $("#host").textContent = location.host;

  document.querySelectorAll("#nav button").forEach(function (b) {
    b.addEventListener("click", function () { show(b.dataset.view); });
  });

  $("#btn-open").addEventListener("click", function () { $("#file").click(); });
  $("#file").addEventListener("change", function (e) {
    if (e.target.files[0]) upload(e.target.files[0]);
    e.target.value = "";
  });

  $("#live-start").addEventListener("click", liveStart);
  $("#live-stop").addEventListener("click", liveStop);
  $("#live-refresh").addEventListener("click", liveFetchDevices);
  $("#ctl-capture").addEventListener("click", ctlCapture);
  $("#ctl-hide-system").addEventListener("change", ctlRender);
  document.querySelectorAll("#ctl-actionbar button[data-act]").forEach(function (b) {
    b.addEventListener("click", function () { ctlPlanAll(b.dataset.act); });
  });
  $("#ctl-modal-cancel").addEventListener("click", function () {
    $("#ctl-modal").hidden = true; ctl.pending = null;
  });
  $("#ctl-modal-run").addEventListener("click", ctlRunPending);
  $("#ctl-modal-copy").addEventListener("click", function () {
    if (!ctl.pending) return;
    var text = ctl.pending.plans.filter(function (p) { return !p.blocked; })
      .map(function (p) { return "adb shell " + p.command; }).join("\n");
    if (navigator.clipboard) navigator.clipboard.writeText(text);
    toast("Commands copied, run them yourself");
  });

  $("#live-reset").addEventListener("click", function () {
    fetch("/api/live/reset", { method: "POST" }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, body: j }; });
    }).then(function (res) {
      if (!res.ok) { toast(res.body.error || "Could not reset."); return; }
      toast("Culprit ranking reset");
    });
  });

  $("#live-sync").addEventListener("click", function () {
    toast("Pulling full batterystats");
    fetch("/api/live/deep-sync", { method: "POST" })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        if (!res.ok) { toast(res.body.error || "Deep sync failed"); return; }
        RENDERED = {}; RENDERED.live = true;
        load();
        toast("Loaded into the static views, " + res.body.sections + " sections");
      });
  });

  $("#raw-search").addEventListener("input", function () {
    clearTimeout(rawTimer);
    rawTimer = setTimeout(filterRaw, 140);
  });

  var drop = $("#drop");
  var depth = 0;
  window.addEventListener("dragenter", function (e) { e.preventDefault(); depth++; drop.classList.add("on"); });
  window.addEventListener("dragover", function (e) { e.preventDefault(); });
  window.addEventListener("dragleave", function () { if (--depth <= 0) { depth = 0; drop.classList.remove("on"); } });
  window.addEventListener("drop", function (e) {
    e.preventDefault();
    depth = 0;
    drop.classList.remove("on");
    if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]);
  });

  window.addEventListener("hashchange", function () {
    var v = (location.hash || "#overview").slice(1);
    if (RENDERERS[v]) show(v);
  });

  load();
});
