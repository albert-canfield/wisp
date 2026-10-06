#include "wisp_component.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <new>

#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "sdkconfig.h"

#include "esphome/components/wifi/wifi_component.h"
#include "esphome/core/hal.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"

#include "core_hive_report.h"
#include "core_raw_packet.h"

namespace esphome::wisp {

static const char *const TAG = "wisp";

static constexpr UBaseType_t CSI_QUEUE_DEPTH = 16;
static constexpr UBaseType_t ESPNOW_QUEUE_DEPTH = 16;  // a layout solve on a C3 with 16 nodes takes tens of ms
static constexpr uint32_t CORE_TASK_STACK = 8192;  // measured worst case about 4 KB: room to spare
static constexpr UBaseType_t CORE_TASK_PRIORITY = 5;
static constexpr uint32_t STATS_INTERVAL_MS = 10000;
static constexpr uint32_t HIVE_REPORT_INTERVAL_MS = 5000;
static constexpr uint32_t HEALTH_LOG_INTERVAL_MS = 10 * 60 * 1000;
static constexpr uint32_t CSI_WAIT_FIRST_S = 30;    // no AP frame this long: re-arm CSI, restart pings
static constexpr uint32_t CSI_WAIT_MAX_S = 600;     // doubling up to this (a gateway that never answers)
static constexpr uint32_t ALONE_WAIT_FIRST_MS = 5 * 60 * 1000;   // alone this long: check the channel
static constexpr uint32_t ALONE_WAIT_MAX_MS = 6 * 3600 * 1000;   // doubling up to this
static constexpr uint32_t STEER_RETRY_MS = 30 * 60 * 1000;       // after 3 failed moves, wait this long

#if defined(CONFIG_IDF_TARGET_ESP32S3)
static constexpr uint8_t CHIP = wisp_core::CHIP_ESP32S3;
#elif defined(CONFIG_IDF_TARGET_ESP32C3)
static constexpr uint8_t CHIP = wisp_core::CHIP_ESP32C3;
#elif defined(CONFIG_IDF_TARGET_ESP32C6)
static constexpr uint8_t CHIP = wisp_core::CHIP_ESP32C6;
#elif defined(CONFIG_IDF_TARGET_ESP32)
static constexpr uint8_t CHIP = wisp_core::CHIP_ESP32;
#else
static constexpr uint8_t CHIP = wisp_core::CHIP_OTHER;
#endif

// The grid access point, saved so later boots join it directly.
struct GridApPref {
  uint8_t bssid[6];
  uint8_t channel;
  uint8_t valid;
};

static uint32_t core_now_ms() { return static_cast<uint32_t>(esp_timer_get_time() / 1000); }

static void format_mac(const wisp_core::Mac &m, char *out) {
  snprintf(out, 18, "%02x:%02x:%02x:%02x:%02x:%02x", m.b[0], m.b[1], m.b[2], m.b[3], m.b[4], m.b[5]);
}

void WispComponent::setup() {
  esp_read_mac(this->self_.b, ESP_MAC_WIFI_STA);
  this->grid_.reset(this->self_);
  this->hive_.reset(this->self_);
  this->confirm_.reset(this->self_);
  this->threshold_applied_ = this->motion_threshold_.load();
  this->links_.set_threshold(this->threshold_applied_);
  this->csi_queue_ = xQueueCreate(CSI_QUEUE_DEPTH, sizeof(wisp_core::CsiRecord));
  this->espnow_queue_ = xQueueCreate(ESPNOW_QUEUE_DEPTH, sizeof(wisp_platform::EspNowFrame));
  if (this->csi_queue_ == nullptr || this->espnow_queue_ == nullptr) {
    ESP_LOGE(TAG, "No memory for the core queues");
    this->mark_failed();
    return;
  }
  this->grid_ap_pref_ = global_preferences->make_preference<GridApPref>(fnv1_hash("wisp_grid_ap"));
  GridApPref saved{};
  if (this->grid_ap_pref_.load(&saved) && saved.valid == 1) {
    this->grid_ap_ = wisp_core::Mac::from(saved.bssid);
    this->grid_ap_channel_ = saved.channel;
    this->has_grid_ap_ = true;
    this->boost_grid_ap_();  // before ESPHome's first scan finishes
  }
  this->stream_open_.store(this->stream_.open(this->raw_stream_port_));
  if (!this->stream_open_.load())
    ESP_LOGW(TAG, "Cannot open UDP port %u yet, retrying", this->raw_stream_port_);
  if (xTaskCreate(&WispComponent::core_task_, "wisp_core", CORE_TASK_STACK, this, CORE_TASK_PRIORITY, &this->task_) !=
      pdPASS) {
    ESP_LOGE(TAG, "No memory for the core task");
    this->mark_failed();
    return;
  }
  this->last_stats_ms_ = this->last_health_ms_ = millis();
  this->csi_wait_s_ = CSI_WAIT_FIRST_S;
  this->alone_wait_ms_ = ALONE_WAIT_FIRST_MS;
}

void WispComponent::loop() {
  const uint32_t now = millis();
  this->watch_wifi_();
  if (now - this->last_check_ms_ < 1000)
    return;
  this->last_check_ms_ = now;
  if (!this->csi_started_.load() && this->capture_.start(this->csi_queue_)) {
    this->csi_started_.store(true);
    ESP_LOGI(TAG, "CSI capture started");
  }
  if (this->csi_started_.load() && !this->espnow_started_.load() && this->radio_.start(this->espnow_queue_) &&
      this->scheduler_.start(&this->radio_)) {
    this->espnow_started_.store(true);
    ESP_LOGI(TAG, "ESP-NOW grid started");
  }
  if (!this->stream_open_.load() && this->stream_.open(this->raw_stream_port_)) {
    this->stream_open_.store(true);
    ESP_LOGI(TAG, "UDP port %u open", this->raw_stream_port_);
  }
  const bool connected = wifi::global_wifi_component->is_connected();
  if (connected && !this->was_connected_ && this->csi_started_.load()) {
    // A (re)connection can follow a Wi-Fi restart, which resets CSI and ESP-NOW: arm both again.
    if (this->capture_.start(this->csi_queue_))
      ESP_LOGD(TAG, "CSI capture re-armed after connecting");
    else
      ESP_LOGW(TAG, "CSI capture could not be re-armed; the CSI watchdog tries again");
    if (this->espnow_started_.load() && !this->radio_.restart())
      ESP_LOGW(TAG, "ESP-NOW could not be restarted; the beacon watchdog tries again");
  }
  this->was_connected_ = connected;
  if (this->espnow_started_.load())
    this->scheduler_.keep_armed();
  this->watch_csi_(connected);
  this->watch_alone_(now, connected);
  // Beacon watchdog: a node that has a slot must be sending. If nothing went out for 10 s,
  // restart ESP-NOW (a Wi-Fi restart can leave it dead without telling anyone).
  if (this->espnow_started_.load()) {
    const uint32_t sent = this->scheduler_.sent();
    if (this->scheduler_.slot() < 0 || sent != this->beacons_seen_) {
      this->beacons_seen_ = sent;
      this->beacons_stalled_s_ = 0;
    } else if (++this->beacons_stalled_s_ >= 10) {
      this->beacons_stalled_s_ = 0;
      ESP_LOGW(TAG, "No beacon sent for 10 s, restarting ESP-NOW");
      if (!this->radio_.restart())
        ESP_LOGW(TAG, "ESP-NOW could not be restarted, trying again in 10 s");
    }
  }
  if (connected && !this->steered_)
    this->steer_wifi_(now);
  this->update_ap_();
  if (this->ap_motion_score_sensor_ != nullptr)
    this->ap_motion_score_sensor_->publish_state(this->ap_score_.load());
  if (this->ap_motion_binary_sensor_ != nullptr)
    this->ap_motion_binary_sensor_->publish_state(this->ap_active_.load());
  const bool latched = this->motion_latched_.exchange(false);  // a second confirmed in between counts too
  if (this->motion_binary_sensor_ != nullptr)
    this->motion_binary_sensor_->publish_state(this->motion_.load() || latched);
  if (this->breathing_binary_sensor_ != nullptr)
    this->breathing_binary_sensor_->publish_state(this->breathing_.load());
  if (this->channel_sensor_ != nullptr) {
    const float channel = static_cast<float>(this->grid_channel_.load());
    if (channel > 0 && this->channel_sensor_->get_raw_state() != channel)
      this->channel_sensor_->publish_state(channel);
  }
  if (this->hive_sync_binary_sensor_ != nullptr)
    this->hive_sync_binary_sensor_->publish_state(this->hive_in_sync_.load());
  if (this->grid_nodes_sensor_ != nullptr) {
    const float nodes = static_cast<float>(this->grid_nodes_.load());
    if (this->grid_nodes_sensor_->get_raw_state() != nodes)
      this->grid_nodes_sensor_->publish_state(nodes);
  }
  if (now - this->last_stats_ms_ >= STATS_INTERVAL_MS)
    this->publish_stats_(now);
#ifdef USE_WISP_FTM
  // Experimental: range to the home access point every 30 s; every 30 min after it failed to
  // answer three times in a row (many home access points do not support FTM).
  const uint32_t ftm_interval = this->ftm_failures_ >= 3 ? 30 * 60 * 1000 : 30000;
  if (connected && now - this->last_ftm_ms_ >= ftm_interval) {
    this->last_ftm_ms_ = now;
    if (!this->ftm_started_ && !(this->ftm_started_ = this->ftm_.start()))
      ESP_LOGW(TAG, "FTM probe could not register for reports");
    wifi_ap_record_t ap;
    if (this->ftm_started_ && esp_wifi_sta_get_ap_info(&ap) == ESP_OK && !this->ftm_.measure(ap.bssid, ap.primary))
      ESP_LOGW(TAG, "Could not start an FTM session");
  }
  if (this->ftm_.results() != this->ftm_results_seen_) {
    this->ftm_results_seen_ = this->ftm_.results();
    const int status = this->ftm_.status();
    this->ftm_failures_ = status == 0 ? 0 : static_cast<uint8_t>(this->ftm_failures_ < 255 ? this->ftm_failures_ + 1 : 255);
    if (status == 0) {
      ESP_LOGI(TAG, "FTM to the access point: %.2f m", this->ftm_.distance_m());
    } else {
      ESP_LOGI(TAG, "FTM to the access point failed: status %d (1 unsupported, 3 no response, 5 no valid measurement)",
               status);
    }
    if (this->ap_distance_sensor_ != nullptr)
      this->ap_distance_sensor_->publish_state(this->ftm_.distance_m());
  }
#endif
}

// While ESPHome is (re)connecting: rebuild the list of the home network's APs from each new
// scan (ESPHome may scan several times, and frees the results once connected), and prefer the
// grid AP slightly, once per disconnection. ESPHome clears preferences after each connection and
// lowers an AP that keeps failing, so a refusing or dead access point never strands the node.
void WispComponent::watch_wifi_() {
  auto *wifi = wifi::global_wifi_component;
  if (wifi->is_connected()) {
    this->wifi_up_ = true;
    return;
  }
  if (this->wifi_up_) {  // just disconnected
    this->wifi_up_ = false;
    this->boost_grid_ap_();
    portENTER_CRITICAL(&this->ap_lock_);
    this->has_bssid_ = false;  // no home AP until connected again
    portEXIT_CRITICAL(&this->ap_lock_);
  }
  this->steered_ = false;
  const auto &results = wifi->get_scan_result();
  if (results.empty())
    return;
  const auto sta = wifi->get_sta();
  uint32_t fingerprint = 2166136261u;  // what this scan saw of the home network
  for (const auto &r : results) {
    if (r.get_ssid() != sta.get_ssid())
      continue;
    const int8_t rssi = r.get_rssi();
    fingerprint = wisp_core::fnv1a(fingerprint, r.get_bssid().data(), 6);
    fingerprint = wisp_core::fnv1a(fingerprint, reinterpret_cast<const uint8_t *>(&rssi), 1);
  }
  if (fingerprint == this->scan_fingerprint_)
    return;
  this->scan_fingerprint_ = fingerprint;
  this->aps_seen_.clear();
  for (const auto &r : results) {
    if (r.get_ssid() == sta.get_ssid())
      this->aps_seen_.add(wisp_core::Mac::from(r.get_bssid().data()), r.get_channel(), r.get_rssi());
  }
}

void WispComponent::boost_grid_ap_() {
  if (!this->has_grid_ap_)
    return;
  wifi::bssid_t bssid;
  memcpy(bssid.data(), this->grid_ap_.b, 6);
  wifi::global_wifi_component->set_sta_priority(bssid, 1);
}

// Saved across reboots, so the next boot joins the grid AP straight away. Written only on change.
void WispComponent::remember_grid_ap_(const wisp_core::Mac &bssid, uint8_t channel) {
  if (this->has_grid_ap_ && bssid == this->grid_ap_ && channel == this->grid_ap_channel_)
    return;
  this->grid_ap_ = bssid;
  this->grid_ap_channel_ = channel;
  this->has_grid_ap_ = true;
  GridApPref pref{};
  memcpy(pref.bssid, bssid.b, 6);
  pref.channel = channel;
  pref.valid = 1;
  this->grid_ap_pref_.save(&pref);
}

// Moves this node to the grid channel: same rule on every node, see core_wifi_plan.h.
void WispComponent::steer_wifi_(uint32_t now) {
  this->steered_ = true;  // once per connection
  wifi_ap_record_t cur;
  if (esp_wifi_sta_get_ap_info(&cur) != ESP_OK)
    return;
  const wisp_core::Mac current = wisp_core::Mac::from(cur.bssid);
  this->aps_seen_.add(current, cur.primary, cur.rssi);
  // Keep the remembered grid AP's channel while it is still heard reasonably (6 dB of hysteresis
  // below the usual minimum), so a borderline AP does not make nodes flip between channels. Its
  // channel comes from the scan: the AP may have changed channel since it was saved. If the scan
  // missed it (rebooting, or a power cut brought the APs back in another order), keep its old
  // channel for two scans before letting the rule choose again from a partial picture.
  uint8_t channel = 0;
  if (this->grid_channel_cfg_ == 0 && this->has_grid_ap_) {
    const wisp_core::ApSeen *saved = nullptr;
    for (int i = 0; i < this->aps_seen_.count(); i++) {
      if (this->aps_seen_.data()[i].bssid == this->grid_ap_)
        saved = &this->aps_seen_.data()[i];
    }
    if (saved != nullptr) {
      this->grid_ap_missing_ = 0;
      if (saved->rssi >= this->ap_min_rssi_ - 6)
        channel = saved->channel;
    } else if (this->grid_ap_missing_ < 2) {
      this->grid_ap_missing_++;
      channel = this->grid_ap_channel_;
    }
  }
  if (channel == 0)
    channel = wisp_core::choose_grid_channel(this->aps_seen_.data(), this->aps_seen_.count(), this->ap_min_rssi_,
                                             this->grid_channel_cfg_);
  if (channel == 0 || channel == cur.primary) {
    this->grid_channel_.store(cur.primary);
    this->remember_grid_ap_(current, cur.primary);
    this->steer_attempts_ = 0;
    ESP_LOGI(TAG, "On the grid channel %u (%d APs of this network seen)", cur.primary, this->aps_seen_.count());
    return;
  }
  const int i = wisp_core::choose_home_ap(this->aps_seen_.data(), this->aps_seen_.count(), channel);
  if (i < 0) {
    ESP_LOGW(TAG, "Grid channel %u has no usable AP of this network", channel);
    return;
  }
  if (this->steer_attempts_ >= 3) {
    if (now - this->last_steer_ms_ < STEER_RETRY_MS) {
      ESP_LOGW(TAG, "Could not reach the grid channel %u, staying on channel %u for now", channel, cur.primary);
      return;
    }
    this->steer_attempts_ = 0;  // a while later: try again
  }
  this->steer_attempts_++;
  this->last_steer_ms_ = now;
  const wisp_core::ApSeen &target = this->aps_seen_.data()[i];
  this->remember_grid_ap_(target.bssid, channel);
  this->boost_grid_ap_();
  char mac[18];
  format_mac(target.bssid, mac);
  ESP_LOGI(TAG, "Moving from channel %u to the grid channel %u: access point %s (%d dBm)", cur.primary, channel, mac,
           target.rssi);
  esp_wifi_disconnect();  // ESPHome reconnects, now preferring the grid AP
}

// Follows the home access point: its BSSID names the AP link, its gateway gets the pings.
void WispComponent::update_ap_() {
  if (!wifi::global_wifi_component->is_connected()) {
    if (this->pinger_.running()) {
      this->pinger_.stop();
      ESP_LOGI(TAG, "WiFi down, gateway pings stopped");
    }
    return;
  }
  wifi_ap_record_t ap;
  if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
    portENTER_CRITICAL(&this->ap_lock_);
    memcpy(this->bssid_.b, ap.bssid, 6);
    this->has_bssid_ = true;
    portEXIT_CRITICAL(&this->ap_lock_);
  }
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
  const uint32_t frames = this->ap_frames_.exchange(0);
  const float rate = frames * 1000.0f / static_cast<float>(now - this->last_stats_ms_);
  this->last_stats_ms_ = now;
  this->dropped_total_ += this->capture_.take_dropped();
  this->espnow_dropped_total_ += wisp_platform::EspNowRadio::take_dropped();
  if (this->ap_csi_rate_sensor_ != nullptr)
    this->ap_csi_rate_sensor_->publish_state(rate);
  if (this->csi_dropped_sensor_ != nullptr)
    this->csi_dropped_sensor_->publish_state(this->dropped_total_);
  const UBaseType_t stack_free = this->task_ != nullptr ? uxTaskGetStackHighWaterMark(this->task_) : 0;  // bytes
  if (this->core_stack_sensor_ != nullptr)
    this->core_stack_sensor_->publish_state(stack_free);
  if (this->breathing_rate_sensor_ != nullptr)
    this->breathing_rate_sensor_->publish_state(this->breath_rate_.load());
  const float max_threshold = this->max_link_threshold_.load();
  if (this->max_link_threshold_sensor_ != nullptr &&
      !(std::fabs(this->max_link_threshold_sensor_->get_raw_state() - max_threshold) < 0.01f))
    this->max_link_threshold_sensor_->publish_state(max_threshold);
  const uint32_t jumps = this->self_jumps_.load();
  if (jumps != this->self_jumps_seen_) {
    this->self_jumps_seen_ = jumps;
    if (jumps <= 2)
      ESP_LOGI(TAG, "Hive: took this node's row back from the grid after a restart");
    else
      ESP_LOGW(TAG, "Hive: own row taken back from a relayed copy %" PRIu32 " times: does another device use this MAC?",
               jumps);
  }
  if (now - this->last_health_ms_ >= HEALTH_LOG_INTERVAL_MS) {
    this->last_health_ms_ = now;
    ESP_LOGI(TAG, "Health: core stack %u B free at worst, CSI dropped %" PRIu32 ", ESP-NOW dropped %" PRIu32,
             static_cast<unsigned>(stack_free), this->dropped_total_, this->espnow_dropped_total_);
    if (stack_free < 1024)
      ESP_LOGW(TAG, "Core task stack nearly full: %u B left", static_cast<unsigned>(stack_free));
  }
}

