"""Parser for Android `dumpsys batterystats` dumps.

Standard library only. Every section is parsed independently and defensively:
OEM builds reorder, rename and omit sections, so a missing or malformed block
makes that one section absent from the report rather than failing the parse.
"""

import re
from collections import defaultdict
from datetime import datetime

# ---------------------------------------------------------------- duration --

_DUR_TOKEN = re.compile(r"(\d+)\s*(ms|d|h|m|s)")
_UNIT_MS = {"d": 86400000, "h": 3600000, "m": 60000, "s": 1000, "ms": 1}


def parse_duration(text):
    """'2d 6h 43m 15s 600ms', '+14m18s974ms', '948ms' -> milliseconds."""
    if not text:
        return None
    total = 0
    found = False
    for value, unit in _DUR_TOKEN.findall(text):
        total += int(value) * _UNIT_MS[unit]
        found = True
    return total if found else None


def human_duration(ms):
    if ms is None:
        return "-"
    if ms < 1000:
        return "%dms" % ms
    s, ms = divmod(int(ms), 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    parts = []
    if d:
        parts.append("%dd" % d)
    if h:
        parts.append("%dh" % h)
    if m:
        parts.append("%dm" % m)
    if s or not parts:
        parts.append("%ds" % s)
    return " ".join(parts)


# ------------------------------------------------------------ uid labelling --

# Android's reserved uids. Anything else resolves from Apk/Proc lines in the
# dump itself, and falls back to the bare uid string.
SYSTEM_UIDS = {
    "0": "kernel / root",
    "1000": "android system",
    "1001": "telephony / radio",
    "1002": "bluetooth",
    "1003": "graphics",
    "1004": "input",
    "1005": "audio",
    "1006": "camera",
    "1007": "log",
    "1008": "compass",
    "1009": "mount",
    "1010": "wifi stack",
    "1011": "adb",
    "1012": "package installer",
    "1013": "mediaserver",
    "1014": "dhcp client",
    "1015": "sdcard (rw)",
    "1016": "vpn",
    "1017": "keystore",
    "1018": "usb",
    "1019": "drm",
    "1020": "mdns responder",
    "1021": "gps / location",
    "1023": "media (rw)",
    "1024": "mtp",
    "1026": "drm rpc",
    "1027": "nfc",
    "1028": "sdcard (r)",
    "1036": "logd",
    "1040": "media extractor",
    "1041": "audioserver",
    "1046": "media codec",
    "1047": "cameraserver",
    "1048": "firewall",
    "1049": "trunks (tpm)",
    "1050": "nvram",
    "1051": "dns resolver (netd)",
    "1052": "dns resolver (tether)",
    "1053": "webview zygote",
    "1054": "vehicle network",
    "1058": "tombstoned",
    "1060": "secure element (ese)",
    "1061": "ota update",
    "1063": "lowpan",
    "1064": "hardware security module",
    "1066": "statsd",
    "1067": "incidentd",
    "1068": "secure element",
    "1069": "low memory killer",
    "1070": "live lock daemon",
    "1071": "iorapd",
    "1072": "gpu service",
    "1073": "network stack",
    "1074": "gsid",
    "1075": "fs-verity cert",
    "1076": "credstore",
    "1077": "external storage",
    "2000": "adb shell",
    "2001": "cache",
    "2002": "diag",
    "9999": "nobody",
}


def _uid_sort_key(uid):
    m = re.match(r"^u(\d+)([as])(\d+)$", uid)
    if m:
        return (1, int(m.group(1)), m.group(2), int(m.group(3)))
    if uid.isdigit():
        return (0, int(uid), "", 0)
    return (2, 0, uid, 0)


# ------------------------------------------------------------------ helpers --

def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _split_events(text):
    """Split a history line's event list on spaces outside double quotes."""
    tokens, buf, quoted = [], [], False
    for ch in text:
        if ch == '"':
            quoted = not quoted
            buf.append(ch)
        elif ch == " " and not quoted:
            if buf:
                tokens.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        tokens.append("".join(buf))
    return tokens


# ------------------------------------------------------------------- report --

class Report(dict):
    """Plain dict; subclassed only so the parse helpers read clearly."""


def parse(text):
    lines = text.splitlines()
    report = Report(
        summary={},
        power={"global": [], "uids": []},
        partial_wakelocks=[],
        kernel_wakelocks=[],
        wakeup_reasons=[],
        connectivity={},
        history={"events": [], "spans": [], "levels": [], "duration_ms": 0},
        uids={},
        uid_names={},
        daily=[],
        per_pid=[],
        cpu_freqs=[],
        sections=[],
        findings=[],
    )

    _parse_history(lines, report)
    _parse_per_pid(lines, report)
    _parse_daily(lines, report)
    _parse_summary(lines, report)
    _parse_connectivity(lines, report)
    _parse_power_use(lines, report)
    _parse_wakelock_lists(lines, report)
    _parse_wakeup_reasons(lines, report)
    _parse_cpu_freqs(lines, report)
    _parse_uid_blocks(lines, report)

    _resolve_names(report)
    _score_culprits(report)
    _derive_findings(report)
    return report


# ------------------------------------------------------------------ history --

_HIST_HEAD = re.compile(r"^Battery History\s*\(")
_HIST_LINE = re.compile(r"^\s+(\+?[\dhmsd]+)\s+\((\d+)\)\s+(.*)$")

# Flags that describe a duration, so they become spans in the timeline.
SPAN_FLAGS = [
    "screen", "running", "wake_lock", "job", "sync", "audio", "video",
    "wifi_radio", "wifi_scan", "wifi_full_lock", "gps", "sensor", "camera",
    "flashlight", "phone_scanning", "plugged", "usb_data", "top", "fg",
    "package_inst", "bluetooth_scan_on", "mobile_radio", "wifi_running",
    "tmpwhitelist",
]

# State-value tokens (name=value, not a +/- flag) worth turning into spans
# because their duration matters. Standard AOSP dumps carry `doze=light` /
# `doze=deep` / `doze=off`; OEM builds vary and some (observed: OPlus/Realme)
# carry none of this at all, in which case the resulting span list is simply
# empty and the UI hides the lane rather than showing nothing useful.
STATE_SPAN_NAMES = ["doze"]


# RESET:TIME and "Start clock time" have been seen in more than one format
# across Android versions/OEMs; each is tried in turn rather than assuming one.
_CLOCK_FORMATS = ["%Y-%m-%d-%H-%M-%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]


def _parse_clock(text):
    if not text:
        return None
    text = text.strip()
    for fmt in _CLOCK_FORMATS:
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            continue
    return None


def _parse_history(lines, report):
    start = None
    for i, line in enumerate(lines):
        if _HIST_HEAD.match(line):
            start = i + 1
            break
    if start is None:
        return
    report["sections"].append("Battery History")

    events = []
    t = 0
    level = None
    reset_clock = None

    for line in lines[start:]:
        if not line.strip():
            continue
        if not line.startswith(" "):
            break
        m = _HIST_LINE.match(line)
        if not m:
            continue
        stamp, _cnt, rest = m.groups()

        if stamp == "0":
            t = 0
        else:
            delta = parse_duration(stamp)
            # History deltas are cumulative offsets from the reset point, not
            # increments, so assign rather than accumulate.
            if delta is not None:
                t = delta

        rest = rest.strip()
        if rest.startswith("RESET:TIME:"):
            reset_clock = rest.split(":", 1)[1].replace("TIME:", "").strip()
            events.append({"t": t, "level": level, "kind": "reset",
                           "label": "history reset", "raw": rest})
            continue
        if rest.startswith("*"):
            events.append({"t": t, "level": level, "kind": "marker",
                           "label": rest.strip("*"), "raw": rest})
            continue

        tokens = _split_events(rest)
        if tokens and re.match(r"^\d{3}$", tokens[0]):
            level = int(tokens[0])
            tokens = tokens[1:]

        parsed = []
        for tok in tokens:
            parsed.append(_parse_event_token(tok))
        if parsed:
            events.append({"t": t, "level": level, "kind": "events",
                           "items": parsed, "raw": rest})

    report["history"]["events"] = events
    report["history"]["reset_clock"] = reset_clock
    report["history"]["duration_ms"] = events[-1]["t"] if events else 0
    report["history"]["levels"] = [
        {"t": e["t"], "level": e["level"]} for e in events if e["level"] is not None
    ]
    # start_clock is parsed later by _parse_summary; wall_clock_anchor is
    # finalised there once both candidates are available.
    report["history"]["wall_clock_anchor"] = _parse_clock(reset_clock)
    _build_spans(report)


_EVENT_RE = re.compile(r'^([+-]?)([A-Za-z_][\w]*)(?:=([^:"]*)(?::"(.*)")?)?$')


def _parse_event_token(tok):
    m = _EVENT_RE.match(tok)
    if not m:
        return {"type": "raw", "text": tok}
    sign, name, value, quoted = m.groups()
    if sign in ("+", "-"):
        return {
            "type": "flag",
            "on": sign == "+",
            "name": name,
            "uid": value or None,
            "tag": quoted,
        }
    return {"type": "state", "name": name, "value": value,
            "tag": quoted, "raw": tok}


def _covers(spans_for_lane, t):
    return any(s["start"] <= t < (s["end"] or t) for s in spans_for_lane)


def _derive_power_state(spans, end_t):
    """Approximate active/idle-awake/suspended from screen+running.

    This exists for dumps with no explicit doze=light/deep tokens (observed:
    OPlus/Realme builds carry none at all). screen on is unambiguous;
    screen off with the cpu still running approximates a light-idle state
    (a wakelock or job is being serviced); screen off with running also off
    approximates full suspend. This is a derived approximation, not a
    measurement, and the UI labels it as such rather than presenting it as
    equivalent to real doze state data.
    """
    screen = [s for s in spans if s["lane"] == "screen"]
    running = [s for s in spans if s["lane"] == "running"]
    if not screen and not running:
        return []

    bounds = {0, end_t}
    for s in screen + running:
        bounds.add(s["start"])
        bounds.add(s["end"] or end_t)
    bounds = sorted(b for b in bounds if 0 <= b <= end_t)

    out = []
    cur = None
    for i in range(len(bounds) - 1):
        t0, t1 = bounds[i], bounds[i + 1]
        if t1 <= t0:
            continue
        mid = (t0 + t1) / 2.0
        if _covers(screen, mid):
            state = "active"
        elif _covers(running, mid):
            state = "idle_awake"
        else:
            state = "suspended"
        if cur and cur["state"] == state:
            cur["end"] = t1
        else:
            if cur:
                out.append(cur)
            cur = {"lane": "power_state", "state": state, "start": t0, "end": t1}
    if cur:
        out.append(cur)
    for s in out:
        s["duration"] = s["end"] - s["start"]
    return out


def _build_spans(report):
    """Turn paired +flag / -flag events into timeline spans."""
    open_spans = {}
    spans = []
    end_t = report["history"]["duration_ms"]

    for ev in report["history"]["events"]:
        if ev["kind"] != "events":
            continue
        for item in ev["items"]:
            if item["type"] != "flag":
                continue
            name = item["name"]
            if name not in SPAN_FLAGS:
                continue
            if item["on"]:
                if name in open_spans:
                    prev = open_spans.pop(name)
                    prev["end"] = ev["t"]
                    spans.append(prev)
                open_spans[name] = {
                    "lane": name,
                    "start": ev["t"],
                    "end": None,
                    "uid": item["uid"],
                    "tag": item["tag"],
                }
            else:
                cur = open_spans.pop(name, None)
                if cur is not None:
                    cur["end"] = ev["t"]
                    spans.append(cur)

    for name, cur in open_spans.items():
        cur["end"] = end_t
        cur["truncated"] = True
        spans.append(cur)

    # doze (and any future STATE_SPAN_NAMES entry) arrives as name=value
    # rather than a +/- flag, so its duration comes from tracking value
    # changes instead of on/off pairs. "off" is the non-doze baseline and is
    # not rendered as a segment.
    open_state = {}
    for ev in report["history"]["events"]:
        if ev["kind"] != "events":
            continue
        for item in ev["items"]:
            if item["type"] != "state" or item["name"] not in STATE_SPAN_NAMES:
                continue
            name = item["name"]
            value = item["value"]
            cur = open_state.get(name)
            if cur and cur["value"] == value:
                continue
            if cur:
                cur["end"] = ev["t"]
                if cur["value"] and cur["value"] != "off":
                    spans.append(cur)
            open_state[name] = {"lane": name, "start": ev["t"], "end": None,
                                "value": value, "uid": None, "tag": None}
    for name, cur in open_state.items():
        cur["end"] = end_t
        cur["truncated"] = True
        if cur["value"] and cur["value"] != "off":
            spans.append(cur)

    for s in spans:
        s["duration"] = max(0, (s["end"] or 0) - s["start"])
    spans.sort(key=lambda s: s["start"])
    report["history"]["spans"] = spans
    report["history"]["has_doze_data"] = any(s["lane"] == "doze" for s in spans)
    report["history"]["power_state"] = _derive_power_state(spans, end_t)

    # Point events worth marking on the timeline.
    points = []
    for ev in report["history"]["events"]:
        if ev["kind"] != "events":
            continue
        for item in ev["items"]:
            if item["type"] == "state" and item["name"] in (
                "wake_reason", "screenwake", "wakeupap", "brightness",
                "status", "plug", "temp", "phone_state",
            ):
                points.append({
                    "t": ev["t"],
                    "name": item["name"],
                    "value": item["tag"] or item["value"],
                })
    report["history"]["points"] = points


# ----------------------------------------------------------------- per-pid --

def _parse_per_pid(lines, report):
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith("Per-PID Stats:"))
    except StopIteration:
        return
    report["sections"].append("Per-PID Stats")
    agg = defaultdict(lambda: {"ms": 0, "count": 0})
    for line in lines[start + 1:]:
        if not line.strip():
            break
        m = re.match(r"\s*PID (\d+) wake time: (\S+)", line)
        if not m:
            continue
        pid, dur = m.groups()
        ms = parse_duration(dur) or 0
        agg[pid]["ms"] += ms
        agg[pid]["count"] += 1
    report["per_pid"] = sorted(
        ({"pid": p, "ms": v["ms"], "count": v["count"]} for p, v in agg.items()),
        key=lambda r: -r["ms"],
    )


# ------------------------------------------------------------- daily stats --

_STEP = re.compile(r"#(\d+):\s*(\S+)\s+to\s+(\d+)\s*\(([^)]*)\)")


def _parse_daily(lines, report):
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith("Daily stats:"))
    except StopIteration:
        return
    report["sections"].append("Daily stats")

    days = []
    current = None
    bucket = None

    for line in lines[start + 1:]:
        if line and not line.startswith(" "):
            break
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("Current start time:"):
            current = {"label": "today", "from": stripped.split(":", 1)[1].strip(),
                       "to": None, "discharge": [], "charge": []}
            days.append(current)
            continue
        m = re.match(r"Daily from (\S+) to (\S+):", stripped)
        if m:
            current = {"label": m.group(1)[:10], "from": m.group(1),
                       "to": m.group(2), "discharge": [], "charge": []}
            days.append(current)
            bucket = None
            continue
        if current is None:
            continue

        if "discharge step durations" in stripped.lower():
            bucket = "discharge"
            continue
        if "charge step durations" in stripped.lower():
            bucket = "charge"
            continue

        m = _STEP.match(stripped)
        if m and bucket:
            ms = parse_duration(m.group(2)) or 0
            current[bucket].append({
                "index": int(m.group(1)),
                "ms": ms,
                "level": int(m.group(3)),
                "flags": [f.strip() for f in m.group(4).split(",")],
                "screen_on": "screen-on" in m.group(4),
                "rate_pct_hr": round(3600000.0 / ms, 2) if ms else None,
            })

    for d in days:
        disch = d["discharge"]
        d["steps"] = len(disch)
        d["total_ms"] = sum(s["ms"] for s in disch)
        on = [s for s in disch if s["screen_on"]]
        off = [s for s in disch if not s["screen_on"]]
        d["screen_on_ms"] = sum(s["ms"] for s in on)
        d["screen_off_ms"] = sum(s["ms"] for s in off)
        d["rate_all"] = round(3600000.0 * len(disch) / d["total_ms"], 2) if d["total_ms"] else None
        d["rate_screen_on"] = round(3600000.0 * len(on) / d["screen_on_ms"], 2) if d["screen_on_ms"] else None
        d["rate_screen_off"] = round(3600000.0 * len(off) / d["screen_off_ms"], 2) if d["screen_off_ms"] else None

    report["daily"] = days


