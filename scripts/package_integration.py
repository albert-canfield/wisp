#!/usr/bin/env python3
"""Zip the integration for a manual install, stamped with the commit it was built from.

    python scripts/package_integration.py        -> dist/wisp-<version>+<commit>.zip

The zip holds custom_components/wisp/ as committed (git archive, so no caches or local edits);
its manifest version becomes <version>+<commit>, which Home Assistant shows on the integration
page, so two builds of the same version can be told apart. Releases use the plain version.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "custom_components/wisp/manifest.json"


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


def main() -> int:
    if git("status", "--porcelain", "custom_components/wisp").strip():
        print("custom_components/wisp has uncommitted changes: commit them first", file=sys.stderr)
        return 1
    commit = git("rev-parse", "--short=7", "HEAD").decode().strip()
    archive = zipfile.ZipFile(io.BytesIO(git("archive", "--format=zip", "HEAD", "custom_components/wisp")))
    manifest = json.loads(archive.read(MANIFEST))
    version = f"{manifest['version']}+{commit}"
    manifest["version"] = version
    out = ROOT / "dist" / f"wisp-{version}.zip"
    out.parent.mkdir(exist_ok=True)
    for old in out.parent.glob("wisp-*.zip"):
        old.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info in archive.infolist():
            data = archive.read(info.filename)
            if info.filename == MANIFEST:
                data = (json.dumps(manifest, indent=2) + "\n").encode()
            z.writestr(info, data)
    print(f"{out.relative_to(ROOT)}  (version {version}, {len(archive.infolist())} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