// CSI watchdog: connected and pinging, but no frame from the access point for a while (CSI
// disarmed by a Wi-Fi restart, or the ping session stuck): re-arm CSI and restart the pings.
// The wait doubles each time, so a gateway that never answers pings costs little.
void WispComponent::watch_csi_(bool connected) {
  if (!connected || !this->csi_started_.load() || !this->pinger_.running()) {
    this->csi_quiet_s_ = 0;
    return;
  }
  const uint32_t frames = this->ap_frames_total_.load();
  if (frames != this->csi_seen_) {
    this->csi_seen_ = frames;
    this->csi_quiet_s_ = 0;
    this->csi_wait_s_ = CSI_WAIT_FIRST_S;
    return;
  }
  if (++this->csi_quiet_s_ < this->csi_wait_s_)
    return;
  ESP_LOGW(TAG, "No CSI from the access point for %" PRIu32 " s: re-arming CSI and restarting gateway pings",
           this->csi_quiet_s_);
  this->csi_quiet_s_ = 0;
  this->csi_wait_s_ = std::min(this->csi_wait_s_ * 2, CSI_WAIT_MAX_S);
  if (!this->capture_.start(this->csi_queue_))
    ESP_LOGW(TAG, "CSI capture could not be re-armed");
  this->pinger_.stop();  // update_ap_() starts it again
}

