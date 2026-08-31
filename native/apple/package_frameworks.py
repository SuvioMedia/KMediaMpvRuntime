#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Turn one namespaced iOS dylib graph into app-embeddable dynamic frameworks."""

from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from apple_common import EXPECTED_TARGETS, load_apple_policy  # noqa: E402


LOGICAL_NAMES = (
    "mpv",
    "avcodec",
    "avfilter",
    "avformat",
    "avutil",
    "swresample",
    "swscale",
    "placebo",
    "ass",
    "freetype",
    "fribidi",
    "harfbuzz",
)
FRAMEWORK_NAMES = {
    name: (
        "KMediaMpv"
        if name == "mpv"
        else "KMediaMpv" + "".join(part.capitalize() for part in name.split("_"))
    )
    for name in LOGICAL_NAMES
}
SAFE_LIBRARY_NAME = re.compile(r"^libkmediampv_[A-Za-z0-9_]+(?:\.[0-9]+)*\.dylib$")


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


def select_library(prefix: Path, logical_name: str) -> Path:
    candidates = sorted(
        path
        for path in (prefix / "lib").glob(f"libkmediampv_{logical_name}*.dylib")
        if path.is_file()
        and not path.is_symlink()
        and SAFE_LIBRARY_NAME.fullmatch(path.name)
    )
    if len(candidates) != 1:
        raise ValueError(
            f"{logical_name}: expected one real namespaced dylib, "
            f"found {[path.name for path in candidates]}"
        )
    return candidates[0]


def install_name(path: Path) -> str:
    values = [
        line.strip()
        for line in run("otool", "-D", str(path)).splitlines()[1:]
        if line.strip()
    ]
    if len(values) != 1:
        raise ValueError(f"{path.name}: expected one Mach-O install name")
    return values[0]


def dependencies(path: Path) -> list[str]:
    own = install_name(path)
    return sorted(
        {
            line.strip().split(" (", 1)[0]
            for line in run("otool", "-L", str(path)).splitlines()[1:]
            if line.strip()
        }
        - {own}
    )


def write_framework_metadata(
    framework: Path,
    framework_name: str,
    target: dict,
    logical_name: str,
    mpv_headers: Path,
) -> None:
    headers = framework / "Headers"
    modules = framework / "Modules"
    headers.mkdir()
    modules.mkdir()
    if logical_name == "mpv":
        if not mpv_headers.is_dir() or mpv_headers.is_symlink():
            raise ValueError("installed mpv headers are missing")
        for source in sorted(mpv_headers.iterdir()):
            if source.is_symlink() or not source.is_file():
                raise ValueError("mpv header inventory contains an unsupported entry")
            shutil.copyfile(source, headers / source.name)
        umbrella = "client.h"
        if not (headers / umbrella).is_file():
            raise ValueError("mpv client.h is missing from the installed headers")
    else:
        umbrella = f"{framework_name}.h"
        (headers / umbrella).write_text(
            "#pragma once\n",
            encoding="utf-8",
        )
    (modules / "module.modulemap").write_text(
        f"framework module {framework_name} {{\n"
        f"  umbrella header \"{umbrella}\"\n"
        "  export *\n"
        "  module * { export * }\n"
        "}\n",
        encoding="utf-8",
    )
    minimum_key = (
        "MinimumOSVersion"
        if not target["simulator"]
        else "MinimumOSVersion"
    )
    plist = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": framework_name,
        "CFBundleIdentifier": f"cc.suviomedia.kmediampv.{framework_name.lower()}",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": framework_name,
        "CFBundlePackageType": "FMWK",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        minimum_key: target["minimumOs"],
    }
    with (framework / "Info.plist").open("wb") as output:
        plistlib.dump(plist, output, sort_keys=True)


def package_frameworks(
    prefix: Path,
    output: Path,
    policy_path: Path,
    target_name: str,
    inventory_path: Path,
) -> dict:
    policy = load_apple_policy(policy_path)
    if target_name not in EXPECTED_TARGETS:
        raise ValueError(f"unsupported Apple target: {target_name}")
    target = policy["targets"][target_name]
    if output.exists() or output.is_symlink():
        raise ValueError("Apple framework output must not already exist")
    output.parent.resolve(strict=True)
    output.mkdir()

    sources = {name: select_library(prefix, name) for name in LOGICAL_NAMES}
    original_names = {name: install_name(path) for name, path in sources.items()}
    basename_to_logical = {
        Path(value).name: logical for logical, value in original_names.items()
    }
    if len(basename_to_logical) != len(LOGICAL_NAMES):
        raise ValueError("Apple dylib install names are not unique")

    records: list[dict[str, object]] = []
    for logical_name in LOGICAL_NAMES:
        framework_name = FRAMEWORK_NAMES[logical_name]
        framework = output / f"{framework_name}.framework"
        framework.mkdir()
        binary = framework / framework_name
        shutil.copyfile(sources[logical_name], binary)
        binary.chmod(0o755)
        destination_id = f"@rpath/{framework.name}/{framework_name}"
        subprocess.run(
            ["install_name_tool", "-id", destination_id, str(binary)],
            check=True,
        )
        internal_dependencies: list[str] = []
        external_dependencies: list[str] = []
        for dependency in dependencies(binary):
            dependency_logical = basename_to_logical.get(Path(dependency).name)
            if dependency_logical is None:
                if not (
                    dependency.startswith("/System/Library/")
                    or dependency.startswith("/usr/lib/")
                ):
                    raise ValueError(
                        f"{logical_name}: unsupported external dependency {dependency}"
                    )
                external_dependencies.append(dependency)
                continue
            dependency_framework = FRAMEWORK_NAMES[dependency_logical]
            replacement = (
                f"@rpath/{dependency_framework}.framework/{dependency_framework}"
            )
            subprocess.run(
                [
                    "install_name_tool",
                    "-change",
                    dependency,
                    replacement,
                    str(binary),
                ],
                check=True,
            )
            internal_dependencies.append(dependency_framework)
        write_framework_metadata(
            framework,
            framework_name,
            target,
            logical_name,
            prefix / "include/mpv",
        )
        final_dependencies = dependencies(binary)
        expected_internal = {
            f"@rpath/{name}.framework/{name}" for name in internal_dependencies
        }
        actual_internal = {
            dependency
            for dependency in final_dependencies
            if dependency.startswith("@rpath/KMediaMpv")
        }
        if actual_internal != expected_internal:
            raise ValueError(f"{logical_name}: framework dependency rewrite is incomplete")
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        records.append(
            {
                "logicalName": logical_name,
                "framework": framework.name,
                "binary": framework_name,
                "installName": destination_id,
                "sha256": digest,
                "internalDependencies": sorted(internal_dependencies),
                "externalDependencies": sorted(external_dependencies),
            }
        )

    inventory = {
        "schemaVersion": 1,
        "target": target_name,
        "architecture": target["architecture"],
        "minimumOs": target["minimumOs"],
        "simulator": target["simulator"],
        "frameworkCount": len(records),
        "frameworks": records,
    }
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_path.write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return inventory


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--target", choices=sorted(EXPECTED_TARGETS), required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    args = parser.parse_args()
    package_frameworks(
        args.prefix.resolve(strict=True),
        args.output.absolute(),
        args.policy.resolve(strict=True),
        args.target,
        args.inventory.absolute(),
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
