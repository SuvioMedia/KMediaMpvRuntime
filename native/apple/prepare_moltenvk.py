#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Select, thin, and normalize one audited dynamic MoltenVK Apple slice."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


TARGETS = {
    "ios-arm64": (
        "MoltenVK/MoltenVK/dynamic/MoltenVK.xcframework/"
        "ios-arm64/MoltenVK.framework/MoltenVK",
        "ios",
        False,
    ),
    "ios-simulator-arm64": (
        "MoltenVK/MoltenVK/dynamic/MoltenVK.xcframework/"
        "ios-arm64_x86_64-simulator/MoltenVK.framework/MoltenVK",
        "iossim",
        True,
    ),
    "macos-aarch64": (
        "MoltenVK/MoltenVK/dynamic/MoltenVK.xcframework/"
        "macos-arm64_x86_64/MoltenVK.framework/Versions/A/MoltenVK",
        "macos",
        True,
    ),
}
VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+)+$")
MAX_ARCHIVE_MEMBERS = 2_000
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_BINARY_BYTES = 256 * 1024 * 1024


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def checked_member(archive: tarfile.TarFile, expected: str) -> tarfile.TarInfo:
    members = archive.getmembers()
    if not members or len(members) > MAX_ARCHIVE_MEMBERS:
        raise ValueError("MoltenVK archive member count is invalid")
    names: set[str] = set()
    total = 0
    selected: list[tarfile.TarInfo] = []
    for member in members:
        name = member.name
        relative = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or any(part.casefold().startswith(".env") for part in relative.parts)
            or name in names
            or not (member.isfile() or member.isdir() or member.issym())
            or member.size < 0
        ):
            raise ValueError("MoltenVK archive contains an unsafe member")
        if member.issym():
            link = PurePosixPath(member.linkname)
            if link.is_absolute() or ".." in link.parts or "\\" in member.linkname:
                raise ValueError("MoltenVK archive contains an unsafe symbolic link")
        names.add(name)
        total += member.size
        if total > MAX_ARCHIVE_BYTES:
            raise ValueError("MoltenVK archive expands beyond its size limit")
        if name == expected and member.isfile():
            selected.append(member)
    if len(selected) != 1 or selected[0].size > MAX_BINARY_BYTES:
        raise ValueError("MoltenVK target binary is missing or oversized")
    return selected[0]


def run(*command: str) -> str:
    result = subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )
    return result.stdout


def prepare(
    archive_path: Path,
    prefix: Path,
    target: str,
    minimum_os: str,
    sdk_version: str,
    evidence_path: Path,
) -> dict:
    if target not in TARGETS or not VERSION.fullmatch(minimum_os) or not VERSION.fullmatch(sdk_version):
        raise ValueError("MoltenVK target or version metadata is invalid")
    if archive_path.is_symlink() or not archive_path.is_file():
        raise ValueError("MoltenVK archive must be a regular file")
    if prefix.is_symlink() or not prefix.is_dir():
        raise ValueError("MoltenVK prefix must be an existing directory")
    library_dir = prefix / "lib"
    pkgconfig_dir = library_dir / "pkgconfig"
    library_dir.mkdir(parents=True, exist_ok=True)
    pkgconfig_dir.mkdir(parents=True, exist_ok=True)
    destination = library_dir / "libkmediampv_moltenvk.dylib"
    pkgconfig = pkgconfig_dir / "vulkan.pc"
    if destination.exists() or destination.is_symlink() or pkgconfig.exists() or pkgconfig.is_symlink():
        raise ValueError("MoltenVK prefix output already exists")

    member_name, platform, must_thin = TARGETS[target]
    # Keep the staging file on the destination filesystem: os.replace is atomic
    # only within one volume (the build prefix may be on an external disk).
    with tempfile.TemporaryDirectory(prefix=".kmediampv-moltenvk-", dir=library_dir) as temporary_name:
        temporary = Path(temporary_name)
        selected = temporary / "selected"
        with tarfile.open(archive_path, mode="r:") as archive:
            member = checked_member(archive, member_name)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("MoltenVK target binary cannot be read")
            with source, selected.open("xb") as output:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    output.write(block)
        selected.chmod(0o755)
        architecture = selected
        if must_thin:
            architecture = temporary / "arm64"
            run("xcrun", "lipo", str(selected), "-thin", "arm64", "-output", str(architecture))
        normalized = temporary / "normalized"
        run(
            "xcrun",
            "vtool",
            "-set-build-version",
            platform,
            minimum_os,
            sdk_version,
            "-replace",
            "-output",
            str(normalized),
            str(architecture),
        )
        normalized.chmod(0o755)
        os.replace(normalized, destination)
    run(
        "install_name_tool",
        "-id",
        "@rpath/libkmediampv_moltenvk.dylib",
        str(destination),
    )
    if target == "macos-aarch64":
        run(
            "codesign",
            "--force",
            "--sign",
            "-",
            "--timestamp=none",
            str(destination),
        )

    pkgconfig.write_text(
        "prefix=" + str(prefix) + "\n"
        "exec_prefix=${prefix}\n"
        "libdir=${exec_prefix}/lib\n"
        "includedir=${prefix}/include\n\n"
        "Name: Vulkan\n"
        "Description: Audited MoltenVK Vulkan implementation for KMediaMpv Apple targets\n"
        "Version: 1.4.337\n"
        "Libs: -L${libdir} -lkmediampv_moltenvk\n"
        "Cflags: -I${includedir}\n",
        encoding="utf-8",
    )
    inventory = {
        "schemaVersion": 1,
        "target": target,
        "archiveSha256": digest(archive_path),
        "selectedMember": member_name,
        "architecture": "arm64",
        "minimumOs": minimum_os,
        "sdkVersion": sdk_version,
        "installName": run("otool", "-D", str(destination)).splitlines()[-1].strip(),
        "sha256": digest(destination),
    }
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return inventory


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--target", choices=sorted(TARGETS), required=True)
    parser.add_argument("--minimum-os", required=True)
    parser.add_argument("--sdk-version", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    prepare(
        args.archive.resolve(strict=True),
        args.prefix.resolve(strict=True),
        args.target,
        args.minimum_os,
        args.sdk_version,
        args.evidence.absolute(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
