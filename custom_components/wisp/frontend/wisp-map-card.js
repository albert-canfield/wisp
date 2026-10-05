/*
 * Wisp map card: the grid drawn in ink on parchment, live from the Wisp integration.
 * Nodes sit where the hive's layout puts them, access points beside the nodes that hear them
 * best, links darken and thicken with their motion score and glow while they see motion, and
 * footprints follow whoever moves. On a floor with a plan (set in the Wisp panel) everything is
 * drawn on the plan, in its metres, over the rooms drawn on it. Shipped and registered by the
 * integration, no build step.
 *
 *   type: custom:wisp-map-card
 *   title: Wisp     # optional
 *   floor: Upstairs # optional, floor id or name; the first floor with nodes by default
 *   rotate: 0       # optional, degrees clockwise, to match your home (a plan turns in 90° steps)
 *   flip: false     # optional, mirror left to right
 *   plan_photo: false # optional, the floor plan is a photo: dimmed, not inverted, in dark mode
 */

const W = 360; // viewBox width; the height follows the drawing
const PAD_X = 52; // room for labels around the drawing
const PAD_Y = 36;
const MIN_H = 220;
const MAX_H = 400;
const PLAN_PAD = 24; // around a floor plan; labels may overlap it
const PLAN_MAX_H = 560;
const SCALES = [0.5, 1, 2, 5, 10, 20, 50, 100]; // metres a scale bar may show
const STRIDE = 16; // px between footprints on a trail
const HEAD_GAP = 15; // px clear behind the pair of prints where someone is now
const TRAIL_N = 8; // fixes a trail keeps, one a second while someone moves
const TRAIL_S = 10; // s a trail reaches back
const JUMP_M = 3; // m between two fixes that is no step: a new trail
const GAP_S = 4; // s without a fix that ends a trail
const FADE_S = 4; // s a trail takes to fade once nobody moves
const STEP_IN = 0.6; // s the newest stretch of prints takes to appear
const PULSE_S = 2.4; // s a moving link's glow takes to pulse
// The logo's footprint, toes up
const SOLE = "M0-13c4.6 0 6.4 4.4 6.4 8.6 0 4.6-2.2 7.9-6.4 7.9s-6.4-3.3-6.4-7.9C-6.4-8.6-4.6-13 0-13Z";
const HEEL = "M0 5.6c3.3 0 4.8 2.1 4.8 4.4 0 2.6-2 4-4.8 4s-4.8-1.4-4.8-4c0-2.3 1.5-4.4 4.8-4.4Z";
const FOOT = `<path d="${SOLE}"/><path d="${HEEL}"/>`;

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const n1 = (v) => Math.round(v * 10) / 10;
const short = (s, max = 18) => (s.length > max ? `${s.slice(0, max - 1)}…` : s);
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
/* A gentle hue per area, the same in the panel */
const hue = (area) => {
  let h = 2166136261; // FNV-1a, then mixed so that ids alike get hues apart
  for (const c of String(area)) h = Math.imul(h ^ c.codePointAt(0), 16777619);
  h = Math.imul(h ^ (h >>> 16), 0x85ebca6b);
  h = Math.imul(h ^ (h >>> 13), 0xc2b2ae35);
  return ((h ^ (h >>> 16)) >>> 0) % 360;
};

function bounds(points) {
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  return { x0, x1, y0, y1, w: x1 - x0, h: y1 - y0, cx: (x0 + x1) / 2, cy: (y0 + y1) / 2 };
}

/* Positions in metres, y up: nodes from the layout, access points where the position engine puts
   them, else beside who hears them best. */
function place(map) {
  const pos = new Map();
  for (const n of map.nodes) if (n.x != null && n.y != null) pos.set(n.mac, { x: n.x, y: n.y });
  const loose = map.nodes.filter((n) => !pos.has(n.mac));
  const onLayout = pos.size > 0; // else the nodes go on a circle, and layout metres would not fit it
  if (!pos.size) {
    // No layout yet: a circle, so the links still show
    loose.forEach((n, i) => {
      const a = Math.PI / 2 - (2 * Math.PI * i) / loose.length;
      pos.set(n.mac, { x: Math.cos(a), y: Math.sin(a) });
    });
  } else if (loose.length) {
    // Not on the layout yet: a row below it
    const b = bounds([...pos.values()]);
    const span = Math.max(b.w, b.h, 0.6);
    loose.forEach((n, i) => pos.set(n.mac, { x: b.cx + (i - (loose.length - 1) / 2) * span / loose.length, y: b.y0 - 0.4 * span }));
  }
  const nodes = [...pos.values()];
  const b = bounds(nodes);
  const c = { x: nodes.reduce((s, p) => s + p.x, 0) / nodes.length, y: nodes.reduce((s, p) => s + p.y, 0) / nodes.length };
  const span = Math.max(b.w, b.h, 0.5);
  const beside = new Map(); // access points already placed beside a node
  for (const ap of map.access_points) {
    if (onLayout && ap.x != null && ap.y != null) {
      pos.set(ap.bssid, { x: ap.x, y: ap.y });
      continue;
    }
    const heard = ap.heard_by.filter((h) => pos.has(h.node));
    let anchor = { x: c.x, y: b.y1 };
    if (heard.length) {
      // Weighted by received power relative to the best: 10 dB weaker counts a tenth
      let sx = 0, sy = 0, sw = 0;
      for (const h of heard) {
        const w = 10 ** ((h.rssi - heard[0].rssi) / 10);
        const p = pos.get(h.node);
        sx += w * p.x; sy += w * p.y; sw += w;
      }
      anchor = { x: sx / sw, y: sy / sw };
    }
    let dx = anchor.x - c.x, dy = anchor.y - c.y;
    const d = Math.hypot(dx, dy);
    if (d < 1e-3) { dx = 0; dy = 1; } else { dx /= d; dy /= d; }
    // Outward and turned aside, so its lines do not lie on the node lines; the next one turns the other way
    const key = heard[0]?.node ?? "";
    const k = beside.get(key) ?? 0;
    beside.set(key, k + 1);
    const a = ((k % 2 ? -1 : 1) * (35 + 30 * Math.floor(k / 2)) * Math.PI) / 180;
    const r = 0.4 * span;
    pos.set(ap.bssid, {
      x: anchor.x + r * (dx * Math.cos(a) - dy * Math.sin(a)),
      y: anchor.y + r * (dx * Math.sin(a) + dy * Math.cos(a)),
    });
  }
  // Someone moving, per floor, on the same layout
  for (const p of map.people ?? []) pos.set(`person:${p.floor ?? ""}`, { x: p.x, y: p.y });
  return pos;
}

