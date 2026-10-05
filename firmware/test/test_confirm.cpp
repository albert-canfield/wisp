// Host tests for the hive's motion confirmation (core_confirm.h).
// Run: firmware/test/run.sh
#include <cmath>
#include <cstdio>
#include <cstring>
#include <random>
#include <vector>

#include "core_confirm.h"

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

// A floor of nodes exchanging their live scores in beacons. score[i][j]: node i hearing node j.
// Each second, like the firmware: every node's flags change at its tick, its beacons carry the
// new scores ten times, then every node works out the pairs.
struct Floor {
  int n;
  std::vector<Mac> macs;
  std::vector<MotionConfirm> nodes;
  std::vector<std::vector<float>> score;
  std::vector<bool> beaconing;
  std::vector<LayoutPoint> layout;
  const Hive *hive{nullptr};
  uint32_t now;

  explicit Floor(int count, uint32_t start = 1000) : n(count), now(start) {
    for (int i = 0; i < n; i++) {
      macs.push_back(mac_n(10 + (i * 7) % 31));  // MAC order differs from index order
      nodes.emplace_back(macs.back());
      layout.push_back(LayoutPoint{macs.back(), static_cast<float>(i % 4), static_cast<float>(i / 4)});
    }
    score.assign(n, std::vector<float>(n, 1.0f));
    beaconing.assign(n, true);
  }

  void quiet() { score.assign(n, std::vector<float>(n, 1.0f)); }

  void second() {
    for (int i = 0; i < n; i++) {
      for (int j = 0; j < n; j++) {
        if (j != i)  // a node that sends nothing gives no CSI either
          nodes[i].own(macs[j], score[i][j] >= 2.0f, beaconing[j], now);
      }
    }
    RowEntry row[MAX_ROW];
    for (int r = 0; r < 10; r++) {
      now += 100;
      for (int i = 0; i < n; i++) {
        if (!beaconing[i])
          continue;
        int len = 0;
        for (int j = 0; j < n; j++) {
          if (j != i) {
            const uint8_t s10 = static_cast<uint8_t>(std::fmin(254.0f, score[i][j] * 10.0f));
            row[len++] = RowEntry{macs[j], -55, beaconing[j] ? s10 : SCORE_UNKNOWN};
          }
        }
        for (int k = 0; k < n; k++) {
          if (k != i)
            nodes[k].on_beacon(macs[i], row, len, now);
        }
      }
    }
    for (int i = 0; i < n; i++)
      nodes[i].tick(now, layout.data(), static_cast<int>(layout.size()), hive);
  }

  void seconds(int s) {
    while (s-- > 0)
      second();
  }

  // Someone near the link between a and b changes it both ways.
  void both(int a, int b) { score[a][b] = score[b][a] = 3.0f; }

  // How many of the given observers confirm pair (a, b).
  int confirming(int a, int b, std::vector<int> who = {}) const {
    if (who.empty()) {
      for (int i = 0; i < n; i++)
        who.push_back(i);
    }
    int c = 0;
    for (int i : who)
      c += nodes[i].confirmed(macs[a], macs[b]);
    return c;
  }

  // Confirmed pairs on every node, which must agree.
  int pairs_everywhere() const {
    PairList first;
    const int count = nodes[0].pairs(first);
    for (int i = 1; i < n; i++) {
      PairList p;
      if (nodes[i].pairs(p) != count)
        return -1;
      for (int k = 0; k < count; k++) {
        if (p.a[k] != first.a[k] || p.b[k] != first.b[k])
          return -1;
      }
    }
    return count;
  }
};

