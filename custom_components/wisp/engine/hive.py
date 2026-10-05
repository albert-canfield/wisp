"""Latest hive state from hive reports: layout, rows and agreement. The caller owns the clock (seconds)."""
from __future__ import annotations

from dataclasses import dataclass

from .links import LinkTable
from .protocol import KIND_AP, HiveReport, HiveRow


@dataclass(slots=True)
class HiveState:
    reporter: str  # node that sent it
    seq: int
    hash: int
    in_sync: bool
    truncated: bool
    layout: dict[str, tuple[float, float]]  # node MAC: (x, y) in metres, relative
    rows: dict[str, HiveRow]  # by origin
    updated: float

    @property
    def nodes(self) -> set[str]:
        """Every node the hive knows: row origins and layout points."""
        return set(self.layout) | set(self.rows)


class HiveTracker:
    """Each node's latest hive report, and the one to show.

    In sync every node reports the same rows. While the grid syncs they differ, so the pick
    prefers an in-sync report, then the fullest layout, and the map does not flip between views.
    A large hive does not fit one report: each carries the rows from a different starting row,
    so while a node's hash stays the same its truncated reports add up to all of its rows.
    """

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self.reports: dict[str, HiveState] = {}

    def apply(self, report: HiveReport, now: float) -> bool:
        """Take in one report. False for a duplicate."""
        old = self.reports.get(report.node)
        if old is not None and old.seq == report.seq and now - old.updated <= self.timeout:
            return False
        rows = {row.origin: row for row in report.rows}
        if report.truncated and old is not None and old.hash == report.hash:
            rows = {**old.rows, **rows}  # same knowledge: the rows this report left out still hold
        self.reports[report.node] = HiveState(
            reporter=report.node,
            seq=report.seq,
            hash=report.hash,
            in_sync=report.in_sync,
            truncated=report.truncated,
            layout={p.node: (p.x, p.y) for p in report.layout},
            rows=rows,
            updated=now,
        )
        return True

    def current(self, now: float) -> HiveState | None:
        """The report to show: the best fresh one, else the last one heard."""
        fresh = [s for s in self.reports.values() if now - s.updated <= self.timeout]
        if not fresh:
            return max(self.reports.values(), key=lambda s: s.updated, default=None)
        return max(fresh, key=lambda s: (s.in_sync, len(s.layout), len(s.rows), s.updated))

    def fresh(self, mac: str, now: float) -> bool:
        state = self.reports.get(mac)
        return state is not None and now - state.updated <= self.timeout

    def forget(self, mac: str) -> None:
        self.reports.pop(mac, None)


def access_points(table: LinkTable, hive: HiveState | None, receivers: set[str]) -> dict[str, list[tuple[str, int]]]:
    """Access points seen as transmitters, each with the receivers that hear it, strongest first.

    Signal from the hive rows (slow medians) where present, else from the latest link report.
    """
    heard: dict[str, dict[str, int]] = {}
    for link in table.links.values():
        if link.kind != KIND_AP or link.receiver not in receivers:
            continue
        by = heard.setdefault(link.transmitter, {})
        if link.rssi is not None:
            by[link.receiver] = link.rssi
    if hive is not None:
        for row in hive.rows.values():
            if row.origin not in receivers:
                continue
            for entry in row.entries:
                if entry.neighbour in heard:
                    heard[entry.neighbour][row.origin] = entry.rssi
    return {
        bssid: sorted(by.items(), key=lambda item: (-item[1], item[0])) for bssid, by in sorted(heard.items())
    }
