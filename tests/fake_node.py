#!/usr/bin/env python3
"""A fake Wisp node: answers subscriptions with link and hive reports, no hardware needed.

Listens on UDP 47010 and sends every subscriber (lease 10 s, up to 4) a link report 5 times a
second, for 3 links: the access point and two other nodes, with scores that drift and burst.
Subscribers that want the hive get a hive report every 5 s: the three nodes in a triangle.
Packet format: docs/PROTOCOL.md. Self-contained on purpose, so it also checks the engine's parser.

    python tests/fake_node.py
    python tests/fake_node.py --mac 02:57:49:53:50:01 --port 47010

Then in Home Assistant: Wisp, Add node, the IP address of this computer. Home Assistant always
subscribes on port 47010; other ports are for tests.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import struct
import time

PORT = 47010
LEASE = 10.0
MAX_SUBSCRIBERS = 4
INTERVAL = 0.2
HIVE_INTERVAL = 5.0
DEFAULT_MAC = "02:57:49:53:50:01"
# (transmitter, kind): the access point, then two nodes
DEFAULT_LINKS = [("a8:29:48:db:b6:70", 0), ("02:57:49:53:50:02", 1), ("02:57:49:53:50:03", 1)]
HIVE_IN_SYNC = 0x01

_REPORT_HEAD = struct.Struct("<4sBBHI6sBBI")  # common header + link report fields, 24 bytes
_LINK = struct.Struct("<6sBbHHBB")  # 14 bytes
_HIVE_HEAD = struct.Struct("<4sBBHI6sIBBBB")  # common header + hive report fields, 26 bytes
_POINT = struct.Struct("<6shh")  # 10 bytes
_ROW = struct.Struct("<6sHB")  # 9 bytes
_ENTRY = struct.Struct("<6sb")  # 7 bytes


def mac_bytes(mac: str) -> bytes:
    return bytes.fromhex(mac.replace(":", ""))


def encode_link(transmitter: str, kind: int, rssi: int, score: int, spread: int, frames: int, flags: int) -> bytes:
    """score and spread are x100 (score 65535 = unknown), rssi -128 = no frames."""
    return _LINK.pack(mac_bytes(transmitter), kind, rssi, score, spread, frames, flags)


def encode_report(seq: int, node: str, links: list[tuple], uptime: int = 0, flags: int = 0) -> bytes:
    """links: (transmitter, kind, rssi, score x100, spread x100, frames, flags) each."""
    head = _REPORT_HEAD.pack(b"WISP", 1, 2, _REPORT_HEAD.size, seq, mac_bytes(node), len(links), flags, uptime)
    return head + b"".join(encode_link(*link) for link in links)


def encode_hive_report(seq: int, node: str, hive_hash: int, layout: list[tuple], rows: list[tuple],
                       flags: int = HIVE_IN_SYNC) -> bytes:
    """layout: (node, x cm, y cm) each; rows: (origin, version, [(neighbour, rssi), ...]) each."""
    head = _HIVE_HEAD.pack(b"WISP", 1, 3, _HIVE_HEAD.size, seq, mac_bytes(node), hive_hash, flags,
                           len(layout), len(rows), 0)
    body = b"".join(_POINT.pack(mac_bytes(mac), x, y) for mac, x, y in layout)
    for origin, version, entries in rows:
        body += _ROW.pack(mac_bytes(origin), version, len(entries))
        body += b"".join(_ENTRY.pack(mac_bytes(mac), rssi) for mac, rssi in entries)
    return head + body


def parse_subscribe(data: bytes) -> int | None:
    """Stream mask of a subscription request, None if it is not one."""
    if len(data) < 5 or data[:4] != b"WSUB" or data[4] != 1:
        return None
    return data[5] if len(data) >= 6 else 0x01  # 5 bytes: raw CSI only


class FakeNode(asyncio.DatagramProtocol):
    def __init__(self, mac: str = DEFAULT_MAC, links: list[tuple[str, int]] | None = None,
                 interval: float = INTERVAL, verbose: bool = False) -> None:
        self.mac = mac
        self.links = links or DEFAULT_LINKS
        self.interval = interval
        self.verbose = verbose
        self.subscribers: dict[tuple[str, int], tuple[float, int]] = {}  # addr: (lease end, mask)
        self.seq = 0
        self.hive_seq = 0
        self.started = time.monotonic()
        self.transport: asyncio.DatagramTransport | None = None
        self._task: asyncio.Task | None = None
        self._hive_due = 0.0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        mask = parse_subscribe(data)
        if mask is None:
            return
        now = time.monotonic()
        if addr not in self.subscribers and len(self.subscribers) >= MAX_SUBSCRIBERS:
            del self.subscribers[min(self.subscribers, key=lambda a: self.subscribers[a][0])]
        if self.verbose and addr not in self.subscribers:
            print(f"subscriber {addr[0]}:{addr[1]} streams 0x{mask:02x}", flush=True)
        self.subscribers[addr] = (now + LEASE, mask)

    def report(self, now: float) -> bytes:
        """One report: scores drift around 1 and burst to about 3 for 4 s of every 20 s."""
        t = now - self.started
        links = []
        for i, (transmitter, kind) in enumerate(self.links):
            walking = (t + 6 * i) % 20 < 4
            score = 1.0 + 0.2 * math.sin(t / 3 + i) + (2.0 + math.sin(t * 2)) * walking
            spread = 2.0 + 1.5 * walking + 0.5 * math.sin(t + i)
            rssi = round(-50 - 8 * i + 3 * math.sin(t / 5 + i))
            links.append((transmitter, kind, rssi, round(score * 100), round(spread * 100), 20, int(score >= 2.0)))
        self.seq += 1
        return encode_report(self.seq, self.mac, links, uptime=int(t))

    def hive_report(self) -> bytes:
        """The grid as this node sees it: the nodes on a circle (cm), each hearing the AP and the others."""
        aps = [tx for tx, kind in self.links if kind == 0]
        nodes = [self.mac] + [tx for tx, kind in self.links if kind == 1]
        angles = [math.pi * (1.25 - 2 * i / len(nodes)) for i in range(len(nodes))]
        layout = [(mac, round(180 * math.cos(a)), round(180 * math.sin(a))) for mac, a in zip(nodes, angles)]
        rows = [
            (origin, 1, [(mac, -48 - 6 * i) for i, mac in enumerate(aps + [n for n in nodes if n != origin])])
            for origin in nodes
        ]
        self.hive_seq += 1
        return encode_hive_report(self.hive_seq, self.mac, 0x57495350, layout, rows)

    def send_reports(self) -> int:
        now = time.monotonic()
        self.subscribers = {a: s for a, s in self.subscribers.items() if s[0] > now}
        wanted = [a for a, (_, mask) in self.subscribers.items() if mask & 0x02]
        if wanted and self.transport:
            packet = self.report(now)
            for addr in wanted:
                self.transport.sendto(packet, addr)
        hive = [a for a, (_, mask) in self.subscribers.items() if mask & 0x04]
        if hive and self.transport and now >= self._hive_due:
            self._hive_due = now + HIVE_INTERVAL
            packet = self.hive_report()
            for addr in hive:
                self.transport.sendto(packet, addr)
        return len(wanted)

    async def run(self) -> None:
        while True:
            self.send_reports()
            await asyncio.sleep(self.interval)

    async def start(self, host: str = "0.0.0.0", port: int = PORT) -> int:
        """Bind and start sending. Returns the bound port (useful with port 0)."""
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: self, local_addr=(host, port))
        self._task = loop.create_task(self.run())
        return self.transport.get_extra_info("sockname")[1]

    def close(self) -> None:
        if self._task:
            self._task.cancel()
        if self.transport:
            self.transport.close()


async def _main(args: argparse.Namespace) -> None:
    node = FakeNode(args.mac, verbose=True)
    port = await node.start(args.host, args.port)
    print(f"Fake Wisp node {args.mac} on UDP {args.host}:{port}, {len(node.links)} links. Ctrl+C to stop.", flush=True)
    try:
        while True:
            await asyncio.sleep(5)
            print(f"seq {node.seq}, {len(node.subscribers)} subscriber(s)", flush=True)
    finally:
        node.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0", help="address to listen on")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--mac", default=DEFAULT_MAC, help="this node's MAC, as Home Assistant will see it")
    try:
        asyncio.run(_main(parser.parse_args()))
    except KeyboardInterrupt:
        pass
