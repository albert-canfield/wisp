#pragma once
// wisp-core: motion the hive confirms. A body changes a link both ways and the links around it;
// one node's own noise shows only on what it sends or receives. A pair of nodes (A, B) is
// confirmed while
//   1. both ways agree: one direction reports motion now, the other did within CONFIRM_WINDOW_MS;
//   2. other nodes near the pair agree: of the K = min(CONFIRM_SUPPORT_NEAREST, live nodes - 2)
//      nodes nearest the pair, at least confirm_need(K) saw motion on a link to A or to B (either
//      direction) within the same window.
// Every beacon carries its sender's live score for each neighbour (core_grid.h), so a node knows
// every direction it hears a beacon about: every node that hears the same beacons holds the same
// pairs, with no extra traffic. Only beacons heard directly and no older than CONFIRM_FRESH_MS
// count (relayed rows carry no scores). A direction moves as its receiver's own flag would at
// this node's threshold (on at it, off halfway back to 1, as MotionDetector), in the beacon's
// tenths; this node's own directions are its detectors' flags. Nearest goes by the hive's layout,
// the same on every node in sync: distance from the node to the segment A-B, or, for a node
// without a layout position, the nearer of A and B by RSSI. Ties go by MAC.
//
// This node's links: one from a node is confirmed while it reports motion and its pair is
// confirmed; one from an access point (no reverse) while it reports motion and a pair with this
// node was confirmed within the window.

#include <cmath>
#include <cstdint>

#include "core_grid.h"
#include "core_hive.h"
#include "core_layout.h"
#include "core_links.h"

namespace wisp_core {

constexpr uint32_t CONFIRM_WINDOW_MS = 2000;  // both ways, and the supporters, within this
constexpr uint32_t CONFIRM_FRESH_MS = 1500;   // a beacon older than this says nothing
constexpr int CONFIRM_NODES = MAX_ROWS;       // this node and every member
constexpr int CONFIRM_SUPPORT_NEAREST = 6;    // supporters count among this many nodes nearest the pair
constexpr int CONFIRM_SUPPORT_FEW = 4;        // up to this many candidates, one supporter is enough
static_assert(CONFIRM_NODES <= 32, "pairs are kept as 32-bit masks");

// Supporters a pair needs among its k nearest nodes.
inline int confirm_need(int k) { return k <= CONFIRM_SUPPORT_FEW ? 1 : 2; }

class MotionConfirm {
 public:
  explicit MotionConfirm(const Mac &self) { this->reset(self); }

  void reset(const Mac &self) {
    this->nodes_[0] = self;
    this->count_ = 1;
    this->node_[0] = this->pairs_[0] = 0;
    for (int j = 0; j < CONFIRM_NODES; j++)
      this->dir_[0][j] = 0;
    this->own_set_ = 0;
    this->self_seen_ = false;
  }

  // This node's motion threshold: the directions other nodes report are judged with it.
  void set_threshold(float t) {
    const int t10 = static_cast<int>(t * 10.0f + 0.5f);
    this->on10_ = t10 < 11 ? 11 : (t10 > 254 ? 254 : t10);
  }

  // A beacon heard directly: the sender's live scores for the nodes it hears (access points left
  // out by the caller). Neighbours it no longer lists are no longer heard by it.
  void on_beacon(const Mac &from, const RowEntry *row, int n, uint32_t now_ms) {
    if (from == this->self())
      return;
    const int i = this->index_(from, now_ms);
    if (i < 0)
      return;
    this->heard_ms_[i] = this->seen_ms_[i] = now_ms;
    this->node_[i] |= NODE_HEARD | NODE_LIVE;
    bool listed[CONFIRM_NODES] = {};
    for (int k = 0; k < n; k++) {
      const int s = row[k].score10;
      const bool known = s != SCORE_UNKNOWN;
      const int j = known ? this->index_(row[k].mac, now_ms) : this->find(row[k].mac);
      if (j < 0 || j == i)
        continue;
      listed[j] = true;
      const bool on = (this->dir_[i][j] & DIR_ON) ? 2 * s >= 10 + this->on10_ : s >= this->on10_;
      this->set_(i, j, known && on, known, now_ms);
    }
    for (int j = 0; j < this->count_; j++) {
      if (!listed[j])
        this->set_(i, j, false, false, now_ms);
    }
  }