/* The floor to draw, by id or name, else the first, with only its nodes, access points, people
   and rooms. A feed without floors (one floor, no plan) is drawn whole. */
function pickFloor(map, wanted) {
  const floors = map.floors ?? [];
  if (!floors.length) return { map, floor: null };
  const key = String(wanted ?? "").trim().toLowerCase();
  const floor = !key
    ? floors[0]
    : floors.find((f) => (f.floor ?? "").toLowerCase() === key) ?? floors.find((f) => f.name.toLowerCase() === key);
  if (!floor) return { missing: String(wanted).trim(), floors };
  const macs = new Set(floor.nodes);
  const here = (x) => (x.floor ?? null) === (floor.floor ?? null);
  const known = new Map(map.access_points.map((ap) => [ap.bssid, ap]));
  // On a plan the access points are the ones with a place on it, heard now or not
  const aps = floor.plan
    ? Object.keys(floor.positions ?? {}).filter((id) => !macs.has(id)).map((id) => known.get(id) ?? { bssid: id, label: `AP ${id.slice(-5)}`, heard_by: [] })
    : map.access_points.filter((ap) => ap.heard_by.some((h) => macs.has(h.node)));
  return {
    floor,
    map: {
      ...map,
      nodes: map.nodes.filter((n) => macs.has(n.mac)),
      access_points: aps,
      people: (map.people ?? []).filter(here),
      rooms: (map.rooms ?? []).filter(here),
    },
  };
}

/* Plan metres (x right, y down, from the top left) to the viewBox: the plan turned a quarter at a
   time clockwise, mirrored left to right, scaled to fit; nodes without a place in a row below. */
function projectPlan(map, floor, rotate, flip) {
  const plan = floor.plan, positions = floor.positions ?? {};
  const turn = ((Math.round(rotate / 90) % 4) + 4) % 4;
  const [pw, ph] = turn % 2 ? [plan.height, plan.width] : [plan.width, plan.height]; // as drawn
  const inner = W - 2 * PLAN_PAD;
  const s = Math.min(inner / pw, (PLAN_MAX_H - 2 * PLAN_PAD) / ph);
  const x0 = (W - pw * s) / 2, y0 = PLAN_PAD;
  const to = (p) => {
    const [u, v] = [[p.x, p.y], [plan.height - p.y, p.x], [plan.width - p.x, plan.height - p.y], [p.y, plan.width - p.x]][turn];
    return { x: x0 + (flip ? pw - u : u) * s, y: y0 + v * s };
  };
  const frame = { x: x0, y: y0, w: pw * s, h: ph * s, s, turn, flip, plan, to };
  const pts = new Map();
  for (const [id, p] of Object.entries(positions)) pts.set(id, to(p));
  for (const p of map.people ?? []) pts.set(`person:${p.floor ?? ""}`, to(p));
  const loose = map.nodes.filter((n) => !pts.has(n.mac));
  const gap = Math.min(80, inner / Math.max(loose.length, 1));
  loose.forEach((n, i) => pts.set(n.mac, { x: W / 2 + (i - (loose.length - 1) / 2) * gap, y: frame.y + frame.h + 26 }));
  return { pts, frame, to, s, h: Math.round(frame.y + frame.h + PLAN_PAD + (loose.length ? 30 : 0)) };
}

/* A plan without an image: a grid of whole metres (or halves, or a few) from the plan's top left,
   about 14 px or more apart, turned with the plan. */
function gridPath(f) {
  const { plan, to } = f;
  const step = [0.5, 1, 2, 5, 10].find((v) => v * f.s >= 14) ?? 10;
  const end = 0.5 / f.s; // no line on the edge
  let d = "";
  const line = (a, b) => {
    const p = to(a), q = to(b);
    d += `M${n1(p.x)} ${n1(p.y)}L${n1(q.x)} ${n1(q.y)}`;
  };
  for (let x = step; x < plan.width - end; x += step) line({ x, y: 0 }, { x, y: plan.height });
  for (let y = step; y < plan.height - end; y += step) line({ x: 0, y }, { x: plan.width, y });
  return d;
}

/* The plan's edge, its grid without an image, and a scale bar of a round number of metres. The
   image itself lies under the drawing as an img that stays across redraws, so it does not load
   again each second. */
function planLayer(f, grid = false) {
  const m = SCALES.find((v) => v * f.s >= 36) ?? SCALES[SCALES.length - 1];
  const x = f.x + 10, y = f.y + f.h - 10, len = m * f.s;
  return `${grid ? `<path class="grid" d="${gridPath(f)}"/>` : ""}<rect class="edge" x="${n1(f.x)}" y="${n1(f.y)}" width="${n1(f.w)}" height="${n1(f.h)}"/><g class="scale" aria-hidden="true"><path d="M${n1(x)} ${n1(y - 4)}V${n1(y)}H${n1(x + len)}V${n1(y - 4)}"/><text x="${n1(x + len / 2)}" y="${n1(y - 6)}">${m} m</text></g>`;
}

