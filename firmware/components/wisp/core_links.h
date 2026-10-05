#pragma once
// wisp-core: every link this node receives on (from access points and other nodes), each with
// its own motion score and threshold (QuietThreshold), optionally breathing (core_breathing.h),
// and the link report that carries them to Home Assistant (type 2 in docs/PROTOCOL.md), with the
// node pairs the hive confirms (core_confirm.h).

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>

#include "core_breathing.h"
#include "core_csi_record.h"
#include "core_grid.h"
#include "core_link_motion.h"
#include "core_raw_packet.h"

namespace wisp_core {

constexpr int MAX_LINKS = 20;  // access points plus up to 16 other nodes
constexpr uint8_t PACKET_LINK_REPORT = 2;
constexpr size_t LINK_REPORT_HEADER_BYTES = 24;
constexpr size_t LINK_REPORT_ENTRY_BYTES = 14;
constexpr uint32_t LINK_EXPIRE_MS = 10 * 60 * 1000;  // a link silent this long frees its entry
constexpr uint8_t LINK_FLAG_MOTION = 0x01;
constexpr uint8_t LINK_FLAG_CONFIRMED = 0x02;  // the hive confirms its motion
constexpr uint8_t LINK_FLAG_BREATHING = 0x04;  // breathing seen on it (core_breathing.h)
constexpr uint32_t QUIET_HOLD_MS = 10000;  // after motion the hive confirmed, seconds this long are not quiet
constexpr uint8_t REPORT_FLAG_CONFIRMS = 0x01;  // links carry LINK_FLAG_CONFIRMED, confirmed pairs follow
constexpr uint8_t REPORT_FLAG_PAIRS_TRUNCATED = 0x02;
constexpr int MAX_REPORT_PAIRS = 16;
constexpr size_t LINK_REPORT_PAIR_BYTES = 12;
constexpr size_t LINK_REPORT_MAX = LINK_REPORT_HEADER_BYTES + LINK_REPORT_ENTRY_BYTES * MAX_LINKS + 1 +
                                   LINK_REPORT_PAIR_BYTES * MAX_REPORT_PAIRS;

enum class LinkKind : uint8_t { ACCESS_POINT = 0, NODE = 1 };

// Node pairs the hive confirms, for the link report: a and b in MAC order.
struct PairList {
  Mac a[MAX_REPORT_PAIRS];
  Mac b[MAX_REPORT_PAIRS];
  int n{0};
  bool truncated{false};  // more were confirmed than fit
};

struct Link {
  Mac source;
  LinkKind kind;
  LinkMotion motion{};
  MotionDetector detector{};
  QuietThreshold quiet{};
  float score{NAN};
  float threshold{DEFAULT_THRESHOLD};  // its own: the user's, or higher for a noisy link
  bool active{false};
  bool confirmed{false};  // set after each tick_second() by core_confirm.h
  bool breathing{false};
  float breath_rate{NAN};  // a minute, while breathing
  uint32_t last_frame_ms{0};
  int32_t rssi_sum{0};  // since the last report
  uint16_t frames{0};   // since the last report
  float rssi_avg{0.0f};  // slow average, for the hive row
};

// The score a beacon carries for a link, x 10: normalised so the link's own threshold reads
// DEFAULT_THRESHOLD, measured from a quiet 1.0 as the hysteresis is, so a node judging it at
// DEFAULT_THRESHOLD (on at it, off halfway back to 1) follows the receiver's own flag exactly.
// Equal to the score itself while the link uses the default threshold.
inline uint8_t beacon_score10(const Link &l) {
  if (std::isnan(l.score))
    return SCORE_UNKNOWN;
  const float span = l.threshold > 1.0f ? l.threshold - 1.0f : DEFAULT_THRESHOLD - 1.0f;
  const float n = 1.0f + (l.score - 1.0f) * (DEFAULT_THRESHOLD - 1.0f) / span;
  return static_cast<uint8_t>(std::fmin(254.0f, std::fmax(0.0f, n * 10.0f)));
}

class LinkTable {
 public:
  // The user's Motion threshold: the least any link uses.
  void set_threshold(float t) {
    this->threshold_ = t;
    for (int i = 0; i < this->count_; i++) {
      Link &l = this->links_[i];
      l.threshold = l.quiet.threshold(t);
      l.detector.set_threshold(l.threshold);
    }
  }
  float threshold() const { return this->threshold_; }

