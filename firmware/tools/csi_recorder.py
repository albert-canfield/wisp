#!/usr/bin/env python3
"""Record and watch raw CSI from a Wisp node.

Turn on the node's "Raw CSI stream" switch first, then:

    python firmware/tools/csi_recorder.py wisp-a8c77c.local
    python firmware/tools/csi_recorder.py wisp-a8c77c.local --out walk.csv --seconds 60
    python firmware/tools/csi_recorder.py wisp-a8c77c.local --plot   # needs numpy + matplotlib

Every second it prints the frame rate, RSSI and a motion score: how much the shape of the
signal across subcarriers changed over the last second (gain changes cancelled out).
Packet format: firmware/components/wisp/core_raw_packet.h.
"""

from __future__ import annotations

import argparse
from collections import deque
import csv
import math
import socket
import statistics
import struct
import sys
import time

PORT = 47010
SUBSCRIBE = b"WSUB\x01"
HEADER = struct.Struct("<4sBBHI6s6sIbbBBBBBBH")  # 38 bytes, see core_raw_packet.h
SIG_MODES = {0: "legacy", 1: "HT", 3: "VHT"}
# LLTF: 64 subcarriers ordered 0..31 then -32..-1. Usable ones are -26..-1 and 1..26.
LLTF_USED = list(range(1, 27)) + list(range(38, 64))


def parse(packet: bytes) -> dict | None:
    if len(packet) < HEADER.size:
        return None
    f = HEADER.unpack_from(packet)
    magic, version, ptype, header_len, seq, node, src, ts, rssi, noise, ch, ch2, sig, mcs, cwb, flags, n = f
    if magic != b"WISP" or version != 1 or ptype != 1:
        return None
    csi = packet[header_len : header_len + n]
    if len(csi) < 128:
        return None
    raw = struct.unpack(f"<{len(csi)}b", csi)
    used = LLTF_USED[1:] if flags & 0x02 else LLTF_USED  # first word invalid: drop subcarrier 1
    amp = [math.hypot(raw[2 * i + 1], raw[2 * i]) for i in used]  # bytes: imaginary, real
    return {
        "seq": seq,
        "node": node.hex(":"),
        "source": src.hex(":"),
        "ts": ts,
        "rssi": rssi,
        "noise": noise,
        "channel": ch,
        "sig": sig,
        "mcs": mcs,
        "cwb": cwb,
        "flags": flags,
        "len": n,
        "amp": amp,
    }


def motion_score(window: deque) -> float:
    """Mean per-subcarrier spread of gain-normalised amplitudes, in percent."""
    if len(window) < 5:
        return 0.0
    width = min(len(a) for a in window)
    norm = []
    for a in window:
        m = sum(a[:width]) / width or 1.0
        norm.append([x / m for x in a[:width]])
    spread = [statistics.pstdev(col) for col in zip(*norm)]
    return 100.0 * sum(spread) / len(spread)


def bar(value: float, full: float = 10.0, width: int = 30) -> str:
    filled = min(width, int(width * value / full))
    return "#" * filled + "." * (width - filled)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("node", help="node hostname or IP, e.g. wisp-a8c77c.local")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--out", help="write every frame to this CSV file")
    ap.add_argument("--seconds", type=float, default=0, help="stop after this many seconds (0 = until Ctrl+C)")
    ap.add_argument("--plot", action="store_true", help="live plot (numpy + matplotlib)")
    args = ap.parse_args()

    node = (socket.gethostbyname(args.node), args.port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", 0))
    sock.settimeout(0.2)

    writer = None
    if args.out:
        out = open(args.out, "w", newline="")
        writer = csv.writer(out)
        writer.writerow(["host_time", "seq", "ts_us", "rssi", "noise", "channel", "sig", "mcs", "cwb", "flags", "len"]
                        + [f"a{i}" for i in LLTF_USED])

    plot = Plot() if args.plot else None
    start = last_sub = last_print = time.time()
    window: deque = deque()
    frames = lost = 0
    last_seq = None
    kinds: dict[str, int] = {}
    rssis: list[int] = []
    print(f"Subscribing to {args.node} ({node[0]}:{node[1]}). Is the node's 'Raw CSI stream' switch on?")
    try:
        while not args.seconds or time.time() - start < args.seconds:
            now = time.time()
            if now - last_sub >= 2 or frames == 0 and now - last_sub >= 0.5:
                sock.sendto(SUBSCRIBE, node)
                last_sub = now
            try:
                data, _ = sock.recvfrom(2048)
            except socket.timeout:
                data = None
            if data and (p := parse(data)):
                frames += 1
                if last_seq is not None and p["seq"] > last_seq + 1:
                    lost += p["seq"] - last_seq - 1
                last_seq = p["seq"]
                kind = SIG_MODES.get(p["sig"], str(p["sig"]))
                kinds[kind] = kinds.get(kind, 0) + 1
                rssis.append(p["rssi"])
                window.append(p["amp"])
                while len(window) > 40:
                    window.popleft()
                if writer:
                    writer.writerow([f"{now:.3f}", p["seq"], p["ts"], p["rssi"], p["noise"], p["channel"], p["sig"],
                                     p["mcs"], p["cwb"], p["flags"], p["len"]] + [f"{a:.1f}" for a in p["amp"]])
                if plot:
                    plot.add(p["amp"], motion_score(window))
            if now - last_print >= 1:
                score = motion_score(window)
                mix = " ".join(f"{k} {v}" for k, v in sorted(kinds.items()))
                rssi = f"{statistics.mean(rssis):5.1f}" if rssis else "  -  "
                print(f"{now - start:6.0f}s  {sum(kinds.values()):3d} frames/s ({mix or 'none'})  "
                      f"RSSI {rssi}  lost {lost:4d}  motion {score:5.2f} |{bar(score)}|", flush=True)
                kinds, rssis, last_print = {}, [], now
                if plot:
                    plot.draw()
    except KeyboardInterrupt:
        pass
    print(f"\n{frames} frames, {lost} lost" + (f", saved to {args.out}" if args.out else ""))
    return 0


class Plot:
    """Live view: subcarrier amplitudes over time, and the motion score."""

    def __init__(self, frames: int = 300):
        import matplotlib.pyplot as plt
        import numpy as np

        self.np, self.plt = np, plt
        self.amps: deque = deque(maxlen=frames)
        self.scores: deque = deque(maxlen=frames)
        plt.ion()
        self.fig, (self.ax1, self.ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
        self.fig.suptitle("Wisp raw CSI")

    def add(self, amp: list[float], score: float) -> None:
        self.amps.append(amp)
        self.scores.append(score)

    def draw(self) -> None:
        if not self.amps:
            return
        np = self.np
        width = min(len(a) for a in self.amps)
        grid = np.array([a[:width] for a in self.amps]).T
        grid = grid / np.maximum(grid.mean(axis=0, keepdims=True), 1e-6)
        self.ax1.clear()
        self.ax1.imshow(grid, aspect="auto", origin="lower", cmap="magma", vmin=0.3, vmax=1.7)
        self.ax1.set_ylabel("subcarrier")
        self.ax2.clear()
        self.ax2.plot(list(self.scores))
        self.ax2.set_ylabel("motion")
        self.ax2.set_xlabel("frame")
        self.plt.pause(0.001)


if __name__ == "__main__":
    sys.exit(main())
