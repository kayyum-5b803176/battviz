"""Live adb session for battviz.

The analyser must not become the thing it is measuring. Four decisions follow
from that, and they shape this whole module:

1. One persistent `adb shell` process, not one per sample. Spawning a shell
   per poll costs a USB round trip plus a fork on the device, which dwarfs the
   cost of the data actually being read.
2. Adaptive cadence. Polling a screen-off device keeps it out of doze, so the
   interval stretches from 2s (screen on) to 30s (dozing). Measuring harder
   would change the result.
3. Cheap sources only. `dumpsys batterystats` is ~200KB and expensive, so it is
   never polled - only read on an explicit deep sync. Per-tick data comes from
   `dumpsys battery`, one sysfs current reading, a grepped wakelock section and
   a row-capped `top`.
4. Filtering happens on the device. grep and head run inside the shell command
   so less data crosses the wire.

The session measures its own cost and reports it, so the overhead is visible
rather than assumed.
"""

import atexit
import os
import re
import subprocess
import threading
import time
from collections import deque, OrderedDict

SENTINEL = "__BATTVIZ_EOC__"
# Section markers must not contain SENTINEL, or the command reader stops at the
# first section boundary instead of the end of the response.
MARKER = "__BATTVIZ_SEC__"

# Adaptive poll intervals, seconds, keyed by device wakefulness.
INTERVAL_SCREEN_ON = 2.0
INTERVAL_SCREEN_OFF = 10.0
INTERVAL_DOZE = 30.0

# Ring buffer sizes. Bounded so a long session cannot grow without limit.
MAX_SAMPLES = 1800
MAX_EVENTS = 600

# Candidate sysfs paths for instantaneous current. Probed once at startup;
# vendors disagree about which of these exists and about the sign convention.
CURRENT_PATHS = [
    "/sys/class/power_supply/battery/current_now",
    "/sys/class/power_supply/bms/current_now",
    "/sys/class/power_supply/Battery/current_now",
]

LOG_PATTERNS = [
    (re.compile(r"\bANR in ([\w\.]+)"), "anr", "ANR"),
    (re.compile(r"\bFATAL EXCEPTION\b"), "crash", "Fatal exception"),
    (re.compile(r"\bForce finishing activity ([\w\.\/]+)"), "crash", "Force finished"),
    (re.compile(r"Slow operation|Long monitor contention|Skipped \d+ frames"), "jank", "Slow operation"),
    (re.compile(r"\bWakeLock\b.*\bacquire", re.I), "wakelock", "Wakelock acquired"),
    (re.compile(r"\bdoze\b|\bDeviceIdleController\b", re.I), "doze", "Doze state"),
    (re.compile(r"\bJobScheduler\b.*\b(start|run)", re.I), "job", "Job started"),
    (re.compile(r"\balarm\b.*\btrigger", re.I), "alarm", "Alarm fired"),
]

# The polling command's own pieces (top, grep, the persistent shell itself)
# run inside the same process tree being observed, so they can appear in
# their own top sample - a genuine observer-effect artifact, not a real
# device culprit. Excluded by name wherever process rows are parsed.
SELF_NOISE = {"top", "grep", "head", "cat", "sh", "toybox", "dumpsys",
             "echo", "logcat", "adb", "adbd"}


def _now():
    return time.monotonic()


class AdbError(Exception):
    pass


def adb_base(serial=None):
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    return cmd


