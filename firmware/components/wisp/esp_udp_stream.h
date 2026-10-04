#pragma once
// Platform adapter (ESP-IDF): UDP streams to subscribers (raw CSI, link reports).

#include <cstddef>
#include <cstdint>

#include "lwip/sockets.h"

namespace wisp_platform {

class UdpStream {
 public:
  static constexpr int MAX_SUBSCRIBERS = 4;

  bool open(uint16_t port);
  // Handles subscription requests. Returns the streams (bit mask) that have a live subscriber.
  uint8_t poll(uint32_t now_ms);
  // Sends to every live subscriber of that stream.
  void send(uint8_t stream, const uint8_t *data, size_t len, uint32_t now_ms);

 protected:
  struct Subscriber {
    sockaddr_in addr;
    uint8_t streams;
    uint32_t lease_until_ms;
    bool used;
  };
  static bool live_(const Subscriber &s, uint32_t now_ms) {
    return s.used && static_cast<int32_t>(s.lease_until_ms - now_ms) > 0;
  }

  int sock_{-1};
  Subscriber subs_[MAX_SUBSCRIBERS]{};
};

}  // namespace wisp_platform
