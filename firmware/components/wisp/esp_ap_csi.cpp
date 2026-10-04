#include "esp_ap_csi.h"

#include <cstring>

#include "lwip/ip_addr.h"
#include "ping/ping_sock.h"

namespace wisp_platform {

bool CsiCapture::start(QueueHandle_t queue) {
  this->queue_ = queue;
  wifi_csi_config_t cfg = {};
  cfg.lltf_en = true;
  cfg.htltf_en = true;
  cfg.stbc_htltf2_en = true;
  cfg.ltf_merge_en = false;      // keep LLTF and HT-LTF apart in the raw data
  cfg.channel_filter_en = false;  // raw subcarriers, no smoothing between neighbours
  cfg.manu_scale = false;
  if (esp_wifi_set_csi_config(&cfg) != ESP_OK)
    return false;
  if (esp_wifi_set_csi_rx_cb(&CsiCapture::on_csi_, this) != ESP_OK)
    return false;
  return esp_wifi_set_csi(true) == ESP_OK;
}

void CsiCapture::set_source(const uint8_t mac[6]) {
  portENTER_CRITICAL(&this->lock_);
  memcpy(this->source_, mac, 6);
  this->has_source_ = true;
  portEXIT_CRITICAL(&this->lock_);
}

void CsiCapture::on_csi_(void *ctx, wifi_csi_info_t *info) {
  auto *self = static_cast<CsiCapture *>(ctx);
  if (info == nullptr || info->buf == nullptr || self->queue_ == nullptr)
    return;
  portENTER_CRITICAL(&self->lock_);
  const bool match = self->has_source_ && memcmp(info->mac, self->source_, 6) == 0;
  portEXIT_CRITICAL(&self->lock_);
  if (!match)
    return;

  wisp_core::CsiRecord &rec = self->scratch_;
  const wifi_pkt_rx_ctrl_t &rx = info->rx_ctrl;
  memcpy(rec.source, info->mac, 6);
  rec.timestamp_us = rx.timestamp;
  rec.rssi = static_cast<int8_t>(rx.rssi);
  rec.noise_floor = static_cast<int8_t>(rx.noise_floor);
  rec.channel = rx.channel;
  rec.secondary_channel = rx.secondary_channel;
  rec.sig_mode = rx.sig_mode;
  rec.mcs = rx.mcs;
  rec.cwb = rx.cwb;
  rec.flags = (rx.stbc ? wisp_core::CSI_FLAG_STBC : 0) |
              (info->first_word_invalid ? wisp_core::CSI_FLAG_FIRST_WORD_INVALID : 0) |
              (rx.sgi ? wisp_core::CSI_FLAG_SGI : 0);
  const uint16_t n = info->len > wisp_core::MAX_CSI_BYTES ? wisp_core::MAX_CSI_BYTES : info->len;
  rec.len = n;
  memcpy(rec.data, info->buf, n);
  if (xQueueSend(self->queue_, &rec, 0) != pdTRUE)
    self->dropped_.fetch_add(1);
}

bool GatewayPinger::start(uint32_t gateway, uint32_t interval_ms) {
  this->stop();
  ip_addr_t target = {};
  IP_SET_TYPE(&target, IPADDR_TYPE_V4);
  ip4_addr_set_u32(ip_2_ip4(&target), gateway);

  esp_ping_config_t cfg = ESP_PING_DEFAULT_CONFIG();
  cfg.target_addr = target;
  cfg.count = ESP_PING_COUNT_INFINITE;
  cfg.interval_ms = interval_ms;
  cfg.timeout_ms = 1000;
  cfg.data_size = 8;  // tiny: the reply only has to exist

  esp_ping_callbacks_t cbs = {};
  esp_ping_handle_t handle = nullptr;
  if (esp_ping_new_session(&cfg, &cbs, &handle) != ESP_OK)
    return false;
  if (esp_ping_start(handle) != ESP_OK) {
    esp_ping_delete_session(handle);
    return false;
  }
  this->handle_ = handle;
  this->target_ = gateway;
  return true;
}

void GatewayPinger::stop() {
  if (this->handle_ == nullptr)
    return;
  esp_ping_stop(this->handle_);
  esp_ping_delete_session(this->handle_);
  this->handle_ = nullptr;
  this->target_ = 0;
}

}  // namespace wisp_platform