def list_devices():
    """Return [{serial, state, model}] for attached devices."""
    try:
        out = subprocess.run(["adb", "devices", "-l"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=15)
    except FileNotFoundError:
        raise AdbError("adb not found on PATH. Install android platform-tools.")
    except subprocess.TimeoutExpired:
        raise AdbError("adb timed out listing devices.")
    if out.returncode != 0:
        raise AdbError(out.stderr.decode("utf-8", "replace").strip() or "adb failed")

    devices = []
    for line in out.stdout.decode("utf-8", "replace").splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        serial, state = parts[0], parts[1]
        model = ""
        for p in parts[2:]:
            if p.startswith("model:"):
                model = p.split(":", 1)[1].replace("_", " ")
        devices.append({"serial": serial, "state": state, "model": model})
    return devices


class PersistentShell(object):
    """A single long-lived `adb shell`, driven by sentinel-delimited commands.

    Reusing one shell avoids a process spawn on the device for every sample,
    which is the single largest contributor to the tool's own power cost.
    """

    def __init__(self, serial=None):
        self.serial = serial
        self.lock = threading.Lock()
        self.proc = None
        self.queue = deque()
        self.cv = threading.Condition()
        self.reader = None
        self.alive = False
        # Self-cost accounting.
        self.command_count = 0
        self.shell_seconds = 0.0
        self.start_wall = time.time()

    def open(self):
        cmd = adb_base(self.serial) + ["shell"]
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=1, universal_newlines=True)
        except FileNotFoundError:
            raise AdbError("adb not found on PATH.")
        self.alive = True
        self.reader = threading.Thread(target=self._pump, name="battviz-shell-reader")
        self.reader.daemon = True
        self.reader.start()
        # Confirm the shell answers before the session claims to be connected.
        probe = self.run("echo ok", timeout=15)
        if "ok" not in probe:
            raise AdbError("adb shell did not respond. Is the device authorised?")

    def _pump(self):
        try:
            for line in self.proc.stdout:
                with self.cv:
                    self.queue.append(line.rstrip("\n"))
                    self.cv.notify_all()
        except Exception:
            pass
        finally:
            with self.cv:
                self.alive = False
                self.cv.notify_all()

    def run(self, command, timeout=20.0):
        """Run one command, return its stdout. Empty string on failure."""
        if not self.proc or self.proc.poll() is not None:
            return ""
        started = _now()
        with self.lock:
            with self.cv:
                self.queue.clear()
            try:
                self.proc.stdin.write(command + "\necho " + SENTINEL + "\n")
                self.proc.stdin.flush()
            except (IOError, ValueError):
                self.alive = False
                return ""

            lines = []
            deadline = _now() + timeout
            with self.cv:
                while True:
                    while self.queue:
                        line = self.queue.popleft()
                        if line.strip() == SENTINEL:
                            elapsed = _now() - started
                            self.command_count += 1
                            self.shell_seconds += elapsed
                            return "\n".join(lines)
                        lines.append(line)
                    remaining = deadline - _now()
                    if remaining <= 0 or not self.alive:
                        elapsed = _now() - started
                        self.command_count += 1
                        self.shell_seconds += elapsed
                        return "\n".join(lines)
                    self.cv.wait(min(remaining, 0.5))

    def close(self):
        self.alive = False
        if self.proc:
            try:
                self.proc.stdin.write("exit\n")
                self.proc.stdin.flush()
            except Exception:
                pass
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None

    def overhead(self):
        wall = max(1.0, time.time() - self.start_wall)
        return {
            "commands": self.command_count,
            "shell_seconds": round(self.shell_seconds, 2),
            "commands_per_min": round(self.command_count / (wall / 60.0), 1),
            "shell_ms_per_min": round((self.shell_seconds * 1000.0) / (wall / 60.0)),
            "duty_pct": round(100.0 * self.shell_seconds / wall, 2),
        }


