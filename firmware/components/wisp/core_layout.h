#pragma once
// wisp-core: a layout of the grid's nodes from the hive's rows.
//
// RSSI between two nodes becomes a rough distance (log-distance path loss), both directions
// averaged. Pairs nobody measured get the shortest path through known pairs. Indoors the path
// loss exponent is unknown, and walls and per-board antenna and TX differences bend RSSI away
// from any one model: far pairs come out too far, the distances break the triangle inequality,
// and the least stress layout of a small hive is a straight line. So the solver tries six
// exponents, from about twice the nominal one (distances squeezed together) down to 0.64x, each
// one continuing from the layout of the one before. Classical MDS of the most squeezed distances
// gives a start with no wrong folds; each exponent gets 10 SMACOF steps with relative weights
// (1/d^2: an error of so many dB is the same share of any distance, and near pairs are the
// trustworthy ones). Each fit is scored by its mean square error in dB over the measured pairs;
// of the fits within 0.4 dB^2 of the best, the exponent nearest the nominal one wins, gets 15
// more steps and is scaled to the nominal exponent's metres. On simulated homes with walls and
// per-board offsets (firmware/tools/layout_study.py), against one fixed exponent of 2.7: layouts
// on a line 14% -> 1% (3 nodes 55% -> 7%), median shape error 0.33 -> 0.27.
//
// The result is put in a fixed pose (lowest MAC on the left, second lowest above the axis) and
// rounded to 10 cm, so every node that holds the same rows gets the same layout. Relative only:
// rotation, mirror and scale come from the user's anchors (access points and nodes placed on the
// floor plan).
//
// Same layout on every chip: only + - * / and sqrt, which IEEE 754 rounds the same everywhere
// (built with -ffp-contract=off), no libm pow, exp, log or trigonometry, whose last bits differ;
// fixed step counts and start vectors. A node with no measured pair (just booted, or out of range
// of all) is left out.

#include <algorithm>
#include <cmath>
#include <cstdint>

#include "core_hive.h"

