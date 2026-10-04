"""Raw evdev access: find mice, read their packets, measure what they do.

Nothing here imports Qt, so the CLI and the GUI share the same code path.
Reading /dev/input/event* needs membership in the `input` group.
"""
from __future__ import annotations

import ctypes
import fcntl
import glob
import os
import select
import statistics
import struct
import sys
import time
from dataclasses import dataclass, field

# --- evdev ioctls ----------------------------------------------------------

def _IOC(direction: int, letter: str, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord(letter) << 8) | nr


_READ = 2


def EVIOCGNAME(length: int) -> int: return _IOC(_READ, "E", 0x06, length)
def EVIOCGID() -> int: return _IOC(_READ, "E", 0x02, 8)
def EVIOCGBIT(ev: int, length: int) -> int: return _IOC(_READ, "E", 0x20 + ev, length)


EV_SYN, EV_KEY, EV_REL = 0x00, 0x01, 0x02
REL_X, REL_Y, REL_WHEEL, REL_HWHEEL = 0x00, 0x01, 0x08, 0x06
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE, BTN_EXTRA = 0x110, 0x111, 0x112, 0x113, 0x114

BUTTON_NAMES = {
    BTN_LEFT: "esquerdo", BTN_RIGHT: "direito", BTN_MIDDLE: "meio",
    BTN_SIDE: "voltar", BTN_EXTRA: "avançar",
}

EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)

STANDARD_RATES = (125, 250, 500, 1000, 2000, 4000, 8000)

# A polling interval only counts when it is long enough not to be timestamp
# noise and short enough that the hand was still moving.
MIN_INTERVAL = 0.0001
MAX_INTERVAL = 0.05


def _has_bit(blob: bytes, bit: int) -> bool:
    index = bit // 8
    return index < len(blob) and bool(blob[index] >> (bit % 8) & 1)


def _read_sysfs(path: str) -> str | None:
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return None


# --- devices ---------------------------------------------------------------

@dataclass
class Mouse:
    """One event node that behaves like a mouse: REL_X, REL_Y and a left button."""

    path: str
    name: str
    vendor: int
    product: int
    usb: dict = field(default_factory=dict)

    @property
    def ident(self) -> str:
        return "%04x:%04x" % (self.vendor, self.product)

    @property
    def wired(self) -> bool:
        """A receiver enumerates on behalf of the mouse; the mouse itself does not."""
        return not any(word in self.name for word in ("Receiver", "Dongle", "Adapter"))

    @property
    def link(self) -> str:
        return "cabo USB" if self.wired else "sem fio (dongle)"

    def open(self) -> int:
        return os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)


def _usb_info(event_name: str) -> dict:
    """Walk up from an event node until the USB device that owns it."""
    path = os.path.realpath("/sys/class/input/%s/device" % event_name)
    interface: str | None = None
    while path != "/":
        base = os.path.basename(path)
        if ":1." in base and interface is None:
            interface = base.split(":1.")[-1]
        if os.path.exists(os.path.join(path, "idVendor")):
            info = {
                "interface": interface,
                "port": base,
                "speed": _read_sysfs(path + "/speed"),
                "product": _read_sysfs(path + "/product"),
                "manufacturer": _read_sysfs(path + "/manufacturer"),
            }
            if interface is not None:
                endpoints = glob.glob("%s/%s:1.%s/ep_*" % (path, base, interface))
                if endpoints:
                    info["bInterval"] = _read_sysfs(endpoints[0] + "/bInterval")
            return info
        path = os.path.dirname(path)
    return {}


