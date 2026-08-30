#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Generate a pinned Meson cross file from an explicit Android NDK path."""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from android_common import load_android_policy  # noqa: E402


def quote(value: str) -> str:
    return "'" + value.replace("'", "\\'") + "'"


def meson_array(values: list[str]) -> str:
    return "[" + ", ".join(quote(value) for value in values) + "]"


def require_tool(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"required Android tool is missing: {path}")
    path.resolve(strict=True)
    return str(path.absolute())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--ndk", type=Path, required=True)
    parser.add_argument("--cmake", type=Path, required=True)
    parser.add_argument("--abi", required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    args = parser.parse_args()

    policy = load_android_policy(args.policy)
    if args.abi not in policy["abis"]:
        raise ValueError(f"unsupported Android ABI: {args.abi}")
    ndk = args.ndk.resolve(strict=True)
    source_properties = (ndk / "source.properties").read_text(encoding="utf-8")
    revision_match = re.search(r"^Pkg\.Revision\s*=\s*(\S+)\s*$", source_properties, re.MULTILINE)
    if not revision_match or revision_match.group(1) != policy["toolchain"]["ndkVersion"]:
        raise ValueError("Android NDK revision differs from build policy")

    host_system = platform.system()
    host_machine = platform.machine().lower()
    if host_system == "Darwin" and host_machine in {"arm64", "aarch64", "x86_64"}:
        host_tag = "darwin-x86_64"
    elif host_system == "Linux" and host_machine in {"x86_64", "amd64"}:
        host_tag = "linux-x86_64"
    else:
        raise ValueError(f"unsupported Android build host: {host_system}/{host_machine}")
    bin_dir = ndk / "toolchains/llvm/prebuilt" / host_tag / "bin"
    details = policy["abis"][args.abi]
    api = policy["toolchain"]["minSdk"]
    target = f"{details['clangTriple']}{api}"
    tools = {
        "c": require_tool(bin_dir / f"{target}-clang"),
        "cpp": require_tool(bin_dir / f"{target}-clang++"),
        "ar": require_tool(bin_dir / "llvm-ar"),
        "nm": require_tool(bin_dir / "llvm-nm"),
        "ranlib": require_tool(bin_dir / "llvm-ranlib"),
        "strip": require_tool(bin_dir / "llvm-strip"),
        "readelf": require_tool(bin_dir / "llvm-readelf"),
        "cmake": require_tool(args.cmake.resolve(strict=True) / "bin/cmake"),
    }
    try:
        cmake_output = subprocess.run(
            [tools["cmake"], "--version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError("cannot execute the pinned Android CMake") from error
    cmake_match = re.search(r"^cmake version (\S+)$", cmake_output, re.MULTILINE)
    if (
        not cmake_match
        or cmake_match.group(1)
        != policy["toolchain"]["cmakeReportedVersions"][host_tag]
    ):
        raise ValueError("Android CMake version differs from build policy")
    sysroot = (ndk / "toolchains/llvm/prebuilt" / host_tag / "sysroot").resolve(strict=True)
    prefix = args.prefix.resolve()
    mapping = str(args.output.resolve().parents[1]) + "=."
    compile_args = ["-fPIC", f"-ffile-prefix-map={mapping}", f"-fdebug-prefix-map={mapping}"]
    link_args = [
        f"-L{prefix / 'lib'}",
        "-Wl,--build-id=sha1",
        "-Wl,-z,relro",
        "-Wl,-z,now",
        "-Wl,-z,max-page-size=16384",
        "-Wl,-z,common-page-size=16384",
    ]
    lines = ["[binaries]"]
    for name in ("c", "cpp", "ar", "nm", "ranlib", "strip", "cmake"):
        lines.append(f"{name} = {quote(tools[name])}")
    lines.append("pkg-config = 'pkg-config'")
    lines.extend(
        [
            "",
            "[properties]",
            "needs_exe_wrapper = true",
            f"pkg_config_libdir = {quote(str(prefix / 'lib/pkgconfig') + ':' + str(prefix / 'share/pkgconfig'))}",
            "",
            "[host_machine]",
            "system = 'android'",
            f"cpu_family = {quote(details['mesonCpuFamily'])}",
            f"cpu = {quote(details['mesonCpu'])}",
            "endian = 'little'",
            "",
            "[built-in options]",
            f"c_args = {meson_array(compile_args)}",
            f"cpp_args = {meson_array(compile_args)}",
            f"c_link_args = {meson_array(link_args)}",
            f"cpp_link_args = {meson_array(link_args + ['-static-libstdc++'])}",
            "b_lundef = true",
        ]
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    metadata = {
        "schemaVersion": 1,
        "abi": args.abi,
        "api": api,
        "ndkRevision": revision_match.group(1),
        "cmakeVersion": cmake_match.group(1),
        "hostTag": host_tag,
        "target": target,
        "sysroot": str(sysroot),
        "tools": tools,
        "compileArguments": compile_args,
        "linkArguments": link_args,
        "cxxRuntime": "single hidden static libc++ owner: libkmediampv_placebo.so",
    }
    args.metadata.parent.mkdir(parents=True, exist_ok=True)
    args.metadata.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
