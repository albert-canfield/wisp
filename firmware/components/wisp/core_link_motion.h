#pragma once
// wisp-core: motion score for one link, from its CSI frames. Portable, no allocation.
//
// Each frame becomes a "shape": the amplitudes of the usable LLTF subcarriers divided by their
// mean, so automatic gain changes cancel out. Running mean and variance per subcarrier give the
// spread (how much the shape moves, in percent). A baseline learns the spread of a quiet room:
// for the first SETTLE_TICKS seconds both ways (the score is unknown meanwhile, so a start-up
// reading never looks like motion), then quickly downwards, and upwards only slowly and only
// towards the quietest second of the last QUIET_WINDOW_MINUTES minutes: someone sitting still for
// hours still has quiet moments, so they never become the new normal, while a real change in the
// room (furniture moved, a new reflection) has none and is learned in about half an hour.
// Score = spread / baseline: 1 is as quiet as usual, higher means more movement.

#include <cmath>
#include <cstdint>

#include "core_csi_record.h"

namespace wisp_core {

// LLTF subcarriers -26..-1 and 2..26: subcarrier 1 shares the first word, which some chips
// flag as invalid, so it is always left out to keep every frame the same length.
constexpr int SHAPE_LEN = 51;

inline bool lltf_shape(const CsiRecord &r, float out[SHAPE_LEN]) {
  if (r.len < 128)
    return false;
  int n = 0;
  float sum = 0.0f;
  for (int i = 2; i < 64; i++) {
    if (i > 26 && i < 38)
      continue;  // guard band around the band edges
    const float im = r.data[2 * i];
    const float re = r.data[2 * i + 1];
    const float a = std::sqrt(re * re + im * im);
    out[n++] = a;
    sum += a;
  }
  if (sum <= 0.0f)
    return false;
  const float k = static_cast<float>(n) / sum;
  for (int j = 0; j < n; j++)
    out[j] *= k;
  return true;
}

class LinkMotion {
 public:
  // alpha: weight of each new frame in the running statistics, about 1 / frames per second.
  explicit LinkMotion(float alpha = 0.05f) : alpha_(alpha) {}

  void add_frame(const CsiRecord &r) {
    float s[SHAPE_LEN];
    if (lltf_shape(r, s))
      this->add_shape(s);
  }

  // One frame's shape (lltf_shape), for a caller that needs it too.
  void add_shape(const float s[SHAPE_LEN]) {
    if (this->frames_ == 0) {
      for (int j = 0; j < SHAPE_LEN; j++) {
        this->mean_[j] = s[j];
        this->var_[j] = 0.0f;
      }
    } else {
      for (int j = 0; j < SHAPE_LEN; j++) {
        const float d = s[j] - this->mean_[j];
        this->mean_[j] += this->alpha_ * d;
        this->var_[j] = (1.0f - this->alpha_) * (this->var_[j] + this->alpha_ * d * d);
      }
    }
    this->frames_++;
    this->frames_since_tick_++;
  }

  // Spread of the shape, in percent.
  float spread() const {
    float total = 0.0f;
    for (int j = 0; j < SHAPE_LEN; j++)
      total += std::sqrt(this->var_[j]);
    return 100.0f * total / SHAPE_LEN;
  }

  // Call once a second. Updates the baseline and returns the score, or NAN while there is no data.
  float tick() {
    const uint32_t fresh = this->frames_since_tick_;
    this->frames_since_tick_ = 0;
    if (fresh == 0 || this->frames_ < WARMUP_FRAMES)
      return NAN;
    const float sp = this->spread();
    // Quietest second per minute, for the last QUIET_WINDOW_MINUTES minutes
    if (this->minute_ticks_ == 0 || sp < this->quiet_[this->minute_])
      this->quiet_[this->minute_] = sp;
    if (++this->minute_ticks_ >= 60) {
      this->minute_ticks_ = 0;
      this->minute_ = static_cast<uint8_t>((this->minute_ + 1) % QUIET_WINDOW_MINUTES);
      if (this->minutes_ < QUIET_WINDOW_MINUTES)
        this->minutes_++;
    }
    float quietest = this->quiet_[this->minute_];
    for (uint8_t m = 0; m < this->minutes_; m++)
      quietest = std::fmin(quietest, this->quiet_[m]);
    if (this->baseline_ <= 0.0f) {
      this->baseline_ = sp;
    } else if (this->settle_ < SETTLE_TICKS) {
      this->baseline_ += BASELINE_SETTLE * (sp - this->baseline_);
    } else if (sp < this->baseline_) {
      this->baseline_ += BASELINE_DOWN * (sp - this->baseline_);
    } else if (quietest > this->baseline_) {
      this->baseline_ += BASELINE_UP * (quietest - this->baseline_);
    }
    if (this->settle_ < SETTLE_TICKS) {
      this->settle_++;
      return NAN;
    }
    return this->baseline_ > 0.0f ? sp / this->baseline_ : NAN;
  }

  float baseline() const { return this->baseline_; }
  bool settled() const { return this->settle_ >= SETTLE_TICKS; }

 protected:
  static constexpr uint32_t WARMUP_FRAMES = 40;
  static constexpr uint32_t SETTLE_TICKS = 20;   // seconds of learning before scores count
  static constexpr float BASELINE_SETTLE = 0.2f;
  static constexpr float BASELINE_DOWN = 0.05f;  // per tick: about 20 s to learn a quieter room
  static constexpr float BASELINE_UP = 0.001f;   // per tick: about 17 min to follow a lasting change
  static constexpr uint8_t QUIET_WINDOW_MINUTES = 10;

