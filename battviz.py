#!/usr/bin/env python3
"""battviz - a local viewer for Android `dumpsys batterystats` dumps.

    python3 battviz.py batterystats.txt
    python3 battviz.py --adb            # pull a fresh dump from a device
    python3 battviz.py --port 9000 dump.txt

Serves a single-page viewer on 127.0.0.1 and opens it in a browser. Nothing
leaves the machine and nothing outside the standard library is required.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import webbrowser
import socketserver
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

try:
    # Python 3.7+
    from http.server import ThreadingHTTPServer
except ImportError:
    # Older Python 3: build the same thing by hand.
    class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon_threads = True

import parser as bsparser
import live as livemod

HERE = os.path.dirname(os.path.abspath(__file__))
# Accept either layout: a proper static/ subfolder, or the static files
# dropped flat next to battviz.py (common when files are saved individually
# rather than as a folder).
_STATIC_SUBDIR = os.path.join(HERE, "static")
STATIC = _STATIC_SUBDIR if os.path.isfile(os.path.join(_STATIC_SUBDIR, "index.html")) else HERE

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

# Guarded by _state_lock because ThreadingHTTPServer handles uploads and reads
# concurrently.
_state = {"report": None, "source": None, "raw": ""}
_state_lock = threading.Lock()

# The live session is global and single: one device, one poll loop. Starting a
# second would double the device-side cost for no extra information.
_live = {"session": None}
_live_lock = threading.Lock()

MAX_UPLOAD = 64 * 1024 * 1024


def load_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def pull_via_adb():
    print("running: adb shell dumpsys batterystats", file=sys.stderr)
    try:
        out = subprocess.run(
            ["adb", "shell", "dumpsys", "batterystats"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
        )
    except FileNotFoundError:
        sys.exit("adb not found on PATH. Install platform-tools or pass a file "
                 "path instead.")
    except subprocess.TimeoutExpired:
        sys.exit("adb timed out after 120s. Is the device authorised?")
    if out.returncode != 0:
        sys.exit("adb failed: " + out.stderr.decode("utf-8", "replace").strip())
    text = out.stdout.decode("utf-8", "replace")
    if len(text) < 500:
        sys.exit("adb returned almost nothing. Check `adb devices`.")
    return text


def set_report(text, source):
    report = bsparser.parse(text)
    report["meta"] = {
        "source": source,
        "bytes": len(text.encode("utf-8", "replace")),
        "lines": text.count("\n") + 1,
    }
    with _state_lock:
        _state["report"] = report
        _state["source"] = source
        _state["raw"] = text
    return report


class Handler(BaseHTTPRequestHandler):
    server_version = "battviz"

    def log_message(self, fmt, *args):
        if os.environ.get("BATTVIZ_VERBOSE"):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # ------------------------------------------------------------ helpers --

    def _send(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload, allow_nan=False),
                   "application/json; charset=utf-8")

    def _static(self, relpath):
        # Resolve then confirm the path stays inside STATIC, so a crafted
        # request cannot walk out of the static directory.
        full = os.path.realpath(os.path.join(STATIC, relpath.lstrip("/")))
        if not full.startswith(os.path.realpath(STATIC) + os.sep):
            return self._send(403, "forbidden")
        if not os.path.isfile(full):
            return self._send(404, "not found")
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as fh:
            self._send(200, fh.read(), MIME.get(ext, "application/octet-stream"))

    # ---------------------------------------------------------- endpoints --

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path == "/api/report":
            with _state_lock:
                report = _state["report"]
            if report is None:
                return self._json(503, {"error": "no dump loaded"})
            return self._json(200, report)
        if path == "/api/raw":
            with _state_lock:
                raw = _state["raw"]
            return self._send(200, raw, "text/plain; charset=utf-8")
        if path == "/api/live/devices":
            try:
                return self._json(200, {"devices": livemod.list_devices()})
            except livemod.AdbError as exc:
                return self._json(502, {"error": str(exc)})
        if path == "/api/live/state":
            with _live_lock:
                session = _live["session"]
            if session is None:
                return self._json(200, {"running": False})
            try:
                since = int(parse_qs(urlparse(self.path).query).get("since", ["0"])[0])
            except ValueError:
                since = 0
            return self._json(200, session.snapshot(since))
        if path == "/api/adb":
            try:
                text = pull_via_adb()
            except SystemExit as exc:
                return self._json(502, {"error": str(exc)})
            report = set_report(text, "adb: connected device")
            return self._json(200, {"ok": True, "source": report["meta"]["source"]})
        return self._static(path)

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 1024 * 64:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except ValueError:
            return {}

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/live/start":
            body = self._read_json_body()
            with _live_lock:
                if _live["session"] is not None and _live["session"].running:
                    return self._json(409, {"error": "a live session is already running"})
                session = livemod.LiveSession(
                    serial=body.get("serial") or None,
                    unplug=bool(body.get("unplug", True)),
                    watch_logcat=bool(body.get("logcat", True)))
                try:
                    session.start()
                except livemod.AdbError as exc:
                    return self._json(502, {"error": str(exc)})
                except Exception as exc:  # noqa: BLE001
                    return self._json(500, {"error": "could not start session: %s" % exc})
                _live["session"] = session
            return self._json(200, {"ok": True, "device": session.device_info})

        if path == "/api/live/stop":
            with _live_lock:
                session = _live["session"]
                _live["session"] = None
            if session is None:
                return self._json(200, {"ok": True})
            session.stop()
            return self._json(200, {"ok": True})

        if path == "/api/live/focus":
            body = self._read_json_body()
            with _live_lock:
                session = _live["session"]
            if session is None:
                return self._json(409, {"error": "no live session"})
            return self._json(200, session.focus(body.get("package")))

        if path == "/api/live/deep-sync":
            with _live_lock:
                session = _live["session"]
            if session is None:
                return self._json(409, {"error": "no live session"})
            text = session.deep_sync()
            if not text:
                return self._json(502, {"error": "deep sync returned nothing"})
            report = set_report(text, "live deep sync")
            return self._json(200, {"ok": True, "sections": len(report["sections"])})

        if path != "/api/upload":
            return self._send(404, "not found")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "bad content-length"})
        if length <= 0:
            return self._json(400, {"error": "empty upload"})
        if length > MAX_UPLOAD:
            return self._json(413, {"error": "file larger than 64 MB"})
        raw = self.rfile.read(length).decode("utf-8", "replace")
        name = self.headers.get("X-Filename") or "uploaded dump"
        try:
            report = set_report(raw, os.path.basename(name))
        except Exception as exc:  # noqa: BLE001 - surface any parse failure
            return self._json(422, {"error": "could not parse: %s" % exc})
        if not report["sections"]:
            return self._json(422, {
                "error": "no batterystats sections found. Is this the output "
                         "of `adb shell dumpsys batterystats`?"})
        return self._json(200, {"ok": True, "source": report["meta"]["source"]})


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="battviz",
        description="Visualise battery drain from an Android batterystats dump.")
    ap.add_argument("dump", nargs="?", help="path to a batterystats text dump")
    ap.add_argument("--adb", action="store_true",
                    help="pull a dump from a connected device over adb")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.isfile(os.path.join(STATIC, "index.html")):
        sys.exit(
            "cannot find index.html.\n"
            "  looked in: %s\n"
            "  battviz.py is in: %s\n"
            "  fix: make sure index.html, style.css and app.js sit either in "
            "a static/ subfolder next to battviz.py, or directly next to it."
            % (STATIC, HERE)
        )

    if args.adb:
        set_report(pull_via_adb(), "adb: connected device")
    elif args.dump:
        if not os.path.isfile(args.dump):
            sys.exit("no such file: %s" % args.dump)
        set_report(load_text(args.dump), os.path.basename(args.dump))
    else:
        print("No dump given. Starting empty; drop a file onto the page.",
              file=sys.stderr)

    with _state_lock:
        report = _state["report"]
    if report:
        print("parsed %s: %d sections, %d uids, %d history events"
              % (report["meta"]["source"], len(report["sections"]),
                 len(report["uids"]), len(report["history"]["events"])),
              file=sys.stderr)

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        sys.exit("cannot bind %s:%d (%s). Try --port." % (args.host, args.port, exc))

    url = "http://%s:%d/" % (args.host, args.port)
    print("battviz serving on %s  (ctrl-c to stop)" % url, file=sys.stderr)
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        with _live_lock:
            session = _live["session"]
        if session is not None:
            # Restores `dumpsys battery reset` so the device is not left
            # believing it is unplugged.
            session.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
