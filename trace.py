"""Perfetto capture and analysis for battviz.

Where live mode samples (`top` every few seconds) this records: Perfetto hooks
the kernel's own ftrace scheduler, so the result is every thread switch and
every wakeup with real timestamps, not a periodic guess. That is what makes a
"why" answer possible rather than a correlation.

Analysis parses the trace protobuf directly in Python, using the schema
bundled with the `perfetto` pip package (`pip install perfetto`), rather than
shelling out to Google's trace_processor_shell binary and parsing its text
table output. Two things forced this change after the first version shipped:

  1. trace_processor_shell has to be downloaded separately, from a URL that
     is unreachable from this analysis environment, and its table output
     format has genuinely varied across versions - the first version of this
     module guessed at that format against a hand-written mock, and the
     guess was wrong against a real captured trace, silently returning zero
     rows with no error.
  2. Once a real trace was available to test against, direct parsing turned
     out to be fast (under 2 seconds for a 35MB / 30s capture) and removes
     the external-binary dependency entirely, so there is no longer a
     reason to carry the fragile path at all.

Two ftrace-visible sources of cpu time are not real culprits and are
excluded from the ranking the same way `live.py` excludes `top` measuring
itself: the per-core idle thread (`swapper/N`, tid 0), and `traced`/
`traced_probes`, which are only busy because they are the ones recording
the trace.
"""

import collections
import os
import re
import subprocess
import tempfile
import time