/* A room's outline from its rectangles [x, y, w, h], without the seams between them: each side
   less where another rectangle lies just beyond it. Segments [x0, y0, x1, y1] in plan metres. */
function outline(rects) {
  const E = 1e-6, out = [];
  rects.forEach(([x, y, w, h], i) => {
    const others = rects.filter((_, j) => j !== i);
    // A side: across, along from, along to, and whether a rectangle covers the strip beyond it
    const sides = [
      [false, y, x, x + w, (o) => o[1] < y - E && o[1] + o[3] >= y - E],
      [false, y + h, x, x + w, (o) => o[1] <= y + h + E && o[1] + o[3] > y + h + E],
      [true, x, y, y + h, (o) => o[0] < x - E && o[0] + o[2] >= x - E],
      [true, x + w, y, y + h, (o) => o[0] <= x + w + E && o[0] + o[2] > x + w + E],
    ];
    for (const [upright, at, from, to, beyond] of sides) {
      let parts = [[from, to]];
      for (const o of others.filter(beyond)) {
        const [a, b] = upright ? [o[1], o[1] + o[3]] : [o[0], o[0] + o[2]];
        parts = parts.flatMap(([p, q]) => [[p, Math.min(q, a)], [Math.max(p, b), q]]).filter(([p, q]) => q - p > E);
      }
      for (const [p, q] of parts) out.push(upright ? [at, p, at, q] : [p, at, q, at]);
    }
  });
  return out;
}

/* The rooms drawn on the plan, turned with it: a faint wash and outline each in its hue, stronger
   on a plan without an image, where they are the house; the room someone moves in a little more.
   Names go in the top left corner of each room's largest rectangle, as drawn. */
function roomsLayer(rooms, f, here) {
  let shapes = "", names = "";
  for (const room of rooms) {
    if (!room.rects?.length) continue;
    const boxes = room.rects.map(([x, y, w, h]) => {
      const p = f.to({ x, y }), q = f.to({ x: x + w, y: y + h });
      return { x: Math.min(p.x, q.x), y: Math.min(p.y, q.y), w: Math.abs(q.x - p.x), h: Math.abs(q.y - p.y) };
    });
    const wash = boxes.map((b) => `<rect x="${n1(b.x)}" y="${n1(b.y)}" width="${n1(b.w)}" height="${n1(b.h)}"/>`).join("");
    const wall = outline(room.rects).map(([x0, y0, x1, y1]) => {
      const p = f.to({ x: x0, y: y0 }), q = f.to({ x: x1, y: y1 });
      return `M${n1(p.x)} ${n1(p.y)}L${n1(q.x)} ${n1(q.y)}`;
    }).join("");
    shapes += `<g class="room${here.has(room.area) ? " here" : ""}" style="--hue:${hue(room.area)}"><title>${esc(room.name)}</title><g class="wash">${wash}</g><path class="wall" d="${wall}"/></g>`;
    const big = boxes.reduce((a, b) => (b.w * b.h > a.w * a.h ? b : a));
    const fit = Math.floor((big.w - 8) / 5.2); // characters of 10 px italic
    if (fit >= 3 && big.h >= 16) names += `<text class="room-name" x="${n1(big.x + 4)}" y="${n1(big.y + 12)}">${esc(short(room.name, fit))}</text>`;
  }
  return { shapes, names };
}

/* Metres to the viewBox: turn, mirror, then scale to fit. */
function project(pos, rotate, flip) {
  const a = (rotate * Math.PI) / 180, cos = Math.cos(a), sin = Math.sin(a);
  const turn = (p) => {
    const x = p.x * cos + p.y * sin, y = p.x * sin - p.y * cos; // screen y points down, clockwise
    return { x: flip ? -x : x, y };
  };
  const b = bounds([...pos.values()].map(turn));
  const inner = W - 2 * PAD_X;
  const h = clamp(Math.round(inner * (b.h / Math.max(b.w, 1e-6)) + 2 * PAD_Y), MIN_H, MAX_H);
  const s = Math.min(inner / Math.max(b.w, 1e-6), (h - 2 * PAD_Y) / Math.max(b.h, 1e-6));
  const to = (p) => {
    const q = turn(p);
    return { x: W / 2 + (q.x - b.cx) * s, y: h / 2 + (q.y - b.cy) * s };
  };
  const pts = new Map();
  for (const [key, p] of pos) pts.set(key, to(p));
  return { pts, h, to, s };
}

/* One line per pair: the two directions of a node pair share it, the busier one sets the ink. */
function pairs(links, pts) {
  const out = new Map();
  for (const l of links) {
    if (!pts.has(l.transmitter) || !pts.has(l.receiver)) continue;
    const [a, b] = [l.transmitter, l.receiver].sort();
    const g = out.get(`${a} ${b}`) ?? { a, b, kind: l.kind, score: null, motion: false };
    if (l.score != null && (g.score == null || l.score > g.score)) g.score = l.score;
    if (l.motion) g.motion = true;
    out.set(`${a} ${b}`, g);
  }
  // Quiet lines first, so busy ones are drawn on top
  return [...out.values()].sort((x, y) => (x.motion - y.motion) || ((x.score ?? 0) - (y.score ?? 0)));
}

/* A quadratic curve with a slight bow, like a line drawn by hand. */
function curve(p0, p1) {
  const dx = p1.x - p0.x, dy = p1.y - p0.y, len = Math.hypot(dx, dy);
  const bow = 0.07 * len;
  return { p0, p1, len, c: { x: (p0.x + p1.x) / 2 - (dy / len) * bow, y: (p0.y + p1.y) / 2 + (dx / len) * bow } };
}