# ---------------------------------------------------------------- summary --

_STATS_HEAD = "Statistics since last charge:"


def _stats_start(lines):
    for i, l in enumerate(lines):
        if l.startswith(_STATS_HEAD):
            return i
    return None


_PCT = re.compile(r"\(([\d.]+)%\)")


def _parse_summary(lines, report):
    start = _stats_start(lines)
    if start is None:
        return
    report["sections"].append("Statistics since last charge")
    s = report["summary"]

    for line in lines[start + 1:start + 60]:
        t = line.strip()
        if not t:
            continue
        if t.startswith("Estimated battery capacity:"):
            s["capacity_mah"] = _first_float(t)
        elif t.startswith("Last learned battery capacity:"):
            s["learned_capacity_mah"] = _first_float(t)
        elif t.startswith("Min learned battery capacity:"):
            s["min_learned_mah"] = _first_float(t)
        elif t.startswith("Max learned battery capacity:"):
            s["max_learned_mah"] = _first_float(t)
        elif t.startswith("Time on battery:"):
            s["time_on_battery_ms"] = parse_duration(t.split(":", 1)[1].split("(")[0])
            p = _PCT.search(t)
            s["time_on_battery_pct"] = float(p.group(1)) if p else None
        elif t.startswith("Time on battery screen off:"):
            s["screen_off_ms"] = parse_duration(t.split(":", 1)[1].split("(")[0])
        elif t.startswith("Time on battery screen doze:"):
            s["screen_doze_ms"] = parse_duration(t.split(":", 1)[1].split("(")[0])
        elif t.startswith("Total run time:"):
            s["total_run_ms"] = parse_duration(t.split(":", 1)[1].split("realtime")[0])
        elif t.startswith("Discharge:"):
            s["discharge_mah"] = _first_float(t)
        elif t.startswith("Screen off discharge:"):
            s["screen_off_discharge_mah"] = _first_float(t)
        elif t.startswith("Screen on discharge:"):
            s["screen_on_discharge_mah"] = _first_float(t)
        elif t.startswith("Start clock time:"):
            s["start_clock"] = t.split(":", 1)[1].strip()
            if not report["history"].get("wall_clock_anchor"):
                report["history"]["wall_clock_anchor"] = _parse_clock(s["start_clock"])
        elif t.startswith("Screen on:"):
            s["screen_on_ms"] = parse_duration(t.split(":", 1)[1].split("(")[0])
            p = _PCT.search(t)
            s["screen_on_pct"] = float(p.group(1)) if p else None
            c = re.search(r"\)\s*(\d+)x", t)
            s["screen_on_count"] = int(c.group(1)) if c else None
        elif t.startswith("Device light idling:"):
            s["light_idle_ms"] = parse_duration(t.split(":", 1)[1].split("(")[0])
            p = _PCT.search(t)
            s["light_idle_pct"] = float(p.group(1)) if p else None
        elif t.startswith("Device deep idling:"):
            s["deep_idle_ms"] = parse_duration(t.split(":", 1)[1].split("(")[0])
            p = _PCT.search(t)
            s["deep_idle_pct"] = float(p.group(1)) if p else None
        elif t.startswith("Total partial wakelock time:"):
            s["total_partial_wakelock_ms"] = parse_duration(t.split(":", 1)[1])

    # Screen brightness distribution.
    bright = {}
    grabbing = False
    for line in lines[start:start + 60]:
        t = line.strip()
        if t.startswith("Screen brightnesses:"):
            grabbing = True
            continue
        if grabbing:
            m = re.match(r"(\w+)\s+([^:]*?)\s*\(([\d.]+)%\)$", t)
            if m and ":" not in t:
                bright[m.group(1)] = {"ms": parse_duration(m.group(2)),
                                      "pct": float(m.group(3))}
            else:
                grabbing = False
    s["brightness"] = bright

    # Drain totals live in the power-use block.
    for line in lines[start:]:
        t = line.strip()
        if t.startswith("Capacity:") and "Computed drain:" in t:
            m = re.search(r"Capacity:\s*([\d.]+),\s*Computed drain:\s*([\d.]+),"
                          r"\s*actual drain:\s*([\d.]+)", t)
            if m:
                s["power_capacity_mah"] = float(m.group(1))
                s["computed_drain_mah"] = float(m.group(2))
                s["actual_drain_mah"] = float(m.group(3))
            break


