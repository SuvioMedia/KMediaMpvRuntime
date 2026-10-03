#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""Build and run the native Metal host geometry policy fixture against a local macOS runtime."""
import argparse
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--include', type=Path, required=True, help='Built prefix/include with pinned libplacebo and Vulkan headers')
    parser.add_argument('--lib', type=Path, required=True, help='Directory containing packaged libkmediampv_placebo and MoltenVK dylibs')
    parser.add_argument('--output', type=Path, default=Path('build/metal-processing-test'))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    config = output.parent / (output.name + '-config')
    config.mkdir(exist_ok=True)
    (config / 'config.h').write_text('#define HAVE_COCOA 1\n#define HAVE_IOS_VULKAN 1\n')
    subprocess.run([
        'xcrun', 'clang', '-fno-objc-arc', '-fblocks', '-Wall', '-Wextra', '-Werror',
        '-Wno-nullability-completeness', '-I', str(args.include.resolve()), '-I', str(config),
        str(root / 'native/tests/metal_processing.m'),
        str(root / 'native/mpv-patches/video/out/kmedia_metal_processing.m'),
        '-framework', 'Metal', '-framework', 'Foundation', '-L', str(args.lib.resolve()),
        '-lkmediampv_placebo', '-lkmediampv_moltenvk', '-Wl,-rpath,' + str(args.lib.resolve()),
        '-o', str(output),
    ], check=True)
    subprocess.run([str(output)], check=True)


if __name__ == '__main__':
    main()
