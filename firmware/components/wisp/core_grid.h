#pragma once
// wisp-core: the grid. Who is in it, where each member is in its lifecycle, which time slot
// this node transmits in, and the ESP-NOW beacon every node sends once per round.
//
// No leader: every node applies the same rules to what it hears. Members are nodes heard
// recently; slots go by MAC order among the active members and every other node the hive knows
// (passed in by the caller), so nodes that do not all hear each other (A and C both next to B,
// not to each other) still pick distinct slots. The round is aligned to a shared clock (the
// access point's TSF) when there is one.

#include <cstddef>
#include <cstdint>
#include <cstring>

namespace wisp_core {

struct Mac {
  uint8_t b[6];
  bool operator==(const Mac &o) const { return memcmp(this->b, o.b, 6) == 0; }
  bool operator!=(const Mac &o) const { return !(*this == o); }
  bool operator<(const Mac &o) const { return memcmp(this->b, o.b, 6) < 0; }
  static Mac from(const uint8_t *p) {
    Mac m;
    memcpy(m.b, p, 6);
    return m;
  }
};

constexpr int MAX_MEMBERS = 16;          // other nodes one node keeps track of
constexpr int SLOTS = 16;                // transmit slots per round
constexpr uint32_t ROUND_US = 100000;    // 10 rounds a second
constexpr uint32_t SLOT_US = ROUND_US / SLOTS;
constexpr uint32_t SLOT_GUARD_US = 800;  // transmit this far into the slot
constexpr uint32_t LISTEN_MS = 500;      // a starting node listens this long before it transmits

enum class MemberState : uint8_t { ACTIVE = 0, QUIET = 1, MISSING = 2 };  // forgotten = dropped

struct GridTimers {
  uint32_t quiet_ms = 1500;               // about 15 missed rounds: slot freed
  uint32_t missing_ms = 5 * 60 * 1000;    // position and calibration kept
  uint32_t forget_ms = 24 * 3600 * 1000;  // dropped from the grid
};

struct Member {
  Mac mac;
  MemberState state;
  uint32_t first_heard_ms;
  uint32_t last_heard_ms;
  float rssi;  // smoothed RSSI of its beacons
  uint16_t last_seq;
  uint8_t chip;
  uint32_t uptime_s;
  uint32_t hive_hash;  // what it last said it knows
};

// Next transmit time on the shared clock for a slot, strictly after now_us plus a small lead.
inline uint64_t next_slot_time(uint64_t now_us, int slot, uint32_t lead_us = 300) {
  const uint64_t round_start = now_us - now_us % ROUND_US;
  uint64_t t = round_start + static_cast<uint64_t>(slot) * SLOT_US + SLOT_GUARD_US;
  while (t <= now_us + lead_us)
    t += ROUND_US;
  return t;
}

class Grid {
 public:
  explicit Grid(const Mac &self, GridTimers timers = {}) : self_(self), timers_(timers) {}

  const Mac &self() const { return this->self_; }
  void set_timers(const GridTimers &t) { this->timers_ = t; }
  // Empty again, for another MAC: in place, no large temporary on the caller's stack.
  void reset(const Mac &self) {
    this->self_ = self;
    this->count_ = 0;
    this->started_ = this->listened_ = false;
    this->start_ms_ = 0;
    this->version_ = 0;
  }

  // Called when this node starts (or restarts) taking part: it listens before transmitting.
  void start(uint32_t now_ms) {
    this->started_ = true;
    this->listened_ = false;
    this->start_ms_ = now_ms;
  }

  // A beacon from another node. Returns true if that node is new to this grid.
  bool on_beacon(const Mac &from, uint32_t now_ms, int8_t rssi, uint16_t seq, uint8_t chip, uint32_t uptime_s,
                 uint32_t hive_hash = 0) {
    if (from == this->self_)
      return false;
    Member *m = this->find_(from);
    bool is_new = false;
    if (m == nullptr) {
      m = this->make_room_(now_ms);
      if (m == nullptr)
        return false;  // table full of active members: this node cannot track more
      *m = Member{from, MemberState::ACTIVE, now_ms, now_ms, static_cast<float>(rssi), seq, chip, uptime_s, hive_hash};
      this->count_++;
      is_new = true;
      this->version_++;
    } else {
      if (m->state != MemberState::ACTIVE)
        this->version_++;
      if (uptime_s < m->uptime_s && m->uptime_s - uptime_s > 2)
        m->first_heard_ms = now_ms;  // it rebooted
      m->state = MemberState::ACTIVE;
      m->last_heard_ms = now_ms;
      m->rssi += 0.1f * (static_cast<float>(rssi) - m->rssi);
      m->last_seq = seq;
      m->chip = chip;
      m->uptime_s = uptime_s;
      m->hive_hash = hive_hash;
    }
    return is_new;
  }

