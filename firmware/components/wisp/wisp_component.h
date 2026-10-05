#pragma once
// ESPHome wrapper: settings and entities around the core, which runs in its own task.

#include <atomic>

#include "esphome/components/binary_sensor/binary_sensor.h"
#include "esphome/components/sensor/sensor.h"
#include "esphome/core/component.h"

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "core_grid.h"
#include "core_hive.h"
#include "core_layout.h"
#include "core_links.h"
#include "core_wifi_plan.h"
#include "esp_ap_csi.h"
#include "esp_espnow.h"
#include "esp_udp_stream.h"

namespace esphome::wisp {

class WispComponent : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

  void set_ap_ping_interval(uint32_t ms) { this->ap_ping_interval_ms_ = ms; }
  void set_raw_stream_port(uint16_t port) { this->raw_stream_port_ = port; }
  void set_report_interval(uint32_t ms) { this->report_interval_ms_ = ms; }
  void set_motion_threshold(float t) { this->motion_threshold_ = t; }
  void set_ap_csi_rate_sensor(sensor::Sensor *s) { this->ap_csi_rate_sensor_ = s; }
  void set_csi_dropped_sensor(sensor::Sensor *s) { this->csi_dropped_sensor_ = s; }
  void set_ap_motion_score_sensor(sensor::Sensor *s) { this->ap_motion_score_sensor_ = s; }
  void set_ap_motion_binary_sensor(binary_sensor::BinarySensor *s) { this->ap_motion_binary_sensor_ = s; }
  void set_grid_nodes_sensor(sensor::Sensor *s) { this->grid_nodes_sensor_ = s; }
  void set_hive_sync_binary_sensor(binary_sensor::BinarySensor *s) { this->hive_sync_binary_sensor_ = s; }
  void set_raw_stream_enabled(bool enabled) { this->raw_stream_enabled_.store(enabled); }
  void set_grid_channel(uint8_t channel) { this->grid_channel_cfg_ = channel; }
  void set_ap_min_rssi(int8_t rssi) { this->ap_min_rssi_ = rssi; }

 protected:
  // Core task (owns grid_ and links_).
  static void core_task_(void *arg);
  void handle_csi_(const wisp_core::CsiRecord &rec, uint32_t now, uint8_t *packet);
  void handle_espnow_(const wisp_platform::EspNowFrame &f, uint32_t now);
  void update_hive_(uint32_t now);
  void core_round_(uint32_t now);
  void core_second_(uint32_t now);
  void send_report_(uint32_t now);
  void update_sources_();
  bool home_bssid_(wisp_core::Mac &out);

  // Main loop.
  void watch_wifi_(uint32_t now);
  void steer_wifi_();
  void update_ap_();
  void publish_stats_(uint32_t now);

  uint32_t ap_ping_interval_ms_{50};
  uint16_t raw_stream_port_{47010};
  uint32_t report_interval_ms_{200};
  float motion_threshold_{2.0f};
  sensor::Sensor *ap_csi_rate_sensor_{nullptr};
  sensor::Sensor *csi_dropped_sensor_{nullptr};
  sensor::Sensor *ap_motion_score_sensor_{nullptr};
  binary_sensor::BinarySensor *ap_motion_binary_sensor_{nullptr};
  sensor::Sensor *grid_nodes_sensor_{nullptr};
  binary_sensor::BinarySensor *hive_sync_binary_sensor_{nullptr};

  wisp_platform::CsiCapture capture_;
  wisp_platform::GatewayPinger pinger_;
  wisp_platform::UdpStream stream_;
  wisp_platform::EspNowRadio radio_;
  wisp_platform::SlotScheduler scheduler_;
  QueueHandle_t csi_queue_{nullptr};
  QueueHandle_t espnow_queue_{nullptr};
  TaskHandle_t task_{nullptr};
  wisp_core::Mac self_{};
  bool stream_open_{false};

  // Owned by the core task.
  wisp_core::Grid grid_{wisp_core::Mac{}};
  wisp_core::LinkTable links_;
  wisp_core::Hive hive_{wisp_core::Mac{}};
  wisp_core::LayoutWorkspace layout_ws_;
  wisp_core::LayoutPoint layout_[wisp_core::MAX_POINTS]{};
  int layout_count_{0};
  uint32_t layout_hash_{0};
  uint32_t hive_seq_{0};
  uint16_t relay_seq_{0};
  uint32_t last_hive_ms_{0};
  uint32_t sources_version_{0xFFFFFFFF};
  wisp_core::Mac sources_bssid_{};
  uint16_t beacon_seq_{0};
  uint32_t report_seq_{0};
  uint32_t raw_seq_{0};
  uint8_t live_streams_{0};

  // Shared between the main loop and the core task.
  portMUX_TYPE ap_lock_ = portMUX_INITIALIZER_UNLOCKED;
  wisp_core::Mac bssid_{};
  bool has_bssid_{false};
  std::atomic<bool> csi_started_{false};
  std::atomic<bool> espnow_started_{false};
  std::atomic<bool> raw_stream_enabled_{false};
  std::atomic<float> ap_score_{NAN};
  std::atomic<bool> ap_active_{false};
  std::atomic<int> grid_nodes_{1};
  std::atomic<bool> hive_in_sync_{false};
  std::atomic<uint32_t> ap_frames_{0};

  // Grid channel steering (main loop).
  uint8_t grid_channel_cfg_{0};  // 0 = automatic
  int8_t ap_min_rssi_{wisp_core::DEFAULT_MIN_RSSI};
  wisp_core::ApList aps_seen_;
  bool steered_{false};
  bool pinned_{false};
  bool was_connected_{false};
  uint32_t disconnected_since_{0};
  std::atomic<uint8_t> grid_channel_{0};

  uint32_t dropped_total_{0};
  uint32_t last_check_ms_{0};
  uint32_t last_stats_ms_{0};
};

}  // namespace esphome::wisp
