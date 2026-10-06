#!/usr/bin/env python3
"""Which per-second classifier names the room someone walks in best, trained on calibration only.

    .venv-ha/bin/python firmware/tools/study/classifiers.py

Data: the owner's calibration store (data/study/calibration.json), each sample tied to the run it
was recorded in (calib_times.py). Every room was walked twice (about 20:56 and 21:40, office
21:22): train on one session's runs, test on the other's, both ways (leave one session out), so
no test second is in its own training data and 45 minutes of drift lie between them.
Classifiers (numpy here; each is a few lines of pure Python at run time):
  engine      ClassModel.fit: diagonal Gaussian, mean log-likelihood per link (today)
  engine+sig  the same with the signal features, weighted as Rooms does
  knn-k       k nearest samples, Euclidean on log scores
  logreg      multinomial logistic regression on log scores, L2
  lda         shared covariance (shrunk), linear discriminant
  cosine      the room's mean pattern of log scores (unit length) against this second's
  rank        the room whose most disturbed links are this second's most disturbed
Reports accuracy, mean log-loss of the softmax (how honest its confidence is, which a temporal
model needs) and the confusion of the best. Also the empty class against the moving ones: how
many moving seconds the empty class wins, with its samples as stored and with only the runs
recorded on a quiet floor (under 15% of their seconds with confirmed activity: two of the five
empty runs were recorded while someone moved on the floor).
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
import json
import math

import numpy as np

from baseline import active_links
from common import CACHE, SHORT, hms, load_calibration, load_plan, seconds
from custom_components.wisp.engine.rooms import ClassModel, Rooms

ROOMS = ["office", "downstairs_bathroom", "kid_s_room", "service_area", "hallway_ground_floor"]


def dataset():
    cal = load_calibration()
    eng = Rooms()
    eng.load(cal)
    times = json.loads((CACHE / "calib_sample_times.json").read_text())
    runs = json.loads((CACHE / "calib_runs.json").read_text())

    def run_of(t):
        for k, r in enumerate(runs):
            if r["from"] <= t <= r["to"]:
                return k
        return None

    rows = []
    for kind, store in (("moving", eng.areas), ("still", eng.still), ("empty", eng.empty)):
        for name, samples in store.items():
            ts = times[kind][name]
            last = None
            for vec, t in zip(samples, ts, strict=True):
                run = run_of(t) if t is not None else last
                last = run
                rows.append((vec, kind, name, run, t))
    links = sorted({k for vec, *_ in rows for k in vec if len(k) == 2})
    X = np.array([[vec.get(k, np.nan) for k in links] for vec, *_ in rows])
    S = np.array([[vec.get((*k, "rssi"), np.nan) for k in links] for vec, *_ in rows])
    meta = [(kind, name, run, t) for _, kind, name, run, t in rows]
    return rows, links, X, S, meta, runs


def session(meta, runs):
    """0 for each class's first run in time, 1 for later ones."""
    first = {}
    for k, r in enumerate(runs):
        first.setdefault((r["kind"], r["class"]), k)
    return np.array([0 if run == first.get((kind, name)) else 1 for kind, name, run, _ in meta])


