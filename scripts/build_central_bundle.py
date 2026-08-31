#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Fail-closed Maven Central bundle builder for the two public KMediaMpv runtime artifacts."""

from __future__ import annotations

import argparse
import hashlib
import re
import time
import zipfile
from pathlib import Path


SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?")
ARTIFACTS = {
    "kmedia-mpv-lgpl-runtime-android": "aar",
    "kmedia-mpv-lgpl-runtime-desktop": "jar",
}
GENERATED_CHECKSUM_SUFFIXES = (".md5", ".sha1", ".sha256", ".sha512")


def checksum(path: Path, algorithm: str) -> str:
    value = hashlib.new(algorithm)
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def unsigned_files(root: Path, version: str) -> set[Path]:
    result: set[Path] = set()
    for artifact, extension in ARTIFACTS.items():
        directory = root / "cc/suviomedia" / artifact / version
        prefix = f"{artifact}-{version}"
        required = {
            directory / f"{prefix}.{extension}",
            directory / f"{prefix}.module",
            directory / f"{prefix}.pom",
            directory / f"{prefix}-sources.jar",
            directory / f"{prefix}-javadoc.jar",
            directory / f"{prefix}-corresponding-source.tar.gz",
        }
        for path in required:
            if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
                raise ValueError(f"staged Maven artifact is missing: {path.relative_to(root)}")
            result.add(path)
    return result


def expected_files(root: Path, version: str) -> set[Path]:
    result: set[Path] = set()
    for path in unsigned_files(root, version):
        signature = path.with_name(path.name + ".asc")
        if not signature.is_file() or signature.is_symlink() or signature.stat().st_size == 0:
            raise ValueError(f"PGP signature is missing: {signature.relative_to(root)}")
        result.update((path, signature))
    return result


def normalize_staging(root: Path, version: str) -> None:
    """Remove only Gradle's mutable metadata and generated checksum sidecars."""
    expected = unsigned_files(root, version)
    generated: set[Path] = set()
    for artifact in ARTIFACTS:
        artifact_root = root / "cc/suviomedia" / artifact
        metadata = artifact_root / "maven-metadata.xml"
        generated.add(metadata)
        for path in (*expected, metadata):
            if path.is_relative_to(artifact_root):
                for suffix in GENERATED_CHECKSUM_SUFFIXES:
                    generated.add(path.with_name(path.name + suffix))
    for path in sorted(generated):
        if path.is_symlink():
            raise ValueError(f"refusing to normalize a generated symlink: {path.relative_to(root)}")
        if path.is_file():
            path.unlink()
    actual = {path for path in root.rglob("*") if path.is_file()}
    if actual != expected:
        extras = sorted(str(path.relative_to(root)) for path in actual - expected)
        raise ValueError(f"normalized Maven staging contains unexpected files: {extras}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--epoch", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--normalize", action="store_true")
    arguments = parser.parse_args()
    if not SEMVER.fullmatch(arguments.version):
        raise ValueError("version must be immutable SemVer")
    staging = arguments.staging.resolve()
    if not staging.is_dir() or staging.is_symlink():
        raise ValueError("staging must be a real directory")
    if any(path.is_symlink() for path in staging.rglob("*")):
        raise ValueError("staging contains a symbolic link")
    if arguments.normalize:
        if arguments.epoch is not None or arguments.output is not None:
            raise ValueError("normalization does not accept bundle output arguments")
        normalize_staging(staging, arguments.version)
        return 0
    if arguments.epoch is None or arguments.output is None:
        raise ValueError("bundle creation requires --epoch and --output")
    primary = expected_files(staging, arguments.version)
    allowed = set(primary)
    for path in sorted(primary):
        for algorithm in ("md5", "sha1"):
            sidecar = path.with_name(f"{path.name}.{algorithm}")
            sidecar.write_text(checksum(path, algorithm), encoding="ascii")
            allowed.add(sidecar)
    actual = {path for path in staging.rglob("*") if path.is_file()}
    if actual != allowed:
        extras = sorted(str(path.relative_to(staging)) for path in actual - allowed)
        raise ValueError(f"Maven staging contains unexpected files: {extras}")
    if arguments.output.exists() or arguments.output.is_symlink():
        raise ValueError("Central output must not exist")
    timestamp = tuple(time.gmtime(max(arguments.epoch, 315532800))[:6])
    with zipfile.ZipFile(arguments.output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(actual):
            info = zipfile.ZipInfo(path.relative_to(staging).as_posix(), timestamp)
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
