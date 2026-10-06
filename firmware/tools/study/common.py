"""Shared pieces of the room presence study: paths, the owner's labels of 2026-10-05, the plan and
calibration, and the recordings turned into what the integration sees once a second.

The integration's view is rebuilt with its own LinkTable (engine/links.py): every link report in
host-time order, a tick each whole second, a link live while its latest score is at most
ROOM_LINK_AGE (3 s) old, "confirmed" while a report flagged it within the last second. The result
is cached as numpy arrays (data/study/cache), so each experiment replays hours in seconds.

Environment:
  WISP_ENGINE_ROOT  folder holding custom_components/wisp/engine (default: this repo). The study
                    used a frozen copy, so another branch editing the engine does not move the baseline.
  WISP_STUDY_DATA   folder with calibration.json and plans.json (default: data/study)
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
import glob
import json
import os
from pathlib import Path
import struct
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
ENGINE_ROOT = os.environ.get("WISP_ENGINE_ROOT", str(ROOT))
STUDY_DATA = Path(os.environ.get("WISP_STUDY_DATA", ROOT / "data" / "study"))
CACHE = STUDY_DATA / "cache"
sys.path.insert(0, ENGINE_ROOT)
sys.path.insert(0, str(ROOT / "firmware" / "tools"))

from custom_components.wisp.engine.links import LinkTable  # noqa: E402
from custom_components.wisp.engine.protocol import LinkReport, ProtocolError, parse_packet  # noqa: E402

FLOOR = "ground_floor"
DAY = (2026, 10, 5)
ROOM_LINK_AGE = 3.0  # const.py
LINK_TIMEOUT = 10.0  # const.py
CONFIRMED_WITHIN = 1.0  # presence.py
ACTIVE_WITHIN = 2.0  # presence.py
REC = struct.Struct("<QH")

NODES = {
    "44:1b:f6:8d:58:58": "n2",  # service area wall
    "ac:27:6e:a8:c7:7c": "n1",  # hallway, by the system room
    "e0:72:a1:d7:14:30": "n3",  # office corner
    "58:cf:79:ee:90:58": "n4",  # play room corner (C3)
}
AP = "a8:29:48:e1:6d:70"
SHORT = {
    "office": "office", "downstairs_bathroom": "wc", "kid_s_room": "play", "service_area": "util",
    "hallway_ground_floor": "hall", "system": "system", None: "empty",
}
HALL = "hallway_ground_floor"


def at(hms: str, day: tuple[int, int, int] = DAY) -> int:
    """Local time on the study's day (or the next one for hours past midnight) to epoch seconds."""
    h, m, *s = (int(v) for v in hms.split(":"))
    y, mo, d = day
    return int(time.mktime((y, mo, d, h, m, s[0] if s else 0, 0, 0, -1)))


def hms(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t))


# The owner's notes, local time 2026-10-05, ground floor.
STAYS = [  # (from, to, room or None for an empty floor, activity)
    ("21:23:00", "21:31:00", "office", "desk"),
    # The notes say 21:58:00; from 21:55:48 every link shows someone walking through the hallway,
    # the utility room and the play room (10+ links flagged, scores to 7 on the play room's
    # links), so the desk ends there. Scored to 21:55:45.
    ("21:49:00", "21:55:45", "office", "desk"),
    ("22:01:34", "22:03:38", "office", "desk"),
    ("22:04:00", "22:05:40", "kid_s_room", "sitting"),
    ("22:06:05", "22:07:45", "office", "desk"),
    ("22:08:30", "22:12:15", None, "empty"),
    ("22:20:40", "22:24:00", "office", "desk"),
    ("22:29:00", "22:36:50", None, "empty"),
]
WALKS = [  # (from, to, start room, end room or None: left the floor)
    ("22:03:41", "22:03:56", "office", "kid_s_room"),
    ("22:05:44", "22:06:00", "kid_s_room", "office"),
    ("22:07:47", "22:08:05", "office", None),
    ("22:12:20", "22:12:35", None, "office"),
]
# Rooms walked one after the other, times unknown (scored as a sequence only)
ROUTE = ("23:29:00", "23:38:00", ["hallway_ground_floor", "office", "hallway_ground_floor", "service_area",
                                   "kid_s_room", "hallway_ground_floor"])


