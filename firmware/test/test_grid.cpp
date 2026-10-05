// Host tests and a small simulator for the grid (core_grid.h) and links (core_links.h).
// Run: firmware/test/run.sh
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <random>
#include <set>
#include <vector>

#include "core_grid.h"
#include "core_links.h"
#include "core_wifi_plan.h"

static int failures = 0;
#define CHECK(cond)                                               \
  do {                                                            \
    if (!(cond)) {                                                \
      std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
      failures++;                                                 \
    }                                                             \
  } while (0)

using namespace wisp_core;

static Mac mac_n(int n) {
  Mac m{{0x44, 0x1b, 0xf6, 0x00, static_cast<uint8_t>(n >> 8), static_cast<uint8_t>(n)}};
  return m;
}

// Virtual nodes exchanging real encoded beacons, one round (100 ms) at a time.
struct Sim {
  struct Node {
    Grid grid;
    bool on{true};
    uint16_t seq{0};
    explicit Node(const Mac &m) : grid(m) {}
  };
  std::vector<Node> nodes;
  uint32_t now_ms{0};
  std::mt19937 rng{7};
  float loss{0.05f};

  explicit Sim(int n) {
    for (int i = 0; i < n; i++) {
      nodes.emplace_back(mac_n(i * 7 + 3));  // MAC order differs from index order
      nodes.back().grid.start(0);
    }
  }

  void round() {
    std::uniform_real_distribution<float> u(0, 1);
    for (auto &tx : nodes) {
      if (!tx.on)
        continue;
      const int slot = tx.grid.self_slot(now_ms);
      if (slot < 0)
        continue;
      Beacon b{};
      b.seq = tx.seq++;
      b.slot = static_cast<int8_t>(slot);
      b.uptime_s = now_ms / 1000;
      b.active = static_cast<uint8_t>(tx.grid.active_count());
      uint8_t buf[BEACON_MAX_BYTES];
      const size_t n = encode_beacon(b, buf, sizeof(buf));
      for (auto &rx : nodes) {
        if (&rx == &tx || !rx.on || u(rng) < loss)
          continue;
        Beacon got{};
        if (decode_beacon(buf, n, got))
          rx.grid.on_beacon(tx.grid.self(), now_ms, -55, got.seq, got.chip, got.uptime_s);
      }
    }
    now_ms += 100;
    for (auto &nd : nodes) {
      if (nd.on)
        nd.grid.tick(now_ms);
    }
  }

  void run_ms(uint32_t ms) {
    for (uint32_t t = 0; t < ms; t += 100)
      round();
  }

  // Jumps the clock without traffic between online nodes being lost (used for long timers).
  void advance_quiet_ms(uint64_t ms) {
    const uint32_t step = 60 * 1000;
    for (uint64_t t = 0; t < ms; t += step) {
      run_ms(1000);  // keep the online nodes hearing each other
      now_ms += step - 1000;
      for (auto &nd : nodes) {
        if (nd.on)
          nd.grid.tick(now_ms);
      }
    }
    run_ms(2000);  // the jumps above make online nodes look quiet to each other for a moment
  }

  // All online nodes: same active count, distinct slots, and slot = MAC rank.
  bool agreed(int expect_active) {
    std::vector<Mac> online;
    for (auto &nd : nodes) {
      if (nd.on)
        online.push_back(nd.grid.self());
    }
    std::sort(online.begin(), online.end());
    std::set<int> slots;
    for (auto &nd : nodes) {
      if (!nd.on)
        continue;
      if (nd.grid.active_count() != expect_active)
        return false;
      const int rank = static_cast<int>(std::find(online.begin(), online.end(), nd.grid.self()) - online.begin());
      const int slot = nd.grid.self_slot(now_ms);
      if (slot != (rank < SLOTS ? rank : -1))
        return false;
      if (slot >= 0 && !slots.insert(slot).second)
        return false;
    }
    return true;
  }
};

