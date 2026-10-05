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
    STREAM_HIVE_REPORTS,
    STREAM_LINK_REPORTS,
    STREAM_RAW_CSI,
    HiveEntry,
    HiveReport,
    HiveRow,
    HiveTracker,
    LayoutPoint,
    LinkReport,
    LinkTable,
    ProtocolError,
    RawCsi,
    access_points,
    build_subscribe,
    parse_header,
    parse_hive_report,
    parse_link_report,
    parse_packet,
    parse_raw_csi,
)

from .conftest import REAL_AP, REAL_HIVE, REAL_LINKS_1, REAL_LINKS_2, REAL_NODE_1, REAL_NODE_2  # noqa: E402
from .fake_node import FakeNode, encode_hive_report, encode_report  # noqa: E402

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


def hive(seq=3, node=NODE, hive_hash=0xCAFE0123, flags=1, layout=(), rows=(), header_len=26,
         points=None, row_count=None, extra=b""):
    """Type 3 hive report, field by field as in docs/PROTOCOL.md. layout: (mac, x cm, y cm);
    rows: (origin, version, [(neighbour, rssi), ...])."""
    out = bytearray(b"WISP") + bytes([1, 3]) + struct.pack("<H", header_len)
    out += struct.pack("<I", seq) + mac(node)  # 8 seq, 12 node
    out += struct.pack("<I", hive_hash)  # 18 hive hash
    out += bytes([flags, len(layout) if points is None else points])  # 22 flags, 23 layout points
    out += bytes([len(rows) if row_count is None else row_count, 0])  # 24 rows, 25 reserved
    out += extra
    for node_mac, x, y in layout:
        out += mac(node_mac) + struct.pack("<hh", x, y)
    for origin, version, entries in rows:
        out += mac(origin) + struct.pack("<H", version) + bytes([len(entries)])
        for neighbour, rssi in entries:
            out += mac(neighbour) + struct.pack("<b", rssi)
    return bytes(out)


LINKS = [
    (AP, KIND_AP, -55, 123, 210, 20, 0),
    (OTHER, KIND_NODE, -128, 0xFFFF, 0, 0, 0),  # no frames: RSSI and score unknown
    ("02:57:49:53:50:03", KIND_NODE, -70, 345, 1234, 255, 1),  # motion
]
LAYOUT = [(NODE, -150, 80), (OTHER, 150, -80)]
ROWS = [(NODE, 7, [(AP, -50), (OTHER, -61)]), (OTHER, 2, [(NODE, -60)])]


# Subscribe

def test_subscribe_packets():
    assert build_subscribe() == b"WSUB\x01\x02"
    assert build_subscribe(STREAM_LINK_REPORTS) == b"WSUB\x01\x02"
    assert build_subscribe(STREAM_RAW_CSI | STREAM_LINK_REPORTS) == b"WSUB\x01\x03"
    assert build_subscribe(STREAM_LINK_REPORTS | STREAM_HIVE_REPORTS) == b"WSUB\x01\x06"
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


def test_link_report_with_confirmation():
    """Node firmware 0.1.6 and later: report flag bit 0 says the links carry the confirmed bit (1),
    and the node pairs the hive confirms follow the links."""
    links = [(AP, KIND_AP, -55, 310, 900, 20, 3), (OTHER, KIND_NODE, -60, 290, 800, 10, 1)]
    r = parse_packet(report(links=links, flags=1) + bytes([1]) + mac(NODE) + mac(OTHER))
    assert r.confirms and not r.pairs_truncated and r.pairs == ((NODE, OTHER),)
    assert [(link.motion, link.confirmed) for link in r.links] == [(True, True), (True, False)]
    assert parse_packet(report(links=links, flags=1) + b"\x00").pairs == ()
    assert parse_packet(report(flags=3) + b"\x00").pairs_truncated
    old = parse_packet(report(links=LINKS) + b"\x01\x02")  # older firmware: trailing bytes mean nothing
    assert not old.confirms and old.pairs == () and not any(link.confirmed for link in old.links)
    with pytest.raises(ProtocolError):
        parse_link_report(report(links=links, flags=1))  # no pair count
    with pytest.raises(ProtocolError):
        parse_link_report(report(links=links, flags=1) + bytes([2]) + mac(NODE) + mac(OTHER))  # a pair short


def test_fake_node_encoder_matches_spec():
    assert encode_report(7, NODE, LINKS, uptime=42) == report(links=LINKS)
    assert encode_report(7, NODE, LINKS, uptime=42, pairs=[(NODE, OTHER)]) == (
        report(links=LINKS, flags=1) + bytes([1]) + mac(NODE) + mac(OTHER)
    )
    node = FakeNode()
    r = parse_packet(node.report(node.started + 1))
    assert r.node == NODE and len(r.links) == 3 and r.links[0].kind == KIND_AP


# Hive reports

