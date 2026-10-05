#pragma once
// wisp-core: motion score for one link, from its CSI frames. Portable, no allocation.
//
// Each frame becomes a "shape": the amplitudes of the usable LLTF subcarriers divided by their
// mean, so automatic gain changes cancel out. Running mean and variance per subcarrier give the
// spread (how much the shape moves, in percent). A baseline learns the spread of a quiet room:
// for the first SETTLE_TICKS seconds both ways (the score is unknown meanwhile, so a start-up
// reading never looks like motion), then quickly downwards and very slowly upwards, so people
// moving do not become the new normal.
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
    if (!lltf_shape(r, s))
      return;
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
    if (this->baseline_ <= 0.0f) {
      this->baseline_ = sp;
    } else if (this->settle_ < SETTLE_TICKS) {
      this->baseline_ += BASELINE_SETTLE * (sp - this->baseline_);
    } else if (sp < this->baseline_) {
      this->baseline_ += BASELINE_DOWN * (sp - this->baseline_);
    } else {
      this->baseline_ += BASELINE_UP * (sp - this->baseline_);
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
  static constexpr float BASELINE_UP = 0.001f;   // per tick: about 17 min to follow slow drift

  float alpha_;
  float mean_[SHAPE_LEN]{};
  float var_[SHAPE_LEN]{};
  uint32_t frames_{0};
  uint32_t frames_since_tick_{0};
  float baseline_{0.0f};
  uint32_t settle_{0};
};

// On above the threshold, off below 75% of it, so the state does not flicker at the edge.
class MotionDetector {
 public:
  explicit MotionDetector(float threshold = 2.0f) : threshold_(threshold) {}
  void set_threshold(float t) { this->threshold_ = t; }
  void reset() { this->active_ = false; }
  bool update(float score) {
    if (std::isnan(score))
      return this->active_;
    if (!this->active_ && score >= this->threshold_)
      this->active_ = true;
    else if (this->active_ && score < 0.75f * this->threshold_)
      this->active_ = false;
    return this->active_;
  }

 protected:
  float threshold_;
  bool active_{false};
};

}  // namespace wisp_core
