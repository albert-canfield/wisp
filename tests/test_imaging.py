"""Radio tomographic imaging on synthetic floors (no Home Assistant needed)."""

from __future__ import annotations

import math
import random

from custom_components.wisp.engine.imaging import Imager, Locator, _solve_inverse, line_distance

# Four nodes in the corners of a 6 x 4.5 m room and an access point on one wall.
NODES = {"A": (0.0, 0.0), "B": (6.0, 0.0), "C": (6.0, 4.5), "D": (0.0, 4.5), "AP": (3.0, 5.0)}
LINKS = [(t, r) for t in NODES for r in "ABCD" if t != r]  # every node hears the others and the AP


def disturbance(person: tuple[float, float], tx: str, rx: str, sigma: float = 0.35) -> float:
    """A person near the straight line between tx and rx disturbs that link."""
    (ax, ay), (bx, by), (px, py) = NODES[tx], NODES[rx], person
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
    return math.exp(-(d * d) / (2 * sigma * sigma))


def test_inverse() -> None:
    a = [[4.0, 1.0], [1.0, 3.0]]
    inv = _solve_inverse(a)
    for i in range(2):
        for j in range(2):
            assert abs(sum(a[i][k] * inv[k][j] for k in range(2)) - (1.0 if i == j else 0.0)) < 1e-9


def test_finds_a_person() -> None:
    imager = Imager(NODES, LINKS)
    assert len(imager.links) == len(LINKS)
    rng = random.Random(4)
    errors = []
    for person in [(1.5, 1.2), (3.0, 2.2), (4.6, 3.4), (2.2, 3.6), (5.0, 1.0)]:
        values = {k: disturbance(person, *k) + rng.gauss(0, 0.03) for k in LINKS}
        spot = imager.locate(values)
        assert spot is not None
        errors.append(math.hypot(spot.x - person[0], spot.y - person[1]))
    assert sum(errors) / len(errors) < 1.0, errors
    assert max(errors) < 1.6, errors


def test_quiet_floor_finds_nobody() -> None:
    imager = Imager(NODES, LINKS)
    assert imager.locate({k: 0.0 for k in LINKS}) is None
    assert imager.locate({}) is None


def test_missing_links_and_unknown_positions() -> None:
    # Links whose ends have no position are left out; missing readings count as quiet.
    imager = Imager({**NODES, "E": (9.0, 9.0)}, [*LINKS, ("X", "A")])
    assert ("X", "A") not in imager.links
    person = (1.5, 1.2)
    some = {k: disturbance(person, *k) for k in LINKS[::2]}
    spot = imager.locate(some)
    assert spot is not None and math.hypot(spot.x - person[0], spot.y - person[1]) < 1.5


def test_grid_covers_the_nodes() -> None:
    imager = Imager(NODES, LINKS, pixel=0.5, margin=0.5)
    nx, ny = imager.size
    assert imager.xs[0] < 0 < imager.xs[-1] and imager.xs[-1] > 6
    assert imager.ys[0] < 0 and imager.ys[-1] > 5
    assert nx * ny == len(imager.image({}))


def test_locator_beats_imaging_on_random_positions() -> None:
    """Gains vary by +-40%, readings are noisy and real people are wider than the model."""
    locator = Locator(NODES, LINKS)
    imager = Imager(NODES, LINKS)
    rng = random.Random(2)
    fit, img = [], []
    for _ in range(150):
        person = (rng.uniform(0.3, 5.7), rng.uniform(0.3, 4.2))
        values = {
            k: max(0.0, disturbance(person, *k, sigma=0.5) * rng.uniform(0.6, 1.4) + rng.gauss(0, 0.05)) for k in LINKS
        }
        a, b = locator.locate(values, 0.0), imager.locate(values, 0.0)
        fit.append(math.hypot(a.x - person[0], a.y - person[1]))
        img.append(math.hypot(b.x - person[0], b.y - person[1]))
    fit.sort()
    img.sort()
    assert fit[75] < 0.6 and fit[135] < 1.6, (fit[75], fit[135])
    assert fit[75] < img[75] / 2


def test_locator_quiet_and_quality() -> None:
    locator = Locator(NODES, LINKS)
    assert locator.locate({}) is None
    person = (3.0, 2.2)
    spot = locator.locate({k: disturbance(person, *k) for k in LINKS})
    assert spot is not None and math.hypot(spot.x - 3.0, spot.y - 2.2) < 0.4
    assert spot.contrast > 0.9  # a clean pattern is explained almost fully


def test_line_distance() -> None:
    assert line_distance((1, 1), (0, 0), (2, 0)) == 1
    assert line_distance((3, 0), (0, 0), (2, 0)) == 1  # past the end
    assert line_distance((1, 1), (0, 0), (0, 0)) == math.hypot(1, 1)

