"""Config flow: hub setup, zeroconf discovery, nodes added or changed by address."""
from __future__ import annotations

from ipaddress import ip_address
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.zeroconf.discovery import _match_against_props
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER, SOURCE_ZEROCONF
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar, device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from homeassistant.loader import async_get_zeroconf

from custom_components.wisp.hub import node_devices
from custom_components.wisp.const import DOMAIN, PROJECT_NAME
from custom_components.wisp.hub import ProbeError

from .conftest import IP_A, IP_B, NODE_A, NODE_B, FakeUdp
from .test_init import HALL, setup_hub

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

PROBE = "custom_components.wisp.config_flow.async_probe"


def esphome_txt(mac: str = NODE_A, project: str = PROJECT_NAME) -> dict[str, str]:
    """TXT records of an ESPHome 2026.9 node (esphome/components/mdns/mdns_component.cpp)."""
    suffix = mac.replace(":", "")[-6:]
    return {
        "friendly_name": f"Wisp {suffix}",
        "version": "2026.9.1",
        "config_hash": "1a2b3c4d",
        "mac": mac.replace(":", ""),
        "platform": "ESP32",
        "board": "esp32-s3-devkitc-1",
        "network": "wifi",
        "api_encryption": "Noise_NNpsk0_25519_ChaChaPoly_SHA256",
        "project_name": project,
        "project_version": "0.1.0",
    }


def discovery(mac: str = NODE_A, ip: str = IP_A, **kwargs) -> ZeroconfServiceInfo:
    suffix = mac.replace(":", "")[-6:]
    return ZeroconfServiceInfo(
        ip_address=ip_address(ip),
        ip_addresses=[ip_address(ip)],
        port=6053,
        hostname=f"wisp-{suffix}.local.",
        type="_esphomelib._tcp.local.",
        name=f"wisp-{suffix}._esphomelib._tcp.local.",
        properties=esphome_txt(mac, **kwargs),
    )


async def discover(hass: HomeAssistant, info: ZeroconfServiceInfo):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_ZEROCONF}, data=info)


def nodes(entry) -> dict[str, dict]:
    return {sub.unique_id: dict(sub.data) | {"title": sub.title} for sub in entry.subentries.values()}


async def test_manifest_matcher_matches_wisp_nodes_only(hass: HomeAssistant) -> None:
    matchers = await async_get_zeroconf(hass)
    (ours,) = [m for m in matchers["_esphomelib._tcp.local."] if m["domain"] == DOMAIN]
    assert ours["properties"] == {"project_name": PROJECT_NAME}
    assert _match_against_props(ours["properties"], esphome_txt())
    assert not _match_against_props(ours["properties"], esphome_txt(project="esphome.bluetooth-proxy"))
    assert not _match_against_props(ours["properties"], {k: v for k, v in esphome_txt().items() if k != "project_name"})


async def test_user_sets_up_the_hub_once(hass: HomeAssistant, udp: FakeUdp) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Wisp"
    entry = result["result"]
    assert entry.unique_id == DOMAIN and not entry.subentries
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


def esphome_node(hass: HomeAssistant, mac: str, host: str, name: str, model: str = "wisp-node") -> None:
    """A node Home Assistant has as an ESPHome device, named as the ESPHome integration names it."""
    entry = MockConfigEntry(domain="esphome", unique_id=mac, data={"host": host})
    entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, connections={(CONNECTION_NETWORK_MAC, mac)}, name=name,
        manufacturer="albert-canfield", model=model,
    )


async def test_new_hub_adopts_the_nodes_home_assistant_has(hass: HomeAssistant, udp: FakeUdp) -> None:
    """Set up again after removing it, a hub takes every Wisp node ESPHome has, not only the one
    Home Assistant rediscovers."""
    esphome_node(hass, NODE_A, IP_A, "Wisp 535001")
    esphome_node(hass, NODE_B, IP_B, "Wisp 535002")
    esphome_node(hass, "02:57:49:53:50:09", "192.168.1.99", "Plug", model="smart-plug")  # not a node
    result = await discover(hass, discovery())  # Node A, rediscovered
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert nodes(result["result"]) == {
        NODE_A: {"mac": NODE_A, "host": IP_A, "name": "Wisp 535001", "title": "Wisp 535001"},
        NODE_B: {"mac": NODE_B, "host": IP_B, "name": "Wisp 535002", "title": "Wisp 535002"},
    }
    assert sorted(result["result"].runtime_data.nodes) == [NODE_A, NODE_B]


async def test_hub_set_up_by_hand_adopts_the_nodes_too(hass: HomeAssistant, udp: FakeUdp) -> None:
    esphome_node(hass, NODE_B, IP_B, "Wisp 535002")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert list(nodes(result["result"])) == [NODE_B]


