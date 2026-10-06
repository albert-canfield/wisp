#!/usr/bin/env python3
"""Room presence against the owner's labels of 2026-10-05, second by second.

    .venv-ha/bin/python firmware/tools/study/evaluate.py baseline tracker [--detail] [--seq]

Each method replays the recordings as the integration sees them (common.Seconds) from 21:05 to
23:45 in one go (labelled stays and walks, a quiet evening, the room-by-room walk at 23:30),
and says each second where the one person is (a room or nobody), which areas it shows present
(the presence binary sensors) and whether they walk. Ground truth (common.STAYS, WALKS):
  stays    a room (desk, sitting) or the empty floor; calibration runs found in the recording
           (calib_times.py) are left out
  walks    mid-walk seconds count only for the walking flag; each walk is scored as a transition:
           whether the method reaches the destination (a room, or nobody after the stairs) and how
           many seconds after the walk started
  route    23:29 to 23:38, the rooms walked in order: the method's rooms held 3 s or more, in
           order, against the route (edit distance; times are unknown)
  evening  22:41 to 23:28:30, not labelled: no activity on any link for 47 minutes between the
           owner's empty window and the 23:30 walk, so most likely nobody downstairs. Scored
           apart (eve fp/h), not in err.
Metrics:
  err        share of all labelled stay seconds wrong (wrong room, nobody while present, someone
             on the empty floor): the tuning objective
  acc        share of present seconds with the right room (map view: one room)
  miss       share of present seconds showing nobody
  fp/h       empty-floor seconds showing someone, per hour; fp60/h the same from 60 s into each
             empty window (the hold after leaving is the exit latency, not a phantom)
  onsets/h   times per hour someone appears on the empty floor
  sw/min     room changes per minute while the truth stays in one room
  walk rec   share of mid-walk seconds flagged walking; walk fa: share of stay seconds flagged
  P/R        per room, sensor view (every area shown present counts)
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import importlib
import json
import math

from common import CACHE, HALL, ROUTE, SHORT, at, hms, load_calibration, load_plan, seconds, walks, windows

STRETCHES = [("21:05:00", "23:45:00")]
EVENING = ("22:41:00", "23:28:30")
ROOM_CLASSES = ["office", "kid_s_room", None]
SETTLE = 60  # s into an empty window before fp60 counts
WALK_LATE = (10, 30)  # s after a walk ends at which the destination is checked


def calibration_seconds() -> set[int]:
    path = CACHE / "calib_runs.json"
    if not path.exists():
        return set()
    out = set()
    for run in json.loads(path.read_text()):
        out.update(range(run["from"], run["to"] + 1))
    return out


@dataclass
class Truth:
    stay: dict[int, tuple[str | None, int]] = field(default_factory=dict)  # t: (room, window index)
    walk: dict[int, int] = field(default_factory=dict)  # t: walk index (mid-walk seconds)


def truth(selected: set[int] | None = None) -> Truth:
    """selected: window indexes to keep (None: all)."""
    out = Truth()
    skip = calibration_seconds()
    for w, (a, b, room, _kind) in enumerate(windows()):
        if selected is not None and w not in selected:
            continue
        for t in range(a, b):
            if t not in skip:
                out.stay[t] = (room, w)
    for k, (a, b, _r0, _r1) in enumerate(walks()):
        for t in range(a + 2, b):
            out.walk[t] = k
    return out


def make(name: str, calibration: dict, plan: dict, **params):
    if name == "baseline":
        from baseline import Baseline
        return Baseline(calibration, plan)
    module, _, cls = name.partition(":")
    mod = importlib.import_module(module)
    return getattr(mod, cls or "Method")(calibration, plan, **params)


def run(factory, s, stretches=STRETCHES) -> dict[int, object]:
    """Per second: the method's Out. factory() gives a fresh method per stretch."""
    nodes = None
    outs = {}
    for a, b in stretches:
        method = factory()
        part = s.slice(at(a), at(b))
        for i, t in enumerate(part.t):
            outs[int(t)] = method.step(part.frame(i, nodes), float(t))
        if hasattr(method, "finish"):  # offline methods decide once they have seen the stretch
            outs.update(method.finish())
    return outs


def phantom(factory, s, limit: int = 3600) -> int | None:
    """Seconds someone stays shown after walking into the office (22:12:16 to 22:12:45) when the
    floor then stays as quiet as the empty floor of 22:29 to 22:36:50, looped: how long a walk out
    the method misses leaves a phantom. None: still shown after limit seconds."""
    method = factory()
    walk = s.slice(at("22:12:16"), at("22:12:45"))
    empty = s.slice(at("22:29:00"), at("22:36:50"))
    now = 0.0
    for i in range(len(walk.t)):
        now += 1
        method.step(walk.frame(i), now)
    for k in range(limit):
        now += 1
        if method.step(empty.frame(k % len(empty.t)), now).room is None:
            return k + 1
    return None