/* Corners cut twice (Chaikin), the ends kept, so a jittery path walks smoothly. */
function smooth(pts) {
  let out = pts;
  for (let k = 0; k < 2 && out.length > 2; k++) {
    const next = [out[0]];
    for (let i = 0; i < out.length - 1; i++) {
      const a = out[i], b = out[i + 1];
      const mix = (w) => ({ x: a.x + (b.x - a.x) * w, y: a.y + (b.y - a.y) * w, t: a.t + (b.t - a.t) * w });
      if (i > 0) next.push(mix(0.25));
      if (i < out.length - 2) next.push(mix(0.75));
    }
    next.push(out[out.length - 1]);
    out = next;
  }
  return out;
}

const heading = (ux, uy) => (Math.atan2(ux, -uy) * 180) / Math.PI; // toes up is 0, clockwise

/* Someone walking: footprints along their last fixes, left and right in turn, older ones fainter,
   and where they are now a pair of prints in a halo that grows with the doubt. The prints sit at
   whole strides of the distance walked, so they stay put while the trail grows. */
function walker(tr, to, s, now) {
  const pts = smooth(tr.fixes.map((f) => ({ ...to(f), t: f.t })));
  const head = pts[pts.length - 1], newest = tr.fixes[tr.fixes.length - 1];
  const segs = []; // from the head back
  let len = 0;
  for (let i = pts.length - 1; i > 0; i--) {
    const a = pts[i], b = pts[i - 1], l = Math.hypot(a.x - b.x, a.y - b.y);
    if (l < 1e-3) continue;
    segs.push({ a, b, l, from: len, ux: (a.x - b.x) / l, uy: (a.y - b.y) / l });
    len += l;
  }
  const ref = tr.gone ?? now;
  const prev = tr.fixes.length > 1 ? tr.fixes[tr.fixes.length - 2].t : null;
  const walked = tr.walked * s;
  let prints = "";
  for (let back = walked % STRIDE; back <= len; back += STRIDE) {
    if (back < HEAD_GAP) continue;
    const g = segs.find((g) => back <= g.from + g.l) ?? segs[segs.length - 1];
    const w = (back - g.from) / g.l;
    const p = { x: g.a.x + (g.b.x - g.a.x) * w, y: g.a.y + (g.b.y - g.a.y) * w, t: g.a.t + (g.b.t - g.a.t) * w };
    const opacity = 0.9 * Math.min(1 - (ref - p.t) / 1000 / TRAIL_S, 1 - (0.85 * back) / len);
    if (opacity < 0.04) continue;
    const side = Math.round((walked - back) / STRIDE) % 2 ? 1 : -1; // right, left, right
    const x = p.x - g.uy * 4.2 * side, y = p.y + g.ux * 4.2 * side;
    // The stretch walked since the last fix steps in, print after print
    let delay = "";
    if (!tr.gone && prev != null && p.t > prev && newest.t > prev) {
      const d = ((p.t - prev) / (newest.t - prev)) * STEP_IN - (now - newest.t) / 1000;
      if (d > -0.5) delay = `;animation-delay:${d.toFixed(2)}s`;
    }
    prints += `<g class="print${delay ? " new" : ""}" transform="translate(${n1(x)} ${n1(y)}) rotate(${n1(heading(g.ux, g.uy) + side * 8)}) scale(.5)" style="opacity:${opacity.toFixed(2)}${delay}">${FOOT}</g>`;
  }
  const sure = clamp(newest.q, 0, 1);
  const r = 12 + 18 * clamp((1 - sure) / 0.7, 0, 1); // 12 px sure, 30 px at 30% and less
  const angle = segs.length ? heading(segs[0].ux, segs[0].uy) : 0; // toes up before the first step
  const tip = tr.gone ? "Someone moved here" : `Someone moving here, ${Math.round(sure * 100)}% sure`;
  const mark = `<g class="person" transform="translate(${n1(head.x)} ${n1(head.y)})"><title>${tip}</title><circle class="halo" r="${n1(r)}"/><g transform="rotate(${n1(angle)})"><g transform="translate(-4.6 2.5) rotate(-9) scale(.55)">${FOOT}</g><g transform="translate(4.6 -2.5) rotate(9) scale(.55)">${FOOT}</g></g></g>`;
  // Nobody moves: the trail fades out; a negative delay keeps it fading across redraws
  const fade = tr.gone ? ` style="animation-delay:${((tr.gone - now) / 1000).toFixed(2)}s"` : "";
  const wrap = (cls, html) => `<g class="${cls}${tr.gone ? " gone" : ""}"${fade}>${html}</g>`;
  return { prints: wrap("trail", prints), mark: wrap("walker", mark) };
}

/* Label away from the drawing's middle, below or above its mark; on the other side, or a line
   further out, when it would cover a label already drawn (nodes side by side). */
function label(p, c, text, cls, below, above, size, taken) {
  const t = short(text);
  const half = (t.length * size * 0.56) / 2;
  const down = p.y + below + 9, up = p.y - above, step = size + 3;
  const tries = p.y >= c.y - 1 ? [down, up, down + step, up - step] : [up, down, up - step, down + step];
  const box = (y) => ({ x0: p.x - half, x1: p.x + half, y0: y - size, y1: y + 3 });
  const free = (b) => !taken.some((o) => b.x0 < o.x1 && o.x0 < b.x1 && b.y0 < o.y1 && o.y0 < b.y1);
  const y = tries.find((y) => free(box(y))) ?? tries[0];
  taken.push(box(y));
  return `<text class="${cls}" x="${n1(p.x)}" y="${n1(y)}">${esc(t)}</text>`;
}

/* The trails to draw: the floor's, or all of them on a feed without floors. */
function trailsOf(trails, floor) {
  if (!floor) return [...trails];
  const key = floor.floor ?? "";
  return trails.has(key) ? [[key, trails.get(key)]] : [];
}

