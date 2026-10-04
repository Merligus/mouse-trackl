"""Entry point. Runs the GUI by default; subcommands give the same data in a shell."""
from __future__ import annotations

import argparse
import sys

from . import cli


def build_parser() -> argparse.ArgumentParser:
    # -d is useful both before and after the subcommand, so it lives on a
    # parent parser that every subcommand inherits.
    # SUPPRESS matters: without it the subparser's own default (None) would
    # overwrite a -d given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-d", "--device", default=argparse.SUPPRESS,
                        help="node, nome ou vid:pid (padrão: todos)")

    parser = argparse.ArgumentParser(
        prog="mousetrack", parents=[common],
        description="Mede DPI, taxa de polling e uso do mouse. "
                    "Sem argumentos, abre a interface.")
    sub = parser.add_subparsers(dest="command")

    def add(name: str, help_text: str):
        return sub.add_parser(name, help=help_text, parents=[common])

    add("list", "lista os mouses detectados").set_defaults(fn=cli.cmd_list)
    add("status", "resumo e valores salvos").set_defaults(fn=cli.cmd_status)
    add("health", "checa o teclado fantasma").set_defaults(fn=cli.cmd_health)

    rate = add("rate", "mede a taxa de polling real")
    rate.add_argument("-s", "--seconds", type=int, default=10)
    rate.set_defaults(fn=cli.cmd_rate)

    dpi = add("dpi", "calibra o DPI por distância física")
    dpi.add_argument("-c", "--cm", type=float, default=20.0, help="distância em cm (padrão 20)")
    dpi.add_argument("-a", "--axis", choices=("x", "y"), default="x")
    dpi.add_argument("--stage", help="rótulo do estágio de DPI, ex: 2")
    dpi.set_defaults(fn=cli.cmd_dpi)

    sens = add("sensitivity", "lê ou ajusta a sensibilidade do ponteiro")
    sens.add_argument("--set", type=float, metavar="-1.0..1.0")
    sens.add_argument("--profile", choices=("flat", "adaptive"))
    sens.add_argument("--reset", action="store_true", help="volta ao padrão do device")
    sens.set_defaults(fn=cli.cmd_sensitivity)

    add("gui", "abre a interface").set_defaults(fn=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # A subcommand's own -d wins; the global one applies when it has none.
    args.device = getattr(args, "device", None)
    if args.command in (None, "gui"):
        from . import ui
        return ui.run()
    try:
        return args.fn(args)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