DEVICE_TRACE_DIR = "/data/misc/perfetto-traces"

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
      ftrace_events: "sched/sched_waking"
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
%(network_packets_block)sdata_sources {
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

# android.network_packets was only added to the on-device perfetto binary's
# own schema in Android 14. The on-device pbtxt parser rejects a config
# containing ANY field it does not recognise as a hard error for the WHOLE
# config, not just the unrecognised part - so on Android 12/13 this single
# block silently broke capture entirely (cpu, wakeups, everything), not just
# network data. Included only when the device is confirmed new enough.
NETWORK_PACKETS_BLOCK = """data_sources {
  config {
    name: "android.network_packets"
    network_packet_trace_config {
      poll_ms: 250
    }
  }
}
"""

# ftrace-visible processes that are not real culprits. Two kinds: the
# per-core idle thread (matched by prefix, since each core has its own
# swapper/N), and a fixed set of names that are the analyser's own activity
# rather than the device's - Perfetto's own capture daemons, and (because a
# trace capture runs with a live session's polling loop still active on the
# same device) the shell commands that loop uses. Matched on the resolved
# basename, since process names can arrive as a bare comm ("top") or a full
# path ("/system/bin/traced_probes") depending on whether a process_tree
# snapshot resolved them.
SELF_NOISE_NAMES = {
    "traced_probes", "traced",
    "top", "grep", "head", "cat", "sh", "toybox", "dumpsys", "echo",
    "logcat", "adb", "adbd",
}

PACKAGE_RE = re.compile(r"^[a-z][\w]*(\.[\w]+){2,}$")


class TraceError(Exception):
    pass


def _import_proto():
    try:
        from perfetto.protos.perfetto.trace.perfetto_trace_pb2 import Trace
        return Trace
    except ImportError:
        raise TraceError(
            "the perfetto package is not installed. Run:\n"
            "  pip install perfetto --break-system-packages\n"
            "This is pure Python plus protobuf, a few hundred KB - not the "
            "large trace_processor_shell binary, and nothing to chmod or "
            "put on PATH.")


def find_trace_processor(explicit=None):
    """Retained for the /api/trace/state status line: whether analysis is
    available at all. There is no external binary any more."""
    try:
        _import_proto()
        return "perfetto (python)"
    except TraceError:
        return None


def _supports_network_packets(shell):
    """SDK 34 (Android 14) is when network_packet_trace_config was added to
    the on-device schema. Checked by SDK int rather than the release string,
    since that's what the platform itself guarantees monotonically increases.
    Any failure to read it is treated as "no" - the failure mode of wrongly
    omitting network data is far better than wrongly including a field that
    breaks the entire capture on an older device.
    """
    try:
        out = shell.run("getprop ro.build.version.sdk", timeout=10).strip()
        return int(out) >= 34
    except (ValueError, AttributeError):
        return False


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

    network_block = NETWORK_PACKETS_BLOCK if _supports_network_packets(shell) else ""
    config = TRACE_CONFIG % {"duration_ms": duration_s * 1000,
                             "network_packets_block": network_block}

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
        elif "no field named" in err.lower():
            hint = (" The on-device perfetto binary rejected a config field it "
                    "does not recognise - this usually means a data source "
                    "added in a newer Android version than this device runs. "
                    "If this recurs, it likely needs the same version-gating "
                    "network_packets already has in this file.")
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


def _is_noise(name):
    # swapper/N is never a filesystem path - it's a bare kernel comm string
    # that happens to contain a slash, so it must be checked before any
    # path-style basename split, or the split wrongly reduces it to just "N".
    if name.startswith("swapper"):
        return True
    base = name.rsplit("/", 1)[-1]
    return base in SELF_NOISE_NAMES


def _parse_battery_and_rails(trace):
    """Battery counters and power rail energy, straight from the trace.

    Both message types are native fields on the installed schema - no
    compilation or extension tricks needed, unlike network_packets. Verified
    against a real capture: this device reported 31 battery samples with
    genuine capacity/charge/current readings, and one (empty) power_rails
    packet, confirming the parse path works even though this particular
    device's rails go unpopulated (no PowerStats HAL - common outside Pixel
    devices).
    """
    battery_series = []
    first_ts = None
    for pkt in trace.packet:
        if pkt.WhichOneof("data") != "battery":
            continue
        if first_ts is None:
            first_ts = pkt.timestamp
        bc = pkt.battery
        battery_series.append({
            "t_ms": round((pkt.timestamp - first_ts) / 1e6, 1),
            "capacity_pct": bc.capacity_percent if bc.HasField("capacity_percent") else None,
            "current_ua": bc.current_ua if bc.HasField("current_ua") else None,
            "charge_uah": bc.charge_counter_uah if bc.HasField("charge_counter_uah") else None,
            "voltage_uv": bc.voltage_uv if bc.HasField("voltage_uv") else None,
        })

    # A device with no meaningful discharge signal in this reading is common
    # enough (short captures, or a battery gauge that only updates coarsely)
    # that it is flagged explicitly rather than shown as a plausible chart.
    # A real device's active discharge current is normally in the tens to
    # hundreds of milliamps; this device's own readings during live testing
    # elsewhere in this session were consistently in that range, so a max
    # magnitude far below it here means the field is not carrying a useful
    # signal on this device/build, not that the device was unusually idle.
    currents = [abs(b["current_ua"]) for b in battery_series if b["current_ua"] is not None]
    current_reliable = bool(currents) and max(currents) >= 5000  # 5 mA floor

    rail_descriptors = {}
    rail_samples = collections.defaultdict(list)
    for pkt in trace.packet:
        if pkt.WhichOneof("data") != "power_rails":
            continue
        pr = pkt.power_rails
        for rd in pr.rail_descriptor:
            rail_descriptors[rd.index] = {
                "name": rd.rail_name, "subsys": rd.subsys_name,
                "sampling_rate": rd.sampling_rate,
            }
        for ed in pr.energy_data:
            rail_samples[ed.index].append({"t_ms": ed.timestamp_ms, "energy_uj": ed.energy})

    power_rails = []
    for index, samples in rail_samples.items():
        samples.sort(key=lambda s: s["t_ms"])
        desc = rail_descriptors.get(index, {"name": "rail %d" % index, "subsys": ""})
        energy_delta_uj = (samples[-1]["energy_uj"] - samples[0]["energy_uj"]) if len(samples) > 1 else 0
        duration_s = (samples[-1]["t_ms"] - samples[0]["t_ms"]) / 1000.0 if len(samples) > 1 else 0
        power_rails.append({
            "name": desc["name"], "subsys": desc["subsys"],
            "samples": len(samples),
            "energy_delta_uj": energy_delta_uj,
            "avg_power_mw": round(energy_delta_uj / 1000.0 / duration_s, 2) if duration_s > 0 else None,
        })
    power_rails.sort(key=lambda r: -(r["energy_delta_uj"] or 0))

    return {
        "battery_series": battery_series,
        "current_reliable": current_reliable,
        "power_rails": power_rails,
        "power_rails_available": len(power_rails) > 0,
    }


def analyse(trace_path, tp_path=None):
    """Rank processes by real scheduled cpu time, parsed directly from the
    trace protobuf.

    tp_path is accepted and ignored; kept so callers built against the
    previous trace_processor_shell-based signature do not need to change.
    """
    if not os.path.isfile(trace_path):
        raise TraceError("trace file not found: %s" % trace_path)

    Trace = _import_proto()
    trace = Trace()
    with open(trace_path, "rb") as fh:
        trace.ParseFromString(fh.read())

    power = _parse_battery_and_rails(trace)

    # Process tree snapshots can appear more than once as new processes
    # start; later ones simply extend/override earlier ones.
    tid_to_pid = {}
    pid_to_name = {}
    pid_to_uid = {}
    for pkt in trace.packet:
        if pkt.WhichOneof("data") != "process_tree":
            continue
        for p in pkt.process_tree.processes:
            if p.cmdline:
                pid_to_name[p.pid] = p.cmdline[0]
            if p.HasField("uid"):
                pid_to_uid[p.pid] = p.uid
        for th in pkt.process_tree.threads:
            tid_to_pid[th.tid] = th.tgid

    per_cpu = collections.defaultdict(list)
    wakeups_by_tid = collections.Counter()
    min_ts, max_ts = None, None

    for pkt in trace.packet:
        if pkt.WhichOneof("data") != "ftrace_events":
            continue
        cpu = pkt.ftrace_events.cpu
        for ev in pkt.ftrace_events.event:
            which = ev.WhichOneof("event")
            if which == "sched_switch":
                ts = ev.timestamp
                per_cpu[cpu].append((ts, ev.sched_switch.next_pid,
                                     ev.sched_switch.next_comm))
                min_ts = ts if min_ts is None else min(min_ts, ts)
                max_ts = ts if max_ts is None else max(max_ts, ts)
            elif which in ("sched_wakeup", "sched_waking"):
                pid = (ev.sched_wakeup.pid if which == "sched_wakeup"
                      else ev.sched_waking.pid)
                wakeups_by_tid[pid] += 1

    # Integrate: the task named by one switch's next_pid runs from that
    # switch's timestamp until the next switch on the same cpu core. Keyed by
    # (tid, comm) rather than tid alone: tid 0 is the Linux idle task and is
    # shared across every core, so each core's swapper/N is a distinct
    # logical entity that happens to reuse the same tid - keying by tid alone
    # silently sums all cores' idle time into one bucket, labelled with
    # whichever core's name was resolved last.
    tid_ns = collections.Counter()
    for events in per_cpu.values():
        events.sort(key=lambda e: e[0])
        for i in range(len(events) - 1):
            ts, next_pid, next_comm = events[i]
            tid_ns[(next_pid, next_comm)] += events[i + 1][0] - ts

    def resolve(tid, comm):
        pid = tid_to_pid.get(tid, tid)
        return pid_to_name.get(pid, comm or ("tid %d" % tid))

    def resolve_uid(tid):
        pid = tid_to_pid.get(tid, tid)
        return pid_to_uid.get(pid)

    proc_ns = collections.Counter()
    proc_wakeups = collections.Counter()
    proc_threads = collections.defaultdict(list)
    proc_uid = {}
    for (tid, comm), ns in tid_ns.items():
        name = resolve(tid, comm)
        if _is_noise(name):
            continue
        proc_ns[name] += ns
        proc_threads[name].append({"thread": comm or ("tid %d" % tid),
                                   "cpu_ms": round(ns / 1e6, 2)})
        if name not in proc_uid:
            u = resolve_uid(tid)
            if u is not None:
                proc_uid[name] = u
    for tid, count in wakeups_by_tid.items():
        name = resolve(tid, None)
        if not _is_noise(name):
            proc_wakeups[name] += count

    span_ms = ((max_ts - min_ts) / 1e6) if (min_ts is not None) else None

    culprits = []
    for name, ns in proc_ns.most_common(60):
        cpu_ms = ns / 1e6
        threads = sorted(proc_threads[name], key=lambda t: -t["cpu_ms"])[:8]
        culprits.append({
            "name": name,
            # The one identifier both this cpu ranking and control.py's
            # network ranking can resolve independently and correctly, even
            # when either side's own package-name resolution disagrees or
            # fails (pm list packages -U output has already been seen to
            # vary by vendor). The UI joins network data on this first,
            # falling back to name matching only when it is unavailable.
            "uid": str(proc_uid[name]) if name in proc_uid else None,
            "cpu_ms": round(cpu_ms, 2),
            "cpu_pct_of_trace": (round(100.0 * cpu_ms / span_ms, 2)
                                 if span_ms else None),
            "slices": None,
            "wakeups": proc_wakeups.get(name, 0),
            "threads": threads,
            "is_package": bool(PACKAGE_RE.match(name)),
        })

    return {
        "trace": trace_path,
        "bytes": os.path.getsize(trace_path),
        "span_ms": round(span_ms, 1) if span_ms else None,
        "packets": len(trace.packet),
        "culprits": culprits,
        "wakeup_sources": [],
        "battery_series": power["battery_series"],
        "current_reliable": power["current_reliable"],
        "power_rails": power["power_rails"],
        "power_rails_available": power["power_rails_available"],
        "queries": {"parsed": "ok"},
    }
