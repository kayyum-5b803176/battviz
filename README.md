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
