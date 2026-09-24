"""Package control actions for battviz.

This is the one module that changes state on the device rather than reading
from it, so it is deliberately conservative:

- Package names are validated against a strict pattern before ever reaching a
  shell. Everything else is a fixed literal, so no user-supplied text is
  interpolated into a command unchecked.
- Every action declares the exact command it will run, and the UI shows that
  command before anything executes. Nothing here runs implicitly.
- Disabling is refused for system packages and for a denylist of components
  that would brick basic device function, unless explicitly forced.
- Every action is reversible, and each one carries the command that undoes it.
"""

import re
import time

import parser as _parser

# Android package names: at least one dot, no shell metacharacters. Anything
# that does not match this is rejected outright rather than escaped, because
# there is no legitimate package name that needs escaping.
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")

# Disabling any of these leaves the device unusable or unrecoverable without
# a factory reset, so they are refused even with force.
NEVER_DISABLE = {
    "android",
    "com.android.systemui",
    "com.android.settings",
    "com.android.shell",
    "com.android.phone",
    "com.android.providers.settings",
    "com.android.packageinstaller",
    "com.google.android.packageinstaller",
}

# Disabling these is legal and sometimes intended, but breaks enough that the
# UI should warn first.
WARN_DISABLE = {
    "com.google.android.gms": "Play services. Disabling breaks push "
                              "notifications, location and many apps.",
    "com.google.android.gsf": "Google services framework. Breaks push.",
    "com.android.vending": "Play Store. App updates and installs stop.",
    "com.android.bluetooth": "Bluetooth stops working.",
    "com.android.nfc": "NFC and tap to pay stop working.",
}


class ControlError(Exception):
    pass


def validate_package(pkg):
    if not pkg or not isinstance(pkg, str):
        raise ControlError("no package given")
    pkg = pkg.strip()
    if len(pkg) > 255:
        raise ControlError("package name too long")
    if not PACKAGE_RE.match(pkg):
        raise ControlError(
            "not a valid android package name: %r. Expected something like "
            "com.example.app." % pkg[:80])
    return pkg


# Each action: the command template, what it does, how to undo it, and whether
# it is destructive enough to need a confirmation step in the UI.
ACTIONS = {
    "force-stop": {
        "label": "Force stop",
        "command": "am force-stop --user %(user)s %(pkg)s",
        "effect": "Kills the app's processes now. It restarts on next launch "
                  "or next scheduled job, so this is a temporary measure.",
        "undo": None,
        "undo_label": "restarts by itself",
        "destructive": False,
        "allow_system": True,
    },
    "restrict": {
        "label": "Restrict background",
        "command": "am set-standby-bucket --user %(user)s %(pkg)s restricted",
        "effect": "Puts the app in the restricted standby bucket. Background "
                  "jobs, alarms and network are heavily throttled. The app "
                  "still works in the foreground.",
        "undo": "am set-standby-bucket --user %(user)s %(pkg)s active",
        "undo_label": "Unrestrict",
        "destructive": False,
        "allow_system": True,
    },
    "unrestrict": {
        "label": "Unrestrict",
        "command": "am set-standby-bucket --user %(user)s %(pkg)s active",
        "effect": "Returns the app to the active standby bucket.",
        "undo": "am set-standby-bucket --user %(user)s %(pkg)s restricted",
        "undo_label": "Restrict again",
        "destructive": False,
        "allow_system": True,
    },
    "disable": {
        "label": "Disable",
        "command": "pm disable-user --user %(user)s %(pkg)s",
        "effect": "Stops the app from running at all until re-enabled. It "
                  "stays installed and keeps its data. Notifications and "
                  "background sync stop.",
        "undo": "pm enable --user %(user)s %(pkg)s",
        "undo_label": "Enable",
        "destructive": True,
        "allow_system": False,
    },
    "enable": {
        "label": "Enable",
        "command": "pm enable --user %(user)s %(pkg)s",
        "effect": "Re-enables a previously disabled app.",
        "undo": "pm disable-user --user %(user)s %(pkg)s",
        "undo_label": "Disable",
        "destructive": False,
        "allow_system": True,
    },
}