def discover() -> list[Mouse]:
    """Every event node the kernel exposes that looks like a pointing device."""
    found: list[Mouse] = []
    for path in sorted(glob.glob("/dev/input/event*"),
                       key=lambda p: int(p.rsplit("event", 1)[1])):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            rel = ctypes.create_string_buffer(8)
            key = ctypes.create_string_buffer(96)
            fcntl.ioctl(fd, EVIOCGBIT(EV_REL, 8), rel)
            fcntl.ioctl(fd, EVIOCGBIT(EV_KEY, 96), key)
            if not (_has_bit(rel.raw, REL_X) and _has_bit(rel.raw, REL_Y)
                    and _has_bit(key.raw, BTN_LEFT)):
                continue
            name_buf = ctypes.create_string_buffer(256)
            fcntl.ioctl(fd, EVIOCGNAME(256), name_buf)
            id_buf = ctypes.create_string_buffer(8)
            fcntl.ioctl(fd, EVIOCGID(), id_buf)
            _bus, vendor, product, _version = struct.unpack("HHHH", id_buf.raw)
            found.append(Mouse(
                path=path,
                name=name_buf.value.decode(errors="replace"),
                vendor=vendor, product=product,
                usb=_usb_info(os.path.basename(path)),
            ))
        except OSError:
            continue
        finally:
            os.close(fd)
    return found


def resolve(selector: str | None) -> list[Mouse]:
    mice = discover()
    if not mice:
        raise RuntimeError(
            "nenhum mouse encontrado em /dev/input — você está no grupo input?")
    if not selector:
        return mice
    for mouse in mice:
        if selector in (mouse.path, mouse.name, mouse.ident):
            return [mouse]
    raise RuntimeError("mouse %r não encontrado" % selector)


# --- reading ---------------------------------------------------------------

class Reader:
    """Reads several event nodes at once, grouping events into SYN_REPORT packets.

    A packet is what the mouse actually sent in one USB transfer, so the gap
    between packets is the polling interval.
    """

    def __init__(self, mice: list[Mouse]) -> None:
        self.mice: dict[int, Mouse] = {}
        self.failed: list[tuple[Mouse, OSError]] = []
        for mouse in mice:
            try:
                self.mice[mouse.open()] = mouse
            except OSError as exc:
                self.failed.append((mouse, exc))
        self._pending: dict[int, list[tuple[int, int, int]]] = {fd: [] for fd in self.mice}

    def __enter__(self) -> "Reader":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        for fd in list(self.mice):
            try:
                os.close(fd)
            except OSError:
                pass
        self.mice.clear()

    def packets(self, timeout: float, extra_fds: tuple[int, ...] = ()):
        """Yield (mouse, timestamp, events) per packet.

        Yields (None, None, None) instead when one of `extra_fds` became
        readable, which is how the callers notice stdin without blocking.
        """
        if not self.mice and not extra_fds:
            time.sleep(timeout)
            return
        ready, _, _ = select.select(list(self.mice) + list(extra_fds), [], [], timeout)
        for fd in ready:
            if fd in extra_fds:
                yield None, None, None
                continue
            try:
                data = os.read(fd, EVENT_SIZE * 256)
            except OSError:
                # Unplugged mid-read.
                self.mice.pop(fd, None)
                self._pending.pop(fd, None)
                continue
            for offset in range(0, len(data), EVENT_SIZE):
                sec, usec, etype, code, value = struct.unpack(
                    EVENT_FORMAT, data[offset:offset + EVENT_SIZE])
                if etype == EV_SYN and code == 0:
                    if self._pending[fd]:
                        yield self.mice[fd], sec + usec / 1e6, self._pending[fd]
                        self._pending[fd] = []
                else:
                    self._pending[fd].append((etype, code, value))


def is_motion(events) -> bool:
    return any(e[0] == EV_REL and e[1] in (REL_X, REL_Y) for e in events)


# --- measurements ----------------------------------------------------------

@dataclass
class RateResult:
    mouse: Mouse
    packets: int
    hz: float
    nominal: int
    low_hz: float
    high_hz: float
    jitter_ms: float
    median_hz: float
    confidence: float


# Gaps are quantised to this before the fundamental period is looked for.
BIN_SECONDS = 0.00005


