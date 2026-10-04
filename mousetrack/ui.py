"""PySide6 interface.

One background thread owns every mouse event node and accumulates statistics;
the widgets just render a snapshot of that state four times a second. Every
measurement the app offers is derived from the same stream, so "medir" and
"calibrar" never have to grab the devices for themselves.
"""
from __future__ import annotations

import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from PySide6 import QtCore, QtGui, QtWidgets

from . import core, health, kde, store

REFRESH_MS = 250
RATE_WINDOW_SECONDS = 10
RESCAN_SECONDS = 2.0

ACCENT = "#5c9ded"
MUTED = "#8a8f98"
WARN = "#e5a33c"
BAD = "#e06c6c"
GOOD = "#5fb87a"


# ---------------------------------------------------------------------------
# background event pump
# ---------------------------------------------------------------------------

@dataclass
class DeviceState:
    mouse: core.Mouse
    stamps: deque = field(default_factory=lambda: deque(maxlen=20000))
    abs_x: int = 0
    abs_y: int = 0
    net_x: int = 0
    net_y: int = 0
    wheel: int = 0
    packets: int = 0
    clicks: dict = field(default_factory=dict)
    held: set = field(default_factory=set)
    last_seen: float = 0.0


class EventPump(QtCore.QThread):
    """Reads every mouse continuously and survives hotplug."""

    devices_changed = QtCore.Signal()

    def __init__(self) -> None:
        super().__init__()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._state: dict[str, DeviceState] = {}
        self.error: str | None = None

    def stop(self) -> None:
        self._stop.set()
        self.wait(2000)

    def snapshot(self) -> dict[str, DeviceState]:
        """A shallow copy good enough to render; the counters are plain ints."""
        with self._lock:
            return {
                path: DeviceState(
                    mouse=s.mouse, stamps=deque(s.stamps), abs_x=s.abs_x, abs_y=s.abs_y,
                    net_x=s.net_x, net_y=s.net_y, wheel=s.wheel, packets=s.packets,
                    clicks=dict(s.clicks), held=set(s.held), last_seen=s.last_seen)
                for path, s in self._state.items()
            }

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                mice = core.discover()
            except OSError as exc:
                self.error = str(exc)
                self._stop.wait(RESCAN_SECONDS)
                continue
            if not mice:
                self.error = "nenhum mouse encontrado em /dev/input"
                self._stop.wait(RESCAN_SECONDS)
                continue

            known = {m.path for m in mice}
            with self._lock:
                for path in list(self._state):
                    if path not in known:
                        del self._state[path]
                for mouse in mice:
                    if mouse.path not in self._state:
                        self._state[mouse.path] = DeviceState(mouse=mouse)
            self.devices_changed.emit()

            with core.Reader(mice) as reader:
                if not reader.mice:
                    self.error = ("sem permissão para ler /dev/input — "
                                  "você está no grupo input?")
                    self._stop.wait(RESCAN_SECONDS)
                    continue
                self.error = None
                next_rescan = time.monotonic() + RESCAN_SECONDS
                while not self._stop.is_set() and reader.mice:
                    for mouse, timestamp, events in reader.packets(0.05):
                        if mouse is None:
                            continue
                        self._absorb(mouse, timestamp, events)
                    if time.monotonic() >= next_rescan:
                        if {m.path for m in core.discover()} != known:
                            break
                        next_rescan = time.monotonic() + RESCAN_SECONDS

    def _absorb(self, mouse: core.Mouse, timestamp: float, events) -> None:
        with self._lock:
            state = self._state.get(mouse.path)
            if state is None:
                return
            state.packets += 1
            state.last_seen = timestamp
            if core.is_motion(events):
                state.stamps.append(timestamp)
            for etype, code, value in events:
                if etype == core.EV_REL:
                    if code == core.REL_X:
                        state.abs_x += abs(value)
                        state.net_x += value
                    elif code == core.REL_Y:
                        state.abs_y += abs(value)
                        state.net_y += value
                    elif code in (core.REL_WHEEL, core.REL_HWHEEL):
                        state.wheel += abs(value)
                elif etype == core.EV_KEY and code in core.BUTTON_NAMES:
                    if value == 1:
                        state.held.add(code)
                        state.clicks[code] = state.clicks.get(code, 0) + 1
                    elif value == 0:
                        state.held.discard(code)


