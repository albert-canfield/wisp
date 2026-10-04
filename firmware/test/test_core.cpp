// Host tests for wisp-core (core_* files). Run: firmware/test/run.sh
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>

#include "core_link_motion.h"
#include "core_raw_packet.h"

static int failures = 0;
#define CHECK(cond)                                                    \
  do {                                                                 \
    if (!(cond)) {                                                     \
      std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);      \
      failures++;                                                      \
    }                                                                  \
  } while (0)

using namespace wisp_core;

// A 20 MHz HT frame: a fixed channel shape, a gain, and optional noise on every subcarrier.
static CsiRecord make_frame(float gain, float noise, std::mt19937 &rng) {
  std::normal_distribution<float> n(0.0f, 1.0f);
  CsiRecord r{};
  r.len = 256;
  r.sig_mode = 1;
  for (int i = 0; i < 128; i++) {
    const float shape = 20.0f + 8.0f * std::sin(i * 0.3f);
    const float re = gain * shape * (1.0f + noise * n(rng));
    const float im = gain * shape * 0.5f * (1.0f + noise * n(rng));
    r.data[2 * i] = static_cast<int8_t>(std::fmax(-127.0f, std::fmin(127.0f, im)));
    r.data[2 * i + 1] = static_cast<int8_t>(std::fmax(-127.0f, std::fmin(127.0f, re)));
  }
  return r;
}

static void test_raw_packet() {
  CsiRecord r{};
  const uint8_t src[6] = {0xa8, 0x29, 0x48, 0xdb, 0xb6, 0x70};
  const uint8_t node[6] = {0xac, 0x27, 0x6e, 0xa8, 0xc7, 0x7c};
  memcpy(r.source, src, 6);
  r.rssi = -51;
  r.channel = 6;
  r.len = 4;
  r.data[0] = -3;
  uint8_t out[RAW_PACKET_MAX];
  const size_t n = encode_raw_csi(r, node, 0x01020304, out, sizeof(out));
  CHECK(n == RAW_HEADER_BYTES + 4);
  CHECK(memcmp(out, "WISP", 4) == 0);
  CHECK(out[4] == PROTOCOL_VERSION && out[5] == PACKET_RAW_CSI);
  CHECK(out[6] == RAW_HEADER_BYTES && out[7] == 0);
  CHECK(out[8] == 0x04 && out[11] == 0x01);  // little-endian sequence
  CHECK(memcmp(out + 12, node, 6) == 0 && memcmp(out + 18, src, 6) == 0);
  CHECK(static_cast<int8_t>(out[28]) == -51 && out[30] == 6);
  CHECK(out[36] == 4 && static_cast<int8_t>(out[38]) == -3);
  CHECK(encode_raw_csi(r, node, 0, out, RAW_HEADER_BYTES + 3) == 0);  // too small: refused

  const uint8_t sub[] = {'W', 'S', 'U', 'B', PROTOCOL_VERSION};
  const uint8_t links[] = {'W', 'S', 'U', 'B', PROTOCOL_VERSION, STREAM_LINKS};
  const uint8_t old[] = {'W', 'S', 'U', 'B', 0};
  CHECK(parse_subscribe(sub, sizeof(sub)) == STREAM_RAW_CSI);
  CHECK(parse_subscribe(links, sizeof(links)) == STREAM_LINKS);
  CHECK(parse_subscribe(old, sizeof(old)) == 0);
  CHECK(parse_subscribe(sub, 4) == 0);
}

static void test_shape_ignores_gain() {
  std::mt19937 rng(1);
  float a[SHAPE_LEN], b[SHAPE_LEN];
  CHECK(lltf_shape(make_frame(1.0f, 0.0f, rng), a));
  CHECK(lltf_shape(make_frame(2.0f, 0.0f, rng), b));
  float worst = 0.0f;
  for (int j = 0; j < SHAPE_LEN; j++)
    worst = std::fmax(worst, std::fabs(a[j] - b[j]));
  CHECK(worst < 0.05f);  // only int8 rounding left
  CsiRecord legacy_too_short{};
  legacy_too_short.len = 64;
  CHECK(!lltf_shape(legacy_too_short, a));
}

static void test_motion_score() {
  std::mt19937 rng(2);
  LinkMotion link;
  MotionDetector detector(2.0f);
  float score = NAN;
  // 60 quiet seconds at 20 frames a second, with the gain wandering (AGC): stays near 1.
  for (int s = 0; s < 60; s++) {
    for (int f = 0; f < 20; f++)
      link.add_frame(make_frame(1.0f + 0.3f * std::sin(s * 0.7f + f), 0.01f, rng));
    score = link.tick();
    if (s > 5)
      CHECK(!detector.update(score));
  }
  std::printf("  quiet: score %.2f, baseline spread %.2f%%\n", score, link.baseline());
  CHECK(score > 0.5f && score < 1.5f);

  // Someone walks through the link: the shape moves a lot. Score and detector go up quickly.
  bool detected = false;
  for (int s = 0; s < 5; s++) {
    for (int f = 0; f < 20; f++)
      link.add_frame(make_frame(1.0f, 0.15f, rng));
    score = link.tick();
    detected = detector.update(score) || detected;
  }
  std::printf("  moving: score %.2f\n", score);
  CHECK(score > 3.0f);
  CHECK(detected);

  // Quiet again: back below the off threshold within a few seconds; the baseline barely moved.
  for (int s = 0; s < 10; s++) {
    for (int f = 0; f < 20; f++)
      link.add_frame(make_frame(1.0f, 0.01f, rng));
    score = link.tick();
    detector.update(score);
  }
  std::printf("  quiet again: score %.2f\n", score);
  CHECK(score < 1.5f);
  CHECK(!detector.update(score));

  // No frames in the last second: unknown, and the detector keeps its state.
  CHECK(std::isnan(link.tick()));
}

int main() {
  test_raw_packet();
  test_shape_ignores_gain();
  test_motion_score();
  if (failures) {
    std::printf("%d check(s) failed\n", failures);
    return 1;
  }
  std::printf("All core tests passed\n");
  return 0;
}
