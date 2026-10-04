# CLAUDE.md — MouseTrack

Notes for whoever (or whatever) picks this up next. The README is the user
manual; this file is the design rationale and the list of traps that cost real
debugging.

## What this is

A Linux desktop app that measures what a mouse will not report: **DPI**,
**polling rate**, and live usage. It also changes the pointer sensitivity,
which is the only speed knob actually reachable on this hardware.

Written for a `Compx ATK A9 Plus Nearlink` on CachyOS / KDE Plasma 6 Wayland,
but nothing is hardcoded to it except the phantom-keyboard watchdog.

## Layout

```
bin/mousetrack        launcher (cd to repo root, python3 -m mousetrack)
mousetrack/core.py    evdev: discovery, Reader, estimators. No Qt imports.
mousetrack/ui.py      PySide6 window
mousetrack/cli.py     terminal commands
mousetrack/kde.py     pointer settings over KWin's D-Bus
mousetrack/store.py   calibrations in ~/.config/mousetrack/config.json
mousetrack/health.py  phantom-keyboard watchdog
```

`core.py` must stay Qt-free — the CLI works on a machine with no PySide6.

## Architecture

One background thread (`ui.EventPump`) owns every mouse event node and
accumulates statistics under a lock. Widgets render a snapshot four times a
second. **Every measurement derives from that one stream**, so "Medir" and
"Calibrar" never grab the devices for themselves — they just mark a window in
data that is already flowing.

The pump rescans every 2 s and rebuilds its readers when the set of devices
changes, which is how hotplug works. `Reader.packets()` drops a device that
disappears mid-read instead of raising.

A packet is the events between two `SYN_REPORT`s — one USB transfer. The gap
between packets is the polling interval.

## Traps

### 1. The median polling rate is wrong

A mouse only sends a packet when it has movement to report. A hand that pauses
produces gaps that are *multiples* of the polling period, so the median lands
on whichever multiple the user happened to produce most. Measured 335 Hz on a
1000 Hz mouse this way.

`core.rate_from_timestamps` instead histograms the intervals, takes the
smallest bin that recurs often enough not to be noise, then re-centres a window
around it — always below 2x the period, where the first harmonic begins.

Validated against synthetic data, 125 Hz to 8000 Hz, up to 80% pause
probability: error under 0.3% in every case, where the median was off by 4x.
If you touch this function, re-run that check.

`RateResult.confidence` is the fraction of intervals sitting on the
fundamental. Below 0.25 the reading is a floor, not a figure, and both front
ends say so.

### 2. kcminputrc + reconfigure does not apply pointer settings

This one cost a whole round trip. Writing `kcminputrc` and calling
`org.kde.KWin.reconfigure` changes nothing: KWin reads the per-device input
configuration **when the device appears** and `reconfigure` never revisits it.

What works is KWin's live object, one per input device:

```
/org/kde/KWin/InputDevice/<sysName>
    pointerAcceleration             double, -1.0 .. 1.0   readwrite
    pointerAccelerationProfileFlat  bool                  readwrite
```

`sysName` is the event node basename, e.g. `event14`. `kde.Pointer` writes the
D-Bus property (applies immediately) **and** mirrors it into `kcminputrc`
(survives logout and replug). Both, not one.

### 3. qdbus6 cannot read these properties

`qdbus6 org.kde.KWin /org/kde/KWin/InputDevice/event14 org.kde.KWin.InputDevice.name`
fails with `No such interface`, even though introspecting the same path lists
that interface with that property. `gdbus call ... Properties.Get` works. The
module parses gdbus's `(<value>,)` output in `kde._parse`.

### 4. argparse parents clobber a global option

`-d` lives on a parent parser inherited by every subcommand so it works in both
positions. Without `default=argparse.SUPPRESS` the subparser's own default
(`None`) silently overwrites a `-d` given *before* the subcommand, and the
command then applies to every mouse instead of one. The symptom is quiet and
wrong, not an error.

### 5. DPI cannot be read, and cannot be set

No HID Power Page, no DPI usage, nothing. Probing the vendor feature reports
(`0xFF02` ids `0x13`/`0x08`, `0xFF04` id `0x06`) returns zeros and `ff ff`
without even echoing the report id — on interface 02 too, which declares no
feature report at all. That is the device ignoring us, not data.

So DPI is **calibrated**, not read: count raw `REL_X` over a distance the user
measures with a ruler. And it is **not settable** — changing it needs the
closed Compx protocol. Do not blind-write vendor reports to someone's mouse to
guess at it.

Battery is unavailable for the same reason. The honest path to it is capturing
the Windows `ATK Hub` USB traffic with Wireshark/USBPcap and reimplementing the
report. Until then the app says so plainly instead of inventing a number.

## The phantom keyboard

In wired mode this mouse enumerates three HID interfaces, and **interface 00 is
a fake boot keyboard**. Shortly after a click it emits a report with modifier
byte `0x64` — LeftAlt + RightShift + AltGr — and AltGr then auto-repeats until
the cable is pulled, which makes the real keyboard unusable.

Captured 2026-10-03 from evdev: 748 auto-repeats over 26 seconds, every one of
them from interface 00. Nothing came from interface 01.

Fixed outside this repo, in `/etc/udev/rules.d/99-atk-a9-no-fake-keyboard.rules`:

```
ACTION=="bind", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_interface", DRIVER=="usbhid", \
  ATTRS{idVendor}=="373b", ATTRS{idProduct}=="1115", ATTR{bInterfaceNumber}=="00", \
  RUN+="/bin/sh -c 'echo %k > /sys/bus/usb/drivers/usbhid/unbind'"
```

`health.check()` reports whether that is still in place; the window shows it in
the footer. Side buttons are safe — `BTN_SIDE` and `BTN_EXTRA` were confirmed
to come from interface 02, which the rule never touches.

## Hardware topology

```
1-11.1  3554:fa09  combo 2.4G dongle (keyboard AND mouse)
        iface 0 -> boot keyboard
        iface 1 -> consumer + system + NKRO keyboard + mouse (report id 0x07)

1-11.2  373b:1115  ATK A9 Plus on the cable
        iface 0 -> fake boot keyboard   <- unbound by the udev rule
        iface 1 -> consumer + system + NKRO keyboard + vendor 0xFF02/0xFF04
        iface 2 -> mouse: 5 buttons, wheel, tilt, motion

        373b:10c9  mouse-only Nearlink dongle (seen in kcminputrc)
```

Three paths to one physical mouse, with different `vid:pid`. That is why the
device picker sometimes lists more than one, and why calibrations are keyed by
`vid:pid` so they never mix.

The device negotiates full speed (12 Mbps) behind an ASMedia ASM1074 hub, so
1000 Hz is the physical ceiling regardless of what the firmware is set to —
USB frames are 1 ms. `bInterval=1` on the endpoint only states that ceiling,
never the configured rate.

## Testing

No test suite yet. What was actually run:

- GUI smoke test under `QT_QPA_PLATFORM=offscreen`, driving `_tick`,
  `_start_rate`, the calibration dialog, device switching and the sensitivity
  controls, asserting no exceptions.
- The rate estimator against synthetic timestamp streams (see trap 1).
- Every CLI subcommand.

`rate`, `dpi` and the live panel need a human moving a mouse, so they cannot be
asserted headless.

When changing pointer settings while testing, **put them back**. They are live
and the user feels them.
