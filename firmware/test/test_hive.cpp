// Host tests for the hive (core_hive.h) and the layout solver (core_layout.h).
// Run: firmware/test/run.sh
#include <cmath>
#include <cstdio>
#include <random>
#include <vector>

#include "core_hive.h"
#include "core_layout.h"

static int failures = 0;
#define CHECK(cond)                                               \
  do {                                                            \
    if (!(cond)) {                                                \
      std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
      failures++;                                                 \
    }                                                             \
  } while (0)

using namespace wisp_core;

static Mac mac_n(int n) { return Mac{{0x44, 0x1b, 0xf6, 0x00, static_cast<uint8_t>(n >> 8), static_cast<uint8_t>(n)}}; }

static void test_row_versions() {
  Hive h(mac_n(1));
  HiveEntry e[2] = {{mac_n(2), -50}, {mac_n(3), -70}};
  CHECK(h.set_own(e, 2, 0));
  const uint16_t v1 = h.own()->version;
  e[0].rssi = -52;  // small wobble: same row
  CHECK(!h.set_own(e, 2, 100));
  CHECK(h.own()->version == v1);
  e[0].rssi = -54;  // 4 dB away from the published -50: new version
  CHECK(h.set_own(e, 2, 200));
  CHECK(h.own()->version == v1 + 1);
  CHECK(h.set_own(e, 1, 300));  // a neighbour left
  CHECK(row_newer(1, 65535) && !row_newer(65535, 1));  // wraparound
}

static void test_rows_and_hash() {
  Hive a(mac_n(1)), b(mac_n(2));
  HiveEntry r3[1] = {{mac_n(1), -60}}, r4[1] = {{mac_n(2), -65}};
  // Same rows, learned in a different order: same hash.
  CHECK(a.on_row(mac_n(3), 5, r3, 1, 0));
  CHECK(a.on_row(mac_n(4), 9, r4, 1, 0));
  CHECK(b.on_row(mac_n(4), 9, r4, 1, 0));
  CHECK(b.on_row(mac_n(3), 5, r3, 1, 0));
  // Own rows differ, so compare after each also learns the other's own row.
  HiveEntry own_a[1] = {{mac_n(2), -40}}, own_b[1] = {{mac_n(1), -41}};
  a.set_own(own_a, 1, 0);
  b.set_own(own_b, 1, 0);
  a.on_row(mac_n(2), b.own()->version, b.own()->entries, b.own()->len, 0);
  b.on_row(mac_n(1), a.own()->version, a.own()->entries, a.own()->len, 0);
  CHECK(a.hash() == b.hash());
  // Older or equal versions are not news; newer ones are.
  CHECK(!a.on_row(mac_n(3), 4, r3, 1, 10));
  CHECK(!a.on_row(mac_n(3), 5, r3, 1, 10));
  const uint32_t before = a.hash();
  CHECK(a.on_row(mac_n(3), 6, r3, 1, 10));
  CHECK(a.hash() != before);
  // A row nobody refreshes for a day goes.
  a.expire(10 + ROW_EXPIRE_MS + 1);
  CHECK(a.find(mac_n(3)) == nullptr && a.own() != nullptr);
}

static void test_row_frame() {
  HiveRow row{};
  row.origin = mac_n(7);
  row.version = 300;
  row.len = 2;
  row.entries[0] = HiveEntry{mac_n(8), -44};
  row.entries[1] = HiveEntry{mac_n(9), -81};
  uint8_t buf[ROW_FRAME_MAX_BYTES];
  const size_t n = encode_row_frame(row, 12, buf, sizeof(buf));
  CHECK(n == ROW_FRAME_HEADER_BYTES + 14);
  Mac origin;
  uint16_t version;
  HiveEntry e[MAX_ROW];
  int count;
  CHECK(decode_row_frame(buf, n, origin, version, e, count));
  CHECK(origin == mac_n(7) && version == 300 && count == 2 && e[1].mac == mac_n(9) && e[1].rssi == -81);
  CHECK(!decode_row_frame(buf, n - 1, origin, version, e, count));
}

