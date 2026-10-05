#!/usr/bin/env python3
"""Layout solver study: the firmware's solve_layout() (core_layout.h) against other candidates,
on simulated homes and on the owner's live rows.

    .venv/bin/python firmware/tools/layout_study.py [--trials 300] [--seed 1] [--world homes|all]
                                                    [--only "name,name"]

Homes: floors of 8x8 to 14x12 m cut into a grid of rooms (a quarter of the wall stretches left
open), 3 to 8 nodes near walls and corners. RSSI = P - 10 n log10(d) - wall_loss * walls crossed
+ offset_i + offset_j + noise, P -50 to -40 dBm, n 2 to 3.5, wall loss 3 to 7 dB, per-node
offsets +-4 dB, noise sd 1.5 dB per direction, whole dB, nothing below -92 dBm. Other worlds:
open plan (no walls), concrete (walls 6 to 10 dB) and matched boards (offsets +-1 dB).

Scores: Procrustes error (RMS after the best similarity fit, mirror allowed, relative to the true
spread), the share of near-collinear layouts (second singular value under 10% of the first while
the true layout is not) and the scale (layout metres per true metre). numpy only, float64, so the
last bits differ from the firmware's float32 (the live layout matches to the 10 cm rounding).
"""

from __future__ import annotations

import argparse
import math

import numpy as np

LIVE_ROWS = {  # receiver: {transmitter: RSSI}, 2026-10-05, the access point left out
    "44:1b:f6:8d:58:58": {"58:cf:79:ee:90:58": -71, "ac:27:6e:a8:c7:7c": -42, "e0:72:a1:d7:14:30": -51},
    "58:cf:79:ee:90:58": {"44:1b:f6:8d:58:58": -70, "ac:27:6e:a8:c7:7c": -71, "e0:72:a1:d7:14:30": -80},
    "e0:72:a1:d7:14:30": {"44:1b:f6:8d:58:58": -52, "ac:27:6e:a8:c7:7c": -63, "58:cf:79:ee:90:58": -82},
    "ac:27:6e:a8:c7:7c": {"44:1b:f6:8d:58:58": -43, "58:cf:79:ee:90:58": -74, "e0:72:a1:d7:14:30": -64},
}

WORLDS = {
    "homes": dict(exp=(2.0, 3.5), wall=(3.0, 7.0), offset=4.0),
    "open": dict(exp=(2.0, 3.0), wall=(0.0, 0.0), offset=4.0),
    "concrete": dict(exp=(2.5, 3.5), wall=(6.0, 10.0), offset=4.0),
    "matched": dict(exp=(2.0, 3.5), wall=(3.0, 7.0), offset=1.0),
}

# ---- Simulated homes --------------------------------------------------------------------------


def cuts(length, rng):
    """Room boundaries along one side: rooms 2.5 to 5 m."""
    pos = [0.0]
    while length - pos[-1] > 5.0:
        step = rng.uniform(2.5, 5.0)
        if length - (pos[-1] + step) < 2.5:
            break
        pos.append(pos[-1] + step)
    pos.append(length)
    return np.array(pos)


