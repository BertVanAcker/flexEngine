#!/usr/bin/env python3
"""Browser GUI for the flexEngineV2 desk runbooks: the pinned flexNode-arduino runbook GUI (tools/hwtest/gui.py +
hwgui/, unmodified) on the pinned runner with the flex step types registered (plan 8.2 option (c)).

    python hwtest/gui.py                         # http://127.0.0.1:8765, runbooks of hwtest
    python hwtest/gui.py --scan ../flexNode-arduino-conveyerbelt-example   # more runbook roots
    python hwtest/gui.py --host 0.0.0.0          # reachable from the bench network (no login!)

What the wrapper adds: `--scan hwtest` (always), the bench values file hwtest/.hwtest/
secrets.yaml (git-ignored) unless --secrets is given, and the board images of boards/*.svg (LattePanda 3 Delta,
UNO Q, Beckhoff CX, a plain Linux host) as GUI boards, selected with `target: {board: ...}` in a <runbook>.gui.yaml.
Every other option is the GUI's own.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run as flexrun  # noqa: E402

BOARDS_DIR = HERE / "boards"
BOARDS = {
    "lattepanda_3_delta": {"name": "LattePanda 3 Delta", "views": ["top"]},
    "uno_q": {"name": "Arduino UNO Q", "views": ["top"]},
    "beckhoff_cx": {"name": "Beckhoff CX (TwinCAT 3)", "views": ["top"]},
    "simple": {"name": "Linux host", "views": ["top"]},
}
_GUI = {}


def load_gui():
    """The pinned GUI's modules (catalog, server, gui) with the flex boards registered."""
    if _GUI:
        return _GUI
    flexrun.load_runner()
    gdir = flexrun.upstream.runner_dir()
    for p in (str(gdir / "hwgui"), str(gdir)):
        if p not in sys.path:
            sys.path.insert(0, p)
    import catalog
    import server

    for key, info in BOARDS.items():
        catalog.BOARDS.setdefault(key, info)
    orig_get = server.Handler.do_GET

    def do_GET(self):  # board images of this package next to the GUI's own static/boards/
        path = self.path.split("?", 1)[0]
        if path.startswith("/static/boards/"):
            name = os.path.basename(path)
            mine = BOARDS_DIR / name
            if name.endswith(".svg") and mine.is_file() and mine.parent == BOARDS_DIR:
                return self._file(str(mine))
        return orig_get(self)

    server.Handler.do_GET = do_GET
    spec = importlib.util.spec_from_file_location("flexnode_hwtest_gui", gdir / "gui.py")
    gui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gui)
    _GUI.update(catalog=catalog, server=server, gui=gui)
    return _GUI


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    g = load_gui()
    argv = ["--scan", str(HERE)] + argv
    if not any(a == "--secrets" or a.startswith("--secrets=") for a in argv):
        argv += ["--secrets", str(HERE / ".hwtest" / "secrets.yaml")]
    return g["gui"].main(argv)


if __name__ == "__main__":
    sys.exit(main())
