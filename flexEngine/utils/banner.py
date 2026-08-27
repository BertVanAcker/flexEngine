#**************************************************************************
# * Copyright (C) 2025-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the flexIA project.
# *
# * flexIA can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
"""ASCII welcome banner for the flexEngine CLI (styled after the Claude CLI)."""
import os
import sys

from flexEngine import __version__, __author__, __email__

#--- ANSI styling -----------------------------------------------------------
_RESET = "\033[0m"
_ACCENT = "\033[38;5;209m"   # flexIA coral/orange - frame + headings
_YELLOW = "\033[38;5;220m"   # rocket exhaust
_GREEN = "\033[38;5;42m"     # agent running
_GREY = "\033[38;5;244m"     # agent stopped / unknown
_BOLD = "\033[1m"
_DIM = "\033[2m"

#--- box-drawing characters ----------------------------------------------
_TL, _TR, _BL, _BR, _H, _V = "╭", "╮", "╰", "╯", "─", "│"

#--- box geometry (visible columns) ---------------------------------------
#   each content row is:  | <sp> LEFT <sp> | <sp> RIGHT <sp> |
_LEFT_W = 30                            # left column (rocket + identity)
_RIGHT_W = 41                           # right column (getting started)
_WIDTH = _LEFT_W + _RIGHT_W + 7         # full outer width, borders included

#--- a small rocket, exhaust burning on the last line -------------------
_ROCKET = [
    "      /\\",
    "     |  |",
    "     |==|",
    "    /|  |\\",
    "   /_|__|_\\",
    "     ~~~~",
]


def _use_color(stream):
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(stream, "isatty") and stream.isatty()


def _shorten(text, width):
    """Clip ``text`` to ``width`` visible columns, keeping the tail (…/foo/bar)."""
    if len(text) <= width:
        return text
    return "…" + text[-(width - 1):]


def _pad(segments, width, color):
    """Render ``segments`` (list of (text, style)) padded to ``width`` visible cols."""
    visible = sum(len(text) for text, _ in segments)
    tail = " " * max(width - visible, 0)
    if color:
        return "".join(f"{style}{text}{_RESET}" if style else text for text, style in segments) + tail
    return "".join(text for text, _ in segments) + tail


def render_banner(color=True, agent_running=None, agent_pid=None, workdir=None):
    a, y, b, d = _ACCENT, _YELLOW, _BOLD, _DIM
    edge = f"{a}{_V}{_RESET}" if color else _V
    workdir = workdir or os.getcwd()

    if agent_running is True:
        status = ("● agent running" + (f" ({agent_pid})" if agent_pid else ""), _GREEN)
    elif agent_running is False:
        status = ("○ agent stopped", _GREY)
    else:
        status = ("○ agent status unknown", _GREY)

    # left column: rocket logo, agent status, then identity + workspace
    left = [[("", None)]]
    for i, art in enumerate(_ROCKET):
        left.append([(art, y if i == len(_ROCKET) - 1 else a)])
    left += [
        [("", None)],
        [("  " + _shorten(status[0], _LEFT_W - 2), status[1])],
        [("", None)],
        [("  " + _shorten(__author__, _LEFT_W - 2), b)],
        [("  " + _shorten(__email__, _LEFT_W - 2), d)],
        [("  " + _shorten(workdir, _LEFT_W - 2), d)],
    ]

    # right column: a compact getting-started list
    right = [
        [("", None)],
        [("Getting started", b + a)],
        [("", None)],
        [("flexEngine init   ", a), ("scaffold a workspace", None)],
        [("flexEngine start  ", a), ("run the agent", None)],
        [("flexEngine stop   ", a), ("stop the agent", None)],
        [("flexEngine stats  ", a), ("device dashboard", None)],
        [("flexEngine logs   ", a), ("tail agent logs", None)],
        [("flexEngine --help ", a), ("all commands", None)],
    ]

    rows = max(len(left), len(right))
    left += [[("", None)]] * (rows - len(left))
    right += [[("", None)]] * (rows - len(right))

    # top border carries the version label, like the Claude CLI
    label = f" flexEngine v{__version__} "
    lead = _TL + _H
    dashes = _H * (_WIDTH - len(lead) - len(label) - 1)
    full_bottom = _H * (_WIDTH - 2)
    if color:
        top = f"{a}{lead}{_RESET}{b}{a}{label}{_RESET}{a}{dashes}{_TR}{_RESET}"
        bottom = f"{a}{_BL}{full_bottom}{_BR}{_RESET}"
    else:
        top = f"{lead}{label}{dashes}{_TR}"
        bottom = f"{_BL}{full_bottom}{_BR}"

    lines = [top]
    for l_segs, r_segs in zip(left, right):
        l = _pad(l_segs, _LEFT_W, color)
        r = _pad(r_segs, _RIGHT_W, color)
        lines.append(f"{edge} {l} {edge} {r} {edge}")
    lines.append(bottom)
    return "\n".join(lines)


def print_banner(stream=None, **kwargs):
    stream = stream or sys.stdout
    stream.write(render_banner(color=_use_color(stream), **kwargs) + "\n")
    stream.flush()
