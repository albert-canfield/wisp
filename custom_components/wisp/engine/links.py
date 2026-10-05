"""Latest state per node and per link, from link reports. The caller owns the clock (seconds)."""
from __future__ import annotations

from dataclasses import dataclass

from .protocol import LinkReport

type LinkKey = tuple[str, str]  # (transmitter, receiver)


@dataclass(slots=True)
class LinkState:
    transmitter: str
    receiver: str
    kind: int
    rssi: int | None = None
    score: float | None = None
    spread: float = 0.0
    frames: int = 0
    motion: bool = False
    confirmed: bool = False  # the hive confirms its motion (node firmware 0.1.6+)
    confirmed_at: float | None = None  # the latest report that said so
    updated: float = 0.0
    fresh: bool = True  # a report arrived within the timeout


@dataclass(slots=True)
class NodeState:
    mac: str
    seq: int = 0
    uptime: int = 0
    updated: float = 0.0
    address: str | None = None  # where its reports come from
    reports: int = 0
    lost: int = 0  # gaps in the sequence numbers
    confirms: bool = False  # its firmware flags the links the hive confirms
    last_report: LinkReport | None = None


class LinkTable:
    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self.nodes: dict[str, NodeState] = {}
        self.links: dict[LinkKey, LinkState] = {}

    def apply(
        self, report: LinkReport, now: float, address: str | None = None
    ) -> tuple[list[LinkKey], list[LinkKey]] | None:
        """Take in one report. Returns (links in it, links seen for the first time),
        or None for a duplicate or late packet."""
        node = self.nodes.get(report.node)
        if node is None:
            node = self.nodes[report.node] = NodeState(report.node)
        elif report.uptime >= node.uptime and now - node.updated <= self.timeout:
            # Same boot: sequence numbers only go up. A reboot resets both.
            if report.seq <= node.seq:
                return None
            node.lost += report.seq - node.seq - 1
        node.seq, node.uptime, node.updated, node.last_report = report.seq, report.uptime, now, report
        node.confirms = report.confirms
        node.reports += 1
        if address:
            node.address = address

        keys: list[LinkKey] = []
        new: list[LinkKey] = []
        for link in report.links:
            key = (link.transmitter, report.node)
            state = self.links.get(key)
            if state is None:
                state = self.links[key] = LinkState(link.transmitter, report.node, link.kind)
                new.append(key)
            state.kind = link.kind
            state.rssi, state.score, state.spread = link.rssi, link.score, link.spread
            state.frames, state.motion, state.confirmed = link.frames, link.motion, link.confirmed
            if link.confirmed:
                state.confirmed_at = now
            state.updated, state.fresh = now, True
            keys.append(key)
        return keys, new

    def expire(self, now: float) -> list[LinkKey]:
        """Links that just went quiet: no report for longer than the timeout."""
        quiet = []
        for key, link in self.links.items():
            if link.fresh and now - link.updated > self.timeout:
                link.fresh = False
                quiet.append(key)
        return quiet

    def forget(self, mac: str) -> list[LinkKey]:
        """Drop a node and the links it receives."""
        self.nodes.pop(mac, None)
        keys = [key for key in self.links if key[1] == mac]
        for key in keys:
            del self.links[key]
        return keys
