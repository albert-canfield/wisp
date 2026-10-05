#pragma once
// wisp-core: every link this node receives on (from access points and other nodes), each with
// its own motion score, and the link report that carries them to Home Assistant
// (type 2 in docs/PROTOCOL.md).

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>

#include "core_csi_record.h"
#include "core_grid.h"
#include "core_link_motion.h"
#include "core_raw_packet.h"

namespace wisp_core {

constexpr int MAX_LINKS = 20;  // access points plus up to 16 other nodes
constexpr uint8_t PACKET_LINK_REPORT = 2;
constexpr size_t LINK_REPORT_HEADER_BYTES = 24;
constexpr size_t LINK_REPORT_ENTRY_BYTES = 14;
constexpr size_t LINK_REPORT_MAX = LINK_REPORT_HEADER_BYTES + LINK_REPORT_ENTRY_BYTES * MAX_LINKS;
constexpr uint32_t LINK_EXPIRE_MS = 10 * 60 * 1000;  // a link silent this long frees its entry

enum class LinkKind : uint8_t { ACCESS_POINT = 0, NODE = 1 };

struct Link {
  Mac source;
  LinkKind kind;
  LinkMotion motion{};
  MotionDetector detector{};
  float score{NAN};
  bool active{false};
  uint32_t last_frame_ms{0};
  int32_t rssi_sum{0};  // since the last report
  uint16_t frames{0};   // since the last report
  float rssi_avg{0.0f};  // slow average, for the hive row
};

class LinkTable {
 public:
  void set_threshold(float t) {
    this->threshold_ = t;
    for (int i = 0; i < this->count_; i++)
      this->links_[i].detector.set_threshold(t);
  }

  // Adds one CSI frame. Returns false when the table is full.
  bool add_frame(const CsiRecord &r, LinkKind kind, uint32_t now_ms) {
    Link *l = this->find_or_add_(Mac::from(r.source), kind, now_ms);
    if (l == nullptr)
      return false;
    l->kind = kind;
    l->motion.add_frame(r);
    l->last_frame_ms = now_ms;
    l->rssi_sum += r.rssi;
    l->frames++;
    l->rssi_avg += (l->rssi_avg == 0.0f ? 1.0f : 0.02f) * (static_cast<float>(r.rssi) - l->rssi_avg);
    return true;
  }

  // Once a second: score every link, and drop links that have been silent for a long time.
  void tick_second(uint32_t now_ms) {
    for (int i = 0; i < this->count_;) {
      Link &l = this->links_[i];
      if (now_ms - l.last_frame_ms > LINK_EXPIRE_MS) {
        this->links_[i] = this->links_[--this->count_];
        continue;
      }
      l.score = l.motion.tick();
      if (std::isnan(l.score)) {
        l.detector.reset();  // a silent link reports no motion, it does not keep the last state
        l.active = false;
      } else {
        l.active = l.detector.update(l.score);
      }
      i++;
    }
  }

  // Writes a link report and starts a new interval. Returns its length, or 0 if it does not fit.
  size_t encode_report(const Mac &self, uint32_t seq, uint32_t uptime_s, uint8_t *out, size_t cap) {
    const size_t len = LINK_REPORT_HEADER_BYTES + LINK_REPORT_ENTRY_BYTES * static_cast<size_t>(this->count_);
    if (len > cap)
      return 0;
    memcpy(out, "WISP", 4);
    out[4] = PROTOCOL_VERSION;
    out[5] = PACKET_LINK_REPORT;
    put_u16(out + 6, LINK_REPORT_HEADER_BYTES);
    put_u32(out + 8, seq);
    memcpy(out + 12, self.b, 6);
    out[18] = static_cast<uint8_t>(this->count_);
    out[19] = 0;
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
      e[13] = l.active ? 1 : 0;
      l.rssi_sum = 0;
      l.frames = 0;
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
    l.detector.set_threshold(this->threshold_);
    l.last_frame_ms = now_ms;
    return &l;
  }

  Link links_[MAX_LINKS]{};
  int count_{0};
  float threshold_{2.0f};
};

}  // namespace wisp_core