def _first_float(text):
    m = re.search(r"([-\d.]+)", text.split(":", 1)[1] if ":" in text else text)
    return float(m.group(1)) if m else None


# ----------------------------------------------------------- connectivity --

def _parse_connectivity(lines, report):
    try:
        start = next(i for i, l in enumerate(lines)
                     if "CONNECTIVITY POWER SUMMARY START" in l)
        end = next(i for i, l in enumerate(lines)
                   if "CONNECTIVITY POWER SUMMARY END" in l)
    except StopIteration:
        return
    report["sections"].append("Connectivity power summary")

    conn = {"cellular": {}, "wifi": {}, "gps": {}}
    group = None
    for line in lines[start:end]:
        t = line.strip()
        if t.startswith("Cellular Statistics"):
            group = "cellular"
            continue
        if t.startswith("Wifi Statistics"):
            group = "wifi"
            continue
        if t.startswith("GPS Statistics"):
            group = "gps"
            continue
        if group is None or ":" not in t:
            continue
        key, _, val = t.partition(":")
        key = key.strip()
        val = val.strip()
        if not val:
            continue
        entry = {"raw": val}
        ms = parse_duration(val)
        if ms is not None and re.search(r"\d\s*(ms|s|m|h)\b", val):
            entry["ms"] = ms
        p = _PCT.search(val)
        if p:
            entry["pct"] = float(p.group(1))
        m = re.match(r"^([\d.]+)\s*(mAh|B|KB|MB)$", val)
        if m:
            entry["value"] = float(m.group(1))
            entry["unit"] = m.group(2)
        conn[group][key] = entry
    report["connectivity"] = conn


