#pragma once
// wisp-core: a layout of the grid's nodes from the hive's rows.
//
// RSSI between two nodes becomes a rough distance (log-distance path loss), both directions
// averaged. Pairs nobody measured get the shortest path through known pairs. Classical MDS gives
// a start with no wrong folds, a few SMACOF steps refine it, and the result is put in a fixed
// pose (lowest MAC on the left, second lowest above the axis) and rounded to 10 cm, so every node
// that holds the same rows gets the same layout. Relative only: rotation, mirror and scale come
// from the user's anchors (access points and nodes placed on the floor plan).

#include <algorithm>
#include <cmath>
#include <cstdint>

#include "core_hive.h"

namespace wisp_core {

constexpr int MAX_POINTS = MAX_ROWS;

struct PathLoss {
  float rssi_at_1m = -45.0f;
  float exponent = 2.7f;
};

inline float rssi_to_metres(float rssi, const PathLoss &pl = {}) {
  const float d = std::pow(10.0f, (pl.rssi_at_1m - rssi) / (10.0f * pl.exponent));
  return std::fmin(40.0f, std::fmax(0.3f, d));
}

struct LayoutPoint {
  Mac mac;
  float x;
  float y;
};

// Scratch matrices (about 5 KB): owned by the caller, so small task stacks are safe.
struct LayoutWorkspace {
  float d[MAX_POINTS][MAX_POINTS];
  float w[MAX_POINTS][MAX_POINTS];
  float sp[MAX_POINTS][MAX_POINTS];
  float bm[MAX_POINTS][MAX_POINTS];
};

// Fills out (sorted by MAC) and returns the number of points; 0 when fewer than two nodes.
inline int solve_layout(const Hive &hive, LayoutPoint *out, LayoutWorkspace &ws, const PathLoss &pl = {},
                        int iterations = 40) {
  auto &d = ws.d;
  auto &w = ws.w;
  auto &sp = ws.sp;
  auto &bm = ws.bm;
  // Nodes: every row's origin, in MAC order so every node builds the same matrix.
  Mac macs[MAX_POINTS];
  int n = 0;
  for (int i = 0; i < hive.count() && n < MAX_POINTS; i++)
    macs[n++] = hive.row(i).origin;
  if (n < 2)
    return 0;
  std::sort(macs, macs + n);

  // Distances and weights: measured pairs weigh 1, filled-in pairs 0.2.
  const float unknown = -1.0f;
  for (int i = 0; i < n; i++) {
    for (int j = 0; j < n; j++) {
      d[i][j] = i == j ? 0.0f : unknown;
      w[i][j] = 0.0f;
    }
  }
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
  float largest = 0.0f;
  for (int i = 0; i < n; i++) {
    for (int j = i + 1; j < n; j++) {
      float a = 0, b = 0;
      const bool ha = heard(macs[i], macs[j], a), hb = heard(macs[j], macs[i], b);
      if (!ha && !hb)
        continue;
      const float rssi = ha && hb ? 0.5f * (a + b) : (ha ? a : b);
      d[i][j] = d[j][i] = rssi_to_metres(rssi, pl);
      w[i][j] = w[j][i] = 1.0f;
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
      if (i != j && d[i][j] < 0) {
        d[i][j] = std::isinf(sp[i][j]) ? 1.5f * std::fmax(largest, 1.0f) : sp[i][j];
        w[i][j] = 0.2f;
      }
    }
  }

  // Classical MDS: B = -1/2 J D^2 J, two leading eigenvectors by power iteration.
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
  float xy[MAX_POINTS][2] = {};
  for (int axis = 0; axis < 2; axis++) {
    float v[MAX_POINTS];
    for (int i = 0; i < n; i++)
      v[i] = 1.0f + 0.37f * i + 0.11f * axis * (i % 3);  // fixed start: same answer everywhere
    float lambda = 0.0f;
    for (int it = 0; it < 100; it++) {
      float nv[MAX_POINTS] = {};
      float norm = 0.0f;
      for (int i = 0; i < n; i++) {
        for (int j = 0; j < n; j++)
          nv[i] += bm[i][j] * v[j];
        norm += nv[i] * nv[i];
      }
      norm = std::sqrt(norm);
      if (norm < 1e-9f)
        break;
      for (int i = 0; i < n; i++)
        v[i] = nv[i] / norm;
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
    for (int i = 0; i < n; i++)
      for (int j = 0; j < n; j++)
        bm[i][j] -= lambda * v[i] * v[j];  // deflate
  }

  // SMACOF refinement (weighted Guttman transform, simplified for small n).
  for (int it = 0; it < iterations; it++) {
    float next[MAX_POINTS][2] = {};
    for (int i = 0; i < n; i++) {
      float wsum = 0.0f;
      for (int j = 0; j < n; j++) {
        if (i == j || w[i][j] == 0.0f)
          continue;
        const float dx = xy[i][0] - xy[j][0], dy = xy[i][1] - xy[j][1];
        const float dist = std::fmax(std::sqrt(dx * dx + dy * dy), 1e-4f);
        const float ratio = d[i][j] / dist;
        next[i][0] += w[i][j] * (xy[j][0] + ratio * dx);
        next[i][1] += w[i][j] * (xy[j][1] + ratio * dy);
        wsum += w[i][j];
      }
      if (wsum > 0.0f) {
        next[i][0] /= wsum;
        next[i][1] /= wsum;
      }
    }
    for (int i = 0; i < n; i++) {
      xy[i][0] = next[i][0];
      xy[i][1] = next[i][1];
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
  const float angle = std::atan2(xy[0][1], xy[0][0]);
  const float rot = 3.14159265f - angle;
  const float c = std::cos(rot), s = std::sin(rot);
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
