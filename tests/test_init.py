"""Hub, UDP path and entities, with a fake socket and a clock moved by hand."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import json
from pathlib import Path
import socket
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from homeassistant.config_entries import ConfigEntryState, ConfigSubentry, ConfigSubentryData
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.util import dt as dt_util

from custom_components.wisp.const import DOMAIN, VERSION
from custom_components.wisp.diagnostics import async_get_config_entry_diagnostics
from custom_components.wisp.hub import PER_ENTRY_DEVICES, ProbeError, async_probe, node_devices

from .conftest import AP, IP_A, IP_B, NODE_A, NODE_B, FakeClock, FakeUdp
from .fake_node import FakeNode, encode_hive_report, encode_report

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

SUBSCRIBE = b"WSUB\x01\x06"  # link and hive reports
NODE_C = "02:57:49:53:50:03"  # a node Home Assistant does not know
HALL = (NODE_A, IP_A, "Hall")
OFFICE = (NODE_B, IP_B, "Office")
SCORE = "sensor.hall_ap_a8_29_48_db_b6_70_motion_score"
SIGNAL = "sensor.hall_ap_a8_29_48_db_b6_70_signal"
SPREAD = "sensor.hall_ap_a8_29_48_db_b6_70_spread"
MOTION = "binary_sensor.hall_ap_a8_29_48_db_b6_70_motion"
NODES_ONLINE = "sensor.wisp_nodes_online"
HIVE_IN_SYNC = "binary_sensor.wisp_hive_in_sync"


def link_ids(hass: HomeAssistant, domain: str | None = None) -> list[str]:
    """Entity ids without the hub's own two, which are always there."""
    ids = hass.states.async_entity_ids(domain) if domain else hass.states.async_entity_ids()
    return [e for e in ids if e not in (NODES_ONLINE, HIVE_IN_SYNC)]


def node_subentry(mac: str, host: str, name: str) -> ConfigSubentryData:
    return ConfigSubentryData(
        data={"mac": mac, "host": host, "name": name}, subentry_type="node", title=name, unique_id=mac
    )


async def setup_hub(hass: HomeAssistant, *nodes: tuple[str, str, str]) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Wisp", unique_id=DOMAIN, data={}, subentries_data=[node_subentry(*n) for n in nodes]
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def links(score: int = 123, motion: int = 0, rssi: int = -55, spread: int = 210) -> list[tuple]:
    return [
        (AP, 0, rssi, score, spread, 20, motion),
        (NODE_B, 1, -60, 100, 150, 10, 0),
        (NODE_C, 1, -128, 0xFFFF, 0, 0, 0),
    ]


class Feed:
    """Sends link reports from node A, one sequence number after the other."""

    def __init__(self, hass: HomeAssistant, udp: FakeUdp, clock: FakeClock) -> None:
        self.hass, self.udp, self.clock, self.seq = hass, udp, clock, 0

    async def __call__(self, at: float | None = None, **kwargs) -> None:
        if at is not None:
            self.clock.now = 1000.0 + at
        self.seq += 1
        self.udp.receive(encode_report(self.seq, NODE_A, links(**kwargs), uptime=60))
        await self.hass.async_block_till_done()


@pytest.fixture
async def feed(hass: HomeAssistant, udp: FakeUdp) -> Feed:
    entry = await setup_hub(hass, HALL, OFFICE)
    clock = FakeClock()
    entry.runtime_data.clock = clock
    return Feed(hass, udp, clock)


def state(hass: HomeAssistant, entity_id: str) -> str | None:
    s = hass.states.get(entity_id)
    return s.state if s else None


