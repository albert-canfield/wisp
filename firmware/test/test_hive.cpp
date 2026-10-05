// Host tests for the hive (core_hive.h) and the layout solver (core_layout.h).
// Run: firmware/test/run.sh
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <random>
#include <vector>

#include "core_hive.h"
#include "core_hive_report.h"
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
  CHECK(h.set_own(e, 2, 0));  // the first view is published at once
  const uint16_t v1 = h.own()->version;
  // Someone walking through the link: readings swing by 15 dB for 5 s. The averaged row
  // barely moves: no new version.
  uint32_t t = 0;
  for (int i = 0; i < 50; i++) {
    e[0].rssi = static_cast<int8_t>(i % 2 ? -60 : -45);
    CHECK(!h.set_own(e, 2, t += 100));
  }
  CHECK(h.own()->version == v1);
  // A lasting change of 6 dB: once the average has moved 3 dB, a new version, but not within a
  // minute of the last one.
  e[0].rssi = -56;
  uint32_t when = 0;
  for (int i = 0; i < 1200 && when == 0; i++) {
    t += 100;
    if (h.set_own(e, 2, t))
      when = t;
  }
  CHECK(when >= ROW_MIN_CHANGE_MS && h.own()->version == v1 + 1);
  CHECK(h.own()->entries[0].rssi <= -53 && h.own()->entries[1].rssi == -70);
  CHECK(h.set_own(e, 1, t + 100));  // a neighbour left: at once
  CHECK(h.own()->version == v1 + 2);
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
  row.heard_ms = 1000;
  const size_t n = encode_row_frame(row, 12, 61000, buf, sizeof(buf));
  CHECK(n == ROW_FRAME_HEADER_BYTES + 14);
  Mac origin;
  uint16_t version;
  uint32_t age_ms;
  HiveEntry e[MAX_ROW];
  int count;
  CHECK(decode_row_frame(buf, n, origin, version, age_ms, e, count));
  CHECK(origin == mac_n(7) && version == 300 && count == 2 && e[1].mac == mac_n(9) && e[1].rssi == -81);
  CHECK(age_ms == 60000);  // last heard directly 60 s before it was sent
  CHECK(!decode_row_frame(buf, n - 1, origin, version, age_ms, e, count));
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

// After a reboot a node's row counter restarts; the others hold its old, higher version and would
// ignore the new rows. Hearing its own old row relayed back, it takes that row back, version and
// readings: nothing changes for the others, and its averages go on from where they were.
static void test_rebooted_node_gets_back_in_sync() {
  Hive a(mac_n(1)), b(mac_n(2));
  HiveEntry ea[1] = {{mac_n(2), -50}}, eb[1] = {{mac_n(1), -52}};
  a.set_own(ea, 1, 0);
  for (int i = 0; i < 300; i++) {  // B's row has moved on a lot
    eb[0].rssi = static_cast<int8_t>(i % 2 ? -52 : -60);
    b.set_own(eb, 1, 0);
  }
  a.on_row(mac_n(2), b.own()->version, b.own()->entries, 1, 0);
  b.on_row(mac_n(1), a.own()->version, a.own()->entries, 1, 0);
  CHECK(a.hash() == b.hash());
  Hive b2(mac_n(2));  // B reboots
  b2.set_own(eb, 1, 1000);
  CHECK(!a.on_row(mac_n(2), b2.own()->version, b2.own()->entries, 1, 1000));  // looks old to A
  const HiveRow *old = a.find(mac_n(2));
  CHECK(b2.on_row(mac_n(2), old->version, old->entries, old->len, 1100, 0));  // A relays B's old row
  CHECK(b2.own()->version == old->version && b2.own()->entries[0].rssi == old->entries[0].rssi);
  b2.on_row(mac_n(1), a.own()->version, a.own()->entries, 1, 1200);
  CHECK(a.hash() == b2.hash());  // in sync, and A's view of B never changed
  CHECK(!b2.set_own(eb, 1, 1300) && b2.own()->version == old->version);  // no jump in its readings
}