// A node alone on the grid while its network has access points on more than one channel may
// sit on the wrong channel (a scan that missed APs after a power cut, an AP that changed
// channel). Reconnecting makes ESPHome scan again and the steering re-check; waits double, so a
// node that is really alone (the others are off) reconnects rarely.
void WispComponent::watch_alone_(uint32_t now, bool connected) {
  if (!connected || this->grid_channel_cfg_ != 0 || !this->espnow_started_.load() || this->grid_nodes_.load() > 1) {
    this->alone_ = false;
    if (this->grid_nodes_.load() > 1)
      this->alone_wait_ms_ = ALONE_WAIT_FIRST_MS;
    return;
  }
  if (!this->alone_) {
    this->alone_ = true;
    this->alone_since_ms_ = now;
    return;
  }
  if (now - this->alone_since_ms_ < this->alone_wait_ms_)
    return;
  bool channels = false;
  for (int i = 1; i < this->aps_seen_.count() && !channels; i++)
    channels = this->aps_seen_.data()[i].channel != this->aps_seen_.data()[0].channel;
  this->alone_since_ms_ = now;
  const uint32_t waited_min = this->alone_wait_ms_ / 60000;
  this->alone_wait_ms_ = std::min(this->alone_wait_ms_ * 2, ALONE_WAIT_MAX_MS);
  if (!channels)
    return;  // one channel only: nothing to steer
  ESP_LOGI(TAG, "Alone on the grid for %" PRIu32 " min: reconnecting to check the grid channel", waited_min);
  esp_wifi_disconnect();  // ESPHome scans and reconnects; steering runs again
}

