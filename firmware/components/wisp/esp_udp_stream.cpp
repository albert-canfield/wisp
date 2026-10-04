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

bool UdpStream::poll(uint32_t now_ms) {
  if (this->sock_ < 0)
    return false;
  uint8_t buf[16];
  sockaddr_in from = {};
  socklen_t from_len = sizeof(from);
  int n;
  while ((n = lwip_recvfrom(this->sock_, buf, sizeof(buf), 0, reinterpret_cast<sockaddr *>(&from), &from_len)) > 0) {
    if (wisp_core::is_subscribe_request(buf, static_cast<size_t>(n))) {
      this->peer_ = from;
      this->has_peer_ = true;
      this->lease_until_ms_ = now_ms + wisp_core::SUBSCRIBE_LEASE_MS;
    }
    from_len = sizeof(from);
  }
  return this->has_peer_ && static_cast<int32_t>(this->lease_until_ms_ - now_ms) > 0;
}

bool UdpStream::send(const uint8_t *data, size_t len) {
  if (this->sock_ < 0 || !this->has_peer_)
    return false;
  return lwip_sendto(this->sock_, data, len, 0, reinterpret_cast<const sockaddr *>(&this->peer_),
                     sizeof(this->peer_)) == static_cast<int>(len);
}

}  // namespace wisp_platform
