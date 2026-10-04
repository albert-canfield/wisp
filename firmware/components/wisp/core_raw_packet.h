#pragma once
// Raw CSI packet, protocol version 1, little-endian. Reader: firmware/tools/csi_recorder.py
//
//  0  4  magic "WISP"          18  6  source MAC        31  1  secondary channel
//  4  1  protocol version      24  4  timestamp (us)    32  1  sig mode
//  5  1  packet type (1)       28  1  RSSI (int8)       33  1  MCS
//  6  2  header length (38)    29  1  noise floor       34  1  bandwidth (cwb)
//  8  4  sequence number       30  1  channel           35  1  flags
// 12  6  node MAC                                       36  2  CSI length, then CSI bytes
//
// A computer asks for streams by sending "WSUB" + protocol version + stream mask to port 47010;
// the node streams to that address for a short lease, renewed by each request.

#include <cstddef>
#include <cstdint>
#include <cstring>

#include "core_csi_record.h"

namespace wisp_core {

constexpr uint8_t PROTOCOL_VERSION = 1;
constexpr uint8_t PACKET_RAW_CSI = 1;
constexpr size_t RAW_HEADER_BYTES = 38;
constexpr size_t RAW_PACKET_MAX = RAW_HEADER_BYTES + MAX_CSI_BYTES;
constexpr uint32_t SUBSCRIBE_LEASE_MS = 10000;

inline void put_u16(uint8_t *p, uint16_t v) {
  p[0] = static_cast<uint8_t>(v);
  p[1] = static_cast<uint8_t>(v >> 8);
}

inline void put_u32(uint8_t *p, uint32_t v) {
  for (int i = 0; i < 4; i++)
    p[i] = static_cast<uint8_t>(v >> (8 * i));
}

// Writes one raw CSI packet into out. Returns its length, or 0 if it does not fit.
inline size_t encode_raw_csi(const CsiRecord &r, const uint8_t node_mac[6], uint32_t seq, uint8_t *out,
                             size_t cap) {
  const size_t len = RAW_HEADER_BYTES + r.len;
  if (r.len > MAX_CSI_BYTES || len > cap)
    return 0;
  memcpy(out, "WISP", 4);
  out[4] = PROTOCOL_VERSION;
  out[5] = PACKET_RAW_CSI;
  put_u16(out + 6, RAW_HEADER_BYTES);
  put_u32(out + 8, seq);
  memcpy(out + 12, node_mac, 6);
  memcpy(out + 18, r.source, 6);
  put_u32(out + 24, r.timestamp_us);
  out[28] = static_cast<uint8_t>(r.rssi);
  out[29] = static_cast<uint8_t>(r.noise_floor);
  out[30] = r.channel;
  out[31] = r.secondary_channel;
  out[32] = r.sig_mode;
  out[33] = r.mcs;
  out[34] = r.cwb;
  out[35] = r.flags;
  put_u16(out + 36, r.len);
  memcpy(out + RAW_HEADER_BYTES, r.data, r.len);
  return len;
}

constexpr uint8_t STREAM_RAW_CSI = 0x01;
constexpr uint8_t STREAM_LINKS = 0x02;

// The streams a subscription asks for (bit mask), or 0 if p is not a subscription.
// A 5 byte request (version 1 recorders) means raw CSI only.
inline uint8_t parse_subscribe(const uint8_t *p, size_t len) {
  if (len < 5 || memcmp(p, "WSUB", 4) != 0 || p[4] != PROTOCOL_VERSION)
    return 0;
  return len >= 6 ? p[5] : STREAM_RAW_CSI;
}

}  // namespace wisp_core