  // This node's link from another node, once a second before tick(): its motion flag, and
  // whether it has a score at all. Links not given before a tick are not heard.
  void own(const Mac &from, bool moving, bool known, uint32_t now_ms) {
    const int j = known ? this->index_(from, now_ms) : this->find(from);
    if (j <= 0)
      return;
    this->own_set_ |= 1u << j;
    this->set_(0, j, moving && known, known, now_ms);
  }

  // Once a second: the confirmed pairs. points: the hive's layout; hive: its rows, for the RSSI
  // of nodes without a layout position.
  void tick(uint32_t now_ms, const LayoutPoint *points = nullptr, int n_points = 0, const Hive *hive = nullptr) {
    for (int j = 1; j < this->count_; j++) {
      if (!(this->own_set_ & (1u << j)))
        this->set_(0, j, false, false, now_ms);
    }
    this->own_set_ = 0;
    this->heard_ms_[0] = this->seen_ms_[0] = now_ms;
    this->node_[0] |= NODE_HEARD | NODE_LIVE;
    // Forget what is too old, so a time read again after the clock wraps means nothing.
    int live = 0;
    for (int i = 0; i < this->count_; i++) {
      if ((this->node_[i] & NODE_HEARD) && now_ms - this->heard_ms_[i] > CONFIRM_FRESH_MS) {
        this->node_[i] &= static_cast<uint8_t>(~NODE_HEARD);
        for (int j = 0; j < this->count_; j++)
          this->dir_[i][j] &= static_cast<uint8_t>(~DIR_ON);  // its readings are unknown now
      }
      if ((this->node_[i] & NODE_LIVE) && now_ms - this->seen_ms_[i] > CONFIRM_FRESH_MS)
        this->node_[i] &= static_cast<uint8_t>(~NODE_LIVE);
      live += (this->node_[i] & NODE_LIVE) != 0;
      for (int j = 0; j < this->count_; j++) {
        if ((this->dir_[i][j] & DIR_RECENT) && now_ms - this->moving_ms_[i][j] > CONFIRM_WINDOW_MS)
          this->dir_[i][j] &= static_cast<uint8_t>(~DIR_RECENT);
      }
      this->pairs_[i] = 0;
    }
    for (int i = 0; i < this->count_; i++) {
      this->placed_[i] = false;
      for (int p = 0; p < n_points && !this->placed_[i]; p++) {
        if (points[p].mac == this->nodes_[i]) {
          this->x_[i] = points[p].x;
          this->y_[i] = points[p].y;
          this->placed_[i] = true;
        }
      }
    }
    const int k = live - 2 < CONFIRM_SUPPORT_NEAREST ? live - 2 : CONFIRM_SUPPORT_NEAREST;
    const int need = confirm_need(k);
    bool self_pair = false;
    for (int a = 0; a < this->count_; a++) {
      for (int b = a + 1; b < this->count_; b++) {
        if (k < need || !this->both_ways_(a, b) || this->supporters_(a, b, k, need, live, hive) < need)
          continue;
        this->pairs_[a] |= 1u << b;
        this->pairs_[b] |= 1u << a;
        self_pair = self_pair || a == 0;
      }
    }
    if (self_pair) {
      this->self_ms_ = now_ms;
      this->self_seen_ = true;
    } else if (this->self_seen_ && now_ms - this->self_ms_ > CONFIRM_WINDOW_MS) {
      this->self_seen_ = false;
    }
  }

  // Once a second, right after links.tick_second(): this node's flags in, the pairs, then each
  // link's confirmed bit. Returns whether any of this node's links is confirmed.
  bool update(LinkTable &links, uint32_t now_ms, const LayoutPoint *points = nullptr, int n_points = 0,
              const Hive *hive = nullptr) {
    for (int i = 0; i < links.count(); i++) {
      const Link &l = links.link(i);
      if (l.kind == LinkKind::NODE)
        this->own(l.source, l.active, !std::isnan(l.score), now_ms);
    }
    this->tick(now_ms, points, n_points, hive);
    bool any = false;
    for (int i = 0; i < links.count(); i++) {
      const Link &l = links.link(i);
      links.set_confirmed(i, l.kind == LinkKind::NODE ? this->confirmed(this->self(), l.source)
                                                      : this->self_confirmed(now_ms));
      any = any || links.link(i).confirmed;
    }
    return any;
  }

