#include "esp_udp_stream.h"

#include <fcntl.h>

#include "core_raw_packet.h"

namespace wisp_platform {

bool UdpStream::open(uint16_t port) {
  this->sock_ = lwip_socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
  if (this->sock_ < 0)
    return false;
  sockaddr_in addr = {};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(port);
  addr.sin_addr.s_addr = htonl(INADDR_ANY);
  if (lwip_bind(this->sock_, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) != 0) {
    lwip_close(this->sock_);
    this->sock_ = -1;
    return false;
  }
  lwip_fcntl(this->sock_, F_SETFL, lwip_fcntl(this->sock_, F_GETFL, 0) | O_NONBLOCK);
  return true;
}

uint8_t UdpStream::poll(uint32_t now_ms) {
  if (this->sock_ < 0)
    return 0;
  for (auto &s : this->subs_) {
    if (s.used && !live_(s, now_ms))
      s.used = false;  // expired: never let a stale address come back when the clock wraps
  }
  uint8_t buf[16];
  sockaddr_in from = {};
  socklen_t from_len = sizeof(from);
  int n;
  while ((n = lwip_recvfrom(this->sock_, buf, sizeof(buf), 0, reinterpret_cast<sockaddr *>(&from), &from_len)) > 0) {
    from_len = sizeof(from);
    const uint8_t streams = wisp_core::parse_subscribe(buf, static_cast<size_t>(n));
    if (streams == 0)
      continue;
    // Same address renews; otherwise a free entry; otherwise the one closest to expiry.
    Subscriber *slot = nullptr;
    for (auto &s : this->subs_) {
      if (s.used && s.addr.sin_addr.s_addr == from.sin_addr.s_addr && s.addr.sin_port == from.sin_port) {
        slot = &s;
        break;
      }
    }
    for (auto &s : this->subs_) {
      if (slot == nullptr && !live_(s, now_ms))
        slot = &s;
    }
    if (slot == nullptr) {
      slot = &this->subs_[0];
      for (auto &s : this->subs_) {
        if (static_cast<int32_t>(s.lease_until_ms - slot->lease_until_ms) < 0)
          slot = &s;
      }
    }
    *slot = Subscriber{from, streams, now_ms + wisp_core::SUBSCRIBE_LEASE_MS, true};
  }
  uint8_t live = 0;
  for (const auto &s : this->subs_) {
    if (live_(s, now_ms))
      live |= s.streams;
  }
  return live;
}

void UdpStream::send(uint8_t stream, const uint8_t *data, size_t len, uint32_t now_ms) {
  if (this->sock_ < 0)
    return;
  for (const auto &s : this->subs_) {
    if (live_(s, now_ms) && (s.streams & stream))
      lwip_sendto(this->sock_, data, len, 0, reinterpret_cast<const sockaddr *>(&s.addr), sizeof(s.addr));
  }
}

}  // namespace wisp_platform