class LogcatTail(object):
    """Tails logcat in a subprocess and keeps only lines worth surfacing."""

    def __init__(self, serial, sink):
        self.serial = serial
        self.sink = sink
        self.proc = None
        self.thread = None
        self.running = False
        self.pid_filter = None
        self.dropped = 0

    def start(self):
        # -T 1 starts at "now" so the wrapped historical buffer is not replayed.
        # Priority filter keeps debug spam on the device instead of the wire.
        cmd = adb_base(self.serial) + [
            "logcat", "-v", "brief", "-T", "1",
            "*:W", "ActivityManager:I", "PowerManagerService:I",
            "JobScheduler:I", "AlarmManager:I", "DeviceIdleController:I",
        ]
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=1, universal_newlines=True)
        except FileNotFoundError:
            return
        self.running = True
        self.thread = threading.Thread(target=self._read, name="battviz-logcat")
        self.thread.daemon = True
        self.thread.start()

    def _read(self):
        try:
            for line in self.proc.stdout:
                if not self.running:
                    break
                line = line.rstrip("\n")
                if not line or line.startswith("---------"):
                    continue
                self._classify(line)
        except Exception:
            pass

    def _classify(self, line):
        for pattern, kind, label in LOG_PATTERNS:
            m = pattern.search(line)
            if not m:
                continue
            package = None
            if m.groups():
                package = m.group(1)
            if self.pid_filter and self.pid_filter not in line:
                return
            self.sink({
                "source": "logcat",
                "kind": kind,
                "label": label,
                "package": package,
                "text": line[-220:],
            })
            return

    def stop(self):
        self.running = False
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None