# ------------------------------------------------------------- power usage --

_UID_POWER = re.compile(r"^UID (\S+):\s*([\d.eE+-]+)\s*\(([^)]*)\)")
_GLOBAL_POWER = re.compile(r"^(\w[\w ]*):\s*([\d.eE+-]+)\s*apps:\s*([\d.eE+-]+)"
                           r"(?:\s*duration:\s*(.*))?$")


def _parse_power_use(lines, report):
    try:
        start = next(i for i, l in enumerate(lines)
                     if l.strip().startswith("Estimated power use (mAh):"))
    except StopIteration:
        return
    report["sections"].append("Estimated power use")

    glob, uids = [], []
    for line in lines[start + 1:]:
        t = line.strip()
        if not t:
            continue
        if line and not line.startswith("    "):
            break
        if t.startswith("Capacity:") or t == "Global":
            continue

        m = _UID_POWER.match(t)
        if m:
            uid, total, comps = m.groups()
            components = {}
            for cm in re.finditer(r"(\w+)=([\d.eE+-]+)", comps):
                components[cm.group(1)] = float(cm.group(2))
            uids.append({"uid": uid, "total_mah": float(total),
                         "components": components})
            continue

        m = _GLOBAL_POWER.match(t)
        if m:
            name, mah, apps, dur = m.groups()
            glob.append({"component": name.strip(), "mah": float(mah),
                         "apps_mah": float(apps),
                         "duration_ms": parse_duration(dur) if dur else None})
            continue

        if t.startswith("UID ") or t.startswith("Uid "):
            continue
        if re.match(r"^[A-Za-z]", t) and ":" not in t:
            break

    report["power"]["global"] = glob
    report["power"]["uids"] = sorted(uids, key=lambda u: -u["total_mah"])
    report["power"]["total_global_mah"] = round(sum(g["mah"] for g in glob), 4)
    report["power"]["total_uid_mah"] = round(sum(u["total_mah"] for u in uids), 4)


