"""Smoke test against real nodes: the Wisp integration inside a real Home Assistant core.

Not part of the pytest suite: it needs the homeassistant package and Wisp nodes on the network.
Run: python tests/smoke_live.py <mac>=<host> [<mac>=<host> ...] [--seconds 30]
It sets up a Wisp hub with those nodes, lets it run, then prints the entities it made, the map
feed and the diagnostics. Nothing is stored outside a temporary folder.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).parents[1]


async def main(nodes: list[tuple[str, str]], seconds: float) -> int:
    from homeassistant import loader
    from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigSubentryData
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import area_registry, device_registry, entity_registry, floor_registry

    config_dir = tempfile.mkdtemp(prefix="wisp-smoke-")
    os.makedirs(f"{config_dir}/custom_components")
    os.symlink(ROOT / "custom_components" / "wisp", f"{config_dir}/custom_components/wisp")
    hass = HomeAssistant(config_dir)
    for registry in (floor_registry, area_registry, device_registry, entity_registry):
        if hasattr(registry, "async_setup"):
            registry.async_setup(hass)
        await registry.async_load(hass)
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    await hass.async_start()

    entry = ConfigEntry(
        data={},
        discovery_keys={},
        domain="wisp",
        minor_version=1,
        options={},
        source="user",
        subentries_data=[
            ConfigSubentryData(data={"mac": mac, "host": host, "name": f"Node {mac[-5:]}"}, subentry_type="node",
                               title=f"Node {mac[-5:]}", unique_id=mac)
            for mac, host in nodes
        ],
        title="Wisp",
        unique_id="wisp",
        version=1,
    )
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    print(f"entry state: {entry.state}")
    await asyncio.sleep(seconds)

    hub = entry.runtime_data
    states = sorted((s for s in hass.states.async_all() if s.domain in ("sensor", "binary_sensor")), key=lambda s: s.entity_id)
    print(f"\n{len(states)} entities with a state:")
    for s in states:
        print(f"  {s.entity_id:60s} {s.state}")
    print("\nmap feed:")
    print(json.dumps(hub.map_snapshot(), indent=1, default=str)[:3000])
    print("\nhub stats:", hub.stats)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_stop()
    return 0 if hub.stats.get("reports") else 1


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("nodes", nargs="+", help="mac=host pairs, for example ac:27:6e:a8:c7:7c=wisp-a8c77c.local")
    ap.add_argument("--seconds", type=float, default=30)
    args = ap.parse_args()
    pairs = [tuple(n.split("=", 1)) for n in args.nodes]
    sys.exit(asyncio.run(main(pairs, args.seconds)))