  // Moves members along the lifecycle. Every node runs the same timers, so they agree.
  void tick(uint32_t now_ms) {
    if (this->started_ && !this->listened_ && now_ms - this->start_ms_ >= LISTEN_MS)
      this->listened_ = true;  // latched: the clock wrapping after 49.7 days must not mute it again
    for (int i = 0; i < this->count_;) {
      Member &m = this->members_[i];
      const uint32_t silent = now_ms - m.last_heard_ms;
      MemberState next = MemberState::ACTIVE;
      if (silent >= this->timers_.forget_ms) {
        this->members_[i] = this->members_[--this->count_];  // forgotten
        this->version_++;
        continue;
      }
      if (silent >= this->timers_.missing_ms)
        next = MemberState::MISSING;
      else if (silent >= this->timers_.quiet_ms)
        next = MemberState::QUIET;
      if (next != m.state) {
        m.state = next;
        this->version_++;
      }
      i++;
    }
  }

  // This node's slot, or -1 while it is still listening (tick() ends that) or when every slot
  // is taken (listener). known: other nodes the hive knows, heard directly or not; each MAC
  // below this node's counts once, whether it is an active member, known, or both.
  int self_slot(const Mac *known = nullptr, int known_n = 0) const {
    if (!this->started_ || !this->listened_)
      return -1;
    int rank = 0;
    for (int i = 0; i < this->count_; i++) {
      if (this->members_[i].state == MemberState::ACTIVE && this->members_[i].mac < this->self_)
        rank++;
    }
    for (int k = 0; k < known_n; k++) {
      if (!(known[k] < this->self_))
        continue;
      const Member *m = this->find(known[k]);
      bool seen = m != nullptr && m->state == MemberState::ACTIVE;
      for (int j = 0; j < k && !seen; j++)
        seen = known[j] == known[k];
      rank += !seen;
    }
    return rank < SLOTS ? rank : -1;
  }

  // Active members plus this node.
  int active_count() const {
    int n = 1;
    for (int i = 0; i < this->count_; i++)
      n += this->members_[i].state == MemberState::ACTIVE;
    return n;
  }

  int count() const { return this->count_; }
  const Member &member(int i) const { return this->members_[i]; }
  const Member *find(const Mac &mac) const { return const_cast<Grid *>(this)->find_(mac); }
  bool is_member(const Mac &mac) const { return this->find(mac) != nullptr; }
  // Changes whenever a member joins, changes state or is forgotten.
  uint32_t version() const { return this->version_; }

 protected:
  Member *find_(const Mac &mac) {
    for (int i = 0; i < this->count_; i++) {
      if (this->members_[i].mac == mac)
        return &this->members_[i];
    }
    return nullptr;
  }

  // A free entry, or the longest-silent non-active member when full (by time since heard, so
  // the choice holds across the clock wrap).
  Member *make_room_(uint32_t now_ms) {
    if (this->count_ < MAX_MEMBERS)
      return &this->members_[this->count_];
    int victim = -1;
    for (int i = 0; i < this->count_; i++) {
      const Member &m = this->members_[i];
      if (m.state != MemberState::ACTIVE &&
          (victim < 0 || now_ms - m.last_heard_ms > now_ms - this->members_[victim].last_heard_ms))
        victim = i;
    }
    if (victim < 0)
      return nullptr;
    this->members_[victim] = this->members_[--this->count_];
    this->version_++;
    return &this->members_[this->count_];
  }