static void test_four_nodes_need_a_third() {
  Floor f(4);
  f.seconds(3);
  CHECK(f.pairs_everywhere() == 0);
  f.both(0, 1);  // the link between nodes 0 and 1 moves both ways: a body, or both nodes' noise
  f.seconds(3);
  CHECK(f.pairs_everywhere() == 0);  // no other node agrees
  f.score[2][0] = 3.0f;              // node 2 sees node 0's signal move too: someone there
  f.second();
  CHECK(f.confirming(0, 1) == 4 && f.pairs_everywhere() == 1);
  CHECK(f.confirming(0, 2) == 0);  // one way only
  CHECK(f.nodes[0].self_confirmed(f.now) && f.nodes[1].self_confirmed(f.now));
  CHECK(!f.nodes[2].self_confirmed(f.now) && !f.nodes[3].self_confirmed(f.now));
  // Held while one way still moves and the other did within the window
  f.seconds(5);
  CHECK(f.confirming(0, 1) == 4);
  f.score[1][0] = 1.0f;
  f.seconds(2);
  CHECK(f.confirming(0, 1) == 4);
  f.second();
  CHECK(f.pairs_everywhere() == 0);
  // Both ways quiet: over at once; the access point's links follow for the window
  f.both(0, 1);
  f.second();
  CHECK(f.confirming(0, 1) == 4);
  f.quiet();
  f.second();
  CHECK(f.pairs_everywhere() == 0 && f.nodes[0].self_confirmed(f.now));
  f.second();
  CHECK(f.nodes[0].self_confirmed(f.now));
  f.second();
  CHECK(!f.nodes[0].self_confirmed(f.now));

  // One direction alone, with a supporter: one node's noise on what it receives
  Floor g(4);
  g.seconds(3);
  g.score[0][1] = 3.0f;
  g.score[2][0] = 3.0f;
  g.seconds(4);
  CHECK(g.pairs_everywhere() == 0);
  // The reverse moving a while after the first stopped is not the same moment
  g.score[0][1] = 1.0f;
  g.seconds(3);
  g.score[1][0] = 3.0f;
  g.second();
  CHECK(g.pairs_everywhere() == 0);
}

static void test_stale_beacons() {
  Floor f(4);
  f.seconds(3);
  f.both(0, 1);
  f.score[2][0] = 3.0f;
  f.seconds(2);
  CHECK(f.confirming(0, 1) == 4);
  f.beaconing[1] = false;  // node 1 goes quiet: its last beacon was 1 s ago at the next tick
  f.second();
  CHECK(f.confirming(0, 1, {0, 2, 3}) == 3);
  f.second();  // 2 s: too old to say anything
  CHECK(f.confirming(0, 1, {0, 2, 3}) == 0);
  // Back with quiet scores: no motion is read into the gap
  f.quiet();
  f.beaconing[1] = true;
  f.second();
  CHECK(f.pairs_everywhere() == 0);
}

// Exact edges, one node: A hears B; B and C report what they hear of A.
static void test_window_edges() {
  const Mac a = mac_n(1), b = mac_n(2), c = mac_n(3);
  for (uint32_t late = 0; late < 2; late++) {
    MotionConfirm m(a);
    uint32_t t = 5000;
    RowEntry quiet_b[2] = {{a, -50, 10}, {c, -50, 10}}, quiet_c[2] = {{a, -50, 10}, {b, -50, 10}};
    RowEntry moving_b[2] = {{a, -50, 30}, {c, -50, 10}}, moving_c[2] = {{a, -50, 30}, {b, -50, 10}};
    m.on_beacon(b, quiet_b, 2, t);
    m.on_beacon(c, quiet_c, 2, t);
    t += 100;
    m.on_beacon(b, moving_b, 2, t);
    m.on_beacon(c, moving_c, 2, t);
    // Hysteresis as a node's own flag: 1.6 keeps it on (off below 1.5), 1.0 ends it
    RowEntry held_b[2] = {{a, -50, 16}, {c, -50, 10}};
    t += 100;
    m.on_beacon(b, held_b, 2, t);
    m.on_beacon(c, moving_c, 2, t);
    t += 100;
    const uint32_t stopped = t;  // both seen quiet first here: they moved until now
    m.on_beacon(b, quiet_b, 2, t);
    m.on_beacon(c, quiet_c, 2, t);
    const uint32_t tick = stopped + CONFIRM_WINDOW_MS + late;
    m.on_beacon(b, quiet_b, 2, tick - 100);  // fresh
    m.on_beacon(c, quiet_c, 2, tick - 100);
    m.own(b, true, true, tick);  // A's own flag for B turns on now
    m.own(c, false, true, tick);
    m.tick(tick);
    CHECK(m.confirmed(a, b) == (late == 0));
    CHECK(!m.confirmed(a, c));
  }
  // This node's threshold, 2.5: B reporting 2.4 for A is no motion, 2.5 is
  for (uint8_t s10 = 24; s10 <= 25; s10++) {
    MotionConfirm m(a);
    m.set_threshold(2.5f);
    RowEntry from_b[2] = {{a, -50, s10}, {c, -50, 10}}, from_c[2] = {{a, -50, 30}, {b, -50, 10}};
    m.on_beacon(b, from_b, 2, 100);
    m.on_beacon(c, from_c, 2, 100);
    m.own(b, true, true, 100);
    m.own(c, false, true, 100);
    m.tick(100);
    CHECK(m.confirmed(a, b) == (s10 == 25) && !m.confirmed(a, c));
  }
}

