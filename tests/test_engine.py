"""Engine: protocol and link state. Plain pytest, no Home Assistant needed."""
import random
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "wisp"))

from engine import (  # noqa: E402
    KIND_AP,
    KIND_NODE,
    STREAM_LINK_REPORTS,
    STREAM_RAW_CSI,
    LinkReport,
    LinkTable,
    ProtocolError,
    RawCsi,
    build_subscribe,
    parse_header,
    parse_link_report,
    parse_packet,
    parse_raw_csi,
)

from .fake_node import FakeNode, encode_report  # noqa: E402

NODE = "02:57:49:53:50:01"
OTHER = "02:57:49:53:50:02"
AP = "a8:29:48:db:b6:70"


def mac(text: str) -> bytes:
    return bytes.fromhex(text.replace(":", ""))


def report(seq=7, node=NODE, links=(), uptime=42, flags=0, version=1, ptype=2, header_len=24, count=None, extra=b""):
    """Type 2 link report, field by field as in docs/PROTOCOL.md."""
    out = bytearray(b"WISP")  # 0 magic
    out += bytes([version, ptype])  # 4 version, 5 packet type
    out += struct.pack("<H", header_len)  # 6 header length
    out += struct.pack("<I", seq)  # 8 sequence number
    out += mac(node)  # 12 node MAC
    out += bytes([len(links) if count is None else count, flags])  # 18 number of links, 19 flags
    out += struct.pack("<I", uptime)  # 20 uptime
    out += extra  # fields a newer node may add before the payload
    for tx, kind, rssi, score, spread, frames, link_flags in links:
        out += mac(tx) + bytes([kind])  # 0 transmitter, 6 kind
        out += struct.pack("<b", rssi)  # 7 RSSI
        out += struct.pack("<HH", score, spread)  # 8 score x100, 10 spread x100
        out += bytes([frames, link_flags])  # 12 frames, 13 flags
    return bytes(out)


def raw_csi(csi=bytes(range(128)), header_len=38, csi_len=None, extra=b""):
    """Type 1 raw CSI, field by field as in docs/PROTOCOL.md."""
    out = bytearray(b"WISP") + bytes([1, 1]) + struct.pack("<H", header_len)
    out += struct.pack("<I", 99) + mac(NODE) + mac(AP)  # 8 seq, 12 node, 18 source
    out += struct.pack("<I", 123456)  # 24 timestamp
    out += struct.pack("<bb", -61, -95)  # 28 RSSI, 29 noise floor
    out += bytes([6, 1, 1, 7, 0, 0b101])  # 30 channel .. 35 flags
    out += struct.pack("<H", len(csi) if csi_len is None else csi_len)  # 36 CSI length
    return bytes(out) + extra + csi


LINKS = [
    (AP, KIND_AP, -55, 123, 210, 20, 0),
    (OTHER, KIND_NODE, -128, 0xFFFF, 0, 0, 0),  # no frames: RSSI and score unknown
    ("02:57:49:53:50:03", KIND_NODE, -70, 345, 1234, 255, 1),  # motion
]


# Subscribe

def test_subscribe_packets():
    assert build_subscribe() == b"WSUB\x01\x02"
    assert build_subscribe(STREAM_LINK_REPORTS) == b"WSUB\x01\x02"
    assert build_subscribe(STREAM_RAW_CSI | STREAM_LINK_REPORTS) == b"WSUB\x01\x03"
    with pytest.raises(ValueError):
        build_subscribe(256)


# Link reports

def test_link_report_golden_bytes():
    data = bytes.fromhex(
        "57495350" "01" "02" "1800"  # WISP, v1, type 2, header 24
        "07000000" "025749535001" "01" "00" "2a000000"  # seq 7, node, 1 link, flags, uptime 42
        "a82948dbb670" "00" "c9" "7b00" "d200" "14" "01"  # AP, kind 0, -55 dBm, 1.23, 2.10 %, 20 frames, motion
    )
    r = parse_packet(data)
    assert isinstance(r, LinkReport)
    assert (r.seq, r.node, r.flags, r.uptime) == (7, NODE, 0, 42)
    (link,) = r.links
    assert (link.transmitter, link.kind, link.rssi, link.score, link.spread, link.frames) == (AP, 0, -55, 1.23, 2.1, 20)
    assert link.motion


def test_link_report_round_trip():
    r = parse_link_report(report(links=LINKS))
    assert [link.transmitter for link in r.links] == [AP, OTHER, "02:57:49:53:50:03"]
    ap, quiet, moving = r.links
    assert (ap.kind, ap.rssi, ap.score, ap.spread, ap.motion) == (KIND_AP, -55, 1.23, 2.1, False)
    assert (quiet.rssi, quiet.score, quiet.frames) == (None, None, 0)
    assert (moving.score, moving.spread, moving.frames, moving.motion) == (3.45, 12.34, 255, True)


def test_link_report_without_links():
    assert parse_packet(report(links=())).links == ()


def test_header_length_skips_unknown_fields():
    r = parse_packet(report(links=LINKS[:1], header_len=28, extra=b"\xaa\xbb\xcc\xdd"))
    assert r.links[0].transmitter == AP and r.links[0].score == 1.23


def test_trailing_bytes_are_ignored():
    assert len(parse_packet(report(links=LINKS) + b"\x00\x01").links) == 3


