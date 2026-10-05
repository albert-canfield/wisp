#pragma once
// Platform adapter (ESP-IDF): ESP-NOW broadcast beacons, sent in this node's time slot.

#include <atomic>
#include <cstddef>
#include <cstdint>

#include "esp_now.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

#include "core_grid.h"
#include "core_hive.h"

namespace wisp_platform {

// One received ESP-NOW frame, as queued for the core task.
struct EspNowFrame {
  uint8_t src[6];
  int8_t rssi;
  uint8_t len;
  uint8_t data[wisp_core::BEACON_MAX_BYTES];  // also fits a hive row frame
};
static_assert(wisp_core::ROW_FRAME_MAX_BYTES <= wisp_core::BEACON_MAX_BYTES, "row frames must fit");

class EspNowRadio {
 public:
  // Starts ESP-NOW with a broadcast peer at a fixed 802.11n rate. False while WiFi is not up.
  bool start(QueueHandle_t rx_queue);
  // Tears ESP-NOW down and starts it again, for example after a Wi-Fi restart.
  bool restart();
  bool send_broadcast(const uint8_t *data, size_t len);

 protected:
  static void on_recv_(const esp_now_recv_info_t *info, const uint8_t *data, int len);
  static QueueHandle_t rx_queue_;
  static std::atomic<uint32_t> dropped_;
};

// Fires once per round at this node's slot, aligned to the access point's clock (TSF) when
// connected, and sends the latest beacon the core task prepared, then one relayed hive row.
class SlotScheduler {
 public:
  bool start(EspNowRadio *radio);
  void set_slot(int slot) { this->slot_.store(slot); }
  int slot() const { return this->slot_.load(); }
  void set_beacon(const uint8_t *data, size_t len);
  void set_relay(const uint8_t *data, size_t len);
  bool synced() const { return this->synced_.load(); }
  uint32_t sent() const { return this->sent_.load(); }

 protected:
  static void on_timer_(void *arg);
  void arm_();

  EspNowRadio *radio_{nullptr};
  esp_timer_handle_t timer_{nullptr};
  portMUX_TYPE lock_ = portMUX_INITIALIZER_UNLOCKED;
  uint8_t buf_[wisp_core::BEACON_MAX_BYTES]{};
  size_t len_{0};
  uint8_t relay_[wisp_core::ROW_FRAME_MAX_BYTES]{};
  size_t relay_len_{0};
  std::atomic<int> slot_{-1};
  std::atomic<bool> synced_{false};
  std::atomic<uint32_t> sent_{0};
};

}  // namespace wisp_platform
