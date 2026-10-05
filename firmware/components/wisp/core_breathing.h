#pragma once
// wisp-core: breathing on a link, as radars see a chest rise 0.2 to 0.5 times a second. Portable,
// no allocation; floats (per node, nothing has to match across chips).
//
// Someone sitting perfectly still leaves the motion score as quiet as an empty room, but their
// breathing moves the shape of the signal slowly and regularly, mostly along one direction. Per
// link: each frame's shape (lltf_shape) in BREATH_DIM groups of BREATH_GROUP neighbouring
// subcarriers, averaged per 125 ms bin; its deviation from a slow mean, projected on the
// direction it moves most (followed by Oja's rule); the last 32 s of that projection in a ring
// of int16. Every BREATH_EVAL_MS, when the link reported no motion for the whole ring (a window
// holding a walk reads as breathing in its slow parts), the ring is detrended, Hann-windowed, and
// a DFT gives the power at 0.16 to 0.59 Hz (bins of 1/32 Hz) and at 0.72 to 2.0 Hz. A window is
// positive when the band's peak is a local maximum and BREATH_RATIO times the median of the
// reference bins. Breathing turns on after two positive windows in a row with peaks within
// BREATH_PEAK_STEP bins, off after two negative ones; the rate is the peak's frequency.
//
// On the owner's recordings (firmware/tools/breathing_study.py): no window flagged on an empty
// floor (2,286 motion-free link evaluations, 16 links) nor at night (10,628, 4 links over 3 h);
// sitting quietly, from the first window clear of the walk in, at least one link breathed at
// every evaluation, a median 13 times a minute. One 100 s sitting only: off by default.
//
// Up to BREATH_SLOTS links are followed, about 0.8 KB each.

#include <cmath>
#include <cstdint>

#include "core_grid.h"
#include "core_link_motion.h"

namespace wisp_core {

constexpr int BREATH_SLOTS = 8;  // the access point and the other nodes of a floor
constexpr int BREATH_GROUP = 3;
constexpr int BREATH_DIM = SHAPE_LEN / BREATH_GROUP;  // 17
constexpr uint32_t BREATH_BIN_MS = 125;               // 8 a second
constexpr int BREATH_LEN = 256;                       // 32 s
constexpr uint32_t BREATH_WINDOW_MS = BREATH_LEN * BREATH_BIN_MS;
constexpr uint32_t BREATH_EVAL_MS = 4000;
constexpr uint32_t BREATH_GAP_BINS = 40;        // 5 s without frames starts the ring over
constexpr uint32_t BREATH_RELEASE_MS = 60000;  // a link silent this long gives up its slot
constexpr float BREATH_SLOW = 0.02f;           // slow mean, per bin: about 6 s
constexpr float BREATH_ETA = 0.05f;            // Oja step, per bin
constexpr float BREATH_VAR_RATE = 0.05f;       // running size of the deviation, for the step
constexpr int BREATH_BAND_LO = 5;              // DFT bins of 1/32 Hz: 0.16 Hz
constexpr int BREATH_BAND_HI = 19;             // 0.59 Hz
constexpr int BREATH_REF_LO = 23;              // 0.72 Hz
constexpr int BREATH_REF_HI = 64;              // 2.0 Hz, the Nyquist frequency of 8 bins a second
constexpr int BREATH_REF_N = BREATH_REF_HI - BREATH_REF_LO + 1;
constexpr float BREATH_RATIO = 24.0f;
constexpr int BREATH_PEAK_STEP = 2;
constexpr float BREATH_SCALE = 4096.0f;  // ring units per unit of projection

struct BreathSlot {
  Mac source;
  bool used{false};
  bool started{false};  // a bin is being filled
  bool primed{false};   // mean and direction set
  bool moved{false};    // motion since moved_ms
  bool on{false};
  uint8_t pos{0};
  uint8_t neg{0};
  int8_t prev_peak{-1};
  uint16_t acc_n{0};
  uint16_t head{0};    // next write
  uint16_t filled{0};  // bins in the ring
  int16_t last{0};
  uint32_t bin{0};  // the bin being filled: ms / BREATH_BIN_MS
  uint32_t last_frame_ms{0};
  uint32_t moved_ms{0};
  uint32_t eval_ms{0};
  float var{0.0f};
  float rate{NAN};   // per minute, while on
  float ratio{0.0f};  // the latest window's
  float acc[BREATH_DIM]{};
  float mean[BREATH_DIM]{};
  float dir[BREATH_DIM]{};
  int16_t ring[BREATH_LEN]{};
};

class BreathingBank {
 public:
  // One frame's shape for the link from src: a slot is taken while one is free.
  void add_shape(const Mac &src, const float shape[SHAPE_LEN], uint32_t now_ms) {
    BreathSlot *s = this->take_(src, now_ms);
    if (s == nullptr)
      return;
    s->last_frame_ms = now_ms;
    const uint32_t bin = now_ms / BREATH_BIN_MS;
    if (!s->started) {
      s->started = true;
      s->bin = bin;
    } else if (bin != s->bin) {
      close_(*s, bin);
    }
    for (int g = 0; g < BREATH_DIM; g++) {
      const float *p = shape + g * BREATH_GROUP;
      s->acc[g] += (p[0] + p[1] + p[2]) / 3.0f;
    }
    s->acc_n++;
  }

