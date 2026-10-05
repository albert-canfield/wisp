"""Where on a floor the disturbance is, from the links that see it.

Two tools. Locator finds one moving person: for every pixel it predicts how much each link would
be disturbed by someone standing there (falling off with the distance to the link's line), and
picks the pixel whose prediction fits the observed links best, with the best scale. Links that
did not react count as much as links that did, so it is not fooled by a single long link. On
synthetic floors it is several times more accurate than imaging (median about 0.3 m against
1.2 m, see tests/test_imaging.py).

Imager is classic radio tomographic imaging, kept for heat maps:

each link (transmitter and receiver at known positions) sees a person who stands close to the
straight line between them. Pixels inside a thin ellipse around that line get a weight
(1 / sqrt(link length), the usual model, Wilson and Patwari 2010). With W the links x pixels
weight matrix and y the links' disturbances, the image is the regularised least squares

    image = W^T (W W^T + alpha I)^-1 y

which is the same as (W^T W + alpha I)^-1 W^T y but needs only a links x links solve, so it runs
in plain Python. The geometry part is computed once per layout; each image is a cheap product.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass, field
import math

Point = tuple[float, float]
LinkKey = tuple[Hashable, Hashable]  # (transmitter, receiver)


def _solve_inverse(a: list[list[float]]) -> list[list[float]]:
    """Inverse of a small symmetric positive definite matrix (Gauss-Jordan with pivoting)."""
    n = len(a)
    m = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[pivot] = m[pivot], m[col]
        p = m[col][col]
        if abs(p) < 1e-12:
            raise ValueError("singular matrix")
        inv_p = 1.0 / p
        row = m[col]
        for j in range(2 * n):
            row[j] *= inv_p
        for r in range(n):
            if r != col and m[r][col] != 0.0:
                f = m[r][col]
                other = m[r]
                for j in range(2 * n):
                    other[j] -= f * row[j]
    return [row[n:] for row in m]


@dataclass
class Location:
    x: float
    y: float
    strength: float  # peak pixel value
    contrast: float  # peak over the mean of the positive pixels: how clear the spot is


@dataclass
class Imager:
    """Imaging for a fixed layout. Rebuild it when nodes move or links come and go."""

    positions: Mapping[Hashable, Point]
    links: Sequence[LinkKey]
    pixel: float = 0.25  # metres
    margin: float = 0.75  # around the outermost nodes
    excess: float = 0.3  # ellipse: path via the pixel at most this much longer than the link
    alpha: float = 0.5  # regularisation: higher is smoother and less sensitive
    xs: list[float] = field(init=False)
    ys: list[float] = field(init=False)
    _rows: list[list[tuple[int, float]]] = field(init=False)  # sparse W, per link
    _k: list[list[float]] = field(init=False)  # (W W^T + alpha I)^-1

    def __post_init__(self) -> None:
        used = [k for k in self.links if k[0] in self.positions and k[1] in self.positions]
        self.links = used
        pts = [self.positions[m] for k in used for m in k] or [(0.0, 0.0)]
        x0 = min(p[0] for p in pts) - self.margin
        y0 = min(p[1] for p in pts) - self.margin
        x1 = max(p[0] for p in pts) + self.margin
        y1 = max(p[1] for p in pts) + self.margin
        nx = max(1, round((x1 - x0) / self.pixel))
        ny = max(1, round((y1 - y0) / self.pixel))
        self.xs = [x0 + (i + 0.5) * self.pixel for i in range(nx)]
        self.ys = [y0 + (j + 0.5) * self.pixel for j in range(ny)]
        self._rows = [self._weights(*k) for k in used]
        n = len(used)
        gram = [[0.0] * n for _ in range(n)]
        dense = [dict(r) for r in self._rows]
        for a in range(n):
            for b in range(a, n):
                small, large = (dense[a], dense[b]) if len(dense[a]) <= len(dense[b]) else (dense[b], dense[a])
                v = sum(w * large.get(p, 0.0) for p, w in small.items())
                gram[a][b] = gram[b][a] = v
            gram[a][a] += self.alpha
        self._k = _solve_inverse(gram) if n else []

    @property
    def size(self) -> tuple[int, int]:
        return len(self.xs), len(self.ys)

    def _weights(self, tx: Hashable, rx: Hashable) -> list[tuple[int, float]]:
        (ax, ay), (bx, by) = self.positions[tx], self.positions[rx]
        length = max(math.hypot(bx - ax, by - ay), 0.1)
        w = 1.0 / math.sqrt(length)
        out = []
        nx = len(self.xs)
        for j, py in enumerate(self.ys):
            for i, px in enumerate(self.xs):
                if math.hypot(px - ax, py - ay) + math.hypot(px - bx, py - by) < length + self.excess:
                    out.append((j * nx + i, w))
        return out

    def image(self, values: Mapping[LinkKey, float]) -> list[float]:
        """Pixel values, row by row. values: disturbance per link, for example log(score); missing
        links count as undisturbed."""
        y = [max(0.0, values.get(k, 0.0)) for k in self.links]
        n = len(y)
        if n == 0:
            return [0.0] * (len(self.xs) * len(self.ys))
        z = [sum(self._k[a][b] * y[b] for b in range(n)) for a in range(n)]
        img = [0.0] * (len(self.xs) * len(self.ys))
        for a, row in enumerate(self._rows):
            za = z[a]
            if za == 0.0:
                continue
            for p, w in row:
                img[p] += w * za
        return img

    def locate(self, values: Mapping[LinkKey, float], min_disturbance: float = 0.3) -> Location | None:
        """The brightest spot, or None when the links together are too quiet to say. The image's
        scale depends on pixel size and geometry, so "is anyone there" is judged on the links."""
        if sum(max(0.0, values.get(k, 0.0)) for k in self.links) < min_disturbance:
            return None
        img = self.image(values)
        best = max(range(len(img)), key=img.__getitem__, default=None)
        if best is None or img[best] <= 0.0:
            return None
        nx = len(self.xs)
        j, i = divmod(best, nx)
        # Centre of mass of the pixels near the peak, for a position finer than one pixel.
        sx = sy = sw = 0.0
        for dj in (-1, 0, 1):
            for di in (-1, 0, 1):
                jj, ii = j + dj, i + di
                if 0 <= jj < len(self.ys) and 0 <= ii < nx:
                    v = max(0.0, img[jj * nx + ii])
                    sx += v * self.xs[ii]
                    sy += v * self.ys[jj]
                    sw += v
        positive = [v for v in img if v > 0]
        contrast = img[best] / (sum(positive) / len(positive)) if positive else 0.0
        return Location(sx / sw, sy / sw, img[best], contrast)


def line_distance(p: Point, a: Point, b: Point) -> float:
    """Distance from p to the segment a-b."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    span = dx * dx + dy * dy
    t = 0.0 if span == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / span))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


