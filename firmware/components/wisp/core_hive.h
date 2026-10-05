#pragma once
// wisp-core: the hive. Every node keeps the latest row of every node it knows, directly or
// relayed, so all of them hold the same matrix and can solve the same layout. A short hash over
// (origin, row version) pairs tells nodes whether they know the same things.
//
// A row is one node's view: the RSSI it receives from each neighbour (nodes and access points),
// averaged over about 20 s, since a person in the way or a fading second moves single readings
// by several dB. Its version moves at once when a neighbour comes or goes, and when an average
// moved by ROW_CHANGE_DB at most once a minute: nodes stand still, so the hash, the rows and the
// layout stay still too, busy house or not. A version always means the same readings: the beacon
// carries the published row, never the live one.
//
// Each row also knows when its origin was last heard directly. Relays carry that as an age, so a
// departed node's row grows old everywhere and expires after a day, even though the remaining
// nodes keep relaying it to each other: each relay rounds the age up, so a row only grows older
// on its way around. A node that hears its own row relayed back at a version it has not reached,
// or at its current version with other readings (it restarted, and its count began again), takes
// that row back: its version and its readings, which become the start of its averages. The
// others already hold it, so nothing changes for them and a restart does not move the layout.
//
// A full hive makes room for a new node by dropping the row unheard for longest, once that is
// longer than ROW_EVICT_MS (a missing node); rows of nodes still heard are never dropped.

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>

#include "core_grid.h"

namespace wisp_core {

constexpr int MAX_ROWS = MAX_MEMBERS + 1;  // every member plus this node
constexpr int8_t ROW_CHANGE_DB = 3;
constexpr float ROW_SMOOTHING = 0.005f;            // per set_own (every 100 ms round): about 20 s
constexpr uint32_t ROW_MIN_CHANGE_MS = 60 * 1000;  // readings alone move the row at most this often
constexpr uint32_t ROW_EXPIRE_MS = 24 * 3600 * 1000;  // same as forgetting a node
constexpr uint32_t ROW_AGE_UNIT_MS = 4000;  // relayed ages: 16 bits of 4 s, up to 72 h, beyond the expiry
constexpr uint32_t ROW_AGE_MAX_MS = 65535u * ROW_AGE_UNIT_MS;
constexpr uint32_t ROW_EVICT_MS = 5 * 60 * 1000;  // a full hive drops a row unheard this long
constexpr uint32_t SELF_JUMP_MIN_MS = 10 * 1000;  // a duplicate MAC must not make the version race
constexpr uint32_t ROW_SEED_MS = 60 * 1000;      // a taken-back reading starts a neighbour's average this long

struct HiveEntry {
  Mac mac;
  int8_t rssi;
};

struct HiveRow {
  Mac origin;
  uint16_t version;
  uint8_t len;
  HiveEntry entries[MAX_ROW];
  uint32_t heard_ms;  // when its origin was last heard directly, by this node or by a relayer
};

// True if version a is newer than b, with wraparound.
inline bool row_newer(uint16_t a, uint16_t b) { return static_cast<int16_t>(a - b) > 0; }

inline uint32_t fnv1a(uint32_t h, const uint8_t *p, size_t n) {
  for (size_t i = 0; i < n; i++) {
    h ^= p[i];
    h *= 16777619u;
  }
  return h;
}

class Hive {
 public:
  explicit Hive(const Mac &self) { this->reset(self); }

  // Empty again, for another MAC: in place, no large temporary on the caller's stack.
  void reset(const Mac &self) {
    this->self_ = self;
    this->rows_[0] = HiveRow{};
    this->rows_[0].origin = self;  // reserved: a full hive never refuses this node's own row
    this->count_ = 1;
    this->relay_cursor_ = 0;
    this->hash_dirty_ = true;
    this->last_self_jump_ms_ = 0;
    this->self_jumps_ = 0;
    this->live_n_ = 0;
    this->own_bumps_ = 0;
    this->seed_n_ = 0;
  }