bool WispComponent::home_bssid_(wisp_core::Mac &out) {
  portENTER_CRITICAL(&this->ap_lock_);
  const bool has = this->has_bssid_;
  out = this->bssid_;
  portEXIT_CRITICAL(&this->ap_lock_);
  return has;
}

// ---- Core task ----------------------------------------------------------------------------

void WispComponent::core_task_(void *arg) {
  auto *self = static_cast<WispComponent *>(arg);
  wisp_core::CsiRecord rec;
  wisp_platform::EspNowFrame frame;
  uint8_t packet[wisp_core::RAW_PACKET_MAX];
  uint32_t last_round = 0, last_second = 0, last_report = 0;
  bool grid_started = false;
  // Under the task watchdog: if this loop ever hangs, the node reboots instead of going quiet.
  esp_task_wdt_add(nullptr);
  for (;;) {
    esp_task_wdt_reset();
    const bool got = xQueueReceive(self->csi_queue_, &rec, pdMS_TO_TICKS(10)) == pdTRUE;
    const uint32_t now = core_now_ms();
    if (!grid_started && self->espnow_started_.load()) {
      self->grid_.start(now);
      grid_started = true;
    }
    while (xQueueReceive(self->espnow_queue_, &frame, 0) == pdTRUE)
      self->handle_espnow_(frame, now);
    self->live_streams_ = self->stream_open_.load() ? self->stream_.poll(now) : 0;
    if (got)
      self->handle_csi_(rec, now, packet);
    if (now - last_round >= wisp_core::ROUND_US / 1000) {
      last_round = now;
      self->core_round_(now);
    }
    if (now - last_report >= self->report_interval_ms_) {
      last_report = now;
      self->send_report_(now);
    }
    if (now - last_second >= 1000) {
      last_second = now;
      self->core_second_(now);
    }
  }
}