async def fire(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


def test_versions_and_translations_in_sync():
    root = Path(__file__).parents[1] / "custom_components" / "wisp"
    assert json.loads((root / "manifest.json").read_text())["version"] == VERSION
    assert (root / "strings.json").read_text() == (root / "translations" / "en.json").read_text()


async def test_subscribes_to_every_node_every_3_seconds(hass: HomeAssistant, udp: FakeUdp) -> None:
    entry = await setup_hub(hass, HALL, OFFICE)
    assert entry.state is ConfigEntryState.LOADED
    assert udp.transport.sent == [(SUBSCRIBE, (IP_A, 47010)), (SUBSCRIBE, (IP_B, 47010))]
    udp.clear()
    await fire(hass, 3)
    assert sorted(udp.sent_to()) == [(IP_A, 47010), (IP_B, 47010)]


async def test_link_entities_appear_on_first_report(hass: HomeAssistant, feed: Feed) -> None:
    assert link_ids(hass, "sensor") == []
    await feed()
    # Motion score per link; Signal and Spread exist but start disabled (recorder load)
    assert len(link_ids(hass, "sensor")) == 3
    assert len(link_ids(hass, "binary_sensor")) == 3
    registry = er.async_get(hass)
    for entity_id in (SIGNAL, SPREAD):
        entry = registry.async_get(entity_id)
        assert entry is not None and entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION

    assert state(hass, SCORE) == "1.23"
    assert hass.states.get(SCORE).attributes["state_class"] == "measurement"
    assert state(hass, MOTION) == "off"
    assert hass.states.get(MOTION).attributes["device_class"] == "motion"
    assert hass.states.get(SCORE).attributes["friendly_name"] == "Hall AP a8:29:48:db:b6:70 motion score"
    # A node link is named after the other node; an unknown node by its MAC
    assert state(hass, "sensor.hall_office_motion_score") == "1.0"
    assert state(hass, "sensor.hall_node_02_57_49_53_50_03_motion_score") == STATE_UNKNOWN

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    sub_a = next(s.subentry_id for s in entry.subentries.values() if s.unique_id == NODE_A)
    reg = er.async_get(hass).async_get(SCORE)
    assert reg.unique_id == "025749535001_a82948dbb670_motion_score"
    assert reg.config_subentry_id == sub_a
    assert er.async_get(hass).async_get(SIGNAL).entity_category == "diagnostic"
    device = dr.async_get(hass).async_get(reg.device_id)
    assert (CONNECTION_NETWORK_MAC, NODE_A) in device.connections
    assert (device.name, device.manufacturer, device.model) == ("Hall", "albert-canfield", "wisp-node")
    if PER_ENTRY_DEVICES:  # Home Assistant 2026.9+: one entry per device
        assert (device.config_entry_id, device.config_subentry_id) == (entry.entry_id, sub_a)
    else:
        assert device.config_entries_subentries[entry.entry_id] == {sub_a}


async def test_state_writes_are_rate_limited(hass: HomeAssistant, feed: Feed) -> None:
    await feed(at=0.0, score=123)
    events = async_capture_events(hass, EVENT_STATE_CHANGED)

    def writes(entity_id: str) -> int:
        return sum(e.data["entity_id"] == entity_id for e in events)

    await feed(at=0.2, score=150)
    await feed(at=0.4, score=160)
    assert state(hass, SCORE) == "1.23" and writes(SCORE) == 0
    # The last value lands once the second is up, even without another report
    feed.clock.now = 1001.0
    await fire(hass, 1)
    assert state(hass, SCORE) == "1.6" and writes(SCORE) == 1
    await feed(at=1.3, score=200)  # 0.4 up: worth a write, once the second is up
    assert state(hass, SCORE) == "1.6"
    await feed(at=2.1, score=200)
    assert state(hass, SCORE) == "2.0" and writes(SCORE) == 2
    # A small change waits up to a minute, so the recorder stays light
    await feed(at=3.2, score=210)
    await feed(at=30.0, score=210)
    assert state(hass, SCORE) == "2.0" and writes(SCORE) == 2
    await feed(at=62.4, score=210)
    assert state(hass, SCORE) == "2.1" and writes(SCORE) == 3
    # Motion is written at once, on and off
    await feed(at=62.5, score=210, motion=1)
    assert state(hass, MOTION) == "on"
    await feed(at=62.6, score=210, motion=0)
    assert state(hass, MOTION) == "off" and writes(MOTION) == 2
    # Same values: nothing written
    await feed(at=65.0, score=210)
    await feed(at=67.0, score=210)
    assert writes(SCORE) == 3


async def test_links_go_unavailable_after_10_seconds(hass: HomeAssistant, feed: Feed) -> None:
    await feed(at=0.0)
    feed.clock.now = 1009.5
    await fire(hass, 1)
    assert state(hass, SCORE) == "1.23"
    feed.clock.now = 1010.5
    await fire(hass, 1)
    for entity_id in (SCORE, MOTION, "sensor.hall_office_motion_score"):
        assert state(hass, entity_id) == STATE_UNAVAILABLE
    await feed(at=11.0, score=140)
    assert state(hass, SCORE) == "1.4"
    assert state(hass, MOTION) == "off"


async def test_device_matches_the_esphome_device(hass: HomeAssistant, udp: FakeUdp) -> None:
    esphome = MockConfigEntry(domain="esphome", unique_id=NODE_A)
    esphome.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=esphome.entry_id,
        connections={(CONNECTION_NETWORK_MAC, NODE_A.upper())},
        name="Wisp 535001",
        manufacturer="albert-canfield",
        model="wisp-node",
    )
    entry = await setup_hub(hass, HALL)
    udp.receive(encode_report(1, NODE_A, links()))
    await hass.async_block_till_done()
    devices = {d.id: d for d in node_devices(hass, NODE_A)}
    assert dev_reg.async_get(device.id).name == "Wisp 535001"  # ESPHome's name is kept
    if PER_ENTRY_DEVICES:
        # Home Assistant 2026.9+: Wisp's own device, linked to ESPHome's by the MAC
        assert len(devices) == 2
        (ours,) = [d for d in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
                   if (DOMAIN, entry.entry_id) not in d.identifiers]  # the hub has its own device
        assert ours.id in devices and ours.name == "Hall"
        reg = er.async_get(hass).async_get("sensor.hall_ap_a8_29_48_db_b6_70_motion_score")
        assert reg.device_id == ours.id
    else:
        # One device shared with ESPHome
        assert list(devices) == [device.id]
        assert dev_reg.async_get(device.id).config_entries == {esphome.entry_id, entry.entry_id}
        reg = er.async_get(hass).async_get("sensor.wisp_535001_ap_a8_29_48_db_b6_70_motion_score")
        assert reg.device_id == device.id


