#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Assemble verified public desktop runtime payloads without duplicating the shared runtime."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


PLATFORMS = {"linux-x86_64", "linux-aarch64", "macos-aarch64", "windows-x86_64"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", action="append", default=[], metavar="PLATFORM=DIRECTORY")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    clients: dict[str, Path] = {}
    for value in arguments.client:
        platform, separator, directory = value.partition("=")
        if not separator or platform not in PLATFORMS or platform in clients:
            raise ValueError(f"invalid or duplicate client mapping: {value}")
        clients[platform] = Path(directory).resolve()
    if set(clients) != PLATFORMS:
        raise ValueError(f"desktop client set differs: {sorted(clients)}")
    output = arguments.output.resolve()
    if output.exists():
        raise ValueError("output already exists")
    for platform, source in sorted(clients.items()):
        subprocess.run(
            [sys.executable, "-B", root / "scripts/verify_client_output.py", "--output", source, "--target", platform],
            check=True,
        )
        destination = output / "META-INF/kmediampv/native" / platform
        destination.mkdir(parents=True)
        shutil.copyfile(source / "manifest.properties", destination / "manifest.properties")
        for library in sorted((source / "runtime").iterdir()):
            if not library.is_file() or library.is_symlink():
                raise ValueError("runtime payload contains a non-regular file")
            shutil.copyfile(library, destination / library.name)
    subprocess.run(
        [sys.executable, "-B", root / "scripts/verify_client_resources.py", "--resources", output],
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
