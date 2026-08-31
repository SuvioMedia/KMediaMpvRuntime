#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path


LIBRARIES = {"libkmediampv_mpv.so", "libkmediampv_placebo.so"}


def props(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if line and not line.startswith("#"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm64", type=Path, required=True)
    parser.add_argument("--armv7", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    inputs = [("arm64-v8a", args.arm64.resolve()), ("armeabi-v7a", args.armv7.resolve())]
    manifests = [(abi, source, props(source / "manifest.properties")) for abi, source in inputs]
    expected_platforms = {
        "arm64-v8a": "android-arm64-v8a",
        "armeabi-v7a": "android-armeabi-v7a",
    }
    if any(manifest.get("platform") != expected_platforms[abi] for abi, _, manifest in manifests):
        raise ValueError("Android MPV runtime slice targets another platform")
    ids = {manifest["sharedRuntimeId"] for _, _, manifest in manifests}
    runtime_versions = {manifest["sharedRuntimeVersion"] for _, _, manifest in manifests}
    revisions = {manifest["recipeRevision"] for _, _, manifest in manifests}
    source_offers = {manifest["sourceOffer"] for _, _, manifest in manifests}
    if any(manifest.get("licenseSpdx") != "LGPL-2.1-or-later" for _, _, manifest in manifests):
        raise ValueError("Android MPV runtime slices do not declare LGPL-2.1-or-later")
    if len(ids) != 1 or len(runtime_versions) != 1 or len(revisions) != 1 or len(source_offers) != 1:
        raise ValueError("Android MPV runtime slices do not bind one runtime, source, and recipe revision")
    output = args.output.resolve()
    if output.exists():
        raise ValueError("output already exists")
    values = [
        ("schemaVersion", "1"), ("licenseSpdx", "LGPL-2.1-or-later"),
        ("sharedRuntimeId", next(iter(ids))),
        ("sharedRuntimeVersion", next(iter(runtime_versions))),
        ("sourceOffer", next(iter(source_offers))),
        ("mpvVersion", "0.41.0"), ("libplaceboVersion", "7.360.1"),
        ("recipeRevision", next(iter(revisions))), ("abi.count", "2"),
    ]
    for index, (abi, source, _) in enumerate(manifests):
        actual = {path.name for path in (source / "runtime").iterdir() if path.is_file()}
        if actual != LIBRARIES:
            raise ValueError(f"Android MPV client inventory differs for {abi}")
        values.append((f"abi.{index}.name", abi))
        destination = output / "jni" / abi
        destination.mkdir(parents=True)
        for name in sorted(LIBRARIES):
            shutil.copyfile(source / "runtime" / name, destination / name)
            values.append((f"abi.{index}.{name}.sha256", sha256(destination / name)))
    (output / "manifest.properties").write_text(
        "\n".join(f"{key}={value}" for key, value in values) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