class Home:
    def __init__(self, rng):
        self.w, self.h = rng.uniform(8, 14), rng.uniform(8, 12)
        self.xs, self.ys = cuts(self.w, rng), cuts(self.h, rng)
        # Wall stretches between rooms; a quarter are open (doorways, open plan).
        self.vwall = rng.random((len(self.xs) - 2, len(self.ys) - 1)) < 0.75
        self.hwall = rng.random((len(self.ys) - 2, len(self.xs) - 1)) < 0.75

    def walls(self, p, q):
        count = 0
        for k, c in enumerate(self.xs[1:-1]):
            if (p[0] - c) * (q[0] - c) < 0:
                y = p[1] + (c - p[0]) / (q[0] - p[0]) * (q[1] - p[1])
                cell = min(max(np.searchsorted(self.ys, y) - 1, 0), len(self.ys) - 2)
                count += self.vwall[k][cell]
        for k, c in enumerate(self.ys[1:-1]):
            if (p[1] - c) * (q[1] - c) < 0:
                x = p[0] + (c - p[1]) / (q[1] - p[1]) * (q[0] - p[0])
                cell = min(max(np.searchsorted(self.xs, x) - 1, 0), len(self.xs) - 2)
                count += self.hwall[k][cell]
        return count

    def place(self, n, rng):
        """n nodes near walls and corners, one per room while rooms last, at least 1 m apart."""
        rooms = [(i, j) for i in range(len(self.xs) - 1) for j in range(len(self.ys) - 1)]
        for _ in range(200):
            pick = rng.choice(len(rooms), n, replace=n > len(rooms))
            pts = []
            for r in pick:
                i, j = rooms[r]
                x0, x1, y0, y1 = self.xs[i], self.xs[i + 1], self.ys[j], self.ys[j + 1]
                m1, m2 = rng.uniform(0.1, 0.6, 2)
                if rng.random() < 0.4:  # a corner
                    x = x0 + m1 if rng.random() < 0.5 else x1 - m1
                    y = y0 + m2 if rng.random() < 0.5 else y1 - m2
                else:  # along one wall
                    side = rng.integers(4)
                    if side < 2:
                        x = x0 + m1 if side == 0 else x1 - m1
                        y = rng.uniform(y0 + 0.3, y1 - 0.3)
                    else:
                        y = y0 + m1 if side == 2 else y1 - m1
                        x = rng.uniform(x0 + 0.3, x1 - 0.3)
                pts.append((x, y))
            pts = np.array(pts)
            gaps = np.linalg.norm(pts[:, None] - pts[None, :], axis=2) + np.eye(n) * 99
            if gaps.min() >= 1.0:
                return pts
        return pts


def simulate(n, rng, world):
    """True positions and rssi[i][j], what node i hears from node j (nan: not heard)."""
    home = Home(rng)
    pos = home.place(n, rng)
    p0 = rng.uniform(-50, -40)
    exp = rng.uniform(*world["exp"])
    wall_db = rng.uniform(*world["wall"])
    offset = rng.uniform(-world["offset"], world["offset"], n)
    rssi = np.full((n, n), np.nan)
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            d = max(0.5, float(np.linalg.norm(pos[i] - pos[j])))
            v = p0 - 10 * exp * math.log10(d) - wall_db * home.walls(pos[i], pos[j])
            v = round(v + offset[i] + offset[j] + rng.normal(0, 1.5))
            if v >= -92:
                rssi[i][j] = v
    return pos, rssi


# ---- Firmware port, before this study (one exponent, 2.7) -------------------------------------


def linked_nodes(rssi):
    n = len(rssi)
    heard = ~np.isnan(rssi)
    return [i for i in range(n) if any(heard[i, j] or heard[j, i] for j in range(n) if j != i)]


def pair_rssi(rssi, keep):
    """Both directions averaged; nan where nobody heard the other."""
    a = rssi[np.ix_(keep, keep)]
    b = a.T
    out = np.where(np.isnan(a), b, np.where(np.isnan(b), a, 0.5 * (a + b)))
    np.fill_diagonal(out, np.nan)
    return out


def to_metres(pr, exponent, at_1m=-45.0):
    return np.clip(10 ** ((at_1m - pr) / (10 * exponent)), 0.3, 40.0)


def fill(d):
    """Shortest paths for unmeasured pairs. Returns distances, weights (measured 1, filled 0.2)
    and the measured mask."""
    n = len(d)
    measured = ~np.isnan(d)
    np.fill_diagonal(measured, False)
    sp = np.where(measured, d, np.inf)
    np.fill_diagonal(sp, 0.0)
    for k in range(n):
        sp = np.minimum(sp, sp[:, k : k + 1] + sp[k : k + 1, :])
    largest = np.max(d[measured]) if measured.any() else 1.0
    out = np.where(measured, d, np.where(np.isinf(sp), 1.5 * max(largest, 1.0), sp))
    np.fill_diagonal(out, 0.0)
    w = np.where(measured, 1.0, 0.2)
    np.fill_diagonal(w, 0.0)
    return out, w, measured


