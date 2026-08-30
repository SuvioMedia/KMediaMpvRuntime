#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse


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
EXPECTED_RUNTIME_LIBRARIES = {
    "moltenvk",
    "mpv",
    "placebo",
}
ALLOWED_LINKAGES = {"dynamic", "header-only", "build-only"}
ALLOWED_BUILD_SYSTEMS = {
    "audited-upstream-xcframework",
    "cmake-static",
    "configure-make",
    "header-only",
    "meson",
    "python-build-tool",
}
ALLOWED_LICENSES = {
    "Apache-2.0",
    "BSL-1.0",
    "BSD-3-Clause",
    "FTL",
    "ISC",
    "LGPL-2.1-or-later",
    "MIT",
}
CONDITIONAL_SOURCE_LICENSE = "LGPL-2.1-or-later AND GPL-2.0-or-later"
FAST_FLOAT_SOURCE_LICENSE = "Apache-2.0 OR MIT OR BSL-1.0"
EXPECTED_SPECIAL_COPYRIGHT_FILES = {
    "fast_float": {
        ("LICENSE-APACHE", "fast_float-apache.txt"),
        ("LICENSE-BOOST", "fast_float-boost.txt"),
        ("LICENSE-MIT", "fast_float-mit.txt"),
        ("README.md", "fast_float-wuffs-readme.txt"),
    },
    "harfbuzz": {
        ("COPYING", "harfbuzz.txt"),
        ("src/ms-use/COPYING", "harfbuzz-ms-use.txt"),
    },
}
NOTICE_FILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]*\.txt$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def load_manifests(directory: Path) -> dict[str, dict]:
    manifests: dict[str, dict] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Cannot read component manifest {path}: {error}") from error
        name = value.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"Component manifest {path} has no valid name")
        if name in manifests:
            raise ValueError(f"Duplicate component manifest for {name}")
        manifests[name] = value
    return manifests


