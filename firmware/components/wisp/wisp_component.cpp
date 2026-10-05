#include "wisp_component.h"

#include <cmath>
#include <cstdio>

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
static constexpr UBaseType_t ESPNOW_QUEUE_DEPTH = 8;
static constexpr uint32_t CORE_TASK_STACK = 6144;
static constexpr UBaseType_t CORE_TASK_PRIORITY = 5;
static constexpr uint32_t STATS_INTERVAL_MS = 10000;
static constexpr uint32_t HIVE_REPORT_INTERVAL_MS = 5000;

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
  this->grid_ = wisp_core::Grid(this->self_);
  this->hive_ = wisp_core::Hive(this->self_);
  this->links_.set_threshold(this->motion_threshold_);
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
  this->stream_open_ = this->stream_.open(this->raw_stream_port_);
  if (!this->stream_open_)
    ESP_LOGW(TAG, "Cannot open UDP port %u", this->raw_stream_port_);
  xTaskCreate(&WispComponent::core_task_, "wisp_core", CORE_TASK_STACK, this, CORE_TASK_PRIORITY, &this->task_);
  this->last_stats_ms_ = millis();
}

void WispComponent::loop() {
  const uint32_t now = millis();
  this->watch_wifi_(now);
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
  const bool connected = wifi::global_wifi_component->is_connected();
  if (connected && !this->was_connected_ && this->csi_started_.load()) {
    // A (re)connection can reset the radio's CSI settings: arm them again.
    if (this->capture_.start(this->csi_queue_))
      ESP_LOGD(TAG, "CSI capture re-armed after connecting");
  }
  this->was_connected_ = connected;
  if (connected && !this->steered_)
    this->steer_wifi_();
  this->update_ap_();
  if (this->ap_motion_score_sensor_ != nullptr)
    this->ap_motion_score_sensor_->publish_state(this->ap_score_.load());
  if (this->ap_motion_binary_sensor_ != nullptr)
    this->ap_motion_binary_sensor_->publish_state(this->ap_active_.load());
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

// While ESPHome is (re)connecting: remember the home network's APs from its scan results (it
// frees them once connected) and keep the grid AP slightly preferred, so ESPHome joins it
// first. Only by one step: a single failed attempt puts it level with the others again, so a
// dead access point never strands the node.
void WispComponent::watch_wifi_(uint32_t now) {
  auto *wifi = wifi::global_wifi_component;
  if (wifi->is_connected())
    return;
  this->steered_ = false;
  const auto &results = wifi->get_scan_result();
  if (!results.empty()) {
    const auto sta = wifi->get_sta();
    for (const auto &r : results) {
      if (r.get_ssid() == sta.get_ssid())
        this->aps_seen_.add(wisp_core::Mac::from(r.get_bssid().data()), r.get_channel(), r.get_rssi());
    }
  }
  if (now - this->last_boost_ms_ >= 2000) {
    this->last_boost_ms_ = now;
    this->boost_grid_ap_();
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
void WispComponent::steer_wifi_() {
  this->steered_ = true;  // once per connection
  wifi_ap_record_t cur;
  if (esp_wifi_sta_get_ap_info(&cur) != ESP_OK)
    return;
  const wisp_core::Mac current = wisp_core::Mac::from(cur.bssid);
  this->aps_seen_.add(current, cur.primary, cur.rssi);
  const uint8_t channel = wisp_core::choose_grid_channel(this->aps_seen_.data(), this->aps_seen_.count(),
                                                         this->ap_min_rssi_, this->grid_channel_cfg_);
  if (channel == 0 || channel == cur.primary) {
    this->grid_channel_.store(cur.primary);
    this->remember_grid_ap_(current, cur.primary);
    ESP_LOGI(TAG, "On the grid channel %u (%d APs of this network seen)", cur.primary, this->aps_seen_.count());
    return;
  }
  const int i = wisp_core::choose_home_ap(this->aps_seen_.data(), this->aps_seen_.count(), channel);
  if (i < 0) {
    ESP_LOGW(TAG, "Grid channel %u has no usable AP of this network", channel);
    return;
  }
  if (this->steer_attempts_ >= 3) {
    ESP_LOGW(TAG, "Could not reach the grid channel %u, staying on channel %u", channel, cur.primary);
    return;
  }
  this->steer_attempts_++;
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
  if (this->ap_csi_rate_sensor_ != nullptr)
    this->ap_csi_rate_sensor_->publish_state(rate);
  if (this->csi_dropped_sensor_ != nullptr)
    this->csi_dropped_sensor_->publish_state(this->dropped_total_);
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
    self->live_streams_ = self->stream_open_ ? self->stream_.poll(now) : 0;
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

// Beacons (membership plus the sender's own hive row) and relayed hive rows.
void WispComponent::handle_espnow_(const wisp_platform::EspNowFrame &f, uint32_t now) {
  const wisp_core::Mac from = wisp_core::Mac::from(f.src);
  wisp_core::HiveEntry entries[wisp_core::MAX_ROW];
  if (f.len >= 4 && f.data[3] == wisp_core::ROW_FRAME_TYPE) {
    wisp_core::Mac origin;
    uint16_t version;
    int n;
    // Only from members: a stranger's relays wait until its beacon makes it one.
    if (this->grid_.is_member(from) && wisp_core::decode_row_frame(f.data, f.len, origin, version, entries, n))
      this->hive_.on_row(origin, version, entries, n, now);
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
}

// Every round: lifecycle, this node's slot, and the beacon it sends in that slot.
void WispComponent::core_round_(uint32_t now) {
  const uint32_t version = this->grid_.version();
  this->grid_.tick(now);
  if (this->grid_.version() != version)
    ESP_LOGI(TAG, "Grid changed: %d active nodes", this->grid_.active_count());
  const int slot = this->grid_.self_slot(now);
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
    uint8_t score10 = wisp_core::SCORE_UNKNOWN;
    if (l != nullptr && !std::isnan(l->score))
      score10 = static_cast<uint8_t>(std::fmin(254.0f, std::fmax(0.0f, l->score * 10.0f)));
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
  // This node's hive row is the same view, without the scores.
  wisp_core::HiveEntry own[wisp_core::MAX_ROW];
  for (int i = 0; i < b.row_len; i++)
    own[i] = wisp_core::HiveEntry{b.row[i].mac, b.row[i].rssi};
  this->hive_.set_own(own, b.row_len, now);
  b.hive_hash = this->hive_.hash();
  b.row_version = this->hive_.own() != nullptr ? this->hive_.own()->version : 0;

  uint8_t buf[wisp_core::BEACON_MAX_BYTES];
  const size_t n = wisp_core::encode_beacon(b, buf, sizeof(buf));
  if (n > 0)
    this->scheduler_.set_beacon(buf, n);
  if (const wisp_core::HiveRow *relay = this->hive_.next_relay()) {
    uint8_t rbuf[wisp_core::ROW_FRAME_MAX_BYTES];
    const size_t rn = wisp_core::encode_row_frame(*relay, this->relay_seq_++, rbuf, sizeof(rbuf));
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
    if (this->layout_count_ > 0) {
      char mac[18];
      for (int i = 0; i < this->layout_count_; i++) {
        format_mac(this->layout_[i].mac, mac);
        ESP_LOGD(TAG, "Layout: %s at (%.1f, %.1f) m", mac, this->layout_[i].x, this->layout_[i].y);
      }
    }
  }
  if (this->live_streams_ & wisp_core::STREAM_HIVE) {
    static uint8_t report[wisp_core::HIVE_REPORT_MAX];  // core task only
    const size_t n = wisp_core::encode_hive_report(this->self_, this->hive_seq_++, hash, in_sync, this->layout_,
                                                   this->layout_count_, this->hive_, report, sizeof(report));
    if (n > 0)
      this->stream_.send(wisp_core::STREAM_HIVE, report, n, now);
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
  const size_t n = this->links_.encode_report(this->self_, this->report_seq_++, uptime, buf, sizeof(buf));
  if (n > 0 && (this->live_streams_ & wisp_core::STREAM_LINKS))
    this->stream_.send(wisp_core::STREAM_LINKS, buf, n, now);
}

void WispComponent::core_second_(uint32_t now) {
  this->links_.tick_second(now);
  wisp_core::Mac bssid;
  const wisp_core::Link *ap = this->home_bssid_(bssid) ? this->links_.find(bssid) : nullptr;
  this->ap_score_.store(ap != nullptr ? ap->score : NAN);
  this->ap_active_.store(ap != nullptr && ap->active);
  this->grid_nodes_.store(this->grid_.active_count());
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
                "  Motion threshold: %.2f",
                mac, this->ap_ping_interval_ms_, this->raw_stream_port_, this->stream_open_ ? "" : " (not open)",
                this->report_interval_ms_, this->motion_threshold_);
  LOG_SENSOR("  ", "AP CSI rate", this->ap_csi_rate_sensor_);
  LOG_SENSOR("  ", "CSI dropped", this->csi_dropped_sensor_);
  LOG_SENSOR("  ", "AP motion score", this->ap_motion_score_sensor_);
  LOG_BINARY_SENSOR("  ", "AP motion", this->ap_motion_binary_sensor_);
  LOG_SENSOR("  ", "Grid nodes", this->grid_nodes_sensor_);
  LOG_SENSOR("  ", "Grid channel", this->channel_sensor_);
  LOG_BINARY_SENSOR("  ", "Hive in sync", this->hive_sync_binary_sensor_);
}

}  // namespace esphome::wisp