async def test_first_node_found_sets_up_the_hub(hass: HomeAssistant, udp: FakeUdp) -> None:
    result = await discover(hass, discovery())
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "discovery_confirm"
    assert result["description_placeholders"] == {"name": "Wisp 535001", "host": IP_A}
    flow = hass.config_entries.flow.async_progress()[0]
    assert flow["context"]["title_placeholders"] == {"name": "Wisp 535001"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id == DOMAIN
    assert nodes(entry) == {NODE_A: {"mac": NODE_A, "host": IP_A, "name": "Wisp 535001", "title": "Wisp 535001"}}
    assert (IP_A, 47010) in udp.sent_to()


async def test_more_nodes_join_without_asking(hass: HomeAssistant, udp: FakeUdp) -> None:
    entry = await setup_hub(hass)
    result = await discover(hass, discovery())
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "node_added"
    await hass.async_block_till_done()
    assert list(nodes(entry)) == [NODE_A]
    assert list(entry.runtime_data.nodes) == [NODE_A]
    assert (IP_A, 47010) in udp.sent_to()
    assert hass.config_entries.flow.async_progress() == []

    result = await discover(hass, discovery())  # announced again
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert len(entry.subentries) == 1


async def test_found_node_follows_new_address(hass: HomeAssistant, udp: FakeUdp) -> None:
    entry = await setup_hub(hass, HALL)
    udp.clear()
    result = await discover(hass, discovery(ip="192.168.1.99"))
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    await hass.async_block_till_done()
    assert nodes(entry)[NODE_A] == {"mac": NODE_A, "host": "192.168.1.99", "name": "Hall", "title": "Hall"}
    assert udp.sent_to() == [("192.168.1.99", 47010)]


@pytest.mark.parametrize(
    "info",
    [
        discovery(project="esphome.bluetooth-proxy"),
        ZeroconfServiceInfo(
            ip_address=ip_address(IP_A), ip_addresses=[ip_address(IP_A)], port=6053, hostname="x.local.",
            type="_esphomelib._tcp.local.", name="x._esphomelib._tcp.local.",
            properties={"project_name": PROJECT_NAME, "mac": "not-a-mac"},
        ),
    ],
)
async def test_other_devices_are_ignored(hass: HomeAssistant, info: ZeroconfServiceInfo) -> None:
    result = await discover(hass, info)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "not_wisp_node"


async def test_nodes_waiting_for_confirmation_join_the_new_hub(hass: HomeAssistant, udp: FakeUdp) -> None:
    first = await discover(hass, discovery())
    second = await discover(hass, discovery(NODE_B, IP_B))
    assert second["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(first["flow_id"], {})
    await hass.async_block_till_done()
    entry = result["result"]
    assert sorted(nodes(entry)) == [NODE_A, NODE_B]
    assert hass.config_entries.flow.async_progress() == []
    assert sorted(entry.runtime_data.nodes) == [NODE_A, NODE_B]


async def test_ignored_node_stays_ignored(hass: HomeAssistant, udp: FakeUdp) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=NODE_A, source="ignore").add_to_hass(hass)
    result = await discover(hass, discovery())
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    entry = await setup_hub(hass)
    result = await discover(hass, discovery())
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert not entry.subentries


async def add_node(hass: HomeAssistant, entry, user_input: dict, mac: str | Exception = NODE_B):
    result = await hass.config_entries.subentries.async_init((entry.entry_id, "node"), context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    probe = AsyncMock(side_effect=mac) if isinstance(mac, Exception) else AsyncMock(return_value=mac)
    with patch(PROBE, probe):
        result = await hass.config_entries.subentries.async_configure(result["flow_id"], user_input)
    await hass.async_block_till_done()
    return result, probe


async def test_add_node_by_address(hass: HomeAssistant, udp: FakeUdp) -> None:
    entry = await setup_hub(hass, HALL)
    udp.clear()
    result, probe = await add_node(hass, entry, {"host": f" {IP_B} "})
    assert probe.await_args.args[1] == IP_B
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert nodes(entry)[NODE_B] == {"mac": NODE_B, "host": IP_B, "name": "Wisp 535002", "title": "Wisp 535002"}
    assert (IP_B, 47010) in udp.sent_to()  # subscribed at once


async def test_add_node_named_by_user_or_esphome(hass: HomeAssistant, udp: FakeUdp) -> None:
    entry = await setup_hub(hass)
    await add_node(hass, entry, {"host": IP_A, "name": "Hall"}, NODE_A)
    esphome = MockConfigEntry(domain="esphome")
    esphome.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=esphome.entry_id, connections={(CONNECTION_NETWORK_MAC, NODE_B)}, name="Kitchen node"
    )
    await add_node(hass, entry, {"host": IP_B})
    assert {mac: n["title"] for mac, n in nodes(entry).items()} == {NODE_A: "Hall", NODE_B: "Kitchen node"}


@pytest.mark.parametrize("error", ["no_answer", "cannot_resolve"])
async def test_add_node_errors(hass: HomeAssistant, udp: FakeUdp, error: str) -> None:
    entry = await setup_hub(hass)
    result, _ = await add_node(hass, entry, {"host": "wisp-nowhere.local"}, ProbeError(error))
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": error}
    assert not entry.subentries


async def test_add_node_already_added(hass: HomeAssistant, udp: FakeUdp) -> None:
    entry = await setup_hub(hass, HALL)
    result, _ = await add_node(hass, entry, {"host": "wisp-535001.local"}, NODE_A)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert len(entry.subentries) == 1


async def reconfigure(hass: HomeAssistant, entry, user_input: dict, mac: str = NODE_A):
    sub_id = next(iter(entry.subentries))
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "node"), context={"source": SOURCE_RECONFIGURE, "subentry_id": sub_id}
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "reconfigure"
    with patch(PROBE, AsyncMock(return_value=mac)) as probe:
        result = await hass.config_entries.subentries.async_configure(result["flow_id"], user_input)
    await hass.async_block_till_done()
    return result, probe