def cmds(d, its=300):
    """Classical MDS by shifted power iteration, as the firmware does it."""
    n = len(d)
    d2 = d * d
    row = d2.mean(1)
    b = -0.5 * (d2 - row[:, None] - row[None, :] + row.mean())
    xy = np.zeros((n, 2))
    idx = np.arange(n)
    for axis in range(2):
        shift = np.abs(b).sum(1).max()
        v = 1.0 + 0.37 * idx + 0.11 * axis * (idx % 3)
        for _ in range(its):
            nv = b @ v + shift * v
            norm = np.linalg.norm(nv)
            if norm < 1e-9:
                break
            v = nv / norm
        lam = v @ (b @ v)
        scale = math.sqrt(max(lam, 0.0))
        xy[:, axis] = v * scale
        if scale < 1e-3:
            xy[:, axis] = 0.05 * ((idx * 7) % 5 - 2)
        b = b - lam * np.outer(v, v)
    return xy


def guttman(xy, target, w):
    """One step of the firmware's per-point weighted Guttman update."""
    diff = xy[:, None, :] - xy[None, :, :]
    dist = np.maximum(np.sqrt((diff**2).sum(-1)), 1e-4)
    ratio = target / dist
    num = (w[..., None] * (xy[None, :, :] + ratio[..., None] * diff)).sum(1)
    ws = w.sum(1)
    return np.where(ws[:, None] > 0, num / np.where(ws > 0, ws, 1)[:, None], 0.0)


def smacof(xy, d, w, its):
    for _ in range(its):
        xy = guttman(xy, d, w)
    return xy


def dists(xy):
    return np.linalg.norm(xy[:, None] - xy[None, :], axis=2)


def solve_current(pr, exponent=2.7, its=40):
    d, w, _ = fill(to_metres(pr, exponent))
    return smacof(cmds(d), d, w, its)


# ---- Candidates -------------------------------------------------------------------------------


def relative(pr, exponent):
    """Targets and relative weights (1/d^2) for one exponent."""
    d, w, measured = fill(to_metres(pr, exponent))
    return d, np.where(w > 0, w / (d * d + np.eye(len(d))), 0.0), measured


def solve_relative(pr, exponent=4.0, its=40):
    d, w, _ = relative(pr, exponent)
    return smacof(cmds(d), d, w, its)


def misfit_db2(xy, d, measured, exponent):
    """Mean square of 10 n log10(layout distance / target) over measured pairs, best scale; the
    log from 2 (r - 1) / (r + 1) as in the firmware."""
    r = dists(xy)[measured] / d[measured]
    s = r.sum() / (r * r).sum()
    db = 8.6858896 * exponent * (s * r - 1) / (s * r + 1)
    return float((db * db).mean())


def solve_select(pr, exponents=(2.7, 3.3, 4.0, 4.8, 5.8), its=40):
    """Exponent per hive, each from its own classical MDS start: the least dB misfit."""
    best = None
    for e in exponents:
        d, w, measured = relative(pr, e)
        xy = smacof(cmds(d), d, w, its)
        m = misfit_db2(xy, d, measured, e)
        if best is None or m < best[0]:
            best = (m, xy)
    return best[1]


def pav(y, w):
    """Weighted pool-adjacent-violators: the closest non-decreasing sequence."""
    vals, wts, sizes = [], [], []
    for yi, wi in zip(y, w):
        vals.append(yi)
        wts.append(wi)
        sizes.append(1)
        while len(vals) > 1 and vals[-2] > vals[-1]:
            v2, w2, s2 = vals.pop(), wts.pop(), sizes.pop()
            tw = wts[-1] + w2
            vals[-1] = (vals[-1] * wts[-1] + v2 * w2) / tw
            wts[-1] = tw
            sizes[-1] += s2
    out = []
    for v, s in zip(vals, sizes):
        out.extend([v] * s)
    return np.array(out)


