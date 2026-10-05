#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Assemble the public KMediaMpv runtime frameworks and an exact-runtime podspec."""

from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path


FRAMEWORKS = ("KMediaMpv", "KMediaMpvPlacebo", "KMediaMpvMoltenVK")
RUNTIME_ID = re.compile(r"kmediaffmpeg-9\.0\.1-ass-0\.17\.5-[0-9a-f]{16}")
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?")


def properties(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="ISO-8859-1").splitlines():
        if not line or line.startswith(("#", "!")):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or not value or key in values:
            raise ValueError(f"malformed manifest: {path}")
        values[key] = value
    return values


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_tree(root: Path) -> None:
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"framework input must be a real directory: {root}")
    if any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError(f"framework input contains a symlink: {root}")


def verify_framework_dependencies(framework: Path, name: str) -> None:
    output = subprocess.run(
        ["otool", "-L", str(framework / name)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout
    dependencies = [line.strip().split(" (", 1)[0] for line in output.splitlines()[1:]]
    if any("libkmediaffmpeg_" in dependency for dependency in dependencies):
        raise ValueError(f"{name} still references a raw shared-runtime dylib")
    if name == "KMediaMpv":
        if not any("KMediaFfmpeg" in dependency for dependency in dependencies):
            raise ValueError("KMediaMpv does not reference the shared runtime frameworks")
    if name in {"KMediaMpv", "KMediaMpvPlacebo"} and not any(
        "KMediaMpvMoltenVK" in dependency for dependency in dependencies
    ):
        raise ValueError(f"{name} does not reference the bundled MoltenVK framework")


def deterministic_zip(source: Path, output: Path, epoch: int) -> None:
    if output.exists() or output.is_symlink():
        raise ValueError("archive output must not exist")
    timestamp = tuple(time.gmtime(max(epoch, 315532800))[:6])
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            info = zipfile.ZipInfo(path.relative_to(source).as_posix(), timestamp)
            mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
            info.external_attr = (0o100000 | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())


def podspec(version: str, runtime_version: str, archive_name: str, archive_sha256: str) -> str:
    return f"""Pod::Spec.new do |spec|
  spec.name                = 'KMediaMpvRuntime'
  spec.version             = '{version}'
  spec.summary             = 'Public LGPL mpv and libplacebo runtime for KMedia.'
  spec.homepage            = 'https://github.com/SuvioMedia/KMediaMpvRuntime'
  spec.license             = {{ :type => 'LGPL-2.1-or-later', :file => 'LICENSE' }}
  spec.author              = {{ 'SuvioMedia' => 'SuvioMedia' }}
  spec.platform            = :ios, '16.2'
  spec.source              = {{ :http => 'https://github.com/SuvioMedia/KMediaMpvRuntime/releases/download/v{version}/{archive_name}', :sha256 => '{archive_sha256}' }}
  spec.vendored_frameworks = 'Frameworks/*.xcframework'
  spec.dependency 'KMediaFfmpegRuntime', '= {runtime_version}'
end
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", type=Path, required=True)
    parser.add_argument("--simulator", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--podspec", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--runtime-version", required=True)
    parser.add_argument("--epoch", type=int, required=True)
    arguments = parser.parse_args()
    if not SEMVER.fullmatch(arguments.version) or not SEMVER.fullmatch(arguments.runtime_version):
        raise ValueError("client and runtime versions must be immutable SemVer")
    if arguments.output.exists() or arguments.output.is_symlink():
        raise ValueError("aggregate output must not exist")
    manifests = [properties(root.resolve() / "manifest.properties") for root in (arguments.device, arguments.simulator)]
    expected_targets = ("ios-arm64", "ios-simulator-arm64")
    if tuple(manifest.get("platform") for manifest in manifests) != expected_targets:
        raise ValueError("Apple runtime slices have the wrong target identity")
    runtime_ids = {manifest.get("sharedRuntimeId") for manifest in manifests}
    if len(runtime_ids) != 1 or not RUNTIME_ID.fullmatch(next(iter(runtime_ids), "")):
        raise ValueError("Apple runtime slices target different or invalid runtime IDs")
    frameworks = arguments.output / "Frameworks"
    frameworks.mkdir(parents=True)
    for name in FRAMEWORKS:
        slices = []
        for root in (arguments.device.resolve(), arguments.simulator.resolve()):
            framework = root / "Frameworks" / f"{name}.framework"
            safe_tree(framework)
            verify_framework_dependencies(framework, name)
            with (framework / "Info.plist").open("rb") as source:
                if plistlib.load(source).get("CFBundleExecutable") != name:
                    raise ValueError(f"{name} framework identity differs")
            slices.extend(("-framework", str(framework)))
        subprocess.run(
            ["xcodebuild", "-create-xcframework", *slices, "-output", str(frameworks / f"{name}.xcframework")],
            check=True,
        )
    (arguments.output / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "frameworks": list(FRAMEWORKS),
                "runtimeId": next(iter(runtime_ids)),
                "runtimeVersion": arguments.runtime_version,
                "targets": list(expected_targets),
                "version": arguments.version,
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    repository = Path(__file__).resolve().parent.parent
    shutil.copyfile(repository / "NOTICE", arguments.output / "NOTICE")
    shutil.copyfile(repository / "LICENSE", arguments.output / "LICENSE")
    shutil.copyfile(repository / "THIRD_PARTY_NOTICES.md", arguments.output / "THIRD_PARTY_NOTICES.md")
    shutil.copytree(repository / "LICENSES", arguments.output / "LICENSES")
    shutil.copytree(repository / "compliance" / "components", arguments.output / "compliance" / "components")
    deterministic_zip(arguments.output, arguments.archive, arguments.epoch)
    arguments.podspec.write_text(
        podspec(arguments.version, arguments.runtime_version, arguments.archive.name, digest(arguments.archive)),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