  Mac self_;
  GridTimers timers_;
  Member members_[MAX_MEMBERS]{};
  int count_{0};
  bool started_{false};
  bool listened_{false};
  uint32_t start_ms_{0};
  uint32_t version_{0};
};

// ---- ESP-NOW beacon, protocol version 3 -------------------------------------------------
//
//  0  2  magic "WG"            12  1  chip (see CHIP_*)
//  2  1  protocol version      13  1  active nodes the sender sees (itself included)
//  3  1  type (1 beacon)       14  6  BSSID of the access point whose clock the sender follows
//  4  2  sequence number       20  4  hive hash: what the sender knows (see core_hive.h)
//  6  1  flags (bit 0 synced)  24  2  version of the sender's own row
//  7  1  slot                  26  1  row entries n (up to MAX_ROW)
//  8  4  uptime, seconds       27  8n row: neighbour MAC (6), mean RSSI (int8),
//                                     motion score x 10 (uint8, 255 = unknown)
//
// The row is what the sender hears from each neighbour (nodes and access points): the
// sender's line of the shared matrix that every node keeps (the hive).

constexpr uint8_t GRID_PROTOCOL_VERSION = 3;
constexpr uint8_t BEACON_TYPE = 1;
constexpr int MAX_ROW = 24;
constexpr size_t BEACON_HEADER_BYTES = 27;
constexpr size_t BEACON_MAX_BYTES = BEACON_HEADER_BYTES + 8 * MAX_ROW;
constexpr uint8_t BEACON_FLAG_SYNCED = 0x01;
constexpr uint8_t CHIP_ESP32 = 0, CHIP_ESP32S3 = 1, CHIP_ESP32C3 = 2, CHIP_ESP32C6 = 3, CHIP_OTHER = 255;
constexpr uint8_t SCORE_UNKNOWN = 255;

struct RowEntry {
  Mac mac;
  int8_t rssi;
  uint8_t score10;
};

struct Beacon {
  uint16_t seq;
  uint8_t flags;
  int8_t slot;
  uint32_t uptime_s;
  uint8_t chip;
  uint8_t active;
  Mac clock_bssid;
  uint32_t hive_hash;
  uint16_t row_version;
  uint8_t row_len;
  RowEntry row[MAX_ROW];
};

inline size_t encode_beacon(const Beacon &b, uint8_t *out, size_t cap) {
  const uint8_t n = b.row_len > MAX_ROW ? MAX_ROW : b.row_len;
  const size_t len = BEACON_HEADER_BYTES + 8 * static_cast<size_t>(n);
  if (len > cap)
    return 0;
  out[0] = 'W';
  out[1] = 'G';
  out[2] = GRID_PROTOCOL_VERSION;
  out[3] = BEACON_TYPE;
  out[4] = static_cast<uint8_t>(b.seq);
  out[5] = static_cast<uint8_t>(b.seq >> 8);
  out[6] = b.flags;
  out[7] = static_cast<uint8_t>(b.slot);
  for (int i = 0; i < 4; i++)
    out[8 + i] = static_cast<uint8_t>(b.uptime_s >> (8 * i));
  out[12] = b.chip;
  out[13] = b.active;
  memcpy(out + 14, b.clock_bssid.b, 6);
  for (int i = 0; i < 4; i++)
    out[20 + i] = static_cast<uint8_t>(b.hive_hash >> (8 * i));
  out[24] = static_cast<uint8_t>(b.row_version);
  out[25] = static_cast<uint8_t>(b.row_version >> 8);
  out[26] = n;
  for (int i = 0; i < n; i++) {
    uint8_t *e = out + BEACON_HEADER_BYTES + 8 * i;
    memcpy(e, b.row[i].mac.b, 6);
    e[6] = static_cast<uint8_t>(b.row[i].rssi);
    e[7] = b.row[i].score10;
  }
  return len;
}

inline bool decode_beacon(const uint8_t *p, size_t len, Beacon &b) {
  if (len < BEACON_HEADER_BYTES || p[0] != 'W' || p[1] != 'G' || p[2] != GRID_PROTOCOL_VERSION ||
      p[3] != BEACON_TYPE)
    return false;
  const uint8_t n = p[26];
  if (n > MAX_ROW || len < BEACON_HEADER_BYTES + 8 * static_cast<size_t>(n))
    return false;
  b.seq = static_cast<uint16_t>(p[4] | (p[5] << 8));
  b.flags = p[6];
  b.slot = static_cast<int8_t>(p[7]);
  b.uptime_s = static_cast<uint32_t>(p[8]) | (static_cast<uint32_t>(p[9]) << 8) |
               (static_cast<uint32_t>(p[10]) << 16) | (static_cast<uint32_t>(p[11]) << 24);
  b.chip = p[12];
  b.active = p[13];
  memcpy(b.clock_bssid.b, p + 14, 6);
  b.hive_hash = static_cast<uint32_t>(p[20]) | (static_cast<uint32_t>(p[21]) << 8) |
                (static_cast<uint32_t>(p[22]) << 16) | (static_cast<uint32_t>(p[23]) << 24);
  b.row_version = static_cast<uint16_t>(p[24] | (p[25] << 8));
  b.row_len = n;
  for (int i = 0; i < n; i++) {
    const uint8_t *e = p + BEACON_HEADER_BYTES + 8 * i;
    memcpy(b.row[i].mac.b, e, 6);
    b.row[i].rssi = static_cast<int8_t>(e[6]);
    b.row[i].score10 = e[7];
  }
  return true;
}

}  // namespace wisp_core