def validate_manifests(manifests: dict[str, dict]) -> None:
    names = set(manifests)
    if names != EXPECTED_COMPONENTS:
        raise ValueError(
            "Component inventory differs from the audited allowlist: "
            f"missing={sorted(EXPECTED_COMPONENTS - names)}, "
            f"unexpected={sorted(names - EXPECTED_COMPONENTS)}"
        )

    runtime_libraries: list[str] = []
    archive_names: set[str] = set()
    for name, manifest in manifests.items():
        required = {
            "schemaVersion",
            "name",
            "version",
            "upstreamLicenseSummarySpdx",
            "builtOutputLicenseSpdx",
            "linkage",
            "sourceUrl",
            "sourceSha256",
            "sourceArchive",
            "copyrightFiles",
            "buildSystem",
            "buildArguments",
            "runtimeLibraries",
            "dependencies",
            "bundled",
        }
        missing = required - set(manifest)
        unknown = set(manifest) - required
        if missing or unknown:
            raise ValueError(
                f"{name}: component fields differ from the closed schema; "
                f"missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        if manifest["schemaVersion"] != 2:
            raise ValueError(f"{name}: unsupported schemaVersion")
        if manifest["upstreamLicenseSummarySpdx"] not in ALLOWED_LICENSES | {
            CONDITIONAL_SOURCE_LICENSE,
            FAST_FLOAT_SOURCE_LICENSE,
        }:
            raise ValueError(f"{name}: source license is not on the audited allowlist")
        if manifest["builtOutputLicenseSpdx"] not in ALLOWED_LICENSES:
            raise ValueError(f"{name}: built-output license is not on the audited allowlist")
        if name == "mpv" and manifest["upstreamLicenseSummarySpdx"] != CONDITIONAL_SOURCE_LICENSE:
            raise ValueError(f"{name}: conditional upstream source license changed")
        if name == "fast_float" and (
            manifest["upstreamLicenseSummarySpdx"] != FAST_FLOAT_SOURCE_LICENSE
            or manifest["builtOutputLicenseSpdx"] != "Apache-2.0"
        ):
            raise ValueError("fast_float: source grant or selected Apache-2.0 output scope changed")
        if name == "libplacebo" and manifest["builtOutputLicenseSpdx"] != "LGPL-2.1-or-later":
            raise ValueError("libplacebo: selected built-output license changed")
        if manifest["linkage"] not in ALLOWED_LINKAGES:
            raise ValueError(f"{name}: invalid linkage")
        if manifest["buildSystem"] not in ALLOWED_BUILD_SYSTEMS:
            raise ValueError(f"{name}: invalid buildSystem")

        parsed_url = urlparse(manifest["sourceUrl"])
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            raise ValueError(f"{name}: sourceUrl must be an absolute HTTPS URL")
        if not SHA256_PATTERN.fullmatch(manifest["sourceSha256"]):
            raise ValueError(f"{name}: sourceSha256 must be 64 lowercase hex characters")
        archive = manifest["sourceArchive"]
        if archive != Path(archive).name or not archive:
            raise ValueError(f"{name}: sourceArchive must be a basename")
        if archive in archive_names:
            raise ValueError(f"{name}: duplicate sourceArchive {archive}")
        archive_names.add(archive)
        copyright_files = manifest["copyrightFiles"]
        if not isinstance(copyright_files, list) or not copyright_files:
            raise ValueError(f"{name}: copyrightFiles must be a non-empty array")
        actual_copyright_files: set[tuple[str, str]] = set()
        for record in copyright_files:
            if not isinstance(record, dict) or set(record) != {"sourcePath", "noticeFile"}:
                raise ValueError(f"{name}: copyrightFiles has an unsupported closed schema")
            source_path = record["sourcePath"]
            notice_file = record["noticeFile"]
            if (
                not isinstance(source_path, str)
                or not source_path
                or PurePosixPath(source_path).is_absolute()
                or ".." in PurePosixPath(source_path).parts
                or "\\" in source_path
                or any(character in source_path for character in ("\n", "\r", "\t", "\x00"))
                or not isinstance(notice_file, str)
                or not NOTICE_FILE_PATTERN.fullmatch(notice_file)
                or PurePosixPath(notice_file).name != notice_file
            ):
                raise ValueError(f"{name}: copyrightFiles contains an unsafe path")
            pair = (source_path, notice_file)
            if pair in actual_copyright_files:
                raise ValueError(f"{name}: copyrightFiles contains a duplicate record")
            actual_copyright_files.add(pair)
        expected_copyright_files = EXPECTED_SPECIAL_COPYRIGHT_FILES.get(name)
        if (
            expected_copyright_files is not None
            and actual_copyright_files != expected_copyright_files
        ):
            raise ValueError(f"{name}: audited subtree notice/provenance inventory changed")

        arguments = manifest["buildArguments"]
        if not isinstance(arguments, list) or not arguments:
            raise ValueError(f"{name}: buildArguments must be a non-empty array")
        if any(not isinstance(argument, str) or not argument or "\n" in argument for argument in arguments):
            raise ValueError(f"{name}: buildArguments contains an invalid token")
        if len(arguments) != len(set(arguments)):
            raise ValueError(f"{name}: buildArguments contains duplicates")

        libraries = manifest["runtimeLibraries"]
        if not isinstance(libraries, list) or any(not isinstance(item, str) for item in libraries):
            raise ValueError(f"{name}: invalid runtimeLibraries")
        if manifest["linkage"] == "dynamic" and not libraries:
            raise ValueError(f"{name}: dynamic component has no runtime library")
        if manifest["linkage"] != "dynamic" and libraries:
            raise ValueError(f"{name}: non-runtime component declares a runtime library")
        runtime_libraries.extend(libraries)

        dependencies = manifest.get("dependencies", [])
        if not isinstance(dependencies, list) or any(item not in names for item in dependencies):
            raise ValueError(f"{name}: dependencies contain an unknown component")
        if name in dependencies:
            raise ValueError(f"{name}: self-dependency is forbidden")

    if len(runtime_libraries) != len(set(runtime_libraries)):
        raise ValueError("Runtime library logical names are not unique")
    if set(runtime_libraries) != EXPECTED_RUNTIME_LIBRARIES:
        raise ValueError(
            "Runtime library inventory differs from the audited allowlist: "
            f"actual={sorted(runtime_libraries)}"
        )
    notice_files = [
        record["noticeFile"]
        for manifest in manifests.values()
        for record in manifest["copyrightFiles"]
    ]
    if len(notice_files) != len(set(notice_files)):
        raise ValueError("Component copyright notice filenames are not globally unique")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise ValueError(f"Dependency cycle involving {name}")
        if name in visited:
            return
        visiting.add(name)
        for dependency in manifests[name].get("dependencies", []):
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in sorted(names):
        visit(name)


def verify_archives(manifests: dict[str, dict], archive_directory: Path) -> None:
    for name, manifest in sorted(manifests.items()):
        path = archive_directory / manifest["sourceArchive"]
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"{name}: source archive is missing or is a symlink: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != manifest["sourceSha256"]:
            raise ValueError(f"{name}: source archive SHA-256 mismatch")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    plan = subparsers.add_parser("plan")
    plan.add_argument("--archives", type=Path)
    arguments = subparsers.add_parser("arguments")
    arguments.add_argument("name")
    copyright_files = subparsers.add_parser("copyright-files")
    copyright_files.add_argument("name")
    value = subparsers.add_parser("value")
    value.add_argument("name")
    value.add_argument("field")
    verify = subparsers.add_parser("verify-archives")
    verify.add_argument("--archives", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        manifests = load_manifests(args.directory)
        validate_manifests(manifests)
        if args.command == "validate":
            return 0
        if args.command == "plan":
            for name in sorted(manifests):
                manifest = manifests[name]
                print(
                    "\t".join(
                        [
                            name,
                            manifest["sourceArchive"],
                            manifest["sourceUrl"],
                            manifest["sourceSha256"],
                        ]
                    )
                )
            return 0
        if args.command == "arguments":
            for argument in manifests[args.name]["buildArguments"]:
                print(argument)
            return 0
        if args.command == "copyright-files":
            for record in manifests[args.name]["copyrightFiles"]:
                print(f"{record['sourcePath']}\t{record['noticeFile']}")
            return 0
        if args.command == "value":
            value = manifests[args.name][args.field]
            if isinstance(value, (dict, list)):
                print(json.dumps(value, separators=(",", ":"), sort_keys=True))
            else:
                print(value)
            return 0
        if args.command == "verify-archives":
            verify_archives(manifests, args.archives)
            return 0
    except (KeyError, ValueError) as error:
        print(f"component manifest error: {error}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
