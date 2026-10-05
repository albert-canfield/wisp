#include "esp_ftm.h"

#ifdef USE_WISP_FTM
#include <cstring>

#include "esp_event.h"
#include "esp_wifi.h"

namespace wisp_platform {

bool FtmProbe::start() {
  return esp_event_handler_register(WIFI_EVENT, WIFI_EVENT_FTM_REPORT, &FtmProbe::on_event_, this) == ESP_OK;
}

bool FtmProbe::measure(const uint8_t bssid[6], uint8_t channel) {
  wifi_ftm_initiator_cfg_t cfg = {};
  memcpy(cfg.resp_mac, bssid, 6);
  cfg.channel = channel;
  cfg.frm_count = 16;
  cfg.burst_period = 2;  // 200 ms between bursts
  cfg.use_get_report_api = false;
  return esp_wifi_ftm_initiate_session(&cfg) == ESP_OK;
}

void FtmProbe::on_event_(void *arg, const char *base, int32_t id, void *data) {
  auto *self = static_cast<FtmProbe *>(arg);
  auto *report = static_cast<wifi_event_ftm_report_t *>(data);
  self->status_.store(static_cast<int>(report->status));
  self->results_.fetch_add(1);
  self->distance_m_.store(report->status == FTM_STATUS_SUCCESS ? report->dist_est / 100.0f : NAN);
  free(report->ftm_report_data);
  report->ftm_report_data = nullptr;
}

}  // namespace wisp_platform

#else

namespace wisp_platform {
bool FtmProbe::start() { return false; }
bool FtmProbe::measure(const uint8_t *, uint8_t) { return false; }
void FtmProbe::on_event_(void *, const char *, int32_t, void *) {}
}  // namespace wisp_platform

#endif
