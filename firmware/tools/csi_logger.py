#!/usr/bin/env python3
"""Log raw CSI from several Wisp nodes for hours, for offline analysis.

    python firmware/tools/csi_logger.py 192.168.10.75 192.168.10.76 --dir data

Writes, per node and per hour:
  <dir>/<node>-<YYYYmmdd-HH>.wcsi   every packet: u64 host time (us) + u16 length + packet
  <dir>/summary-<YYYYmmdd>.csv      once a second per node and source: frames, mean RSSI, motion
Packets are raw CSI plus the node's own link reports (its scores and motion flags) and hive
reports (its layout), so replays can be checked against the firmware; --raw-only for raw CSI
alone. Turn on each node's "Raw CSI stream" switch first. Read .wcsi files with read_wcsi() below.
"""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
import os
import shutil
import socket
import struct
import time

from csi_recorder import HEADER, SUBSCRIBE, motion_score, parse

REC = struct.Struct("<QH")
STREAMS_ALL = 0x07  # raw CSI, link reports, hive reports
MIN_FREE_BYTES = 5 * 1024**3  # stop raw logging below 5 GB free; summaries continue


def read_wcsi(path: str):
    """Yields (host_time_s, packet bytes) from a .wcsi file."""
    with open(path, "rb") as f:
        while head := f.read(REC.size):
            if len(head) < REC.size:
                return
            t_us, n = REC.unpack(head)
            yield t_us / 1e6, f.read(n)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("nodes", nargs="+", help="node IPs or hostnames")
    ap.add_argument("--dir", default="data")
    ap.add_argument("--port", type=int, default=47010)
    ap.add_argument("--raw-only", action="store_true", help="raw CSI only, no link or hive reports")
    args = ap.parse_args()
    subscribe = SUBSCRIBE if args.raw_only else SUBSCRIBE + bytes((STREAMS_ALL,))
    os.makedirs(args.dir, exist_ok=True)

    addrs = {socket.gethostbyname(n): n for n in args.nodes}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", 0))
    sock.settimeout(0.2)
    files: dict[str, tuple[str, object]] = {}
    windows: dict[tuple, deque] = defaultdict(lambda: deque(maxlen=40))
    stats: dict[tuple, list] = defaultdict(lambda: [0, 0])
    last_sub = last_sum = last_disk = 0.0
    total = 0
    raw_ok = True
    print(f"Logging {', '.join(args.nodes)} into {args.dir}/", flush=True)
    while True:
        now = time.time()
        if now - last_sub >= 2:
            for ip in addrs:
                try:
                    sock.sendto(subscribe, (ip, args.port))
                except OSError:
                    pass
            last_sub = now
        try:
            data, (ip, _) = sock.recvfrom(2048)
        except (socket.timeout, OSError):
            data = None
        if now - last_disk >= 60:
            raw_ok = shutil.disk_usage(args.dir).free > MIN_FREE_BYTES
            last_disk = now
        if data and len(data) >= HEADER.size and ip in addrs:
            name = addrs[ip].split(".")[0]
            hour = time.strftime("%Y%m%d-%H", time.localtime(now))
            path = os.path.join(args.dir, f"{name}-{hour}.wcsi")
            if not raw_ok:
                pass
            elif name not in files or files[name][0] != path:
                if name in files:
                    files[name][1].close()
                files[name] = (path, open(path, "ab"))
            if raw_ok:
                f = files[name][1]
                f.write(REC.pack(int(now * 1e6), len(data)))
                f.write(data)
            total += 1
            p = parse(data)
            if p:
                key = (name, p["source"])
                windows[key].append(p["amp"])
                s = stats[key]
                s[0] += 1
                s[1] += p["rssi"]
        if now - last_sum >= 1:
            day = time.strftime("%Y%m%d", time.localtime(now))
            path = os.path.join(args.dir, f"summary-{day}.csv")
            new = not os.path.exists(path)
            with open(path, "a") as out:
                if new:
                    out.write("time,node,source,frames,rssi,motion\n")
                for (name, src), (n, rssi_sum) in stats.items():
                    if n:
                        out.write(f"{now:.0f},{name},{src},{n},{rssi_sum / n:.1f},"
                                  f"{motion_score(windows[(name, src)]):.2f}\n")
            stats.clear()
            for _, fh in files.values():
                fh.flush()
            last_sum = now
            if int(now) % 600 == 0:
                print(f"{time.strftime('%H:%M')} {total} packets", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