async def test_ignores_unknown_nodes_and_bad_packets(hass: HomeAssistant, udp: FakeUdp) -> None:
    entry = await setup_hub(hass, HALL)
    udp.receive(encode_report(1, NODE_C, links()), "192.168.1.60")
    udp.receive(b"\x00garbage")
    udp.receive(encode_report(1, NODE_A, links())[:30])  # truncated
    udp.receive(b"WISP\x01\x01\x26\x00" + bytes(30))  # raw CSI, no CSI bytes
    udp.receive(b"WISP\x02\x02\x18\x00" + bytes(16))  # newer protocol version
    await hass.async_block_till_done()
    assert link_ids(hass) == []
    stats = entry.runtime_data.stats
    assert (stats["unknown_node"], stats["invalid"], stats["ignored"], stats["reports"]) == (1, 2, 2, 0)


async def test_nodes_added_and_removed(hass: HomeAssistant, udp: FakeUdp) -> None:
    entry = await setup_hub(hass, HALL)
    hub = entry.runtime_data
    udp.clear()
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=MappingProxyType({"mac": NODE_B, "host": IP_B, "name": "Office"}),
            subentry_type="node",
            title="Office",
            unique_id=NODE_B,
        ),
    )
    await hass.async_block_till_done()
    assert (IP_B, 47010) in udp.sent_to()  # subscribed at once, no reload
    assert hub is entry.runtime_data
    assert [d.name for d in node_devices(hass, NODE_B)] == ["Office"]

    udp.receive(encode_report(1, NODE_A, links()))
    udp.receive(encode_report(1, NODE_B, [(AP, 0, -50, 110, 100, 20, 0)]), IP_B)
    await hass.async_block_till_done()
    assert state(hass, "sensor.office_ap_a8_29_48_db_b6_70_motion_score") == "1.1"

    sub_a = next(s.subentry_id for s in entry.subentries.values() if s.unique_id == NODE_A)
    hass.config_entries.async_remove_subentry(entry, sub_a)
    await hass.async_block_till_done()
    assert state(hass, SCORE) is None
    assert er.async_get(hass).async_get(SCORE) is None
    assert list(hub.nodes) == [NODE_B]
    udp.clear()
    await fire(hass, 3)
    assert udp.sent_to() == [(IP_B, 47010)]
    udp.receive(encode_report(2, NODE_A, links()))
    assert hub.stats["unknown_node"] == 1


