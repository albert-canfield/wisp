#!/usr/bin/env python3
"""When each calibration sample was recorded: its log scores matched against the recordings.

    .venv-ha/bin/python firmware/tools/study/calib_times.py

A sample is the log score of every live link (3 decimals) at one second of a calibration run.
A recorded second within 0.002 on all shared links but one, each link taken at that second or one
either side (HA ticks at another phase), is a match; runs of matched seconds give
the calibration runs (class, from, to), written to data/study/cache/calib_runs.json (and each
sample's time to calib_sample_times.json). The study
uses them to keep calibration seconds out of the test, and as labelled training data (where the
owner walked or sat during each run) for features the calibration store does not keep.
"""
from __future__ import annotations

import json
import numpy as np

from common import CACHE, hms, load_calibration, seconds


def main() -> int:
    s = seconds()
    cal = load_calibration()
    col = {k: j for j, k in enumerate(s.keys)}
    logs = np.log(np.maximum(np.nan_to_num(s.score, nan=-1.0), 0.1))
    logs[np.isnan(s.score)] = np.nan
    runs = []
    sample_times: dict[str, dict[str, list]] = {}
    for kind, classes in (("moving", cal["areas"]), ("still", cal["still"]), ("empty", cal["empty"])):
        for name, packed in classes.items():
            links = [tuple(x.split(">")) for x in packed["links"]]
            cols = [col.get(k) for k in links]
            found = []
            for row in packed["samples"]:
                use = [(c, v) for c, v in zip(cols, row, strict=True) if c is not None and v is not None]
                if not use:
                    found.append(None)
                    continue
                c_idx = np.array([c for c, _ in use])
                vals = np.array([v for _, v in use])
                d = np.abs(logs[:, c_idx] - vals) <= 0.002  # this second, or one either side (tick phase)
                d[1:] |= np.abs(logs[:-1, c_idx] - vals) <= 0.002
                d[:-1] |= np.abs(logs[1:, c_idx] - vals) <= 0.002
                ok = d.sum(axis=1) >= max(1, len(use) - 1)
                hits = np.flatnonzero(ok)
                found.append(int(s.t[hits[0]]) if len(hits) == 1 else (int(s.t[hits[0]]) if len(hits) else None))
            sample_times.setdefault(kind, {})[name] = found
            times = sorted(t for t in found if t is not None)
            missing = sum(t is None for t in found)
            # group into runs: gaps over 20 s split
            groups = []
            for t in times:
                if groups and t - groups[-1][1] <= 20:
                    groups[-1][1] = t
                    groups[-1][2] += 1
                else:
                    groups.append([t, t, 1])
            for a, b, n in groups:
                runs.append({"kind": kind, "class": name, "from": a, "to": b, "samples": n})
            print(f"{kind:6s} {name:22s} {len(found):4d} samples, {missing:3d} unmatched: "
                  + ", ".join(f"{hms(a)}-{hms(b)} ({n})" for a, b, n in groups))
    runs.sort(key=lambda r: r["from"])
    (CACHE / "calib_runs.json").write_text(json.dumps(runs, indent=1))
    (CACHE / "calib_sample_times.json").write_text(json.dumps(sample_times))
    print("\nRuns in time order:")
    for r in runs:
        print(f"  {hms(r['from'])}-{hms(r['to'])}  {r['kind']:6s} {r['class']:22s} {r['samples']:3d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