namespace wisp_core {

constexpr int MAX_POINTS = MAX_ROWS;
constexpr int LAYOUT_STAGES = 6;
constexpr int LAYOUT_STAGE_STEPS = 10;
constexpr int LAYOUT_FINAL_STEPS = 15;
constexpr float LAYOUT_MISFIT_TOLERANCE_DB2 = 0.4f;
// Exponent of each stage over the nominal one: 1.25^3 down to 1.25^-2.
constexpr float LAYOUT_STAGE_FACTOR[LAYOUT_STAGES] = {1.953125f, 1.5625f, 1.25f, 1.0f, 0.8f, 0.64f};

struct PathLoss {
  float rssi_at_1m = -45.0f;
  float exponent = 4.0f;  // nominal: typical indoors through walls; the solver tries others around it
};

// 2^x from + - * / only: whole part by repeated doubling, the rest by a degree 7 Taylor series
// on [-0.5, 0.5] (error below 1e-7). Plenty for distances clamped to 0.3 to 40 m.
inline float exp2_portable(float x) {
  x = std::fmax(-30.0f, std::fmin(30.0f, x));
  const float whole = std::floor(x + 0.5f);
  const float f = (x - whole) * 0.69314718f;  // ln 2
  float term = 1.0f, sum = 1.0f;
  for (int k = 1; k <= 7; k++) {
    term = term * f / static_cast<float>(k);
    sum += term;
  }
  const int w = static_cast<int>(whole);
  for (int i = 0; i < w; i++)
    sum *= 2.0f;
  for (int i = 0; i > w; i--)
    sum *= 0.5f;
  return sum;
}

inline float rssi_to_metres(float rssi, const PathLoss &pl = {}) {
  // 10^e = 2^(e log2 10)
  const float d = exp2_portable((pl.rssi_at_1m - rssi) / (10.0f * pl.exponent) * 3.32192809f);
  return std::fmin(40.0f, std::fmax(0.3f, d));
}

struct LayoutPoint {
  Mac mac;
  float x;
  float y;
};

// Scratch (about 6 KB): owned by the caller, so small task stacks are safe.
struct LayoutWorkspace {
  float rssi[MAX_POINTS][MAX_POINTS];  // per pair, both directions averaged
  bool measured[MAX_POINTS][MAX_POINTS];
  float d[MAX_POINTS][MAX_POINTS];   // target distances of the current exponent
  float w[MAX_POINTS][MAX_POINTS];   // their weights
  float sp[MAX_POINTS][MAX_POINTS];  // shortest paths, then classical MDS
  float stage_xy[LAYOUT_STAGES][MAX_POINTS][2];
};

namespace layout_detail {

inline float distance(const float (*xy)[2], int i, int j) {
  const float dx = xy[i][0] - xy[j][0], dy = xy[i][1] - xy[j][1];
  return std::sqrt(dx * dx + dy * dy);
}

// Target distances and weights for one exponent. Measured pairs weigh 1, filled-in pairs 0.2,
// both over d^2.
inline void fill_distances(LayoutWorkspace &ws, int n, const PathLoss &pl) {
  auto &d = ws.d;
  auto &sp = ws.sp;
  float largest = 0.0f;
  for (int i = 0; i < n; i++) {
    d[i][i] = 0.0f;
    for (int j = i + 1; j < n; j++) {
      d[i][j] = d[j][i] = ws.measured[i][j] ? rssi_to_metres(ws.rssi[i][j], pl) : -1.0f;
      if (ws.measured[i][j])
        largest = std::fmax(largest, d[i][j]);
    }
  }
  // Shortest paths for unmeasured pairs (Floyd-Warshall over measured ones).
  for (int i = 0; i < n; i++)
    for (int j = 0; j < n; j++)
      sp[i][j] = d[i][j] < 0 ? INFINITY : d[i][j];
  for (int k = 0; k < n; k++)
    for (int i = 0; i < n; i++)
      for (int j = 0; j < n; j++)
        sp[i][j] = std::fmin(sp[i][j], sp[i][k] + sp[k][j]);
  for (int i = 0; i < n; i++) {
    for (int j = 0; j < n; j++) {
      if (i == j) {
        ws.w[i][j] = 0.0f;
        continue;
      }
      if (d[i][j] < 0)
        d[i][j] = std::isinf(sp[i][j]) ? 1.5f * std::fmax(largest, 1.0f) : sp[i][j];
      ws.w[i][j] = (ws.measured[i][j] ? 1.0f : 0.2f) / (d[i][j] * d[i][j]);
    }
  }
}

// Classical MDS: B = -1/2 J D^2 J, the two largest eigenvalues by power iteration. Noisy
// distances give B negative eigenvalues too, often larger in size than the second positive
// one, so the iteration runs on B + cI (c from Gershgorin: every eigenvalue then >= 0) and
// finds the largest positive ones, never a negative one that would flatten an axis.
inline void classical_mds(LayoutWorkspace &ws, int n, float (*xy)[2]) {
  const auto &d = ws.d;
  auto &bm = ws.sp;  // free once the distances are filled in
  float row_mean[MAX_POINTS] = {};
  float all_mean = 0.0f;
  for (int i = 0; i < n; i++) {
    for (int j = 0; j < n; j++)
      row_mean[i] += d[i][j] * d[i][j] / n;
    all_mean += row_mean[i] / n;
  }
  for (int i = 0; i < n; i++)
    for (int j = 0; j < n; j++)
      bm[i][j] = -0.5f * (d[i][j] * d[i][j] - row_mean[i] - row_mean[j] + all_mean);
  for (int axis = 0; axis < 2; axis++) {
    float shift = 0.0f;
    for (int i = 0; i < n; i++) {
      float r = 0.0f;
      for (int j = 0; j < n; j++)
        r += std::fabs(bm[i][j]);
      shift = std::fmax(shift, r);
    }
    float v[MAX_POINTS];
    for (int i = 0; i < n; i++)
      v[i] = 1.0f + 0.37f * i + 0.11f * axis * (i % 3);  // fixed start: same answer everywhere
    float lambda = 0.0f;
    for (int it = 0; it < 100; it++) {  // a start only: the SMACOF steps below finish it
      float nv[MAX_POINTS] = {};
      float norm = 0.0f;
      for (int i = 0; i < n; i++) {
        for (int j = 0; j < n; j++)
          nv[i] += bm[i][j] * v[j];
        nv[i] += shift * v[i];
        norm += nv[i] * nv[i];
      }
      norm = std::sqrt(norm);
      if (norm < 1e-9f)
        break;
      const float inv = 1.0f / norm;
      for (int i = 0; i < n; i++)
        v[i] = nv[i] * inv;
    }
    for (int i = 0; i < n; i++) {
      float bv = 0.0f;
      for (int j = 0; j < n; j++)
        bv += bm[i][j] * v[j];
      lambda += v[i] * bv;
    }
    const float scale = std::sqrt(std::fmax(lambda, 0.0f));
    for (int i = 0; i < n; i++)
      xy[i][axis] = v[i] * scale;
    if (scale < 1e-3f) {
      // Nothing along this axis (a line, or noise only): a small fixed spread lets the refinement
      // below still find a second dimension, the same way on every node.
      for (int i = 0; i < n; i++)
        xy[i][axis] = 0.05f * static_cast<float>((i * 7) % 5 - 2);
    }
    for (int i = 0; i < n; i++)
      for (int j = 0; j < n; j++)
        bm[i][j] -= lambda * v[i] * v[j];  // deflate
  }
}

// SMACOF refinement (weighted Guttman transform, per point, simplified for small n).
inline void smacof(const LayoutWorkspace &ws, int n, float (*xy)[2], int steps) {
  float inv_wsum[MAX_POINTS] = {};
  for (int i = 0; i < n; i++) {
    float wsum = 0.0f;
    for (int j = 0; j < n; j++)
      wsum += ws.w[i][j];
    inv_wsum[i] = wsum > 0.0f ? 1.0f / wsum : 0.0f;
  }
  for (int it = 0; it < steps; it++) {
    float next[MAX_POINTS][2] = {};
    for (int i = 0; i < n; i++) {
      for (int j = i + 1; j < n; j++) {
        const float wij = ws.w[i][j];
        if (wij == 0.0f)
          continue;
        const float dx = xy[i][0] - xy[j][0], dy = xy[i][1] - xy[j][1];
        const float ratio = ws.d[i][j] / std::fmax(std::sqrt(dx * dx + dy * dy), 1e-4f);
        next[i][0] += wij * (xy[j][0] + ratio * dx);
        next[i][1] += wij * (xy[j][1] + ratio * dy);
        next[j][0] += wij * (xy[i][0] - ratio * dx);
        next[j][1] += wij * (xy[i][1] - ratio * dy);
      }
    }
    for (int i = 0; i < n; i++) {
      xy[i][0] = next[i][0] * inv_wsum[i];
      xy[i][1] = next[i][1] * inv_wsum[i];
    }
  }
}

// How well a layout fits the measured pairs: mean square of 10 n log10(layout distance / target
// distance) in dB, after the best scale. The log from 2 (r - 1) / (r + 1), close enough for
// ratios of 0.5 to 2.
inline float misfit_db2(const LayoutWorkspace &ws, int n, const float (*xy)[2], float exponent) {
  float sr = 0.0f, srr = 0.0f;
  int pairs = 0;
  for (int i = 0; i < n; i++) {
    for (int j = i + 1; j < n; j++) {
      if (!ws.measured[i][j])
        continue;
      const float r = distance(xy, i, j) / ws.d[i][j];
      sr += r;
      srr += r * r;
      pairs++;
    }
  }
  if (pairs == 0 || !(srr > 0.0f))
    return INFINITY;
  const float scale = sr / srr;
  float sum = 0.0f;
  for (int i = 0; i < n; i++) {
    for (int j = i + 1; j < n; j++) {
      if (!ws.measured[i][j])
        continue;
      const float r = scale * distance(xy, i, j) / ws.d[i][j];
      const float db = 8.6858896f * exponent * (r - 1.0f) / (r + 1.0f);  // 20 / ln 10
      sum += db * db;
    }
  }
  return sum / pairs;
}

}  // namespace layout_detail

// Fills out (sorted by MAC) and returns the number of points; 0 when fewer than two nodes.
inline int solve_layout(const Hive &hive, LayoutPoint *out, LayoutWorkspace &ws, const PathLoss &pl = {}) {
  auto heard = [&hive](const Mac &rx, const Mac &tx, float &rssi) {
    const HiveRow *row = hive.find(rx);
    if (row == nullptr)
      return false;
    for (int k = 0; k < row->len; k++) {
      if (row->entries[k].mac == tx) {
        rssi = row->entries[k].rssi;
        return true;
      }
    }
    return false;
  };
  // Nodes: every row's origin with at least one measured pair, in MAC order so every node builds
  // the same matrix.
  Mac all[MAX_POINTS];
  int count = 0;
  for (int i = 0; i < hive.count() && count < MAX_POINTS; i++)
    all[count++] = hive.row(i).origin;
  std::sort(all, all + count);
  Mac macs[MAX_POINTS];
  int n = 0;
  for (int i = 0; i < count; i++) {
    bool linked = false;
    float unused;
    for (int j = 0; j < count && !linked; j++)
      linked = i != j && (heard(all[i], all[j], unused) || heard(all[j], all[i], unused));
    if (linked)
      macs[n++] = all[i];
  }
  if (n < 2)
    return 0;

  for (int i = 0; i < n; i++) {
    ws.measured[i][i] = false;
    ws.rssi[i][i] = 0.0f;
    for (int j = i + 1; j < n; j++) {
      float a = 0, b = 0;
      const bool ha = heard(macs[i], macs[j], a), hb = heard(macs[j], macs[i], b);
      ws.measured[i][j] = ws.measured[j][i] = ha || hb;
      ws.rssi[i][j] = ws.rssi[j][i] = ha && hb ? 0.5f * (a + b) : (ha ? a : b);
    }
  }

  // Squeezed first, each exponent continuing from the last layout.
  float xy[MAX_POINTS][2] = {};
  float misfit[LAYOUT_STAGES];
  float best = INFINITY;
  PathLoss stage = pl;
  for (int s = 0; s < LAYOUT_STAGES; s++) {
    stage.exponent = pl.exponent * LAYOUT_STAGE_FACTOR[s];
    layout_detail::fill_distances(ws, n, stage);
    if (s == 0)
      layout_detail::classical_mds(ws, n, xy);
    layout_detail::smacof(ws, n, xy, LAYOUT_STAGE_STEPS);
    misfit[s] = layout_detail::misfit_db2(ws, n, xy, stage.exponent);
    best = std::fmin(best, misfit[s]);
    for (int i = 0; i < n; i++) {
      ws.stage_xy[s][i][0] = xy[i][0];
      ws.stage_xy[s][i][1] = xy[i][1];
    }
  }
  // Of the fits about as good as the best, the one nearest the nominal exponent.
  int pick = -1;
  for (int s = 0; s < LAYOUT_STAGES; s++) {
    if (!(misfit[s] <= best + LAYOUT_MISFIT_TOLERANCE_DB2))
      continue;
    if (pick < 0 || std::fabs(LAYOUT_STAGE_FACTOR[s] - 1.0f) < std::fabs(LAYOUT_STAGE_FACTOR[pick] - 1.0f))
      pick = s;
  }
  if (pick < 0)
    pick = 3;  // every fit failed (all points on one spot): the nominal exponent
  stage.exponent = pl.exponent * LAYOUT_STAGE_FACTOR[pick];
  layout_detail::fill_distances(ws, n, stage);
  for (int i = 0; i < n; i++) {
    xy[i][0] = ws.stage_xy[pick][i][0];
    xy[i][1] = ws.stage_xy[pick][i][1];
  }
  layout_detail::smacof(ws, n, xy, LAYOUT_FINAL_STEPS);

  // Metres of the nominal exponent: the scale that best matches its distances (relative error).
  float sr = 0.0f, srr = 0.0f;
  for (int i = 0; i < n; i++) {
    for (int j = i + 1; j < n; j++) {
      if (!ws.measured[i][j])
        continue;
      const float r = layout_detail::distance(xy, i, j) / rssi_to_metres(ws.rssi[i][j], pl);
      sr += r;
      srr += r * r;
    }
  }
  if (srr > 0.0f) {
    const float k = sr / srr;
    for (int i = 0; i < n; i++) {
      xy[i][0] *= k;
      xy[i][1] *= k;
    }
  }

  // Fixed pose: centred, lowest MAC on the left, second lowest above the axis.
  float cx = 0.0f, cy = 0.0f;
  for (int i = 0; i < n; i++) {
    cx += xy[i][0] / n;
    cy += xy[i][1] / n;
  }
  for (int i = 0; i < n; i++) {
    xy[i][0] -= cx;
    xy[i][1] -= cy;
  }
  // Rotate the lowest MAC onto the negative x axis: cos and sin straight from its coordinates.
  const float r0 = std::sqrt(xy[0][0] * xy[0][0] + xy[0][1] * xy[0][1]);
  const float c = r0 > 1e-6f ? -xy[0][0] / r0 : 1.0f, s = r0 > 1e-6f ? xy[0][1] / r0 : 0.0f;
  for (int i = 0; i < n; i++) {
    const float x = xy[i][0] * c - xy[i][1] * s;
    const float y = xy[i][0] * s + xy[i][1] * c;
    xy[i][0] = x;
    xy[i][1] = y;
  }
  if (n > 1 && xy[1][1] < 0.0f) {
    for (int i = 0; i < n; i++)
      xy[i][1] = -xy[i][1];
  }
  for (int i = 0; i < n; i++)
    out[i] = LayoutPoint{macs[i], std::round(xy[i][0] * 10.0f) / 10.0f, std::round(xy[i][1] * 10.0f) / 10.0f};
  return n;
}

}  // namespace wisp_core