async def test_host_names_are_resolved(hass: HomeAssistant, udp: FakeUdp) -> None:
    lookup = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("192.168.1.77", 47010))])
    with patch.object(hass.loop, "getaddrinfo", lookup):
        await setup_hub(hass, (NODE_A, "wisp-535001.local", "Hall"))
    assert udp.sent_to() == [("192.168.1.77", 47010)]
    assert lookup.await_args.args[0] == "wisp-535001.local"


async def test_unresolvable_host_falls_back_to_report_address(hass: HomeAssistant, udp: FakeUdp) -> None:
    with patch.object(hass.loop, "getaddrinfo", AsyncMock(side_effect=socket.gaierror("no"))):
        await setup_hub(hass, (NODE_A, "wisp-535001.local", "Hall"))
        assert udp.sent_to() == []
        udp.receive(encode_report(1, NODE_A, links()), "192.168.1.78")
        await fire(hass, 3)
    assert udp.sent_to() == [("192.168.1.78", 47010)]


async def test_diagnostics(hass: HomeAssistant, udp: FakeUdp) -> None:
    entry = await setup_hub(hass, HALL)
    udp.receive(encode_report(4, NODE_A, links(motion=1), uptime=60))
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["hub"]["running"] and diag["hub"]["port"] == 40000
    assert diag["hub"]["stats"]["reports"] == 1
    (node,) = diag["nodes"]
    assert (node["mac"], node["host"], node["name"]) == (NODE_A, IP_A, "Hall")
    assert node["seen"]["address"] == IP_A and node["seen"]["uptime"] == 60
    assert node["last_report"]["seq"] == 4
    assert node["last_report"]["links"][0] == {
        "transmitter": AP, "kind": 0, "rssi": -55, "score": 1.23, "spread": 2.1, "frames": 20, "flags": 1
    }
    assert len(diag["links"]) == 3 and diag["links"][0]["motion"] is True
    assert diag["hive"] is None
    json.dumps(diag)


async def test_hub_device_and_health(hass: HomeAssistant, udp: FakeUdp) -> None:
    """The hub has its own device from the start: Open leads to the Wisp panel, and two sensors
    tell how the grid is doing."""
    entry = await setup_hub(hass, HALL, OFFICE)
    hub = entry.runtime_data
    hub.clock = FakeClock()
    (device,) = [d for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
                 if (DOMAIN, entry.entry_id) in d.identifiers]
    assert device is not None and device.name == "Wisp" and device.configuration_url == "homeassistant://wisp"
    registry = er.async_get(hass)
    assert {registry.async_get(e).device_id for e in (NODES_ONLINE, HIVE_IN_SYNC)} == {device.id}
    assert registry.async_get(HIVE_IN_SYNC).entity_category == "diagnostic"
    online = hass.states.get(NODES_ONLINE)
    assert online.state == "0" and online.attributes["nodes"] == 2 and online.attributes["offline"] == ["Hall", "Office"]
    assert state(hass, HIVE_IN_SYNC) == STATE_UNKNOWN

    layout = [(NODE_A, -100, 0), (NODE_B, 100, 0)]
    rows = [(NODE_A, 1, [(AP, -50), (NODE_B, -60)]), (NODE_B, 1, [(AP, -55), (NODE_A, -61)])]
    udp.receive(encode_report(1, NODE_A, links(), uptime=60))
    udp.receive(encode_hive_report(1, NODE_A, 0x1234, layout, rows))
    await fire(hass, 1)
    online = hass.states.get(NODES_ONLINE)
    assert online.state == "1" and online.attributes["offline"] == ["Office"]
    hive = hass.states.get(HIVE_IN_SYNC)
    assert hive.state == "on" and hive.attributes["nodes"] == 2

    udp.receive(encode_hive_report(2, NODE_A, 0x1234, layout, rows, flags=0))  # still syncing
    await fire(hass, 1)
    assert state(hass, HIVE_IN_SYNC) == "off"
    hub.clock.now += 60  # nothing heard for a minute
    await fire(hass, 2)
    assert state(hass, NODES_ONLINE) == "0" and state(hass, HIVE_IN_SYNC) == STATE_UNKNOWN