def solve_nmds(pr, start_exponent=2.7, its=60):
    """Kruskal's non-metric SMACOF from the classical MDS start: disparities by monotone
    regression on the RSSI order (equal RSSI kept equal), scaled to a fixed size."""
    d, w, _ = fill(to_metres(pr, start_exponent))
    xy = cmds(d)
    n = len(d)
    iu = sorted(((i, j) for i in range(n) for j in range(i + 1, n)), key=lambda p: (d[p], p))
    ks = np.array([d[p] for p in iu])
    wv = np.array([w[p] for p in iu])
    blocks, s = [], 0
    for k in range(1, len(iu) + 1):
        if k == len(iu) or ks[k] != ks[s]:
            blocks.append((s, k))
            s = k
    target_ss = (wv * ks * ks).sum()
    for _ in range(its):
        dx = dists(xy)
        dv = np.array([dx[p] for p in iu])
        fit = pav([(wv[a:b] * dv[a:b]).sum() / wv[a:b].sum() for a, b in blocks], [wv[a:b].sum() for a, b in blocks])
        dhat = np.empty(len(iu))
        for (a, b), f in zip(blocks, fit):
            dhat[a:b] = f
        dhat *= math.sqrt(target_ss / max((wv * dhat * dhat).sum(), 1e-12))
        t = np.zeros((n, n))
        for (i, j), v in zip(iu, dhat):
            t[i][j] = t[j][i] = v
        xy = guttman(xy, t, w)
    return xy


def solve_logfit(pr, its=60, start_exponent=4.0):
    """Exponent fitted continuously: alternate a regression of log(layout distance) on the RSSI
    with a relative-stress step (needs a portable log on the nodes)."""
    d0, w, _ = fill(to_metres(pr, 2.7))
    t = np.log(np.where(d0 > 0, d0, 1.0))  # linear in RSSI
    xy = cmds(fill(to_metres(pr, start_exponent))[0])
    mask = w > 0
    for _ in range(its):
        ll = np.log(np.maximum(dists(xy), 1e-4))[mask]
        ww, tt = w[mask], t[mask]
        tm, lm = (ww * tt).sum() / ww.sum(), (ww * ll).sum() / ww.sum()
        b = (ww * (tt - tm) * (ll - lm)).sum() / max((ww * (tt - tm) ** 2).sum(), 1e-12)
        b = min(max(b, 0.35), 1.0)
        target = np.exp(lm + b * (t - tm))
        np.fill_diagonal(target, 0)
        xy = guttman(xy, target, np.where(mask, w / np.maximum(target, 1e-3) ** 2, 0.0))
    return xy


STAGE_FACTOR = (1.953125, 1.5625, 1.25, 1.0, 0.8, 0.64)


def solve_chosen(pr, nominal=4.0, steps=10, final=15, tolerance=0.4, info=None):
    """core_layout.h now: six exponents, squeezed first, each continuing from the last layout;
    of the fits within the tolerance (dB^2) of the best, the one nearest the nominal exponent;
    then scaled to the nominal exponent's metres."""
    xy = None
    stages = []
    for f in STAGE_FACTOR:
        d, w, measured = relative(pr, nominal * f)
        if xy is None:
            xy = cmds(d, its=100)
        xy = smacof(xy, d, w, steps)
        stages.append((misfit_db2(xy, d, measured, nominal * f), f, xy))
    best = min(m for m, _, _ in stages)
    _, f, xy = min((s for s in stages if s[0] <= best + tolerance), key=lambda s: abs(s[1] - 1.0))
    d, w, measured = relative(pr, nominal * f)
    xy = smacof(xy, d, w, final)
    r = dists(xy)[measured] / to_metres(pr, nominal)[measured]
    if info is not None:
        info.append(nominal * f)
    return xy * (r.sum() / (r * r).sum())