def test_hive_report_golden_bytes():
    data = bytes.fromhex(
        "57495350" "01" "03" "1a00"  # WISP, v1, type 3, header 26
        "03000000" "025749535001" "2301feca"  # seq 3, node, hash 0xcafe0123
        "03" "01" "01" "00"  # flags in sync and truncated, 1 point, 1 row, reserved
        "025749535001" "6aff" "5000"  # point: node, x -150 cm, y 80 cm
        "025749535001" "0700" "01" "a82948dbb670" "ce"  # row: origin, version 7, 1 entry: AP at -50 dBm
    )
    r = parse_packet(data)
    assert isinstance(r, HiveReport)
    assert (r.seq, r.node, r.hash, r.in_sync, r.truncated) == (3, NODE, 0xCAFE0123, True, True)
    assert r.layout == (LayoutPoint(NODE, -1.5, 0.8),)
    assert r.rows == (HiveRow(NODE, 7, (HiveEntry(AP, -50),)),)


def test_hive_report_round_trip():
    r = parse_hive_report(hive(layout=LAYOUT, rows=ROWS, flags=0))
    assert (r.in_sync, r.truncated) == (False, False)
    assert [(p.node, p.x, p.y) for p in r.layout] == [(NODE, -1.5, 0.8), (OTHER, 1.5, -0.8)]
    assert [(row.origin, row.version, len(row.entries)) for row in r.rows] == [(NODE, 7, 2), (OTHER, 2, 1)]
    assert r.rows[0].entries[1] == HiveEntry(OTHER, -61)


def test_reports_from_real_nodes():
    r = parse_packet(REAL_HIVE)
    assert (r.seq, r.node, f"{r.hash:08x}", r.in_sync, r.truncated) == (4, REAL_NODE_1, "0bcd88ba", True, False)
    assert r.layout == (LayoutPoint(REAL_NODE_1, -0.2, 0.0), LayoutPoint(REAL_NODE_2, 0.2, 0.0))
    assert [(row.origin, row.version) for row in r.rows] == [(REAL_NODE_1, 2), (REAL_NODE_2, 2)]
    assert r.rows[0].entries == (HiveEntry(REAL_AP, -62), HiveEntry(REAL_NODE_2, -27))
    assert r.rows[1].entries == (HiveEntry(REAL_AP, -49), HiveEntry(REAL_NODE_1, -29))
    one, two = parse_packet(REAL_LINKS_1), parse_packet(REAL_LINKS_2)
    assert (one.node, one.uptime, [(x.transmitter, x.kind, x.rssi, x.score) for x in one.links]) == (
        REAL_NODE_1, 17, [(REAL_NODE_2, KIND_NODE, -27, 0.98)]
    )
    assert (two.node, two.uptime, [(x.transmitter, x.kind, x.rssi, x.score) for x in two.links]) == (
        REAL_NODE_2, 394, [(REAL_AP, KIND_AP, -49, 1.03), (REAL_NODE_1, KIND_NODE, -28, 1.1)]
    )


def test_hive_report_edges():
    assert parse_packet(hive()).layout == () and parse_packet(hive()).rows == ()
    r = parse_packet(hive(layout=LAYOUT, rows=[(NODE, 1, [])], header_len=30, extra=b"\x01\x02\x03\x04"))
    assert r.layout[0].x == -1.5 and r.rows[0].entries == ()
    assert len(parse_packet(hive(rows=ROWS) + b"\xff\xff").rows) == 2  # trailing bytes ignored
    far = parse_packet(hive(layout=[(NODE, -32767, 32767)])).layout[0]
    assert (far.x, far.y) == (-327.67, 327.67)


def test_fake_node_hive_encoder_matches_spec():
    assert encode_hive_report(3, NODE, 0xCAFE0123, LAYOUT, ROWS, flags=0) == hive(layout=LAYOUT, rows=ROWS, flags=0)
    r = parse_packet(FakeNode().hive_report())
    assert isinstance(r, HiveReport) and r.in_sync
    assert len(r.layout) == 3 and [row.entries[0].neighbour for row in r.rows] == [AP, AP, AP]


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
    with pytest.raises(ProtocolError):
        parse_hive_report(report(links=LINKS))


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
        hive(layout=LAYOUT, points=3),  # says 3 layout points, carries 2
        hive(rows=ROWS, row_count=3),  # says 3 rows, carries 2
        hive(rows=ROWS)[:-1],  # last entry cut short
        hive(layout=LAYOUT, header_len=24),  # header shorter than the type 3 fields
        hive(header_len=60),  # header longer than the packet
    ],
)
def test_malformed_packets_raise_protocol_error(data):
    with pytest.raises(ProtocolError):
        parse_packet(data)


def test_every_truncation_is_rejected():
    for full in (report(links=LINKS), raw_csi(), hive(layout=LAYOUT, rows=ROWS), REAL_HIVE):
        for n in range(len(full)):
            with pytest.raises(ProtocolError):
                parse_packet(full[:n])


def test_random_bytes_never_crash():
    rng = random.Random(1)
    for prefix in (b"WISP\x01", b"WISP\x01\x03\x1a\x00"):
        for _ in range(3000):
            data = prefix + bytes(rng.randrange(256) for _ in range(rng.randrange(80)))
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