  bool confirmed(const Mac &a, const Mac &b) const {
    const int i = this->find(a), j = this->find(b);
    return i >= 0 && j >= 0 && (this->pairs_[i] & (1u << j)) != 0;
  }
  // A pair with this node was confirmed within the window.
  bool self_confirmed(uint32_t now_ms) const {
    return this->self_seen_ && now_ms - this->self_ms_ <= CONFIRM_WINDOW_MS;
  }
  // The confirmed pairs, in MAC order; returns how many.
  int pairs(PairList &out) const {
    int order[CONFIRM_NODES] = {};
    for (int i = 0; i < this->count_; i++) {
      int at = i;
      for (; at > 0 && this->nodes_[i] < this->nodes_[order[at - 1]]; at--)
        order[at] = order[at - 1];
      order[at] = i;
    }
    out.n = 0;
    out.truncated = false;
    for (int x = 0; x < this->count_; x++) {
      for (int y = x + 1; y < this->count_; y++) {
        if (!(this->pairs_[order[x]] & (1u << order[y])))
          continue;
        if (out.n >= MAX_REPORT_PAIRS) {
          out.truncated = true;
          continue;
        }
        out.a[out.n] = this->nodes_[order[x]];
        out.b[out.n] = this->nodes_[order[y]];
        out.n++;
      }
    }
    return out.n;
  }
  const Mac &self() const { return this->nodes_[0]; }
  int count() const { return this->count_; }
  int find(const Mac &m) const {
    for (int i = 0; i < this->count_; i++) {
      if (this->nodes_[i] == m)
        return i;
    }
    return -1;
  }

 protected:
  static constexpr uint8_t DIR_ON = 0x01;      // its latest reading shows motion
  static constexpr uint8_t DIR_RECENT = 0x02;  // moved within the window, until moving_ms_
  static constexpr uint8_t NODE_HEARD = 0x01;  // its beacon is fresh, at heard_ms_
  static constexpr uint8_t NODE_LIVE = 0x02;   // heard, or listed with a score in a fresh beacon, at seen_ms_

  // A reading of direction i <- j. It moved until now if it moves or just stopped: so a node's own
  // flag (read once a second) and the beacons that carry it (ten a second) stop at the same time.
  // A score at all says j is alive.
  void set_(int i, int j, bool on, bool known, uint32_t now_ms) {
    uint8_t &d = this->dir_[i][j];
    if (on || (d & DIR_ON)) {
      this->moving_ms_[i][j] = now_ms;
      d |= DIR_RECENT;
    }
    d = static_cast<uint8_t>(on ? d | DIR_ON : d & ~DIR_ON);
    if (known) {
      this->seen_ms_[j] = now_ms;
      this->node_[j] |= NODE_LIVE;
    }
  }

  // Only a fresh beacon tells what its sender hears; this node's own row is always fresh.
  bool moving_(int i, int j) const { return (this->node_[i] & NODE_HEARD) && (this->dir_[i][j] & DIR_ON); }
  bool recent_(int i, int j) const { return (this->node_[i] & NODE_HEARD) && (this->dir_[i][j] & DIR_RECENT); }
  bool both_ways_(int a, int b) const {
    return (this->moving_(a, b) && this->recent_(b, a)) || (this->moving_(b, a) && this->recent_(a, b));
  }
  bool supports_(int c, int a, int b) const {
    return this->recent_(c, a) || this->recent_(a, c) || this->recent_(c, b) || this->recent_(b, c);
  }

  // Supporters of pair (a, b) among its k nearest live nodes (ranked only when there are more).
  int supporters_(int a, int b, int k, int need, int live, const Hive *hive) const {
    int cand[CONFIRM_NODES];
    float key[CONFIRM_NODES];
    int n = 0, support = 0;
    for (int c = 0; c < this->count_; c++) {
      if (c == a || c == b || !(this->node_[c] & NODE_LIVE))
        continue;
      cand[n++] = c;
      support += this->supports_(c, a, b);
    }
    if (support < need || live - 2 <= k)
      return support;
    for (int x = 0; x < n; x++) {
      const int c = cand[x];
      const float d = this->distance2_(c, a, b, hive);
      int at = x;
      for (; at > 0 && (d < key[at - 1] || (d == key[at - 1] && this->nodes_[c] < this->nodes_[cand[at - 1]]));
           at--) {
        key[at] = key[at - 1];
        cand[at] = cand[at - 1];
      }
      key[at] = d;
      cand[at] = c;
    }
    support = 0;
    for (int x = 0; x < k && x < n; x++)
      support += this->supports_(cand[x], a, b);
    return support;
  }

