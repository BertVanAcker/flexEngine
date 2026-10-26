"""Bounded status outbox (EV-05): what to drop when the cloud has been unreachable for long.

Policy (`compact(outbox, max_len)`), applied only when the outbox is longer than `max_len`:
  1. drop superseded transitional reports (Pending, Installing, Removing) -- a later report of the same deployment
     exists, so flexCloud would overwrite them anyway;
  2. if still too long, drop superseded terminal reports (Installed, Failed, Removed) -- keep only the newest
     terminal report per deployment;
  3. never drop the last report of a deployment: with more deployments than `max_len` the outbox stays over the
     bound (the caller logs it).
Order and sequence numbers of the kept reports are unchanged (flexCloud deduplicates by seq; gaps are fine).
FE-4: Staged and Stopped are terminal states; the progress and log events of a build (BuildProgress, BuildLog) are
transitional (the build's BuildSucceeded / BuildFailed says how it ended).
"""

from __future__ import annotations

TRANSITIONAL = frozenset({"Pending", "Installing", "Removing"})
TERMINAL = frozenset({"Installed", "Failed", "Removed", "Staged", "Stopped"})
TRANSITIONAL_EVENTS = frozenset({"BuildProgress", "BuildLog"})


def _transitional(r: dict) -> bool:
    return r.get("state") in TRANSITIONAL or r.get("event") in TRANSITIONAL_EVENTS


def compact(outbox: list[dict], max_len: int) -> tuple[list[dict], int]:
    """(kept, n_dropped). Returns the input list unchanged when it fits."""
    if len(outbox) <= max_len:
        return outbox, 0
    last = {r["dep"]: i for i, r in enumerate(outbox)}
    keep = [i == last[r["dep"]] or not _transitional(r) for i, r in enumerate(outbox)]
    if sum(keep) > max_len:
        newest_terminal: dict[str, int] = {}
        for i, r in enumerate(outbox):
            if keep[i] and r.get("state") in TERMINAL:
                newest_terminal[r["dep"]] = i
        for i, r in enumerate(outbox):
            if keep[i] and i != last[r["dep"]] and newest_terminal.get(r["dep"]) != i:
                keep[i] = False
    kept = [r for i, r in enumerate(outbox) if keep[i]]
    return kept, len(outbox) - len(kept)
