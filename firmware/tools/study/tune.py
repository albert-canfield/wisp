#!/usr/bin/env python3
"""Tune the room tracker's few parameters without fitting the labelled minutes: a grid, scored
leave one window out, over every calibration snapshot of the owner's.

    .venv-ha/bin/python firmware/tools/study/tune.py [--fix '{"exits": []}'] [--sensitivity]

The class fits per second (the expensive part) are computed once per calibration; each parameter
set then replays the forward pass from 21:05 to 23:45. Objective: the share of labelled stay
seconds the tracker gets wrong (a wrong room, nobody while present, someone on the empty floor),
summed over the four calibration snapshots the owner's Home Assistant saved that evening (20:21,
20:29, 21:28, 21:50: the same test seconds, other training data), so a parameter set has to work
with a thin calibration as well as with a full one. Leave one window out: for each of the eight
stay windows, the best parameter set on the other seven, then its errors on the window left out;
summed over the windows they are the honest estimate. Also prints the in-sample best and the
defaults (tracker.Params) per calibration, with how long a missed walk out leaves someone shown.
--sensitivity: each default halved and doubled alone, on the latest calibration and on all four.
"""
from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import ProcessPoolExecutor
import itertools
import json
import pickle
import time

from baseline import Out, active_links
from common import CACHE, at, load_calibration, load_plan, seconds, windows
from evaluate import STRETCHES, score, truth
import hmm
import tracker as T

CALIBRATIONS = ["calibration.json", "calibration-old3.json", "calibration-old2.json", "calibration-old.json"]
GRID = {
    "hall_only": [False, True],
    "exits": [(), ("downstairs_bathroom",)],
    "fit_weight": [1.0, 1.5, 2.0, 3.0],
    "p_busy": [0.3, 0.5, 0.7],
    "start_walk": [0.005, 0.01, 0.02],
    "stir": [0.005, 0.01, 0.02],
    "walk_leave": [0.1, 0.2],
}
DEFAULT = {"hall_only": True, "exits": (), "fit_weight": 1.0, "p_busy": 0.7, "start_walk": 0.005, "stir": 0.005,
           "walk_leave": 0.2}  # as tracker.Params and RoomTracker
_DATA: dict = {}
_CALS: dict = {}


def calibration(name: str) -> dict:
    if name not in _CALS:
        _CALS[name] = load_calibration(name)
    return _CALS[name]


def precompute(cal: str):
    """Per second of the stretches: (time, class fits, active) under one calibration."""
    if cal in _DATA:
        return _DATA[cal]
    path = CACHE / f"fits-{cal}.pkl"
    if path.exists():
        _DATA[cal] = pickle.loads(path.read_bytes())
        return _DATA[cal]
    plan = load_plan()
    tr = hmm.build(calibration(cal), plan)
    s = seconds()
    data = []
    for a, b in STRETCHES:
        part = s.slice(at(a), at(b))
        seen: deque = deque()
        rows = []
        for i, t in enumerate(part.t):
            f = part.frame(i)
            active = bool(active_links(f, set(plan["nodes"]), seen, float(t)))
            rows.append((int(t), tr.fits(f["scores"]), active))
        data.append(rows)
    path.write_bytes(pickle.dumps(data))
    _DATA[cal] = data
    return data


def simulate(params: dict, cal: str = CALIBRATIONS[0]) -> dict[int, Out]:
    tracker = hmm.build(calibration(cal), load_plan(), **params)
    outs = {}
    for rows in precompute(cal):
        tracker.reset()
        for t, fits, active in rows:
            e = tracker.step_fits(fits, float(t), active)
            outs[t] = Out(e.room, frozenset({e.room} if e.room else ()), e.walking)
    return outs


def window_errors(outs, tr) -> dict[int, tuple[int, int]]:
    out: dict[int, list[int]] = {}
    for t, (room, w) in tr.stay.items():
        if (o := outs.get(t)) is not None:
            e = out.setdefault(w, [0, 0])
            e[0] += 1
            e[1] += o.room != room
    return {w: (n, k) for w, (n, k) in out.items()}


def evaluate_params(params: dict):
    tr = truth()
    per_cal = {}
    for cal in CALIBRATIONS:
        outs = simulate(params, cal)
        m = score(outs, tr)
        per_cal[cal] = (window_errors(outs, tr), {k: m[k] for k in ("err", "acc", "fp_h", "eve_fp_h", "walks_ok")})
    return params, per_cal


def expand(combo: dict) -> dict:
    p = dict(combo)
    if "p_busy" in p:
        p["p_active"] = (0.90, p.pop("p_busy"), 0.003, 0.003)
    return p


def hold(params: dict, cal: str = CALIBRATIONS[0], limit: int = 3600) -> int | None:
    """Seconds someone stays shown after walking into the office (22:12:16 to 22:12:45) when the
    floor then stays as quiet as the empty floor of 22:29 to 22:36:50, looped: how long a missed
    walk out leaves a phantom. None: still shown after limit seconds."""
    rows = [r for part in precompute(cal) for r in part]
    walk = [r for r in rows if at("22:12:16") <= r[0] < at("22:12:45")]
    empty = [r for r in rows if at("22:29:00") <= r[0] < at("22:36:50")]
    tracker = hmm.build(calibration(cal), load_plan(), **params)
    now = 0.0
    for _, fits, active in walk:
        now += 1
        tracker.step_fits(fits, now, active)
    for k in range(limit):
        _, fits, active = empty[k % len(empty)]
        now += 1
        if tracker.step_fits(fits, now, active).room is None:
            return k + 1
    return None