# ---------------------------------------------------------------------------
# small widgets
# ---------------------------------------------------------------------------

class Card(QtWidgets.QFrame):
    """A big number with a caption and an optional action button."""

    def __init__(self, title: str, unit: str = "", action: str | None = None) -> None:
        super().__init__()
        self.setObjectName("card")
        self.setFrameShape(QtWidgets.QFrame.NoFrame)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(2)

        caption = QtWidgets.QLabel(title.upper())
        caption.setObjectName("caption")
        layout.addWidget(caption)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)
        self.value = QtWidgets.QLabel("—")
        self.value.setObjectName("value")
        row.addWidget(self.value)
        if unit:
            unit_label = QtWidgets.QLabel(unit)
            unit_label.setObjectName("unit")
            row.addWidget(unit_label, alignment=QtCore.Qt.AlignBottom)
        row.addStretch(1)
        layout.addLayout(row)

        self.detail = QtWidgets.QLabel("")
        self.detail.setObjectName("detail")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)

        self.button: QtWidgets.QPushButton | None = None
        if action:
            layout.addSpacing(6)
            self.button = QtWidgets.QPushButton(action)
            layout.addWidget(self.button)

    def set(self, value: str, detail: str = "") -> None:
        self.value.setText(value)
        self.detail.setText(detail)