static void test_beacon_round_trip() {
  Beacon b{};
  b.seq = 513;
  b.flags = BEACON_FLAG_SYNCED;
  b.slot = 3;
  b.uptime_s = 86401;
  b.chip = CHIP_ESP32S3;
  b.active = 4;
  b.clock_bssid = mac_n(99);
  b.hive_hash = 0xdeadbeef;
  b.row_version = 0x1234;
  b.row_len = 2;
  b.row[0] = RowEntry{mac_n(1), -48, 12};
  b.row[1] = RowEntry{mac_n(2), -71, SCORE_UNKNOWN};
  uint8_t buf[BEACON_MAX_BYTES];
  const size_t n = encode_beacon(b, buf, sizeof(buf));
  CHECK(n == BEACON_HEADER_BYTES + 16);
  Beacon d{};
  CHECK(decode_beacon(buf, n, d));
  CHECK(d.seq == 513 && d.flags == BEACON_FLAG_SYNCED && d.slot == 3 && d.uptime_s == 86401);
  CHECK(d.chip == CHIP_ESP32S3 && d.active == 4 && d.clock_bssid == mac_n(99));
  CHECK(d.hive_hash == 0xdeadbeef && d.row_version == 0x1234);
  CHECK(d.row_len == 2 && d.row[0].mac == mac_n(1) && d.row[0].rssi == -48 && d.row[1].score10 == SCORE_UNKNOWN);
  CHECK(!decode_beacon(buf, n - 1, d));  // truncated row
  buf[2] = 9;
  CHECK(!decode_beacon(buf, n, d));  // unknown version
}

static void test_slot_timing() {
  CHECK(next_slot_time(0, 0) == SLOT_GUARD_US);
  CHECK(next_slot_time(0, 2) == 2 * SLOT_US + SLOT_GUARD_US);
  CHECK(next_slot_time(SLOT_GUARD_US, 0) == ROUND_US + SLOT_GUARD_US);  // just passed: next round
  CHECK(next_slot_time(1234567, 5) % ROUND_US == 5 * SLOT_US + SLOT_GUARD_US);
  CHECK(next_slot_time(1234567, 5) > 1234567);
}

static void test_grid_forms_and_heals() {
  Sim sim(4);
  CHECK(sim.nodes[0].grid.self_slot(0) == -1);  // listening first
  sim.run_ms(2000);
  CHECK(sim.agreed(4));

  // A node unplugged: quiet within seconds, slots re-ranked without it.
  sim.nodes[1].on = false;
  const uint32_t version = sim.nodes[0].grid.version();
  sim.run_ms(2000);
  CHECK(sim.agreed(3));
  CHECK(sim.nodes[0].grid.version() != version);
  const Member *m = sim.nodes[0].grid.find(sim.nodes[1].grid.self());
  CHECK(m != nullptr && m->state == MemberState::QUIET);

  // Missing after 5 minutes, still remembered.
  sim.advance_quiet_ms(6 * 60 * 1000);
  m = sim.nodes[0].grid.find(sim.nodes[1].grid.self());
  CHECK(m != nullptr && m->state == MemberState::MISSING);

  // Back before it is forgotten: active again at once.
  sim.nodes[1].on = true;
  sim.nodes[1].grid.start(sim.now_ms);
  sim.run_ms(2000);
  CHECK(sim.agreed(4));

  // Gone for over a day: forgotten by everyone.
  sim.nodes[2].on = false;
  sim.advance_quiet_ms(25ull * 3600 * 1000);
  CHECK(sim.agreed(3));
  CHECK(!sim.nodes[0].grid.is_member(sim.nodes[2].grid.self()));
  CHECK(sim.nodes[0].grid.count() == 2);
}

static void test_new_node_joins_running_grid() {
  Sim sim(5);
  sim.nodes[4].on = false;
  sim.run_ms(3000);
  CHECK(sim.agreed(4));
  sim.nodes[4].on = true;
  sim.nodes[4].grid.start(sim.now_ms);
  sim.run_ms(1500);
  CHECK(sim.agreed(5));
}

static void test_more_nodes_than_slots() {
  Sim sim(18);
  sim.loss = 0.02f;
  sim.run_ms(10000);
  int transmitting = 0;
  std::set<int> slots;
  bool unique = true;
  for (auto &nd : sim.nodes) {
    const int s = nd.grid.self_slot(sim.now_ms);
    if (s >= 0) {
      transmitting++;
      unique = slots.insert(s).second && unique;
    }
  }
  std::printf("  18 nodes: %d transmit, %d listen\n", transmitting, 18 - transmitting);
  CHECK(transmitting <= SLOTS);
  CHECK(transmitting >= SLOTS - 1);
  CHECK(unique);
}