@dataclass
class Locator:
    """Best fit position of one moving person, for a fixed layout."""

    positions: Mapping[Hashable, Point]
    links: Sequence[LinkKey]
    pixel: float = 0.25
    margin: float = 0.5
    width: float = 0.4  # how far from a link's line a person still disturbs it (metres, 1 sigma)
    xs: list[float] = field(init=False)
    ys: list[float] = field(init=False)
    _pred: list[list[float]] = field(init=False)  # per pixel: predicted disturbance per link
    _norm: list[float] = field(init=False)

    def __post_init__(self) -> None:
        used = [k for k in self.links if k[0] in self.positions and k[1] in self.positions]
        self.links = used
        pts = [self.positions[m] for k in used for m in k] or [(0.0, 0.0)]
        x0 = min(p[0] for p in pts) - self.margin
        y0 = min(p[1] for p in pts) - self.margin
        nx = max(1, round((max(p[0] for p in pts) + self.margin - x0) / self.pixel))
        ny = max(1, round((max(p[1] for p in pts) + self.margin - y0) / self.pixel))
        self.xs = [x0 + (i + 0.5) * self.pixel for i in range(nx)]
        self.ys = [y0 + (j + 0.5) * self.pixel for j in range(ny)]
        two_s2 = 2 * self.width * self.width
        ends = [(self.positions[t], self.positions[r]) for t, r in used]
        self._pred = []
        self._norm = []
        for y in self.ys:
            for x in self.xs:
                row = [math.exp(-(line_distance((x, y), a, b) ** 2) / two_s2) for a, b in ends]
                self._pred.append(row)
                self._norm.append(sum(v * v for v in row))

    def locate(self, values: Mapping[LinkKey, float], min_disturbance: float = 0.3) -> Location | None:
        """values: disturbance per link (for example log(score)); missing links count as quiet.
        Location.strength is the fitted scale, Location.contrast the share of the observed
        pattern the fit explains (0 to 1)."""
        y = [max(0.0, values.get(k, 0.0)) for k in self.links]
        total = sum(y)
        if total < min_disturbance:
            return None
        yy = sum(v * v for v in y)
        best = None
        for i, row in enumerate(self._pred):
            norm = self._norm[i]
            if norm < 1e-9:
                continue
            dot = sum(a * b for a, b in zip(row, y))
            if dot <= 0:
                continue
            residual = yy - dot * dot / norm  # least squares with the best scale
            if best is None or residual < best[0]:
                best = (residual, i, dot / norm)
        if best is None:
            return None
        residual, i, scale = best
        j, k = divmod(i, len(self.xs))
        return Location(self.xs[k], self.ys[j], scale, max(0.0, 1.0 - residual / yy))