async def test_reconfigure_node(hass: HomeAssistant, udp: FakeUdp) -> None:
    entry = await setup_hub(hass, HALL)
    udp.clear()
    result, _ = await reconfigure(hass, entry, {"host": "192.168.1.70", "name": "Hallway"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert nodes(entry)[NODE_A] == {"mac": NODE_A, "host": "192.168.1.70", "name": "Hallway", "title": "Hallway"}
    assert udp.sent_to() == [("192.168.1.70", 47010)]

    result, probe = await reconfigure(hass, entry, {"host": "192.168.1.70", "name": "Hall"})
    assert result["reason"] == "reconfigure_successful" and probe.await_count == 0  # same host: no probe

    result, _ = await reconfigure(hass, entry, {"host": IP_B}, NODE_B)
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": "different_node"}
    assert nodes(entry)[NODE_A]["host"] == "192.168.1.70"


def suggested(result, field: str):
    (key,) = [key for key in result["data_schema"].schema if key == field]
    return (key.description or {}).get("suggested_value")


async def test_node_area(hass: HomeAssistant, udp: FakeUdp) -> None:
    """The room a node stands in, which gives its floor: set when adding, changed or cleared later."""
    for name in ("Kitchen", "Office"):
        ar.async_get(hass).async_create(name)
    entry = await setup_hub(hass, HALL)
    await add_node(hass, entry, {"host": IP_B, "area": "office"})
    # The area moves onto the node's devices, where Home Assistant keeps areas
    assert nodes(entry)[NODE_B] == {"mac": NODE_B, "host": IP_B, "name": "Wisp 535002", "title": "Wisp 535002"}
    assert entry.runtime_data.nodes[NODE_B].area == "office"
    assert {d.area_id for d in node_devices(hass, NODE_B)} == {"office"}
    assert entry.runtime_data.presence.floors[""].nodes == {NODE_A, NODE_B}  # areas without a floor share one

    # Hall has no area yet: the form suggests the area of its ESPHome device
    esphome = MockConfigEntry(domain="esphome")
    esphome.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=esphome.entry_id, connections={(CONNECTION_NETWORK_MAC, NODE_A)}, name="Wisp 535001"
    )
    dev_reg.async_update_device(device.id, area_id="kitchen")
    sub_id = next(s.subentry_id for s in entry.subentries.values() if s.unique_id == NODE_A)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "node"), context={"source": SOURCE_RECONFIGURE, "subentry_id": sub_id}
    )
    assert suggested(result, "area") == "kitchen" and suggested(result, "host") == IP_A
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"host": IP_A, "name": "Hall", "area": "kitchen"}
    )
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert "area" not in nodes(entry)[NODE_A] and entry.runtime_data.nodes[NODE_A].area == "kitchen"
    assert {d.area_id for d in node_devices(hass, NODE_A)} == {"kitchen"}

    # Cleared: the node goes back to the hub's own floor
    result, _ = await reconfigure(hass, entry, {"host": IP_A, "name": "Hall"})
    assert result["reason"] == "reconfigure_successful"
    assert nodes(entry)[NODE_A] == {"mac": NODE_A, "host": IP_A, "name": "Hall", "title": "Hall"}
    assert entry.runtime_data.nodes[NODE_A].area is None
    assert {d.area_id for d in node_devices(hass, NODE_A)} == {None}


async def test_options_set_the_presence_hold(hass: HomeAssistant, udp: FakeUdp) -> None:
    entry = await setup_hub(hass, HALL)
    hub = entry.runtime_data
    assert hub.presence.engine.hold == 60
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"presence_hold": 120})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"presence_hold": 120}
    assert entry.runtime_data is hub and hub.presence.engine.hold == 120  # no reload
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert suggested(result, "presence_hold") == 120