void WispComponent::handle_csi_(const wisp_core::CsiRecord &rec, uint32_t now, uint8_t *packet) {
  const wisp_core::Mac src = wisp_core::Mac::from(rec.source);
  wisp_core::Mac bssid;
  wisp_core::LinkKind kind;
  if (this->home_bssid_(bssid) && src == bssid) {
    kind = wisp_core::LinkKind::ACCESS_POINT;
    this->ap_frames_.fetch_add(1);
    this->ap_frames_total_.fetch_add(1);
  } else if (this->grid_.is_member(src)) {
    kind = wisp_core::LinkKind::NODE;
  } else {
    return;  // allowed a moment ago, but no longer a source
  }
  this->links_.add_frame(rec, kind, now);
  if ((this->live_streams_ & wisp_core::STREAM_RAW_CSI) && this->raw_stream_enabled_.load()) {
    const size_t n = wisp_core::encode_raw_csi(rec, this->self_.b, this->raw_seq_++, packet, wisp_core::RAW_PACKET_MAX);
    if (n > 0)
      this->stream_.send(wisp_core::STREAM_RAW_CSI, packet, n, now);
  }
}

// Beacons (membership, the sender's own hive row and its live scores) and relayed hive rows.
void WispComponent::handle_espnow_(const wisp_platform::EspNowFrame &f, uint32_t now) {
  const wisp_core::Mac from = wisp_core::Mac::from(f.src);
  wisp_core::HiveEntry entries[wisp_core::MAX_ROW];
  if (f.len >= 4 && f.data[3] == wisp_core::ROW_FRAME_TYPE) {
    wisp_core::Mac origin;
    uint16_t version;
    int n;
    // Only from members: a stranger's relays wait until its beacon makes it one.
    uint32_t age_ms;
    if (this->grid_.is_member(from) &&
        wisp_core::decode_row_frame(f.data, f.len, origin, version, age_ms, entries, n))
      this->hive_.on_row(origin, version, entries, n, now, age_ms);
    return;
  }
  wisp_core::Beacon b;
  if (!wisp_core::decode_beacon(f.data, f.len, b))
    return;
  if (this->grid_.on_beacon(from, now, f.rssi, b.seq, b.chip, b.uptime_s, b.hive_hash)) {
    char mac[18];
    format_mac(from, mac);
    ESP_LOGI(TAG, "Node %s joined the grid (%d active)", mac, this->grid_.active_count());
  }
  for (int i = 0; i < b.row_len; i++)
    entries[i] = wisp_core::HiveEntry{b.row[i].mac, b.row[i].rssi};
  this->hive_.on_row(from, b.row_version, entries, b.row_len, now);
  // Its live scores of the nodes it hears, for the confirmation: access points left out.
  int n = 0;
  for (int i = 0; i < b.row_len; i++) {
    const wisp_core::Mac &m = b.row[i].mac;
    if (m == this->self_ || this->grid_.is_member(m) || this->hive_.find(m) != nullptr)
      b.row[n++] = b.row[i];
  }
  this->confirm_.on_beacon(from, b.row, n, now);
}

