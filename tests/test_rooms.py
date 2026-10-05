"""Room presence in Home Assistant: floors from node areas, calibration services, storage, entities."""
from __future__ import annotations

import json
import logging
from types import MappingProxyType

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from homeassistant.config_entries import ConfigSubentry, ConfigSubentryData
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers import floor_registry as fr, issue_registry as ir

from custom_components.wisp.const import DOMAIN
from custom_components.wisp.diagnostics import async_get_config_entry_diagnostics

from .conftest import AP, IP_A, IP_B, NODE_A, NODE_B, FakeClock, FakeUdp
from .fake_node import encode_hive_report, encode_report
from .test_init import HALL, OFFICE, fire, link_ids, setup_hub, state

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

ROOM = "sensor.wisp_room"  # the hub's own floor: nodes without a floor
CALIBRATION = "sensor.wisp_calibration"
KITCHEN = "binary_sensor.wisp_kitchen_presence"
OFFICE_PRESENCE = "binary_sensor.wisp_office_presence"
# Scores x100 while someone moves in a room: (Hall hears AP, Office), (Office hears AP, Hall)
SCORES = {
    "kitchen": ((320, 230), (105, 110)),
    "office": ((105, 120), (310, 260)),
    None: ((104, 102), (103, 106)),
    "restless": ((192, 185), (104, 102)),  # the Kitchen's links busy, all under the motion threshold
    "upstairs": ((104, 215), (103, 225)),  # someone on the floor above: two links flag motion, no room's pattern
}
# RSSI with nobody there, same order, and how much weaker the links arrive with someone in a room,
# walking or sitting still: a body near a link absorbs some of it
RSSI = ((-50, -60), (-52, -61))
DROPS = {"kitchen": ((3, 2), (0, 0)), "office": ((0, 0), (3, 2))}


def moving(score: int) -> int:
    """The motion flag a node sets on a link at its default threshold (score x100)."""
    return int(score >= 200)


class House:
    """Hall and Office report once a second on the hub's clock; the rooms tick after each second."""

    def __init__(self, hass: HomeAssistant, udp: FakeUdp, entry: MockConfigEntry) -> None:
        self.hass, self.udp, self.seq = hass, udp, 0
        self.attach(entry)

    def attach(self, entry: MockConfigEntry) -> None:
        self.entry = entry
        self.clock = entry.runtime_data.clock = FakeClock()

    async def seconds(
        self, n: int = 1, room: str | None = None, step: float = 1.0, sitting: str | None = None, fidget: int = 0
    ) -> None:
        """n ticks of the rooms, with the hub's clock moving step seconds each: someone moving in
        room, or sitting still in sitting (quiet scores, weaker links), shifting in the chair every
        fidget seconds (the link between the nodes moves both ways that second)."""
        for _ in range(n):
            self.seq += 1
            jitter, noise = (self.seq * 7) % 11 - 5, self.seq % 3 - 1
            (a_ap, a_b), (b_ap, b_a) = SCORES[room]
            a_ap, a_b, b_ap, b_a = a_ap + jitter, a_b - jitter, b_ap - jitter, b_a + jitter
            if fidget and self.seq % fidget == 0:
                a_b, b_a = 205, 205
            (da_ap, da_b), (db_ap, db_a) = DROPS.get(sitting or room, ((0, 0), (0, 0)))
            (ra_ap, ra_b), (rb_ap, rb_a) = RSSI
            self.udp.receive(encode_report(self.seq, NODE_A, [
                (AP, 0, ra_ap - da_ap + noise, a_ap, 200, 20, moving(a_ap)),
                (NODE_B, 1, ra_b - da_b - noise, a_b, 150, 10, moving(a_b)),
            ], uptime=60), IP_A)
            self.udp.receive(encode_report(self.seq, NODE_B, [
                (AP, 0, rb_ap - db_ap - noise, b_ap, 200, 20, moving(b_ap)),
                (NODE_A, 1, rb_a - db_a + noise, b_a, 150, 10, moving(b_a)),
            ], uptime=60), IP_B)
            self.clock.now += step
            await fire(self.hass, 1)

    async def calibrate(self, area: str | None, seconds: int = 25, **data) -> None:
        """Calibrate a room (or the empty floor, area None) while moving in it (or not)."""
        if area is None:
            await self.hass.services.async_call(DOMAIN, "calibrate_empty", {"duration": seconds, **data}, blocking=True)
        else:
            await self.hass.services.async_call(DOMAIN, "calibrate_room", {"area": area, "duration": seconds}, blocking=True)
        await self.seconds(seconds, area)