static void test_link_report() {
  LinkTable links;
  std::mt19937 rng(3);
  std::normal_distribution<float> noise(0.0f, 1.0f);
  CsiRecord r{};
  r.len = 128;
  const Mac ap = mac_n(500), node = mac_n(501);
  uint32_t now = 0;
  for (int s = 0; s < 5; s++) {
    for (int f = 0; f < 20; f++) {
      for (int i = 0; i < 128; i++)
        r.data[i] = static_cast<int8_t>(20 + 3 * noise(rng));
      memcpy(r.source, ap.b, 6);
      r.rssi = -50;
      links.add_frame(r, LinkKind::ACCESS_POINT, now);
      memcpy(r.source, node.b, 6);
      r.rssi = -60;
      links.add_frame(r, LinkKind::NODE, now);
      now += 50;
    }
    links.tick_second(now);
  }
  CHECK(links.count() == 2);
  uint8_t out[LINK_REPORT_MAX];
  const size_t n = links.encode_report(mac_n(1), 42, 3600, out, sizeof(out));
  CHECK(n == LINK_REPORT_HEADER_BYTES + 2 * LINK_REPORT_ENTRY_BYTES);
  CHECK(memcmp(out, "WISP", 4) == 0 && out[5] == PACKET_LINK_REPORT && out[6] == LINK_REPORT_HEADER_BYTES);
  CHECK(out[8] == 42 && out[18] == 2 && out[20] == 0x10 && out[21] == 0x0e);  // seq 42, uptime 3600
  const uint8_t *e0 = out + LINK_REPORT_HEADER_BYTES;
  CHECK(Mac::from(e0) == ap && e0[6] == 0 && static_cast<int8_t>(e0[7]) == -50);
  CHECK(e0[12] == 100);  // frames since the start, capped below 255
  const uint8_t *e1 = e0 + LINK_REPORT_ENTRY_BYTES;
  CHECK(Mac::from(e1) == node && e1[6] == 1 && static_cast<int8_t>(e1[7]) == -60);
  // The next interval starts empty.
  links.encode_report(mac_n(1), 43, 3600, out, sizeof(out));
  CHECK(out[LINK_REPORT_HEADER_BYTES + 12] == 0 && static_cast<int8_t>(out[LINK_REPORT_HEADER_BYTES + 7]) == -128);
  // A link that goes silent while it shows motion drops the motion flag at once.
  {
    LinkTable t;
    std::normal_distribution<float> wild(0.0f, 1.0f);
    CsiRecord m{};
    m.len = 128;
    memcpy(m.source, ap.b, 6);
    uint32_t tm = 0;
    for (int s = 0; s < 60; s++) {
      for (int f = 0; f < 20; f++) {
        const float spread = s < 40 ? 1.0f : 12.0f;  // calm, then someone moving
        for (int i = 0; i < 128; i++)
          m.data[i] = static_cast<int8_t>(std::fmax(-127.0f, std::fmin(127.0f, 30 + spread * wild(rng))));
        t.add_frame(m, LinkKind::ACCESS_POINT, tm);
        tm += 50;
      }
      t.tick_second(tm);
    }
    CHECK(t.find(ap)->active);
    tm += 1000;
    t.tick_second(tm);  // no frames this second
    CHECK(!t.find(ap)->active);
  }
  // Silent for over 10 minutes: the entry is freed.
  links.tick_second(now + LINK_EXPIRE_MS + 1);
  CHECK(links.count() == 0);
}

static void test_wifi_plan() {
  // The owner's home: three APs on channels 1, 6 and 11.
  const Mac ap1 = Mac{{0x58, 0x04, 0x4f, 0x1d, 0x12, 0xf9}};
  const Mac ap6 = Mac{{0xa8, 0x29, 0x48, 0xdb, 0xb6, 0x70}};
  const Mac ap11 = Mac{{0xa8, 0x29, 0x48, 0xe1, 0x6d, 0x70}};
  ApList seen;
  seen.add(ap6, 6, -55);
  seen.add(ap11, 11, -45);
  seen.add(ap1, 1, -61);
  CHECK(choose_grid_channel(seen.data(), seen.count()) == 1);  // lowest BSSID wins, not the strongest
  CHECK(choose_home_ap(seen.data(), seen.count(), 1) == 2);
  // Another node hears the same APs at other strengths: same channel.
  ApList other;
  other.add(ap11, 11, -70);
  other.add(ap1, 1, -78);
  other.add(ap6, 6, -40);
  CHECK(choose_grid_channel(other.data(), other.count()) == 1);
  // Too weak to use: the next lowest BSSID decides.
  other.add(ap1, 1, -88);
  CHECK(choose_grid_channel(other.data(), other.count()) == 6);
  CHECK(choose_grid_channel(other.data(), other.count(), DEFAULT_MIN_RSSI, 11) == 11);  // forced
  ApList none;
  CHECK(choose_grid_channel(none.data(), none.count()) == 0);
  CHECK(choose_home_ap(seen.data(), seen.count(), 13) == -1);
}

int main() {
  test_wifi_plan();
  test_beacon_round_trip();
  test_slot_timing();
  test_grid_forms_and_heals();
  test_new_node_joins_running_grid();
  test_more_nodes_than_slots();
  test_link_report();
  if (failures) {
    std::printf("%d grid check(s) failed\n", failures);
    return 1;
  }
  std::printf("All grid tests passed\n");
  return 0;
}