function draw(map, config, floor, trails = new Map(), now = Date.now()) {
  const plan = floor?.plan;
  const rotate = Number(config.rotate) || 0, flip = !!config.flip;
  const walks = trailsOf(trails, floor);
  let projected;
  if (plan) projected = projectPlan(map, floor, rotate, flip);
  else {
    const pos = place(map);
    // A trail fading out keeps the drawing where it was
    for (const [key, tr] of walks) if (!pos.has(`person:${key}`)) pos.set(`person:${key}`, tr.fixes[tr.fixes.length - 1]);
    projected = project(pos, rotate, flip);
  }
  const { pts, h, frame, to, s } = projected;
  const names = new Map(map.nodes.map((n) => [n.mac, n.name]));
  const centre = frame ? { cx: frame.x + frame.w / 2, cy: frame.y + frame.h / 2 } : bounds([...pts.values()]);
  const c = { x: centre.cx, y: centre.cy };
  // On a plan, placed means placed by the user; the rest is fitted to them, or waits below it
  const placed = (id) => (plan ? !!floor.positions?.[id]?.placed : true);
  let lines = "", steps = "", marks = "", walkers = "", labels = "";
  const moving = [], taken = [];
  const pulse = `animation-delay:-${((performance.now() / 1000) % PULSE_S).toFixed(2)}s`; // in step across redraws
  for (const g of pairs(map.links, pts)) {
    const q = curve(pts.get(g.a), pts.get(g.b));
    if (q.len < 1) continue;
    const score = g.score == null ? 0 : clamp((g.score - 1) / 2, 0, 1); // 1 quiet, 3 and up busy
    const t = g.motion ? Math.max(score, 0.75) : score; // a link that sees motion lights up
    const opacity = g.score == null && !g.motion ? 0.35 : 0.45 + 0.5 * t;
    const style = `stroke-width:${n1(1.1 + 3.2 * t)};opacity:${n1(opacity)};stroke:color-mix(in srgb,var(--wisp-hot) ${Math.round(t * 100)}%,var(--wisp-ink))`;
    const cls = `link ${g.kind}${g.score == null ? " unknown" : ""}`;
    const d = `M${n1(q.p0.x)} ${n1(q.p0.y)}Q${n1(q.c.x)} ${n1(q.c.y)} ${n1(q.p1.x)} ${n1(q.p1.y)}`;
    if (g.motion) lines += `<path class="glow" d="${d}" style="stroke-width:${n1(7 + 3.2 * t)};${pulse}"/>`;
    lines += `<path class="${cls}" d="${d}" style="${style}"><title>${esc(names.get(g.a) ?? g.a)} and ${esc(names.get(g.b) ?? g.b)}: ${g.score == null ? "no score yet" : `motion score ${g.score}`}${g.motion ? ", motion" : ""}</title></path>`;
    if (g.motion) moving.push(`${names.get(g.a) ?? "access point"} and ${names.get(g.b) ?? "access point"}`);
  }
  for (const ap of map.access_points) {
    const p = pts.get(ap.bssid);
    const where = placed(ap.bssid) ? "" : ", placed from the signal";
    marks += `<g class="ap" transform="translate(${n1(p.x)} ${n1(p.y)})"><title>Access point ${esc(ap.bssid)}${where}</title><path class="waves" d="M-7.1-7.1A10 10 0 0 1 7.1-7.1M-9.9-9.9A14 14 0 0 1 9.9-9.9"/><rect class="ring" x="-4.6" y="-4.6" width="9.2" height="9.2" transform="rotate(45)"/><circle class="dot" r="1.8"/></g>`;
    labels += label(p, c, ap.label, "ap-label", 8, 18, 11, taken);
  }
  let unplaced = 0;
  for (const n of map.nodes) {
    const p = pts.get(n.mac);
    const known = plan ? !!floor.positions?.[n.mac] : n.x != null && n.y != null;
    const fixed = known && placed(n.mac);
    if (!fixed) unplaced += 1;
    const where = fixed ? "" : !known ? ", not placed yet" : ", not placed on the plan: fitted to the placed nodes";
    marks += `<g class="node${n.online ? "" : " off"}${fixed ? "" : " loose"}" transform="translate(${n1(p.x)} ${n1(p.y)})"><title>${esc(n.name)}: ${n.online ? "online" : "offline"}${where}</title><circle class="ring" r="6.5"/><circle class="dot" r="2.2"/></g>`;
    labels += label(p, c, n.name, `node-label${n.online ? "" : " off"}`, 10, 12, 13, taken);
  }
  // Where someone moves, and the way they came
  for (const [, tr] of walks) {
    const w = walker(tr, to, s, now);
    steps += w.prints;
    walkers += w.mark;
  }
  // The rooms drawn on the plan under it all; the one room presence puts someone in a little stronger
  const here = new Set((map.rooms ?? []).map((r) => r.area).filter(Boolean));
  const rooms = plan && floor.rooms?.length ? roomsLayer(floor.rooms, frame, here) : null;
  const pending = unplaced ? ` (${unplaced} not placed ${plan ? "on the plan" : "yet"})` : "";
  const summary = `${floor ? `${floor.name}: ` : ""}${plural(map.nodes.length, "node", "nodes")}${pending}, ${plural(map.access_points.length, "access point", "access points")}`;
  // The room someone moves in, per calibrated floor
  const occupied = (map.rooms ?? []).filter((f) => f.area).map((f) => f.room);
  const where = occupied.length ? `${occupied.length > 1 ? "Rooms" : "Room"}: ${occupied.join(", ")}. ` : "";
  const motion = where + (moving.length ? `Motion: ${moving.join(", ")}` : "All quiet");
  return {
    svg: `<svg viewBox="0 0 ${W} ${h}" role="img" aria-label="${esc(`Map of ${summary}. ${motion}.`)}">${rooms ? `<g class="rooms${plan.url ? "" : " bare"}">${rooms.shapes}</g>` : ""}${plan ? planLayer(frame, !plan.url) : ""}${rooms?.names ? `<g class="room-names">${rooms.names}</g>` : ""}<g class="lines">${lines}</g><g class="steps">${steps}</g><g class="marks">${marks}${walkers}</g><g class="labels">${labels}</g></svg>`,
    summary,
    motion,
    // Where the plan's image goes, in shares of the drawing: unturned, centred on the frame, then
    // turned and mirrored about its centre to lie as the frame does
    plan: plan && {
      url: plan.url,
      left: (frame.x + frame.w / 2 - (plan.width * s) / 2) / W,
      top: (frame.y + frame.h / 2 - (plan.height * s) / 2) / h,
      width: (plan.width * s) / W,
      height: (plan.height * s) / h,
      transform: frame.turn || frame.flip ? `${frame.flip ? "scaleX(-1) " : ""}rotate(${frame.turn * 90}deg)` : "",
    },
  };
}