# ------------------------------------------------------------- wakelocks --

# Tags and reasons routinely contain colons ("Abort: Last active Wakeup
# Source: eventpoll"), so the name group is greedy and the duration group is
# anchored to the "(N times)" suffix instead.
_KERNEL_WL = re.compile(r"^Kernel Wake lock (.+):\s*([^:]*?)\s*\((\d+) times\)")
_PARTIAL_WL = re.compile(
    r"^Wake lock (\S+)\s+(.+):\s*([^:]*?)\s*\((\d+) times\)"
    r"(?:\s*max=(\d+))?(?:\s*actual=(\d+))?"
)


def _parse_wakelock_lists(lines, report):
    kernel, partial = [], []
    for line in lines:
        t = line.strip()
        if t.startswith("Kernel Wake lock "):
            m = _KERNEL_WL.match(t)
            if m:
                kernel.append({
                    "name": m.group(1),
                    "ms": parse_duration(m.group(2)) or 0,
                    "count": int(m.group(3)),
                })
        elif t.startswith("Wake lock ") and " realtime" in t:
            m = _PARTIAL_WL.match(t)
            if m:
                uid, tag, dur, count, mx, actual = m.groups()
                if not re.match(r"^(u?\d|u\d+[as]\d+)", uid):
                    continue
                partial.append({
                    "uid": uid,
                    "tag": tag.strip(),
                    "ms": parse_duration(dur) or 0,
                    "count": int(count),
                    "max_ms": int(mx) if mx else None,
                    "actual_ms": int(actual) if actual else None,
                })
    if kernel:
        report["sections"].append("Kernel wake locks")
        report["kernel_wakelocks"] = sorted(kernel, key=lambda r: -r["ms"])
    if partial:
        report["sections"].append("Partial wake locks")
        report["partial_wakelocks"] = sorted(partial, key=lambda r: -r["ms"])


