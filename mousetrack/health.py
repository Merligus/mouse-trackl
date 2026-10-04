"""Watchdog for the phantom keyboard this mouse ships with.

In wired mode the ATK A9 Plus enumerates three HID interfaces, and interface 00
is a fake boot keyboard. Shortly after a click it emits a report with modifier
byte 0x64 — LeftAlt + RightShift + AltGr — and AltGr then auto-repeats until the
cable is pulled, which makes the real keyboard unusable.

Captured 2026-10-03: 748 auto-repeats over 26 seconds.

The fix lives in /etc/udev/rules.d/99-atk-a9-no-fake-keyboard.rules, which
unbinds usbhid from that interface on every plug. This module only reports
whether the fix is still in place.
"""
from __future__ import annotations

import glob
import os

AFFECTED_VENDOR = "373b"
RULE = "/etc/udev/rules.d/99-atk-a9-no-fake-keyboard.rules"

OK, BOUND, ABSENT, NO_RULE = "ok", "bound", "absent", "no_rule"

MESSAGES = {
    OK: "teclado fantasma: interface 00 desligada — ok",
    BOUND: "ATENÇÃO: a interface 00 está ligada ao usbhid e vai travar o AltGr",
    ABSENT: "mouse ATK não está no cabo agora",
    NO_RULE: "ATENÇÃO: a regra udev sumiu — o teclado fantasma pode voltar",
}


def check() -> tuple[str, str]:
    """Returns (state, human message)."""
    if not os.path.exists(RULE):
        return NO_RULE, MESSAGES[NO_RULE]
    for interface in glob.glob("/sys/bus/usb/devices/*:1.0"):
        name = os.path.basename(interface)
        device = name.split(":")[0]
        try:
            with open("/sys/bus/usb/devices/%s/idVendor" % device) as handle:
                if handle.read().strip() != AFFECTED_VENDOR:
                    continue
        except OSError:
            continue
        if os.path.exists("/sys/bus/usb/drivers/usbhid/%s" % name):
            return BOUND, MESSAGES[BOUND]
        return OK, MESSAGES[OK]
    return ABSENT, MESSAGES[ABSENT]
