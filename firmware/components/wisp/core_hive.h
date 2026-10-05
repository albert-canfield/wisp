#pragma once
// wisp-core: the hive. Every node keeps the latest row of every node it knows, directly or
// relayed, so all of them hold the same matrix and can solve the same layout. A short hash over
// (origin, row version) pairs tells nodes whether they know the same things.
//
// A row is one node's view: the RSSI it receives from each neighbour (nodes and access points).
// Its version only moves when the view changes enough to matter (a neighbour comes or goes, or a
// reading moves by ROW_CHANGE_DB), so the hash stays still in a calm house.

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>

#include "core_grid.h"

namespace wisp_core {

constexpr int MAX_ROWS = MAX_MEMBERS + 1;  // every member plus this node
constexpr int8_t ROW_CHANGE_DB = 3;
constexpr uint32_t ROW_EXPIRE_MS = 24 * 3600 * 1000;  // same as forgetting a node

struct HiveEntry {
  Mac mac;
  int8_t rssi;
};

struct HiveRow {
  Mac origin;
  uint16_t version;
  uint8_t len;
  HiveEntry entries[MAX_ROW];
  uint32_t received_ms;
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
  explicit Hive(const Mac &self) : self_(self) {}

  // This node's own view. Bumps its version when it changed enough. Returns true if it did.
  bool set_own(const HiveEntry *entries, int n, uint32_t now_ms) {
    if (n > MAX_ROW)
      n = MAX_ROW;
    HiveRow *own = this->find_or_add_(this->self_);
    bool changed = own->len != n;
    for (int i = 0; i < n && !changed; i++) {
      const HiveEntry *old = find_entry_(*own, entries[i].mac);
      changed = old == nullptr || std::abs(old->rssi - entries[i].rssi) >= ROW_CHANGE_DB;
    }
    own->received_ms = now_ms;
    if (!changed)
      return false;
    own->len = static_cast<uint8_t>(n);
    memcpy(own->entries, entries, sizeof(HiveEntry) * n);
    own->version++;
    this->hash_dirty_ = true;
    return true;
  }

  // A row heard from its origin or relayed by another node. Returns true if it was news.
  bool on_row(const Mac &origin, uint16_t version, const HiveEntry *entries, int n, uint32_t now_ms) {
    if (origin == this->self_ || n > MAX_ROW)
      return false;
    HiveRow *row = this->find_(origin);
    if (row != nullptr && !row_newer(version, row->version)) {
      if (version == row->version)
        row->received_ms = now_ms;
      return false;
    }
    if (row == nullptr && (row = this->find_or_add_(origin)) == nullptr)
      return false;
    row->version = version;
    row->len = static_cast<uint8_t>(n);
    memcpy(row->entries, entries, sizeof(HiveEntry) * n);
    row->received_ms = now_ms;
    this->hash_dirty_ = true;
    return true;
  }

  // Drops rows nobody has refreshed for a day (their origin was forgotten).
  void expire(uint32_t now_ms) {
    for (int i = 0; i < this->count_;) {
      if (this->rows_[i].origin != this->self_ && now_ms - this->rows_[i].received_ms > ROW_EXPIRE_MS) {
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
  HiveRow *find_or_add_(const Mac &m) {
    if (HiveRow *r = this->find_(m))
      return r;
    if (this->count_ >= MAX_ROWS)
      return nullptr;
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
};

// ---- ESP-NOW hive row frame (type 2), sent right after the beacon to relay one row ----------
//
//  0  2  magic "WG"        6  6  origin MAC
//  2  1  protocol version  12 2  row version
//  3  1  type (2)          14 1  entries n, then 7n: neighbour MAC (6), RSSI (int8)
//  4  2  sequence number

constexpr uint8_t ROW_FRAME_TYPE = 2;
constexpr size_t ROW_FRAME_HEADER_BYTES = 15;
constexpr size_t ROW_FRAME_MAX_BYTES = ROW_FRAME_HEADER_BYTES + 7 * MAX_ROW;

inline size_t encode_row_frame(const HiveRow &row, uint16_t seq, uint8_t *out, size_t cap) {
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
  out[14] = row.len;
  for (int i = 0; i < row.len; i++) {
    memcpy(out + ROW_FRAME_HEADER_BYTES + 7 * i, row.entries[i].mac.b, 6);
    out[ROW_FRAME_HEADER_BYTES + 7 * i + 6] = static_cast<uint8_t>(row.entries[i].rssi);
  }
  return len;
}

inline bool decode_row_frame(const uint8_t *p, size_t len, Mac &origin, uint16_t &version, HiveEntry *entries,
                             int &n) {
  if (len < ROW_FRAME_HEADER_BYTES || p[0] != 'W' || p[1] != 'G' || p[2] != GRID_PROTOCOL_VERSION ||
      p[3] != ROW_FRAME_TYPE)
    return false;
  n = p[14];
  if (n > MAX_ROW || len < ROW_FRAME_HEADER_BYTES + 7 * static_cast<size_t>(n))
    return false;
  memcpy(origin.b, p + 6, 6);
  version = static_cast<uint16_t>(p[12] | (p[13] << 8));
  for (int i = 0; i < n; i++) {
    memcpy(entries[i].mac.b, p + ROW_FRAME_HEADER_BYTES + 7 * i, 6);
    entries[i].rssi = static_cast<int8_t>(p[ROW_FRAME_HEADER_BYTES + 7 * i + 6]);
  }
  return true;
}

}  // namespace wisp_core