// Eight nodes: K = 6 nearest, two of them must agree. Two people at once, far apart in the grid,
// confirm their own pairs and nothing in between.
static void test_two_people() {
  Floor f(8);
  f.seconds(3);
  f.both(0, 1);
  f.score[2][0] = 3.0f;  // one supporter: not enough with K = 6
  f.both(4, 5);
  f.score[6][4] = 3.0f;
  f.score[7][5] = 3.0f;
  f.seconds(2);
  CHECK(f.confirming(0, 1) == 0 && f.confirming(4, 5) == 8);
  f.score[3][1] = 3.0f;  // the second supporter
  f.second();
  CHECK(f.confirming(0, 1) == 8 && f.confirming(4, 5) == 8 && f.pairs_everywhere() == 2);
  PairList p;
  f.nodes[3].pairs(p);
  CHECK(p.n == 2 && !p.truncated && p.a[0] < p.b[0] && p.a[1] < p.b[1] && p.a[0] < p.a[1]);
  // The second person leaves, the first stays
  f.score[4][5] = f.score[5][4] = f.score[6][4] = f.score[7][5] = 1.0f;
  f.seconds(3);
  CHECK(f.confirming(0, 1) == 8 && f.confirming(4, 5) == 0 && f.pairs_everywhere() == 1);
}

// Ten nodes: supporters count among the 6 nearest the pair only, by the layout or, without one,
// by RSSI. Nodes 8 and 9 stand far off.
static void test_far_node_cannot_support() {
  for (int by_rssi = 0; by_rssi < 2; by_rssi++) {
    Floor f(10);
    const float xy[10][2] = {{0, 0}, {2, 0}, {0, 1}, {2, 1}, {1, -1}, {1, 1.5f}, {-1, 0}, {3, 0}, {20, 0}, {0, 20}};
    Hive hive(mac_n(999));
    for (int i = 0; i < 10; i++)
      f.layout[i] = LayoutPoint{f.macs[i], xy[i][0], xy[i][1]};
    if (by_rssi) {
      for (int i = 0; i < 10; i++) {
        HiveEntry e[MAX_ROW];
        int len = 0;
        for (int j = 0; j < 10; j++) {
          if (j == i)
            continue;
          const float d = std::hypot(xy[i][0] - xy[j][0], xy[i][1] - xy[j][1]);
          e[len++] = HiveEntry{f.macs[j], static_cast<int8_t>(std::lround(-45.0f - 40.0f * std::log10(d)))};
        }
        CHECK(hive.on_row(f.macs[i], 1, e, len, 0));
      }
      f.layout.clear();
      f.hive = &hive;
    }
    f.seconds(3);
    f.both(0, 1);
    f.score[8][0] = 3.0f;  // far nodes see the pair's ends move: not among the 6 nearest
    f.score[9][1] = 3.0f;
    f.score[2][0] = 3.0f;  // one near supporter
    f.seconds(2);
    CHECK(f.confirming(0, 1) == 0);
    f.score[1][3] = 3.0f;  // a second near one (node 1 hearing node 3 counts too)
    f.second();
    CHECK(f.confirming(0, 1) == 10 && f.pairs_everywhere() == 1);
  }
}

// Same beacons, same pairs: every node of a floor agrees each second, whatever moves where.
static void test_same_pairs_everywhere() {
  Floor f(6);
  std::mt19937 rng(11);
  std::uniform_int_distribution<int> pick(0, 5), people(0, 2);
  std::bernoulli_distribution half(0.5);
  f.seconds(3);
  int confirmed_seconds = 0;
  for (int s = 0; s < 120; s++) {
    if (s % 3 == 0) {
      f.quiet();
      for (int k = people(rng); k > 0; k--) {  // near a link, both ways or one; a third node or not
        const int a = pick(rng), b = (a + 1 + pick(rng) % 5) % 6, c = (b + 1 + pick(rng) % 5) % 6;
        f.score[a][b] = 3.0f;
        if (half(rng))
          f.score[b][a] = 3.0f;
        if (half(rng) && c != a)
          f.score[c][a] = 3.0f;
      }
    }
    f.second();
    const int pairs = f.pairs_everywhere();
    CHECK(pairs >= 0);
    confirmed_seconds += pairs > 0;
  }
  std::printf("  6 nodes, people moving about: pairs confirmed in %d of 120 s, the same on every node\n",
              confirmed_seconds);
  CHECK(confirmed_seconds > 10 && confirmed_seconds < 110);
}

