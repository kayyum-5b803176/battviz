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

var tl = { start: 0, end: 0, full: 0, drag: null };

function renderTimeline() {
  var h = report.history || {};
  tl.full = h.duration_ms || 0;
  tl.start = 0;
  tl.end = tl.full;
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
}

function drawTimeline() {
  var body = $("#tl-body");
  body.textContent = "";
  var spans = (report.history && report.history.spans) || [];
  var points = (report.history && report.history.points) || [];
  var span = Math.max(1, tl.end - tl.start);

  $("#tl-window").textContent = clock(tl.start) + " to " + clock(tl.end) +
    "  (" + dur(span) + " of " + dur(tl.full) + ")";

  var byLane = {};
  spans.forEach(function (s) {
    (byLane[s.lane] = byLane[s.lane] || []).push(s);
  });

  var order = ["screen", "top", "running", "wake_lock", "job", "sync", "audio",
    "wifi_radio", "wifi_scan", "mobile_radio", "phone_scanning", "gps",
    "sensor", "camera", "flashlight", "plugged", "usb_data"];
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
  for (var i = 0; i <= 4; i++) {
    axis.appendChild(el("span", null, clock(tl.start + (span * i) / 4)));
  }

  body.onmousemove = function (e) {
    var t = e.target;
    if (!t.dataset || !t.dataset.info) return;
    showReadout(JSON.parse(t.dataset.info));
  };
}

function showReadout(info) {
  var box = $("#tl-readout");
  box.textContent = "";
  var t = el("div", "t", clock(info.start) +
    (info.end > info.start ? " \u2192 " + clock(info.end) + "   (" + dur(info.end - info.start) + ")" : ""));
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

/* -------------------------------------------------------------- routing -- */

var RENDERED = {};
var RENDERERS = {
  overview: renderOverview,
  culprits: renderCulprits,
  timeline: renderTimeline,
  wakelocks: renderWakelocks,
  wakeups: renderWakeups,
  daily: renderDaily,
  raw: renderRaw
};

function show(view) {
  document.querySelectorAll(".view").forEach(function (v) {
    v.classList.toggle("active", v.id === "view-" + view);
  });
  document.querySelectorAll("#nav button").forEach(function (b) {
    b.setAttribute("aria-current", b.dataset.view === view ? "true" : "false");
  });
  if (report && RENDERERS[view] && !RENDERED[view]) {
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
