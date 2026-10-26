"""Probe P4: Arduino App Lab (UNO Q / VENTUNO Q) as a flexCloud target."""
import sys
from pathlib import Path

_P2 = Path(__file__).resolve().parents[2] / "p2-reconciling-engine"
if str(_P2) not in sys.path:
    sys.path.insert(0, str(_P2))  # P2 engine, driver SPI and release tooling (used read-only)