def errors(per_cal: dict, skip: int | None = None, cals=CALIBRATIONS) -> float:
    n = k = 0
    for cal in cals:
        for w, (nn, kk) in per_cal[cal][0].items():
            if w != skip:
                n += nn
                k += kk
    return k / n if n else 1.0


def sensitivity(exits=()) -> None:
    """Each default halved and doubled alone: how flat the error is around them."""
    base = dict(DEFAULT, exits=exits)
    rows = [("defaults", base)]
    for k in ("fit_weight", "start_walk", "stir", "calm", "walk_leave", "walk_stop", "enter", "quiet_hold"):
        v = base.get(k, getattr(T.Params(), k))
        rows += [(f"{k} x0.5", dict(base, **{k: v * 0.5})), (f"{k} x2", dict(base, **{k: v * 2}))]
    rows += [("p_busy 0.5", dict(base, p_busy=0.5)), ("p_busy 0.9", dict(base, p_busy=0.9)),
             ("p_empty x0.3", dict(base, p_active=(0.9, base["p_busy"], 0.001, 0.001))),
             ("p_empty x3", dict(base, p_active=(0.9, base["p_busy"], 0.009, 0.009))),
             ("touching rooms adjacent", dict(base, hall_only=False))]
    print(f"| change (exits {list(exits)}) | err, latest calibration | err, 4 calibrations | fp/h | eve fp/h |"
          " walks@10 | missed walk out shown |")
    print("|---|---|---|---|---|---|---|")
    for label, params in rows:
        params = expand(params)
        _, per_cal = evaluate_params(params)
        m = per_cal[CALIBRATIONS[0]][1]
        h = hold(params)
        print(f"| {label} | {m['err']:.1%} | {errors(per_cal):.1%} | {m['fp_h']:.0f} | {m['eve_fp_h']:.0f} |"
              f" {m['walks_ok']}/4 | {'over 1 h' if h is None else f'{h} s'} |")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fix", help='JSON of grid values to hold fixed, e.g. {"exits": []}')
    ap.add_argument("--sensitivity", action="store_true", help="only the one-at-a-time table around the defaults")
    args = ap.parse_args()
    for cal in CALIBRATIONS:
        precompute(cal)
    if args.sensitivity:
        sensitivity(())
        print()
        sensitivity(("downstairs_bathroom",))
        return 0
    combos = [dict(zip(GRID, values, strict=True)) for values in itertools.product(*GRID.values())]
    if args.fix:
        fixed = json.loads(args.fix)
        fixed = {k: tuple(v) if isinstance(v, list) else v for k, v in fixed.items()}
        combos = [c for c in combos if all(c.get(k) == v for k, v in fixed.items())]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(evaluate_params, [expand(c) for c in combos], chunksize=4))
    print(f"{len(results)} parameter sets x {len(CALIBRATIONS)} calibrations in {time.time() - t0:.0f} s")
    ranked = sorted(results, key=lambda r: errors(r[1]))
    print("\nin-sample best 5 (error over all stay seconds and calibrations; then per calibration):")
    for params, per_cal in ranked[:5]:
        each = " ".join(f"{per_cal[c][1]['err']:.3f}" for c in CALIBRATIONS)
        print(f"  {errors(per_cal):.3f}  {each}  {params}")
    print("\nleave one window out (the best on the other windows, summed over the calibrations):")
    held = {c: [0, 0] for c in CALIBRATIONS}
    chosen = []
    for w in range(len(windows())):
        best = min(results, key=lambda r: errors(r[1], skip=w))
        chosen.append(best[0])
        cells = []
        for c in CALIBRATIONS:
            n, k = best[1][c][0].get(w, (0, 0))
            held[c][0] += n
            held[c][1] += k
            cells.append(f"{k}/{n}")
        print(f"  window {w}: held-out errors {' '.join(cells)}   with {best[0]}")
    print("  held-out error per calibration: " + ", ".join(f"{c} {k / n:.3f}" for c, (n, k) in held.items()))
    print(f"  overall {sum(k for n, k in held.values()) / sum(n for n, k in held.values()):.3f}")
    print("\nvalues chosen per fold: " + json.dumps(
        {k: sorted({str(c.get(k)) for c in chosen}) for k in [*GRID, "p_active"] if k != "p_busy"}))
    default = expand(DEFAULT)
    for params, per_cal in results:
        if params == default:
            each = ", ".join(f"{c} {per_cal[c][1]['err']:.3f}" for c in CALIBRATIONS)
            print(f"\ndefaults: error {errors(per_cal):.3f} over the calibrations; {each};"
                  f" a missed walk out is shown {hold(params)} s")
    (CACHE / "tune_results.pkl").write_bytes(pickle.dumps(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