def rate_from_timestamps(mouse: Mouse, stamps: list[float]) -> RateResult | None:
    """Smallest recurring gap between packets -> polling rate.

    The naive median is wrong here. A mouse only sends a packet when it has
    movement to report, so a hand that pauses produces gaps that are *multiples*
    of the polling period, and the median lands on whichever multiple the user
    happened to produce most. The fundamental period is the smallest gap that
    recurs often enough not to be noise, so that is what we look for.
    """
    intervals = sorted(b - a for a, b in zip(stamps, stamps[1:])
                       if MIN_INTERVAL < b - a < MAX_INTERVAL)
    if len(intervals) < 20:
        return None

    histogram: dict[int, list[float]] = {}
    for interval in intervals:
        histogram.setdefault(int(interval / BIN_SECONDS), []).append(interval)

    floor = max(3, int(len(intervals) * 0.02))
    candidates = sorted(key for key, values in histogram.items() if len(values) >= floor)
    seed = ((candidates[0] + 0.5) * BIN_SECONDS if candidates
            else statistics.median(intervals[:max(5, len(intervals) // 20)]))

    # The seed sits on the left edge of the first lobe, so widen around it and
    # re-centre. The window stays below 2x the period, which is where the first
    # harmonic (one skipped packet) begins.
    cluster = intervals
    for _ in range(3):
        window = [i for i in intervals if 0.6 * seed <= i <= 1.5 * seed]
        if len(window) < 5:
            break
        cluster = window
        recentred = statistics.median(window)
        if abs(recentred - seed) < seed * 0.01:
            seed = recentred
            break
        seed = recentred

    period = statistics.median(cluster)
    median_period = statistics.median(intervals)
    quantiles = statistics.quantiles(cluster, n=20) if len(cluster) >= 20 else [
        min(cluster), max(cluster)]
    return RateResult(
        mouse=mouse,
        packets=len(stamps),
        hz=1.0 / period,
        nominal=min(STANDARD_RATES, key=lambda r: abs(r - 1.0 / period)),
        low_hz=1.0 / quantiles[-1],
        high_hz=1.0 / quantiles[0],
        jitter_ms=statistics.pstdev(cluster) * 1000 if len(cluster) > 1 else 0.0,
        median_hz=1.0 / median_period,
        # How much of the sample sits on the fundamental period. Low means the
        # hand kept stopping, so the reading is a floor rather than a figure.
        confidence=len(cluster) / len(intervals),
    )


def measure_rate(mice: list[Mouse], seconds: float,
                 on_tick=None) -> list[RateResult]:
    """Collect packet timestamps while the user moves the mouse."""
    stamps: dict[str, list[float]] = {}
    by_path = {m.path: m for m in mice}
    deadline = time.monotonic() + seconds
    with Reader(mice) as reader:
        while time.monotonic() < deadline:
            for mouse, timestamp, events in reader.packets(0.05):
                if mouse is None or not is_motion(events):
                    continue
                stamps.setdefault(mouse.path, []).append(timestamp)
            if on_tick is not None:
                remaining = max(0.0, deadline - time.monotonic())
                total = sum(len(v) for v in stamps.values())
                if on_tick(remaining, total) is False:
                    break
    results = [r for path, s in stamps.items()
               if (r := rate_from_timestamps(by_path[path], s)) is not None]
    return sorted(results, key=lambda r: -r.packets)


@dataclass
class DpiResult:
    mouse: Mouse
    counts: int
    dpi: float
    rounded: int


def dpi_from_counts(mouse: Mouse, counts: int, centimetres: float) -> DpiResult | None:
    if abs(counts) < 50 or centimetres <= 0:
        return None
    dpi = abs(counts) / (centimetres / 2.54)
    return DpiResult(mouse=mouse, counts=abs(counts), dpi=dpi,
                     rounded=int(round(dpi / 50.0) * 50))