// The 32-bit millisecond clock wraps every 49.7 days: confirmation carries on across it and
// nothing old comes back.
static void test_clock_wrap() {
  Floor f(4, 0xFFFFE000u);  // 8.2 s before the wrap
  f.seconds(4);
  f.both(0, 1);
  f.score[3][1] = 3.0f;
  f.seconds(3);
  CHECK(f.confirming(0, 1) == 4);
  f.seconds(3);  // across the wrap
  CHECK(f.now < 0x10000000u && f.confirming(0, 1) == 4);
  f.quiet();
  f.seconds(3);
  CHECK(f.pairs_everywhere() == 0);

  // A reading 2^32 ms old reads as recent by its time alone: it was forgotten long before.
  const Mac a = mac_n(1), b = mac_n(2), c = mac_n(3);
  MotionConfirm m(a);
  RowEntry from_b[2] = {{a, -50, 30}, {c, -50, 30}}, from_c[2] = {{a, -50, 30}, {b, -50, 30}};
  m.on_beacon(b, from_b, 2, 1000);
  m.on_beacon(c, from_c, 2, 1000);
  for (uint32_t t = 2000; t < 10000; t += 1000)
    m.tick(t);  // silent: forgotten
  RowEntry quiet_b[2] = {{a, -50, 10}, {c, -50, 10}}, quiet_c[2] = {{a, -50, 10}, {b, -50, 10}};
  m.on_beacon(b, quiet_b, 2, 1000 + 100);  // 2^32 ms later the clock reads 1100 again
  m.on_beacon(c, quiet_c, 2, 1000 + 100);
  m.own(b, true, true, 1200);
  m.own(c, true, true, 1200);
  m.tick(1200);
  CHECK(!m.confirmed(a, b) && !m.confirmed(a, c));
}

// The table holds this node and 16 others; a newcomer waits while all of them are live.
static void test_bounded() {
  Floor f(CONFIRM_NODES);
  f.seconds(2);
  for (int i = 0; i < f.n; i++)
    CHECK(f.nodes[i].count() == CONFIRM_NODES);
  MotionConfirm &m = f.nodes[0];
  RowEntry row[1] = {{f.macs[1], -50, 30}};
  m.on_beacon(mac_n(500), row, 1, f.now);
  CHECK(m.count() == CONFIRM_NODES && m.find(mac_n(500)) < 0);
  for (int i = 2; i < f.n; i++)
    f.beaconing[i] = false;
  f.seconds(3);  // most of them gone quiet: room for it, in place of one of them
  m.on_beacon(mac_n(500), row, 1, f.now);
  CHECK(m.count() == CONFIRM_NODES && m.find(mac_n(500)) > 0 && m.find(f.macs[1]) > 0);
  f.seconds(1);  // the quiet ones are not taken back while they say nothing
  CHECK(m.find(mac_n(500)) > 0);
}

static void feed(LinkTable &links, const Mac &src, LinkKind kind, float spread, std::mt19937 &rng, uint32_t now) {
  std::normal_distribution<float> noise(0.0f, 1.0f);
  CsiRecord r{};
  r.len = 128;
  r.rssi = -55;
  memcpy(r.source, src.b, 6);
  for (int f = 0; f < 20; f++) {
    for (int i = 0; i < 128; i++)
      r.data[i] = static_cast<int8_t>(std::fmax(-127.0f, std::fmin(127.0f, 30 + spread * noise(rng))));
    links.add_frame(r, kind, now);
  }
}

