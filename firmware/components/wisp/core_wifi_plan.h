#pragma once
// wisp-core: which channel the grid uses and which access point each node joins.
//
// ESP-NOW only reaches nodes on the same channel, but homes with several access points spread
// them over channels 1, 6 and 11, and every node would otherwise join whichever AP is strongest.
// Every node applies the same rule to what it hears, with no voting: the grid channel is the
// channel of the lowest BSSID of the home network heard at a usable level. Nodes that hear the
// same APs agree; a floor that cannot hear that AP forms its own grid, which is fine because
// floors are fused separately anyway. Each node then joins the strongest AP on that channel.

#include <cstdint>

#include "core_grid.h"

namespace wisp_core {

constexpr int MAX_APS_SEEN = 16;
constexpr int8_t DEFAULT_MIN_RSSI = -80;

struct ApSeen {
  Mac bssid;
  uint8_t channel;
  int8_t rssi;
};

// The grid channel, or 0 when no AP is heard well enough. forced (1 to 14) overrides the rule.
inline uint8_t choose_grid_channel(const ApSeen *aps, int n, int8_t min_rssi = DEFAULT_MIN_RSSI,
                                   uint8_t forced = 0) {
  if (forced != 0)
    return forced;
  int best = -1;
  for (int i = 0; i < n; i++) {
    if (aps[i].rssi < min_rssi || aps[i].channel == 0)
      continue;
    if (best < 0 || aps[i].bssid < aps[best].bssid)
      best = i;
  }
  return best < 0 ? 0 : aps[best].channel;
}

// Index of the strongest AP on the channel, or -1.
inline int choose_home_ap(const ApSeen *aps, int n, uint8_t channel) {
  int best = -1;
  for (int i = 0; i < n; i++) {
    if (aps[i].channel == channel && (best < 0 || aps[i].rssi > aps[best].rssi))
      best = i;
  }
  return best;
}

// The latest reading per BSSID, up to MAX_APS_SEEN entries. When full, it keeps the lowest
// BSSIDs, so the grid channel never depends on the order a scan lists them in.
class ApList {
 public:
  void clear() { this->count_ = 0; }
  void add(const Mac &bssid, uint8_t channel, int8_t rssi) {
    for (int i = 0; i < this->count_; i++) {
      if (this->aps_[i].bssid == bssid) {
        this->aps_[i].channel = channel;
        this->aps_[i].rssi = rssi;
        return;
      }
    }
    if (this->count_ < MAX_APS_SEEN) {
      this->aps_[this->count_++] = ApSeen{bssid, channel, rssi};
      return;
    }
    int highest = 0;
    for (int i = 1; i < this->count_; i++) {
      if (this->aps_[highest].bssid < this->aps_[i].bssid)
        highest = i;
    }
    if (bssid < this->aps_[highest].bssid)
      this->aps_[highest] = ApSeen{bssid, channel, rssi};
  }
  int count() const { return this->count_; }
  const ApSeen *data() const { return this->aps_; }

 protected:
  ApSeen aps_[MAX_APS_SEEN]{};
  int count_{0};
};

}  // namespace wisp_core
