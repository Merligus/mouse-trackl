"""Pointer settings, through KWin's live D-Bus objects.

The mouse's hardware DPI is locked behind Compx's closed vendor protocol, so
what this app can actually change is libinput's speed multiplier. KWin exposes
one object per input device with the settings as read-write properties:

    /org/kde/KWin/InputDevice/event14
        pointerAcceleration             double, -1.0 .. 1.0
        pointerAccelerationProfileFlat  bool

Writing those applies immediately. Writing `kcminputrc` and calling
`reconfigure` does *not*: KWin reads the per-device configuration when the
device appears, and `reconfigure` never revisits it. So the D-Bus property is
what takes effect, and the config file is written alongside only so the setting
survives a logout or a replug.

`gdbus` is used rather than `qdbus6` because qdbus6 fails to resolve properties
on these objects ("No such interface"), even though introspection lists them.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

SERVICE = "org.kde.KWin"
MANAGER_PATH = "/org/kde/KWin/InputDevice"
IFACE = "org.kde.KWin.InputDevice"

ACCELERATION = "pointerAcceleration"
FLAT_PROFILE = "pointerAccelerationProfileFlat"
SUPPORTS_ACCELERATION = "supportsPointerAcceleration"
SUPPORTS_FLAT = "supportsPointerAccelerationProfileFlat"

PROFILE_ADAPTIVE, PROFILE_FLAT = 0, 1

_VALUE = re.compile(r"^\(<(.*)>,\)$", re.DOTALL)


def available() -> bool:
    return bool(shutil.which("gdbus")) and _manager_responds()


def _manager_responds() -> bool:
    result = subprocess.run(
        ["gdbus", "call", "--session", "--dest", SERVICE,
         "--object-path", MANAGER_PATH,
         "--method", "org.freedesktop.DBus.Properties.Get",
         "org.kde.KWin.InputDeviceManager", "devicesSysNames"],
        capture_output=True, text=True)
    return result.returncode == 0


def _parse(raw: str):
    """gdbus prints `(<0.6>,)`, `(<true>,)`, `(<'name'>,)`, `(<uint32 14139>,)`."""
    match = _VALUE.match(raw.strip())
    if not match:
        return None
    body = match.group(1).strip()
    for prefix in ("uint32 ", "int32 ", "uint64 ", "int64 ", "double "):
        if body.startswith(prefix):
            body = body[len(prefix):].strip()
    if body in ("true", "false"):
        return body == "true"
    if len(body) >= 2 and body[0] == body[-1] == "'":
        return body[1:-1]
    try:
        return float(body) if "." in body or "e" in body.lower() else int(body)
    except ValueError:
        return body


def _device_path(sys_name: str) -> str:
    return "%s/%s" % (MANAGER_PATH, sys_name)


def get(sys_name: str, prop: str):
    result = subprocess.run(
        ["gdbus", "call", "--session", "--dest", SERVICE,
         "--object-path", _device_path(sys_name),
         "--method", "org.freedesktop.DBus.Properties.Get", IFACE, prop],
        capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return _parse(result.stdout)


def set(sys_name: str, prop: str, literal: str) -> bool:
    """`literal` is a gdbus variant, e.g. "<0.600>" or "<true>"."""
    result = subprocess.run(
        ["gdbus", "call", "--session", "--dest", SERVICE,
         "--object-path", _device_path(sys_name),
         "--method", "org.freedesktop.DBus.Properties.Set", IFACE, prop, literal],
        capture_output=True, text=True)
    return result.returncode == 0


# --- per device -------------------------------------------------------------

class Pointer:
    """Live pointer settings for one mouse."""

    def __init__(self, mouse) -> None:
        self.sys_name = os.path.basename(mouse.path)
        self.vendor = mouse.vendor
        self.product = mouse.product
        self.name = mouse.name

    @property
    def known(self) -> bool:
        """KWin only publishes devices it actually drives."""
        return get(self.sys_name, "pointer") is True

    @property
    def supports_acceleration(self) -> bool:
        return get(self.sys_name, SUPPORTS_ACCELERATION) is True

    @property
    def supports_flat(self) -> bool:
        return get(self.sys_name, SUPPORTS_FLAT) is True

    @property
    def acceleration(self) -> float:
        value = get(self.sys_name, ACCELERATION)
        return float(value) if isinstance(value, (int, float)) else 0.0

    @property
    def flat(self) -> bool:
        return get(self.sys_name, FLAT_PROFILE) is True

    def set_acceleration(self, value: float) -> bool:
        value = max(-1.0, min(1.0, value))
        ok = set(self.sys_name, ACCELERATION, "<%.3f>" % value)
        _persist(self.vendor, self.product, self.name, "PointerAcceleration", "%.3f" % value)
        return ok

    def set_flat(self, flat: bool) -> bool:
        ok = set(self.sys_name, FLAT_PROFILE, "<%s>" % ("true" if flat else "false"))
        _persist(self.vendor, self.product, self.name, "PointerAccelerationProfile",
                 str(PROFILE_FLAT if flat else PROFILE_ADAPTIVE))
        return ok

    def reset(self) -> bool:
        default = get(self.sys_name, "defaultPointerAcceleration")
        return self.set_acceleration(float(default) if isinstance(default, (int, float)) else 0.0)


# --- persistence ------------------------------------------------------------

def _persist(vendor: int, product: int, name: str, key: str, value: str) -> None:
    """Mirror the setting into kcminputrc so it survives a logout or a replug.

    KDE keys the group by decimal vendor and product ids, then the device name.
    """
    if not shutil.which("kwriteconfig6"):
        return
    subprocess.run(
        ["kwriteconfig6", "--file", "kcminputrc",
         "--group", "Libinput",
         "--group", str(vendor),
         "--group", str(product),
         "--group", name,
         "--key", key, value],
        capture_output=True, check=False)