// Every round: lifecycle, this node's slot, and the beacon it sends in that slot.
void WispComponent::core_round_(uint32_t now) {
  const uint32_t version = this->grid_.version();
  this->grid_.tick(now);
  if (this->grid_.version() != version)
    ESP_LOGI(TAG, "Grid changed: %d active nodes", this->grid_.active_count());
  // Slots by MAC rank among every node the hive knows, not only those heard directly.
  wisp_core::Mac known[wisp_core::MAX_ROWS];
  int known_n = 0;
  for (int i = 0; i < this->hive_.count() && known_n < wisp_core::MAX_ROWS; i++)
    known[known_n++] = this->hive_.row(i).origin;
  const int slot = this->grid_.self_slot(known, known_n);
  this->scheduler_.set_slot(slot);

  wisp_core::Beacon b{};
  b.seq = this->beacon_seq_++;
  b.flags = this->scheduler_.synced() ? wisp_core::BEACON_FLAG_SYNCED : 0;
  b.slot = static_cast<int8_t>(slot);
  b.uptime_s = static_cast<uint32_t>(esp_timer_get_time() / 1000000);
  b.chip = CHIP;
  b.active = static_cast<uint8_t>(this->grid_.active_count());
  wisp_core::Mac bssid;
  const bool has_bssid = this->home_bssid_(bssid);
  b.clock_bssid = has_bssid ? bssid : wisp_core::Mac{};
  auto add_row = [&b, this](const wisp_core::Mac &mac, float rssi) {
    if (b.row_len >= wisp_core::MAX_ROW)
      return;
    const wisp_core::Link *l = this->links_.find(mac);
    // Normalised to the link's own threshold: the others judge it at the default (core_confirm.h)
    const uint8_t score10 = l != nullptr ? wisp_core::beacon_score10(*l) : wisp_core::SCORE_UNKNOWN;
    b.row[b.row_len++] = wisp_core::RowEntry{mac, static_cast<int8_t>(std::lround(rssi)), score10};
  };
  if (has_bssid) {
    const wisp_core::Link *ap = this->links_.find(bssid);
    if (ap != nullptr)
      add_row(bssid, ap->rssi_avg);
  }
  for (int i = 0; i < this->grid_.count(); i++) {
    const wisp_core::Member &m = this->grid_.member(i);
    if (m.state != wisp_core::MemberState::MISSING)
      add_row(m.mac, m.rssi);
  }
  // The live view feeds this node's hive row (averaged, versioned); the beacon then carries the
  // published row with that version, never the live readings, so every node that stores a
  // version stores the same readings. The scores stay live.
  wisp_core::HiveEntry live[wisp_core::MAX_ROW];
  uint8_t score[wisp_core::MAX_ROW];
  const int live_n = b.row_len;
  for (int i = 0; i < live_n; i++) {
    live[i] = wisp_core::HiveEntry{b.row[i].mac, b.row[i].rssi};
    score[i] = b.row[i].score10;
  }
  this->hive_.set_own(live, live_n, now);
  const wisp_core::HiveRow *own = this->hive_.own();
  b.row_len = 0;
  for (int i = 0; i < own->len; i++) {
    uint8_t s10 = wisp_core::SCORE_UNKNOWN;
    for (int k = 0; k < live_n; k++) {
      if (live[k].mac == own->entries[i].mac)
        s10 = score[k];
    }
    b.row[b.row_len++] = wisp_core::RowEntry{own->entries[i].mac, own->entries[i].rssi, s10};
  }
  b.hive_hash = this->hive_.hash();
  b.row_version = own->version;

  uint8_t buf[wisp_core::BEACON_MAX_BYTES];
  const size_t n = wisp_core::encode_beacon(b, buf, sizeof(buf));
  if (n > 0)
    this->scheduler_.set_beacon(buf, n);
  if (const wisp_core::HiveRow *relay = this->hive_.next_relay()) {
    uint8_t rbuf[wisp_core::ROW_FRAME_MAX_BYTES];
    const size_t rn = wisp_core::encode_row_frame(*relay, this->relay_seq_++, now, rbuf, sizeof(rbuf));
    if (rn > 0)
      this->scheduler_.set_relay(rbuf, rn);
  }
  this->update_sources_();
}