_WAKEUP = re.compile(r"^Wakeup reason (.+):\s*([^:]*?)\s*\((\d+) times\)\s*realtime")
_WAKEUP_BARE = re.compile(r"^Wakeup reason (.+?)\s+realtime\s*$")


def _parse_wakeup_reasons(lines, report):
    out = []
    for line in lines:
        t = line.strip()
        if not t.startswith("Wakeup reason "):
            continue
        m = _WAKEUP.match(t)
        if m:
            out.append({"reason": m.group(1).strip(),
                        "ms": parse_duration(m.group(2)) or 0,
                        "count": int(m.group(3))})
            continue
        m = _WAKEUP_BARE.match(t)
        if m:
            out.append({"reason": m.group(1).strip(), "ms": 0, "count": 0})
    if out:
        report["sections"].append("Wakeup reasons")
        report["wakeup_reasons"] = sorted(out, key=lambda r: (-r["ms"], -r["count"]))


def _parse_cpu_freqs(lines, report):
    for line in lines:
        t = line.strip()
        if t.startswith("CPU freqs:"):
            report["cpu_freqs"] = [int(x) for x in t.split(":", 1)[1].split()]
            return


# ----------------------------------------------------------- per-uid blocks --

_UID_HEAD = re.compile(r"^  ((?:u\d+[as]\d+)|(?:u\d+)|(?:\d+)):\s*$")


def _parse_uid_blocks(lines, report):
    start = _stats_start(lines)
    if start is None:
        return
    blocks = {}
    current = None
    body = []

    for line in lines[start:]:
        m = _UID_HEAD.match(line)
        if m:
            if current is not None:
                blocks[current] = body
            current = m.group(1)
            body = []
            continue
        if current is not None:
            if line.strip() and _indent(line) <= 2:
                blocks[current] = body
                current = None
                body = []
                continue
            body.append(line)
    if current is not None:
        blocks[current] = body

    if blocks:
        report["sections"].append("Per-uid detail")
    for uid, body_lines in blocks.items():
        report["uids"][uid] = _parse_uid_body(body_lines)


def _parse_uid_body(body):
    info = {
        "wakelocks": [], "sensors": [], "jobs": [], "syncs": [],
        "procs": [], "apks": [], "services": [],
        "cpu_user_ms": None, "cpu_sys_ms": None,
        "cpu_per_freq": [], "cpu_screen_off_per_freq": [],
        "foreground_ms": None, "running_ms": None, "cached_ms": None,
        "audio_ms": None, "vibrator_ms": None, "user_activity": None,
        "wifi_running_ms": None, "mobile_radio_ms": None,
        "raw": [],
    }
    current_apk = None
    current_service = None

    for line in body:
        t = line.strip()
        if not t:
            continue
        info["raw"].append(t)

        m = re.match(r"^Wake lock (.+?):\s*(.*?)\s*partial\s*\((\d+) times\)", t)
        if m:
            info["wakelocks"].append({
                "tag": m.group(1), "ms": parse_duration(m.group(2)) or 0,
                "count": int(m.group(3)),
            })
            continue
        m = re.match(r"^Sensor (\S+):\s*(.*?)\s*\((\d+) times\)", t)
        if m:
            info["sensors"].append({
                "sensor": m.group(1).rstrip(":"),
                "ms": parse_duration(m.group(2).split(",")[0]) or 0,
                "count": int(m.group(3)),
            })
            continue
        m = re.match(r"^Job (.+?):\s*(.*?)\s*\((\d+) times\)", t)
        if m:
            info["jobs"].append({"name": m.group(1),
                                 "ms": parse_duration(m.group(2)) or 0,
                                 "count": int(m.group(3))})
            continue
        m = re.match(r"^Sync (.+?):\s*(.*?)\s*\((\d+) times\)", t)
        if m:
            info["syncs"].append({"name": m.group(1),
                                  "ms": parse_duration(m.group(2)) or 0,
                                  "count": int(m.group(3))})
            continue
        m = re.match(r"^Total cpu time: u=(.*?)\s+s=(.*?)\s*$", t)
        if m:
            info["cpu_user_ms"] = parse_duration(m.group(1)) or 0
            info["cpu_sys_ms"] = parse_duration(m.group(2)) or 0
            continue
        if t.startswith("Total cpu time per freq:"):
            info["cpu_per_freq"] = [int(x) for x in t.split(":", 1)[1].split()]
            continue
        if t.startswith("Total screen-off cpu time per freq:"):
            info["cpu_screen_off_per_freq"] = [int(x) for x in t.split(":", 1)[1].split()]
            continue
        m = re.match(r"^Proc (.+):$", t)
        if m:
            current_proc = {"name": m.group(1), "cpu_user_ms": 0, "cpu_sys_ms": 0}
            info["procs"].append(current_proc)
            continue
        m = re.match(r"^CPU: (.*?) usr \+ (.*?) krn ; (.*?) fg", t)
        if m and info["procs"]:
            info["procs"][-1]["cpu_user_ms"] = parse_duration(m.group(1)) or 0
            info["procs"][-1]["cpu_sys_ms"] = parse_duration(m.group(2)) or 0
            info["procs"][-1]["cpu_fg_ms"] = parse_duration(m.group(3)) or 0
            continue
        m = re.match(r"^Apk (.+):$", t)
        if m:
            current_apk = m.group(1)
            info["apks"].append(current_apk)
            continue
        m = re.match(r"^Service (.+):$", t)
        if m:
            current_service = {"apk": current_apk, "name": m.group(1),
                               "created_ms": 0, "starts": 0, "launches": 0}
            info["services"].append(current_service)
            continue
        m = re.match(r"^Created for: (.*?) uptime", t)
        if m and info["services"]:
            info["services"][-1]["created_ms"] = parse_duration(m.group(1)) or 0
            continue
        m = re.match(r"^Starts: (\d+), launches: (\d+)", t)
        if m and info["services"]:
            info["services"][-1]["starts"] = int(m.group(1))
            info["services"][-1]["launches"] = int(m.group(2))
            continue
        for key, prefix in (("foreground_ms", "Foreground for:"),
                            ("running_ms", "Total running:"),
                            ("cached_ms", "Cached for:"),
                            ("wifi_running_ms", "Wifi running:"),
                            ("mobile_radio_ms", "Mobile radio active:")):
            if t.startswith(prefix):
                info[key] = parse_duration(t.split(":", 1)[1])
        m = re.match(r"^Audio: (.*?) realtime", t)
        if m:
            info["audio_ms"] = parse_duration(m.group(1))
            continue
        m = re.match(r"^Vibrator: (.*?) realtime", t)
        if m:
            info["vibrator_ms"] = parse_duration(m.group(1))
            continue
        if t.startswith("User activity:"):
            info["user_activity"] = t.split(":", 1)[1].strip()

    info["cpu_total_ms"] = (info["cpu_user_ms"] or 0) + (info["cpu_sys_ms"] or 0)
    return info