class CalibrateDialog(QtWidgets.QDialog):
    """Counts raw REL counts over a distance the user measures with a ruler."""

    def __init__(self, parent, pump: EventPump, mouse: core.Mouse) -> None:
        super().__init__(parent)
        self.pump, self.mouse = pump, mouse
        self.setWindowTitle("Calibrar DPI — %s" % mouse.name)
        self.setMinimumWidth(460)
        self._origin: tuple[int, int] | None = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(12)

        intro = QtWidgets.QLabel(
            "O mouse não informa o DPI, então ele é medido. Escolha uma distância "
            "que você consiga marcar com régua ou pela borda do mousepad, clique "
            "em <b>Começar</b>, mova o mouse em linha reta por exatamente essa "
            "distância e clique em <b>Pronto</b>.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QtWidgets.QFormLayout()
        self.distance = QtWidgets.QDoubleSpinBox()
        self.distance.setRange(5.0, 200.0)
        self.distance.setValue(20.0)
        self.distance.setSuffix(" cm")
        self.distance.setDecimals(1)
        form.addRow("Distância", self.distance)

        self.axis = QtWidgets.QComboBox()
        self.axis.addItems(["Horizontal (para a direita)", "Vertical (para baixo)"])
        form.addRow("Direção", self.axis)

        self.stage = QtWidgets.QLineEdit()
        self.stage.setPlaceholderText("opcional — ex: estágio 2")
        form.addRow("Rótulo", self.stage)
        layout.addLayout(form)

        self.readout = QtWidgets.QLabel("—")
        self.readout.setObjectName("value")
        self.readout.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(self.readout)

        self.hint = QtWidgets.QLabel("Pronto para começar.")
        self.hint.setObjectName("detail")
        self.hint.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(self.hint)

        buttons = QtWidgets.QHBoxLayout()
        self.start = QtWidgets.QPushButton("Começar")
        self.finish = QtWidgets.QPushButton("Pronto")
        self.finish.setEnabled(False)
        cancel = QtWidgets.QPushButton("Cancelar")
        buttons.addWidget(self.start)
        buttons.addWidget(self.finish)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

        self.start.clicked.connect(self._begin)
        self.finish.clicked.connect(self._end)
        cancel.clicked.connect(self.reject)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(100)
        self.result: core.DpiResult | None = None

    def _counts(self) -> tuple[int, int]:
        state = self.pump.snapshot().get(self.mouse.path)
        return (state.net_x, state.net_y) if state else (0, 0)

    def _current(self) -> int:
        x, y = self._counts()
        if self._origin is None:
            return 0
        return (x - self._origin[0]) if self.axis.currentIndex() == 0 else (y - self._origin[1])

    def _begin(self) -> None:
        self._origin = self._counts()
        self.finish.setEnabled(True)
        self.start.setEnabled(False)
        self.axis.setEnabled(False)
        self.distance.setEnabled(False)
        self.hint.setText("Movendo… vá até a marca e clique em Pronto.")

    def _tick(self) -> None:
        if self._origin is None:
            return
        counts = self._current()
        self.readout.setText("%+d counts" % counts)
        partial = core.dpi_from_counts(self.mouse, counts, self.distance.value())
        if partial:
            self.hint.setText("daria %d DPI se você parasse agora" % partial.rounded)

    def _end(self) -> None:
        self.timer.stop()
        self.result = core.dpi_from_counts(self.mouse, self._current(), self.distance.value())
        if self.result is None:
            QtWidgets.QMessageBox.warning(
                self, "Movimento insuficiente",
                "Foram menos de 50 counts. Repita cobrindo a distância inteira "
                "sem levantar o mouse.")
            self.timer.start(100)
            self._origin = None
            self.start.setEnabled(True)
            self.axis.setEnabled(True)
            self.distance.setEnabled(True)
            self.finish.setEnabled(False)
            return
        store.record_dpi(self.mouse.ident, self.result.rounded, self.result.dpi,
                         self.stage.text().strip() or None)
        self.accept()


# ---------------------------------------------------------------------------
# main window
# ---------------------------------------------------------------------------

STYLE = """
QWidget { font-size: 13px; }
#card { background: palette(base); border: 1px solid palette(mid); border-radius: 8px; }
#caption { color: %(muted)s; font-size: 10px; font-weight: 600; letter-spacing: 1px; }
#value { font-size: 30px; font-weight: 300; }
#unit { color: %(muted)s; font-size: 13px; padding-bottom: 7px; }
#detail { color: %(muted)s; font-size: 11px; }
#title { font-size: 17px; font-weight: 600; }
#section { font-size: 11px; font-weight: 600; color: %(muted)s; letter-spacing: 1px; }
#status { font-size: 11px; }
""" % {"muted": MUTED}


class Window(QtWidgets.QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MouseTrack")
        self.setMinimumSize(620, 640)
        self.setStyleSheet(STYLE)

        self.pump = EventPump()
        self.pump.devices_changed.connect(self._refill_devices)
        self.pump.start()

        self._paths: list[str] = []
        self._rate_until: float | None = None
        self._rate_from: float | None = None
        self._build()

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(REFRESH_MS)

    # -- layout ------------------------------------------------------------

    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(14)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("MouseTrack")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch(1)
        self.devices = QtWidgets.QComboBox()
        self.devices.setMinimumWidth(280)
        self.devices.currentIndexChanged.connect(self._device_changed)
        header.addWidget(self.devices)
        outer.addLayout(header)

        self.link = QtWidgets.QLabel("")
        self.link.setObjectName("detail")
        outer.addWidget(self.link)

        cards = QtWidgets.QHBoxLayout()
        cards.setSpacing(12)
        self.rate_card = Card("Taxa de polling", "Hz", "Medir (%ds)" % RATE_WINDOW_SECONDS)
        self.dpi_card = Card("DPI", "", "Calibrar")
        self.battery_card = Card("Bateria", "", "Por quê?")
        for card in (self.rate_card, self.dpi_card, self.battery_card):
            cards.addWidget(card, 1)
        outer.addLayout(cards)

        self.rate_card.button.clicked.connect(self._start_rate)
        self.dpi_card.button.clicked.connect(self._start_dpi)
        self.battery_card.button.clicked.connect(self._explain_battery)
        self.battery_card.set("n/d", "não exposta por este hardware")

        outer.addWidget(self._sensitivity_box())
        outer.addWidget(self._live_box(), 1)

        self.status = QtWidgets.QLabel("")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

    def _sensitivity_box(self) -> QtWidgets.QWidget:
        box = QtWidgets.QFrame()
        box.setObjectName("card")
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        caption = QtWidgets.QLabel("SENSIBILIDADE DO PONTEIRO")
        caption.setObjectName("caption")
        layout.addWidget(caption)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("lento"))
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.setRange(-100, 100)
        self.slider.setTickInterval(25)
        self.slider.setTickPosition(QtWidgets.QSlider.TicksBelow)
        row.addWidget(self.slider, 1)
        row.addWidget(QtWidgets.QLabel("rápido"))
        self.slider_value = QtWidgets.QLabel("+0.00")
        self.slider_value.setMinimumWidth(44)
        self.slider_value.setAlignment(QtCore.Qt.AlignRight)
        row.addWidget(self.slider_value)
        layout.addLayout(row)

        profile_row = QtWidgets.QHBoxLayout()
        profile_row.addWidget(QtWidgets.QLabel("Perfil"))
        self.adaptive = QtWidgets.QRadioButton("Adaptativo")
        self.flat = QtWidgets.QRadioButton("Plano")
        profile_row.addWidget(self.adaptive)
        profile_row.addWidget(self.flat)
        profile_row.addStretch(1)
        self.sens_note = QtWidgets.QLabel("")
        self.sens_note.setObjectName("detail")
        profile_row.addWidget(self.sens_note)
        self.sens_reset = QtWidgets.QPushButton("Restaurar")
        self.sens_reset.setToolTip("Volta ao padrão do dispositivo")
        self.sens_reset.clicked.connect(self._reset_sensitivity)
        profile_row.addWidget(self.sens_reset)
        layout.addLayout(profile_row)

        self.slider.valueChanged.connect(
            lambda v: self.slider_value.setText("%+.2f" % (v / 100.0)))
        self.slider.sliderReleased.connect(self._apply_sensitivity)
        self.adaptive.toggled.connect(self._apply_profile)
        self.flat.toggled.connect(self._apply_profile)
        self._suppress_profile = False
        return box

    def _live_box(self) -> QtWidgets.QWidget:
        box = QtWidgets.QFrame()
        box.setObjectName("card")
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)

        caption = QtWidgets.QLabel("AO VIVO")
        caption.setObjectName("caption")
        layout.addWidget(caption)

        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(4)
        self.live = {}
        rows = [("hz", "Hz agora"), ("distance", "Percorrido"),
                ("wheel", "Roda"), ("packets", "Pacotes")]
        for column, (key, label) in enumerate(rows):
            name = QtWidgets.QLabel(label)
            name.setObjectName("detail")
            value = QtWidgets.QLabel("—")
            grid.addWidget(name, 0, column)
            grid.addWidget(value, 1, column)
            self.live[key] = value
        layout.addLayout(grid)

        layout.addSpacing(6)
        buttons_caption = QtWidgets.QLabel("BOTÕES")
        buttons_caption.setObjectName("caption")
        layout.addWidget(buttons_caption)

        self.buttons: dict[int, QtWidgets.QLabel] = {}
        button_row = QtWidgets.QHBoxLayout()
        button_row.setSpacing(8)
        for code, label in core.BUTTON_NAMES.items():
            chip = QtWidgets.QLabel("%s\n0" % label)
            chip.setAlignment(QtCore.Qt.AlignCenter)
            chip.setMinimumWidth(78)
            chip.setStyleSheet(
                "border:1px solid palette(mid); border-radius:6px; padding:6px;")
            button_row.addWidget(chip)
            self.buttons[code] = chip
        button_row.addStretch(1)
        layout.addLayout(button_row)
        layout.addStretch(1)
        return box

    # -- device selection --------------------------------------------------

    def _refill_devices(self) -> None:
        snapshot = self.pump.snapshot()
        paths = sorted(snapshot, key=lambda p: (not snapshot[p].mouse.wired, p))
        if paths == self._paths:
            return
        previous = self.devices.currentData()
        self._paths = paths
        self.devices.blockSignals(True)
        self.devices.clear()
        for path in paths:
            mouse = snapshot[path].mouse
            self.devices.addItem("%s — %s" % (mouse.name, mouse.link), path)
        if previous in paths:
            self.devices.setCurrentIndex(paths.index(previous))
        self.devices.blockSignals(False)
        self._load_sensitivity()

    def _device_changed(self) -> None:
        self._load_sensitivity()
        self._tick()

    def _selected(self, snapshot) -> DeviceState | None:
        path = self.devices.currentData()
        if path in snapshot:
            return snapshot[path]
        return next(iter(snapshot.values()), None)

    # -- periodic refresh --------------------------------------------------

    def _tick(self) -> None:
        snapshot = self.pump.snapshot()
        if not snapshot:
            self.status.setText(self.pump.error or "procurando dispositivos…")
            return
        state = self._selected(snapshot)
        if state is None:
            return
        mouse = state.mouse
        saved = store.get(mouse.ident)

        usb = mouse.usb
        pieces = [mouse.link, mouse.ident]
        if usb.get("port"):
            pieces.append("porta %s · interface %s" % (usb["port"], usb.get("interface")))
        if usb.get("speed"):
            pieces.append("%s Mbps" % usb["speed"])
        self.link.setText("   ·   ".join(pieces))

        live = core.rate_from_timestamps(mouse, list(state.stamps)[-800:])
        self._update_rate_card(mouse, state, saved, live)

        if saved.get("dpi"):
            stages = saved.get("stages") or {}
            detail = "calibrado em %s" % saved.get("dpi_date", "?")
            if stages:
                detail += "   ·   " + ", ".join(
                    "%s: %d" % (k, v) for k, v in sorted(stages.items()))
            self.dpi_card.set(str(saved["dpi"]), detail)
        else:
            self.dpi_card.set("—", "não calibrado — clique em Calibrar")

        self.live["hz"].setText("%.0f" % live.hz if live else "—")
        dpi = saved.get("dpi")
        travelled = (state.abs_x ** 2 + state.abs_y ** 2) ** 0.5
        self.live["distance"].setText(
            "%.1f cm" % (travelled / dpi * 2.54) if dpi else "%d counts" % travelled)
        self.live["wheel"].setText(str(state.wheel))
        self.live["packets"].setText(str(state.packets))

        for code, chip in self.buttons.items():
            count = state.clicks.get(code, 0)
            chip.setText("%s\n%d" % (core.BUTTON_NAMES[code], count))
            held = code in state.held
            chip.setStyleSheet(
                "border:1px solid %s; border-radius:6px; padding:6px; %s"
                % (ACCENT if held else "palette(mid)",
                   "background:%s; color:white;" % ACCENT if held else ""))

        state_name, message = health.check()
        colour = {health.OK: GOOD, health.ABSENT: MUTED}.get(state_name, BAD)
        self.status.setText('<span style="color:%s">●</span> %s' % (colour, message))

    def _update_rate_card(self, mouse, state, saved, live) -> None:
        if self._rate_until is not None:
            remaining = self._rate_until - time.monotonic()
            if remaining > 0:
                self.rate_card.set("%.0f" % live.hz if live else "—",
                                   "medindo… %.0fs — mexa o mouse" % remaining)
                return
            self._finish_rate(mouse, state)
            return
        if live:
            self.rate_card.set("%.0f" % live.hz,
                               "ao vivo · nominal %d Hz · jitter %.2f ms · amostra %.0f%%"
                               % (live.nominal, live.jitter_ms, live.confidence * 100))
        elif saved.get("hz"):
            self.rate_card.set(str(saved["hz"]),
                               "medido em %s — mexa o mouse para ver ao vivo"
                               % saved.get("hz_date", "?"))
        else:
            self.rate_card.set("—", "mexa o mouse ou clique em Medir")

    # -- actions -----------------------------------------------------------

    def _start_rate(self) -> None:
        self._rate_from = time.time()
        self._rate_until = time.monotonic() + RATE_WINDOW_SECONDS
        self.rate_card.button.setEnabled(False)

    def _finish_rate(self, mouse, state) -> None:
        self._rate_until = None
        self.rate_card.button.setEnabled(True)
        window = [t for t in state.stamps if self._rate_from and t >= self._rate_from]
        result = core.rate_from_timestamps(mouse, window)
        if result is None:
            self.rate_card.set("—", "sem movimento suficiente — tente de novo mexendo o mouse")
            return
        store.record_rate(mouse.ident, result.nominal, result.hz)
        note = "" if result.confidence >= 0.25 else " · movimento picotado, repita"
        self.rate_card.set(str(result.nominal),
                           "medido %.0f Hz · faixa %.0f–%.0f · jitter %.2f ms · salvo%s"
                           % (result.hz, result.low_hz, result.high_hz,
                              result.jitter_ms, note))

    def _start_dpi(self) -> None:
        snapshot = self.pump.snapshot()
        state = self._selected(snapshot)
        if state is None:
            return
        dialog = CalibrateDialog(self, self.pump, state.mouse)
        if dialog.exec() == QtWidgets.QDialog.Accepted and dialog.result:
            QtWidgets.QMessageBox.information(
                self, "DPI calibrado",
                "%d counts em %.1f cm → <b>%d DPI</b><br><br>"
                "Medido: %.0f. Vale repetir 2–3 vezes: variação grande significa "
                "movimento torto ou mouse levantado no meio."
                % (dialog.result.counts, dialog.distance.value(),
                   dialog.result.rounded, dialog.result.dpi))

    def _explain_battery(self) -> None:
        QtWidgets.QMessageBox.information(
            self, "Por que não há bateria",
            "Este mouse não expõe o nível de bateria.<br><br>"
            "Nem o mouse no cabo (<code>373b:1115</code>) nem o dongle "
            "(<code>3554:fa09</code>) declaram HID Power Page "
            "(<code>0x84</code>/<code>0x85</code>) ou Battery Strength no "
            "descritor, e não existe <code>/sys/class/power_supply/</code> para "
            "eles. Sondar os feature reports vendor "
            "(<code>0xFF02</code>, <code>0xFF04</code>) devolve lixo — zeros e "
            "<code>ff ff</code>, sem nem ecoar o report ID.<br><br>"
            "O nível só sai pelo protocolo proprietário da Compx, que é fechado. "
            "Para obtê-lo seria preciso capturar o tráfego USB do software de "
            "configuração no Windows e reimplementar o report.")

    # -- sensitivity -------------------------------------------------------

    def _pointer(self) -> kde.Pointer | None:
        state = self._selected(self.pump.snapshot())
        return kde.Pointer(state.mouse) if state else None

    def _set_sensitivity_enabled(self, enabled: bool, note: str) -> None:
        for widget in (self.slider, self.adaptive, self.flat, self.sens_reset):
            widget.setEnabled(enabled)
        self.sens_note.setText(note)

    def _load_sensitivity(self) -> None:
        pointer = self._pointer()
        if pointer is None:
            return
        if not kde.available():
            self._set_sensitivity_enabled(False, "KWin não respondeu no D-Bus")
            return
        if not pointer.known:
            self._set_sensitivity_enabled(False, "o KWin não controla este device")
            return
        if not pointer.supports_acceleration:
            self._set_sensitivity_enabled(False, "device não aceita aceleração")
            return

        value = pointer.acceleration
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(value * 100)))
        self.slider.blockSignals(False)
        self.slider_value.setText("%+.2f" % value)
        self._suppress_profile = True
        (self.flat if pointer.flat else self.adaptive).setChecked(True)
        self._suppress_profile = False
        self._set_sensitivity_enabled(True, "vale na hora")
        self.flat.setEnabled(pointer.supports_flat)

    def _apply_sensitivity(self) -> None:
        pointer = self._pointer()
        if pointer is None:
            return
        ok = pointer.set_acceleration(self.slider.value() / 100.0)
        self.sens_note.setText("aplicado" if ok else "o KWin recusou")

    def _apply_profile(self) -> None:
        if self._suppress_profile:
            return
        if not (self.flat.isChecked() or self.adaptive.isChecked()):
            return
        pointer = self._pointer()
        if pointer is None:
            return
        ok = pointer.set_flat(self.flat.isChecked())
        self.sens_note.setText("aplicado" if ok else "o KWin recusou")

    def _reset_sensitivity(self) -> None:
        pointer = self._pointer()
        if pointer is None:
            return
        pointer.reset()
        self._load_sensitivity()
        self.sens_note.setText("restaurado ao padrão")

    # -- lifecycle ---------------------------------------------------------

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self.pump.stop()
        super().closeEvent(event)


def run() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("MouseTrack")
    app.setApplicationDisplayName("MouseTrack")
    app.setDesktopFileName("mousetrack")
    window = Window()
    window.show()
    return app.exec()
