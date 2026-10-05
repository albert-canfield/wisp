#pragma once
// ESPHome wrapper: settings and entities around the core, which runs in its own task.

#include <atomic>

#include "esphome/components/binary_sensor/binary_sensor.h"
#include "esphome/components/sensor/sensor.h"
#include "esphome/core/component.h"
#include "esphome/core/preferences.h"

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "core_confirm.h"
#include "core_grid.h"
#include "core_hive.h"
#include "core_layout.h"
#include "core_links.h"
#include "core_wifi_plan.h"
#include "esp_ap_csi.h"
#include "esp_espnow.h"
#include "esp_ftm.h"
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
  void set_motion_threshold(float t) { this->motion_threshold_.store(t); }  // any task, applied within a second
  void set_ap_csi_rate_sensor(sensor::Sensor *s) { this->ap_csi_rate_sensor_ = s; }
  void set_csi_dropped_sensor(sensor::Sensor *s) { this->csi_dropped_sensor_ = s; }
  void set_ap_motion_score_sensor(sensor::Sensor *s) { this->ap_motion_score_sensor_ = s; }
  void set_ap_motion_binary_sensor(binary_sensor::BinarySensor *s) { this->ap_motion_binary_sensor_ = s; }
  void set_motion_binary_sensor(binary_sensor::BinarySensor *s) { this->motion_binary_sensor_ = s; }
  void set_grid_nodes_sensor(sensor::Sensor *s) { this->grid_nodes_sensor_ = s; }
  void set_channel_sensor(sensor::Sensor *s) { this->channel_sensor_ = s; }
  void set_ap_distance_sensor(sensor::Sensor *s) { this->ap_distance_sensor_ = s; }
  void set_hive_sync_binary_sensor(binary_sensor::BinarySensor *s) { this->hive_sync_binary_sensor_ = s; }
  void set_core_stack_sensor(sensor::Sensor *s) { this->core_stack_sensor_ = s; }
  void set_breathing_binary_sensor(binary_sensor::BinarySensor *s) { this->breathing_binary_sensor_ = s; }
  void set_breathing_rate_sensor(sensor::Sensor *s) { this->breathing_rate_sensor_ = s; }
  void set_max_link_threshold_sensor(sensor::Sensor *s) { this->max_link_threshold_sensor_ = s; }
  void set_raw_stream_enabled(bool enabled) { this->raw_stream_enabled_.store(enabled); }
  // Breathing detection (core_breathing.h), any task: about 7 KB of RAM while on.
  void set_breathing_enabled(bool enabled) { this->breathing_enabled_.store(enabled); }
  // 0: automatic (see core_wifi_plan.h), else a fixed channel. Changed while running (from Home
  // Assistant), the node checks its channel again at once and moves if it has to.
  void set_grid_channel(uint8_t channel) {
    if (channel == this->grid_channel_cfg_)
      return;
    this->grid_channel_cfg_ = channel;
    this->steered_ = false;
    this->steer_attempts_ = 0;
  }
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
  void update_breathing_();
  bool home_bssid_(wisp_core::Mac &out);

  // Main loop.
  void watch_wifi_();
  void steer_wifi_(uint32_t now);
  void watch_csi_(bool connected);
  void watch_alone_(uint32_t now, bool connected);
  void boost_grid_ap_();
  void remember_grid_ap_(const wisp_core::Mac &bssid, uint8_t channel);
  void update_ap_();
  void publish_stats_(uint32_t now);

  uint32_t ap_ping_interval_ms_{50};
  uint16_t raw_stream_port_{47010};
  uint32_t report_interval_ms_{200};
  sensor::Sensor *ap_csi_rate_sensor_{nullptr};
  sensor::Sensor *csi_dropped_sensor_{nullptr};
  sensor::Sensor *ap_motion_score_sensor_{nullptr};
  binary_sensor::BinarySensor *ap_motion_binary_sensor_{nullptr};
  binary_sensor::BinarySensor *motion_binary_sensor_{nullptr};
  sensor::Sensor *grid_nodes_sensor_{nullptr};
  sensor::Sensor *channel_sensor_{nullptr};
  sensor::Sensor *ap_distance_sensor_{nullptr};
  binary_sensor::BinarySensor *hive_sync_binary_sensor_{nullptr};
  sensor::Sensor *core_stack_sensor_{nullptr};
  binary_sensor::BinarySensor *breathing_binary_sensor_{nullptr};
  sensor::Sensor *breathing_rate_sensor_{nullptr};
  sensor::Sensor *max_link_threshold_sensor_{nullptr};

  wisp_platform::CsiCapture capture_;
  wisp_platform::GatewayPinger pinger_;
  wisp_platform::UdpStream stream_;
  wisp_platform::EspNowRadio radio_;
  wisp_platform::SlotScheduler scheduler_;
  wisp_platform::FtmProbe ftm_;
  bool ftm_started_{false};
  uint32_t last_ftm_ms_{0};
  uint32_t ftm_results_seen_{0};
  uint8_t ftm_failures_{0};
  QueueHandle_t csi_queue_{nullptr};
  QueueHandle_t espnow_queue_{nullptr};
  TaskHandle_t task_{nullptr};
  wisp_core::Mac self_{};
  std::atomic<bool> stream_open_{false};

  // Owned by the core task.
  wisp_core::Grid grid_{wisp_core::Mac{}};
  wisp_core::LinkTable links_;
  wisp_core::Hive hive_{wisp_core::Mac{}};
  wisp_core::MotionConfirm confirm_{wisp_core::Mac{}};
  wisp_core::PairList pairs_{};  // confirmed pairs, for the link reports
  wisp_core::BreathingBank *breathing_bank_{nullptr};  // while breathing detection is on
  bool breathing_no_memory_{false};
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
  int report_first_{0};  // hive report rows start here, moving on by the rows each report carried
  float threshold_applied_{0.0f};

  // Shared between the main loop and the core task.
  portMUX_TYPE ap_lock_ = portMUX_INITIALIZER_UNLOCKED;
  wisp_core::Mac bssid_{};
  bool has_bssid_{false};
  std::atomic<bool> csi_started_{false};
  std::atomic<bool> espnow_started_{false};
  std::atomic<bool> raw_stream_enabled_{false};
  std::atomic<bool> breathing_enabled_{false};
  std::atomic<float> motion_threshold_{2.0f};
  std::atomic<float> ap_score_{NAN};
  std::atomic<bool> ap_active_{false};
  std::atomic<bool> motion_{false};         // one of this node's links is confirmed
  std::atomic<bool> motion_latched_{false};  // ... at some second since the main loop last looked
  std::atomic<int> grid_nodes_{1};
  std::atomic<bool> breathing_{false};  // a link shows someone breathing
  std::atomic<float> breath_rate_{NAN};  // a minute, on the clearest such link
  std::atomic<float> max_link_threshold_{2.0f};
  std::atomic<bool> hive_in_sync_{false};
  std::atomic<uint32_t> ap_frames_{0};
  std::atomic<uint32_t> ap_frames_total_{0};
  std::atomic<uint32_t> self_jumps_{0};

  // Grid channel steering (main loop).
  uint8_t grid_channel_cfg_{0};  // 0 = automatic
  int8_t ap_min_rssi_{wisp_core::DEFAULT_MIN_RSSI};
  wisp_core::ApList aps_seen_;
  bool steered_{false};
  uint8_t steer_attempts_{0};
  uint32_t last_steer_ms_{0};
  uint8_t grid_ap_missing_{0};  // scans in a row without the saved grid AP
  uint32_t scan_fingerprint_{0};
  bool alone_{false};
  uint32_t alone_since_ms_{0};
  uint32_t alone_wait_ms_{0};
  ESPPreferenceObject grid_ap_pref_;
  wisp_core::Mac grid_ap_{};
  uint8_t grid_ap_channel_{0};
  bool has_grid_ap_{false};
  bool was_connected_{false};
  bool wifi_up_{false};
  uint32_t beacons_seen_{0};
  uint8_t beacons_stalled_s_{0};
  std::atomic<uint8_t> grid_channel_{0};

  uint32_t dropped_total_{0};
  uint32_t espnow_dropped_total_{0};
  uint32_t self_jumps_seen_{0};
  // CSI watchdog (main loop): seconds without a frame from the AP, and the wait before acting.
  uint32_t csi_seen_{0};
  uint32_t csi_quiet_s_{0};
  uint32_t csi_wait_s_{0};
  uint32_t last_health_ms_{0};
  uint32_t last_check_ms_{0};
  uint32_t last_stats_ms_{0};
};

}  // namespace esphome::wisp
