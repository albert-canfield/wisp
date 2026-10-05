// Host tests for breathing on a link (core_breathing.h), through the link table.
// Run: firmware/test/run.sh
#include <cmath>
#include <cstdio>
#include <cstring>
#include <random>

#include "core_links.h"

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

// One LLTF frame: a fixed channel shape, part of it moved by a chest rising at `hz` (depth
// `breath`, a share of the amplitude), and noise on every subcarrier.
static CsiRecord frame(const Mac &src, float t, float breath, float hz, float noise, std::mt19937 &rng) {
  std::normal_distribution<float> n(0.0f, 1.0f);
  CsiRecord r{};
  r.len = 128;
  r.rssi = -50;
  memcpy(r.source, src.b, 6);
  const float chest = breath * std::sin(6.2831853f * hz * t);
  for (int i = 0; i < 64; i++) {
    const float a = (60.0f + 25.0f * std::sin(i * 0.4f)) * (1.0f + chest * std::cos(i * 0.7f)) * (1.0f + noise * n(rng));
    r.data[2 * i] = static_cast<int8_t>(std::fmax(-127.0f, std::fmin(127.0f, std::round(a * std::sin(i * 0.5f)))));
    r.data[2 * i + 1] = static_cast<int8_t>(std::fmax(-127.0f, std::fmin(127.0f, std::round(a * std::cos(i * 0.5f)))));
  }
  return r;
}

struct Room {
  LinkTable links;
  BreathingBank bank;
  std::mt19937 rng{3};
  uint32_t now{1000};
  Room() { links.set_breathing(&bank); }

  // Seconds of frames, 20 a second per link; breath[i] and noise[i] per link, a link with noise
  // below 0 sends nothing.
  void seconds(int s, const Mac *src, const float *breath, const float *noise, int n, float hz = 0.25f) {
    for (int k = 0; k < s; k++) {
      for (int f = 0; f < 20; f++) {
        now += 50;
        for (int i = 0; i < n; i++) {
          if (noise[i] >= 0.0f)
            links.add_frame(frame(src[i], now / 1000.0f, breath[i], hz, noise[i], rng), LinkKind::NODE, now);
        }
      }
      links.tick_second(now);
    }
  }
};

// A chest rising 15 times a minute shows on its link within a window and two evaluations; an
// empty room's link next to it never does. The link report carries the flag.
static void test_breathing_and_empty() {
  Room room;
  const Mac src[2] = {mac_n(1), mac_n(2)};
  const float breath[2] = {0.04f, 0.0f}, noise[2] = {0.01f, 0.01f};
  int first = -1;
  for (int s = 0; s < 180; s++) {
    room.seconds(1, src, breath, noise, 2);
    const Link *b = room.links.find(src[0]);
    if (b->breathing && first < 0)
      first = s;
    CHECK(!room.links.find(src[1])->breathing);
    CHECK(!b->active && !room.links.find(src[1])->active);  // still: no motion on either
  }
  const Link *b = room.links.find(src[0]);
  std::printf("  breathing seen after %d s, %.1f a minute\n", first, b->breath_rate);
  CHECK(first >= 32 && first <= 50);
  CHECK(b->breathing && std::fabs(b->breath_rate - 15.0f) < 1.0f);
  uint8_t out[LINK_REPORT_MAX];
  const size_t n = room.links.encode_report(mac_n(9), 1, 100, out, sizeof(out));
  CHECK(n == LINK_REPORT_HEADER_BYTES + 2 * LINK_REPORT_ENTRY_BYTES);
  for (int i = 0; i < 2; i++) {
    const uint8_t *e = out + LINK_REPORT_HEADER_BYTES + LINK_REPORT_ENTRY_BYTES * i;
    CHECK(e[13] == (Mac::from(e) == src[0] ? LINK_FLAG_BREATHING : 0));
  }
  // Slower breathing, 9 a minute, is seen too
  Room slow;
  slow.seconds(90, src, breath, noise, 1, 0.15f);
  CHECK(slow.links.find(src[0])->breathing && std::fabs(slow.links.find(src[0])->breath_rate - 9.0f) < 1.0f);
}

// Ten minutes of empty rooms, at several noise levels: never breathing.
static void test_no_false_alarms() {
  Room room;
  const Mac src[3] = {mac_n(1), mac_n(2), mac_n(3)};
  const float breath[3] = {0.0f, 0.0f, 0.0f}, noise[3] = {0.005f, 0.02f, 0.05f};
  int seconds_on = 0;
  for (int s = 0; s < 600; s++) {
    room.seconds(1, src, breath, noise, 3);
    for (const Mac &m : src)
      seconds_on += room.links.find(m)->breathing;
  }
  CHECK(seconds_on == 0);
}

// Motion on the link ends breathing at once; it comes back only once a whole window is free of it.
// A silent link stops breathing too.
static void test_motion_and_silence() {
  Room room;
  const Mac src[1] = {mac_n(1)};
  const float breath[1] = {0.04f}, still[1] = {0.01f}, moving[1] = {0.2f}, silent[1] = {-1.0f};
  room.seconds(60, src, breath, still, 1);
  CHECK(room.links.find(src[0])->breathing);
  room.seconds(3, src, breath, moving, 1);
  CHECK(room.links.find(src[0])->active && !room.links.find(src[0])->breathing);
  int back = -1;
  for (int s = 0; s < 80 && back < 0; s++) {
    room.seconds(1, src, breath, still, 1);
    if (room.links.find(src[0])->breathing)
      back = s;
    CHECK(back < 0 || !room.links.find(src[0])->active);
  }
  std::printf("  breathing again %d s after the motion\n", back);
  CHECK(back >= 32 && back <= 60);
  room.seconds(6, src, breath, silent, 1);
  CHECK(!room.links.find(src[0])->breathing && std::isnan(room.links.find(src[0])->breath_rate));
}

// At most BREATH_SLOTS links are followed; a link silent for a minute gives its slot up.
static void test_slots_bounded() {
  Room room;
  Mac src[BREATH_SLOTS + 2];
  float breath[BREATH_SLOTS + 2], noise[BREATH_SLOTS + 2];
  for (int i = 0; i < BREATH_SLOTS + 2; i++) {
    src[i] = mac_n(10 + i);
    breath[i] = 0.0f;
    noise[i] = 0.01f;
  }
  room.seconds(5, src, breath, noise, BREATH_SLOTS + 2);
  CHECK(room.bank.used() == BREATH_SLOTS && room.links.count() == BREATH_SLOTS + 2);
  noise[0] = -1.0f;  // the first goes quiet
  room.seconds(BREATH_RELEASE_MS / 1000 + 2, src, breath, noise, BREATH_SLOTS + 2);
  CHECK(room.bank.used() == BREATH_SLOTS);
  CHECK(!room.bank.follows(src[0]) && room.bank.follows(src[BREATH_SLOTS]) != room.bank.follows(src[BREATH_SLOTS + 1]));
  // Detection off: no flags, nothing followed
  room.links.set_breathing(nullptr);
  room.seconds(2, src, breath, noise, BREATH_SLOTS + 2);
  for (int i = 1; i < BREATH_SLOTS + 2; i++)
    CHECK(!room.links.find(src[i])->breathing);
}

int main() {
  test_breathing_and_empty();
  test_no_false_alarms();
  test_motion_and_silence();
  test_slots_bounded();
  std::printf("  bank: %zu bytes for %d links\n", sizeof(BreathingBank), BREATH_SLOTS);
  if (failures) {
    std::printf("%d breathing check(s) failed\n", failures);
    return 1;
  }
  std::printf("All breathing tests passed\n");
  return 0;
}