// This node's links: the one from a node is confirmed with its pair, the one from the access
// point while a pair with this node is; the link report carries both, and the pairs.
static void test_links_and_report() {
  const Mac a = mac_n(1), b = mac_n(2), c = mac_n(3), ap = mac_n(700);
  LinkTable links;
  MotionConfirm m(a);
  std::mt19937 rng(5);
  uint32_t now = 0;
  RowEntry quiet_b[2] = {{a, -50, 10}, {c, -50, 10}}, quiet_c[2] = {{a, -50, 10}, {b, -50, 10}};
  RowEntry moving_b[2] = {{a, -50, 30}, {c, -50, 10}}, moving_c[2] = {{a, -50, 30}, {b, -50, 10}};
  bool any = false;
  for (int s = 0; s < 64; s++) {
    const float spread = s < 40 ? 1.0f : 12.0f;  // calm, then someone moving
    feed(links, ap, LinkKind::ACCESS_POINT, spread, rng, now);
    feed(links, b, LinkKind::NODE, spread, rng, now);
    feed(links, c, LinkKind::NODE, 1.0f, rng, now);
    now += 1000;
    links.tick_second(now);
    const bool others = s >= 62;  // B and C see it from second 62
    m.on_beacon(b, others ? moving_b : quiet_b, 2, now - 50);
    m.on_beacon(c, others ? moving_c : quiet_c, 2, now - 50);
    any = m.update(links, now);
    if (s == 61) {  // this node alone sees motion
      CHECK(links.find(ap)->active && links.find(b)->active && !any);
      CHECK(!links.find(ap)->confirmed && !links.find(b)->confirmed);
    }
  }
  CHECK(any && links.find(ap)->confirmed && links.find(b)->confirmed && !links.find(c)->confirmed);
  PairList pairs;
  CHECK(m.pairs(pairs) == 1 && pairs.a[0] == a && pairs.b[0] == b);
  uint8_t out[LINK_REPORT_MAX];
  const size_t n = links.encode_report(a, 7, 60, out, sizeof(out), &pairs);
  const size_t links_end = LINK_REPORT_HEADER_BYTES + 3 * LINK_REPORT_ENTRY_BYTES;
  CHECK(n == links_end + 1 + LINK_REPORT_PAIR_BYTES);
  CHECK(out[18] == 3 && out[19] == REPORT_FLAG_CONFIRMS);
  for (int i = 0; i < 3; i++) {
    const uint8_t *e = out + LINK_REPORT_HEADER_BYTES + LINK_REPORT_ENTRY_BYTES * i;
    const Mac src = Mac::from(e);
    CHECK(e[13] == (src == c ? 0 : (LINK_FLAG_MOTION | LINK_FLAG_CONFIRMED)));
  }
  CHECK(out[links_end] == 1 && Mac::from(out + links_end + 1) == a && Mac::from(out + links_end + 7) == b);
  // Without pairs, the report is as before: no flag, nothing after the links
  CHECK(links.encode_report(a, 8, 60, out, sizeof(out)) == links_end && out[19] == 0);
  // More pairs than fit: the report says so
  PairList many;
  for (int i = 0; i < MAX_REPORT_PAIRS; i++) {
    many.a[i] = mac_n(i);
    many.b[i] = mac_n(100 + i);
  }
  many.n = MAX_REPORT_PAIRS;
  many.truncated = true;
  CHECK(links.encode_report(a, 9, 60, out, sizeof(out), &many) == LINK_REPORT_MAX - 17 * LINK_REPORT_ENTRY_BYTES);
  CHECK(out[19] == (REPORT_FLAG_CONFIRMS | REPORT_FLAG_PAIRS_TRUNCATED));
}

// Exposes how this node judges the directions other nodes report.
struct Probe : MotionConfirm {
  using MotionConfirm::MotionConfirm;
  bool moving(const Mac &rx, const Mac &tx) const { return this->moving_(this->find(rx), this->find(tx)); }
};

