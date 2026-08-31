#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Generate a compact CycloneDX SBOM for the public KMediaMpv runtime payload."""

from __future__ import annotations

import argparse
import json
import re
import uuid
from pathlib import Path


SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?")


def properties(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="ISO-8859-1").splitlines()
        if line and not line.startswith(("#", "!"))
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--runtime-version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if not SEMVER.fullmatch(arguments.version) or not SEMVER.fullmatch(arguments.runtime_version):
        raise ValueError("versions must be immutable SemVer")
    manifest = properties(arguments.manifest.resolve())
    library_count = int(manifest["library.count"])
    root_component = {
        "type": "library",
        "bom-ref": f"pkg:maven/cc.suviomedia/kmedia-mpv-lgpl-runtime-desktop@{arguments.version}",
        "group": "cc.suviomedia",
        "name": "kmedia-mpv-lgpl-runtime-desktop",
        "version": arguments.version,
        "licenses": [{"license": {"id": "LGPL-2.1-or-later"}}],
    }
    runtime_component = {
        "type": "library",
        "bom-ref": f"pkg:maven/cc.suviomedia/kmedia-ffmpeg-runtime-desktop@{arguments.runtime_version}",
        "group": "cc.suviomedia",
        "name": "kmedia-ffmpeg-runtime-desktop",
        "version": arguments.runtime_version,
        "properties": [{"name": "kmedia:runtimeId", "value": manifest["sharedRuntimeId"]}],
        "licenses": [{"license": {"id": "LGPL-2.1-or-later"}}],
    }
    components = [runtime_component]
    for index in range(library_count):
        component = manifest[f"library.{index}.component"]
        component_version = {
            "mpv": manifest["mpvVersion"],
            "libplacebo": manifest["libplaceboVersion"],
            "moltenvk": manifest.get("moltenVkVersion", "client-build"),
        }.get(component, "client-build")
        components.append(
            {
                "type": "library",
                "bom-ref": f"pkg:generic/{component}@{component_version}",
                "name": component,
                "version": component_version,
                "hashes": [{"alg": "SHA-256", "content": manifest[f"library.{index}.sha256"]}],
            }
        )
    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": "urn:uuid:" + str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"https://github.com/SuvioMedia/KMediaMpvRuntime/{arguments.version}/{manifest['sharedRuntimeId']}")
        ),
        "version": 1,
        "metadata": {"component": root_component},
        "components": components,
        "dependencies": [
            {
                "ref": root_component["bom-ref"],
                "dependsOn": [component["bom-ref"] for component in components],
            }
        ],
    }
    arguments.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