  float alpha_;
  float mean_[SHAPE_LEN]{};
  float var_[SHAPE_LEN]{};
  uint32_t frames_{0};
  uint32_t frames_since_tick_{0};
  float baseline_{0.0f};
  uint32_t settle_{0};
  float quiet_[QUIET_WINDOW_MINUTES]{};  // quietest spread of each minute, a ring
  uint8_t minute_{0};
  uint8_t minute_ticks_{0};
  uint8_t minutes_{0};  // full minutes in the ring
};

constexpr float DEFAULT_THRESHOLD = 2.0f;  // the Motion threshold setting's default

// On at the threshold, off halfway between it and a quiet 1.0 (1.5 for the default 2), so the
// state does not flicker at the edge. Measured from 1.0, not 0: a quiet link sits near 1 and
// would never turn a low threshold off again.
class MotionDetector {
 public:
  explicit MotionDetector(float threshold = DEFAULT_THRESHOLD) : threshold_(threshold) {}
  void set_threshold(float t) { this->threshold_ = t; }
  void reset() { this->active_ = false; }
  bool update(float score) {
    if (std::isnan(score))
      return this->active_;
    if (!this->active_ && score >= this->threshold_)
      this->active_ = true;
    else if (this->active_ && score < this->off())
      this->active_ = false;
    return this->active_;
  }
  float off() const { return 1.0f + 0.5f * (this->threshold_ - 1.0f); }
  float threshold() const { return this->threshold_; }

 protected:
  float threshold_;
  bool active_{false};
};

// A link's own threshold, from its quiet scores (CFAR, as a radar sets each cell's threshold from
// its own noise, so every link flags about as rarely with nobody there). One link of the owner's
// empty floor flagged 20% of its seconds at 2.0, most under 3%, some never.
//
// A histogram of the scores of the seconds the caller says are quiet (no confirmed motion in the
// hive lately, and this link not flagged), in QUIET_BINS log-spaced bins from QUIET_LO to
// QUIET_HI, decayed so it covers about QUIET_SECONDS of them. The threshold sits QUIET_MARGIN
// above the score that QUIET_PFA of them exceed, and never below the user's. The link's flagged
// seconds never count, so a threshold rises only while more than QUIET_PFA of the quiet seconds
// sit within the margin below it: the margin lets it climb, a step at a time, until the link's
// noise lies under it. Until QUIET_WARMUP of them are counted (decayed: about 200 s), the user's.
// Replayed over the owner's evening (firmware/tools/cfar_study.py): on the empty floor, 0.36% of
// link-seconds flagged instead of 2.14%, while sitting at the desk some link still flagged in 63%
// of seconds (67% before) and the hive confirmed 38% (49%); every walking second stayed confirmed.
constexpr int QUIET_BINS = 32;
constexpr float QUIET_LO = 1.0f;  // scores up to this share the first bin, from QUIET_HI the last
constexpr float QUIET_HI = 8.0f;
constexpr float QUIET_PFA = 0.005f;  // the share of quiet seconds above the threshold, before the margin
constexpr float QUIET_MARGIN = 1.1f;
constexpr float QUIET_SECONDS = 1200.0f;  // about 20 min of quiet seconds
constexpr float QUIET_WARMUP = 180.0f;    // quiet seconds before the link's own threshold counts

class QuietThreshold {
 public:
  void reset() {
    for (float &c : this->bins_)
      c = 0.0f;
    this->total_ = 0.0f;
  }

  // One quiet second's score.
  void learn(float score) {
    if (std::isnan(score))
      return;
    constexpr float keep = 1.0f - 1.0f / QUIET_SECONDS;
    for (float &c : this->bins_)
      c *= keep;
    this->total_ = this->total_ * keep + 1.0f;
    this->bins_[bin_(score)] += 1.0f;
  }

  // The score a share pfa of the learned seconds exceed, interpolated within its bin.
  float quantile(float pfa) const {
    const float want = pfa * this->total_;
    float above = 0.0f;
    for (int b = QUIET_BINS - 1; b >= 0; b--) {
      if (this->bins_[b] > 0.0f && above + this->bins_[b] >= want)
        return QUIET_LO * std::exp((static_cast<float>(b + 1) - (want - above) / this->bins_[b]) * log_ratio_());
      above += this->bins_[b];
    }
    return QUIET_LO;
  }

  float threshold(float user) const {
    if (this->total_ < QUIET_WARMUP)
      return user;
    return std::fmax(user, this->quantile(QUIET_PFA) * QUIET_MARGIN);
  }

  float learned() const { return this->total_; }

 protected:
  static float log_ratio_() { return std::log(QUIET_HI / QUIET_LO) / static_cast<float>(QUIET_BINS); }
  static int bin_(float score) {
    if (!(score > QUIET_LO))
      return 0;
    if (!(score < QUIET_HI))
      return QUIET_BINS - 1;
    const int b = static_cast<int>(std::log(score / QUIET_LO) / log_ratio_());
    return b < QUIET_BINS ? b : QUIET_BINS - 1;
  }

  float bins_[QUIET_BINS]{};
  float total_{0.0f};
};

}  // namespace wisp_core