  // Once a second per link, with its motion flag. Evaluates every BREATH_EVAL_MS; returns
  // whether the link breathes.
  bool tick(const Mac &src, bool moving, uint32_t now_ms) {
    BreathSlot *s = this->find_(src);
    if (s == nullptr)
      return false;
    if (now_ms - s->last_frame_ms > BREATH_GAP_BINS * BREATH_BIN_MS) {
      s->filled = 0;
      off_(*s);
      return false;
    }
    if (moving) {
      s->moved = true;
      s->moved_ms = now_ms;
      off_(*s);
      return false;
    }
    if (s->moved && now_ms - s->moved_ms > BREATH_WINDOW_MS)
      s->moved = false;
    if (now_ms - s->eval_ms < BREATH_EVAL_MS)
      return s->on;
    s->eval_ms = now_ms;
    if (s->filled < BREATH_LEN || s->moved)
      return s->on;
    int peak = 0;
    bool local = false;
    float rate = NAN;
    s->ratio = this->window_(*s, peak, local, rate);
    if (s->ratio >= BREATH_RATIO && local) {
      const int step = peak - s->prev_peak;
      s->pos = (s->pos > 0 && step <= BREATH_PEAK_STEP && step >= -BREATH_PEAK_STEP) ? sat_(s->pos) : 1;
      s->neg = 0;
      s->prev_peak = static_cast<int8_t>(peak);
      if (s->on || s->pos >= 2)
        s->rate = rate;
    } else {
      s->neg = sat_(s->neg);
      s->pos = 0;
      s->prev_peak = -1;
    }
    const bool was = s->on;
    s->on = (was || s->pos >= 2) && !(was && s->neg >= 2);
    if (!s->on)
      s->rate = NAN;
    return s->on;
  }

  // Breaths a minute on the link from src, NAN while it does not breathe.
  float rate(const Mac &src) const {
    const BreathSlot *s = this->find_(src);
    return s == nullptr ? NAN : s->rate;
  }
  // The latest window's band peak over the reference median, for diagnostics.
  float ratio(const Mac &src) const {
    const BreathSlot *s = this->find_(src);
    return s == nullptr ? 0.0f : s->ratio;
  }
  bool follows(const Mac &src) const { return this->find_(src) != nullptr; }
  void release(const Mac &src) {
    BreathSlot *s = this->find_(src);
    if (s != nullptr)
      *s = BreathSlot{};
  }
  int used() const {
    int n = 0;
    for (const BreathSlot &s : this->slots_)
      n += s.used;
    return n;
  }

 protected:
  static uint8_t sat_(uint8_t v) { return v < 255 ? static_cast<uint8_t>(v + 1) : v; }

  static void off_(BreathSlot &s) {
    s.on = false;
    s.pos = s.neg = 0;
    s.prev_peak = -1;
    s.rate = NAN;
  }

  // The bin being filled is complete: its projection goes into the ring, repeated over any bins
  // without frames; a long silence starts the ring over.
  static void close_(BreathSlot &s, uint32_t bin) {
    if (s.acc_n > 0) {
      float x[BREATH_DIM];
      if (!s.primed) {
        for (int g = 0; g < BREATH_DIM; g++) {
          s.mean[g] = s.acc[g] / s.acc_n;
          s.dir[g] = 1.0f / std::sqrt(static_cast<float>(BREATH_DIM));
        }
        s.var = 0.0f;
        s.primed = true;
      }
      float y = 0.0f, v = 0.0f;
      for (int g = 0; g < BREATH_DIM; g++) {
        x[g] = s.acc[g] / s.acc_n - s.mean[g];
        s.mean[g] += BREATH_SLOW * x[g];
        y += s.dir[g] * x[g];
        v += x[g] * x[g];
      }
      s.var = s.var == 0.0f ? v : s.var + BREATH_VAR_RATE * (v - s.var);
      if (s.var > 0.0f) {
        float norm = 0.0f;
        for (int g = 0; g < BREATH_DIM; g++) {
          s.dir[g] += BREATH_ETA * y * (x[g] - y * s.dir[g]) / s.var;
          norm += s.dir[g] * s.dir[g];
        }
        norm = std::sqrt(norm);
        for (int g = 0; norm > 0.0f && g < BREATH_DIM; g++)
          s.dir[g] /= norm;
      }
      float z = 0.0f;
      for (int g = 0; g < BREATH_DIM; g++)
        z += s.dir[g] * x[g];
      z = std::fmax(-32767.0f, std::fmin(32767.0f, std::round(z * BREATH_SCALE)));
      s.last = static_cast<int16_t>(z);
    }
    const uint32_t gap = bin - s.bin;
    if (gap > BREATH_GAP_BINS) {
      s.filled = 0;
    } else {
      for (uint32_t i = 0; i < gap && i < static_cast<uint32_t>(BREATH_LEN); i++) {
        s.ring[s.head] = s.last;
        s.head = static_cast<uint16_t>((s.head + 1) % BREATH_LEN);
        if (s.filled < BREATH_LEN)
          s.filled++;
      }
    }
    s.bin = bin;
    s.acc_n = 0;
    for (float &a : s.acc)
      a = 0.0f;
  }