const EMPTY_FEET = `<svg class="feet" viewBox="-22 -18 44 36" aria-hidden="true"><g transform="translate(-7 4) rotate(-12) scale(.9)"><path d="${SOLE}"/><path d="${HEEL}"/></g><g transform="translate(8 -4) rotate(10) scale(.9)"><path d="${SOLE}"/><path d="${HEEL}"/></g></svg>`;

class WispMapCard extends HTMLElement {
  static getStubConfig() {
    return { title: "Wisp", rotate: 0, flip: false };
  }

  static getConfigForm() {
    return {
      schema: [
        { name: "title", selector: { text: {} } },
        { name: "floor", selector: { text: {} } },
        { name: "rotate", selector: { number: { min: 0, max: 359, step: 1, mode: "slider", unit_of_measurement: "°" } } },
        { name: "flip", selector: { boolean: {} } },
        { name: "plan_photo", selector: { boolean: {} } },
      ],
      computeLabel: (s) => ({ title: "Title", floor: "Floor", rotate: "Rotate", flip: "Mirror", plan_photo: "Photo floor plan" })[s.name],
      computeHelper: (s) => ({
        floor: "Name or id of the floor to show, in a home with several. Empty: the first floor with nodes",
        rotate: "Degrees clockwise, to match the drawing to your home. A floor plan turns in steps of 90°",
        flip: "Mirror left to right",
        plan_photo: "The floor plan is a photo: dim it in dark mode instead of inverting it",
      })[s.name],
    };
  }

  setConfig(config) {
    this._config = { title: "Wisp", rotate: 0, flip: false, ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    const dark = !!hass.themes?.darkMode;
    if (dark !== this._dark) {
      this._dark = dark;
      this._render();
    }
    this._subscribe();
  }

  connectedCallback() {
    this._subscribe();
  }

  disconnectedCallback() {
    const sub = this._sub;
    this._sub = null;
    if (sub) sub.then((unsub) => unsub?.()).catch(() => {});
    clearTimeout(this._fadeTimer);
    this._trails?.clear(); // a short history: it starts again with the feed
  }

  getCardSize() {
    return 6;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, min_rows: 4 };
  }

  _subscribe() {
    if (this._sub || !this._hass || !this.isConnected || Date.now() < (this._retryAt ?? 0)) return;
    this._sub = this._hass.connection.subscribeMessage((map) => this._update(map), { type: "wisp/map/subscribe" });
    this._sub.catch((err) => {
      this._sub = null;
      this._retryAt = Date.now() + 30000;
      this._error = err?.message || "Wisp is not loaded";
      this._render();
    });
  }

  _update(map) {
    this._map = map;
    this._received = Date.now();
    this._error = null;
    this._track(map, this._received);
    this._render();
  }

  /* Per floor, the last fixes of whoever moves: a step too long or a pause too long starts a new
     trail; a floor nobody moves on any more keeps its trail while it fades. */
  _track(map, now) {
    const trails = (this._trails ??= new Map());
    const seen = new Set();
    for (const p of map.people ?? []) {
      if (!Number.isFinite(p.x) || !Number.isFinite(p.y)) continue;
      const key = p.floor ?? "";
      seen.add(key);
      const fix = { x: p.x, y: p.y, t: now, q: clamp(p.quality ?? 0, 0, 1) };
      let tr = trails.get(key);
      const last = tr?.fixes[tr.fixes.length - 1];
      const d = last ? Math.hypot(fix.x - last.x, fix.y - last.y) : 0;
      if (!last || d > JUMP_M || now - tr.seen > GAP_S * 1000) {
        tr = { fixes: [fix], walked: 0 };
        trails.set(key, tr);
      } else if (d < 0.02) {
        last.q = fix.q; // standing still
      } else {
        tr.fixes.push(fix);
        tr.walked += d;
      }
      Object.assign(tr, { seen: now, gone: null });
      tr.fixes = tr.fixes.filter((f, i, all) => i === all.length - 1 || now - f.t <= TRAIL_S * 1000).slice(-TRAIL_N);
    }
    for (const [key, tr] of trails) if (!seen.has(key) && tr.gone == null) tr.gone = now;
  }

  /* Trails done fading go; a redraw is due when the next one is done. */
  _prune(now) {
    const trails = (this._trails ??= new Map());
    let due = Infinity;
    for (const [key, tr] of trails) {
      if (tr.gone == null) continue;
      const end = tr.gone + FADE_S * 1000;
      if (now >= end) trails.delete(key);
      else due = Math.min(due, end);
    }
    clearTimeout(this._fadeTimer);
    if (due < Infinity) this._fadeTimer = setTimeout(() => this._render(), due - now + 50);
  }

