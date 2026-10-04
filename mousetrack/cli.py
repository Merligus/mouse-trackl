"""Terminal commands. The GUI is the default front end; this is for scripting."""
from __future__ import annotations

import sys
import time

from . import core, health, kde, store


def cmd_list(args) -> int:
    for mouse in core.resolve(args.device):
        print("%-20s %s  %-38s [%s]" % (mouse.path, mouse.ident, mouse.name, mouse.link))
    return 0


def cmd_status(args) -> int:
    mice = core.resolve(args.device)
    print("MOUSETRACK\n" + "=" * 62)
    for mouse in mice:
        usb = mouse.usb
        saved = store.get(mouse.ident)
        print("\n%s" % mouse.name)
        print("  node         %s" % mouse.path)
        print("  usb id       %s" % mouse.ident)
        print("  conexão      %s" % mouse.link)
        if usb:
            print("  porta usb    %s  (interface %s)" % (usb.get("port"), usb.get("interface")))
            if usb.get("speed"):
                label = {"1.5": "low", "12": "full", "480": "high",
                         "5000": "super"}.get(usb["speed"], "?")
                print("  velocidade   %s Mbps (%s speed)" % (usb["speed"], label))
            if usb.get("bInterval"):
                interval = max(1, int(usb["bInterval"]))
                print("  bInterval    %d ms  → teto de %d Hz pelo descritor"
                      % (interval, 1000 // interval))
        if saved.get("dpi"):
            print("  dpi          %d  (calibrado em %s)" % (saved["dpi"], saved.get("dpi_date", "?")))
        else:
            print("  dpi          não calibrado  → mousetrack dpi")
        if saved.get("hz"):
            print("  polling      %d Hz  (medido em %s)" % (saved["hz"], saved.get("hz_date", "?")))
        else:
            print("  polling      não medido  → mousetrack rate")
        if kde.available():
            pointer = kde.Pointer(mouse)
            if pointer.known:
                print("  sensibilidade %+.2f  (perfil %s)"
                      % (pointer.acceleration, "plano" if pointer.flat else "adaptativo"))
        print("  bateria      indisponível (sem HID Power Page neste device)")
    print("\n" + "-" * 62)
    print(health.check()[1])
    return 0


def cmd_rate(args) -> int:
    mice = core.resolve(args.device)
    print("Mexa o mouse continuamente por %ds..." % args.seconds)

    def tick(remaining, total):
        print("\r  %4.1fs restantes   %d pacotes   " % (remaining, total), end="", flush=True)

    results = core.measure_rate(mice, args.seconds, on_tick=tick)
    print()
    if not results:
        print("nenhum movimento capturado — mexa o mouse durante a medição", file=sys.stderr)
        return 1
    for result in results:
        print("\n%s" % result.mouse.name)
        print("  pacotes      %d em %ds" % (result.packets, args.seconds))
        print("  taxa         %.0f Hz" % result.hz)
        print("  nominal      %d Hz" % result.nominal)
        print("  faixa        %.0f – %.0f Hz" % (result.low_hz, result.high_hz))
        print("  jitter       %.3f ms" % result.jitter_ms)
        print("  amostra      %.0f%% dos intervalos no período fundamental"
              % (result.confidence * 100))
        if result.confidence < 0.25:
            print("               (movimento muito picotado — mexa sem parar e repita)")
        store.record_rate(result.mouse.ident, result.nominal, result.hz)
    print("\nsalvo em %s" % store.PATH)
    return 0


def cmd_dpi(args) -> int:
    mice = core.resolve(args.device)
    axis_code = core.REL_X if args.axis == "x" else core.REL_Y
    direction = "DIREITA" if args.axis == "x" else "BAIXO"
    print("\nMova o mouse %g cm em linha reta para a %s, devagar e sem levantar."
          % (args.cm, direction))
    print("Depois pressione ENTER.\n")

    counts: dict[str, int] = {}
    by_path = {m.path: m for m in mice}
    with core.Reader(mice) as reader:
        stop = False
        while not stop:
            for mouse, _timestamp, events in reader.packets(0.2, extra_fds=(sys.stdin.fileno(),)):
                if mouse is None:
                    sys.stdin.readline()
                    stop = True
                    break
                for etype, code, value in events:
                    if etype == core.EV_REL and code == axis_code:
                        counts[mouse.path] = counts.get(mouse.path, 0) + value
            if not stop and counts:
                shown = "   ".join("%s %+d" % (by_path[p].name[:24], c)
                                   for p, c in counts.items())
                print("\r  %s    " % shown, end="", flush=True)
    print()

    results = [r for path, c in counts.items()
               if (r := core.dpi_from_counts(by_path[path], c, args.cm)) is not None]
    if not results:
        print("movimento insuficiente — repita cobrindo a distância inteira", file=sys.stderr)
        return 1
    for result in results:
        print("\n%s" % result.mouse.name)
        print("  counts       %d em %g cm (%.2f pol)" % (result.counts, args.cm, args.cm / 2.54))
        print("  dpi medido   %.0f" % result.dpi)
        print("  dpi provável %d" % result.rounded)
        store.record_dpi(result.mouse.ident, result.rounded, result.dpi, args.stage)
    print("\nsalvo em %s" % store.PATH)
    print("Repita 2–3x. Variação grande = movimento torto ou mouse levantado.")
    return 0


def cmd_sensitivity(args) -> int:
    if not kde.available():
        print("KWin não respondeu no D-Bus (gdbus instalado? sessão Plasma?)",
              file=sys.stderr)
        return 1
    for mouse in core.resolve(args.device):
        pointer = kde.Pointer(mouse)
        if not pointer.known:
            print("%-38s (o KWin não controla este device)" % mouse.name)
            continue
        if args.reset:
            pointer.reset()
        elif args.profile:
            pointer.set_flat(args.profile == "flat")
        elif args.set is not None:
            if not pointer.set_acceleration(args.set):
                print("%-38s falhou ao aplicar" % mouse.name, file=sys.stderr)
                continue
        print("%-38s %+.2f  (perfil %s)"
              % (mouse.name, pointer.acceleration, "plano" if pointer.flat else "adaptativo"))
    return 0


def cmd_health(args) -> int:
    state, message = health.check()
    print(message)
    return 0 if state in (health.OK, health.ABSENT) else 1
