#pragma once
// wisp-core: the hive report (type 3 in docs/PROTOCOL.md): what this node knows about the whole
// grid, for Home Assistant's floor plan. The layout, then as many rows as fit in one packet,
// starting from row `first`: a caller that moves it on each report gets every row out over a few
// reports when they do not all fit.

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>

#include "core_hive.h"
#include "core_layout.h"
#include "core_raw_packet.h"

namespace wisp_core {

constexpr uint8_t PACKET_HIVE_REPORT = 3;
constexpr size_t HIVE_REPORT_HEADER_BYTES = 26;
constexpr size_t HIVE_REPORT_MAX = 1400;  // one unfragmented UDP packet
constexpr uint8_t HIVE_FLAG_IN_SYNC = 0x01;
constexpr uint8_t HIVE_FLAG_ROWS_TRUNCATED = 0x02;

inline int16_t to_cm(float metres) {
  const float cm = std::round(metres * 100.0f);
  return static_cast<int16_t>(std::fmax(-32767.0f, std::fmin(32767.0f, cm)));
}

inline size_t encode_hive_report(const Mac &self, uint32_t seq, uint32_t hash, bool in_sync, const LayoutPoint *points,
                                 int n_points, const Hive &hive, uint8_t *out, size_t cap, int first = 0) {
  if (cap < HIVE_REPORT_HEADER_BYTES + 10 * static_cast<size_t>(n_points))
    return 0;
  memcpy(out, "WISP", 4);
  out[4] = PROTOCOL_VERSION;
  out[5] = PACKET_HIVE_REPORT;
  put_u16(out + 6, HIVE_REPORT_HEADER_BYTES);
  put_u32(out + 8, seq);
  memcpy(out + 12, self.b, 6);
  put_u32(out + 18, hash);
  uint8_t flags = in_sync ? HIVE_FLAG_IN_SYNC : 0;
  out[23] = static_cast<uint8_t>(n_points);
  out[25] = 0;
  size_t pos = HIVE_REPORT_HEADER_BYTES;
  for (int i = 0; i < n_points; i++) {
    memcpy(out + pos, points[i].mac.b, 6);
    put_u16(out + pos + 6, static_cast<uint16_t>(to_cm(points[i].x)));
    put_u16(out + pos + 8, static_cast<uint16_t>(to_cm(points[i].y)));
    pos += 10;
  }
  uint8_t rows = 0;
  const int count = hive.count();
  for (int r = 0; r < count; r++) {
    const HiveRow &row = hive.row(((first % count) + count + r) % count);
    const size_t need = 9 + 7 * static_cast<size_t>(row.len);
    if (pos + need > cap) {
      flags |= HIVE_FLAG_ROWS_TRUNCATED;
      continue;  // a shorter row further on may still fit
    }
    memcpy(out + pos, row.origin.b, 6);
    put_u16(out + pos + 6, row.version);
    out[pos + 8] = row.len;
    pos += 9;
    for (int k = 0; k < row.len; k++) {
      memcpy(out + pos, row.entries[k].mac.b, 6);
      out[pos + 6] = static_cast<uint8_t>(row.entries[k].rssi);
      pos += 7;
    }
    rows++;
  }
  out[22] = flags;
  out[24] = rows;
  return pos;
}

}  // namespace wisp_core