// A node that left: the two that remain keep relaying its row to each other, but the age they
// pass along keeps growing, so the row still expires after a day. A node that is alive but only
// heard through a relay stays.
static void test_departed_rows_expire_despite_relays() {
  Hive a(mac_n(1)), b(mac_n(2));
  HiveEntry ec[1] = {{mac_n(1), -60}};
  a.on_row(mac_n(3), 7, ec, 1, 0);  // C heard directly at t = 0, then gone
  b.on_row(mac_n(3), 7, ec, 1, 0);
  HiveEntry ed[1] = {{mac_n(2), -60}};
  uint32_t now = 0;
  // Steps that are not whole age units (4 s): each relay must round the age up, or two nodes
  // relaying C's row to each other keep it young for ever.
  for (uint32_t step = 1; now < 26u * 3600 * 1000; step++) {
    now = step * 1300u;
    b.on_row(mac_n(4), 9, ed, 1, now);  // D is alive, heard directly by B only
    // Each relays the rows it holds to the other, with their ages.
    for (Hive *from : {&a, &b}) {
      Hive *to = from == &a ? &b : &a;
      for (int i = 0; i < from->count(); i++) {
        const HiveRow &r = from->row(i);
        uint8_t buf[ROW_FRAME_MAX_BYTES];
        const size_t n = encode_row_frame(r, 0, now, buf, sizeof(buf));
        Mac o;
        uint16_t v;
        uint32_t age;
        HiveEntry e[MAX_ROW];
        int k;
        if (decode_row_frame(buf, n, o, v, age, e, k))
          to->on_row(o, v, e, k, now, age);
      }
    }
    a.expire(now);
    b.expire(now);
  }
  CHECK(a.find(mac_n(3)) == nullptr && b.find(mac_n(3)) == nullptr);  // C expired everywhere
  CHECK(a.find(mac_n(4)) != nullptr);  // D, alive, stays at A through B's relays
}

// A full hive makes room for a newcomer only in place of a missing node's row.
static void test_full_hive_makes_room() {
  Hive h(mac_n(1));
  HiveEntry e[1] = {{mac_n(1), -60}};
  CHECK(h.own() != nullptr && h.count() == 1);  // own row reserved from the start
  for (int i = 0; i < MAX_ROWS - 1; i++)
    CHECK(h.on_row(mac_n(10 + i), 1, e, 1, 0));
  CHECK(h.count() == MAX_ROWS);
  CHECK(h.set_own(e, 1, 0));  // a full hive still takes this node's own row
  CHECK(!h.on_row(mac_n(99), 1, e, 1, 60000));  // everyone heard within ROW_EVICT_MS: refused
  uint32_t now = 0;
  for (; now <= ROW_EVICT_MS + 60000; now += 30000) {
    for (int i = 1; i < MAX_ROWS - 1; i++)  // all but mac_n(10) keep being heard
      h.on_row(mac_n(10 + i), 1, e, 1, now);
  }
  CHECK(!h.on_row(mac_n(99), 1, e, 1, now, ROW_EVICT_MS + 120000));  // a newcomer staler than the stalest
  CHECK(h.on_row(mac_n(99), 1, e, 1, now));
  CHECK(h.find(mac_n(99)) != nullptr && h.find(mac_n(10)) == nullptr && h.count() == MAX_ROWS);
  CHECK(!h.on_row(mac_n(98), 1, e, 1, now, ROW_EXPIRE_MS));  // a relay older than the expiry
}

// After two quick restarts the others may hold this node's old row at the very version it
// reached again: other readings at the same version are taken back too, at most every 10 s.
static void test_stale_copy_of_own_row() {
  Hive a(mac_n(1));
  HiveEntry fresh[1] = {{mac_n(2), -50}}, old[1] = {{mac_n(2), -85}};
  a.set_own(fresh, 1, 0);
  const uint16_t v = a.own()->version;
  CHECK(!a.on_row(mac_n(1), v, fresh, 1, 1000));  // its own row relayed back as it is: nothing
  CHECK(a.on_row(mac_n(1), v, old, 1, 2000));      // same version, other readings: taken back
  CHECK(a.own()->version == v && a.own()->entries[0].rssi == -85 && a.self_jumps() == 1);
  CHECK(!a.on_row(mac_n(1), static_cast<uint16_t>(v + 5), old, 1, 5000));  // within 10 s: waits
  CHECK(a.on_row(mac_n(1), static_cast<uint16_t>(v + 5), old, 1, 13000));
  CHECK(a.own()->version == static_cast<uint16_t>(v + 5) && a.self_jumps() == 2);
}

