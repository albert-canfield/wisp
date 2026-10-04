#pragma once
// Platform adapter (ESP-IDF): CSI capture and the gateway pinger.

#include <atomic>
#include <cstdint>

#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

#include "core_csi_record.h"

namespace wisp_platform {

// Captures CSI from frames sent by one transmitter (the home AP for now) into a queue.
class CsiCapture {
 public:
  // Enables CSI. Returns false while WiFi is not started yet; call again later.
  bool start(QueueHandle_t queue);
  void set_source(const uint8_t mac[6]);
  uint32_t take_dropped() { return this->dropped_.exchange(0); }

 protected:
  static void on_csi_(void *ctx, wifi_csi_info_t *info);

  QueueHandle_t queue_{nullptr};
  portMUX_TYPE lock_ = portMUX_INITIALIZER_UNLOCKED;
  uint8_t source_[6]{};
  bool has_source_{false};
  std::atomic<uint32_t> dropped_{0};
  wisp_core::CsiRecord scratch_{};  // the WiFi task calls back one frame at a time
};

// Pings the gateway at a fixed interval, so the access point answers with frames that carry CSI.
class GatewayPinger {
 public:
  // gateway: IPv4 address in network byte order, as in esp_ip4_addr_t.
  bool start(uint32_t gateway, uint32_t interval_ms);
  void stop();
  bool running() const { return this->handle_ != nullptr; }
  uint32_t target() const { return this->target_; }

 protected:
  void *handle_{nullptr};  // esp_ping_handle_t
  uint32_t target_{0};
};

}  // namespace wisp_platform
