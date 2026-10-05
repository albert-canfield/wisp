#include "esp_espnow.h"

#include <cstring>

#include "esp_wifi.h"

namespace wisp_platform {

static const uint8_t BROADCAST[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};

QueueHandle_t EspNowRadio::rx_queue_ = nullptr;
std::atomic<uint32_t> EspNowRadio::dropped_{0};

bool EspNowRadio::start(QueueHandle_t rx_queue) {
  rx_queue_ = rx_queue;
  if (esp_now_init() != ESP_OK)
    return false;
  esp_now_register_recv_cb(&EspNowRadio::on_recv_);
  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, BROADCAST, 6);
  peer.channel = 0;  // follow the current WiFi channel
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;
  if (!esp_now_is_peer_exist(BROADCAST) && esp_now_add_peer(&peer) != ESP_OK)
    return false;
  // Every beacon at the same 802.11n rate, so every one carries the same kind of CSI.
  esp_now_rate_config_t rate = {};
  rate.phymode = WIFI_PHY_MODE_HT20;
  rate.rate = WIFI_PHY_RATE_MCS0_LGI;
  esp_now_set_peer_rate_config(BROADCAST, &rate);
  return true;
}

bool EspNowRadio::send_broadcast(const uint8_t *data, size_t len) {
  return esp_now_send(BROADCAST, data, len) == ESP_OK;
}

void EspNowRadio::on_recv_(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  if (rx_queue_ == nullptr || info == nullptr || len <= 0 || len > static_cast<int>(wisp_core::BEACON_MAX_BYTES))
    return;
  EspNowFrame f;
  memcpy(f.src, info->src_addr, 6);
  f.rssi = info->rx_ctrl != nullptr ? static_cast<int8_t>(info->rx_ctrl->rssi) : 0;
  f.len = static_cast<uint8_t>(len);
  memcpy(f.data, data, len);
  if (xQueueSend(rx_queue_, &f, 0) != pdTRUE)
    dropped_.fetch_add(1);
}

bool SlotScheduler::start(EspNowRadio *radio) {
  this->radio_ = radio;
  esp_timer_create_args_t args = {};
  args.callback = &SlotScheduler::on_timer_;
  args.arg = this;
  args.dispatch_method = ESP_TIMER_TASK;
  args.name = "wisp_slot";
  if (esp_timer_create(&args, &this->timer_) != ESP_OK)
    return false;
  this->arm_();
  return true;
}

void SlotScheduler::set_beacon(const uint8_t *data, size_t len) {
  if (len > sizeof(this->buf_))
    return;
  portENTER_CRITICAL(&this->lock_);
  memcpy(this->buf_, data, len);
  this->len_ = len;
  portEXIT_CRITICAL(&this->lock_);
}

void SlotScheduler::set_relay(const uint8_t *data, size_t len) {
  if (len > sizeof(this->relay_))
    return;
  portENTER_CRITICAL(&this->lock_);
  memcpy(this->relay_, data, len);
  this->relay_len_ = len;
  portEXIT_CRITICAL(&this->lock_);
}

// The shared clock: the access point's TSF while connected, otherwise the local clock.
static uint64_t shared_clock_us(bool &synced) {
  const int64_t tsf = esp_wifi_get_tsf_time(WIFI_IF_STA);
  synced = tsf > 0;
  return synced ? static_cast<uint64_t>(tsf) : static_cast<uint64_t>(esp_timer_get_time());
}

void SlotScheduler::arm_() {
  bool synced = false;
  const uint64_t now = shared_clock_us(synced);
  this->synced_.store(synced);
  const int slot = this->slot_.load();
  uint64_t delay = wisp_core::ROUND_US;  // not transmitting: check again next round
  if (slot >= 0)
    delay = wisp_core::next_slot_time(now, slot) - now;
  esp_timer_start_once(this->timer_, delay);
}

void SlotScheduler::on_timer_(void *arg) {
  auto *self = static_cast<SlotScheduler *>(arg);
  if (self->slot_.load() >= 0) {
    uint8_t frame[wisp_core::BEACON_MAX_BYTES];
    uint8_t relay[wisp_core::ROW_FRAME_MAX_BYTES];
    size_t len, relay_len;
    portENTER_CRITICAL(&self->lock_);
    len = self->len_;
    memcpy(frame, self->buf_, len);
    relay_len = self->relay_len_;
    memcpy(relay, self->relay_, relay_len);
    self->relay_len_ = 0;  // each relayed row goes out once
    portEXIT_CRITICAL(&self->lock_);
    if (len > 0 && self->radio_->send_broadcast(frame, len))
      self->sent_.fetch_add(1);
    if (relay_len > 0)
      self->radio_->send_broadcast(relay, relay_len);
  }
  self->arm_();
}

}  // namespace wisp_platform
