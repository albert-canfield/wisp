#include "wisp_component.h"

#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"

#include "esphome/components/wifi/wifi_component.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#include "core_raw_packet.h"

namespace esphome::wisp {

static const char *const TAG = "wisp";

static constexpr UBaseType_t QUEUE_DEPTH = 12;
static constexpr uint32_t CORE_TASK_STACK = 4096;
static constexpr UBaseType_t CORE_TASK_PRIORITY = 5;
static constexpr uint32_t STATS_INTERVAL_MS = 10000;

void WispComponent::setup() {
  esp_read_mac(this->node_mac_, ESP_MAC_WIFI_STA);
  this->queue_ = xQueueCreate(QUEUE_DEPTH, sizeof(wisp_core::CsiRecord));
  if (this->queue_ == nullptr) {
    ESP_LOGE(TAG, "No memory for the CSI queue");
    this->mark_failed();
    return;
  }
  this->stream_open_ = this->raw_stream_.open(this->raw_stream_port_);
  if (!this->stream_open_)
    ESP_LOGW(TAG, "Raw CSI stream: cannot open UDP port %u", this->raw_stream_port_);
  xTaskCreate(&WispComponent::core_task_, "wisp_core", CORE_TASK_STACK, this, CORE_TASK_PRIORITY, &this->task_);
  this->last_stats_ms_ = millis();
}

void WispComponent::loop() {
  const uint32_t now = millis();
  if (now - this->last_check_ms_ < 1000)
    return;
  this->last_check_ms_ = now;
  if (!this->csi_started_) {
    this->csi_started_ = this->capture_.start(this->queue_);
    if (this->csi_started_)
      ESP_LOGI(TAG, "CSI capture started");
  }
  this->update_ap_();
  if (this->ap_motion_score_sensor_ != nullptr)
    this->ap_motion_score_sensor_->publish_state(this->ap_score_.load());
  if (this->ap_motion_binary_sensor_ != nullptr)
    this->ap_motion_binary_sensor_->publish_state(this->ap_active_.load());
  if (now - this->last_stats_ms_ >= STATS_INTERVAL_MS)
    this->publish_stats_(now);
}

// Follows the home access point: its BSSID filters the CSI, its gateway gets the pings.
void WispComponent::update_ap_() {
  if (!wifi::global_wifi_component->is_connected()) {
    if (this->pinger_.running()) {
      this->pinger_.stop();
      ESP_LOGI(TAG, "WiFi down, gateway pings stopped");
    }
    return;
  }
  wifi_ap_record_t ap;
  if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK)
    this->capture_.set_source(ap.bssid);

  esp_netif_t *sta = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
  esp_netif_ip_info_t ip;
  if (sta == nullptr || esp_netif_get_ip_info(sta, &ip) != ESP_OK || ip.gw.addr == 0)
    return;
  if (this->pinger_.running() && this->pinger_.target() == ip.gw.addr)
    return;
  if (this->pinger_.start(ip.gw.addr, this->ap_ping_interval_ms_)) {
    ESP_LOGI(TAG, "Pinging gateway " IPSTR " every %" PRIu32 " ms for AP CSI", IP2STR(&ip.gw),
             this->ap_ping_interval_ms_);
  } else {
    ESP_LOGW(TAG, "Could not start gateway pings");
  }
}

void WispComponent::publish_stats_(uint32_t now) {
  const uint32_t frames = this->frames_.exchange(0);
  const float rate = frames * 1000.0f / static_cast<float>(now - this->last_stats_ms_);
  this->last_stats_ms_ = now;
  this->dropped_total_ += this->capture_.take_dropped();
  if (this->ap_csi_rate_sensor_ != nullptr)
    this->ap_csi_rate_sensor_->publish_state(rate);
  if (this->csi_dropped_sensor_ != nullptr)
    this->csi_dropped_sensor_->publish_state(this->dropped_total_);
}

// The core task: drains the CSI queue, scores the AP link once a second and streams raw
// frames to a subscriber.
void WispComponent::core_task_(void *arg) {
  auto *self = static_cast<WispComponent *>(arg);
  wisp_core::CsiRecord rec;
  uint8_t packet[wisp_core::RAW_PACKET_MAX];
  uint32_t seq = 0;
  uint32_t last_tick = 0;
  for (;;) {
    const bool got = xQueueReceive(self->queue_, &rec, pdMS_TO_TICKS(100)) == pdTRUE;
    const uint32_t now = static_cast<uint32_t>(esp_timer_get_time() / 1000);
    if (now - last_tick >= 1000) {
      last_tick = now;
      const float score = self->ap_motion_.tick();
      self->ap_score_.store(score);
      self->ap_active_.store(self->detector_.update(score));
    }
    const bool streaming = self->stream_open_ && self->raw_stream_enabled_.load() && self->raw_stream_.poll(now);
    if (!got)
      continue;
    self->frames_.fetch_add(1);
    self->ap_motion_.add_frame(rec);
    if (streaming) {
      const size_t n = wisp_core::encode_raw_csi(rec, self->node_mac_, seq++, packet, sizeof(packet));
      if (n > 0)
        self->raw_stream_.send(packet, n);
    }
  }
}

void WispComponent::dump_config() {
  ESP_LOGCONFIG(TAG,
                "Wisp core:\n"
                "  AP ping interval: %" PRIu32 " ms\n"
                "  Raw CSI stream port: %u%s\n"
                "  Motion threshold: %.2f",
                this->ap_ping_interval_ms_, this->raw_stream_port_, this->stream_open_ ? "" : " (not open)",
                this->motion_threshold_);
  LOG_SENSOR("  ", "AP CSI rate", this->ap_csi_rate_sensor_);
  LOG_SENSOR("  ", "CSI dropped", this->csi_dropped_sensor_);
  LOG_SENSOR("  ", "AP motion score", this->ap_motion_score_sensor_);
  LOG_BINARY_SENSOR("  ", "AP motion", this->ap_motion_binary_sensor_);
}

}  // namespace esphome::wisp