class LiveSession(object):
    def __init__(self, serial=None, unplug=True, watch_logcat=True):
        self.serial = serial
        self.unplug = unplug
        self.watch_logcat = watch_logcat

        self.shell = PersistentShell(serial)
        self.logcat = None
        self.thread = None
        self.running = False
        self.error = None
        self.started_at = None

        self.lock = threading.Lock()
        self.samples = deque(maxlen=MAX_SAMPLES)
        self.events = deque(maxlen=MAX_EVENTS)
        self.seq = 0

        self.current_path = None
        self.current_divisor = 1000.0  # µA to mA
        self.device_info = {}
        self.screen_on = True
        self.dozing = False
        self.interval = INTERVAL_SCREEN_ON
        self.focus_package = None
        self.focus_pid = None
        self.deep_sync_text = None
        self.unplug_applied = False
        self.last_top = {}
        self._overhead_warned = False

        # Net accumulator: a single -n1 top sample is noisy, so an app doing
        # real work can vanish for several ticks purely by bad luck. Ranking
        # by accumulated cpu-time instead of the latest instantaneous reading
        # is what makes a real background culprit visible and stable rather
        # than flickering in and out with every poll.
        self.proc_accum = {}
        self.accum_start_t = None
        self._accum_last_t = None
        self.ACCUM_MAX_TRACKED = 500
        self.ACCUM_PRUNE_TO = 350

    # ------------------------------------------------------------ startup --

    def start(self):
        self.shell.open()
        self._probe_device()
        if self.unplug:
            self._apply_unplug()
        if self.watch_logcat:
            self.logcat = LogcatTail(self.serial, self._push_event)
            self.logcat.start()

        self.running = True
        self.started_at = time.time()
        self.accum_start_t = self.started_at
        atexit.register(self.stop)
        self.thread = threading.Thread(target=self._loop, name="battviz-live")
        self.thread.daemon = True
        self.thread.start()
        self._push_event({"source": "session", "kind": "info",
                          "label": "Session started", "text":
                          "polling %s" % (self.device_info.get("model") or self.serial or "device")})

    def _probe_device(self):
        info = self.shell.run(
            "getprop ro.product.model; getprop ro.build.version.release; "
            "getprop ro.product.manufacturer")
        parts = [p.strip() for p in info.splitlines() if p.strip()]
        self.device_info = {
            "model": parts[0] if len(parts) > 0 else "",
            "android": parts[1] if len(parts) > 1 else "",
            "vendor": parts[2] if len(parts) > 2 else "",
            "serial": self.serial or "",
        }
        # Find a readable instantaneous-current file once, rather than per tick.
        for path in CURRENT_PATHS:
            out = self.shell.run("cat %s 2>/dev/null" % path).strip()
            if out and re.match(r"^-?\d+$", out.splitlines()[0].strip()):
                self.current_path = path
                value = abs(int(out.splitlines()[0].strip()))
                # Some vendors report mA directly rather than µA.
                self.current_divisor = 1000.0 if value > 10000 else 1.0
                break

    def _apply_unplug(self):
        """Make the framework believe the device is on battery while wired.

        `dumpsys battery unplug` alone leaves the status field untouched on
        some OEM battery services (observed: status stayed "charging" and
        drain rate never moved on a Realme/ColorOS device), so every relevant
        field is forced explicitly rather than relying on the convenience
        command by itself.
        """
        self.shell.run(
            "dumpsys battery unplug; "
            "dumpsys battery set ac 0; "
            "dumpsys battery set usb 0; "
            "dumpsys battery set wireless 0; "
            "dumpsys battery set status 3")
        self.unplug_applied = True
        self._push_event({
            "source": "session", "kind": "info", "label": "Charging masked",
            "text": "battery forced to discharging so drain is measurable while wired"})

    def _restore_plug(self):
        if self.unplug_applied:
            self.shell.run("dumpsys battery reset")
            self.unplug_applied = False

    # --------------------------------------------------------------- loop --

    # Cap what fraction of the interval a tick's shell round trip is allowed
    # to consume. On real USB hardware a tick can take far longer than it did
    # against the mock, and without this the loop settles into back-to-back
    # polling regardless of the nominal interval - the 0.5s floor previously
    # used here measured 38% duty cycle on real hardware, an order of
    # magnitude above the mock's ~0.3%.
    MAX_DUTY_FRACTION = 0.12
    tick_ema = 0.0

    def _loop(self):
        while self.running:
            started = _now()
            try:
                self._tick()
            except Exception as exc:  # keep the session alive on a bad sample
                self.error = str(exc)
            elapsed = _now() - started
            self.tick_ema = elapsed if not self.tick_ema else (0.3 * elapsed + 0.7 * self.tick_ema)
            # Enough headroom that a tick costing `elapsed` seconds keeps duty
            # cycle at or below MAX_DUTY_FRACTION even if every tick is this slow.
            floor_for_duty = self.tick_ema * ((1.0 / self.MAX_DUTY_FRACTION) - 1.0)
            sleep_for = max(0.5, self.interval - elapsed, floor_for_duty)
            end = _now() + sleep_for
            while self.running and _now() < end:
                time.sleep(min(0.25, end - _now()))

    def _tick(self):
        # One shell round trip carries every cheap reading for this sample.
        cmd = "dumpsys battery"
        if self.current_path:
            cmd += "; echo %s; cat %s 2>/dev/null" % (MARKER + "CUR", self.current_path)
        cmd += "; echo %s; dumpsys power | grep -E 'mWakefulness=|Display Power|mHoldingDisplay' | head -4" % (MARKER + "PWR")
        cmd += "; echo %s; dumpsys power | grep -E 'PARTIAL_WAKE_LOCK' | head -12" % (MARKER + "WL")
        # No on-device row cap. A capped slice is exactly what caused the two
        # previous bugs here (tail truncation, then a smaller head still
        # missing a high-pid app) - whatever count a real device turns out to
        # have, cost stays trivial next to a full batterystats pull (measured
        # ~4KB text for 200 processes), so there is no reason to risk it.
        # grep only strips the header/summary noise; nothing here truncates by
        # position.
        cmd += ("; echo %s; top -b -n 1 -o PID,%%CPU,ARGS 2>/dev/null | "
                "grep -E '^[[:space:]]*[0-9]+[[:space:]]'") % (MARKER + "TOP")
        raw = self.shell.run(cmd, timeout=25)

        blocks = self._split_blocks(raw)
        battery = self._parse_battery(blocks.get("head", ""))
        current_ma = self._parse_current(blocks.get("CUR", ""))
        self._parse_power(blocks.get("PWR", ""))
        wakelocks = self._parse_wakelocks(blocks.get("WL", ""))
        procs = self._parse_top(blocks.get("TOP", ""))
        self._accumulate(procs)

        self._adapt_interval()
        self._check_overhead()

        sample = {
            "t": round(time.time(), 2),
            "elapsed": round(time.time() - (self.started_at or time.time()), 1),
            "level": battery.get("level"),
            "temp_c": battery.get("temp_c"),
            "voltage_mv": battery.get("voltage_mv"),
            "status": battery.get("status"),
            "plugged": battery.get("plugged"),
            "current_ma": current_ma,
            "screen_on": self.screen_on,
            "dozing": self.dozing,
            "wakelocks": wakelocks,
            # Only the instantaneous top few travel with the sample history;
            # ranking now comes from the accumulator, and top is uncapped
            # on-device (see the TOP command above), so storing every row in
            # every one of up to MAX_SAMPLES history entries would grow
            # without bound over a long session.
            "procs": procs[:20],
            "interval": self.interval,
        }

        with self.lock:
            self.seq += 1
            sample["seq"] = self.seq
            self.samples.append(sample)

        self._detect_changes(sample, procs, wakelocks)

    def _split_blocks(self, raw):
        """Split one multiplexed shell response on its echoed markers."""
        blocks = {}
        key = "head"
        buf = []
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith(MARKER):
                blocks[key] = "\n".join(buf)
                key = stripped[len(MARKER):]
                buf = []
                continue
            buf.append(line)
        blocks[key] = "\n".join(buf)
        return blocks

    # ------------------------------------------------------------ parsers --

    def _parse_battery(self, text):
        out = {}
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("level:"):
                out["level"] = _int(line)
            elif line.startswith("temperature:"):
                v = _int(line)
                out["temp_c"] = round(v / 10.0, 1) if v is not None else None
            elif line.startswith("voltage:"):
                out["voltage_mv"] = _int(line)
            elif line.startswith("status:"):
                out["status"] = _status_name(_int(line))
            elif line.startswith("USB powered:") or line.startswith("AC powered:"):
                if "true" in line.lower():
                    out["plugged"] = True
                out.setdefault("plugged", False)
        return out

    def _parse_current(self, text):
        line = text.strip().splitlines()
        if not line:
            return None
        try:
            raw = int(line[0].strip())
        except ValueError:
            return None
        ma = abs(raw) / self.current_divisor
        # Positive means discharge here regardless of vendor sign convention.
        return round(ma, 1)

    def _parse_power(self, text):
        low = text.lower()
        if "mwakefulness=awake" in low.replace(" ", ""):
            self.screen_on = True
        elif "mwakefulness=asleep" in low.replace(" ", "") or "mwakefulness=dozing" in low.replace(" ", ""):
            self.screen_on = False
        self.dozing = "dozing" in low

    def _parse_wakelocks(self, text):
        out = []
        for line in text.splitlines():
            line = line.strip()
            if "PARTIAL_WAKE_LOCK" not in line:
                continue
            tag = None
            m = re.search(r"'([^']+)'", line)
            if m:
                tag = m.group(1)
            pkg = None
            m = re.search(r"\(uid=(\d+)[^)]*\)", line)
            uid = m.group(1) if m else None
            m = re.search(r"\b([a-z][\w]*(?:\.[\w]+){2,})\b", line)
            if m:
                pkg = m.group(1)
            out.append({"tag": tag or line[:60], "uid": uid, "package": pkg})
        return out

    def _parse_top(self, text):
        procs = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("PID"):
                continue
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            pid, cpu, args = parts
            if not pid.isdigit():
                continue
            try:
                cpu_val = float(cpu.rstrip("%"))
            except ValueError:
                continue
            name = args.strip().split()[0] if args.strip() else ""
            base = name.rsplit("/", 1)[-1]
            if base.lower() in SELF_NOISE:
                continue
            procs.append({"pid": pid, "cpu": round(cpu_val, 1), "name": name})
        procs.sort(key=lambda p: -p["cpu"])
        return procs

    # ----------------------------------------------------------- cadence --

    def _adapt_interval(self):
        """Slow down when the device is idle.

        Polling a screen-off device holds it out of doze, which would make the
        measurement cause the drain it is looking for.
        """
        if self.dozing:
            self.interval = INTERVAL_DOZE
        elif self.screen_on:
            self.interval = INTERVAL_SCREEN_ON
        else:
            self.interval = INTERVAL_SCREEN_OFF

    def _check_overhead(self):
        if self._overhead_warned or len(self.samples) < 8:
            return
        duty = self.shell.overhead()["duty_pct"]
        if duty > 20.0:
            self._overhead_warned = True
            self._push_event({
                "source": "session", "kind": "info", "label": "High overhead",
                "text": "duty cycle is %.0f%%, this connection is slow enough "
                        "that battviz itself is a meaningful load. Consider "
                        "wireless adb, which is typically faster." % duty})

    def _accumulate(self, procs):
        """Integrate each process's cpu% over wall time into a running total.

        A process at 40% cpu held for a 2s tick contributes 0.8 cpu-seconds.
        Summed across the session this survives the noise of any single
        sample and reflects actual exposure rather than a snapshot.
        """
        now = _now()
        delta = (now - self._accum_last_t) if self._accum_last_t else self.interval
        delta = max(0.0, min(delta, self.interval * 3))  # clamp a stalled tick
        self._accum_last_t = now
        wall = time.time()

        with self.lock:
            for p in procs:
                name = p["name"] or p["pid"]
                cpu = p["cpu"]
                acc = self.proc_accum.get(name)
                if acc is None:
                    acc = {"cpu_seconds": 0.0, "samples": 0, "peak_cpu": 0.0,
                          "last_cpu": 0.0, "first_t": wall, "last_t": wall}
                    self.proc_accum[name] = acc
                acc["cpu_seconds"] += (cpu / 100.0) * delta
                acc["samples"] += 1
                acc["peak_cpu"] = max(acc["peak_cpu"], cpu)
                acc["last_cpu"] = cpu
                acc["last_t"] = wall

            if len(self.proc_accum) > self.ACCUM_MAX_TRACKED:
                keep = sorted(self.proc_accum.items(), key=lambda kv: -kv[1]["cpu_seconds"])
                self.proc_accum = dict(keep[:self.ACCUM_PRUNE_TO])

    def reset_accumulator(self):
        """Zero the net culprit ranking without dropping the adb connection.

        Distinct from stopping the session: this clears only the accumulated
        cpu-time ranking, for isolating a specific window (after installing
        an update, say) without paying to reconnect.
        """
        with self.lock:
            self.proc_accum = {}
        self._accum_last_t = None
        self.accum_start_t = time.time()
        self._push_event({"source": "session", "kind": "info",
                          "label": "Culprit ranking reset",
                          "text": "accumulator cleared, connection kept open"})

    # ------------------------------------------------------------ events --

    def _push_event(self, event):
        with self.lock:
            self.seq += 1
            event = dict(event)
            event["seq"] = self.seq
            event["t"] = round(time.time(), 2)
            self.events.append(event)

    def _detect_changes(self, sample, procs, wakelocks):
        prev = self.last_top
        now = {}
        for p in procs:
            now[p["name"]] = p["cpu"]
            old = prev.get(p["name"], 0.0)
            if p["cpu"] >= 15 and p["cpu"] - old >= 10:
                self._push_event({
                    "source": "top", "kind": "cpu", "label": "Cpu spike",
                    "package": p["name"], "pid": p["pid"],
                    "text": "%s rose to %.0f%% cpu" % (p["name"], p["cpu"])})
        self.last_top = now

        if len(self.samples) >= 2:
            before = self.samples[-2]
            if before.get("level") is not None and sample.get("level") is not None:
                if sample["level"] < before["level"]:
                    self._push_event({
                        "source": "battery", "kind": "level",
                        "label": "Level dropped",
                        "text": "%d%% to %d%%" % (before["level"], sample["level"])})
            if before.get("screen_on") != sample.get("screen_on"):
                self._push_event({
                    "source": "power", "kind": "screen",
                    "label": "Screen " + ("on" if sample["screen_on"] else "off"),
                    "text": "wakefulness changed"})

    # ------------------------------------------------------------- public --

    def focus(self, package):
        """Restrict logcat to one package by resolving its pid on the device."""
        self.focus_package = package or None
        self.focus_pid = None
        if package:
            out = self.shell.run("pidof %s" % package).strip()
            pid = out.split()[0] if out.split() else None
            self.focus_pid = pid
        if self.logcat:
            self.logcat.pid_filter = ("(%s)" % self.focus_pid) if self.focus_pid else None
        return {"package": self.focus_package, "pid": self.focus_pid}

    def deep_sync(self):
        """Pull a full batterystats on demand. Never part of the poll loop."""
        text = self.shell.run("dumpsys batterystats", timeout=120)
        self.deep_sync_text = text
        self._push_event({"source": "session", "kind": "info",
                          "label": "Deep sync", "text":
                          "pulled %d KB of batterystats" % (len(text) // 1024)})
        return text

    # A level drop is only 1% of resolution, so extrapolating one step over a
    # short window produces nonsense (one drop after 20s reads as 180 %/hr).
    # Require both a minimum window and a second drop before reporting.
    MIN_DRAIN_WINDOW_S = 300.0
    MIN_DRAIN_STEPS = 2

    def drain_rate(self):
        """Percent per hour from observed level drops.

        Returns None until there is enough evidence to be meaningful; the UI
        shows "measuring" rather than a confidently wrong number.
        """
        with self.lock:
            samples = [s for s in self.samples if s.get("level") is not None]
        if len(samples) < 2:
            return None
        first, last = samples[0], samples[-1]
        drop = first["level"] - last["level"]
        seconds = last["t"] - first["t"]
        if drop < self.MIN_DRAIN_STEPS or seconds < self.MIN_DRAIN_WINDOW_S:
            return None
        return round(drop / (seconds / 3600.0), 2)

    def snapshot(self, since=0):
        with self.lock:
            samples = [s for s in self.samples if s["seq"] > since]
            events = [e for e in self.events if e["seq"] > since]
            latest = self.samples[-1] if self.samples else None
            seq = self.seq
            trend = [
                {"t": s["elapsed"], "current_ma": s["current_ma"],
                 "level": s["level"], "screen_on": s["screen_on"]}
                for s in list(self.samples)[-120:]
            ]
            accum_items = sorted(self.proc_accum.items(),
                                 key=lambda kv: -kv[1]["cpu_seconds"])

        total_cpu_s = sum(v["cpu_seconds"] for _, v in accum_items) or 1.0
        now_wall = time.time()
        culprits = []
        for name, v in accum_items:
            culprits.append({
                "name": name,
                "cpu_seconds": round(v["cpu_seconds"], 2),
                "share_pct": round(100.0 * v["cpu_seconds"] / total_cpu_s, 1),
                "last_cpu": v["last_cpu"],
                "peak_cpu": v["peak_cpu"],
                "samples": v["samples"],
                "stale": (now_wall - v["last_t"]) > (self.interval * 3),
            })

        return {
            "running": self.running,
            "error": self.error,
            "seq": seq,
            "device": self.device_info,
            "started_at": self.started_at,
            "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "accum_seconds": round(time.time() - self.accum_start_t, 1) if self.accum_start_t else 0,
            "interval": self.interval,
            "screen_on": self.screen_on,
            "dozing": self.dozing,
            "unplug_applied": self.unplug_applied,
            "current_source": self.current_path,
            "focus": {"package": self.focus_package, "pid": self.focus_pid},
            "latest": latest,
            "trend": trend,
            "samples": samples[-60:],
            "events": events[-120:],
            "culprits": culprits,
            "drain_pct_hr": self.drain_rate(),
            "overhead": self.shell.overhead(),
            "has_deep_sync": self.deep_sync_text is not None,
        }

    def stop(self):
        if not self.running and not self.unplug_applied:
            return
        self.running = False
        if self.logcat:
            self.logcat.stop()
        try:
            self._restore_plug()
        except Exception:
            pass
        self.shell.close()


def _int(line):
    m = re.search(r"(-?\d+)", line.split(":", 1)[-1])
    return int(m.group(1)) if m else None


def _status_name(code):
    return {1: "unknown", 2: "charging", 3: "discharging",
            4: "not charging", 5: "full"}.get(code, "unknown")