// A chain of nodes that only hear their neighbours: relays must carry every row to every node.
static void test_gossip_over_a_chain() {
  const int n = 6;
  std::vector<Hive> hives;
  for (int i = 0; i < n; i++)
    hives.emplace_back(mac_n(10 + i));
  for (int i = 0; i < n; i++) {
    HiveEntry e[2];
    int k = 0;
    if (i > 0)
      e[k++] = HiveEntry{mac_n(10 + i - 1), -55};
    if (i < n - 1)
      e[k++] = HiveEntry{mac_n(10 + i + 1), -57};
    hives[i].set_own(e, k, 0);
  }
  int rounds = 0;
  for (; rounds < 200; rounds++) {
    // Each round every node sends its own row (in its beacon) and one relayed row.
    for (int i = 0; i < n; i++) {
      const HiveRow own = *hives[i].own();
      const HiveRow *relay = hives[i].next_relay();
      HiveRow relayed{};
      if (relay != nullptr)
        relayed = *relay;
      for (int j : {i - 1, i + 1}) {
        if (j < 0 || j >= n)
          continue;
        hives[j].on_row(own.origin, own.version, own.entries, own.len, rounds * 100);
        if (relay != nullptr)
          hives[j].on_row(relayed.origin, relayed.version, relayed.entries, relayed.len, rounds * 100);
      }
    }
    bool same = true;
    for (int i = 1; i < n; i++)
      same = same && hives[i].hash() == hives[0].hash() && hives[i].count() == n;
    if (same)
      break;
  }
  std::printf("  chain of %d: every hive agrees after %d rounds (%.1f s)\n", n, rounds + 1, (rounds + 1) / 10.0);
  CHECK(rounds < 50);
}

// Nodes at known positions; RSSI from the path-loss model plus noise; the layout should give
// back the distances.
static void test_layout() {
  const float pos[5][2] = {{0, 0}, {6, 0}, {6, 4.5f}, {0, 4.5f}, {3, 2}};
  const PathLoss pl;
  std::mt19937 rng(5);
  std::normal_distribution<float> noise(0.0f, 1.5f);
  Hive h1(mac_n(100)), h2(mac_n(101));  // observers outside the layout
  for (int i = 0; i < 5; i++) {
    HiveEntry e[4];
    int k = 0;
    for (int j = 0; j < 5; j++) {
      if (i == j)
        continue;
      const float d = std::hypot(pos[i][0] - pos[j][0], pos[i][1] - pos[j][1]);
      const float rssi = pl.rssi_at_1m - 10.0f * pl.exponent * std::log10(d) + noise(rng);
      e[k++] = HiveEntry{mac_n(1 + j), static_cast<int8_t>(std::lround(rssi))};
    }
    // Same rows reach the two hives in opposite orders.
    h1.on_row(mac_n(1 + i), 1, e, k, 0);
  }
  for (int i = 4; i >= 0; i--) {
    const HiveRow *r = h1.find(mac_n(1 + i));
    h2.on_row(r->origin, r->version, r->entries, r->len, 0);
  }
  static LayoutWorkspace ws;
  LayoutPoint a[MAX_POINTS], b[MAX_POINTS];
  const int na = solve_layout(h1, a, ws), nb = solve_layout(h2, b, ws);
  CHECK(na == 5 && nb == 5);
  bool identical = true;
  for (int i = 0; i < na; i++)
    identical = identical && a[i].mac == b[i].mac && a[i].x == b[i].x && a[i].y == b[i].y;
  CHECK(identical);  // every node computes the same layout

  float err = 0.0f;
  int pairs = 0;
  for (int i = 0; i < 5; i++) {
    for (int j = i + 1; j < 5; j++) {
      const float truth = std::hypot(pos[i][0] - pos[j][0], pos[i][1] - pos[j][1]);
      const float got = std::hypot(a[i].x - a[j].x, a[i].y - a[j].y);
      err += std::fabs(got - truth) / truth;
      pairs++;
    }
  }
  err /= pairs;
  std::printf("  layout of 5 nodes from noisy RSSI: mean distance error %.0f%%\n", 100.0f * err);
  CHECK(err < 0.30f);
  CHECK(a[0].x < 0.0f && std::fabs(a[0].y) < 0.05f && a[1].y >= 0.0f);  // fixed pose

  // Two nodes only: a line.
  Hive two(mac_n(1));
  HiveEntry e1[1] = {{mac_n(2), -50}};
  two.set_own(e1, 1, 0);
  HiveEntry e2[1] = {{mac_n(1), -52}};
  two.on_row(mac_n(2), 1, e2, 1, 0);  // points are nodes with a row; APs are placed later
  LayoutPoint t[MAX_POINTS];
  CHECK(solve_layout(two, t, ws) == 2);
  Hive one(mac_n(1));
  CHECK(solve_layout(one, t, ws) == 0);
}

int main() {
  test_row_versions();
  test_rows_and_hash();
  test_row_frame();
  test_gossip_over_a_chain();
  test_layout();
  if (failures) {
    std::printf("%d hive check(s) failed\n", failures);
    return 1;
  }
  std::printf("All hive tests passed\n");
  return 0;
}
