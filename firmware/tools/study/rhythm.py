#!/usr/bin/env python3
"""Rhythm of the disturbance: does walking (steps at 0.5 to 3 Hz) stand apart from working at a
desk, sitting and an empty floor, in the raw CSI and in the per-second scores Home Assistant has?

    .venv-ha/bin/python firmware/tools/study/rhythm.py

Raw CSI (needs the nodes' raw stream in the recordings): per link, the gain-normalised shape of
the 51 subcarriers the firmware uses, averaged in 0.1 s bins; every second, the last 4 s of it
centred, projected on its first principal direction, detrended and Hann-windowed; its power
spectrum in 0.25 Hz bins gives
  energy   the variance of that projection (how much the link moves)
  gait     the share of the power from 0.5 to 3 Hz (steps, arms) against 0.25 to 5 Hz
per second the mean over the three links with the most energy. Per-second scores (what the
integration has): the mean positive log score over the live links (intensity), and its rise over
the last 3 s against the 3 before (walking builds and moves, fidgeting flickers).
Classes, from the owner's labels and the calibration runs found in the recording: walking (the
labelled walks, the office walking run of 21:22, the walk through the floor at 21:55:48 to
21:58:14), desk with activity (21:23 window: typing, 85% of its seconds active), quiet desk,
sitting in the play room, empty. Prints each feature's 10/50/90th percentiles per class and the
AUC of walking against the desk with activity (the confusion that matters: a desk worker shown
walking in the next room) and against the empty floor.
Needs numpy; caches the binned CSI in data/study/cache.
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import struct

import numpy as np

from common import CACHE, REC, at, files_for, link_name, seconds

HEADER = struct.Struct("<4sBBHI6s6sIbbBBBBBBH")
SHAPE_IDX = np.array(list(range(2, 27)) + list(range(38, 64)))  # as lltf_shape() in the firmware
BIN = 0.1  # s
WIN = 40  # bins: 4 s
CLASSES = {
    "walking": [("21:22:15", "21:23:08"), ("21:55:50", "21:58:12"), ("22:03:41", "22:03:56"), ("22:05:44", "22:06:00"),
                ("22:07:47", "22:08:05"), ("22:12:20", "22:12:35")],
    "desk, active": [("21:23:15", "21:31:00")],
    "desk, quiet": [("21:49:00", "21:55:45"), ("22:01:34", "22:03:38")],
    "sitting": [("22:04:00", "22:05:40")],
    "empty": [("22:08:30", "22:12:15"), ("22:29:00", "22:36:50")],
}
SPANS = [("21:22:00", "21:31:10"), ("21:48:50", "22:13:00"), ("22:28:50", "22:37:00")]


def _scan(path: str, spans: list[tuple[float, float]]) -> dict:
    """Binned shapes per link from one file: {(rx, tx): {bin: [sum shape, count]}}."""
    out: dict = defaultdict(dict)
    data = Path(path).read_bytes()
    pos, n = 0, len(data)
    while pos + REC.size <= n:
        t_us, ln = REC.unpack_from(data, pos)
        pos += REC.size
        p0 = pos
        pos += ln
        if pos > n or ln < HEADER.size or data[p0 + 5] != 1:
            continue
        t = t_us / 1e6
        if not any(a <= t < b for a, b in spans):
            continue
        f = HEADER.unpack_from(data, p0)
        if f[16] < 128:
            continue
        raw = np.frombuffer(data, np.int8, 128, p0 + f[3]).astype(np.float32)
        amp = np.hypot(raw[1::2], raw[0::2])[SHAPE_IDX]
        total = amp.sum()
        if total <= 0:
            continue
        key = (f[5].hex(":"), f[6].hex(":"))
        b = int(t / BIN)
        cell = out[key].get(b)
        if cell is None:
            out[key][b] = [amp / total * len(amp), 1]
        else:
            cell[0] += amp / total * len(amp)
            cell[1] += 1
    return dict(out)


def binned() -> dict:
    path = CACHE / "rhythm-bins.npz"
    if path.exists():
        z = np.load(path, allow_pickle=False)
        keys = json.loads(str(z["keys"]))
        return {tuple(k): (z[f"b{i}"], z[f"s{i}"]) for i, k in enumerate(keys)}
    spans = [(at(a), at(b)) for a, b in SPANS]
    files = files_for(spans[0][0], spans[-1][1])
    with ProcessPoolExecutor(max_workers=8) as pool:
        parts = list(pool.map(_scan, files, [spans] * len(files)))
    merged: dict = defaultdict(dict)
    for part in parts:
        for key, cells in part.items():
            for b, (acc, cnt) in cells.items():
                cell = merged[key].get(b)
                if cell is None:
                    merged[key][b] = [acc, cnt]
                else:
                    cell[0] += acc
                    cell[1] += cnt
    out = {}
    save = {}
    keys = []
    for i, (key, cells) in enumerate(sorted(merged.items())):
        bins = np.array(sorted(cells))
        shapes = np.stack([cells[b][0] / cells[b][1] for b in bins]).astype(np.float32)
        out[key] = (bins, shapes)
        save[f"b{i}"], save[f"s{i}"] = bins, shapes
        keys.append(list(key))
    np.savez_compressed(path, keys=json.dumps(keys), **save)
    return out


def spectral(bins: np.ndarray, shapes: np.ndarray, t: int) -> tuple[float, float] | None:
    """Energy and gait share of one link over the 4 s before second t."""
    b1 = int(t / BIN)
    b0 = b1 - WIN
    i, j = np.searchsorted(bins, b0), np.searchsorted(bins, b1)
    if j - i < WIN * 0.6:
        return None
    grid = np.arange(b0, b1)
    x = shapes[i:j]
    filled = np.stack([np.interp(grid, bins[i:j], x[:, k]) for k in range(x.shape[1])], 1)
    filled -= filled.mean(0)
    _, _, vt = np.linalg.svd(filled, full_matrices=False)
    y = filled @ vt[0]
    y = y - np.polyval(np.polyfit(np.arange(WIN), y, 1), np.arange(WIN))
    energy = float(y.var())
    p = np.abs(np.fft.rfft(y * np.hanning(WIN))) ** 2
    f = np.fft.rfftfreq(WIN, BIN)
    band = (f >= 0.25) & (f <= 5.0)
    gait = (f >= 0.5) & (f <= 3.0)
    total = p[band].sum()
    return energy, float(p[gait].sum() / total) if total > 0 else 0.0


def auc(pos: list[float], neg: list[float]) -> float:
    if not pos or not neg:
        return float("nan")
    a, b = np.array(pos), np.array(neg)
    return float((a[:, None] > b[None, :]).mean() + 0.5 * (a[:, None] == b[None, :]).mean())


def main() -> int:
    links = binned()
    s = seconds()
    logs = np.maximum(np.log(np.maximum(np.nan_to_num(s.score, nan=1.0), 0.1)), 0)
    live = ~np.isnan(s.score)
    intensity = (logs * live).sum(1) / np.maximum(live.sum(1), 1)
    feats: dict[str, dict[str, list[float]]] = {c: defaultdict(list) for c in CLASSES}
    for cls, spans in CLASSES.items():
        for a, b in spans:
            for t in range(at(a), at(b)):
                per = [r for key, (bins, shapes) in links.items() if (r := spectral(bins, shapes, t)) is not None]
                if len(per) < 8:
                    continue
                per.sort(key=lambda r: -r[0])
                top = per[:3]
                feats[cls]["energy (CSI)"].append(float(np.log10(np.mean([e for e, _ in top]) + 1e-9)))
                feats[cls]["gait share (CSI)"].append(float(np.mean([g for _, g in top])))
                i = s.index(t)
                feats[cls]["intensity (scores)"].append(float(intensity[i]))
                feats[cls]["rise (scores)"].append(float(intensity[i - 2:i + 1].mean() - intensity[i - 5:i - 2].mean()))
    names = ["energy (CSI)", "gait share (CSI)", "intensity (scores)", "rise (scores)"]
    print(f"{len(links)} links with raw CSI: " + ", ".join(link_name((tx, rx)) for rx, tx in sorted(links)))
    head = " | ".join(f"{c} (n={len(feats[c][names[0]])})" for c in CLASSES)
    print(f"\n| feature | {head} | AUC walk/desk active | AUC walk/empty |")
    print("|---" * (len(CLASSES) + 3) + "|")
    for name in names:
        cells = []
        for c in CLASSES:
            v = feats[c][name]
            cells.append("/".join(f"{x:.2f}" for x in np.percentile(v, [10, 50, 90])) if v else "-")
        print(f"| {name} | " + " | ".join(cells) +
              f" | {auc(feats['walking'][name], feats['desk, active'][name]):.2f}"
              f" | {auc(feats['walking'][name], feats['empty'][name]):.2f} |")
    # Two features together: a logistic split of walking against the active desk, leave one span out
    print("\nWalking against the active desk (logistic, 2 folds of alternate seconds: optimistic, neighbours alike):")
    for combo in (["intensity (scores)"], ["energy (CSI)"], ["gait share (CSI)"], ["energy (CSI)", "gait share (CSI)"],
                  ["intensity (scores)", "gait share (CSI)"]):
        walk, desk = feats["walking"], feats["desk, active"]
        X = np.array([[walk[n][k] for n in combo] for k in range(len(walk[combo[0]]))] +
                     [[desk[n][k] for n in combo] for k in range(len(desk[combo[0]]))])
        y = np.r_[np.ones(len(walk[combo[0]])), np.zeros(len(desk[combo[0]]))]
        folds = np.arange(len(y)) % 2
        acc = []
        for f in (0, 1):
            tr, te = folds != f, folds == f
            mu, sd = X[tr].mean(0), X[tr].std(0) + 1e-9
            Z = (X - mu) / sd
            w = np.zeros(Z.shape[1])
            b = 0.0
            for _ in range(2000):
                p = 1 / (1 + np.exp(-(Z[tr] @ w + b)))
                w -= 0.1 * Z[tr].T @ (p - y[tr]) / tr.sum()
                b -= 0.1 * (p - y[tr]).mean()
            pred = (Z[te] @ w + b) > 0
            acc.append(((pred == y[te]) * np.where(y[te] == 1, 0.5 / y[te].mean(), 0.5 / (1 - y[te].mean()))).mean())
        print(f"  {' + '.join(combo):40s} balanced accuracy {np.mean(acc):.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
