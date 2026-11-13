"""
Main entry point for PS5 Beat Saber (PSVR2) Custom Song Converter.
- Run with no arguments (or double-click the .bat launcher) to open the Desktop GUI.
- Run with CLI arguments (e.g. `python app.py list-slots`) to use the Command-Line Interface.
"""

from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) > 1:
        from ps5_bs_converter.cli import main as cli_main
        return cli_main()

    from ps5_bs_converter.gui import launch_gui
    launch_gui()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
