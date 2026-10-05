"""Anchoring the hive's layout on a floor plan.

The hive's layout is relative: right up to rotation, mirror, scale and shift. Once the user has
placed some of its nodes on the plan, the similarity (rotation, optional mirror, uniform scale,
translation) that maps the layout best onto them is the least squares fit of Umeyama (1991). In
2D it is short with complex numbers: with a and b the layout and plan points minus their means,

    z = sum(conj(a) b) / sum(|a|^2)           rotation and scale in one complex number
    t = mean(plan) - z mean(layout)            translation

The mirror is the same fit on the layout with y negated; the lower residual wins. Points on one
line (two always are) fit both ways equally well, so there the mirror is a preference.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math

Point = tuple[float, float]

THIN = 0.98  # |sum(a^2)| / sum(|a|^2) above this: the points are close to one line


@dataclass(frozen=True, slots=True)
class Similarity:
    """p -> z * p + t, with p's y negated first when mirror is set."""

    z: complex = 1
    t: complex = 0
    mirror: bool = False
    error: float = 0.0  # RMS distance between the placed points and where the fit puts them, metres
    pairs: int = 0  # points it was fitted on: 0 and 1 are shifts only
    guessed: bool = False  # the points could not tell the mirror, so it is the preference

    @property
    def scale(self) -> float:
        return abs(self.z)

    @property
    def rotation(self) -> float:
        """Degrees from the plan's x axis towards its y axis: clockwise on a plan drawn y down."""
        return math.degrees(math.atan2(self.z.imag, self.z.real))

    def apply(self, p: Point) -> Point:
        q = self.z * complex(p[0], -p[1] if self.mirror else p[1]) + self.t
        return (q.real, q.imag)


def shift(source: Point, target: Point, mirror: bool = False, pairs: int = 0) -> Similarity:
    """No turn and no scale: source lands on target."""
    s = complex(source[0], -source[1] if mirror else source[1])
    return Similarity(1, complex(*target) - s, mirror, 0.0, pairs, True)


def _fit(src: list[complex], dst: list[complex], mirror: bool) -> Similarity | None:
    if mirror:
        src = [s.conjugate() for s in src]
    n = len(src)
    ms, md = sum(src) / n, sum(dst) / n
    a = [s - ms for s in src]
    b = [d - md for d in dst]
    saa = sum(abs(v) ** 2 for v in a)
    if saa < 1e-12:
        return None
    cross = sum(x.conjugate() * y for x, y in zip(a, b))
    z = cross / saa
    sse = sum(abs(y - z * x) ** 2 for x, y in zip(a, b))
    return Similarity(z, md - z * ms, mirror, math.sqrt(sse / n), n)


def fit_similarity(
    pairs: Sequence[tuple[Point, Point]], mirror: bool | None = None, prefer_mirror: bool = False
) -> Similarity | None:
    """The similarity taking each pair's first point closest to its second. mirror forces it either
    way; None picks the lower error, or prefer_mirror when the points lie on one line. None for
    fewer than two pairs, or first points that do not spread at all."""
    if len(pairs) < 2:
        return None
    src = [complex(*p) for p, _ in pairs]
    dst = [complex(*q) for _, q in pairs]
    if mirror is not None:
        return _fit(src, dst, mirror)
    plain, mirrored = _fit(src, dst, False), _fit(src, dst, True)
    if plain is None or mirrored is None:
        return None
    mean = sum(src) / len(src)
    a = [s - mean for s in src]
    if abs(sum(v * v for v in a)) > THIN * sum(abs(v) ** 2 for v in a):
        chosen = mirrored if prefer_mirror else plain
        return Similarity(chosen.z, chosen.t, chosen.mirror, chosen.error, chosen.pairs, True)
    return mirrored if mirrored.error < plain.error else plain


def _middle(points: Sequence[Point]) -> Point:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)


def anchor_layout(
    layout: Mapping[str, Point], placed: Mapping[str, Point], centre: Point, prefer_mirror: bool = False
) -> tuple[dict[str, Point], Similarity]:
    """Every point of the layout on the plan: placed ones where the user put them, the others
    through the similarity fitted on the placed ones (two or more), else shifted so the placed one
    lands right, or with none placed, the layout's middle on centre. Placed points that are not
    on the layout (access points, nodes the hive has not placed) are kept as placed."""
    pairs = [(layout[k], placed[k]) for k in sorted(layout) if k in placed]
    fit = fit_similarity(pairs, prefer_mirror=prefer_mirror)
    if fit is None:
        if pairs:  # one, or several on the same spot of the layout
            fit = shift(_middle([p for p, _ in pairs]), _middle([q for _, q in pairs]), prefer_mirror, len(pairs))
        else:
            fit = shift(_middle(list(layout.values())) if layout else centre, centre, prefer_mirror)
    positions = {k: fit.apply(p) for k, p in layout.items() if k not in placed}
    positions.update(placed)
    return positions, fit