def store(hass_storage: dict, entry: MockConfigEntry) -> dict:
    return hass_storage[f"wisp.{entry.entry_id}.calibration"]


def node_subentry(mac: str, host: str, name: str, area: str | None = None) -> ConfigSubentryData:
    data = {"mac": mac, "host": host, "name": name} | ({"area": area} if area else {})
    return ConfigSubentryData(data=data, subentry_type="node", title=name, unique_id=mac)


async def setup_with_areas(hass: HomeAssistant, *nodes: tuple) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Wisp", unique_id=DOMAIN, data={}, subentries_data=[node_subentry(*n) for n in nodes]
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def house(hass: HomeAssistant, udp: FakeUdp) -> House:
    for name in ("Kitchen", "Office"):
        ar.async_get(hass).async_create(name)
    return House(hass, udp, await setup_hub(hass, HALL, OFFICE))


async def test_calibrate_rooms_then_presence(hass: HomeAssistant, house: House, hass_storage: dict) -> None:
    assert hass.states.get(ROOM) is None and hass.states.get(CALIBRATION) is None  # not used yet
    await hass.services.async_call(DOMAIN, "calibrate_room", {"area": "kitchen", "duration": 25}, blocking=True)
    await hass.async_block_till_done()
    calibration = hass.states.get(CALIBRATION)
    assert calibration.state == "recording"
    assert calibration.attributes["recording"] == "Kitchen" and calibration.attributes["seconds_left"] == 25
    assert calibration.attributes["device_class"] == "enum" and calibration.attributes["samples"] == {}
    assert state(hass, ROOM) == STATE_UNAVAILABLE  # no link reported yet

    await house.seconds(24, "kitchen")
    calibration = hass.states.get(CALIBRATION)
    assert calibration.attributes["seconds_left"] == 1 and calibration.attributes["samples"] == {"Kitchen": 24}
    assert hass.states.get(KITCHEN) is None  # appears with the end of its first calibration
    await house.seconds(1, "kitchen")
    assert state(hass, CALIBRATION) == "idle"
    assert hass.states.get(CALIBRATION).attributes == {
        "recording": None, "seconds_left": None, "samples": {"Kitchen": 25},
        "device_class": "enum", "options": ["idle", "recording"], "friendly_name": "Wisp Calibration", "icon": "mdi:walk",
    }
    assert state(hass, KITCHEN) == "on"  # the recording said someone walked in it
    assert sorted(store(hass_storage, house.entry)["data"]["areas"]) == ["kitchen"]

    await house.calibrate("office")
    await house.calibrate(None)  # the empty floor, nobody moving
    assert hass.states.get(CALIBRATION).attributes["samples"] == {"Kitchen": 25, "Office": 25, "empty": 25}
    await house.seconds(61, None)  # the presence the recordings gave has ended
    assert (state(hass, KITCHEN), state(hass, OFFICE_PRESENCE)) == ("off", "off")

    await house.seconds(2, "kitchen")  # a room's presence takes two seconds of walking in a row
    room = hass.states.get(ROOM)
    assert room.state == "Kitchen" and room.attributes["confidence"] >= 0.9
    probabilities = room.attributes["probabilities"]
    assert list(probabilities)[0] == "Kitchen" and sorted(probabilities) == ["Kitchen", "Office", "none"]
    assert room.attributes["friendly_name"] == "Wisp Room"
    assert state(hass, KITCHEN) == "on" and hass.states.get(KITCHEN).attributes["confidence"] >= 0.9
    assert hass.states.get(KITCHEN).attributes["device_class"] == "occupancy"
    await house.seconds(2, "office")
    assert state(hass, ROOM) == "Office"
    assert (state(hass, OFFICE_PRESENCE), state(hass, KITCHEN)) == ("on", "on")  # two rooms, two people maybe

    # Nobody moving: the floor is empty at once, presence holds 60 s after a room last won
    await house.seconds(1, None)
    room = hass.states.get(ROOM)
    assert room.state == "none" and room.attributes == {
        "confidence": None, "probabilities": {}, "icon": "mdi:floor-plan", "friendly_name": "Wisp Room"
    }
    await house.seconds(57, None)
    assert (state(hass, KITCHEN), state(hass, OFFICE_PRESENCE)) == ("on", "on")
    await house.seconds(1, None)
    assert (state(hass, KITCHEN), state(hass, OFFICE_PRESENCE)) == ("off", "on")
    assert hass.states.get(KITCHEN).attributes["confidence"] is None
    await house.seconds(2, None)
    assert state(hass, OFFICE_PRESENCE) == "off"

    # Every room entity is on the hub's device, not a node's
    registry = er.async_get(hass)
    device = dr.async_get(hass).async_get(registry.async_get(ROOM).device_id)
    assert device.identifiers == {(DOMAIN, house.entry.entry_id)} and device.name == "Wisp"
    assert device.entry_type is dr.DeviceEntryType.SERVICE
    assert {registry.async_get(e).device_id for e in (ROOM, CALIBRATION, KITCHEN, OFFICE_PRESENCE)} == {device.id}
    assert registry.async_get(ROOM).config_subentry_id is None
    assert registry.async_get(CALIBRATION).entity_category == "diagnostic"