def _int_or_zero(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# `dumpsys netstats detail` reports cumulative per-UID byte counters straight
# from the kernel's own network accounting. This is a genuinely different
# signal from cpu time: an app can be almost invisible to a cpu ranking while
# steadily sending data (sync, telemetry, ads), which is exactly the case a
# cpu-only ranking misses.
_UID_LINE = re.compile(r"\buid=(\d+)")
_RB = re.compile(r"\brb=(\d+)")
_TB = re.compile(r"\btb=(\d+)")


def network_by_uid(shell):
    """Sum rx/tx bytes per uid from `dumpsys netstats detail`.

    The output nests per-uid blocks of history buckets; each bucket carries
    rb= (received bytes) and tb= (transmitted bytes). Counters are summed
    within whichever uid block they appear under. Output shape varies between
    Android versions, so anything unparseable is skipped rather than guessed
    at, and a uid with no buckets simply reports zero.
    """
    out = shell.run("dumpsys netstats detail", timeout=60)
    totals = {}
    current = None
    for line in out.splitlines():
        m = _UID_LINE.search(line)
        if m:
            current = m.group(1)
            totals.setdefault(current, {"rx": 0, "tx": 0})
            # A uid= line can also carry counters on the same line.
        if current is None:
            continue
        rb = _RB.search(line)
        tb = _TB.search(line)
        if rb or tb:
            entry = totals.setdefault(current, {"rx": 0, "tx": 0})
            if rb:
                entry["rx"] += _int_or_zero(rb.group(1))
            if tb:
                entry["tx"] += _int_or_zero(tb.group(1))
    for uid, entry in totals.items():
        entry["total"] = entry["rx"] + entry["tx"]
    return totals


_PKG_UID_LINE = re.compile(r"^package:(\S+?)\s+uid:(\d+)\s*$")
_USER_LINE = re.compile(r"UserInfo\{(\d+):([^:}]*):")


def list_users(shell):
    """Every user profile on the device, from `pm list users`.

    A device with a work profile, a second space, or an OEM clone-app
    feature (observed: OPlus's app-twin) has more than one profile, and each
    encodes its own set of app uids - `pm list packages -U` with no --user
    flag only ever sees the primary profile (0), which is exactly why a
    uid from any other profile came back unresolved before this existed.
    """
    out = shell.run("pm list users", timeout=20)
    users = []
    for line in out.splitlines():
        m = _USER_LINE.search(line)
        if m:
            users.append({"id": m.group(1), "name": m.group(2).strip()})
    return users or [{"id": "0", "name": "primary"}]


def profile_of_uid(uid):
    """Android encodes uid = profile*100000 + appId. Works out which profile
    a uid belongs to from the number alone, regardless of whether pm ever
    resolved a package name for it - so a row can always show which profile
    it is in, even when the name itself stays unresolved."""
    try:
        n = int(uid)
    except (TypeError, ValueError):
        return None
    return n // 100000 if n >= 100000 else 0


def packages_by_uid(shell):
    """Map uid -> [package names], across every user profile on the device.

    Several packages can share a uid (sharedUserId), so this is a list, not a
    single name, and the UI shows all of them rather than picking one.
    """
    mapping = {}
    for user in list_users(shell):
        out = shell.run("pm list packages -U --user %s" % user["id"], timeout=45)
        for line in out.splitlines():
            m = _PKG_UID_LINE.match(line.strip())
            if not m:
                continue
            pkg, uid = m.group(1), m.group(2)
            mapping.setdefault(uid, []).append(pkg)
    return mapping


def _join_uid_totals_to_packages(totals, by_uid):
    """Shared join used by both the cumulative and windowed-diff paths, so
    the shared-uid handling and unattributed-uid fallback stay identical
    between them rather than drifting if implemented twice.

    `resolved` is set explicitly here rather than left for the frontend to
    guess from whether the label happens to look like a dotted package name -
    that guess previously flagged legitimate AID labels ("kernel / root",
    "gps / location") as "unresolved" purely because they contain spaces,
    when they are in fact accurately resolved, just not to a package name.
    Only the literal "uid <n>" fallback is genuinely unresolved.
    """
    rows = []
    for uid, entry in totals.items():
        pkgs = by_uid.get(uid) or []
        resolved = bool(pkgs)
        if not pkgs:
            label = SYSTEM_UID_LABELS.get(uid)
            if label:
                pkgs = [label]
                resolved = True
            else:
                pkgs = ["uid " + uid]
                resolved = False
        for pkg in pkgs:
            row = dict(entry)
            row["package"] = pkg
            row["uid"] = uid
            row["profile"] = profile_of_uid(uid)
            row["shared_uid"] = len(by_uid.get(uid) or []) > 1
            row["resolved"] = resolved
            rows.append(row)
    rows.sort(key=lambda r: -r["total"])
    return rows


def network_by_package(shell):
    """All-time cumulative totals - whatever history netstats has retained,
    with no relationship to any particular capture window. Kept distinct
    from the windowed diff below rather than conflated with it."""
    totals = network_by_uid(shell)
    by_uid = packages_by_uid(shell)
    return _join_uid_totals_to_packages(totals, by_uid)


def force_netstats_poll(shell):
    """Ask NetworkStatsService to flush its counters to the history buckets.

    Without this, a short-window diff reads the same unflushed buckets twice
    and correctly subtracts to zero, which looks exactly like "no traffic"
    even while an app is visibly downloading. Android only persists netstats
    on its own poll interval (tens of minutes), so the snapshots must trigger
    a poll themselves. Flag names differ across versions, so several forms
    are tried; all are harmless no-ops where unsupported.
    """
    for cmd in ("dumpsys netstats --poll", "dumpsys netstats poll"):
        out = shell.run(cmd, timeout=45) or ""
        low = out.lower()
        if "unknown" not in low and "usage" not in low and "exception" not in low:
            return True
    return False


def network_snapshot(shell, poll=True):
    """One point-in-time read, timestamped, for differencing against a later
    snapshot. The timestamp is taken immediately after the read completes,
    since that is when the counters it captured were actually current."""
    if poll:
        force_netstats_poll(shell)
    by_uid = network_by_uid(shell)
    return {"t": time.time(), "by_uid": by_uid}


def network_diff(before, after):
    """Per-uid bytes transferred strictly between two snapshots.

    netstats counters are cumulative and monotonic under normal operation.
    A uid whose value went down between snapshots - a stats reset, or the
    app being killed/cleared mid-window - would otherwise produce a negative
    number, which is nonsensical as "bytes used". It is clamped to 0 and
    flagged, rather than shown as a negative or silently hidden.
    """
    out = {}
    for uid in set(before["by_uid"]) | set(after["by_uid"]):
        b = before["by_uid"].get(uid, {"rx": 0, "tx": 0, "total": 0})
        a = after["by_uid"].get(uid, {"rx": 0, "tx": 0, "total": 0})
        rx, tx = a["rx"] - b["rx"], a["tx"] - b["tx"]
        reset = rx < 0 or tx < 0
        rx, tx = max(0, rx), max(0, tx)
        out[uid] = {"rx": rx, "tx": tx, "total": rx + tx, "reset_detected": reset}
    window_s = max(0.001, after["t"] - before["t"])
    # A window where literally nothing moved is far more often netstats not
    # having flushed than a genuinely idle device, so it is reported as an
    # explicit condition rather than as a confident set of zeros.
    total_bytes = sum(v["total"] for v in out.values())
    return {"by_uid": out, "window_s": round(window_s, 3),
            "started_at": before["t"], "ended_at": after["t"],
            "total_bytes": total_bytes, "empty": total_bytes == 0}


def network_diff_by_package(shell, diff):
    by_uid = packages_by_uid(shell)
    rows = _join_uid_totals_to_packages(diff["by_uid"], by_uid)
    for r in rows:
        r["window_s"] = diff["window_s"]
        r["bytes_per_s"] = round(r["total"] / diff["window_s"], 1) if diff["window_s"] else 0
    return rows


SYSTEM_UID_LABELS = _parser.SYSTEM_UIDS  # single shared, AOSP-verified table


# Install location is a real, queryable signal about how privileged a package
# is, though a noisy one: /system/priv-app/ holds genuinely privileged
# components, but plenty of OEM preinstalls live there too. It is reported
# as-is rather than folded into any score.
def package_signals(shell, pkg):
    pkg = validate_package(pkg)
    path_out = shell.run("pm path %s" % pkg, timeout=20)
    paths = [l.split(":", 1)[1].strip()
             for l in path_out.splitlines() if l.strip().startswith("package:")]
    apk = paths[0] if paths else ""
    if "/priv-app/" in apk:
        location = "priv-app"
    elif apk.startswith("/system/") or apk.startswith("/product/") \
            or apk.startswith("/vendor/"):
        location = "system"
    elif apk.startswith("/data/"):
        location = "user"
    else:
        location = "unknown"

    info = shell.run("dumpsys package %s | grep -E 'flags=|pkgFlags=|versionName='"
                     % pkg, timeout=30)
    flags = []
    for token in ("SYSTEM", "PERSISTENT", "DEBUGGABLE", "HAS_CODE",
                  "UPDATED_SYSTEM_APP", "STOPPED"):
        if token in info:
            flags.append(token)
    version = None
    m = re.search(r"versionName=(\S+)", info)
    if m:
        version = m.group(1)

    return {
        "package": pkg,
        "apk_path": apk,
        "location": location,
        "flags": flags,
        "version": version,
        "protected": pkg in NEVER_DISABLE,
        "warn": WARN_DISABLE.get(pkg),
    }


class PackageIndex(object):
    """Caches which packages are installed, system, and currently disabled,
    across every user profile on the device - a package that only exists
    under a work profile or a second space would otherwise read as "not
    installed" simply because the primary profile was the only one checked,
    which would incorrectly block a valid action against it."""

    def __init__(self, shell):
        self.shell = shell
        self.system = set()
        self.third_party = set()
        self.disabled = set()
        self.loaded = False

    def refresh(self):
        def names(flag):
            found = set()
            for user in list_users(self.shell):
                out = self.shell.run("pm list packages %s --user %s"
                                     % (flag, user["id"]), timeout=30)
                for line in out.splitlines():
                    line = line.strip()
                    if line.startswith("package:"):
                        name = line.split(":", 1)[1].strip()
                        if name:
                            found.add(name)
            return found

        self.system = names("-s")
        self.third_party = names("-3")
        self.disabled = names("-d")
        self.loaded = True
        return self

    def info(self, pkg):
        if not self.loaded:
            self.refresh()
        installed = pkg in self.system or pkg in self.third_party
        return {
            "package": pkg,
            "installed": installed,
            "system": pkg in self.system,
            "third_party": pkg in self.third_party,
            "disabled": pkg in self.disabled,
        }


def plan(pkg, action, index=None, force=False, user="0"):
    """Describe what an action would do, without running it.

    The UI calls this first so the person sees the exact command and its
    consequence before anything happens. `user` is the Android user-profile
    id the package lives under - a device with a work profile, a second
    space, or an OEM clone-app feature has more than one, and the wrong one
    means the command silently targets a package that is not actually there.
    """
    pkg = validate_package(pkg)
    if action not in ACTIONS:
        raise ControlError("unknown action: %r" % action)
    spec = ACTIONS[action]
    user = str(user) if user is not None else "0"
    if not re.match(r"^\d+$", user):
        raise ControlError("not a valid user id: %r" % user)

    info = index.info(pkg) if index else {"package": pkg, "installed": None,
                                          "system": None, "third_party": None,
                                          "disabled": None}
    blocked = None
    warning = None

    if action == "disable":
        if pkg in NEVER_DISABLE:
            blocked = ("%s is required for the device to function. battviz "
                       "will not disable it." % pkg)
        elif info.get("system") and not spec["allow_system"] and not force:
            blocked = ("%s is a system package. Disabling it usually needs "
                       "root and can break the ROM, so it is blocked by "
                       "default." % pkg)
        elif pkg in WARN_DISABLE:
            warning = WARN_DISABLE[pkg]

    if info.get("installed") is False:
        blocked = "%s is not installed on this device." % pkg

    fmt = {"pkg": pkg, "user": user}
    return {
        "package": pkg,
        "user": user,
        "action": action,
        "label": spec["label"],
        "command": spec["command"] % fmt,
        "effect": spec["effect"],
        "undo_command": (spec["undo"] % fmt) if spec["undo"] else None,
        "undo_label": spec["undo_label"],
        "destructive": spec["destructive"],
        "blocked": blocked,
        "warning": warning,
        "info": info,
    }


def apply(shell, pkg, action, index=None, force=False, user="0"):
    """Run an action after re-checking its plan. Returns the plan plus result."""
    p = plan(pkg, action, index=index, force=force, user=user)
    if p["blocked"]:
        raise ControlError(p["blocked"])

    out = shell.run(p["command"], timeout=45)
    lowered = (out or "").lower()
    # pm and am report failure in stdout rather than via an exit code, since
    # the persistent shell does not surface exit codes per command.
    failed = ("exception" in lowered or "error" in lowered
              or "failure" in lowered or "unknown package" in lowered
              or "permission deni" in lowered)
    p["output"] = (out or "").strip()[:500]
    p["ok"] = not failed
    if index is not None and p["ok"]:
        index.refresh()
        p["info"] = index.info(pkg)
    return p
