"""Perfetto capture and analysis for battviz.

Where live mode samples (`top` every few seconds) this records: Perfetto hooks
the kernel's own ftrace scheduler, so the result is every thread switch and
every wakeup with real timestamps, not a periodic guess. That is what makes a
"why" answer possible rather than a correlation.

Analysis is delegated to Perfetto's own `trace_processor_shell`, queried with
SQL. Reimplementing protobuf trace parsing here would be a large amount of
fragile code duplicating a tool Google already ships and maintains.

If trace_processor_shell is not on the host, capture still works and the
trace file is kept, with instructions for getting the binary.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time

DEVICE_TRACE_DIR = "/data/misc/perfetto-traces"
TRACE_PROCESSOR_URL = "https://get.perfetto.dev/trace_processor"

# Data sources chosen for battery attribution specifically: scheduling (who
# actually ran), cpu frequency and idle (what that cost), suspend/resume (what
# kept the device awake) and wakeup_source (what woke it). atrace categories
# add the framework-level context - alarms, jobs, binder callers.
TRACE_CONFIG = """
buffers {
  size_kb: 63488
  fill_policy: RING_BUFFER
}
data_sources {
  config {
    name: "linux.ftrace"
    ftrace_config {
      ftrace_events: "sched/sched_switch"
      ftrace_events: "sched/sched_wakeup"
      ftrace_events: "sched/sched_wakeup_new"
      ftrace_events: "sched/sched_process_exit"
      ftrace_events: "power/cpu_frequency"
      ftrace_events: "power/cpu_idle"
      ftrace_events: "power/suspend_resume"
      ftrace_events: "power/wakeup_source_activate"
      ftrace_events: "power/wakeup_source_deactivate"
      atrace_categories: "am"
      atrace_categories: "wm"
      atrace_categories: "binder_driver"
      atrace_categories: "dalvik"
      atrace_categories: "sched"
    }
  }
}
data_sources {
  config {
    name: "linux.process_stats"
    process_stats_config {
      scan_all_processes_on_start: true
    }
  }
}
data_sources {
  config {
    name: "android.power"
    android_power_config {
      battery_poll_ms: 1000
      battery_counters: BATTERY_COUNTER_CAPACITY_PERCENT
      battery_counters: BATTERY_COUNTER_CHARGE
      battery_counters: BATTERY_COUNTER_CURRENT
      collect_power_rails: true
    }
  }
}
duration_ms: %(duration_ms)d
"""

# Per-process scheduled cpu time. This is the sum of real scheduling slices
# from the kernel, in nanoseconds - a measurement, not a sampled percentage.
Q_CPU = """
select
  coalesce(p.name, 'pid ' || p.pid) as name,
  sum(s.dur) / 1e6 as cpu_ms,
  count(*) as slices
from sched s
join thread t on s.utid = t.utid
join process p on t.upid = p.upid
where s.dur > 0
group by p.upid
order by cpu_ms desc
limit 60;
"""

# How often each process's threads were woken. Attributes the wakeup to the
# woken process, which is what matters for "who is keeping this device busy".
Q_WAKEUPS = """
select
  coalesce(p.name, 'pid ' || p.pid) as name,
  count(*) as wakeups
from sched s
join thread t on s.utid = t.utid
join process p on t.upid = p.upid
where s.dur > 0
group by p.upid
order by wakeups desc
limit 60;
"""

# Kernel wakeup sources: what actually pulled the AP out of suspend.
Q_WAKEUP_SOURCES = """
select
  s.name as name,
  count(*) as count,
  sum(s.dur) / 1e6 as ms
from slice s
where s.name like '%wakeup%' or s.name like '%Wakeup%'
group by s.name
order by count desc
limit 40;
"""

# Per-process thread detail for the drill-down: which thread inside the app
# actually burned the time.
Q_THREADS = """
select
  coalesce(p.name, 'pid ' || p.pid) as process,
  coalesce(t.name, 'tid ' || t.tid) as thread,
  sum(s.dur) / 1e6 as cpu_ms,
  count(*) as slices