// Random floors with noisy RSSI: the layout must keep two dimensions (it used to collapse onto
// a line when a negative eigenvalue outweighed the second positive one).
static void test_layout_keeps_two_dimensions() {
  static LayoutWorkspace ws;
  const PathLoss pl;
  for (const int n : {6, 10, 16}) {
    std::mt19937 rng(101 + n);
    std::uniform_real_distribution<float> ux(0.0f, 15.0f), uy(0.0f, 10.0f);
    std::normal_distribution<float> noise(0.0f, 4.0f);
    int flat = 0;
    const int runs = 200;
    for (int r = 0; r < runs; r++) {
      float pos[MAX_POINTS][2];
      for (int i = 0; i < n; i++) {
        pos[i][0] = ux(rng);
        pos[i][1] = uy(rng);
      }
      Hive h(mac_n(500));
      for (int i = 0; i < n; i++) {
        HiveEntry e[MAX_ROW];
        int k = 0;
        for (int j = 0; j < n && k < MAX_ROW; j++) {
          if (i == j)
            continue;
          const float d = std::fmax(0.5f, std::hypot(pos[i][0] - pos[j][0], pos[i][1] - pos[j][1]));
          const float rssi = pl.rssi_at_1m - 10.0f * pl.exponent * std::log10(d) + noise(rng);
          e[k++] = HiveEntry{mac_n(1 + j), static_cast<int8_t>(std::lround(std::fmax(-120.0f, rssi)))};
        }
        h.on_row(mac_n(1 + i), 1, e, k, 0);
      }
      LayoutPoint out[MAX_POINTS];
      const int got = solve_layout(h, out, ws);
      float spread = 0.0f;
      for (int i = 0; i < got; i++)
        spread = std::fmax(spread, std::fabs(out[i].y));
      flat += got == n && spread < 0.15f;
    }
    std::printf("  %d nodes, 4 dB noise: %d of %d layouts flat\n", n, flat, runs);
    CHECK(flat <= runs / 100);
  }
}

// More rows than one packet holds: reports starting from different rows get them all out.
static void test_hive_report_rotates_rows() {
  Hive h(mac_n(1));
  HiveEntry e[MAX_ROW];
  for (int k = 0; k < MAX_ROW; k++)
    e[k] = HiveEntry{mac_n(300 + k), -60};
  h.set_own(e, MAX_ROW, 0);
  for (int i = 0; i < MAX_ROWS - 1; i++)
    h.on_row(mac_n(10 + i), 1, e, MAX_ROW, 0);
  std::vector<int> seen(MAX_ROWS, 0);
  int first = 0;
  for (int report = 0; report < 3; report++) {  // each report starts where the last one stopped
    uint8_t buf[HIVE_REPORT_MAX];
    const size_t n = encode_hive_report(h.self(), 1, h.hash(), true, nullptr, 0, h, buf, sizeof(buf), first);
    CHECK(n > 0 && n <= sizeof(buf) && (buf[22] & HIVE_FLAG_ROWS_TRUNCATED));
    size_t pos = HIVE_REPORT_HEADER_BYTES;
    for (int r = 0; r < buf[24]; r++) {
      Mac origin = Mac::from(buf + pos);
      for (int i = 0; i < h.count(); i++)
        seen[i] += h.row(i).origin == origin;
      pos += 9 + 7 * static_cast<size_t>(buf[pos + 8]);
    }
    CHECK(pos == n);
    first += buf[24];
  }
  bool all = true;
  for (int i = 0; i < h.count(); i++)
    all = all && seen[i] > 0;
  CHECK(all);
}

static void test_portable_exp2() {
  float worst = 0.0f;
  for (float x = -12.0f; x <= 12.0f; x += 0.01f)
    worst = std::fmax(worst, std::fabs(exp2_portable(x) / std::exp2(x) - 1.0f));
  CHECK(worst < 1e-6f);
  CHECK(std::fabs(rssi_to_metres(-45.0f) - 1.0f) < 1e-6f && std::fabs(rssi_to_metres(-85.0f) - 10.0f) < 1e-5f);
  CHECK(std::fabs(rssi_to_metres(-72.0f, PathLoss{-45.0f, 2.7f}) - 10.0f) < 1e-5f);
}

// Second over first singular value of the centred layout: 0 on a line, 1 for a round cloud.
static float layout_flatness(const LayoutPoint *p, int n) {
  float cx = 0, cy = 0;
  for (int i = 0; i < n; i++) {
    cx += p[i].x / n;
    cy += p[i].y / n;
  }
  float sxx = 0, syy = 0, sxy = 0;
  for (int i = 0; i < n; i++) {
    sxx += (p[i].x - cx) * (p[i].x - cx);
    syy += (p[i].y - cy) * (p[i].y - cy);
    sxy += (p[i].x - cx) * (p[i].y - cy);
  }
  const float mid = 0.5f * (sxx + syy), half = std::sqrt(0.25f * (sxx - syy) * (sxx - syy) + sxy * sxy);
  return mid + half > 0 ? std::sqrt(std::fmax(mid - half, 0.0f) / (mid + half)) : 0.0f;
}

