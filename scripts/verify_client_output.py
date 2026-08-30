#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
from pathlib import Path


EXPECTED = {
    "android-arm64-v8a": {"libkmediampv_mpv.so", "libkmediampv_placebo.so"},
    "android-armeabi-v7a": {"libkmediampv_mpv.so", "libkmediampv_placebo.so"},
    "linux-x86_64": {"libkmediampv_mpv.so", "libkmediampv_placebo.so"},
    "linux-aarch64": {"libkmediampv_mpv.so", "libkmediampv_placebo.so"},
    "macos-aarch64": {
        "libkmediampv_moltenvk.dylib", "libkmediampv_mpv.dylib",
        "libkmediampv_placebo.dylib",
    },
    "windows-x86_64": {"libkmediampv_mpv.dll", "libkmediampv_placebo.dll"},
    "ios-arm64": {
        "libkmediampv_moltenvk.dylib", "libkmediampv_mpv.dylib",
        "libkmediampv_placebo.dylib",
    },
    "ios-simulator-arm64": {
        "libkmediampv_moltenvk.dylib", "libkmediampv_mpv.dylib",
        "libkmediampv_placebo.dylib",
    },
}
FORBIDDEN = re.compile(r"(?:libkmediampv_(?:avcodec|avfilter|avformat|avutil|swresample|swscale|ass|freetype|fribidi|harfbuzz)|libav.+-kmb)")
RUNTIME_ID = re.compile(r"kmediaffmpeg-9\.0\.1-ass-0\.17\.5-[0-9a-f]{16}")
REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
SHA256 = re.compile(r"[0-9a-f]{64}")
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?")


def properties(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="ISO-8859-1").splitlines():
        if not line or line.startswith(("#", "!")):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or not value or key in result:
            raise ValueError("client manifest contains a malformed or duplicate field")
        result[key] = value
    return result


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def dependencies(path: Path, target: str, readelf: str) -> list[str]:
    if target.startswith(("macos-", "ios-")):
        output = subprocess.run(["otool", "-L", path], check=True, text=True, stdout=subprocess.PIPE).stdout
        return [line.strip().split(" (", 1)[0] for line in output.splitlines()[1:]]
    if target.startswith(("linux-", "android-")):
        output = subprocess.run([readelf, "-d", path], check=True, text=True, stdout=subprocess.PIPE).stdout
        return re.findall(r"\(NEEDED\).*?\[(.+?)\]", output)
    output = subprocess.run(["objdump", "-p", path], check=True, text=True, stdout=subprocess.PIPE).stdout
    return [line.split("DLL Name:", 1)[1].strip() for line in output.splitlines() if "DLL Name:" in line]