// Every few seconds: whether everyone knows the same things, the layout when the hive changed,
// and the hive report for Home Assistant.
void WispComponent::update_hive_(uint32_t now) {
  this->hive_.expire(now);
  const uint32_t hash = this->hive_.hash();
  bool in_sync = true;
  for (int i = 0; i < this->grid_.count(); i++) {
    const wisp_core::Member &m = this->grid_.member(i);
    if (m.state == wisp_core::MemberState::ACTIVE && m.hive_hash != hash)
      in_sync = false;
  }
  this->hive_in_sync_.store(in_sync);
  if (now - this->last_hive_ms_ < HIVE_REPORT_INTERVAL_MS)
    return;
  this->last_hive_ms_ = now;
  if (hash != this->layout_hash_) {
    this->layout_count_ = wisp_core::solve_layout(this->hive_, this->layout_, this->layout_ws_);
    this->layout_hash_ = hash;
    ESP_LOGD(TAG, "Layout of %d nodes", this->layout_count_);  // one line: the task log buffer is small
    char mac[18];
    for (int i = 0; i < this->layout_count_; i++) {
      format_mac(this->layout_[i].mac, mac);
      ESP_LOGV(TAG, "  %s at (%.1f, %.1f) m", mac, this->layout_[i].x, this->layout_[i].y);
    }
  }
  if (this->live_streams_ & wisp_core::STREAM_HIVE) {
    static uint8_t report[wisp_core::HIVE_REPORT_MAX];  // core task only
    const size_t n = wisp_core::encode_hive_report(this->self_, this->hive_seq_++, hash, in_sync, this->layout_,
                                                   this->layout_count_, this->hive_, report, sizeof(report),
                                                   this->report_first_);
    if (n > 0) {
      this->stream_.send(wisp_core::STREAM_HIVE, report, n, now);
      this->report_first_ = (this->report_first_ + report[24]) % wisp_core::MAX_ROWS;  // rows it carried
    }
  }
}

// CSI is captured only from the home access point and grid members.
void WispComponent::update_sources_() {
  wisp_core::Mac bssid;
  const bool has_bssid = this->home_bssid_(bssid);
  if (this->grid_.version() == this->sources_version_ && (!has_bssid || bssid == this->sources_bssid_))
    return;
  uint8_t macs[wisp_platform::CsiCapture::MAX_SOURCES][6];
  int n = 0;
  if (has_bssid)
    memcpy(macs[n++], bssid.b, 6);
  for (int i = 0; i < this->grid_.count() && n < wisp_platform::CsiCapture::MAX_SOURCES; i++)
    memcpy(macs[n++], this->grid_.member(i).mac.b, 6);
  this->capture_.set_sources(macs, n);
  this->sources_version_ = this->grid_.version();
  this->sources_bssid_ = bssid;
}

void WispComponent::send_report_(uint32_t now) {
  uint8_t buf[wisp_core::LINK_REPORT_MAX];
  const uint32_t uptime = static_cast<uint32_t>(esp_timer_get_time() / 1000000);
  // Always encode, so every report covers exactly one interval even after a quiet spell.
  const size_t n =
      this->links_.encode_report(this->self_, this->report_seq_++, uptime, buf, sizeof(buf), &this->pairs_);
  if (n > 0 && (this->live_streams_ & wisp_core::STREAM_LINKS))
    this->stream_.send(wisp_core::STREAM_LINKS, buf, n, now);
}