  _note() {
    const hive = this._map?.hive;
    if (!this._map) return { text: "", cls: "", tip: "" };
    if (!hive) return { text: "waiting for the hive", cls: "wait", tip: "No hive report from the nodes yet" };
    const age = hive.age + Math.round((Date.now() - this._received) / 1000);
    const tip = `Hive ${hive.hash}, ${plural(hive.nodes, "node", "nodes")}, heard ${age} s ago`;
    return hive.in_sync ? { text: "in sync", cls: "sync", tip } : { text: "syncing", cls: "wait", tip };
  }

  _render() {
    if (!this._config) return;
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" }).innerHTML = `<style>${STYLE}</style><ha-card><div class="head"><h2></h2><span class="note"></span></div><div class="map"><img class="plan" alt="" hidden><div class="draw"></div></div><div class="foot"><span class="sum"></span><span class="mot"></span></div></ha-card>`;
      this.shadowRoot.querySelector("img.plan").addEventListener("error", (e) => {
        this._badPlan = e.target.getAttribute("src");
        this._render();
      });
    }
    const root = this.shadowRoot;
    root.querySelector("ha-card").classList.toggle("dark", !!this._dark);
    root.querySelector("ha-card").classList.toggle("photo", !!this._config.plan_photo);
    const title = root.querySelector("h2");
    title.textContent = this._config.title ?? "";
    title.hidden = !title.textContent;
    const note = this._note();
    const noteEl = root.querySelector(".note");
    noteEl.className = `note ${note.cls}`;
    noteEl.textContent = note.text;
    noteEl.title = note.tip;
    const map = this._map;
    const foot = root.querySelector(".foot");
    const img = root.querySelector("img.plan");
    const pick = map && !this._error ? pickFloor(map, this._config.floor) : {};
    img.hidden = true;
    if (!map || map.nodes.length < 2 || pick.missing != null) {
      const [lead, text] = this._error
        ? ["Wisp is not available", this._error]
        : !map
          ? ["Reading the grid", ""]
          : pick.missing != null
            ? ["No such floor", `Wisp has no floor “${pick.missing}” with nodes. Its floors: ${pick.floors.map((f) => f.name).join(", ")}.`]
            : ["Nothing to draw yet", "The map needs two or more Wisp nodes. Add them under Settings, Devices and services, Wisp."];
      root.querySelector(".draw").innerHTML = `<div class="empty">${EMPTY_FEET}<p class="lead">${esc(lead)}</p>${text ? `<p>${esc(text)}</p>` : ""}</div>`;
      foot.hidden = true;
      return;
    }
    const now = Date.now();
    this._prune(now);
    const { svg, summary, motion, plan } = draw(pick.map, this._config, pick.floor, this._trails, now);
    root.querySelector(".draw").innerHTML = svg;
    if (plan?.url) {  // a plan without an image is a grid, drawn in the svg
      if (img.getAttribute("src") !== plan.url) img.setAttribute("src", plan.url);
      const pct = (v) => `${(100 * v).toFixed(3)}%`;
      Object.assign(img.style, { left: pct(plan.left), top: pct(plan.top), width: pct(plan.width), height: pct(plan.height), transform: plan.transform });
      img.hidden = false;
    }
    foot.hidden = false;
    foot.querySelector(".sum").textContent = summary + (plan?.url && this._badPlan === plan.url ? ". The floor plan image could not be loaded" : "");
    foot.querySelector(".mot").textContent = motion;
  }
}