  // This node's live view, every round. Publishes a new version of its row when a neighbour came
  // or went, or (at most every ROW_MIN_CHANGE_MS) when an averaged reading moved ROW_CHANGE_DB
  // from the published one. Returns true if it did.
  bool set_own(const HiveEntry *entries, int n, uint32_t now_ms) {
    if (n > MAX_ROW)
      n = MAX_ROW;
    float avg[MAX_ROW];
    const bool seeding = this->seed_n_ > 0 && now_ms - this->seed_ms_ < ROW_SEED_MS;
    for (int i = 0; i < n; i++) {
      avg[i] = entries[i].rssi;
      for (int k = 0; seeding && k < this->seed_n_; k++) {
        if (this->seed_[k].mac == entries[i].mac)
          avg[i] = this->seed_[k].rssi;  // a neighbour first heard again since the row was taken back
      }
      for (int k = 0; k < this->live_n_; k++) {
        if (this->live_[k].mac == entries[i].mac) {
          avg[i] = this->live_avg_[k] + ROW_SMOOTHING * (static_cast<float>(entries[i].rssi) - this->live_avg_[k]);
          break;
        }
      }
    }
    for (int i = 0; i < n; i++) {
      this->live_[i].mac = entries[i].mac;
      this->live_avg_[i] = avg[i];
    }
    this->live_n_ = n;
    HiveRow *own = this->find_(this->self_);
    own->heard_ms = now_ms;
    bool came_or_went = own->len != n;
    bool moved = false;
    for (int i = 0; i < n && !came_or_went; i++) {
      const HiveEntry *old = find_entry_(*own, entries[i].mac);
      if (old == nullptr)
        came_or_went = true;
      else if (std::fabs(static_cast<float>(old->rssi) - avg[i]) >= ROW_CHANGE_DB)
        moved = true;
    }
    const bool due = this->own_bumps_ == 0 || now_ms - this->last_own_bump_ms_ >= ROW_MIN_CHANGE_MS;
    if (!came_or_went && !(moved && due))
      return false;
    own->len = static_cast<uint8_t>(n);
    for (int i = 0; i < n; i++) {
      const float r = std::fmax(-127.0f, std::fmin(127.0f, std::round(avg[i])));
      own->entries[i] = HiveEntry{entries[i].mac, static_cast<int8_t>(r)};
    }
    own->version++;
    this->last_own_bump_ms_ = now_ms;
    this->own_bumps_++;
    this->hash_dirty_ = true;
    return true;
  }

  // A row heard from its origin (age 0) or relayed by another node, which says how long ago the
  // origin was last heard directly. Returns true if it was news.
  bool on_row(const Mac &origin, uint16_t version, const HiveEntry *entries, int n, uint32_t now_ms,
              uint32_t age_ms = 0) {
    if (n > MAX_ROW)
      return false;
    if (origin == this->self_) {
      HiveRow *own = this->find_(this->self_);
      const bool ahead = row_newer(version, own->version);
      const bool stale = version == own->version && !same_entries_(*own, entries, n);
      if (!ahead && !stale)
        return false;
      if (this->self_jumps_ > 0 && now_ms - this->last_self_jump_ms_ < SELF_JUMP_MIN_MS)
        return false;
      // Take the row back as the others hold it, and start the averages from its readings.
      own->version = version;
      own->len = static_cast<uint8_t>(n);
      memcpy(own->entries, entries, sizeof(HiveEntry) * n);
      memcpy(this->seed_, entries, sizeof(HiveEntry) * n);
      this->seed_n_ = n;
      this->seed_ms_ = now_ms;
      for (int k = 0; k < this->live_n_; k++) {
        for (int i = 0; i < n; i++) {
          if (this->live_[k].mac == entries[i].mac)
            this->live_avg_[k] = entries[i].rssi;
        }
      }
      this->last_own_bump_ms_ = now_ms;
      this->own_bumps_++;
      this->last_self_jump_ms_ = now_ms;
      this->self_jumps_++;
      this->hash_dirty_ = true;
      return true;
    }
    if (age_ms >= ROW_EXPIRE_MS)
      return false;  // nobody has heard its origin for a day: it is being forgotten
    const uint32_t heard = now_ms - age_ms;
    HiveRow *row = this->find_(origin);
    if (row != nullptr && !row_newer(version, row->version)) {
      // A saturated age says only "very old": it must not refresh anything.
      if (version == row->version && age_ms < ROW_AGE_MAX_MS && static_cast<int32_t>(heard - row->heard_ms) > 0)
        row->heard_ms = heard;
      return false;
    }
    if (row == nullptr && (row = this->add_(origin, now_ms, age_ms)) == nullptr)
      return false;
    row->version = version;
    row->len = static_cast<uint8_t>(n);
    memcpy(row->entries, entries, sizeof(HiveEntry) * n);
    row->heard_ms = heard;
    this->hash_dirty_ = true;
    return true;
  }

