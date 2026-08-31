#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Fetch the closed, hash-pinned public source/build inputs for one release run."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_COMPONENTS = {
    "fast_float",
    "glslang",
    "jinja2",
    "libplacebo",
    "markupsafe",
    "moltenvk",
    "mpv",
    "vulkan_headers",
}
SHA256 = re.compile(r"[0-9a-f]{64}")


def records() -> list[dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    components: set[str] = set()
    for path in sorted((ROOT / "compliance/components").glob("*.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        name = manifest.get("name")
        if not isinstance(name, str) or name in components or name not in EXPECTED_COMPONENTS:
            raise ValueError(f"unexpected or duplicate component manifest: {path}")
        components.add(name)
        record = {
            "archive": manifest.get("sourceArchive", ""),
            "url": manifest.get("sourceUrl", ""),
            "sha256": manifest.get("sourceSha256", ""),
            "purpose": f"{name}-source",
        }
        if not isinstance(record["archive"], str) or record["archive"] in result:
            raise ValueError(f"duplicate or invalid source archive filename: {record['archive']}")
        result[record["archive"]] = record
    if components != EXPECTED_COMPONENTS:
        raise ValueError("component source inventory differs from the closed allow-list")
    dependencies = json.loads(
        (ROOT / "native/apple/dependencies.json").read_text(encoding="utf-8")
    )
    binary = dependencies["components"]["moltenvk"]["binaryDistribution"]
    binary_record = {
        "archive": binary["archive"],
        "url": binary["url"],
        "sha256": binary["sha256"],
        "purpose": "moltenvk-audited-binary-input",
    }
    if binary_record["archive"] in result:
        raise ValueError("MoltenVK source and binary archives share a filename")
    result[binary_record["archive"]] = binary_record
    for archive, record in result.items():
        parsed = urlparse(record["url"])
        if (
            not isinstance(record["url"], str)
            or not isinstance(record["sha256"], str)
            or not archive
            or Path(archive).name != archive
            or parsed.scheme != "https"
            or not parsed.netloc
            or not SHA256.fullmatch(record["sha256"])
        ):
            raise ValueError(f"invalid pinned release input: {record}")
    return [result[name] for name in sorted(result)]


def fetch(output: Path) -> None:
    if output.exists() or output.is_symlink():
        raise ValueError("source archive output must not exist")
    output.mkdir(parents=True)
    inventory = records()
    for record in inventory:
        destination = output / record["archive"]
        temporary = destination.with_name(destination.name + ".part")
        request = urllib.request.Request(
            record["url"], headers={"User-Agent": "KMediaMpvRuntime-release/1"}
        )
        digest = hashlib.sha256()
        try:
            with urllib.request.urlopen(request, timeout=120) as response, temporary.open("xb") as target:
                while block := response.read(1024 * 1024):
                    digest.update(block)
                    target.write(block)
            if digest.hexdigest() != record["sha256"]:
                raise ValueError(f"source archive hash differs: {record['archive']}")
            temporary.replace(destination)
        finally:
            if temporary.exists():
                temporary.unlink()
    (output / "SOURCE-ARCHIVES.json").write_text(
        json.dumps({"schemaVersion": 1, "archives": inventory}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    fetch(arguments.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