# ------------------------------------------------------------ name resolve --

def _resolve_names(report):
    names = defaultdict(set)

    for uid, info in report["uids"].items():
        for apk in info["apks"]:
            names[uid].add(apk)
        for proc in info["procs"]:
            base = proc["name"].split(":")[0]
            if "." in base:
                names[uid].add(base)

    for ev in report["history"]["events"]:
        if ev["kind"] != "events":
            continue
        for item in ev["items"]:
            uid = item.get("uid")
            tag = item.get("tag")
            if not uid or not tag:
                continue
            if item.get("name") in ("fg", "top", "job", "sync", "proc", "start"):
                pkg = tag.split("/")[0]
                if "." in pkg:
                    names[uid].add(pkg)
            elif item.get("type") == "state" and item["name"] in ("fg", "top"):
                if "." in tag:
                    names[uid].add(tag)

    resolved = {}
    for uid in set(list(names.keys()) + list(report["uids"].keys())
                   + [u["uid"] for u in report["power"]["uids"]]):
        pkgs = sorted(names.get(uid, []))
        label = SYSTEM_UIDS.get(uid)
        if not label:
            label = pkgs[0] if pkgs else uid
        resolved[uid] = {"label": label, "packages": pkgs,
                         "system": uid in SYSTEM_UIDS}
    report["uid_names"] = resolved


def label_for(report, uid):
    entry = report["uid_names"].get(uid)
    return entry["label"] if entry else uid


# ------------------------------------------------------------- culprit score --

def _score_culprits(report):
    """Blend modelled power with behavioural signals.

    Estimated mAh alone under-weights apps that wake a sleeping device, so the
    score also counts screen-off wakelock time and wakelock acquisitions. Each
    input is normalised to its own maximum, then weighted.
    """
    power_by_uid = {u["uid"]: u for u in report["power"]["uids"]}
    wl_ms = defaultdict(int)
    wl_count = defaultdict(int)
    for wl in report["partial_wakelocks"]:
        wl_ms[wl["uid"]] += wl["ms"]
        wl_count[wl["uid"]] += wl["count"]

    screen_off_cpu = {}
    for uid, info in report["uids"].items():
        screen_off_cpu[uid] = sum(info.get("cpu_screen_off_per_freq") or [])

    uids = set(power_by_uid) | set(wl_ms) | set(screen_off_cpu)
    max_mah = max([u["total_mah"] for u in report["power"]["uids"]] or [0]) or 1
    max_wl = max(list(wl_ms.values()) or [0]) or 1
    max_cnt = max(list(wl_count.values()) or [0]) or 1
    max_cpu = max(list(screen_off_cpu.values()) or [0]) or 1

    rows = []
    for uid in uids:
        p = power_by_uid.get(uid, {"total_mah": 0.0, "components": {}})
        mah = p["total_mah"]
        score = (
            0.50 * (mah / max_mah)
            + 0.25 * (wl_ms[uid] / max_wl)
            + 0.15 * (wl_count[uid] / max_cnt)
            + 0.10 * (screen_off_cpu.get(uid, 0) / max_cpu)
        ) * 100
        info = report["uids"].get(uid, {})
        rows.append({
            "uid": uid,
            "label": label_for(report, uid),
            "packages": report["uid_names"].get(uid, {}).get("packages", []),
            "system": report["uid_names"].get(uid, {}).get("system", False),
            "total_mah": round(mah, 5),
            "components": p.get("components", {}),
            "wakelock_ms": wl_ms[uid],
            "wakelock_count": wl_count[uid],
            "screen_off_cpu_ms": screen_off_cpu.get(uid, 0) * 10,
            "cpu_total_ms": info.get("cpu_total_ms"),
            "sensors": len(info.get("sensors", [])),
            "services": len(info.get("services", [])),
            "score": round(score, 1),
        })
    rows.sort(key=lambda r: -r["score"])
    report["culprits"] = rows