  // Drops rows whose origin nobody has heard directly for a day (it was forgotten).
  void expire(uint32_t now_ms) {
    for (int i = 0; i < this->count_;) {
      if (this->rows_[i].origin != this->self_ && now_ms - this->rows_[i].heard_ms > ROW_EXPIRE_MS) {
        this->rows_[i] = this->rows_[--this->count_];
        this->hash_dirty_ = true;
        continue;
      }
      i++;
    }
  }

  // Order-independent summary of what this node knows: same hash, same rows.
  uint32_t hash() {
    if (!this->hash_dirty_)
      return this->hash_;
    uint32_t total = 0;
    for (int i = 0; i < this->count_; i++) {
      uint8_t buf[8];
      memcpy(buf, this->rows_[i].origin.b, 6);
      buf[6] = static_cast<uint8_t>(this->rows_[i].version);
      buf[7] = static_cast<uint8_t>(this->rows_[i].version >> 8);
      total += fnv1a(2166136261u, buf, sizeof(buf));  // a sum does not depend on row order
    }
    this->hash_ = total;
    this->hash_dirty_ = false;
    return total;
  }

  // The row to relay next: rotates through rows other than this node's own.
  const HiveRow *next_relay() {
    if (this->count_ < 2)
      return nullptr;
    for (int tries = 0; tries < this->count_; tries++) {
      this->relay_cursor_ = (this->relay_cursor_ + 1) % this->count_;
      if (this->rows_[this->relay_cursor_].origin != this->self_)
        return &this->rows_[this->relay_cursor_];
    }
    return nullptr;
  }

  const HiveRow *own() const { return const_cast<Hive *>(this)->find_(this->self_); }
  const HiveRow *find(const Mac &m) const { return const_cast<Hive *>(this)->find_(m); }
  int count() const { return this->count_; }
  const HiveRow &row(int i) const { return this->rows_[i]; }
  const Mac &self() const { return this->self_; }
  // Times this node took its own row back from a relayed copy: after a restart, once or twice.
  // Many more mean another device uses this node's MAC.
  uint32_t self_jumps() const { return this->self_jumps_; }

 protected:
  static const HiveEntry *find_entry_(const HiveRow &row, const Mac &m) {
    for (int i = 0; i < row.len; i++) {
      if (row.entries[i].mac == m)
        return &row.entries[i];
    }
    return nullptr;
  }
  HiveRow *find_(const Mac &m) {
    for (int i = 0; i < this->count_; i++) {
      if (this->rows_[i].origin == m)
        return &this->rows_[i];
    }
    return nullptr;
  }
  static bool same_entries_(const HiveRow &row, const HiveEntry *entries, int n) {
    if (row.len != n)
      return false;
    for (int i = 0; i < n; i++) {
      if (!(row.entries[i].mac == entries[i].mac) || row.entries[i].rssi != entries[i].rssi)
        return false;
    }
    return true;
  }
  // A new origin's row; when full, in place of the row unheard for longest if that is a missing
  // node's and older than the newcomer's.
  HiveRow *add_(const Mac &m, uint32_t now_ms, uint32_t age_ms) {
    if (this->count_ >= MAX_ROWS) {
      int stalest = -1;
      for (int i = 0; i < this->count_; i++) {
        if (this->rows_[i].origin == this->self_)
          continue;
        if (stalest < 0 || now_ms - this->rows_[i].heard_ms > now_ms - this->rows_[stalest].heard_ms)
          stalest = i;
      }
      if (stalest < 0)
        return nullptr;
      const uint32_t silent = now_ms - this->rows_[stalest].heard_ms;
      if (silent <= ROW_EVICT_MS || silent <= age_ms)
        return nullptr;
      this->rows_[stalest] = this->rows_[--this->count_];
      this->hash_dirty_ = true;
    }
    HiveRow &r = this->rows_[this->count_++];
    r = HiveRow{};
    r.origin = m;
    return &r;
  }