  // Breathing detection on (a bank the caller owns) or off (nullptr).
  void set_breathing(BreathingBank *bank) {
    this->breathing_ = bank;
    for (int i = 0; i < this->count_; i++) {
      this->links_[i].breathing = false;
      this->links_[i].breath_rate = NAN;
    }
  }

  // Adds one CSI frame. Returns false when the table is full.
  bool add_frame(const CsiRecord &r, LinkKind kind, uint32_t now_ms) {
    Link *l = this->find_or_add_(Mac::from(r.source), kind, now_ms);
    if (l == nullptr)
      return false;
    l->kind = kind;
    float shape[SHAPE_LEN];
    if (lltf_shape(r, shape)) {
      l->motion.add_shape(shape);
      if (this->breathing_ != nullptr)
        this->breathing_->add_shape(l->source, shape, now_ms);
    }
    l->last_frame_ms = now_ms;
    l->rssi_sum += r.rssi;
    l->frames++;
    l->rssi_avg += (l->rssi_avg == 0.0f ? 1.0f : 0.02f) * (static_cast<float>(r.rssi) - l->rssi_avg);
    return true;
  }

  // Once a second: score every link at its own threshold, follow its breathing, and drop links
  // that have been silent for a long time.
  void tick_second(uint32_t now_ms) {
    for (int i = 0; i < this->count_;) {
      Link &l = this->links_[i];
      if (now_ms - l.last_frame_ms > LINK_EXPIRE_MS) {
        if (this->breathing_ != nullptr)
          this->breathing_->release(l.source);
        this->links_[i] = this->links_[--this->count_];
        continue;
      }
      l.score = l.motion.tick();
      l.confirmed = false;
      l.threshold = l.quiet.threshold(this->threshold_);
      l.detector.set_threshold(l.threshold);
      if (std::isnan(l.score)) {
        l.detector.reset();  // a silent link reports no motion, it does not keep the last state
        l.active = false;
      } else {
        l.active = l.detector.update(l.score);
      }
      l.breathing = this->breathing_ != nullptr && this->breathing_->tick(l.source, l.active, now_ms);
      l.breath_rate = l.breathing ? this->breathing_->rate(l.source) : NAN;
      i++;
    }
  }

  // Once a second, after the hive's confirmation: whether it confirms motion anywhere. Seconds
  // without, QUIET_HOLD_MS after the last, teach each link that is not flagged its quiet scores.
  void learn_quiet(bool hive_moving, uint32_t now_ms) {
    if (hive_moving) {
      this->moving_ms_ = now_ms;
      this->moved_ = true;
    }
    if (this->moved_ && now_ms - this->moving_ms_ <= QUIET_HOLD_MS)
      return;
    this->moved_ = false;
    for (int i = 0; i < this->count_; i++) {
      Link &l = this->links_[i];
      if (!l.active && !std::isnan(l.score))
        l.quiet.learn(l.score);
    }
  }

  // The highest threshold any link uses.
  float max_threshold() const {
    float t = this->threshold_;
    for (int i = 0; i < this->count_; i++)
      t = std::fmax(t, this->links_[i].threshold);
    return t;
  }

