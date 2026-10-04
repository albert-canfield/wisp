#pragma once
// wisp-core: portable logic. core_* files never include ESPHome or ESP-IDF headers.

#include <cstddef>
#include <cstdint>

namespace wisp_core {

// LLTF + HT-LTF + STBC HT-LTF, the most an S3 or C3 reports for one 20 MHz frame.
constexpr size_t MAX_CSI_BYTES = 384;

constexpr uint8_t CSI_FLAG_STBC = 0x01;
constexpr uint8_t CSI_FLAG_FIRST_WORD_INVALID = 0x02;
constexpr uint8_t CSI_FLAG_SGI = 0x04;

// One CSI capture, handed from the radio callback to the core task. Plain data, no chip types.
struct CsiRecord {
  uint8_t source[6];         // transmitter MAC: an access point's BSSID or another node
  uint32_t timestamp_us;     // radio timestamp, local clock
  int8_t rssi;
  int8_t noise_floor;
  uint8_t channel;
  uint8_t secondary_channel;
  uint8_t sig_mode;          // 0 legacy (11a/g), 1 HT (11n), 3 VHT
  uint8_t mcs;
  uint8_t cwb;               // 0 = 20 MHz, 1 = 40 MHz
  uint8_t flags;             // CSI_FLAG_*
  uint16_t len;              // bytes used in data
  int8_t data[MAX_CSI_BYTES];  // per subcarrier: imaginary, then real
};

}  // namespace wisp_core
