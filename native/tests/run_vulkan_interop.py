#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""Build/run the real Vulkan host handoff fixture using an existing local SDK/runtime."""
import argparse
from pathlib import Path
import struct
import shutil
import subprocess
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--include', type=Path, required=True)
parser.add_argument('--lib', type=Path, required=True)
parser.add_argument('--glslc', type=Path, required=True)
parser.add_argument('--output', type=Path, default=Path('build/vulkan-interop-test'))
parser.add_argument('--android-ndk', type=Path)
parser.add_argument('--adb', type=Path)
parser.add_argument('--serial')
parser.add_argument('--repeat', type=int, default=1, help='Repeat render baseline and handoff (1–100)')
args = parser.parse_args()
if args.android_ndk and (not args.adb or not args.serial):
    parser.error('--android-ndk requires --adb and --serial')
if not 1 <= args.repeat <= 100:
    parser.error('--repeat must be between 1 and 100')
root = Path(__file__).resolve().parents[2]
out = args.output.resolve()
out.mkdir(parents=True, exist_ok=True)
spirv = out / 'fixture.spv'
subprocess.run([str(args.glslc), '--target-env=vulkan1.1', str(root / 'native/tests/vulkan_interop.comp'), '-o', str(spirv)], check=True)
data = spirv.read_bytes()
(out / 'vulkan_fixture_spirv.h').write_text('static const uint32_t fixture_spirv[] = {' +
    ','.join(hex(word[0]) for word in struct.iter_unpack('<I', data)) + '};\n')
compiler = 'cc'
link = []
if args.android_ndk:
    host = 'darwin-x86_64' if sys.platform == 'darwin' else 'linux-x86_64'
    compiler = str(args.android_ndk / 'toolchains/llvm/prebuilt' / host / 'bin/aarch64-linux-android28-clang')
    link = ['-Wl,-z,max-page-size=16384', '-Wl,-rpath,$ORIGIN']
loader = '-lkmediampv_moltenvk' if sys.platform == 'darwin' and not args.android_ndk else '-lvulkan'
subprocess.run([compiler, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-I', str(args.include.resolve()),
    '-I', str(out), str(root / 'native/tests/vulkan_interop.c'),
    str(root / 'native/mpv-patches/video/out/kmedia_vulkan_interop.c'),
    '-L', str(args.lib.resolve()), '-Wl,-rpath,' + str(args.lib.resolve()),
    '-lkmediampv_placebo', loader, '-lm', *link, '-o', str(out / 'fixture')], check=True)
if args.android_ndk:
    library = out / 'libkmediampv_placebo.so'
    shutil.copy2(args.lib / library.name, library)
    adb = [str(args.adb), '-s', args.serial]
    remote = '/data/local/tmp/kmedia-vulkan-interop'
    subprocess.run(adb + ['shell', 'mkdir', '-p', remote], check=True)
    for name in ('fixture', library.name):
        subprocess.run(adb + ['push', str(out / name), remote + '/' + name], check=True)
    for iteration in range(args.repeat):
        print(f'Iteration {iteration + 1}/{args.repeat}', flush=True)
        for mode in (' --render-baseline', ''):
            subprocess.run(adb + ['shell', 'cd ' + remote + ' && ./fixture' + mode], check=True)
else:
    for iteration in range(args.repeat):
        print(f'Iteration {iteration + 1}/{args.repeat}', flush=True)
        subprocess.run([str(out / 'fixture'), '--render-baseline'], check=True)
        subprocess.run([str(out / 'fixture')], check=True)