  Mac self_;
  HiveRow rows_[MAX_ROWS]{};
  int count_{0};
  int relay_cursor_{0};
  uint32_t hash_{0};
  bool hash_dirty_{true};
  uint32_t last_self_jump_ms_{0};
  uint32_t self_jumps_{0};
  // The live view behind the own row: neighbour and its averaged RSSI.
  HiveEntry live_[MAX_ROW]{};
  float live_avg_[MAX_ROW]{};
  int live_n_{0};
  uint32_t last_own_bump_ms_{0};
  uint32_t own_bumps_{0};
  // Readings of a row taken back after a restart, to start the averages of neighbours heard again.
  HiveEntry seed_[MAX_ROW]{};
  int seed_n_{0};
  uint32_t seed_ms_{0};
};

// ---- ESP-NOW hive row frame (type 2), sent right after the beacon to relay one row ----------
//
//  0  2  magic "WG"        6  6  origin MAC
//  2  1  protocol version  12 2  row version
//  3  1  type (2)          14 2  age: time since the origin was last heard directly, 4 s units
//  4  2  sequence number   16 1  entries n, then 7n: neighbour MAC (6), RSSI (int8)

constexpr uint8_t ROW_FRAME_TYPE = 2;
constexpr size_t ROW_FRAME_HEADER_BYTES = 17;
constexpr size_t ROW_FRAME_MAX_BYTES = ROW_FRAME_HEADER_BYTES + 7 * MAX_ROW;

inline size_t encode_row_frame(const HiveRow &row, uint16_t seq, uint32_t now_ms, uint8_t *out, size_t cap) {
  const size_t len = ROW_FRAME_HEADER_BYTES + 7 * static_cast<size_t>(row.len);
  if (len > cap)
    return 0;
  out[0] = 'W';
  out[1] = 'G';
  out[2] = GRID_PROTOCOL_VERSION;
  out[3] = ROW_FRAME_TYPE;
  out[4] = static_cast<uint8_t>(seq);
  out[5] = static_cast<uint8_t>(seq >> 8);
  memcpy(out + 6, row.origin.b, 6);
  out[12] = static_cast<uint8_t>(row.version);
  out[13] = static_cast<uint8_t>(row.version >> 8);
  // Rounded up: a relay must never make a row look fresher than it is, or two nodes relaying a
  // departed node's row to each other would keep it young for ever.
  const uint32_t elapsed = now_ms - row.heard_ms;
  const uint32_t units = elapsed / ROW_AGE_UNIT_MS + (elapsed % ROW_AGE_UNIT_MS != 0 ? 1 : 0);
  const uint16_t age = static_cast<uint16_t>(units > 65535 ? 65535 : units);
  out[14] = static_cast<uint8_t>(age);
  out[15] = static_cast<uint8_t>(age >> 8);
  out[16] = row.len;
  for (int i = 0; i < row.len; i++) {
    memcpy(out + ROW_FRAME_HEADER_BYTES + 7 * i, row.entries[i].mac.b, 6);
    out[ROW_FRAME_HEADER_BYTES + 7 * i + 6] = static_cast<uint8_t>(row.entries[i].rssi);
  }
  return len;
}

inline bool decode_row_frame(const uint8_t *p, size_t len, Mac &origin, uint16_t &version, uint32_t &age_ms,
                             HiveEntry *entries, int &n) {
  if (len < ROW_FRAME_HEADER_BYTES || p[0] != 'W' || p[1] != 'G' || p[2] != GRID_PROTOCOL_VERSION ||
      p[3] != ROW_FRAME_TYPE)
    return false;
  n = p[16];
  if (n > MAX_ROW || len < ROW_FRAME_HEADER_BYTES + 7 * static_cast<size_t>(n))
    return false;
  memcpy(origin.b, p + 6, 6);
  version = static_cast<uint16_t>(p[12] | (p[13] << 8));
  age_ms = static_cast<uint32_t>(p[14] | (p[15] << 8)) * ROW_AGE_UNIT_MS;
  for (int i = 0; i < n; i++) {
    memcpy(entries[i].mac.b, p + ROW_FRAME_HEADER_BYTES + 7 * i, 6);
    entries[i].rssi = static_cast<int8_t>(p[ROW_FRAME_HEADER_BYTES + 7 * i + 6]);
  }
  return true;
}

}  // namespace wisp_core
