#!/usr/bin/env python3
"""Build the release manifest.json read by the web flasher and by every node's update entity.

Starts from the ESP Web Tools template (docs/flasher/manifest.json). For each build in it:
- `parts`: the factory image at offset 0, for first installs (ESP Web Tools).
- `ota`: the OTA image with its md5, release URL and summary, for updates (ESPHome
  `update` platform `http_request`).

Images are read from <images>/wisp-node-<chip>.factory.bin and .ota.bin, where <chip> is the
chipFamily in lower case without dashes (ESP32-S3 gives esp32s3). Paths in the manifest are
relative, so they resolve next to manifest.json wherever it is served.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
from pathlib import Path

IMAGE_PREFIX = "wisp-node-"
IMAGE_MAGIC = 0xE9
SUMMARY_MAX = 255  # Home Assistant caps update release summaries at 255 characters
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# chipFamily: (chip id in the ESP image header, bootloader offset inside a factory image)
CHIPS = {
    "ESP32": (0, 0x1000),
    "ESP32-S2": (2, 0x1000),
    "ESP32-S3": (9, 0x0),
    "ESP32-C2": (12, 0x0),
    "ESP32-C3": (5, 0x0),
    "ESP32-C5": (23, 0x2000),
    "ESP32-C6": (13, 0x0),
    "ESP32-H2": (16, 0x0),
    "ESP32-P4": (18, 0x2000),
}


def fail(message: str) -> None:
    sys.exit(f"make_manifest: {message}")


def image_name(chip_family: str, kind: str) -> str:
    return f"{IMAGE_PREFIX}{chip_family.lower().replace('-', '')}.{kind}.bin"


def read_image(path: Path, chip_family: str, header_offset: int) -> bytes:
    """Read an image and check its ESP image header names the expected chip."""
    if not path.is_file():
        fail(f"missing image {path}")
    data = path.read_bytes()
    if len(data) < header_offset + 16 or data[header_offset] != IMAGE_MAGIC:
        fail(f"{path.name} has no ESP image header at {header_offset:#x}")
    chip_id = struct.unpack_from("<H", data, header_offset + 12)[0]
    expected = CHIPS[chip_family][0]
    if chip_id != expected:
        fail(f"{path.name} is built for chip id {chip_id}, {chip_family} is {expected}")
    return data


def make_manifest(template: dict, images: Path, version: str, release_url: str, summary: str) -> dict:
    for key in ("name", "builds"):
        if key not in template:
            fail(f"template has no {key!r}")
    manifest = {**template, "version": version}
    summary = summary.strip()
    if summary in ("", version, f"v{version}"):  # a release titled by its tag says nothing more
        summary = f"{template['name']} {version}"
    summary = summary[:SUMMARY_MAX]

    builds = []
    for build in template["builds"]:
        chip_family = build.get("chipFamily")
        if chip_family not in CHIPS:
            fail(f"unknown chipFamily {chip_family!r}, add it to CHIPS")
        factory = image_name(chip_family, "factory")
        ota = image_name(chip_family, "ota")
        read_image(images / factory, chip_family, CHIPS[chip_family][1])
        ota_data = read_image(images / ota, chip_family, 0)

        out = {key: value for key, value in build.items() if key not in ("parts", "ota")}
        out["parts"] = [{"path": factory, "offset": 0}]
        out["ota"] = {
            "path": ota,
            "md5": hashlib.md5(ota_data).hexdigest(),
            "sha256": hashlib.sha256(ota_data).hexdigest(),  # unused by ESPHome 2026.9, kept for later
            "release_url": release_url,
            "summary": summary,
        }
        builds.append(out)

    manifest["builds"] = builds
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--template", type=Path, required=True, help="docs/flasher/manifest.json")
    parser.add_argument("--images", type=Path, required=True, help="folder with the built images")
    parser.add_argument("--version", required=True, help="X.Y.Z, a leading v is dropped")
    parser.add_argument("--release-url", required=True, help="GitHub release page")
    parser.add_argument("--summary", default="", help="short release summary shown in Home Assistant")
    parser.add_argument("--output", type=Path, help="where to write manifest.json (default: stdout only)")
    args = parser.parse_args()

    # Nodes compare this string with their project_version, so it must match it exactly.
    version = args.version.removeprefix("v")
    if not VERSION_RE.match(version):
        fail(f"version {args.version!r} is not X.Y.Z")
    if not args.release_url.startswith("https://"):
        fail(f"release URL {args.release_url!r} is not https")

    template = json.loads(args.template.read_text(encoding="utf-8"))
    manifest = make_manifest(template, args.images, version, args.release_url, args.summary)
    text = json.dumps(manifest, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