from sched s
join thread t on s.utid = t.utid
join process p on t.upid = p.upid
where s.dur > 0
group by t.utid
order by cpu_ms desc
limit 200;
"""

Q_TRACE_BOUNDS = "select min(ts) as start_ts, max(ts + dur) as end_ts from sched;"


class TraceError(Exception):
    pass


HERE = os.path.dirname(os.path.abspath(__file__))


def find_trace_processor(explicit=None):
    """Locate trace_processor_shell, or return None.

    Checked in order: an explicit path, the env var, this project's own
    folder (so dropping the binary next to battviz.py just works, with no
    PATH or env setup - the most common way people actually do this), PATH,
    then a couple of conventional home-directory locations.

    A binary found in this project's own folder gets its executable bit set
    automatically if it's missing, since that's a controlled location we
    already trust (unlike a PATH or home-directory match, which are never
    silently modified).
    """
    candidates = []
    if explicit:
        candidates.append((explicit, False))
    env = os.environ.get("BATTVIZ_TRACE_PROCESSOR")
    if env:
        candidates.append((env, False))
    for name in ("trace_processor_shell", "trace_processor"):
        candidates.append((os.path.join(HERE, name), True))
    for name in ("trace_processor_shell", "trace_processor"):
        found = shutil.which(name)
        if found:
            candidates.append((found, False))
    candidates.append((os.path.expanduser("~/trace_processor"), False))
    candidates.append((os.path.expanduser("~/.local/bin/trace_processor_shell"), False))

    for path, may_chmod in candidates:
        if not path or not os.path.isfile(path):
            continue
        if not os.access(path, os.X_OK):
            if not may_chmod:
                continue
            try:
                st = os.stat(path)
                os.chmod(path, st.st_mode | 0o111)
            except OSError:
                continue
        if os.access(path, os.X_OK):
            return path
    return None


def capture(shell, serial, duration_s=30, out_dir=None):
    """Record a trace on the device and pull it to the host.

    The config goes in over stdin rather than being written to the device, so
    nothing is left behind on the phone except the trace itself, which is
    removed after the pull.
    """
    duration_s = max(5, min(int(duration_s), 300))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    device_path = "%s/battviz-%s.perfetto-trace" % (DEVICE_TRACE_DIR, stamp)
    out_dir = out_dir or tempfile.gettempdir()
    local_path = os.path.join(out_dir, "battviz-%s.perfetto-trace" % stamp)

    config = TRACE_CONFIG % {"duration_ms": duration_s * 1000}

    base = ["adb"]
    if serial:
        base += ["-s", serial]

    # perfetto reads the config from stdin with `-c -`, so the config never
    # touches the device filesystem.
    cmd = base + ["shell", "perfetto", "-c", "-", "--txt", "-o", device_path]
    try:
        proc = subprocess.run(
            cmd, input=config.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=duration_s + 90)
    except FileNotFoundError:
        raise TraceError("adb not found on PATH.")
    except subprocess.TimeoutExpired:
        raise TraceError("perfetto did not finish within %ds." % (duration_s + 90))

    err = proc.stderr.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        hint = ""
        if "permission" in err.lower() or "denied" in err.lower():
            hint = (" Tracing may be disabled on this build. On Android 9-10 "
                    "try: adb shell setprop persist.traced.enable 1")
        raise TraceError("perfetto failed: %s%s" % (err[:400] or "unknown error", hint))

    pull = subprocess.run(base + ["pull", device_path, local_path],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=180)
    if pull.returncode != 0 or not os.path.isfile(local_path):
        raise TraceError("could not pull the trace: %s"
                         % pull.stderr.decode("utf-8", "replace").strip()[:300])

    subprocess.run(base + ["shell", "rm", "-f", device_path],
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)

    return {
        "path": local_path,
        "bytes": os.path.getsize(local_path),
        "duration_s": duration_s,
        "captured_at": time.time(),
        "stderr": err[:400],
    }


def _run_query(tp_path, trace_path, sql, timeout=180):
    """Run one SQL query through trace_processor_shell, return list of dicts."""
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as fh:
        fh.write(sql)
        query_file = fh.name
    try:
        proc = subprocess.run(
            [tp_path, "-q", query_file, trace_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TraceError("trace_processor timed out on a query.")
    finally:
        try:
            os.unlink(query_file)
        except OSError:
            pass

    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise TraceError("trace_processor failed: %s" % err[:300])

    return _parse_tp_output(proc.stdout.decode("utf-8", "replace"))


def _parse_tp_output(text):
    """Parse trace_processor's tabular stdout.

    Output format has varied between versions (pipe-delimited and
    whitespace-aligned both occur), so both are handled rather than assuming
    one and breaking on the other.
    """
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return []

    rows = []
    header = None
    for line in lines:
        stripped = line.strip()
        if set(stripped) <= set("-+| "):
            continue
        if "|" in stripped:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
        else:
            cells = stripped.split()
        if not cells:
            continue
        if header is None:
            header = cells
            continue
        if len(cells) < len(header):
            continue
        # Extra whitespace-split columns collapse into the first text field.
        if len(cells) > len(header):
            overflow = len(cells) - len(header) + 1
            cells = [" ".join(cells[:overflow])] + cells[overflow:]
        row = {}
        for key, value in zip(header, cells):
            row[key] = _coerce(value)
        rows.append(row)
    return rows


def _coerce(value):
    if value in ("[NULL]", "NULL", ""):
        return None
    try:
        if re.match(r"^-?\d+$", value):
            return int(value)
        if re.match(r"^-?\d*\.\d+([eE][-+]?\d+)?$", value):
            return float(value)
    except (ValueError, TypeError):
        pass
    return value


def analyse(trace_path, tp_path=None):
    """Turn a trace into a ranked culprit list backed by real scheduling data."""
    if not os.path.isfile(trace_path):
        raise TraceError("trace file not found: %s" % trace_path)
    tp = find_trace_processor(tp_path)
    if not tp:
        raise TraceError(
            "trace_processor_shell not found. The trace was captured and "
            "kept, but analysis needs Perfetto's own query engine. Get it "
            "with:\n  curl -LO %s && chmod +x trace_processor\n"
            "then put it on PATH or set BATTVIZ_TRACE_PROCESSOR to its path."
            % TRACE_PROCESSOR_URL)

    result = {"trace": trace_path, "trace_processor": tp,
              "bytes": os.path.getsize(trace_path), "queries": {}}

    def attempt(key, sql):
        try:
            rows = _run_query(tp, trace_path, sql)
            result["queries"][key] = "ok"
            return rows
        except TraceError as exc:
            # One unsupported table should not lose the whole analysis; some
            # data sources are absent on some builds.
            result["queries"][key] = str(exc)[:200]
            return []

    cpu_rows = attempt("cpu", Q_CPU)
    wake_rows = attempt("wakeups", Q_WAKEUPS)
    source_rows = attempt("wakeup_sources", Q_WAKEUP_SOURCES)
    thread_rows = attempt("threads", Q_THREADS)
    bounds = attempt("bounds", Q_TRACE_BOUNDS)

    span_ms = None
    if bounds and bounds[0].get("start_ts") is not None:
        try:
            span_ms = (bounds[0]["end_ts"] - bounds[0]["start_ts"]) / 1e6
        except (TypeError, KeyError):
            span_ms = None

    wake_by_name = {}
    for row in wake_rows:
        if row.get("name"):
            wake_by_name[row["name"]] = row.get("wakeups") or 0

    threads_by_process = {}
    for row in thread_rows:
        proc = row.get("process")
        if not proc:
            continue
        threads_by_process.setdefault(proc, []).append({
            "thread": row.get("thread"),
            "cpu_ms": round(row.get("cpu_ms") or 0.0, 2),
            "slices": row.get("slices"),
        })

    culprits = []
    for row in cpu_rows:
        name = row.get("name")
        if not name:
            continue
        cpu_ms = row.get("cpu_ms") or 0.0
        culprits.append({
            "name": name,
            "cpu_ms": round(cpu_ms, 2),
            "cpu_pct_of_trace": (round(100.0 * cpu_ms / span_ms, 2)
                                 if span_ms else None),
            "slices": row.get("slices"),
            "wakeups": wake_by_name.get(name, 0),
            "threads": threads_by_process.get(name, [])[:8],
            "is_package": bool(re.match(r"^[a-z][\w]*(\.[\w]+){2,}$", name)),
        })

    result["span_ms"] = round(span_ms, 1) if span_ms else None
    result["culprits"] = culprits
    result["wakeup_sources"] = [
        {"name": r.get("name"), "count": r.get("count"),
         "ms": round(r.get("ms") or 0.0, 2)}
        for r in source_rows if r.get("name")
    ]
    return result