  // Squared distance from node c to the pair: to the segment a-b on the layout, else the nearer
  // of a and b by RSSI.
  float distance2_(int c, int a, int b, const Hive *hive) const {
    if (this->placed_[a] && this->placed_[b] && this->placed_[c]) {
      const float dx = this->x_[b] - this->x_[a], dy = this->y_[b] - this->y_[a];
      const float cx = this->x_[c] - this->x_[a], cy = this->y_[c] - this->y_[a];
      const float len2 = dx * dx + dy * dy;
      float t = len2 > 0.0f ? (cx * dx + cy * dy) / len2 : 0.0f;
      t = t < 0.0f ? 0.0f : (t > 1.0f ? 1.0f : t);
      const float px = t * dx - cx, py = t * dy - cy;
      return px * px + py * py;
    }
    const float da = this->rssi_metres_(c, a, hive), db = this->rssi_metres_(c, b, hive);
    const float d = da < db ? da : db;
    return d * d;
  }

  // Distance between two nodes from the hive's rows, both directions averaged; far when unknown.
  float rssi_metres_(int i, int j, const Hive *hive) const {
    if (hive == nullptr)
      return UNKNOWN_METRES;
    float sum = 0.0f;
    int n = 0;
    for (int pass = 0; pass < 2; pass++) {
      const HiveRow *row = hive->find(this->nodes_[pass == 0 ? i : j]);
      const Mac &other = this->nodes_[pass == 0 ? j : i];
      for (int e = 0; row != nullptr && e < row->len; e++) {
        if (row->entries[e].mac == other) {
          sum += row->entries[e].rssi;
          n++;
        }
      }
    }
    return n > 0 ? rssi_to_metres(sum / static_cast<float>(n)) : UNKNOWN_METRES;
  }
  static constexpr float UNKNOWN_METRES = 1000.0f;

  // Index of a node, added if new. A full table makes room by dropping the node silent longest,
  // if it is no longer live; -1 if every node is.
  int index_(const Mac &m, uint32_t now_ms) {
    const int found = this->find(m);
    if (found >= 0)
      return found;
    int v = this->count_;
    if (v >= CONFIRM_NODES) {
      v = -1;
      for (int i = 1; i < this->count_; i++) {
        if (!(this->node_[i] & NODE_LIVE) &&
            (v < 0 || now_ms - this->seen_ms_[i] > now_ms - this->seen_ms_[v]))
          v = i;
      }
      if (v < 0)
        return -1;
    } else {
      this->count_++;
    }
    this->nodes_[v] = m;
    this->node_[v] = 0;
    this->seen_ms_[v] = this->heard_ms_[v] = now_ms;
    this->pairs_[v] = 0;
    for (int i = 0; i < CONFIRM_NODES; i++) {
      this->dir_[v][i] = this->dir_[i][v] = 0;
      this->pairs_[i] &= ~(1u << v);
    }
    this->own_set_ &= ~(1u << v);
    return v;
  }

  Mac nodes_[CONFIRM_NODES]{};  // 0: this node
  int count_{1};
  uint8_t node_[CONFIRM_NODES]{};
  uint32_t heard_ms_[CONFIRM_NODES]{};
  uint32_t seen_ms_[CONFIRM_NODES]{};
  uint8_t dir_[CONFIRM_NODES][CONFIRM_NODES]{};        // [receiver][transmitter]
  uint32_t moving_ms_[CONFIRM_NODES][CONFIRM_NODES]{};  // when that direction last moved
  uint32_t pairs_[CONFIRM_NODES]{};                     // bit j of pairs_[i]: pair (i, j) confirmed
  uint32_t own_set_{0};
  int on10_{20};
  bool self_seen_{false};
  uint32_t self_ms_{0};
  // Layout positions, per tick
  bool placed_[CONFIRM_NODES]{};
  float x_[CONFIRM_NODES]{};
  float y_[CONFIRM_NODES]{};
};

}  // namespace wisp_core