async def test_hive_reports(hass: HomeAssistant, udp: FakeUdp) -> None:
    entry = await setup_hub(hass, HALL, OFFICE)
    hub = entry.runtime_data
    layout = [(NODE_A, -100, 0), (NODE_B, 100, 0)]
    rows = [(NODE_A, 1, [(AP, -50), (NODE_B, -60)]), (NODE_B, 1, [(AP, -55), (NODE_A, -61)])]
    udp.receive(encode_hive_report(1, NODE_C, 0x1234, layout, rows), "192.168.1.60")
    udp.receive(encode_hive_report(1, NODE_A, 0x1234, layout, rows))
    udp.receive(encode_hive_report(1, NODE_A, 0x1234, layout, rows))
    await hass.async_block_till_done()
    assert (hub.stats["hive_reports"], hub.stats["duplicates"], hub.stats["unknown_node"]) == (1, 1, 1)
    assert link_ids(hass) == []  # the hive makes no entities of its own
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert (diag["hive"]["reporter"], diag["hive"]["hash"], diag["hive"]["in_sync"]) == (NODE_A, "00001234", True)
    assert diag["hive"]["layout"] == {NODE_A: [-1.0, 0.0], NODE_B: [1.0, 0.0]}
    assert diag["hive"]["rows"][0]["entries"][0] == {"neighbour": AP, "rssi": -50}
    json.dumps(diag)
    # A removed node's report goes with it
    sub_a = next(s.subentry_id for s in entry.subentries.values() if s.unique_id == NODE_A)
    hass.config_entries.async_remove_subentry(entry, sub_a)
    await hass.async_block_till_done()
    assert hub.hive.current(hub.clock()) is None


async def test_unload_closes_the_socket(hass: HomeAssistant, udp: FakeUdp) -> None:
    entry = await setup_hub(hass, HALL)
    udp.receive(encode_report(1, NODE_A, links()))
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert udp.transport.closed
    assert state(hass, SCORE) == STATE_UNAVAILABLE


async def test_real_udp_with_fake_node(hass: HomeAssistant, socket_enabled: None) -> None:
    """End to end over localhost: subscribe, receive, entities; and the probe used by Add node."""
    node = FakeNode(NODE_A, interval=0.05)
    port = await node.start("127.0.0.1", 0)
    try:
        with (
            patch("custom_components.wisp.hub.NODE_PORT", port),
            patch("custom_components.wisp.hub.LISTEN", ("127.0.0.1", 0)),
            patch("custom_components.wisp.hub.PROBE_TRIES", 1),
        ):
            assert await async_probe(hass, "127.0.0.1") == NODE_A
            entry = await setup_hub(hass, (NODE_A, "127.0.0.1", "Hall"))
            for _ in range(40):
                await asyncio.sleep(0.05)
                if hass.states.get(SCORE) and entry.runtime_data.stats["hive_reports"]:
                    break
            await hass.async_block_till_done()
            assert float(state(hass, SCORE)) > 0
            assert len(link_ids(hass, "binary_sensor")) == 3
            assert entry.runtime_data.stats["reports"] > 0
            assert entry.runtime_data.stats["hive_reports"] == 1  # every 5 s

            assert await hass.config_entries.async_unload(entry.entry_id)

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as silent:  # bound, never answers
            silent.bind(("127.0.0.1", 0))
            with (
                patch("custom_components.wisp.hub.NODE_PORT", silent.getsockname()[1]),
                patch("custom_components.wisp.hub.LISTEN", ("127.0.0.1", 0)),
                patch("custom_components.wisp.hub.PROBE_TRIES", 1),
                pytest.raises(ProbeError, match="no_answer"),
            ):
                await async_probe(hass, "127.0.0.1")
    finally:
        node.close()
