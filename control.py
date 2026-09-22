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
        "command": "am force-stop %s",
        "effect": "Kills the app's processes now. It restarts on next launch "
                  "or next scheduled job, so this is a temporary measure.",
        "undo": None,
        "undo_label": "restarts by itself",
        "destructive": False,
        "allow_system": True,
    },
    "restrict": {
        "label": "Restrict background",
        "command": "am set-standby-bucket %s restricted",
        "effect": "Puts the app in the restricted standby bucket. Background "
                  "jobs, alarms and network are heavily throttled. The app "
                  "still works in the foreground.",
        "undo": "am set-standby-bucket %s active",
        "undo_label": "Unrestrict",
        "destructive": False,
        "allow_system": True,
    },
    "unrestrict": {
        "label": "Unrestrict",
        "command": "am set-standby-bucket %s active",
        "effect": "Returns the app to the active standby bucket.",
        "undo": "am set-standby-bucket %s restricted",
        "undo_label": "Restrict again",
        "destructive": False,
        "allow_system": True,
    },
    "disable": {
        "label": "Disable",
        "command": "pm disable-user --user 0 %s",
        "effect": "Stops the app from running at all until re-enabled. It "
                  "stays installed and keeps its data. Notifications and "
                  "background sync stop.",
        "undo": "pm enable %s",
        "undo_label": "Enable",
        "destructive": True,
        "allow_system": False,
    },
    "enable": {
        "label": "Enable",
        "command": "pm enable %s",
        "effect": "Re-enables a previously disabled app.",
        "undo": "pm disable-user --user 0 %s",
        "undo_label": "Disable",
        "destructive": False,
        "allow_system": True,
    },
}


class PackageIndex(object):
    """Caches which packages are installed, system, and currently disabled."""

    def __init__(self, shell):
        self.shell = shell
        self.system = set()
        self.third_party = set()
        self.disabled = set()
        self.loaded = False

    def refresh(self):
        def names(flag):
            out = self.shell.run("pm list packages %s" % flag, timeout=30)
            found = set()
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


def plan(pkg, action, index=None, force=False):
    """Describe what an action would do, without running it.

    The UI calls this first so the person sees the exact command and its
    consequence before anything happens.
    """
    pkg = validate_package(pkg)
    if action not in ACTIONS:
        raise ControlError("unknown action: %r" % action)
    spec = ACTIONS[action]

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

    return {
        "package": pkg,
        "action": action,
        "label": spec["label"],
        "command": spec["command"] % pkg,
        "effect": spec["effect"],
        "undo_command": (spec["undo"] % pkg) if spec["undo"] else None,
        "undo_label": spec["undo_label"],
        "destructive": spec["destructive"],
        "blocked": blocked,
        "warning": warning,
        "info": info,
    }


def apply(shell, pkg, action, index=None, force=False):
    """Run an action after re-checking its plan. Returns the plan plus result."""
    p = plan(pkg, action, index=index, force=force)
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