  // One window: the ratio of the band's peak to the reference median, the peak's bin, whether it
  // is a local maximum, and its rate per minute.
  float window_(const BreathSlot &s, int &peak, bool &local, float &rate) {
    float *y = this->work_;
    float mean = 0.0f;
    for (int i = 0; i < BREATH_LEN; i++) {
      y[i] = static_cast<float>(s.ring[(s.head + i) % BREATH_LEN]) / BREATH_SCALE;
      mean += y[i];
    }
    mean /= BREATH_LEN;
    // Least squares line, then a Hann window
    constexpr float mid = 0.5f * (BREATH_LEN - 1);
    float sxy = 0.0f;
    for (int i = 0; i < BREATH_LEN; i++)
      sxy += (static_cast<float>(i) - mid) * (y[i] - mean);
    const float sxx = static_cast<float>(BREATH_LEN) * (static_cast<float>(BREATH_LEN) * BREATH_LEN - 1.0f) / 12.0f;
    const float slope = sxy / sxx;
    constexpr float two_pi = 6.28318530718f;
    for (int i = 0; i < BREATH_LEN; i++) {
      const float hann = 0.5f - 0.5f * std::cos(two_pi * static_cast<float>(i) / (BREATH_LEN - 1));
      y[i] = (y[i] - mean - slope * (static_cast<float>(i) - mid)) * hann;
    }
    float band[BREATH_BAND_HI - BREATH_BAND_LO + 3];  // one more bin each side, for the local maximum
    for (int k = BREATH_BAND_LO - 1; k <= BREATH_BAND_HI + 1; k++)
      band[k - BREATH_BAND_LO + 1] = power_(y, k);
    float ref[BREATH_REF_N];
    for (int k = BREATH_REF_LO; k <= BREATH_REF_HI; k++) {
      const float p = power_(y, k);
      int at = k - BREATH_REF_LO;
      for (; at > 0 && ref[at - 1] > p; at--)
        ref[at] = ref[at - 1];
      ref[at] = p;
    }
    const float median = 0.5f * (ref[(BREATH_REF_N - 1) / 2] + ref[BREATH_REF_N / 2]);
    int best = 1;
    for (int i = 2; i <= BREATH_BAND_HI - BREATH_BAND_LO + 1; i++) {
      if (band[i] > band[best])
        best = i;
    }
    peak = BREATH_BAND_LO + best - 1;
    local = band[best] >= band[best - 1] && band[best] >= band[best + 1];
    // Parabolic interpolation between the bins around the peak
    const float den = band[best - 1] - 2.0f * band[best] + band[best + 1];
    const float shift = den < 0.0f ? 0.5f * (band[best - 1] - band[best + 1]) / den : 0.0f;
    rate = 60.0f * (static_cast<float>(peak) + shift) * 1000.0f / static_cast<float>(BREATH_WINDOW_MS);
    return median > 0.0f ? band[best] / median : 0.0f;
  }

  // Power of DFT bin k of the window (Goertzel).
  static float power_(const float *y, int k) {
    const float c = 2.0f * std::cos(6.28318530718f * static_cast<float>(k) / BREATH_LEN);
    float s1 = 0.0f, s2 = 0.0f;
    for (int i = 0; i < BREATH_LEN; i++) {
      const float s0 = y[i] + c * s1 - s2;
      s2 = s1;
      s1 = s0;
    }
    return s1 * s1 + s2 * s2 - c * s1 * s2;
  }

  BreathSlot *find_(const Mac &src) {
    for (BreathSlot &s : this->slots_) {
      if (s.used && s.source == src)
        return &s;
    }
    return nullptr;
  }
  const BreathSlot *find_(const Mac &src) const { return const_cast<BreathingBank *>(this)->find_(src); }

  // Its slot, else a free one, else the one silent longest once that is BREATH_RELEASE_MS.
  BreathSlot *take_(const Mac &src, uint32_t now_ms) {
    if (BreathSlot *s = this->find_(src))
      return s;
    BreathSlot *pick = nullptr;
    for (BreathSlot &s : this->slots_) {
      if (!s.used) {
        pick = &s;
        break;
      }
      if (now_ms - s.last_frame_ms > BREATH_RELEASE_MS &&
          (pick == nullptr || now_ms - s.last_frame_ms > now_ms - pick->last_frame_ms))
        pick = &s;
    }
    if (pick == nullptr)
      return nullptr;
    *pick = BreathSlot{};
    pick->used = true;
    pick->source = src;
    pick->eval_ms = now_ms;
    return pick;
  }

  BreathSlot slots_[BREATH_SLOTS]{};
  float work_[BREATH_LEN]{};  // one window at a time
};

}  // namespace wisp_core