def test_fake_node_encoder_matches_spec():
    assert encode_report(7, NODE, LINKS, uptime=42) == report(links=LINKS)
    node = FakeNode()
    r = parse_packet(node.report(node.started + 1))
    assert r.node == NODE and len(r.links) == 3 and r.links[0].kind == KIND_AP


# Raw CSI

def test_raw_csi_round_trip():
    r = parse_packet(raw_csi())
    assert isinstance(r, RawCsi)
    assert (r.seq, r.node, r.source, r.timestamp_us) == (99, NODE, AP, 123456)
    assert (r.rssi, r.noise_floor, r.channel, r.secondary_channel) == (-61, -95, 6, 1)
    assert (r.sig_mode, r.mcs, r.bandwidth, r.flags) == (1, 7, 0, 0b101)
    assert r.csi == bytes(range(128))


def test_raw_csi_header_length_skips_unknown_fields():
    assert parse_raw_csi(raw_csi(header_len=40, extra=b"\x00\x00")).csi == bytes(range(128))


# Unknown and malformed

def test_unknown_version_and_type_are_ignored():
    assert parse_packet(report(links=LINKS, version=2)) is None
    assert parse_packet(report(links=LINKS, ptype=9)) is None
    assert parse_header(report(version=2)).version == 2
    with pytest.raises(ProtocolError):
        parse_link_report(report(links=LINKS, version=2))
    with pytest.raises(ProtocolError):
        parse_link_report(raw_csi())
    with pytest.raises(ProtocolError):
        parse_raw_csi(report(links=LINKS))


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"WISP\x01\x02\x18",  # shorter than the common header
        b"WSUB\x01\x02",  # a subscription, not a node packet
        b"XXXX" + report(links=LINKS)[4:],  # wrong magic
        report(links=LINKS, count=4),  # says 4 links, carries 3
        report(links=LINKS, header_len=20),  # header shorter than the type 2 fields
        report(links=(), header_len=200),  # header longer than the packet
        raw_csi(csi_len=200),  # says 200 CSI bytes, carries 128
        raw_csi(header_len=30),
    ],
)
def test_malformed_packets_raise_protocol_error(data):
    with pytest.raises(ProtocolError):
        parse_packet(data)


def test_every_truncation_is_rejected():
    for full in (report(links=LINKS), raw_csi()):
        for n in range(len(full)):
            with pytest.raises(ProtocolError):
                parse_packet(full[:n])


def test_random_bytes_never_crash():
    rng = random.Random(1)
    for _ in range(3000):
        data = b"WISP\x01" + bytes(rng.randrange(256) for _ in range(rng.randrange(60)))
        try:
            parse_packet(data)
        except ProtocolError:
            pass


# Link table

def r(seq, links=LINKS, uptime=100, node=NODE):
    return parse_link_report(report(seq=seq, node=node, links=links, uptime=uptime))


def test_table_tracks_new_and_updated_links():
    table = LinkTable(timeout=10)
    keys, new = table.apply(r(1), now=0.0, address="192.168.1.50")
    assert new == keys == [(AP, NODE), (OTHER, NODE), ("02:57:49:53:50:03", NODE)]
    keys, new = table.apply(r(2, LINKS[:1]), now=0.2)
    assert keys == [(AP, NODE)] and new == []
    link = table.links[(AP, NODE)]
    assert (link.score, link.rssi, link.motion, link.updated, link.fresh) == (1.23, -55, False, 0.2, True)
    node = table.nodes[NODE]
    assert (node.seq, node.reports, node.address, node.last_report.seq) == (2, 2, "192.168.1.50", 2)


def test_table_drops_duplicates_and_counts_gaps():
    table = LinkTable(timeout=10)
    table.apply(r(5), now=0.0)
    assert table.apply(r(5), now=0.1) is None
    assert table.apply(r(4), now=0.2) is None
    assert table.apply(r(9), now=0.3) is not None
    assert table.nodes[NODE].lost == 3


def test_table_accepts_restart():
    table = LinkTable(timeout=10)
    table.apply(r(500, uptime=100), now=0.0)
    assert table.apply(r(1, uptime=0), now=0.2) is not None  # rebooted: uptime went down
    table.apply(r(900, uptime=200), now=1.0)
    # Quiet for longer than the timeout: a rebooted node may already show a higher uptime
    assert table.apply(r(3, uptime=300), now=20.0) is not None


def test_table_expires_quiet_links():
    table = LinkTable(timeout=10)
    table.apply(r(1), now=0.0)
    table.apply(r(2, LINKS[:1]), now=5.0)
    assert table.expire(10.0) == []
    assert sorted(table.expire(10.5)) == [(OTHER, NODE), ("02:57:49:53:50:03", NODE)]
    assert table.expire(11.0) == []  # reported once
    assert table.expire(15.5) == [(AP, NODE)]
    table.apply(r(3), now=16.0)
    assert all(link.fresh for link in table.links.values())


def test_table_forgets_a_node():
    table = LinkTable(timeout=10)
    table.apply(r(1), now=0.0)
    table.apply(r(1, [(NODE, KIND_NODE, -60, 100, 100, 10, 0)], node=OTHER), now=0.0)
    assert len(table.forget(NODE)) == 3
    assert list(table.links) == [(NODE, OTHER)] and list(table.nodes) == [OTHER]