async def test_sitting_presence_from_the_walk_and_activity(hass: HomeAssistant, house: House) -> None:
    """Someone walks into the kitchen and sits down to work: the room they walked in stays on while
    the link between the nodes moves both ways now and then (each node confirming the other's), and
    ends once that stops. A link moving one way only is a node's noise and keeps no one."""
    await house.calibrate("kitchen")
    await hass.services.async_call(DOMAIN, "calibrate_room", {"area": "kitchen", "mode": "still", "duration": 25}, blocking=True)
    await house.seconds(25, sitting="kitchen")  # the kitchen is a room people sit in
    await house.calibrate("office")
    await house.calibrate(None)
    await house.seconds(61, None)  # the recordings' presence has ended
    assert state(hass, KITCHEN) == "off"
    await house.seconds(2, "kitchen")
    kitchen = hass.states.get(KITCHEN)
    assert kitchen.state == "on" and kitchen.attributes["still"] is False
    await house.seconds(300, sitting="kitchen", fidget=60)  # a shift in the chair a minute
    kitchen = hass.states.get(KITCHEN)
    assert kitchen.state == "on" and kitchen.attributes["still"] is True and kitchen.attributes["confidence"] >= 0.6
    assert (state(hass, ROOM), state(hass, OFFICE_PRESENCE)) == ("none", "off")  # nobody walks
    rooms = (await async_get_config_entry_diagnostics(hass, house.entry))["rooms"]
    (floor,) = rooms["floors"]
    assert floor["decision"]["room"] is None and floor["walked"] == "kitchen" and floor["active_s_ago"] < 60
    assert sorted(floor["still"]["probabilities"]) == ["kitchen", "none", "office"]
    assert rooms["areas"]["kitchen"]["still"] is True and rooms["settings"]["active_hold_s"] == 180

    # Gone without a sign: after 3 minutes without activity the presence ends at once
    await house.seconds(1, sitting="kitchen", fidget=1)  # a last shift in the chair
    await house.seconds(170, None)
    assert state(hass, KITCHEN) == "on"
    await house.seconds(15, None)
    kitchen = hass.states.get(KITCHEN)
    assert kitchen.state == "off" and kitchen.attributes == kitchen.attributes | {"confidence": None, "still": False}

    # A node's noise, one way only, keeps no one: the office walked in, then only Hall's link flags
    await house.seconds(2, "office")
    for _ in range(4):
        await house.seconds(50, None)
        house.udp.receive(encode_report(house.seq + 1, NODE_A, [(NODE_B, 1, -60, 230, 150, 10, 1)], uptime=60), IP_A)
    assert state(hass, OFFICE_PRESENCE) == "off"