# ---------------------------------------------------------------- findings --

def _derive_findings(report):
    out = []
    s = report["summary"]
    hist_ms = report["history"].get("duration_ms") or 0

    # The detailed event log is a fixed-size ring buffer (the "X used of
    # 4096KB" figure in the Battery History header); once full, the oldest
    # events are silently evicted while the aggregate stats keep counting
    # regardless, since they are a separate running total, not derived from
    # the history log. On a device that has been up a while, this means the
    # scrubbable timeline covers far less than "time on battery" suggests,
    # which reads as data going missing unless it is called out explicitly.
    if s.get("time_on_battery_ms") and hist_ms:
        ratio = s["time_on_battery_ms"] / hist_ms if hist_ms else 0
        if ratio >= 3 and (s["time_on_battery_ms"] - hist_ms) >= 20 * 60 * 1000:
            out.append({
                "level": "warn",
                "title": "Timeline shows less than the full session",
                "body": "The detailed event log only has room for the most "
                        "recent %s (Android's history buffer is a fixed 4096KB "
                        "ring buffer that overwrites its oldest entries), even "
                        "though this device has been on battery for %s since "
                        "its last reset. The totals on this page still cover "
                        "the full %s; only the scrubbable Timeline is limited "
                        "to the most recent window."
                        % (human_duration(hist_ms), human_duration(s["time_on_battery_ms"]),
                           human_duration(s["time_on_battery_ms"])),
                "goto": "timeline",
            })

    total_wl = sum(w["ms"] for w in report["partial_wakelocks"]) or 0
    if report["partial_wakelocks"] and total_wl:
        top = report["partial_wakelocks"][0]
        share = 100.0 * top["ms"] / total_wl
        if share >= 25:
            out.append({
                "level": "warn",
                "title": "One wakelock dominates awake time",
                "body": "%s held %s for %s, %.0f%% of all partial wakelock time."
                        % (label_for(report, top["uid"]), top["tag"],
                           human_duration(top["ms"]), share),
                "goto": "wakelocks",
            })

    for wl in report["partial_wakelocks"][:12]:
        if wl["count"] >= 40 and wl["ms"] < 2000:
            out.append({
                "level": "warn",
                "title": "Wakelock thrashing",
                "body": "%s acquired %s %d times for only %s total, a pattern "
                        "that costs more in wakeups than in hold time."
                        % (label_for(report, wl["uid"]), wl["tag"], wl["count"],
                           human_duration(wl["ms"])),
                "goto": "wakelocks",
            })
            break

    aborts = [w for w in report["wakeup_reasons"] if "Abort" in w["reason"]]
    if aborts:
        ms = sum(a["ms"] for a in aborts)
        cnt = sum(a["count"] for a in aborts)
        if cnt:
            out.append({
                "level": "warn",
                "title": "Suspend attempts aborted",
                "body": "%d suspend attempts were aborted, keeping the device "
                        "awake for %s. Most name %s."
                        % (cnt, human_duration(ms),
                           aborts[0]["reason"].split(":")[-1].strip()),
                "goto": "wakeups",
            })

    for kwl in report["kernel_wakelocks"][:8]:
        if kwl["count"] >= 300:
            out.append({
                "level": "info",
                "title": "High-frequency kernel wakelock",
                "body": "%s fired %d times in the window, for %s total."
                        % (kwl["name"], kwl["count"], human_duration(kwl["ms"])),
                "goto": "wakelocks",
            })
            break

    if s.get("time_on_battery_ms") and s.get("screen_off_ms"):
        off_share = 100.0 * s["screen_off_ms"] / s["time_on_battery_ms"]
        idle = s.get("light_idle_pct") or 0
        if off_share > 70 and idle < 40:
            out.append({
                "level": "warn",
                "title": "Screen was off but the device rarely idled",
                "body": "The screen was off for %.0f%% of the window, yet the "
                        "device only reached idle for %.0f%% of it."
                        % (off_share, idle),
                "goto": "timeline",
            })

    if report["culprits"]:
        top = report["culprits"][0]
        if top["total_mah"]:
            share = 100.0 * top["total_mah"] / (report["power"]["total_uid_mah"] or 1)
            out.append({
                "level": "info",
                "title": "Highest modelled draw",
                "body": "%s accounts for %.0f%% of attributed power (%.2f mAh), "
                        "almost all of it cpu time."
                        % (top["label"], share, top["total_mah"]),
                "goto": "culprits",
            })

    drain = s.get("actual_drain_mah")
    if drain is not None and drain == 0:
        out.append({
            "level": "info",
            "title": "No measured discharge in this window",
            "body": "The battery level did not move during the capture, so the "
                    "ranking comes from Android's power model rather than "
                    "measured drain. Reset stats and use the device for a few "
                    "hours for attributable numbers.",
            "goto": "overview",
        })

    dur = s.get("time_on_battery_ms") or 0
    if dur and dur < 30 * 60 * 1000:
        out.append({
            "level": "info",
            "title": "Short capture window",
            "body": "Only %s on battery. Background offenders usually need an "
                    "hour or more to separate from startup noise."
                    % human_duration(dur),
            "goto": "overview",
        })

    report["findings"] = out