// A link with a threshold of its own (QuietThreshold): its beacon carries the score normalised to
// it, and a node judging that at the default threshold follows the receiver's own flag, on and
// off, whatever the link's threshold. At the default threshold the beacon carries the raw score.
static void test_normalised_beacon_scores() {
  const Mac a = mac_n(1), b = mac_n(2);
  const float scores[] = {1.0f, 1.8f, 2.4f, 2.9f, 3.0f, 3.6f, 2.6f, 2.0f, 1.95f, 1.6f, 3.1f, 1.2f, NAN, 2.2f};
  for (const float t : {2.0f, 2.6f, 3.0f, 4.5f}) {
    Probe m(a);
    Link l{};
    l.threshold = t;
    MotionDetector own(t);
    uint32_t now = 1000;
    int checked = 0;
    for (const float sc : scores) {
      for (int k = 0; k < 4; k++) {  // around each score, a little either side of the edges
        l.score = sc + 0.03f * static_cast<float>(k - 2);
        const bool on = own.update(l.score);
        RowEntry row[1] = {{a, -50, beacon_score10(l)}};
        m.on_beacon(b, row, 1, now += 100);  // B hears A
        if (std::isnan(l.score)) {
          CHECK(row[0].score10 == SCORE_UNKNOWN);
          own.reset();
          continue;
        }
        // The beacon's tenths round down: within 0.1 of the edges, the reading may lag by a step
        const float n = 1.0f + (l.score - 1.0f) / (t - 1.0f);
        if (std::fabs(n - 2.0f) < 0.1f || std::fabs(n - 1.5f) < 0.1f)
          continue;
        CHECK(m.moving(b, a) == on);
        checked++;
      }
    }
    CHECK(checked > 25);
  }
  Link l{};
  l.score = 2.37f;
  CHECK(beacon_score10(l) == 23);  // the default threshold: the score itself, as before 0.1.7
  l.threshold = 3.0f;
  l.score = 3.0f;
  CHECK(beacon_score10(l) == 20);  // exactly at its threshold: exactly the default
}

// A link learns its quiet scores only in seconds the hive confirms no motion (and some after),
// and never while flagged: someone moving about, short of a flag, does not raise its threshold.
static void test_quiet_learning() {
  const Mac ap = mac_n(700);
  for (int confirmed = 0; confirmed < 2; confirmed++) {
    LinkTable links;
    std::mt19937 rng(8);
    uint32_t now = 0;
    for (int s = 0; s < 1500; s++) {
      // 6 quiet minutes, then someone near the link: below the threshold, and the hive (when it
      // can) confirms them moving
      const bool busy = s >= 360;
      feed(links, ap, LinkKind::ACCESS_POINT, busy ? 1.8f : 1.0f, rng, now);
      now += 1000;
      links.tick_second(now);
      links.learn_quiet(busy && confirmed, now);
      if (s == 359)
        CHECK(links.find(ap)->threshold == 2.0f && links.find(ap)->quiet.learned() > QUIET_WARMUP);
    }
    const Link *l = links.find(ap);
    std::printf("  someone about for 19 min %s: threshold %.2f\n", confirmed ? "(confirmed)" : "(never confirmed)",
                l->threshold);
    CHECK(confirmed ? l->threshold == 2.0f : l->threshold > 2.0f);
    CHECK(links.max_threshold() == l->threshold);
  }
  // The hold: seconds within QUIET_HOLD_MS of confirmed motion are not learned either
  LinkTable links;
  std::mt19937 rng(9);
  uint32_t now = 0;
  feed(links, ap, LinkKind::ACCESS_POINT, 1.0f, rng, now);
  for (int s = 0; s < 40; s++) {  // past the score's own settling
    feed(links, ap, LinkKind::ACCESS_POINT, 1.0f, rng, now);
    links.tick_second(now += 1000);
  }
  const float before = links.find(ap)->quiet.learned();
  links.learn_quiet(true, now);
  for (uint32_t s = 1; s <= QUIET_HOLD_MS / 1000; s++) {
    feed(links, ap, LinkKind::ACCESS_POINT, 1.0f, rng, now);
    links.tick_second(now += 1000);
    links.learn_quiet(false, now);
  }
  CHECK(links.find(ap)->quiet.learned() == before);
  feed(links, ap, LinkKind::ACCESS_POINT, 1.0f, rng, now);
  links.tick_second(now += 1000);
  links.learn_quiet(false, now);
  CHECK(links.find(ap)->quiet.learned() > before);
}

int main() {
  test_normalised_beacon_scores();
  test_quiet_learning();
  test_four_nodes_need_a_third();
  test_stale_beacons();
  test_window_edges();
  test_two_people();
  test_far_node_cannot_support();
  test_same_pairs_everywhere();
  test_clock_wrap();
  test_bounded();
  test_links_and_report();
  if (failures) {
    std::printf("%d confirmation check(s) failed\n", failures);
    return 1;
  }
  std::printf("All confirmation tests passed\n");
  return 0;
}
