"""Wisp UDP protocol, version 1. Spec: docs/PROTOCOL.md. Little-endian throughout."""
from __future__ import annotations

from dataclasses import dataclass
import struct

PROTOCOL_VERSION = 1
MAGIC = b"WISP"
SUBSCRIBE_MAGIC = b"WSUB"

PACKET_RAW_CSI = 1
PACKET_LINK_REPORT = 2

# Subscribe stream mask
STREAM_RAW_CSI = 0x01
STREAM_LINK_REPORTS = 0x02

# Link kind: who transmitted
KIND_AP = 0
KIND_NODE = 1

SCORE_UNKNOWN = 0xFFFF
RSSI_NO_FRAMES = -128
LINK_FLAG_MOTION = 0x01

_HEADER = struct.Struct("<4sBBH")  # magic, version, packet type, header length
_RAW = struct.Struct("<I6s6sIbbBBBBBBH")  # raw CSI fields at offset 8
_REPORT = struct.Struct("<I6sBBI")  # link report fields at offset 8
_LINK = struct.Struct("<6sBbHHBB")  # one link, 14 bytes
RAW_HEADER_LEN = 8 + _RAW.size  # 38
REPORT_HEADER_LEN = 8 + _REPORT.size  # 24
LINK_LEN = _LINK.size


class ProtocolError(ValueError):
    """A packet that cannot be read: not Wisp, too short or inconsistent."""


def format_mac(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


@dataclass(frozen=True, slots=True)
class Header:
    version: int
    packet_type: int
    header_len: int  # offset of the payload


@dataclass(frozen=True, slots=True)
class Link:
    transmitter: str  # an access point BSSID or another node's MAC
    kind: int
    rssi: int | None  # mean dBm over the interval, None without frames
    score: float | None  # 1.0 = as quiet as usual, None when unknown
    spread: float  # percent
    frames: int
    flags: int

    @property
    def motion(self) -> bool:
        return bool(self.flags & LINK_FLAG_MOTION)


@dataclass(frozen=True, slots=True)
class LinkReport:
    seq: int
    node: str  # receiver of every link below
    flags: int
    uptime: int  # seconds
    links: tuple[Link, ...]


@dataclass(frozen=True, slots=True)
class RawCsi:
    seq: int
    node: str  # receiver
    source: str  # transmitter
    timestamp_us: int
    rssi: int
    noise_floor: int
    channel: int
    secondary_channel: int
    sig_mode: int
    mcs: int
    bandwidth: int
    flags: int
    csi: bytes


def build_subscribe(streams: int = STREAM_LINK_REPORTS) -> bytes:
    """Subscription request: renews the sender's lease on the node (10 s)."""
    if not 0 <= streams <= 0xFF:
        raise ValueError(f"stream mask out of range: {streams}")
    return SUBSCRIBE_MAGIC + bytes((PROTOCOL_VERSION, streams))


def parse_header(data: bytes) -> Header:
    if len(data) < _HEADER.size:
        raise ProtocolError(f"short packet: {len(data)} bytes")
    magic, version, packet_type, header_len = _HEADER.unpack_from(data)
    if magic != MAGIC:
        raise ProtocolError("not a Wisp packet")
    return Header(version, packet_type, header_len)


def parse_packet(data: bytes) -> LinkReport | RawCsi | None:
    """Any packet a node sends. None for a version or type this reader does not know."""
    header = parse_header(data)
    if header.version != PROTOCOL_VERSION:
        return None
    if header.packet_type == PACKET_LINK_REPORT:
        return parse_link_report(data, header)
    if header.packet_type == PACKET_RAW_CSI:
        return parse_raw_csi(data, header)
    return None


def parse_link_report(data: bytes, header: Header | None = None) -> LinkReport:
    header = header or parse_header(data)
    _check(header, PACKET_LINK_REPORT, REPORT_HEADER_LEN, len(data))
    seq, node, count, flags, uptime = _REPORT.unpack_from(data, 8)
    end = header.header_len + count * LINK_LEN
    if len(data) < end:
        raise ProtocolError(f"{count} links need {end} bytes, got {len(data)}")
    links = tuple(
        _link(*_LINK.unpack_from(data, offset)) for offset in range(header.header_len, end, LINK_LEN)
    )
    return LinkReport(seq, format_mac(node), flags, uptime, links)


def parse_raw_csi(data: bytes, header: Header | None = None) -> RawCsi:
    header = header or parse_header(data)
    _check(header, PACKET_RAW_CSI, RAW_HEADER_LEN, len(data))
    seq, node, source, ts, rssi, noise, ch, ch2, sig, mcs, cwb, flags, csi_len = _RAW.unpack_from(data, 8)
    end = header.header_len + csi_len
    if len(data) < end:
        raise ProtocolError(f"CSI of {csi_len} bytes needs {end} bytes, got {len(data)}")
    return RawCsi(
        seq, format_mac(node), format_mac(source), ts, rssi, noise, ch, ch2, sig, mcs, cwb, flags,
        bytes(data[header.header_len:end]),
    )


def _check(header: Header, packet_type: int, min_header: int, size: int) -> None:
    if header.version != PROTOCOL_VERSION:
        raise ProtocolError(f"unknown protocol version {header.version}")
    if header.packet_type != packet_type:
        raise ProtocolError(f"packet type {header.packet_type}, expected {packet_type}")
    if header.header_len < min_header:
        raise ProtocolError(f"header length {header.header_len}, at least {min_header} expected")
    if size < header.header_len:
        raise ProtocolError(f"short packet: {size} bytes, header says {header.header_len}")


def _link(tx: bytes, kind: int, rssi: int, score: int, spread: int, frames: int, flags: int) -> Link:
    return Link(
        transmitter=format_mac(tx),
        kind=kind,
        rssi=None if rssi == RSSI_NO_FRAMES else rssi,
        score=None if score == SCORE_UNKNOWN else score / 100,
        spread=spread / 100,
        frames=frames,
        flags=flags,
    )