def resolve_readelf(command: str) -> str:
    resolved = shutil.which(command)
    if resolved is not None:
        return resolved
    resolved = shutil.which("greadelf")
    if resolved is not None:
        return resolved
    raise FileNotFoundError("GNU readelf is required to inspect Linux and Android clients")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target", choices=EXPECTED, required=True)
    parser.add_argument("--readelf", default="readelf")
    arguments = parser.parse_args()
    readelf = (
        resolve_readelf(arguments.readelf)
        if arguments.target.startswith(("linux-", "android-"))
        else arguments.readelf
    )
    runtime = arguments.output / "runtime"
    actual = {path.name for path in runtime.iterdir() if path.is_file()}
    if actual != EXPECTED[arguments.target]:
        raise ValueError(f"client library inventory differs: {sorted(actual)}")
    if any(FORBIDDEN.search(name) for name in actual):
        raise ValueError("a legacy private FFmpeg library leaked into the MPV client")
    manifest = properties(arguments.output / "manifest.properties")
    if manifest.get("platform") != arguments.target:
        raise ValueError("client manifest targets another platform")
    if manifest.get("schemaVersion") != "1" or manifest.get("licenseSpdx") != "LGPL-2.1-or-later":
        raise ValueError("client manifest schema or native payload license differs")
    if manifest.get("releaseEligible") != "true" or manifest.get("audioOutputs") != "true":
        raise ValueError("client manifest is not release eligible")
    if (
        manifest.get("mpvVersion") != "0.41.0"
        or manifest.get("libplaceboVersion") != "7.360.1"
        or not RUNTIME_ID.fullmatch(manifest.get("sharedRuntimeId", ""))
    ):
        raise ValueError("client manifest omits the exact shared runtime ID")
    apple = arguments.target.startswith(("macos-", "ios-"))
    if apple != (manifest.get("moltenVkVersion") == "1.4.2"):
        raise ValueError("client manifest MoltenVK version differs from its target inventory")
    if not SEMVER.fullmatch(manifest.get("sharedRuntimeVersion", "")):
        raise ValueError("client manifest omits the exact shared runtime distribution version")
    if not REVISION.fullmatch(manifest.get("recipeRevision", "")):
        raise ValueError("client manifest omits the immutable recipe revision")
    if not manifest.get("sourceOffer", "").startswith("https://"):
        raise ValueError("client manifest omits the HTTPS corresponding-source offer")
    if manifest.get("library.count") != str(len(actual)):
        raise ValueError("client manifest library count differs")
    indexed: dict[str, str] = {}
    components: set[str] = set()
    for index in range(len(actual)):
        prefix = f"library.{index}."
        name = manifest.get(prefix + "name", "")
        path = runtime / name
        if name not in actual or name in indexed:
            raise ValueError("client manifest library inventory differs")
        indexed[name] = manifest.get(prefix + "sha256", "")
        if manifest.get(prefix + "size") != str(path.stat().st_size):
            raise ValueError("client manifest library size differs")
        if not SHA256.fullmatch(indexed[name]) or indexed[name] != sha256(path):
            raise ValueError("client manifest library hash differs")
        components.add(manifest.get(prefix + "component", ""))
    expected_components = {"mpv", "libplacebo"}
    if apple:
        expected_components.add("moltenvk")
    if components != expected_components or set(indexed) != actual:
        raise ValueError("client manifest component inventory differs")
    libmpv = manifest.get("libmpv.name", "")
    if libmpv not in actual or manifest.get("libmpv.sha256") != indexed[libmpv]:
        raise ValueError("client manifest libmpv identity differs")
    expected_keys = {
        "schemaVersion", "platform", "licenseSpdx", "releaseEligible", "audioOutputs",
        "recipeRevision", "sourceOffer", "sharedRuntimeId", "sharedRuntimeVersion", "mpvVersion", "libmpv.name",
        "libplaceboVersion", "libmpv.sha256", "library.count",
    } | {
        f"library.{index}.{field}"
        for index in range(len(actual))
        for field in ("name", "component", "size", "sha256")
    }
    if apple:
        expected_keys.add("moltenVkVersion")
    if set(manifest) != expected_keys:
        raise ValueError("client manifest key set is not closed")
    for library in runtime.iterdir():
        needed = dependencies(library, arguments.target, readelf)
        if any(FORBIDDEN.search(Path(item).name) for item in needed):
            raise ValueError("client links a legacy private FFmpeg library")
        if library.name.startswith("libkmediampv_mpv") and not any("kmediaffmpeg_avutil" in item for item in needed):
            raise ValueError("libmpv is not linked to the shared KMediaFfmpegRuntime ABI")
    if arguments.target.startswith("macos-"):
        for library in runtime.iterdir():
            output = subprocess.run(["lipo", "-archs", library], check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
            if output != "arm64":
                raise ValueError("macOS client is not ARM64-only")
    elif arguments.target.startswith(("linux-", "android-")):
        expected_machine = "AArch64" if arguments.target.endswith(("aarch64", "arm64-v8a")) else "ARM" if arguments.target.endswith("armeabi-v7a") else "Advanced Micro Devices X86-64"
        for library in runtime.iterdir():
            output = subprocess.run([readelf, "-h", library], check=True, text=True, stdout=subprocess.PIPE).stdout
            if expected_machine not in output:
                raise ValueError("ELF client architecture differs from policy")
    if apple:
        libmpv_path = runtime / libmpv
        symbols = subprocess.run(
            ["nm", "-gU", libmpv_path],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout
        capabilities = (
            (
                "_kmediampv_embedded_macvk_api_version",
                "_kmediampv_embedded_macvk_presented_frames",
            )
            if arguments.target.startswith("macos-")
            else ("_kmediampv_embedded_iosvk_api_version",)
        )
        exported_symbols = set(symbols.split())
        if any(capability not in exported_symbols for capability in capabilities):
            raise ValueError("libmpv omits the versioned embedded Vulkan capability")
        linked = {
            Path(item).name
            for library in runtime.iterdir()
            if "moltenvk" not in library.name
            for item in dependencies(library, arguments.target, readelf)
        }
        if not any("kmediampv_moltenvk" in item for item in linked):
            raise ValueError("Apple client graph is not linked to the bundled MoltenVK loader")
        libplacebo_path = next(
            library for library in runtime.iterdir()
            if library.name.startswith("libkmediampv_placebo")
        )
        libplacebo_dependencies = dependencies(libplacebo_path, arguments.target, readelf)
        if not any("kmediampv_moltenvk" in item for item in libplacebo_dependencies):
            raise ValueError(
                "Apple libplacebo is not linked to the bundled MoltenVK procedure loader"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
