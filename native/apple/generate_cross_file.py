#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Generate one closed Meson cross file for an Apple device/simulator slice."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from apple_common import EXPECTED_TARGETS, load_apple_policy  # noqa: E402


SAFE_PATH = re.compile(r"^/[A-Za-z0-9_./+ -]+$")


def xcrun(sdk: str, *arguments: str) -> str:
    result = subprocess.run(
        ["xcrun", "--sdk", sdk, *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )
    value = result.stdout.strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("xcrun returned an invalid single-line value")
    return value


def quote(value: str) -> str:
    if "'" in value or "\n" in value or "\r" in value:
        raise ValueError("Meson cross-file value contains an unsafe character")
    return f"'{value}'"


def array(values: list[str]) -> str:
    return "[" + ", ".join(quote(value) for value in values) + "]"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--target", choices=sorted(EXPECTED_TARGETS), required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    args = parser.parse_args()

    policy = load_apple_policy(args.policy)
    target = policy["targets"][args.target]
    sdk = target["sdk"]
    sysroot = xcrun(sdk, "--show-sdk-path")
    sdk_version = xcrun(sdk, "--show-sdk-version")
    tools = {
        name: xcrun(sdk, "--find", executable)
        for name, executable in {
            "c": "clang",
            "cpp": "clang++",
            "objc": "clang",
            "objcpp": "clang++",
            "ar": "ar",
            "nm": "nm",
            "ranlib": "ranlib",
            "strip": "strip",
            "otool": "otool",
            "install_name_tool": "install_name_tool",
        }.items()
    }
    for path in [sysroot, *tools.values()]:
        if not SAFE_PATH.fullmatch(path) or not Path(path).exists():
            raise ValueError("Apple toolchain contains an invalid path")

    minimum = target["minimumOs"]
    triple = (
        f"arm64-apple-ios{minimum}-simulator"
        if target["simulator"]
        else f"arm64-apple-ios{minimum}"
    )
    common = [
        "-target",
        triple,
        "-isysroot",
        sysroot,
        "-fPIC",
        f"-ffile-prefix-map={args.prefix.parent}=.",
        f"-fdebug-prefix-map={args.prefix.parent}=.",
    ]
    link = [
        "-target",
        triple,
        "-isysroot",
        sysroot,
    ]
    cross_file = "\n".join(
        [
            "[binaries]",
            f"c = {quote(tools['c'])}",
            f"cpp = {quote(tools['cpp'])}",
            f"objc = {quote(tools['objc'])}",
            f"objcpp = {quote(tools['objcpp'])}",
            f"ar = {quote(tools['ar'])}",
            f"nm = {quote(tools['nm'])}",
            f"ranlib = {quote(tools['ranlib'])}",
            f"strip = {quote(tools['strip'])}",
            "pkg-config = 'pkg-config'",
            "",
            "[built-in options]",
            f"c_args = {array(common)}",
            f"cpp_args = {array(common)}",
            f"objc_args = {array(common)}",
            f"objcpp_args = {array(common)}",
            f"c_link_args = {array(link)}",
            f"cpp_link_args = {array(link)}",
            f"objc_link_args = {array(link)}",
            f"objcpp_link_args = {array(link)}",
            "",
            "[properties]",
            "needs_exe_wrapper = true",
            "",
            "[host_machine]",
            f"system = {quote(target['mesonSystem'])}",
            "cpu_family = 'aarch64'",
            "cpu = 'arm64'",
            "endian = 'little'",
            "",
        ]
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(cross_file, encoding="utf-8")
    metadata = {
        "schemaVersion": 1,
        "target": args.target,
        "sdk": sdk,
        "sdkVersion": sdk_version,
        "sysroot": sysroot,
        "triple": triple,
        "minimumOs": minimum,
        "tools": tools,
    }
    args.metadata.parent.mkdir(parents=True, exist_ok=True)
    args.metadata.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
