"""Config flow: one Wisp hub, nodes as subentries (found by zeroconf or added by address), and the
room presence options."""
from __future__ import annotations

from string import hexdigits
from types import MappingProxyType
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_IGNORE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryData,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, selector
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, format_mac
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import CONF_AREA, DOMAIN, MANUFACTURER, MODEL, PROJECT_NAME, SUBENTRY_NODE, TITLE
from .hub import ProbeError, async_probe, default_node_name, node_devices

NODE_SCHEMA = vol.Schema({
    vol.Required(CONF_HOST): str,
    vol.Optional(CONF_NAME): str,
    vol.Optional(CONF_AREA): selector.AreaSelector(),
})
# No settings left: the room tracker times presence itself (the presence hold it replaced kept a
# room occupied a minute after everyone left). The form says where the panel is.
OPTIONS_SCHEMA = vol.Schema({})


def hub_entry(hass: HomeAssistant) -> ConfigEntry | None:
    entry = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, DOMAIN)
    return entry if entry and entry.source != SOURCE_IGNORE else None


def _node_data(mac: str, host: str, name: str, area: str | None = None) -> dict[str, str]:
    """The area only when set: the room the node stands in, which gives its floor."""
    return {CONF_MAC: mac, CONF_HOST: host, CONF_NAME: name} | ({CONF_AREA: area} if area else {})


def _node_subentry(mac: str, host: str, name: str) -> ConfigSubentryData:
    return ConfigSubentryData(data=_node_data(mac, host, name), subentry_type=SUBENTRY_NODE, title=name, unique_id=mac)


def known_nodes(hass: HomeAssistant, first: dict[str, str] | None = None) -> list[ConfigSubentryData]:
    """The nodes a new hub starts with: the one found (first), then every Wisp node Home Assistant
    has as an ESPHome device. Home Assistant rediscovers only the node that created a removed hub,
    and the others stay quiet until they restart, so a hub set up again would miss them."""
    found = {first[CONF_MAC]: _node_subentry(**first)} if first else {}
    registry = dr.async_get(hass)
    for esphome in hass.config_entries.async_entries("esphome", include_ignore=False, include_disabled=False):
        if not (host := esphome.data.get(CONF_HOST)):
            continue
        for device in dr.async_entries_for_config_entry(registry, esphome.entry_id):
            mac = next((format_mac(v) for kind, v in device.connections if kind == CONNECTION_NETWORK_MAC), None)
            if mac and mac not in found and (device.manufacturer, device.model) == (MANUFACTURER, MODEL):
                found[mac] = _node_subentry(mac, host, device.name_by_user or device.name or default_node_name(mac))
    return list(found.values())


@callback
def async_add_or_update_node(hass: HomeAssistant, entry: ConfigEntry, mac: str, host: str, name: str) -> bool:
    """Add a node to the hub, or follow its new address. True when added."""
    for sub in entry.subentries.values():
        if sub.unique_id == mac:
            if sub.data.get(CONF_HOST) != host:
                hass.config_entries.async_update_subentry(entry, sub, data={**sub.data, CONF_HOST: host})
            return False
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=MappingProxyType(_node_data(mac, host, name)), subentry_type=SUBENTRY_NODE, title=name, unique_id=mac
        ),
    )
    return True


def _mdns_mac(value: Any) -> str | None:
    """ESPHome advertises the MAC as 12 lowercase hex digits."""
    if isinstance(value, str) and len(value) == 12 and all(c in hexdigits for c in value):
        return format_mac(value)
    return None


def _node_name(hass: HomeAssistant, mac: str) -> str:
    """The node's device name in Home Assistant (from ESPHome), else like ESPHome would name it."""
    for device in node_devices(hass, mac):
        if name := device.name_by_user or device.name:
            return name
    return default_node_name(mac)


class WispConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._node: dict[str, str] = {}

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_NODE: NodeSubentryFlow}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> WispOptionsFlow:
        return WispOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title=TITLE, data={}, subentries=known_nodes(self.hass))
        return self.async_show_form(step_id="user")

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """An ESPHome device with the Wisp project name (manifest matcher)."""
        props = discovery_info.properties
        mac = _mdns_mac(props.get("mac"))
        if str(props.get("project_name", "")).lower() != PROJECT_NAME or mac is None:
            return self.async_abort(reason="not_wisp_node")
        host = discovery_info.host
        name = props.get("friendly_name") or discovery_info.hostname.removesuffix(".").removesuffix(".local")

        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured()  # the user ignored this node

        if hub := hub_entry(self.hass):  # no question: join the hub, or follow a new address
            added = async_add_or_update_node(self.hass, hub, mac, host, name)
            return self.async_abort(reason="node_added" if added else "already_configured")

        self._node = {CONF_MAC: mac, CONF_HOST: host, CONF_NAME: name}
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """First node found: set up the hub with it. Later ones join without asking."""
        if hub := hub_entry(self.hass):  # set up meanwhile
            added = async_add_or_update_node(self.hass, hub, **self._node)
            return self.async_abort(reason="node_added" if added else "already_configured")
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=TITLE, data={}, subentries=known_nodes(self.hass, self._node))
        self._set_confirm_only()
        return self.async_show_form(
            step_id="discovery_confirm",
            description_placeholders={"name": self._node[CONF_NAME], "host": self._node[CONF_HOST]},
        )


class NodeSubentryFlow(ConfigSubentryFlow):
    """Add a node by host name or IP address, or change its address."""

    async def _async_probe(self, host: str) -> tuple[str | None, dict[str, str]]:
        try:
            return await async_probe(self.hass, host), {}
        except ProbeError as err:
            return None, {"base": str(err)}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            mac, errors = await self._async_probe(host)
            if mac:
                if any(sub.unique_id == mac for sub in self._get_entry().subentries.values()):
                    return self.async_abort(reason="already_configured")
                name = (user_input.get(CONF_NAME) or "").strip() or _node_name(self.hass, mac)
                data = _node_data(mac, host, name, user_input.get(CONF_AREA))
                return self.async_create_entry(title=name, data=data, unique_id=mac)
        return self.async_show_form(
            step_id="user", data_schema=self.add_suggested_values_to_schema(NODE_SCHEMA, user_input), errors=errors
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        sub = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            if host != sub.data[CONF_HOST]:
                mac, errors = await self._async_probe(host)
                if mac and mac != sub.data[CONF_MAC]:
                    errors = {"base": "different_node"}
            if not errors:
                name = (user_input.get(CONF_NAME) or "").strip() or sub.title
                kept = {k: v for k, v in sub.data.items() if k != CONF_AREA}
                area = user_input.get(CONF_AREA) or None
                hub = getattr(self._get_entry(), "runtime_data", None)
                if hub is not None:  # the area lives on the node's devices, as anywhere in Home Assistant
                    hub.async_set_node_area(sub.data[CONF_MAC], area)
                    area = None
                data = kept | _node_data(sub.data[CONF_MAC], host, name, area)
                return self.async_update_and_abort(self._get_entry(), sub, title=name, data=data)
        hub = getattr(self._get_entry(), "runtime_data", None)
        area = hub.node_area(sub.data[CONF_MAC], sub.data.get(CONF_AREA)) if hub else sub.data.get(CONF_AREA) or next(
            (d.area_id for d in node_devices(self.hass, sub.data[CONF_MAC]) if d.area_id), None
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                NODE_SCHEMA, user_input or {CONF_HOST: sub.data[CONF_HOST], CONF_NAME: sub.title, CONF_AREA: area}
            ),
            description_placeholders={"mac": sub.data[CONF_MAC]},
            errors=errors,
        )


class WispOptionsFlow(OptionsFlow):
    """Where Wisp's settings are: the panel, and each node's ESPHome device."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init", data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, self.config_entry.options)
        )
