"""Hub: one UDP socket that subscribes to every node and keeps the latest link and hive state."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import timedelta
import ipaddress
import logging
import socket
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_NAME
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_AREA,
    HIVE_TIMEOUT,
    LINK_TIMEOUT,
    MANUFACTURER,
    MODEL,
    NODE_PORT,
    PROBE_TRIES,
    RESOLVE_INTERVAL,
    SUBENTRY_NODE,
    SUBSCRIBE_INTERVAL,
)
from .engine import (
    KIND_AP,
    STREAM_HIVE_REPORTS,
    STREAM_LINK_REPORTS,
    HiveReport,
    HiveState,
    HiveTracker,
    LinkKey,
    LinkReport,
    LinkState,
    LinkTable,
    ProtocolError,
    access_points,
    build_subscribe,
    parse_packet,
)
from .presence import RoomPresence

_LOGGER = logging.getLogger(__name__)
LISTEN = ("0.0.0.0", 0)  # any interface, ephemeral port
STREAMS = STREAM_LINK_REPORTS | STREAM_HIVE_REPORTS

# Home Assistant 2026.9 gives each config entry its own devices. A node then has a Wisp device
# linked to its ESPHome device by the shared MAC; before, both integrations shared one device.
PER_ENTRY_DEVICES = hasattr(dr.DeviceRegistry, "async_get_device_by_connection")

type WispConfigEntry = ConfigEntry[WispHub]


@dataclass(slots=True)
class Node:
    mac: str
    host: str  # IP address or host name
    name: str
    subentry_id: str
    area: str | None = None  # the room it stands in, which gives its floor
    address: str | None = None  # host resolved to IPv4
    resolved_at: float = 0.0


class ProbeError(Exception):
    """No node found. The message is the config flow error key."""


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, hub: WispHub) -> None:
        self.hub = hub

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        self.hub.async_handle_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("UDP error: %s", exc)


class WispHub:
    def __init__(self, hass: HomeAssistant, entry: WispConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.clock: Callable[[], float] = time.monotonic
        self.table = LinkTable(LINK_TIMEOUT)
        self.hive = HiveTracker(HIVE_TIMEOUT)
        self.nodes: dict[str, Node] = {}
        self.transport: asyncio.DatagramTransport | None = None
        self.port: int | None = None
        self.stats = dict.fromkeys(
            ("packets", "reports", "hive_reports", "duplicates", "unknown_node", "ignored", "invalid", "subscribes"), 0
        )
        self._new_link_listeners: list[Callable[[LinkKey], None]] = []
        self._link_listeners: dict[LinkKey, list[Callable[[], None]]] = {}
        self._unsubs: list[CALLBACK_TYPE] = []
        self._subscribing = False
        self.presence = RoomPresence(self)

    async def async_start(self) -> None:
        self.async_sync_nodes()
        await self.presence.async_start()
        transport, _ = await self.hass.loop.create_datagram_endpoint(
            lambda: _Protocol(self), local_addr=LISTEN, family=socket.AF_INET
        )
        self.transport = transport
        self.port = transport.get_extra_info("sockname")[1]
        self._unsubs = [
            async_track_time_interval(
                self.hass, self._async_subscribe_tick, SUBSCRIBE_INTERVAL,
                name="wisp subscribe", cancel_on_shutdown=True,
            ),
            async_track_time_interval(
                self.hass, self._async_expire_tick, timedelta(seconds=1),
                name="wisp link timeout", cancel_on_shutdown=True,
            ),
        ]
        await self.async_subscribe()

    @callback
    def async_stop(self, *_: Any) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        self.presence.async_stop()
        if self.transport:
            self.transport.close()
            self.transport = None

    # Nodes

    @callback
    def async_sync_nodes(self) -> None:
        """Follow the node subentries: added, changed or removed."""
        wanted = {
            sub.data[CONF_MAC]: sub for sub in self.entry.subentries.values() if sub.subentry_type == SUBENTRY_NODE
        }
        for mac in set(self.nodes) - set(wanted):
            del self.nodes[mac]
            self.hive.forget(mac)
            # Home Assistant removes the subentry's entities and device link itself
            for key in self.table.forget(mac):
                self._link_listeners.pop(key, None)
        dev_reg = dr.async_get(self.hass)
        changed = False
        for mac, sub in wanted.items():
            host, name = sub.data[CONF_HOST], sub.data.get(CONF_NAME) or sub.title
            node = self.nodes.get(mac)
            if node is None:
                self.nodes[mac] = node = Node(mac, host, name, sub.subentry_id)
                changed = True
            else:
                if node.host != host:
                    node.host, node.address, node.resolved_at = host, None, 0.0
                    changed = True
                node.name, node.subentry_id = name, sub.subentry_id
            node.area = sub.data.get(CONF_AREA)
            dev_reg.async_get_or_create(
                config_entry_id=self.entry.entry_id, config_subentry_id=sub.subentry_id, **self.device_info(mac)
            )
        self.presence.async_sync_floors()
        if changed and self.transport:
            self.entry.async_create_task(self.hass, self.async_subscribe(), "wisp subscribe")

    def device_info(self, mac: str) -> DeviceInfo:
        """The node's device, matched to its ESPHome device by MAC."""
        node = self.nodes.get(mac)
        name = node.name if node else default_node_name(mac)
        connections = {(CONNECTION_NETWORK_MAC, mac)}
        if PER_ENTRY_DEVICES:
            return DeviceInfo(connections=connections, name=name, manufacturer=MANUFACTURER, model=MODEL)
        # One device shared with ESPHome: name it only if ESPHome has not
        return DeviceInfo(
            connections=connections, default_name=name, default_manufacturer=MANUFACTURER, default_model=MODEL
        )

    # Subscriptions

    async def async_subscribe(self) -> None:
        """Renew every node's lease, from the hub's socket so the reports come back to it."""
        packet = build_subscribe(STREAMS)
        for node in list(self.nodes.values()):
            address = await self._async_address(node)
            if address is None or self.transport is None:
                continue
            self.transport.sendto(packet, (address, NODE_PORT))
            self.stats["subscribes"] += 1

    async def _async_subscribe_tick(self, _now: Any = None) -> None:
        if self._subscribing:  # a slow host name lookup is still running
            return
        self._subscribing = True
        try:
            await self.async_subscribe()
        finally:
            self._subscribing = False

    async def _async_address(self, node: Node) -> str | None:
        if _ipv4(node.host):
            return node.host
        now = time.monotonic()
        if node.address and now - node.resolved_at < RESOLVE_INTERVAL:
            return node.address
        try:
            node.address = await async_resolve(self.hass, node.host)
            node.resolved_at = now
        except OSError as err:
            _LOGGER.debug("Cannot resolve %s: %s", node.host, err)
            seen = self.table.nodes.get(node.mac)
            return node.address or (seen.address if seen else None)
        return node.address

    # Reports

    @callback
    def async_handle_datagram(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        self.stats["packets"] += 1
        try:
            packet = parse_packet(data)
        except ProtocolError as err:
            self.stats["invalid"] += 1
            _LOGGER.debug("Bad packet from %s: %s", addr[0], err)
            return
        if not isinstance(packet, (LinkReport, HiveReport)):  # raw CSI, or a version or type we do not know
            self.stats["ignored"] += 1
            return
        if packet.node not in self.nodes:
            self.stats["unknown_node"] += 1
            return
        if isinstance(packet, HiveReport):
            self.stats["hive_reports" if self.hive.apply(packet, self.clock()) else "duplicates"] += 1
            return
        applied = self.table.apply(packet, self.clock(), addr[0])
        if applied is None:
            self.stats["duplicates"] += 1
            return
        self.stats["reports"] += 1
        keys, new = applied
        for key in new:
            for listener in list(self._new_link_listeners):
                listener(key)
        for key in keys:
            self._notify(key)

    @callback
    def _async_expire_tick(self, _now: Any = None) -> None:
        for key in self.table.expire(self.clock()):
            self._notify(key)

    def _notify(self, key: LinkKey) -> None:
        for listener in list(self._link_listeners.get(key, ())):
            listener()

    # Entities

    @callback
    def async_listen_new_links(self, listener: Callable[[LinkKey], None]) -> CALLBACK_TYPE:
        """Call listener for every link seen so far and for each new one."""
        self._new_link_listeners.append(listener)
        for key in list(self.table.links):
            if key[1] in self.nodes:
                listener(key)
        return lambda: self._new_link_listeners.remove(listener)

    @callback
    def async_listen_link(self, key: LinkKey, listener: Callable[[], None]) -> CALLBACK_TYPE:
        self._link_listeners.setdefault(key, []).append(listener)

        def remove() -> None:
            listeners = self._link_listeners.get(key)
            if listeners and listener in listeners:
                listeners.remove(listener)
                if not listeners:
                    del self._link_listeners[key]

        return remove

    def link(self, key: LinkKey) -> LinkState | None:
        return self.table.links.get(key)

    def link_available(self, key: LinkKey) -> bool:
        link = self.table.links.get(key)
        return self.transport is not None and link is not None and link.fresh

    def link_name(self, key: LinkKey) -> str:
        """Links are named by their transmitter: the access point, or the other node."""
        transmitter = key[0]
        link = self.table.links.get(key)
        if link is not None and link.kind == KIND_AP:
            return f"AP {transmitter}"
        node = self.nodes.get(transmitter)
        return node.name if node else f"Node {transmitter}"

    def subentry_id(self, mac: str) -> str | None:
        node = self.nodes.get(mac)
        return node.subentry_id if node else None

    # Map

    def online(self, mac: str, now: float) -> bool:
        """The node reported within the timeout: links or hive."""
        seen = self.table.nodes.get(mac)
        fresh = seen is not None and now - seen.updated <= LINK_TIMEOUT
        return self.transport is not None and (fresh or self.hive.fresh(mac, now))

    def map_snapshot(self) -> dict[str, Any]:
        """The live map: nodes at their layout positions, access points, fresh links and the hive;
        once a room or floor is calibrated, the room per floor; with several floors or a floor plan,
        the nodes per floor and the positions on its plan."""
        now = self.clock()
        hive = self.hive.current(now)
        layout = hive.layout if hive else {}
        nodes = []
        for mac in sorted(set(self.nodes) | set(layout)):  # the layout may hold nodes not added yet
            node = self.nodes.get(mac)
            x, y = layout.get(mac, (None, None))
            nodes.append({
                "mac": mac,
                "name": node.name if node else default_node_name(mac),
                "online": node is not None and self.online(mac, now),
                "x": x,
                "y": y,
            })
        aps = access_points(self.table, hive, set(self.nodes))
        on_map = {n["mac"] for n in nodes} | set(aps)
        links = [
            {
                "transmitter": link.transmitter,
                "receiver": link.receiver,
                "kind": "ap" if link.kind == KIND_AP else "node",
                "score": None if link.score is None else round(link.score, 1),
                "motion": link.motion,
            }
            for _, link in sorted(self.table.links.items())
            if now - link.updated <= LINK_TIMEOUT and link.receiver in self.nodes and link.transmitter in on_map
        ]
        snapshot = {
            "nodes": nodes,
            "access_points": [
                {
                    "bssid": bssid,
                    "label": ap_label(bssid),
                    "heard_by": [{"node": mac, "rssi": rssi} for mac, rssi in heard],
                }
                for bssid, heard in aps.items()
            ],
            "links": links,
            "hive": _hive_summary(hive, now),
        }
        if (rooms := self.presence.snapshot()) is not None:
            snapshot["rooms"] = rooms
        if people := self.presence.people():
            snapshot["people"] = people
        if floors := self.presence.map_floors():
            snapshot["floors"] = floors
        return snapshot

    # Panel

    def panel_snapshot(self) -> dict[str, Any]:
        """The panel: every node with its area, floor and ESPHome device, the floors with their rooms
        and calibration, and the hive."""
        now = self.clock()
        hive = self.hive.current(now)
        layout = hive.layout if hive else {}
        presence = self.presence
        floor_of = {mac: key for key, floor in presence.floors.items() for mac in floor.nodes}
        nodes = []
        for mac in sorted(set(self.nodes) | set(layout)):  # the layout may hold nodes not added yet
            node = self.nodes.get(mac)
            floor = floor_of.get(mac)
            nodes.append({
                "mac": mac,
                "name": node.name if node else default_node_name(mac),
                "added": node is not None,
                "host": node.host if node else None,
                "area": node.area if node else None,
                "area_name": presence.area_name(node.area) if node and node.area else None,
                "floor": floor or None,
                "floor_name": None if floor is None else presence.floors[floor].name,
                "online": node is not None and self.online(mac, now),
                "placed": mac in layout,
                "device_id": esphome_device_id(self.hass, mac),
            })
        return {"nodes": nodes, **presence.panel(), "hive": _hive_summary(hive, now)}

    # Diagnostics

    def diagnostics(self) -> dict[str, Any]:
        now = self.clock()

        def age(updated: float) -> float:
            return round(now - updated, 1)

        nodes = []
        for node in self.nodes.values():
            seen = self.table.nodes.get(node.mac)
            nodes.append({
                "mac": node.mac,
                "host": node.host,
                "name": node.name,
                "area": node.area,
                "address": node.address,
                "seen": {
                    "address": seen.address,
                    "seconds_ago": age(seen.updated),
                    "reports": seen.reports,
                    "lost": seen.lost,
                    "seq": seen.seq,
                    "uptime": seen.uptime,
                } if seen else None,
                "last_report": asdict(seen.last_report) if seen and seen.last_report else None,
            })
        links = [
            {**{k: v for k, v in asdict(link).items() if k != "updated"}, "seconds_ago": age(link.updated)}
            for link in self.table.links.values()
        ]
        hive = self.hive.current(now)
        return {
            "hub": {
                "running": self.transport is not None,
                "port": self.port,
                "stats": dict(self.stats),
                "subscribe_interval_s": SUBSCRIBE_INTERVAL.total_seconds(),
                "link_timeout_s": LINK_TIMEOUT,
            },
            "nodes": nodes,
            "links": links,
            "hive": {
                "reporter": hive.reporter,
                "seq": hive.seq,
                "hash": f"{hive.hash:08x}",
                "in_sync": hive.in_sync,
                "truncated": hive.truncated,
                "seconds_ago": age(hive.updated),
                "layout": {mac: list(xy) for mac, xy in hive.layout.items()},
                "rows": [asdict(row) for row in hive.rows.values()],
            } if hive else None,
            "rooms": self.presence.diagnostics(),
        }


def _hive_summary(hive: HiveState | None, now: float) -> dict[str, Any] | None:
    """The hive on the map and the panel; the frontend counts its age on between updates."""
    return {
        "hash": f"{hive.hash:08x}",
        "in_sync": hive.in_sync,
        "nodes": len(hive.nodes),
        "age": round(now - hive.updated),
    } if hive else None


def ap_label(bssid: str) -> str:
    """Short map label: the last two bytes, the full BSSID is long on a phone."""
    return f"AP {bssid[-5:]}"


def default_node_name(mac: str) -> str:
    """Like the node's own ESPHome name: "Wisp" and the last 6 MAC digits."""
    return f"Wisp {mac.replace(':', '')[-6:]}"


def node_devices(hass: HomeAssistant, mac: str) -> list[dr.DeviceEntry]:
    """Devices with this MAC: the ESPHome one, and from Home Assistant 2026.9 Wisp's own."""
    dev_reg = dr.async_get(hass)
    connections = {(CONNECTION_NETWORK_MAC, mac)}
    if PER_ENTRY_DEVICES:
        return dev_reg.async_get_devices(connections=connections)
    device = dev_reg.async_get_device(connections=connections)
    return [device] if device else []


def esphome_device_id(hass: HomeAssistant, mac: str) -> str | None:
    """The node's ESPHome device, for a link to its page."""
    for device in node_devices(hass, mac):
        for entry_id in device.config_entries:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is not None and entry.domain == "esphome":
                return device.id
    return None


def _ipv4(host: str) -> bool:
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        return False
    return True


async def async_resolve(hass: HomeAssistant, host: str) -> str:
    """IPv4 address of host. Raises OSError when it cannot be found."""
    if _ipv4(host):
        return host
    infos = await hass.loop.getaddrinfo(host, NODE_PORT, family=socket.AF_INET, type=socket.SOCK_DGRAM)
    if not infos:
        raise OSError(f"no address for {host}")
    return infos[0][4][0]


async def async_probe(hass: HomeAssistant, host: str) -> str:
    """MAC of the Wisp node at host: subscribe from a temporary socket and wait for its first packet."""
    try:
        address = await async_resolve(hass, host)
    except OSError as err:
        raise ProbeError("cannot_resolve") from err
    found: asyncio.Future[str] = hass.loop.create_future()

    class _Probe(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
            try:
                packet = parse_packet(data)
            except ProtocolError:
                return
            if packet is not None and not found.done():
                found.set_result(packet.node)

    transport, _ = await hass.loop.create_datagram_endpoint(_Probe, local_addr=LISTEN, family=socket.AF_INET)
    try:
        for _ in range(PROBE_TRIES):
            transport.sendto(build_subscribe(STREAM_LINK_REPORTS), (address, NODE_PORT))
            done, _pending = await asyncio.wait({found}, timeout=1.0)
            if done:
                return found.result()
    finally:
        transport.close()
        found.cancel()
    raise ProbeError("no_answer")