SOLVERS = {
    "current (n=2.7)": solve_current,
    "fixed n=4": lambda pr: solve_current(pr, 4.0),
    "relative n=4": solve_relative,
    "select n per hive": solve_select,
    "nmds": solve_nmds,
    "nmds, start n=4": lambda pr: solve_nmds(pr, 4.0),
    "logfit": solve_logfit,
    "chosen": solve_chosen,
}


# ---- Scores -----------------------------------------------------------------------------------


def procrustes(out, truth):
    """RMS error after the best similarity fit (mirror allowed), relative to the true spread,
    and the scale (layout units per true metre)."""
    x = out - out.mean(0)
    y = truth - truth.mean(0)
    sx, sy = (x * x).sum(), (y * y).sum()
    if sx < 1e-12:
        return 1.0, 0.0
    s = np.linalg.svd(x.T @ y, compute_uv=False).sum()
    return math.sqrt(max(sy - s * s / sx, 0.0) / sy), sx / s


def flatness(xy):
    s = np.linalg.svd(xy - xy.mean(0), compute_uv=False)
    return s[1] / s[0] if s[0] > 1e-9 else 0.0


def rounded(xy):
    return np.round(xy * 10) / 10


def run(world, trials, seed, names):
    sizes = range(3, 9)
    res = {name: {n: ([], [], []) for n in sizes} for name in names}
    for n in sizes:
        rng = np.random.default_rng(seed * 1000 + n)
        for _ in range(trials):
            pos, rssi = simulate(n, rng, WORLDS[world])
            keep = linked_nodes(rssi)
            if len(keep) < 3:
                continue
            truth = pos[keep]
            flat_truth = flatness(truth) < 0.1
            pr = pair_rssi(rssi, keep)
            for name in names:
                xy = rounded(SOLVERS[name](pr))
                err, scale = procrustes(xy, truth)
                res[name][n][0].append(err)
                res[name][n][2].append(scale)
                if not flat_truth:
                    res[name][n][1].append(flatness(xy) < 0.1)
    print(f"\n{world}: {trials} homes per node count, median Procrustes error / share on a line")
    print(f"{'solver':20s}" + "".join(f"{f'{n} nodes':>14s}" for n in sizes) + f"{'all':>14s}  scale")
    for name in names:
        line = f"{name:20s}"
        errs, flats, scales = [], [], []
        for n in sizes:
            e, f, s = res[name][n]
            errs, flats, scales = errs + e, flats + f, scales + s
            line += f"  {np.median(e):.3f} {100 * np.mean(f):4.1f}%"
        line += f"  {np.median(errs):.3f} {100 * np.mean(flats):4.1f}%  {np.median(scales):.2f}"
        print(line)


def live(names):
    macs = sorted(LIVE_ROWS)
    n = len(macs)
    rssi = np.full((n, n), np.nan)
    for i, rx in enumerate(macs):
        for j, tx in enumerate(macs):
            if tx in LIVE_ROWS[rx]:
                rssi[i][j] = LIVE_ROWS[rx][tx]
    keep = linked_nodes(rssi)
    pr = pair_rssi(rssi, keep)
    print("live rows (centred, not posed): " + ", ".join(m[-5:].replace(":", "") for m in macs))
    for name in names:
        xy = rounded(SOLVERS[name](pr))
        xy = xy - xy.mean(0)
        pts = "  ".join(f"({x:+5.1f},{y:+5.1f})" for x, y in xy)
        print(f"{name:20s} flatness {flatness(xy):.2f}  {pts}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trials", type=int, default=300)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--world", default="homes", help="homes, open, concrete, matched or all")
    ap.add_argument("--only", help="comma separated solver names")
    args = ap.parse_args()
    names = args.only.split(",") if args.only else list(SOLVERS)
    live(names)
    for world in WORLDS if args.world == "all" else [args.world]:
        run(world, args.trials, args.seed, names)


if __name__ == "__main__":
    main()
