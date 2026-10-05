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

// Right after start, a reading lower than usual must not look like motion: unknown while settling.
static void test_start_up_is_not_motion() {
  std::mt19937 rng(9);
  LinkMotion link;
  MotionDetector detector(2.0f);
  for (int s = 0; s < 40; s++) {
    const float noise = s < 3 ? 0.002f : 0.02f;  // starts unusually calm, then normal
    for (int f = 0; f < 20; f++)
      link.add_frame(make_frame(1.0f, noise, rng));
    const float score = link.tick();
    CHECK(!detector.update(score));
    if (s < 20)
      CHECK(std::isnan(score));
  }
  CHECK(link.settled());
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
  CHECK(link.settled());
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

// Off halfway back to a quiet 1.0: the default turns off at 1.5, a low threshold still turns off.
static void test_detector_hysteresis() {
  for (const float t : {1.2f, 2.0f, 3.0f}) {
    MotionDetector d(t);
    const float off = 1.0f + 0.5f * (t - 1.0f);
    CHECK(!d.update(t - 0.01f));
    CHECK(d.update(t));
    CHECK(d.update(off + 0.01f));  // still on above the off point
    CHECK(d.update(NAN));          // unknown keeps the state
    CHECK(!d.update(off - 0.01f));
  }
  MotionDetector low(1.2f);  // a quiet link around 1.0 turns the lowest setting off again
  CHECK(low.update(1.25f));
  CHECK(!low.update(1.03f));
  MotionDetector def(2.0f);  // the default, as before: on at 2, off below 1.5
  CHECK(def.update(2.0f) && def.update(1.5f) && !def.update(1.49f));
}

// Someone sitting and working for an hour: busy most seconds, a quiet moment now and then. The
// baseline must not climb to them (the score would fade to 1 and they would vanish), while a
// lasting change with no quiet moments is still learned.
static void test_still_person_is_not_absorbed() {
  std::mt19937 rng(11);
  auto second = [&rng](LinkMotion &link, float noise) {
    for (int f = 0; f < 20; f++)
      link.add_frame(make_frame(1.0f, noise, rng));
    return link.tick();
  };
  LinkMotion sitting, changed;
  for (int s = 0; s < 300; s++) {  // five quiet minutes
    second(sitting, 0.01f);
    second(changed, 0.01f);
  }
  float sit_score = 0.0f, change_score = 0.0f;
  for (int s = 0; s < 3600; s++) {
    const float a = second(sitting, s % 45 < 5 ? 0.01f : 0.03f);  // a few still seconds each 45
    const float b = second(changed, 0.03f);                        // busier for good, no pauses
    if (s >= 3300) {  // the last five minutes
      sit_score += a / 300.0f;
      change_score += b / 300.0f;
    }
  }
  std::printf("  after an hour: sitting %.2f, lasting change %.2f\n", sit_score, change_score);
  CHECK(sit_score > 1.8f);     // still clearly someone there
  CHECK(change_score < 1.3f);  // a lasting change has become the new normal
}

// One second of a link at its own threshold, as LinkTable does: flagged seconds never count.
static bool cfar_second(QuietThreshold &q, MotionDetector &d, float score, bool hive_moving = false) {
  d.set_threshold(q.threshold(2.0f));
  const bool active = d.update(score);
  if (!hive_moving && !active)
    q.learn(score);
  return active;
}

// A noisy link (the owner's office corner: 1 to 4% of empty seconds at 2.0 or more) gets its own,
// higher threshold and flags rarely; a quiet one keeps the user's.
static void test_quiet_threshold() {
  std::mt19937 rng(21);
  std::lognormal_distribution<float> noisy(std::log(1.3f), 0.22f), quiet(std::log(1.05f), 0.05f);
  QuietThreshold qn, qq;
  MotionDetector dn, dq;
  int flagged_before = 0, flagged_after = 0;
  for (int s = 0; s < 3600; s++) {
    const float a = noisy(rng);
    if (s < 600)
      flagged_before += a >= 2.0f;
    const bool on = cfar_second(qn, dn, a);
    if (s >= 2400)
      flagged_after += on;
    cfar_second(qq, dq, quiet(rng));
  }
  std::printf("  noisy link: threshold %.2f, flagged %.2f%% of its last 20 min (%.2f%% at or above 2.0)\n",
              qn.threshold(2.0f), 100.0f * flagged_after / 1200, 100.0f * flagged_before / 600);
  CHECK(flagged_before > 6);  // the case: over 1% at 2.0
  CHECK(qn.threshold(2.0f) > 2.2f && qn.threshold(2.0f) < 3.5f);
  CHECK(flagged_after < 12);  // under 1% now
  CHECK(qq.threshold(2.0f) == 2.0f);
  CHECK(qq.threshold(3.0f) == 3.0f && qn.threshold(6.0f) == 6.0f);  // never below the user's

  // Warm-up: the user's threshold until QUIET_WARMUP quiet seconds are counted (decayed: about 200)
  QuietThreshold w;
  for (int s = 0; s < 190; s++)
    w.learn(1.95f);
  CHECK(w.learned() < QUIET_WARMUP && w.threshold(2.0f) == 2.0f);
  for (int s = 0; s < 20; s++)
    w.learn(1.95f);
  CHECK(w.learned() >= QUIET_WARMUP && w.threshold(2.0f) > 2.0f);

  // Decay: the noise goes away (a fridge replaced), the threshold follows within the hour
  float settled = 0.0f;
  for (int s = 0; s < 3600; s++) {
    cfar_second(qn, dn, quiet(rng));
    if (s == 600)
      settled = qn.threshold(2.0f);
  }
  std::printf("  noise gone: threshold %.2f after 10 min, %.2f after an hour\n", settled, qn.threshold(2.0f));
  CHECK(settled > 2.0f);
  CHECK(qn.threshold(2.0f) == 2.0f);

  // People do not raise it: busy seconds the hive confirms are never learned
  QuietThreshold qp;
  MotionDetector dp;
  for (int s = 0; s < 600; s++)
    cfar_second(qp, dp, quiet(rng));
  for (int s = 0; s < 1800; s++)  // half an hour of someone moving about: up to 1.9 without a flag
    cfar_second(qp, dp, 1.2f + 0.7f * static_cast<float>(s % 7) / 6.0f, true);
  CHECK(qp.threshold(2.0f) == 2.0f && qp.learned() > 400.0f);
}

int main() {
  test_quiet_threshold();
  test_raw_packet();
  test_still_person_is_not_absorbed();
  test_detector_hysteresis();
  test_shape_ignores_gain();
  test_start_up_is_not_motion();
  test_motion_score();
  if (failures) {
    std::printf("%d check(s) failed\n", failures);
    return 1;
  }
  std::printf("All core tests passed\n");
  return 0;
}
