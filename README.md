# battviz

A local web viewer for Android `dumpsys batterystats` dumps. Finds what kept
the device awake and what drew power, and shows it on one timeline.

Runs on Linux with nothing but Python 3.8+. No pip install, no virtualenv, no
network access at any point — the dump never leaves the machine.

## Run it

```sh
python3 battviz.py batterystats.txt
```

It parses the dump, starts a server on `http://127.0.0.1:8787` and opens a
browser. Stop it with ctrl-c.

```sh
python3 battviz.py --adb              # pull a fresh dump from a connected device
python3 battviz.py --port 9000 d.txt  # different port
python3 battviz.py --no-browser d.txt # don't open a browser
python3 battviz.py                    # start empty, drag a file onto the page
```

You can also drop a different dump onto the page at any time to swap it in.

## Getting a useful dump

A dump only contains what has happened since stats were last reset, so a
capture taken right after a reboot shows startup noise rather than a culprit.

```sh
adb shell dumpsys batterystats --reset
# unplug, use the phone normally for a few hours
adb shell dumpsys batterystats > batterystats.txt
```

Unplug before you start. If the battery level never moves during the window,
Android reports zero measured discharge and every ranking becomes modelled
rather than measured — battviz flags this on the overview when it happens.

## Live mode

`Live` in the sidebar watches a connected device while it drains, instead of
reading a dump after the fact. Pick a device, start the session, and it polls
in the background while showing current draw, the processes burning cpu right
now, wakelocks currently held, and a merged event stream from logcat.

Select any process to filter logcat to that app's pid. `Deep sync` pulls a
full batterystats on demand and loads it into the normal static views, so a
live session can end as a saved report.

### Keeping the analyser out of the results

Watching a device costs the device something. Four things keep that cost low
enough not to distort the measurement:

- **One persistent shell.** A single long-lived `adb shell` handles every
  sample. Spawning `adb shell` per poll would cost a USB round trip plus a
  fork on the device each time, which dwarfs the data being read.
- **Adaptive cadence.** Polling a screen-off device holds it out of doze, so
  the interval stretches from 2s with the screen on, to 10s screen off, to 30s
  while dozing. That is 1800 commands an hour down to 120.
- **Cheap sources only.** `dumpsys batterystats` is around 200KB and expensive,
  so it is never polled, only read on an explicit deep sync. Each tick reads
  `dumpsys battery`, one sysfs current file, a grepped wakelock section and a
  row-capped `top`, all in one round trip. Filtering runs on the device so
  less crosses the wire.
- **The overhead is shown, not assumed.** The session counts its own shell
  commands and measures the time the device shell spent busy, and reports both
  as a duty-cycle percentage. If the tool is costing more than it is finding,
  that is visible on screen.

### Charging makes drain unmeasurable

A wired device is charging, so there is no discharge to observe and every rate
reads as zero. Two ways around it:

- **Wireless adb**, the honest option, since the device is genuinely unplugged:
  `adb tcpip 5555` then `adb connect <phone-ip>:5555`, then unplug the cable.
- **Mask charging**, on by default for wired sessions. The session runs
  `dumpsys battery unplug` so the framework behaves as though on battery.
  Stopping the session runs `dumpsys battery reset` to undo it, and that also
  runs on ctrl-c and on process exit. If a session is ever killed hard, run
  `adb shell dumpsys battery reset` yourself to restore normal charging.

Drain rate stays at "measuring" until there is a real window to measure: at
least two percent of drop across at least five minutes. One percent over
twenty seconds extrapolates to a confident and meaningless number.

## The views

**Overview** — time on battery, screen-on share, idle share and drain, then the
whole modelled power budget as one strip, the top culprits, and any findings
worth attention.

**Culprits** — one row per uid, ranked by a score that blends modelled power
(50%), wakelock hold time (25%), wakelock acquisition count (15%) and
screen-off cpu (10%). Estimated mAh alone under-rates an app that wakes a
sleeping device often but briefly, which is the usual cause of overnight
drain. Select a row for its processes, wakelocks, sensors, services and jobs.

**Timeline** — every battery-history event on one clock, one lane per
subsystem. Drag to pan, scroll to zoom, hover a segment to read it. Red ticks
are wake reasons.

**Wakelocks** — app wakelocks are chargeable to a uid; kernel wakelocks are held
by drivers and usually point at hardware or firmware. A high acquire count
with a low total is wakelock thrashing, which costs more in wakeups than the
hold time suggests.

**Wakeups** — what pulled the processor out of suspend. Entries starting
`Abort:` are suspend attempts that were rejected, so the device stayed awake.

**Daily drain** — each discharge step is one percent of battery, so its duration
converts to a percent-per-hour rate, split by whether the screen was on.
Screen-off rate is the number that matters for background drain.

**Raw dump** — the original text, searchable, for the OEM sections battviz does
not model.

## Colour

A colour means the same subsystem everywhere: cpu indigo, wakelock amber, wifi
teal, sensors magenta, screen violet, radio blue, idle grey, wake events red.

## Compatibility

Sections vary a lot between Android versions and OEM builds. Each section is
parsed independently: anything missing or malformed leaves that view empty and
greys out its nav entry rather than failing the parse. Tested against an
OPlus/MediaTek dump with all ten sections present.

## Files

```
battviz.py          CLI and HTTP server
parser.py           dump parser and culprit scoring
live.py             live adb session, polling and logcat
static/index.html   page shell
static/style.css    palette and layout
static/app.js       views, charts, timeline interaction
```

`parser.py` is usable on its own:

```python
import parser
report = parser.parse(open("batterystats.txt").read())
for c in report["culprits"][:5]:
    print(c["label"], c["score"], c["total_mah"])
```
