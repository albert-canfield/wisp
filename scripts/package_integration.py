#!/usr/bin/env python3
"""Zip the integration for a manual install.

    python scripts/package_integration.py            -> dist/wisp-<version>.zip
    python scripts/package_integration.py --stamp    -> dist/wisp-<version>+<commit>.zip

The zip holds custom_components/wisp/ as committed (git archive, so no caches or local edits).
Each round of changes handed out gets its own version (bump it in manifest.json, const.py and
firmware/common/base.yaml). --stamp adds the commit to the manifest version, which Home
Assistant shows, for builds in between.
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
    version = f"{manifest['version']}+{commit}" if "--stamp" in sys.argv[1:] else manifest["version"]
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