const STYLE = `
  :host { display: block; }
  ha-card {
    --wisp-paper-1: #f6e9c4; --wisp-paper-2: #ead39c; --wisp-paper-3: #c9a464;
    --wisp-ink: #4b2e16; --wisp-hot: #a3301f; --wisp-mark: #f3e3b7;
    --wisp-fold: rgba(107, 67, 32, .13); --wisp-burn: rgba(122, 77, 31, .32); --wisp-edge: rgba(107, 67, 32, .38);
    --wisp-serif: "Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif;
    display: block; position: relative; overflow: hidden; color: var(--wisp-ink); font-family: var(--wisp-serif);
    border: 1px solid var(--wisp-edge);
    background:
      linear-gradient(90deg, transparent calc(33.3% - 1px), var(--wisp-fold) calc(33.3% - 1px), var(--wisp-fold) 33.3%, transparent 33.3%,
        transparent calc(66.6% - 1px), var(--wisp-fold) calc(66.6% - 1px), var(--wisp-fold) 66.6%, transparent 66.6%),
      linear-gradient(0deg, transparent calc(50% - 1px), var(--wisp-fold) calc(50% - 1px), var(--wisp-fold) 50%, transparent 50%),
      radial-gradient(ellipse at 50% 42%, var(--wisp-paper-1) 0%, var(--wisp-paper-2) 62%, var(--wisp-paper-3) 100%);
    box-shadow: var(--ha-card-box-shadow, none), inset 0 0 42px var(--wisp-burn);
  }
  ha-card.dark {
    --wisp-paper-1: #43362a; --wisp-paper-2: #33291e; --wisp-paper-3: #211a13;
    --wisp-ink: #ead6ab; --wisp-hot: #f0905e; --wisp-mark: #3b2f22;
    --wisp-fold: rgba(234, 214, 171, .07); --wisp-burn: rgba(0, 0, 0, .5); --wisp-edge: rgba(234, 214, 171, .2);
  }
  .head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; padding: 14px 18px 6px; }
  h2 { margin: 0; font: 600 1.4rem/1.2 var(--wisp-serif); letter-spacing: .03em; font-variant: small-caps; color: var(--wisp-ink); }
  h2[hidden] { display: block; visibility: hidden; }
  .note { font-style: italic; font-size: .875rem; opacity: .8; display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
  .note::before { content: ""; width: 7px; height: 7px; border-radius: 50%; border: 1.5px solid currentColor; }
  .note:empty::before { display: none; }
  .note.sync::before { background: currentColor; }
  .note.wait::before { animation: wisp-blink 1.6s ease-in-out infinite; }
  .map { position: relative; margin: 4px 14px 0; border: 1.5px solid color-mix(in srgb, var(--wisp-ink) 55%, transparent);
         outline: 1px solid color-mix(in srgb, var(--wisp-ink) 25%, transparent); outline-offset: 3px; }
  .map svg { display: block; width: 100%; height: auto; }
  .draw { position: relative; }
  /* The plan inked onto the parchment: white turns to paper; in the dark, light lines on it */
  img.plan { position: absolute; display: block; object-fit: fill; pointer-events: none; mix-blend-mode: multiply; opacity: .9; }
  img.plan[hidden] { display: none; }
  ha-card.dark img.plan { filter: invert(1) hue-rotate(180deg); mix-blend-mode: screen; opacity: .7; }
  ha-card.dark.photo img.plan { filter: brightness(.55) saturate(.8); mix-blend-mode: normal; opacity: .85; }
  .edge { fill: none; stroke: var(--wisp-ink); stroke-width: 1; opacity: .35; }
  .grid { fill: none; stroke: var(--wisp-ink); stroke-width: .6; opacity: .14; }
  .scale path { fill: none; stroke: var(--wisp-ink); stroke-width: 1.5; stroke-linecap: square; }
  .scale text { font-size: 10px; font-style: italic; }
  /* Rooms: a wash of watercolour and a pencilled wall, so the plan under them stays readable */
  .room .wash { opacity: .09; }
  .room .wash rect { fill: hsl(var(--hue) 50% 48%); }
  .room .wall { fill: none; stroke: hsl(var(--hue) 40% 30%); stroke-width: 1.2; stroke-linecap: square; opacity: .4; }
  .room.here .wash { opacity: .17; }
  .bare .room .wash { opacity: .16; }
  .bare .room .wall { stroke-width: 1.5; opacity: .6; }
  .bare .room.here .wash { opacity: .26; }
  ha-card.dark .room .wash rect { fill: hsl(var(--hue) 45% 62%); }
  ha-card.dark .room .wall { stroke: hsl(var(--hue) 50% 76%); }
  .room-name { font-size: 10px; font-style: italic; text-anchor: start; opacity: .75; stroke-width: 3px; }
  .link { fill: none; stroke-linecap: round; }
  .link.ap { stroke-dasharray: 5 4; }
  .link.unknown { stroke-dasharray: 1 4; }
  .glow { fill: none; stroke: var(--wisp-hot); stroke-linecap: round; opacity: .1; animation: wisp-glow ${PULSE_S}s ease-in-out infinite; }
  /* Footprints cut out of the paper, so they read on a lit link */
  .print, .person path { fill: color-mix(in srgb, var(--wisp-hot) 70%, var(--wisp-ink)); stroke: var(--wisp-paper-1); stroke-width: 3; paint-order: stroke; }
  .print.new { animation: wisp-in .3s ease-out both; }
  .trail.gone, .walker.gone { animation: wisp-gone ${FADE_S}s linear forwards; }
  .ring { fill: var(--wisp-mark); stroke: var(--wisp-ink); stroke-width: 2.4; }
  .dot { fill: var(--wisp-ink); }
  .waves { fill: none; stroke: var(--wisp-ink); stroke-width: 1.5; stroke-linecap: round; opacity: .75; }
  .ap .ring { stroke-width: 1.8; }
  .person path { fill: var(--wisp-hot); }
  .person .halo { fill: var(--wisp-hot); fill-opacity: .12; stroke: var(--wisp-hot); stroke-opacity: .35; stroke-width: 1; stroke-dasharray: 3 3; }
  .node.off { opacity: .5; }
  .node.off .ring, .node.loose .ring { stroke-dasharray: 2.5 2; }
  text { font-family: var(--wisp-serif); fill: var(--wisp-ink); text-anchor: middle;
         paint-order: stroke; stroke: var(--wisp-paper-1); stroke-width: 3.5px; stroke-linejoin: round; }
  .node-label { font-size: 13px; font-weight: 600; }
  .node-label.off { font-weight: 400; font-style: italic; opacity: .6; }
  .ap-label { font-size: 11px; font-style: italic; opacity: .85; }
  .foot { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 2px 12px; padding: 8px 18px 14px;
          font-size: .8125rem; font-style: italic; opacity: .8; }
  .foot[hidden] { display: none; }
  .empty { display: grid; justify-items: center; gap: 4px; padding: 28px 20px 30px; text-align: center; }
  .empty .feet { width: 56px; height: 46px; fill: var(--wisp-ink); opacity: .55; margin-bottom: 6px; }
  .empty p { margin: 0; max-width: 34ch; font-size: .875rem; line-height: 1.45; opacity: .8; }
  .empty .lead { font-size: 1.05rem; font-style: italic; opacity: 1; }
  @keyframes wisp-glow { 50% { opacity: .3; } }
  @keyframes wisp-in { from { opacity: 0; } }
  @keyframes wisp-gone { to { opacity: 0; } }
  @keyframes wisp-blink { 50% { opacity: .25; } }
  @media (prefers-reduced-motion: reduce) {
    .glow { animation: none; opacity: .2; }
    .print.new { animation: none; }
    .note.wait::before { animation: none; }
  }
`;

if (!customElements.get("wisp-map-card")) customElements.define("wisp-map-card", WispMapCard);
window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "wisp-map-card")) {
  window.customCards.push({
    type: "wisp-map-card",
    name: "Wisp map",
    description: "Live map of the Wisp grid: nodes, access points, links, and footprints where someone moves.",
    preview: true,
    documentationURL: "https://github.com/albert-canfield/wisp",
  });
}