// RMS error after the best similarity fit (mirror allowed), relative to the true spread.
static float procrustes_error(const LayoutPoint *p, const float (*truth)[2], int n) {
  float ox = 0, oy = 0, tx = 0, ty = 0;
  for (int i = 0; i < n; i++) {
    ox += p[i].x / n;
    oy += p[i].y / n;
    tx += truth[i][0] / n;
    ty += truth[i][1] / n;
  }
  // Points as complex numbers: the best rotation and scale is sum(conj(o) t) / sum|o|^2.
  float oo = 0, tt = 0, re = 0, im = 0, mre = 0, mim = 0;
  for (int i = 0; i < n; i++) {
    const float a = p[i].x - ox, b = p[i].y - oy, c = truth[i][0] - tx, d = truth[i][1] - ty;
    oo += a * a + b * b;
    tt += c * c + d * d;
    re += a * c + b * d;
    im += a * d - b * c;
    mre += a * c - b * d;  // mirrored: conj(o) taken as o
    mim += a * d + b * c;
  }
  if (oo <= 0)
    return 1.0f;
  const float fit = std::fmax(re * re + im * im, mre * mre + mim * mim) / oo;
  return std::sqrt(std::fmax(tt - fit, 0.0f) / tt);
}

static Mac mac_of(uint8_t a, uint8_t b, uint8_t c, uint8_t d, uint8_t e, uint8_t f) {
  return Mac{{a, b, c, d, e, f}};
}

// The owner's floor, live rows of 2026-10-05 (four nodes, each also hearing the access point).
// With one fixed path loss exponent (2.7) these came out on a straight line: far pairs too far,
// the triangle inequality broken. They must give a layout with two real dimensions.
static void test_layout_of_live_rows() {
  const Mac n8d58 = mac_of(0x44, 0x1b, 0xf6, 0x8d, 0x58, 0x58), nee90 = mac_of(0x58, 0xcf, 0x79, 0xee, 0x90, 0x58),
            nd714 = mac_of(0xe0, 0x72, 0xa1, 0xd7, 0x14, 0x30), na8c7 = mac_of(0xac, 0x27, 0x6e, 0xa8, 0xc7, 0x7c),
            ap = mac_of(0xa8, 0x29, 0x48, 0xe1, 0x6d, 0x70);
  // Receiver, then what it hears; the access point readings are stand-ins (it is not a node).
  struct Row {
    Mac rx;
    HiveEntry e[4];
  } rows[4] = {
      {n8d58, {{nee90, -71}, {na8c7, -42}, {nd714, -51}, {ap, -58}}},
      {nee90, {{n8d58, -70}, {na8c7, -71}, {nd714, -80}, {ap, -62}}},
      {nd714, {{n8d58, -52}, {na8c7, -63}, {nee90, -82}, {ap, -66}}},
      {na8c7, {{n8d58, -43}, {nee90, -74}, {nd714, -64}, {ap, -55}}},
  };
  Hive h1(mac_n(100)), h2(mac_n(101));
  for (int i = 0; i < 4; i++)
    h1.on_row(rows[i].rx, 1, rows[i].e, 4, 0);
  for (int i = 3; i >= 0; i--)  // same rows, the other way round
    h2.on_row(rows[i].rx, 1, rows[i].e, 4, 0);
  static LayoutWorkspace ws;
  LayoutPoint a[MAX_POINTS], b[MAX_POINTS];
  const int na = solve_layout(h1, a, ws), nb = solve_layout(h2, b, ws);
  CHECK(na == 4 && nb == 4);  // the access point is no point of the layout
  bool identical = na == nb;
  for (int i = 0; i < na && identical; i++)
    identical = a[i].mac == b[i].mac && a[i].x == b[i].x && a[i].y == b[i].y;
  CHECK(identical);
  CHECK(a[0].mac == n8d58 && a[0].x < 0.0f && a[0].y == 0.0f && a[1].y >= 0.0f);  // fixed pose
  float spread = 0.0f;
  for (int i = 0; i < na; i++)
    spread = std::fmax(spread, std::fabs(a[i].y));
  const float flat = layout_flatness(a, na);
  std::printf("  live rows of 4 nodes: flatness %.2f, y spread %.1f m:", flat, spread);
  for (int i = 0; i < na; i++)
    std::printf(" %02x%02x (%.1f, %.1f)", a[i].mac.b[4], a[i].mac.b[5], a[i].x, a[i].y);
  std::printf("\n");
  CHECK(flat > 0.3f && spread >= 0.8f);
  // Nearest pair by far (-42/-43 dB): the nearest in the layout too.
  const float near = std::hypot(a[0].x - a[2].x, a[0].y - a[2].y);  // 8d58 to a8c7
  bool nearest = true;
  for (int i = 0; i < na; i++)
    for (int j = i + 1; j < na; j++)
      if (!(i == 0 && j == 2))
        nearest = nearest && std::hypot(a[i].x - a[j].x, a[i].y - a[j].y) > near;
  CHECK(a[2].mac == na8c7 && nearest);
}