def windows() -> list[tuple[int, int, str | None, str]]:
    return [(at(a), at(b), room, kind) for a, b, room, kind in STAYS]


def walks() -> list[tuple[int, int, str | None, str | None]]:
    return [(at(a), at(b), r0, r1) for a, b, r0, r1 in WALKS]


def load_plan() -> dict:
    return json.loads((STUDY_DATA / "plans.json").read_text())["data"]["floors"][FLOOR]


def load_calibration(name: str = "calibration.json") -> dict:
    return json.loads((STUDY_DATA / name).read_text())["data"]


def areas(calibration: dict) -> list[str]:
    return list(calibration["areas"].keys())


# Recordings to seconds


def _reports_of(path: str, start: float, end: float) -> list[tuple[float, bytes]]:
    out = []
    data = Path(path).read_bytes()
    pos, n = 0, len(data)
    while pos + REC.size <= n:
        t_us, ln = REC.unpack_from(data, pos)
        pos += REC.size
        if pos + ln > n:
            break
        if ln >= 24 and data[pos + 5] == 2:
            t = t_us / 1e6
            if start <= t <= end:
                out.append((t, data[pos:pos + ln]))
        pos += ln
    return out


def files_for(start: float, end: float, data_dir: Path = ROOT / "data") -> list[str]:
    hours = set()
    t = start - 3600
    while t <= end + 3600:
        hours.add(time.strftime("%Y%m%d-%H", time.localtime(t)))
        t += 1800
    return sorted(p for p in glob.glob(str(data_dir / "wisp-*.wcsi")) if p[-16:-5] in hours)


@dataclass
class Seconds:
    """What the integration sees once a second, per link (transmitter, receiver)."""

    t: np.ndarray  # [T] epoch seconds of each tick
    keys: list[tuple[str, str]]
    score: np.ndarray  # [T, L] latest score while live, NaN otherwise
    rssi: np.ndarray  # [T, L] latest RSSI while live, NaN otherwise
    motion: np.ndarray  # [T, L] latest report's motion flag (live links)
    confirmed: np.ndarray  # [T, L] a report flagged it confirmed within CONFIRMED_WITHIN
    breathing: np.ndarray  # [T, L]
    confirms: np.ndarray  # [T, L] the receiving node's firmware confirms motion itself
    spread: np.ndarray  # [T, L]

    def index(self, t: float) -> int:
        return int(np.searchsorted(self.t, t))

    def slice(self, start: float, end: float) -> Seconds:
        i, j = self.index(start), self.index(end)
        return Seconds(self.t[i:j], self.keys, *(getattr(self, f)[i:j] for f in
                       ("score", "rssi", "motion", "confirmed", "breathing", "confirms", "spread")))

    def frame(self, i: int, nodes: set[str] | None = None) -> dict:
        """The integration's inputs at tick i: scores, moving links, signal, confirmed links,
        breathing links, and whether every live link's node confirms motion itself."""
        live = ~np.isnan(self.score[i])
        keys = [k for k, ok in zip(self.keys, live, strict=True) if ok and (nodes is None or k[1] in nodes)]
        idx = [self.keys.index(k) for k in keys] if keys else []
        row = {k: j for k, j in zip(keys, idx, strict=True)}
        return {
            "scores": {k: float(self.score[i, j]) for k, j in row.items()},
            "moving": {k for k, j in row.items() if self.motion[i, j]},
            "signal": {k: (None if np.isnan(self.rssi[i, j]) else float(self.rssi[i, j])) for k, j in row.items()},
            "confirmed": {k for k, j in row.items() if self.confirmed[i, j]},
            "breathing": {k for k, j in row.items() if self.breathing[i, j]},
            "confirms": bool(row) and all(bool(self.confirms[i, j]) for j in row.values()),
        }