def sequence(outs: dict[int, object], a: int, b: int, hold: int = 3) -> list[str]:
    seq, cur, n = [], None, 0
    for t in range(a, b):
        o = outs.get(t)
        r = o.room if o is not None else None
        if r == cur:
            n += 1
        else:
            cur, n = r, 1
        if n == hold and r is not None and (not seq or seq[-1] != r):
            seq.append(r)
    return seq


def edit(a: list, b: list) -> int:
    d = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, d[0] = d[0], i
        for j, y in enumerate(b, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (x != y))
    return d[-1]


def score(outs: dict[int, object], tr: Truth) -> dict:
    m: dict = {}
    conf = defaultdict(Counter)
    per_window = defaultdict(lambda: [0, 0])
    present = correct = missing = 0
    empty_s = fp = fp60 = onsets = 0
    empty60_s = 0
    switches = 0
    walk_fa = stay_n = 0
    room_tp, room_fp, room_fn = Counter(), Counter(), Counter()
    window_start = {w: a for w, (a, _b, _r, _k) in enumerate(windows())}
    prev: dict[int, object] = {}
    for t, (room, w) in sorted(tr.stay.items()):
        o = outs.get(t)
        if o is None:
            continue
        conf[room][o.room] += 1
        stay_n += 1
        walk_fa += bool(o.walking)
        if room is not None:
            present += 1
            correct += o.room == room
            missing += o.room is None
            per_window[w][0] += 1
            per_window[w][1] += o.room == room
        else:
            empty_s += 1
            fp += o.room is not None
            if t - window_start[w] >= SETTLE:
                empty60_s += 1
                fp60 += o.room is not None
            per_window[w][0] += 1
            per_window[w][1] += o.room is None
        p = prev.get(w)
        if p is not None and p[0] == t - 1:
            if room is None:
                onsets += p[1].room is None and o.room is not None
            else:
                switches += p[1].room != o.room
        prev[w] = (t, o)
        shown = set(o.rooms) | ({o.room} if o.room else set())
        for area in shown:
            if area == room:
                room_tp[area] += 1
            else:
                room_fp[area] += 1
        if room is not None and room not in shown:
            room_fn[room] += 1
    present_minutes = present / 60
    m["present_s"], m["empty_s"] = present, empty_s
    m["err"] = ((present - correct) + fp) / (present + empty_s) if present + empty_s else math.nan
    ea, eb = at(EVENING[0]), at(EVENING[1])
    eve = [outs[t].room is not None for t in range(ea, eb) if t in outs]
    m["eve_fp_h"] = sum(eve) / len(eve) * 3600 if eve else math.nan
    m["eve_onsets"] = sum(1 for a, b in zip(eve, eve[1:], strict=False) if b and not a)
    m["acc"] = correct / present if present else math.nan
    m["miss"] = missing / present if present else math.nan
    m["fp_h"] = fp / empty_s * 3600 if empty_s else math.nan
    m["fp60_h"] = fp60 / empty60_s * 3600 if empty60_s else math.nan
    m["onsets_h"] = onsets / empty_s * 3600 if empty_s else math.nan
    m["sw_min"] = switches / present_minutes if present_minutes else math.nan
    m["walk_fa"] = walk_fa / stay_n if stay_n else math.nan
    wk = [o.walking for t, k in tr.walk.items() if (o := outs.get(t)) is not None]
    m["walk_rec"] = sum(wk) / len(wk) if wk else math.nan
    m["conf"] = {str(k): dict(v) for k, v in conf.items()}
    m["per_window"] = {w: (n, c / n if n else math.nan) for w, (n, c) in sorted(per_window.items())}
    m["rooms"] = {a: (room_tp[a], room_fp[a], room_fn[a]) for a in set(room_tp) | set(room_fp) | set(room_fn)}
    # Walks as transitions
    trans = []
    for a, b, r0, r1 in walks():
        reached = None
        for t in range(a, b + 120):
            o = outs.get(t)
            if o is not None and o.room == r1:
                # held 3 s
                if all((q := outs.get(t + d)) is not None and q.room == r1 for d in range(3)):
                    reached = t - a
                    break
        late = [(q := outs.get(b + d)) is not None and q.room == r1 for d in WALK_LATE]
        hall = any((q := outs.get(t)) is not None and (q.room == HALL or HALL in q.rooms) for t in range(a, b + 5))
        trans.append({"walk": f"{SHORT[r0]}>{SHORT[r1]}", "reached_s": reached, "at+10": late[0], "at+30": late[1],
                      "hall": hall})
    m["walks"] = trans
    m["walks_ok"] = sum(x["at+10"] for x in trans)
    m["walks_ok30"] = sum(x["at+30"] for x in trans)
    ra, rb = at(ROUTE[0]), at(ROUTE[1])
    seq = sequence(outs, ra, rb)
    m["route"] = [SHORT[r] for r in seq]
    m["route_edit"] = edit(seq, ROUTE[2])
    return m