// Breathing detection follows its switch: the bank is made when it is turned on, freed when off.
void WispComponent::update_breathing_() {
  const bool want = this->breathing_enabled_.load();
  if (want && this->breathing_bank_ == nullptr && !this->breathing_no_memory_) {
    this->breathing_bank_ = new (std::nothrow) wisp_core::BreathingBank();
    if (this->breathing_bank_ == nullptr) {
      this->breathing_no_memory_ = true;  // once per switching on
      ESP_LOGW(TAG, "No memory for breathing detection (%u B)", static_cast<unsigned>(sizeof(wisp_core::BreathingBank)));
      return;
    }
    this->links_.set_breathing(this->breathing_bank_);
    ESP_LOGI(TAG, "Breathing detection on");
  } else if (!want) {
    this->breathing_no_memory_ = false;
    if (this->breathing_bank_ != nullptr) {
      this->links_.set_breathing(nullptr);
      delete this->breathing_bank_;
      this->breathing_bank_ = nullptr;
      ESP_LOGI(TAG, "Breathing detection off");
    }
  }
}

void WispComponent::core_second_(uint32_t now) {
  const float threshold = this->motion_threshold_.load();
  if (threshold != this->threshold_applied_) {  // changed from Home Assistant
    this->threshold_applied_ = threshold;
    this->links_.set_threshold(threshold);
  }
  this->update_breathing_();
  this->links_.tick_second(now);
  // The hive's confirmation: this node's flags, the scores in the beacons it hears, the layout.
  const bool motion = this->confirm_.update(this->links_, now, this->layout_, this->layout_count_, &this->hive_);
  this->confirm_.pairs(this->pairs_);
  // Each link learns its quiet scores while the hive confirms no motion anywhere
  this->links_.learn_quiet(this->pairs_.n > 0, now);
  this->max_link_threshold_.store(this->links_.max_threshold());
  // Breathing: BREATHING_AGREE links at once (the night's only false alarms were one link alone)
  int breathing = 0;
  float rate = NAN, clearest = 0.0f;
  for (int i = 0; i < this->links_.count() && this->breathing_bank_ != nullptr; i++) {
    const wisp_core::Link &l = this->links_.link(i);
    if (!l.breathing)
      continue;
    breathing++;
    const float ratio = this->breathing_bank_->ratio(l.source);
    if (ratio > clearest) {
      clearest = ratio;
      rate = l.breath_rate;
    }
  }
  this->breathing_.store(breathing >= wisp_core::BREATHING_AGREE);
  if (breathing < wisp_core::BREATHING_AGREE)
    rate = NAN;
  this->breath_rate_.store(rate);
  this->motion_.store(motion);
  if (motion)
    this->motion_latched_.store(true);
  wisp_core::Mac bssid;
  const wisp_core::Link *ap = this->home_bssid_(bssid) ? this->links_.find(bssid) : nullptr;
  this->ap_score_.store(ap != nullptr ? ap->score : NAN);
  this->ap_active_.store(ap != nullptr && ap->active);
  this->grid_nodes_.store(this->grid_.active_count());
  this->self_jumps_.store(this->hive_.self_jumps());
  this->update_hive_(now);
}

void WispComponent::dump_config() {
  char mac[18];
  format_mac(this->self_, mac);
  ESP_LOGCONFIG(TAG,
                "Wisp core:\n"
                "  Node: %s\n"
                "  AP ping interval: %" PRIu32 " ms\n"
                "  UDP port: %u%s\n"
                "  Link report interval: %" PRIu32 " ms\n"
                "  Motion threshold: %.2f (noisy links higher)\n"
                "  Breathing detection: %s",
                mac, this->ap_ping_interval_ms_, this->raw_stream_port_, this->stream_open_.load() ? "" : " (not open)",
                this->report_interval_ms_, this->motion_threshold_.load(),
                this->breathing_enabled_.load() ? "on" : "off");
  LOG_SENSOR("  ", "AP CSI rate", this->ap_csi_rate_sensor_);
  LOG_SENSOR("  ", "CSI dropped", this->csi_dropped_sensor_);
  LOG_SENSOR("  ", "AP motion score", this->ap_motion_score_sensor_);
  LOG_BINARY_SENSOR("  ", "AP motion", this->ap_motion_binary_sensor_);
  LOG_BINARY_SENSOR("  ", "Motion", this->motion_binary_sensor_);
  LOG_SENSOR("  ", "Grid nodes", this->grid_nodes_sensor_);
  LOG_SENSOR("  ", "Grid channel", this->channel_sensor_);
  LOG_BINARY_SENSOR("  ", "Hive in sync", this->hive_sync_binary_sensor_);
  LOG_SENSOR("  ", "Core stack free", this->core_stack_sensor_);
  LOG_BINARY_SENSOR("  ", "Breathing", this->breathing_binary_sensor_);
  LOG_SENSOR("  ", "Breathing rate", this->breathing_rate_sensor_);
  LOG_SENSOR("  ", "Highest link threshold", this->max_link_threshold_sensor_);
}

}  // namespace esphome::wisp
