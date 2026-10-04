#pragma once
// Platform adapter (ESP-IDF): UDP stream to whoever subscribed last (raw CSI for now).

#include <cstddef>
#include <cstdint>

#include "lwip/sockets.h"

namespace wisp_platform {

class UdpStream {
 public:
  bool open(uint16_t port);
  // Handles subscription requests. Returns true while a subscriber's lease is valid.
  bool poll(uint32_t now_ms);
  bool send(const uint8_t *data, size_t len);

 protected:
  int sock_{-1};
  sockaddr_in peer_{};
  bool has_peer_{false};
  uint32_t lease_until_ms_{0};
};

}  // namespace wisp_platform