def softmax_rows(z):
    z = z - z.max(1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(1, keepdims=True)


class Engine:
    def __init__(self, signal=False):
        self.signal = signal

    def fit(self, vecs, y):
        self.models = {c: ClassModel([v if self.signal else {k: x for k, x in v.items() if len(k) == 2}
                                      for v, yy in zip(vecs, y, strict=True) if yy == c]) for c in sorted(set(y))}
        self.classes = list(self.models)
        return self

    def scores(self, vecs):
        out = []
        for v in vecs:
            v = v if self.signal else {k: x for k, x in v.items() if len(k) == 2}
            out.append([self.models[c].fit(v)[0] for c in self.classes])
        return np.array(out)


class Numeric:
    """Classifiers on the matrix of log scores (missing links: the class-free mean, 0)."""

    def __init__(self, kind, k=7, l2=1.0, shrink=0.3):
        self.kind, self.k, self.l2, self.shrink = kind, k, l2, shrink

    def fit(self, X, y):
        X = np.nan_to_num(X, nan=0.0)
        self.classes = sorted(set(y))
        yi = np.array([self.classes.index(c) for c in y])
        self.X, self.y = X, yi
        C = len(self.classes)
        if self.kind == "logreg":
            mu, sd = X.mean(0), X.std(0) + 1e-6
            self.mu, self.sd = mu, sd
            Z = (X - mu) / sd
            W = np.zeros((Z.shape[1], C))
            b = np.zeros(C)
            Y = np.eye(C)[yi]
            for _ in range(800):
                P = softmax_rows(Z @ W + b)
                G = Z.T @ (P - Y) / len(Z) + self.l2 * W / len(Z)
                W -= 0.5 * G
                b -= 0.5 * (P - Y).mean(0)
            self.W, self.b = W, b
        elif self.kind == "lda":
            self.means = np.array([X[yi == c].mean(0) for c in range(C)])
            R = X - self.means[yi]
            cov = R.T @ R / len(X)
            cov = (1 - self.shrink) * cov + self.shrink * np.diag(np.diag(cov)) + 1e-3 * np.eye(len(cov))
            self.P = np.linalg.inv(cov)
        elif self.kind == "cosine" or self.kind == "rank":
            U = np.maximum(X, 0)
            if self.kind == "cosine":
                U = U / (np.linalg.norm(U, axis=1, keepdims=True) + 1e-6)
                self.T = np.array([U[yi == c].mean(0) for c in range(C)])
                self.T /= np.linalg.norm(self.T, axis=1, keepdims=True) + 1e-9
            else:
                self.T = np.array([X[yi == c].mean(0) for c in range(C)])
        return self

    def scores(self, X):
        X = np.nan_to_num(X, nan=0.0)
        C = len(self.classes)
        if self.kind == "knn":
            d = ((X[:, None, :] - self.X[None, :, :]) ** 2).sum(2)
            nn = np.argsort(d, 1)[:, : self.k]
            votes = np.zeros((len(X), C))
            for c in range(C):
                votes[:, c] = (self.y[nn] == c).sum(1)
            return np.log((votes + 0.5) / (self.k + 0.5 * C))
        if self.kind == "logreg":
            return ((X - self.mu) / self.sd) @ self.W + self.b
        if self.kind == "lda":
            D = X[:, None, :] - self.means[None]
            return -0.5 * np.einsum("ncd,de,nce->nc", D, self.P, D) / X.shape[1]
        if self.kind == "cosine":
            U = np.maximum(X, 0)
            U = U / (np.linalg.norm(U, axis=1, keepdims=True) + 1e-6)
            return 10 * U @ self.T.T
        if self.kind == "rank":
            top = np.argsort(-X, 1)[:, :3]
            out = np.zeros((len(X), C))
            for c in range(C):
                out[:, c] = np.take_along_axis(np.broadcast_to(self.T[c], X.shape), top, 1).sum(1)
            return 3 * out
        raise ValueError(self.kind)


def evaluate(make, data, use_vecs, mask_train, mask_test, classes_of):
    rows, X = data
    yt = [classes_of(i) for i in range(len(rows))]
    tr = np.flatnonzero(mask_train)
    te = np.flatnonzero(mask_test)
    m = make()
    if use_vecs:
        m.fit([rows[i][0] for i in tr], [yt[i] for i in tr])
        sc = m.scores([rows[i][0] for i in te])
    else:
        m.fit(X[tr], [yt[i] for i in tr])
        sc = m.scores(X[te])
    pred = [m.classes[j] for j in sc.argmax(1)]
    truth = [yt[i] for i in te]
    P = softmax_rows(sc)
    ll = -np.mean([math.log(max(P[n, m.classes.index(t)], 1e-6)) if t in m.classes else 0 for n, t in enumerate(truth)])
    return pred, truth, ll


def main() -> int:
    rows, links, X, S, meta, runs = dataset()
    sess = session(meta, runs)
    kind = np.array([m[0] for m in meta])
    name = [m[1] for m in meta]
    print(f"{len(rows)} samples, {len(links)} links; runs per class and session:")
    seen = Counter((m[0], SHORT.get(m[1], m[1]), int(s)) for m, s in zip(meta, sess, strict=True))
    print("  " + ", ".join(f"{k}/{c}/s{s}: {n}" for (k, c, s), n in sorted(seen.items())))
    moving = kind == "moving"
    methods = {
        "engine": (lambda: Engine(False), True),
        "engine+sig": (lambda: Engine(True), True),
        "knn-5": (lambda: Numeric("knn", k=5), False),
        "knn-15": (lambda: Numeric("knn", k=15), False),
        "logreg": (lambda: Numeric("logreg", l2=5.0), False),
        "lda": (lambda: Numeric("lda"), False),
        "cosine": (lambda: Numeric("cosine"), False),
        "rank": (lambda: Numeric("rank"), False),
    }
    print("\nWalking room, 5 rooms, leave one session out (train first runs, test second runs, and back):")
    print(f"{'method':12s} {'acc':>5s} {'logloss':>8s}   per room")
    best = None
    for label, (make, vecs) in methods.items():
        preds, truths, lls = [], [], []
        for test_s in (0, 1):
            p, t, ll = evaluate(make, (rows, X), vecs, moving & (sess != test_s), moving & (sess == test_s),
                                lambda i: name[i])
            preds += p
            truths += t
            lls.append(ll)
        acc = np.mean([a == b for a, b in zip(preds, truths, strict=True)])
        per = {r: np.mean([a == b for a, b in zip(preds, truths, strict=True) if b == r]) for r in ROOMS}
        rooms = " ".join(f"{SHORT[r]} {v:.0%}" for r, v in per.items())
        print(f"{label:12s} {acc:5.0%} {np.mean(lls):8.2f}   {rooms}")
        if best is None or acc > best[0]:
            best = (acc, label, preds, truths)
    acc, label, preds, truths = best
    print(f"\nconfusion of {label} (rows truth):")
    conf = defaultdict(Counter)
    for p, t in zip(preds, truths, strict=True):
        conf[t][p] += 1
    print("        " + " ".join(f"{SHORT[r]:>6s}" for r in ROOMS))
    for t in ROOMS:
        n = sum(conf[t].values())
        print(f"{SHORT[t]:7s} " + " ".join(f"{conf[t][r] / n:6.0%}" for r in ROOMS))

    # The empty class in the moving decision: as stored, and only its quiet runs
    print("\nEmpty class against the walking classes (engine, log scores), moving seconds the empty class wins:")
    s = seconds()
    nodes = set(load_plan()["nodes"])
    busy = {}
    for k, r in enumerate(runs):
        if r["kind"] == "empty":
            seen: deque = deque()
            part = s.slice(r["from"] - 3, r["to"] + 1)
            act = [bool(active_links(part.frame(i), nodes, seen, float(t))) for i, t in enumerate(part.t)][3:]
            busy[k] = sum(act) / max(len(act), 1)
    quiet_runs = {k for k, b in busy.items() if b < 0.15}
    print("  empty runs: " + ", ".join(f"{hms(runs[k]['from'])} active {b:.0%}" for k, b in busy.items()))
    for label, keep in (("as stored", lambda i: True), ("quiet runs only", lambda i: meta[i][2] in quiet_runs)):
        for test_s in (0, 1):
            train = (moving & (sess != test_s)) | ((kind == "empty") & np.array([keep(i) for i in range(len(rows))]))
            p, t, _ = evaluate(lambda: Engine(False), (rows, X), True, train, moving & (sess == test_s),
                               lambda i: name[i] if kind[i] == "moving" else "EMPTY")
            won = np.mean([a == "EMPTY" for a in p])
            acc = np.mean([a == b for a, b in zip(p, t, strict=True)])
            print(f"  {label:16s} test session {test_s}: empty wins {won:.0%} of walking seconds, room right {acc:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
