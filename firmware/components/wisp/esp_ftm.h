#pragma once
// Platform adapter (ESP-IDF): Wi-Fi FTM (802.11mc round trip time) towards an access point,
// for chips that can initiate it (S2, S3, C2, C3, C6). Experimental: only built when the YAML
// asks for it, since many home access points do not answer FTM.

#include <atomic>
#include <cmath>
#include <cstdint>

namespace wisp_platform {

class FtmProbe {
 public:
  bool start();
  // Starts one measurement session; the result arrives later through the Wi-Fi event.
  bool measure(const uint8_t bssid[6], uint8_t channel);
  float distance_m() const { return this->distance_m_.load(); }
  int status() const { return this->status_.load(); }  // wifi_ftm_status_t, or -1 before any result
  uint32_t results() const { return this->results_.load(); }

 protected:
  static void on_event_(void *arg, const char *base, int32_t id, void *data);
  std::atomic<float> distance_m_{NAN};
  std::atomic<int> status_{-1};
  std::atomic<uint32_t> results_{0};
};

}  // namespace wisp_platform