def test_table_keeps_confirmation():
    table = LinkTable(timeout=10)
    confirmed = [(OTHER, KIND_NODE, -60, 300, 800, 10, 3)]
    table.apply(parse_link_report(report(seq=1, links=confirmed, flags=1) + b"\x00"), now=100.0)
    link = table.links[(OTHER, NODE)]
    assert link.motion and link.confirmed and link.confirmed_at == 100.0 and table.nodes[NODE].confirms
    moving = [(OTHER, KIND_NODE, -60, 300, 800, 10, 1)]
    table.apply(parse_link_report(report(seq=2, links=moving, flags=1) + b"\x00"), now=100.2)
    assert not link.confirmed and link.confirmed_at == 100.0  # when it last was
    table.apply(parse_link_report(report(seq=3, links=moving)), now=100.4)  # older firmware again
    assert not table.nodes[NODE].confirms


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


# Hive tracker

def h(seq=1, node=NODE, flags=1, layout=LAYOUT, rows=ROWS, hive_hash=0xCAFE0123):
    return parse_hive_report(hive(seq=seq, node=node, flags=flags, layout=layout, rows=rows, hive_hash=hive_hash))


def test_hive_tracker_keeps_the_latest_report_per_node():
    tracker = HiveTracker(timeout=15)
    assert tracker.current(0.0) is None
    assert tracker.apply(h(1), now=0.0)
    assert not tracker.apply(h(1), now=0.5)  # duplicate
    assert tracker.apply(h(2), now=5.0)
    state = tracker.current(5.0)
    assert (state.reporter, state.seq, state.hash, state.in_sync, state.updated) == (NODE, 2, 0xCAFE0123, True, 5.0)
    assert state.layout == {NODE: (-1.5, 0.8), OTHER: (1.5, -0.8)}
    assert set(state.rows) == {NODE, OTHER} and state.rows[NODE].version == 7
    assert state.nodes == {NODE, OTHER}
    assert tracker.apply(h(1), now=6.0)  # rebooted: sequence starts again
    assert tracker.fresh(NODE, 20.0) and not tracker.fresh(NODE, 21.5) and not tracker.fresh(OTHER, 6.0)


def test_hive_tracker_adds_up_truncated_reports():
    """A hive too large for one report: each carries other rows; same hash, they add up."""
    tracker = HiveTracker(timeout=15)
    first, second = ROWS[:1], ROWS[1:]
    tracker.apply(h(1, flags=0x03, rows=first), now=0.0)  # in sync, rows truncated
    tracker.apply(h(2, flags=0x03, rows=second), now=5.0)
    assert set(tracker.current(5.0).rows) == {NODE, OTHER}
    tracker.apply(h(3, flags=0x03, rows=first, hive_hash=0xBEEF), now=10.0)  # the hive changed
    assert set(tracker.current(10.0).rows) == {first[0][0]}
    tracker.apply(h(4, flags=0x01, rows=first, hive_hash=0xBEEF), now=15.0)  # whole again
    assert set(tracker.current(15.0).rows) == {first[0][0]}


def test_hive_tracker_prefers_in_sync_and_fuller_views():
    tracker = HiveTracker(timeout=15)
    tracker.apply(h(1, node=NODE, layout=LAYOUT), now=0.0)
    tracker.apply(h(1, node=OTHER, flags=0, layout=LAYOUT + [("02:57:49:53:50:03", 0, 200)]), now=1.0)
    assert tracker.current(1.0).reporter == NODE  # in sync beats a bigger layout still syncing
    tracker.apply(h(2, node=OTHER, layout=LAYOUT + [("02:57:49:53:50:03", 0, 200)]), now=2.0)
    assert tracker.current(2.0).reporter == OTHER  # both in sync: the fuller layout
    tracker.apply(h(2, node=NODE, layout=LAYOUT), now=3.0)
    assert tracker.current(3.0).reporter == OTHER
    assert tracker.current(17.5).reporter == NODE  # OTHER went quiet
    assert tracker.current(100.0).reporter == NODE  # all quiet: the last one heard
    tracker.forget(NODE)
    assert tracker.current(100.0).reporter == OTHER


def test_access_points_take_signal_from_hive_rows():
    table = LinkTable(timeout=10)
    table.apply(parse_link_report(report(seq=1, node=NODE, links=LINKS)), now=0.0)
    table.apply(parse_link_report(report(seq=1, node=OTHER, links=[(AP, KIND_AP, -128, 100, 0, 0, 0)])), now=0.0)
    assert access_points(table, None, {NODE, OTHER}) == {AP: [(NODE, -55)]}  # no frames, no signal
    tracker = HiveTracker(timeout=15)
    tracker.apply(h(1, rows=[(NODE, 1, [(AP, -50), (OTHER, -60)]), (OTHER, 1, [(AP, -45)])]), now=0.0)
    assert access_points(table, tracker.current(0.0), {NODE, OTHER}) == {AP: [(OTHER, -45), (NODE, -50)]}
    assert access_points(table, tracker.current(0.0), {OTHER}) == {AP: [(OTHER, -45)]}