// Homes with walls (3 by 2 rooms of 4 x 4.5 m), path loss exponent 2 to 3.5, 3 to 7 dB a wall,
// per-board offsets of up to 4 dB and 1.5 dB noise: the shape comes back, and two dimensions.
static void test_layout_through_walls() {
  static LayoutWorkspace ws;
  std::mt19937 rng(77);
  std::uniform_real_distribution<float> unit(0.0f, 1.0f);
  std::normal_distribution<float> noise(0.0f, 1.5f);
  for (const int n : {3, 4, 6, 8}) {
    std::vector<float> errors;
    int flat = 0, runs = 0;
    for (int r = 0; r < 100; r++) {
      float pos[MAX_POINTS][2];
      for (int i = 0; i < n; i++) {
        bool apart = false;
        while (!apart) {
          const int room = (i + r) % 6;  // different rooms while there are rooms left
          pos[i][0] = 4.0f * (room % 3) + 0.3f + 3.4f * unit(rng);
          pos[i][1] = 4.5f * (room / 3) + 0.3f + 3.9f * unit(rng);
          apart = true;
          for (int j = 0; j < i; j++)
            apart = apart && std::hypot(pos[i][0] - pos[j][0], pos[i][1] - pos[j][1]) >= 1.0f;
        }
      }
      const float exponent = 2.0f + 1.5f * unit(rng), wall_db = 3.0f + 4.0f * unit(rng);
      float offset[MAX_POINTS];
      for (int i = 0; i < n; i++)
        offset[i] = -4.0f + 8.0f * unit(rng);
      Hive h(mac_n(500));
      for (int i = 0; i < n; i++) {
        HiveEntry e[MAX_ROW];
        int k = 0;
        for (int j = 0; j < n; j++) {
          if (i == j)
            continue;
          const float d = std::hypot(pos[i][0] - pos[j][0], pos[i][1] - pos[j][1]);
          int walls = 0;
          for (const float wx : {4.0f, 8.0f})
            walls += (pos[i][0] - wx) * (pos[j][0] - wx) < 0;
          walls += (pos[i][1] - 4.5f) * (pos[j][1] - 4.5f) < 0;
          const float rssi = -45.0f - 10.0f * exponent * std::log10(d) - wall_db * walls + offset[i] + offset[j] + noise(rng);
          e[k++] = HiveEntry{mac_n(1 + j), static_cast<int8_t>(std::lround(rssi))};
        }
        h.on_row(mac_n(1 + i), 1, e, k, 0);
      }
      LayoutPoint out[MAX_POINTS];
      if (solve_layout(h, out, ws) != n)
        continue;
      runs++;
      errors.push_back(procrustes_error(out, pos, n));
      float truth_flat = 0;
      {
        LayoutPoint t[MAX_POINTS];
        for (int i = 0; i < n; i++)
          t[i] = LayoutPoint{out[i].mac, pos[i][0], pos[i][1]};
        truth_flat = layout_flatness(t, n);
      }
      flat += truth_flat >= 0.1f && layout_flatness(out, n) < 0.1f;
    }
    std::sort(errors.begin(), errors.end());
    const float median = errors.empty() ? 1.0f : errors[errors.size() / 2];
    std::printf("  %d nodes through walls: median shape error %.2f, %d of %d layouts on a line\n", n, median, flat,
                runs);
    CHECK(runs == 100 && median < 0.28f && flat <= (n == 3 ? 8 : 2));
  }
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
  test_rebooted_node_gets_back_in_sync();
  test_departed_rows_expire_despite_relays();
  test_full_hive_makes_room();
  test_stale_copy_of_own_row();
  test_layout();
  test_layout_keeps_two_dimensions();
  test_layout_of_live_rows();
  test_layout_through_walls();
  test_portable_exp2();
  test_hive_report_rotates_rows();
  if (failures) {
    std::printf("%d hive check(s) failed\n", failures);
    return 1;
  }
  std::printf("All hive tests passed\n");
  return 0;
}