async def test_still_calibration_and_separation(hass: HomeAssistant, house: House, hass_storage: dict) -> None:
    hub = house.entry.runtime_data
    await hass.services.async_call(
        DOMAIN, "calibrate_room", {"area": "office", "mode": "still", "duration": 25}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(CALIBRATION).attributes["recording"] == "Office still"
    assert hub.panel_snapshot()["floors"][0]["run"]["mode"] == "still"
    await house.seconds(25, sitting="office")
    assert hass.states.get(CALIBRATION).attributes["samples"] == {"Office still": 25}
    # Its own entity, on while the recording says someone sits there (no class could tell it yet)
    office = hass.states.get(OFFICE_PRESENCE)
    assert office.state == "on" and office.attributes["still"] is True
    stored = store(hass_storage, house.entry)["data"]
    assert stored["areas"] == {} and len(stored["still"]["office"]["signal"]) == 25

    await house.calibrate("kitchen")
    await hass.services.async_call(
        DOMAIN, "calibrate_room", {"area": "kitchen", "mode": "still", "duration": 25}, blocking=True
    )
    await house.seconds(25, sitting="kitchen")
    await house.calibrate(None)
    floor = hub.panel_snapshot()["floors"][0]
    kitchen = next(a for a in floor["areas"] if a["area"] == "kitchen")
    assert (kitchen["samples"], kitchen["still_samples"]) == (25, 25)
    separation = {(s["area"], s["kind"]): s for s in floor["separation"]}
    assert set(separation) == {(None, "empty"), ("kitchen", "moving"), ("kitchen", "still"), ("office", "still")}
    assert all(s["correct"] >= 0.9 and s["samples"] == 25 for s in separation.values()), separation
    assert separation[("office", "still")]["name"] == "Office"

    # Walking into the kitchen and sitting down to work: the room walked in, held by activity
    await house.seconds(61, None)
    await house.seconds(2, "kitchen")
    await house.seconds(70, sitting="kitchen", fidget=10)  # sitting and working: a shift now and then
    kitchen = hass.states.get(KITCHEN)
    assert kitchen.state == "on" and kitchen.attributes["still"] is True and state(hass, OFFICE_PRESENCE) == "off"

    # An office still calibration made while sitting in the kitchen looks like the kitchen's
    await hass.services.async_call(DOMAIN, "clear_calibration", {"area": "office"}, blocking=True)
    await hass.services.async_call(
        DOMAIN, "calibrate_room", {"area": "office", "mode": "still", "duration": 25}, blocking=True
    )
    await house.seconds(25, sitting="kitchen")
    office = next(s for s in hub.panel_snapshot()["floors"][0]["separation"] if s["area"] == "office")
    assert office["correct"] < 0.8 and office["confused_with"] == {
        "area": "kitchen", "name": "Kitchen", "kind": "still", "share": office["confused_with"]["share"]
    }
    diag = await async_get_config_entry_diagnostics(hass, house.entry)
    assert diag["rooms"]["floors"][0]["separation"] == hub.panel_snapshot()["floors"][0]["separation"]
    with pytest.raises(Exception, match="mode"):
        await hass.services.async_call(DOMAIN, "calibrate_room", {"area": "kitchen", "mode": "lying"}, blocking=True)


async def test_restless_links_make_no_presence(hass: HomeAssistant, house: House) -> None:
    """Links below the nodes' motion threshold (no motion flag) are nobody, however they look."""
    await house.calibrate("kitchen")
    await house.calibrate(None)
    await house.seconds(61, None)
    assert state(hass, KITCHEN) == "off"
    await house.seconds(5, "restless")
    assert (state(hass, ROOM), state(hass, KITCHEN)) == ("none", "off")
    await house.seconds(1, "kitchen")
    assert (state(hass, ROOM), state(hass, KITCHEN)) == ("Kitchen", "off")  # one second: not yet
    await house.seconds(1, "kitchen")
    assert (state(hass, ROOM), state(hass, KITCHEN)) == ("Kitchen", "on")


async def test_no_walker_when_the_empty_floor_wins(hass: HomeAssistant, house: House) -> None:
    """Links can flag motion with nobody on the floor (people upstairs, someone shifting in a
    chair): when room presence weighs that motion and the empty floor wins, the map places nobody
    walking. Before, the locator placed a walker from the flags alone, often in the wrong room."""
    from .test_map import LAYOUT, ROWS

    house.udp.receive(encode_hive_report(1, NODE_A, 0xBEEF, LAYOUT, ROWS), IP_A)
    await house.calibrate("kitchen")
    await hass.services.async_call(DOMAIN, "calibrate_room", {"area": "office", "duration": 25}, blocking=True)
    await house.seconds(3, "office")
    assert state(hass, ROOM) == "Office"  # while it records, the recording says where someone moves
    await house.seconds(22, "office")
    await house.hass.services.async_call(DOMAIN, "calibrate_empty", {"duration": 25}, blocking=True)
    await house.seconds(25, "upstairs")  # the empty floor learns what the floor above does to it
    hub = house.entry.runtime_data
    await house.seconds(5, "upstairs")  # the Office's presence from its recording still holds:
    (person,) = hub.map_snapshot()["people"]  # someone shown there, still, as its presence sensor says
    assert person["walking"] is False and state(hass, OFFICE_PRESENCE) == "on"
    await house.seconds(60, "upstairs")  # once it ends, nobody: the motion is the floor above's
    assert state(hass, ROOM) == "none" and state(hass, OFFICE_PRESENCE) == "off" and "people" not in hub.map_snapshot()
    await house.seconds(5, "kitchen")  # someone walking in a room still shows
    assert state(hass, ROOM) == "Kitchen" and hub.map_snapshot()["people"]


async def test_room_writes(hass: HomeAssistant, house: House) -> None:
    await house.calibrate("kitchen")
    await house.calibrate("office")
    await house.seconds(61, None)  # both rooms won while calibrating: wait out the hold
    assert (state(hass, KITCHEN), state(hass, OFFICE_PRESENCE)) == ("off", "off")
    events = async_capture_events(hass, EVENT_STATE_CHANGED)

    def writes(entity_id: str) -> int:
        return sum(e.data["entity_id"] == entity_id for e in events)

    await house.seconds(3, "kitchen")
    assert state(hass, ROOM) == "Kitchen" and writes(ROOM) == 1  # same room, same confidence: nothing more
    assert writes(KITCHEN) == 1
    await house.seconds(1, "office", step=0.3)
    assert state(hass, ROOM) == "Office" and writes(ROOM) == 2  # another room is written at once
    await house.seconds(1, "office", step=0.3)  # its presence, from two seconds in a row
    assert state(hass, OFFICE_PRESENCE) == "on" and writes(OFFICE_PRESENCE) == 1


async def test_calibration_survives_a_restart(hass: HomeAssistant, house: House, hass_storage: dict) -> None:
    await house.calibrate("kitchen")
    await house.calibrate("office")
    entry = house.entry
    stored = store(hass_storage, entry)
    assert stored["version"] == 1 and sorted(stored["data"]) == ["areas", "empty", "still"]
    kitchen = stored["data"]["areas"]["kitchen"]
    assert kitchen["links"] == [f"{NODE_A}>{NODE_B}", f"{NODE_B}>{NODE_A}", f"{AP}>{NODE_A}", f"{AP}>{NODE_B}"]
    assert len(kitchen["samples"]) == 25 and len(kitchen["samples"][0]) == 4
    assert len(kitchen["signal"]) == 25 and -65 < kitchen["signal"][-1][0] < -55  # RSSI, the same links

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    house.attach(entry)
    # From storage, nothing to relearn; unavailable until the floor's links report
    assert state(hass, KITCHEN) == STATE_UNAVAILABLE and state(hass, CALIBRATION) == "idle"
    assert hass.states.get(CALIBRATION).attributes["samples"] == {"Kitchen": 25, "Office": 25}
    await house.seconds(1, None)
    assert state(hass, KITCHEN) == "off" and state(hass, ROOM) == "none"
    await house.seconds(2, "office")
    assert state(hass, ROOM) == "Office" and state(hass, OFFICE_PRESENCE) == "on"

    # Deleting the hub deletes its calibration
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert f"wisp.{entry.entry_id}.calibration" not in hass_storage


async def test_a_run_cut_short_keeps_its_samples(hass: HomeAssistant, house: House, hass_storage: dict) -> None:
    await hass.services.async_call(DOMAIN, "calibrate_room", {"area": "kitchen"}, blocking=True)
    await house.seconds(30, "kitchen")
    assert hass.states.get(CALIBRATION).attributes["seconds_left"] == 30  # 60 s by default
    # Another room on the same floor replaces the run; what it recorded is kept
    await hass.services.async_call(DOMAIN, "calibrate_room", {"area": "office", "duration": 20}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(CALIBRATION).attributes["recording"] == "Office"
    assert len(store(hass_storage, house.entry)["data"]["areas"]["kitchen"]["samples"]) == 30
    assert state(hass, KITCHEN) == "on"
    # Unloading keeps an unfinished run's samples too
    await house.seconds(5, "office")
    assert await hass.config_entries.async_unload(house.entry.entry_id)
    await hass.async_block_till_done()
    assert len(store(hass_storage, house.entry)["data"]["areas"]["office"]["samples"]) == 5


async def test_stop_calibration_keeps_what_was_recorded(hass: HomeAssistant, house: House, hass_storage: dict) -> None:
    await hass.services.async_call(DOMAIN, "calibrate_room", {"area": "kitchen"}, blocking=True)
    await house.seconds(25, "kitchen")
    assert hass.states.get(CALIBRATION).attributes["recording"] == "Kitchen"
    await hass.services.async_call(DOMAIN, "stop_calibration", {}, blocking=True)
    await hass.async_block_till_done()
    assert state(hass, CALIBRATION) == "idle" and hass.states.get(CALIBRATION).attributes["recording"] is None
    assert len(store(hass_storage, house.entry)["data"]["areas"]["kitchen"]["samples"]) == 25
    await house.seconds(5, "kitchen")  # nothing more is recorded
    assert len(store(hass_storage, house.entry)["data"]["areas"]["kitchen"]["samples"]) == 25
    # Stopping when nothing records, or a floor by the hub's name, is harmless
    await hass.services.async_call(DOMAIN, "stop_calibration", {"floor": "Wisp"}, blocking=True)


async def test_unreadable_storage_starts_clean(
    hass: HomeAssistant, udp: FakeUdp, hass_storage: dict, caplog: pytest.LogCaptureFixture
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="Wisp", unique_id=DOMAIN, data={},
                            subentries_data=[node_subentry(*HALL)])
    hass_storage[f"wisp.{entry.entry_id}.calibration"] = {"version": 1, "data": {"areas": ["kitchen"]}}
    entry.add_to_hass(hass)
    with caplog.at_level(logging.WARNING):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert "Discarding the stored room calibration" in caplog.text
    assert entry.runtime_data.presence.engine.areas == {}
    assert link_ids(hass) == []


async def test_clear_calibration(hass: HomeAssistant, house: House, hass_storage: dict) -> None:
    await house.calibrate("kitchen")
    await house.calibrate("office")
    await house.calibrate(None)
    await hass.services.async_call(DOMAIN, "clear_calibration", {"area": "Office"}, blocking=True)  # by name too
    await hass.async_block_till_done()
    assert hass.states.get(OFFICE_PRESENCE) is None and er.async_get(hass).async_get(OFFICE_PRESENCE) is None
    assert state(hass, KITCHEN) is not None
    assert list(store(hass_storage, house.entry)["data"]["areas"]) == ["kitchen"]
    await house.seconds(1, "office")
    assert state(hass, ROOM) != "Office"

    await hass.services.async_call(DOMAIN, "clear_calibration", {}, blocking=True)
    await hass.async_block_till_done()
    assert store(hass_storage, house.entry)["data"] == {"areas": {}, "empty": {}, "still": {}}
    for entity_id in (ROOM, CALIBRATION, KITCHEN):  # room presence no longer in use
        assert hass.states.get(entity_id) is None and er.async_get(hass).async_get(entity_id) is None


async def test_deleted_area_takes_its_calibration(hass: HomeAssistant, house: House, hass_storage: dict) -> None:
    await house.calibrate("kitchen")
    await house.calibrate("office")
    ar.async_get(hass).async_delete("kitchen")
    await hass.async_block_till_done()
    assert hass.states.get(KITCHEN) is None
    assert list(store(hass_storage, house.entry)["data"]["areas"]) == ["office"]
    assert state(hass, OFFICE_PRESENCE) is not None


async def test_floors_follow_node_areas(hass: HomeAssistant, udp: FakeUdp, hass_storage: dict) -> None:
    floors, areas = fr.async_get(hass), ar.async_get(hass)
    ground, upstairs = floors.async_create("Ground floor", level=0), floors.async_create("Upstairs", level=1)
    areas.async_create("Kitchen", floor_id=ground.floor_id)
    areas.async_create("Bedroom", floor_id=upstairs.floor_id)
    entry = await setup_with_areas(hass, (*HALL, "kitchen"), (*OFFICE, "bedroom"))
    house = House(hass, udp, entry)
    presence = entry.runtime_data.presence
    assert {k: sorted(f.nodes) for k, f in presence.floors.items()} == {"ground_floor": [NODE_A], "upstairs": [NODE_B]}

    # Kitchen is on the ground floor: only the links Hall receives count there
    await house.calibrate("kitchen")
    kitchen = store(hass_storage, entry)["data"]["areas"]["kitchen"]
    assert kitchen["links"] == [f"{NODE_B}>{NODE_A}", f"{AP}>{NODE_A}"]
    assert state(hass, "sensor.wisp_ground_floor_room") == "Kitchen"
    assert hass.states.get("sensor.wisp_ground_floor_room").attributes["friendly_name"] == "Wisp Ground floor room"
    assert state(hass, "sensor.wisp_ground_floor_calibration") == "idle"
    assert hass.states.get("sensor.wisp_upstairs_room") is None  # nothing calibrated upstairs

    # The empty floor without a floor given: every floor records at once
    await house.calibrate(None)
    assert hass.states.get("sensor.wisp_upstairs_calibration").attributes["samples"] == {"empty": 25}
    assert state(hass, "sensor.wisp_upstairs_room") == "none"
    await house.calibrate(None, floor="Upstairs")  # by name too
    assert hass.states.get("sensor.wisp_upstairs_calibration").attributes["samples"] == {"empty": 50}

    # Office moves downstairs: upstairs has no node left and its sensors go
    sub_b = next(s for s in entry.subentries.values() if s.unique_id == NODE_B)
    hass.config_entries.async_update_subentry(entry, sub_b, data={**sub_b.data, "area": "kitchen"})
    await hass.async_block_till_done()
    assert list(presence.floors) == ["ground_floor"]
    assert hass.states.get("sensor.wisp_upstairs_room") is None
    assert state(hass, "sensor.wisp_ground_floor_room") is not None

    # An area moved to another floor goes with it
    areas.async_update("kitchen", floor_id=upstairs.floor_id)
    await hass.async_block_till_done()
    assert presence.floor_areas("upstairs") == ["kitchen"] and list(presence.floors) == ["upstairs"]
    assert hass.states.get("sensor.wisp_ground_floor_room") is None
    assert state(hass, "sensor.wisp_upstairs_room") is not None

    diag = await async_get_config_entry_diagnostics(hass, entry)
    rooms = diag["rooms"]
    assert rooms["settings"]["hold_s"] == 60 and rooms["settings"]["quiet"] == 1.5
    assert rooms["areas"]["kitchen"] == {
        "name": "Kitchen", "floor": "upstairs", "samples": 25, "links": 2, "still_samples": 0,
        "presence": rooms["areas"]["kitchen"]["presence"], "still": False,
    }
    assert rooms["empty"] == [
        {"floor": "ground_floor", "samples": 25, "links": 2}, {"floor": "upstairs", "samples": 50, "links": 2}
    ]
    (floor,) = rooms["floors"]
    assert (floor["floor"], floor["nodes"], floor["areas"]) == ("upstairs", [NODE_A, NODE_B], ["kitchen"])
    json.dumps(diag)


async def test_floors_with_few_nodes_raise_an_issue(hass: HomeAssistant, udp: FakeUdp) -> None:
    def issues() -> dict[str, tuple]:
        return {
            issue.translation_key: (issue.translation_placeholders["floor"], issue.translation_placeholders["count"])
            for (domain, _), issue in ir.async_get(hass).issues.items() if domain == DOMAIN
        }

    floors, areas = fr.async_get(hass), ar.async_get(hass)
    ground, upstairs = floors.async_create("Ground floor", level=0), floors.async_create("Upstairs", level=1)
    areas.async_create("Kitchen", floor_id=ground.floor_id)
    areas.async_create("Bedroom", floor_id=upstairs.floor_id)
    entry = await setup_with_areas(hass, (*HALL, "kitchen"), (*OFFICE, None))
    assert issues() == {"few_nodes": ("Ground floor", "1"), "few_nodes_home": ("Wisp", "1")}
    assert all(i.severity == ir.IssueSeverity.WARNING and not i.is_fixable for i in ir.async_get(hass).issues.values())

    # Office joins the ground floor: one floor, still short of three
    sub_b = next(s for s in entry.subentries.values() if s.unique_id == NODE_B)
    hass.config_entries.async_update_subentry(entry, sub_b, data={**sub_b.data, "area": "kitchen"})
    await hass.async_block_till_done()
    assert issues() == {"few_nodes": ("Ground floor", "2")}

    # A third node on the floor settles it
    den = {"mac": "02:57:49:53:50:03", "host": "192.0.2.13", "name": "Den", "area": "kitchen"}
    sub = ConfigSubentry(data=MappingProxyType(den), subentry_type="node", title="Den", unique_id=den["mac"])
    hass.config_entries.async_add_subentry(entry, sub)
    await hass.async_block_till_done()
    assert issues() == {}

    sub_b = next(s for s in entry.subentries.values() if s.unique_id == NODE_B)
    hass.config_entries.async_update_subentry(entry, sub_b, data={**sub_b.data, "area": "bedroom"})
    await hass.async_block_till_done()
    assert issues() == {"few_nodes": ("Upstairs", "1")}
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert issues() == {}


async def test_service_errors(hass: HomeAssistant, udp: FakeUdp) -> None:
    async def call(service: str, data: dict) -> str:
        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(DOMAIN, service, data, blocking=True)
        return err.value.translation_key

    entry = await setup_hub(hass)
    assert await call("calibrate_room", {"area": "kitchen"}) == "no_nodes"
    await hass.config_entries.async_unload(entry.entry_id)
    assert await call("calibrate_empty", {}) == "not_loaded"
    assert await call("clear_calibration", {}) == "not_loaded"
    await hass.config_entries.async_remove(entry.entry_id)

    floors, areas = fr.async_get(hass), ar.async_get(hass)
    ground, upstairs = floors.async_create("Ground floor"), floors.async_create("Upstairs")
    areas.async_create("Kitchen", floor_id=ground.floor_id)
    areas.async_create("Attic", floor_id=upstairs.floor_id)
    areas.async_create("Garden")
    await setup_with_areas(hass, (*HALL, "kitchen"))
    assert await call("calibrate_room", {"area": "garage"}) == "unknown_area"
    assert await call("calibrate_room", {"area": "attic"}) == "no_nodes_on_floor"
    assert await call("calibrate_room", {"area": "garden"}) == "area_without_floor"
    assert await call("calibrate_empty", {"floor": "cellar"}) == "unknown_floor"
    assert await call("calibrate_empty", {"floor": "upstairs"}) == "floor_without_nodes"
    with pytest.raises(Exception, match="duration"):
        await hass.services.async_call(DOMAIN, "calibrate_room", {"area": "kitchen", "duration": 5}, blocking=True)