def table(results: dict[str, dict]) -> str:
    head = ("method", "err", "acc", "miss", "fp/h", "fp60/h", "onsets/h", "sw/min", "walk rec", "walk fa", "walks@10",
            "walks@30", "route edit", "eve fp/h")
    rows = []
    for name, m in results.items():
        rows.append((name, f"{m['err']:.1%}", f"{m['acc']:.0%}", f"{m['miss']:.0%}", f"{m['fp_h']:.0f}",
                     f"{m['fp60_h']:.0f}",
                     f"{m['onsets_h']:.1f}", f"{m['sw_min']:.2f}", f"{m['walk_rec']:.0%}", f"{m['walk_fa']:.0%}",
                     f"{m['walks_ok']}/4", f"{m['walks_ok30']}/4", f"{m['route_edit']}", f"{m['eve_fp_h']:.0f}"))
    widths = [max(len(str(r[i])) for r in [head, *rows]) for i in range(len(head))]
    lines = ["| " + " | ".join(h.ljust(w) for h, w in zip(head, widths, strict=True)) + " |",
             "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
    lines += ["| " + " | ".join(str(c).ljust(w) for c, w in zip(r, widths, strict=True)) + " |" for r in rows]
    return "\n".join(lines)


def detail(name: str, m: dict) -> str:
    out = [f"## {name}"]
    cols = ["office", "downstairs_bathroom", "kid_s_room", "service_area", HALL, "system", None]
    out.append("truth \\ shown | " + " | ".join(SHORT[c] for c in cols))
    for r in ROOM_CLASSES:
        row = m["conf"].get(str(r), {})
        n = sum(row.values()) or 1
        out.append(f"{SHORT[r]:13s} | " + " | ".join(f"{row.get(c, 0) / n:.0%}" for c in cols) + f"  ({n} s)")
    out.append("per window: " + ", ".join(f"{w}:{acc:.0%}/{n}s" for w, (n, acc) in m["per_window"].items()))
    out.append("sensor view P/R: " + ", ".join(
        f"{SHORT[a]} P={tp / (tp + fp) if tp + fp else math.nan:.0%} R={tp / (tp + fn) if tp + fn else math.nan:.0%}"
        f" (fp {fp} s)" for a, (tp, fp, fn) in sorted(m["rooms"].items())))
    out.append("walks: " + "; ".join(
        f"{w['walk']} reached {w['reached_s']} s, +10 {'Y' if w['at+10'] else 'n'}, +30 {'Y' if w['at+30'] else 'n'},"
        f" hall {'Y' if w['hall'] else 'n'}" for w in m["walks"]))
    route = " > ".join(m["route"])
    out.append(f"route: {route}  (edit {m['route_edit']} from hall > office > hall > util > play > hall)")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("methods", nargs="+", help="baseline, or module[:Class] in this folder")
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--seq", metavar="FROM-TO", help="print each second's output between two times")
    ap.add_argument("--param", action="append", default=[], help="name=value passed to the methods")
    ap.add_argument("--calibration", default="calibration.json", help="calibration file in data/study")
    ap.add_argument("--phantom", action="store_true", help="also how long a missed walk out leaves someone shown")
    args = ap.parse_args()
    params = {}
    for p in args.param:
        k, v = p.split("=", 1)
        try:
            params[k] = json.loads(v)
        except json.JSONDecodeError:
            params[k] = v
    s = seconds()
    cal, plan = load_calibration(args.calibration), load_plan()
    tr = truth()
    results = {}
    outs_all = {}
    for name in args.methods:
        outs = run(lambda name=name: make(name, cal, plan, **params), s)
        outs_all[name] = outs
        results[name] = score(outs, tr)
    print(table(results))
    if args.phantom:
        for name in args.methods:
            if name.endswith("Viterbi"):
                continue
            h = phantom(lambda name=name: make(name, cal, plan, **params), s)
            print(f"{name}: a missed walk out leaves someone shown {'over 1 h' if h is None else f'{h} s'}")
    if args.detail:
        for name, m in results.items():
            print()
            print(detail(name, m))
    if args.seq:
        a, b = (at(x) for x in args.seq.split("-"))
        for t in range(a, b):
            label = tr.stay.get(t)
            lab = SHORT[label[0]] if label else ("walk" if t in tr.walk else "")
            cells = []
            for name in args.methods:
                o = outs_all[name].get(t)
                cells.append("-" if o is None else f"{SHORT[o.room]:6s}{'W' if o.walking else ' '}")
            print(hms(t), f"{lab:6s}", " | ".join(cells))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
