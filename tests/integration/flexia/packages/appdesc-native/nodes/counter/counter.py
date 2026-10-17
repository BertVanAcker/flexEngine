"""Native runnable: reads its config.yaml relative to the working directory (as flcpy nodes do) and counts."""

import argparse
import json
import time
from pathlib import Path

import counter_lib  # a module next to the entry script

ap = argparse.ArgumentParser()
ap.add_argument("--period", type=float, default=1.0)
a = ap.parse_args()
cfg = Path("config.yaml").read_text().strip()
state = Path("count.json")
n = 0
while True:
    n += 1
    state.write_text(json.dumps({"count": n, "config": cfg, "label": counter_lib.LABEL}))
    time.sleep(a.period)
