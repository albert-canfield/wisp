#pragma once
// ESPHome wrapper: settings and entities around the core, which runs in its own task.

#include <atomic>

#include "esphome/components/binary_sensor/binary_sensor.h"
#include "esphome/components/sensor/sensor.h"
#include "esphome/core/component.h"

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "core_link_motion.h"
#include "esp_ap_csi.h"
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
  void set_ap_csi_rate_sensor(sensor::Sensor *s) { this->ap_csi_rate_sensor_ = s; }
  void set_csi_dropped_sensor(sensor::Sensor *s) { this->csi_dropped_sensor_ = s; }
  void set_ap_motion_score_sensor(sensor::Sensor *s) { this->ap_motion_score_sensor_ = s; }
  void set_ap_motion_binary_sensor(binary_sensor::BinarySensor *s) { this->ap_motion_binary_sensor_ = s; }
  void set_motion_threshold(float t) { this->detector_.set_threshold(t); this->motion_threshold_ = t; }
  void set_raw_stream_enabled(bool enabled) { this->raw_stream_enabled_.store(enabled); }

 protected:
  static void core_task_(void *arg);
  void update_ap_();
  void publish_stats_(uint32_t now);

  uint32_t ap_ping_interval_ms_{50};
  uint16_t raw_stream_port_{47010};
  sensor::Sensor *ap_csi_rate_sensor_{nullptr};
  sensor::Sensor *csi_dropped_sensor_{nullptr};
  sensor::Sensor *ap_motion_score_sensor_{nullptr};
  binary_sensor::BinarySensor *ap_motion_binary_sensor_{nullptr};
  float motion_threshold_{2.0f};

  wisp_platform::CsiCapture capture_;
  wisp_platform::GatewayPinger pinger_;
  wisp_platform::UdpStream raw_stream_;
  QueueHandle_t queue_{nullptr};
  TaskHandle_t task_{nullptr};
  uint8_t node_mac_[6]{};

  // Owned by the core task; results cross to the main loop through the atomics below.
  wisp_core::LinkMotion ap_motion_;
  wisp_core::MotionDetector detector_;
  std::atomic<float> ap_score_{NAN};
  std::atomic<bool> ap_active_{false};

  bool csi_started_{false};
  bool stream_open_{false};
  std::atomic<bool> raw_stream_enabled_{false};
  std::atomic<uint32_t> frames_{0};
  uint32_t dropped_total_{0};
  uint32_t last_check_ms_{0};
  uint32_t last_stats_ms_{0};
};

}  // namespace esphome::wisp
