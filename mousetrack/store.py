"""Calibration store.

DPI and polling rate cannot be read back from this hardware, so whatever we
measure is kept here, keyed by `vid:pid`. The cable and each dongle are
different USB ids, so their calibrations never mix.
"""
from __future__ import annotations

import json
import os
import time

PATH = os.path.expanduser("~/.config/mousetrack/config.json")


def load() -> dict:
    try:
        with open(PATH) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save(data: dict) -> None:
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    os.replace(tmp, PATH)


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def record_rate(ident: str, nominal: int, measured: float) -> None:
    data = load()
    data.setdefault(ident, {}).update(
        hz=nominal, hz_measured=round(measured, 1), hz_date=_stamp())
    save(data)


def record_dpi(ident: str, rounded: int, measured: float, stage: str | None = None) -> None:
    data = load()
    entry = data.setdefault(ident, {})
    entry.update(dpi=rounded, dpi_measured=round(measured, 1), dpi_date=_stamp())
    if stage:
        # Keep every calibrated stage so the DPI button's steps can be labelled.
        entry.setdefault("stages", {})[stage] = rounded
    save(data)


def get(ident: str) -> dict:
    return load().get(ident, {})
