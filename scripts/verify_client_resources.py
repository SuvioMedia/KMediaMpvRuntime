#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Verify the closed desktop runtime-resource inventory and one shared runtime ID."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


PLATFORMS = {"linux-x86_64", "linux-aarch64", "macos-aarch64", "windows-x86_64"}
RUNTIME_ID = re.compile(r"sharedRuntimeId=(kmediaffmpeg-9\.0\.1-ass-0\.17\.5-[0-9a-f]{16})$")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resources", type=Path, required=True)
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    resources = arguments.resources.resolve()
    native = resources / "META-INF/kmediampv/native"
    if not native.is_dir() or {path.name for path in native.iterdir()} != PLATFORMS:
        raise ValueError("desktop resource platforms differ from policy")
    ids: set[str] = set()
    for platform in sorted(PLATFORMS):
        directory = native / platform
        temporary = directory.parent / (platform + ".verification")
        if temporary.exists():
            raise ValueError("temporary verification directory already exists")
        temporary.mkdir()
        try:
            (temporary / "runtime").mkdir()
            for path in directory.iterdir():
                if path.name == "manifest.properties":
                    target = temporary / path.name
                else:
                    target = temporary / "runtime" / path.name
                target.write_bytes(path.read_bytes())
            subprocess.run(
                [sys.executable, "-B", root / "scripts/verify_client_output.py", "--output", temporary, "--target", platform],
                check=True,
            )
            for line in (temporary / "manifest.properties").read_text(encoding="ISO-8859-1").splitlines():
                match = RUNTIME_ID.fullmatch(line)
                if match:
                    ids.add(match.group(1))
        finally:
            import shutil
            shutil.rmtree(temporary, ignore_errors=True)
    if len(ids) != 1:
        raise ValueError("desktop runtimes do not target exactly one shared runtime ID")
    meta = resources / "META-INF"
    actual_top = {path.name for path in meta.iterdir()}
    if actual_top != {"kmediampv"}:
        raise ValueError("desktop resource top-level inventory is not closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
