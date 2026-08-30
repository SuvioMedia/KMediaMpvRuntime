#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Validate and query the Apple-only MoltenVK/SPIR-V dependency pins."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tarfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse


EXPECTED_COMPONENTS = {"glslang", "moltenvk"}
EXPECTED_RUNTIME_LIBRARIES = {"moltenvk"}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def load_manifest(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read Apple dependency manifest: {error}") from error
    if not isinstance(value, dict) or set(value) != {"schemaVersion", "components"}:
        raise ValueError("Apple dependency manifest differs from the closed schema")
    if value["schemaVersion"] != 1 or not isinstance(value["components"], dict):
        raise ValueError("Apple dependency manifest identity is invalid")
    components = value["components"]
    if set(components) != EXPECTED_COMPONENTS:
        raise ValueError("Apple-only component inventory differs from policy")

    runtime_libraries: list[str] = []
    archive_names: set[str] = set()
    notice_names: set[str] = set()
    for name, component in components.items():
        expected_fields = {
            "version",
            "licenseSpdx",
            "sourceUrl",
            "sourceSha256",
            "sourceArchive",
            "copyrightFile",
            "noticeFile",
            "buildSystem",
            "buildArguments",
            "runtimeLibraries",
            "binaryDistribution",
        }
        if not isinstance(component, dict) or set(component) != expected_fields:
            raise ValueError(f"{name}: Apple dependency fields differ from policy")
        for field in ("version", "licenseSpdx", "buildSystem"):
            if not isinstance(component[field], str) or not component[field]:
                raise ValueError(f"{name}: {field} is invalid")
        source_url = urlparse(component["sourceUrl"])
        if source_url.scheme != "https" or not source_url.netloc:
            raise ValueError(f"{name}: source URL must use HTTPS")
        if not SHA256.fullmatch(component["sourceSha256"]):
            raise ValueError(f"{name}: source SHA-256 is invalid")
        for field in ("sourceArchive", "noticeFile"):
            item = component[field]
            if not isinstance(item, str) or not SAFE_NAME.fullmatch(item):
                raise ValueError(f"{name}: {field} is invalid")
        copyright_path = component["copyrightFile"]
        if (
            not isinstance(copyright_path, str)
            or not copyright_path
            or PurePosixPath(copyright_path).is_absolute()
            or ".." in PurePosixPath(copyright_path).parts
            or "\\" in copyright_path
        ):
            raise ValueError(f"{name}: copyright path is unsafe")
        if component["sourceArchive"] in archive_names:
            raise ValueError("Apple dependency source archives are not unique")
        archive_names.add(component["sourceArchive"])
        if component["noticeFile"] in notice_names:
            raise ValueError("Apple dependency notice files are not unique")
        notice_names.add(component["noticeFile"])
        arguments = component["buildArguments"]
        if (
            not isinstance(arguments, list)
            or not arguments
            or len(arguments) != len(set(arguments))
            or any(not isinstance(item, str) or not item or "\n" in item for item in arguments)
        ):
            raise ValueError(f"{name}: build arguments are invalid")
        libraries = component["runtimeLibraries"]
        if (
            not isinstance(libraries, list)
            or len(libraries) != len(set(libraries))
            or any(not isinstance(item, str) or not SAFE_NAME.fullmatch(item) for item in libraries)
        ):
            raise ValueError(f"{name}: runtime libraries are invalid")
        runtime_libraries.extend(libraries)

        binary = component["binaryDistribution"]
        if name == "glslang":
            if binary is not None or libraries:
                raise ValueError("glslang must remain a source-built static dependency")
        else:
            if not isinstance(binary, dict) or set(binary) != {"url", "sha256", "archive"}:
                raise ValueError("MoltenVK binary distribution schema is invalid")
            parsed = urlparse(binary["url"])
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("MoltenVK binary URL must use HTTPS")
            if not SHA256.fullmatch(binary["sha256"]):
                raise ValueError("MoltenVK binary SHA-256 is invalid")
            if not isinstance(binary["archive"], str) or not SAFE_NAME.fullmatch(binary["archive"]):
                raise ValueError("MoltenVK binary archive name is invalid")
            if binary["archive"] in archive_names:
                raise ValueError("Apple dependency archives are not unique")
            archive_names.add(binary["archive"])

    if set(runtime_libraries) != EXPECTED_RUNTIME_LIBRARIES:
        raise ValueError("Apple-only runtime library inventory differs from policy")
    return value


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def verify_archives(manifest: dict, directory: Path) -> None:
    for component in manifest["components"].values():
        candidates = [
            (component["sourceArchive"], component["sourceSha256"]),
        ]
        binary = component["binaryDistribution"]
        if binary is not None:
            candidates.append((binary["archive"], binary["sha256"]))
        for archive, expected in candidates:
            path = directory / archive
            if not path.is_file() or path.is_symlink() or digest(path) != expected:
                raise ValueError(f"Apple dependency archive is missing or changed: {archive}")


def extract_copyright(archive_path: Path, relative: str, output: Path) -> None:
    if archive_path.is_symlink() or not archive_path.is_file():
        raise ValueError("Apple dependency source archive is invalid")
    wanted = PurePosixPath(relative)
    matches: list[tarfile.TarInfo] = []
    with tarfile.open(archive_path, mode="r:*") as archive:
        members = archive.getmembers()
        if not members or len(members) > 100_000:
            raise ValueError("Apple dependency source archive inventory is invalid")
        for member in members:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in member.name
                or any(part.casefold().startswith(".env") for part in path.parts)
            ):
                raise ValueError("Apple dependency source archive contains an unsafe path")
            if member.isfile() and len(path.parts) > 1 and PurePosixPath(*path.parts[1:]) == wanted:
                matches.append(member)
        if len(matches) != 1 or matches[0].size < 1 or matches[0].size > 1024 * 1024:
            raise ValueError("Apple dependency copyright file is missing or oversized")
        source = archive.extractfile(matches[0])
        if source is None:
            raise ValueError("Apple dependency copyright file cannot be read")
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists() or output.is_symlink():
            raise ValueError("Apple dependency copyright output already exists")
        with source, output.open("xb") as destination:
            destination.write(source.read())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    subparsers.add_parser("plan")
    source_plan = subparsers.add_parser("source-plan")
    source_plan.add_argument("--component")
    binary_plan = subparsers.add_parser("binary-plan")
    binary_plan.add_argument("--component")
    value = subparsers.add_parser("value")
    value.add_argument("component", choices=sorted(EXPECTED_COMPONENTS))
    value.add_argument("field")
    arguments = subparsers.add_parser("build-arguments")
    arguments.add_argument("component", choices=sorted(EXPECTED_COMPONENTS))
    copyright_files = subparsers.add_parser("copyright-files")
    copyright_files.add_argument("component", choices=sorted(EXPECTED_COMPONENTS))
    extract = subparsers.add_parser("extract-copyright")
    extract.add_argument("component", choices=sorted(EXPECTED_COMPONENTS))
    extract.add_argument("--archive", type=Path, required=True)
    extract.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify-archives")
    verify.add_argument("--archives", type=Path, required=True)
    args = parser.parse_args()

    try:
        manifest = load_manifest(args.manifest)
        components = manifest["components"]
        if args.command == "validate":
            return 0
        if args.command in {"plan", "source-plan"}:
            for name, component in sorted(components.items()):
                if args.command == "source-plan" and args.component not in {None, name}:
                    continue
                print("\t".join((name, "source", component["sourceArchive"], component["sourceUrl"], component["sourceSha256"])))
            if args.command == "source-plan":
                return 0
        if args.command in {"plan", "binary-plan"}:
            for name, component in sorted(components.items()):
                if args.command == "binary-plan" and args.component not in {None, name}:
                    continue
                binary = component["binaryDistribution"]
                if binary is not None:
                    print("\t".join((name, "binary", binary["archive"], binary["url"], binary["sha256"])))
            return 0
        if args.command == "value":
            current = components[args.component]
            for segment in args.field.split("."):
                if not isinstance(current, dict) or segment not in current:
                    raise ValueError("unknown Apple dependency value")
                current = current[segment]
            if isinstance(current, (str, int)):
                print(current)
            else:
                raise ValueError("Apple dependency value must be scalar")
            return 0
        if args.command == "build-arguments":
            print("\n".join(components[args.component]["buildArguments"]))
            return 0
        if args.command == "copyright-files":
            component = components[args.component]
            print(f"{component['copyrightFile']}\t{component['noticeFile']}")
            return 0
        if args.command == "extract-copyright":
            component = components[args.component]
            extract_copyright(
                args.archive.resolve(strict=True),
                component["copyrightFile"],
                args.output.absolute(),
            )
            return 0
        if args.command == "verify-archives":
            verify_archives(manifest, args.archives)
            return 0
    except (KeyError, OSError, ValueError) as error:
        print(f"Apple dependency manifest error: {error}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