  // Writes a link report and starts a new interval. Returns its length, or 0 if it does not fit.
  // With pairs, the report says the links carry confirmation and lists the pairs after them.
  size_t encode_report(const Mac &self, uint32_t seq, uint32_t uptime_s, uint8_t *out, size_t cap,
                       const PairList *pairs = nullptr) {
    const int n_pairs = pairs == nullptr ? 0 : (pairs->n < MAX_REPORT_PAIRS ? pairs->n : MAX_REPORT_PAIRS);
    const size_t links_end = LINK_REPORT_HEADER_BYTES + LINK_REPORT_ENTRY_BYTES * static_cast<size_t>(this->count_);
    const size_t len = links_end + (pairs == nullptr ? 0 : 1 + LINK_REPORT_PAIR_BYTES * static_cast<size_t>(n_pairs));
    if (len > cap)
      return 0;
    memcpy(out, "WISP", 4);
    out[4] = PROTOCOL_VERSION;
    out[5] = PACKET_LINK_REPORT;
    put_u16(out + 6, LINK_REPORT_HEADER_BYTES);
    put_u32(out + 8, seq);
    memcpy(out + 12, self.b, 6);
    out[18] = static_cast<uint8_t>(this->count_);
    out[19] = pairs == nullptr ? 0 : REPORT_FLAG_CONFIRMS | (pairs->truncated ? REPORT_FLAG_PAIRS_TRUNCATED : 0);
    put_u32(out + 20, uptime_s);
    for (int i = 0; i < this->count_; i++) {
      Link &l = this->links_[i];
      uint8_t *e = out + LINK_REPORT_HEADER_BYTES + LINK_REPORT_ENTRY_BYTES * i;
      memcpy(e, l.source.b, 6);
      e[6] = static_cast<uint8_t>(l.kind);
      e[7] = static_cast<uint8_t>(l.frames ? static_cast<int8_t>(l.rssi_sum / l.frames) : int8_t(-128));
      put_u16(e + 8, to_u16_(l.score * 100.0f));
      put_u16(e + 10, to_u16_(l.motion.spread() * 100.0f));
      e[12] = static_cast<uint8_t>(l.frames > 255 ? 255 : l.frames);
      e[13] = static_cast<uint8_t>((l.active ? LINK_FLAG_MOTION : 0) | (l.confirmed ? LINK_FLAG_CONFIRMED : 0) |
                                   (l.breathing ? LINK_FLAG_BREATHING : 0));
      l.rssi_sum = 0;
      l.frames = 0;
    }
    if (pairs != nullptr) {
      uint8_t *p = out + links_end;
      *p++ = static_cast<uint8_t>(n_pairs);
      for (int i = 0; i < n_pairs; i++, p += LINK_REPORT_PAIR_BYTES) {
        memcpy(p, pairs->a[i].b, 6);
        memcpy(p + 6, pairs->b[i].b, 6);
      }
    }
    return len;
  }

  const Link *find(const Mac &m) const {
    for (int i = 0; i < this->count_; i++) {
      if (this->links_[i].source == m)
        return &this->links_[i];
    }
    return nullptr;
  }
  int count() const { return this->count_; }
  const Link &link(int i) const { return this->links_[i]; }
  void set_confirmed(int i, bool c) { this->links_[i].confirmed = c && this->links_[i].active; }

 protected:
  static uint16_t to_u16_(float v) {
    if (std::isnan(v))
      return 0xFFFF;
    if (v <= 0.0f)
      return 0;
    return v >= 65534.0f ? 65534 : static_cast<uint16_t>(v + 0.5f);
  }

  Link *find_or_add_(const Mac &m, LinkKind kind, uint32_t now_ms) {
    for (int i = 0; i < this->count_; i++) {
      if (this->links_[i].source == m)
        return &this->links_[i];
    }
    if (this->count_ >= MAX_LINKS)
      return nullptr;
    Link &l = this->links_[this->count_++];
    l = Link{};
    l.source = m;
    l.kind = kind;
    l.threshold = this->threshold_;
    l.detector.set_threshold(this->threshold_);
    l.last_frame_ms = now_ms;
    return &l;
  }

  Link links_[MAX_LINKS]{};
  int count_{0};
  float threshold_{DEFAULT_THRESHOLD};
  BreathingBank *breathing_{nullptr};
  uint32_t moving_ms_{0};
  bool moved_{false};
};

}  // namespace wisp_core