def extract(start: float, end: float, data_dir: Path = ROOT / "data") -> Seconds:
    """Every whole second from start to end, as the integration's link table holds it."""
    files = files_for(start, end, data_dir)
    lead = start - LINK_TIMEOUT - 5
    with ProcessPoolExecutor(max_workers=min(8, len(files) or 1)) as pool:
        chunks = list(pool.map(_reports_of, files, [lead] * len(files), [end] * len(files)))
    packets = sorted((p for chunk in chunks for p in chunk), key=lambda p: p[0])
    table = LinkTable(LINK_TIMEOUT)
    confirms: dict[str, bool] = {}
    ticks = np.arange(int(start), int(end), dtype=np.int64)
    rows: list[dict] = []
    keys: dict[tuple[str, str], int] = {}
    k = 0
    for now in ticks:
        while k < len(packets) and packets[k][0] <= now:
            t, pkt = packets[k]
            k += 1
            try:
                report = parse_packet(pkt)
            except ProtocolError:
                continue
            if isinstance(report, LinkReport):
                table.apply(report, t)
                confirms[report.node] = report.confirms
        row = {}
        for key, link in table.links.items():
            if link.score is None or now - link.updated > ROOM_LINK_AGE:
                continue
            keys.setdefault(key, len(keys))
            conf = link.confirmed_at is not None and now - link.confirmed_at <= CONFIRMED_WITHIN
            row[key] = (link.score, link.rssi, link.motion, conf, link.breathing,
                        confirms.get(link.receiver, False), link.spread)
        rows.append(row)
    T, L = len(ticks), len(keys)
    arr = {f: np.full((T, L), np.nan, np.float32) for f in ("score", "rssi", "spread")}
    flags = {f: np.zeros((T, L), bool) for f in ("motion", "confirmed", "breathing", "confirms")}
    for i, row in enumerate(rows):
        for key, (score, rssi, motion, conf, breath, fw, spread) in row.items():
            j = keys[key]
            arr["score"][i, j] = score
            arr["rssi"][i, j] = np.nan if rssi is None else rssi
            arr["spread"][i, j] = spread
            flags["motion"][i, j] = motion
            flags["confirmed"][i, j] = conf
            flags["breathing"][i, j] = breath
            flags["confirms"][i, j] = fw
    return Seconds(ticks, list(keys), arr["score"], arr["rssi"], flags["motion"], flags["confirmed"],
                   flags["breathing"], flags["confirms"], arr["spread"])


def seconds(start: str = "17:00:00", end: str = "23:59:59", refresh: bool = False) -> Seconds:
    """The cached seconds of the study's day between two local times."""
    t0, t1 = at(start), at(end)
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"seconds-{t0}-{t1}.npz"
    if path.exists() and not refresh:
        z = np.load(path, allow_pickle=False)
        keys = [tuple(k.split(">")) for k in z["keys"]]
        return Seconds(z["t"], keys, *(z[f] for f in
                       ("score", "rssi", "motion", "confirmed", "breathing", "confirms", "spread")))
    s = extract(t0, t1)
    np.savez_compressed(path, t=s.t, keys=np.array([f"{a}>{b}" for a, b in s.keys]), score=s.score, rssi=s.rssi,
                        motion=s.motion, confirmed=s.confirmed, breathing=s.breathing, confirms=s.confirms,
                        spread=s.spread)
    return s


def link_name(key: tuple[str, str]) -> str:
    def n(mac: str) -> str:
        return NODES.get(mac, "AP" if mac == AP else mac[-5:])
    return f"{n(key[0])}>{n(key[1])}"


if __name__ == "__main__":
    t = time.time()
    s = seconds()
    print(f"{len(s.t)} s, {len(s.keys)} links, {time.time() - t:.0f} s")
    for j, key in enumerate(s.keys):
        live = ~np.isnan(s.score[:, j])
        print(f"  {link_name(key):12s} live {live.mean():.0%}  motion {s.motion[:, j].mean():.1%}  "
              f"confirmed {s.confirmed[:, j].sum()} s  breathing {s.breathing[:, j].sum()} s")
