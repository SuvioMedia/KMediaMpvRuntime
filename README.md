# KMediaMpvRuntime

Public LGPL runtime boundary for the MPV backend used by Suvio.

Published Maven artifacts:

- `cc.suviomedia:kmedia-mpv-lgpl-runtime-android`
- `cc.suviomedia:kmedia-mpv-lgpl-runtime-desktop`

They contain only mpv/libplacebo runtime libraries and legal metadata. The
private Java API and Android JNI library are published separately as
`kmedia-mpv-client-*` from KMediaMpvClient.

The native builder under `native/` consumes a matching extracted
KMediaFfmpegRuntime SDK. Build and compliance tools are under `scripts/`, and
the exact component policy is under `compliance/`. The public boundary also
contains the Apple build policy, patches, MoltenVK preparation, and XCFramework
packaging tools. Apple is released as a CocoaPods/XCFramework payload rather
than as an additional Maven artifact; its release workflow must publish from
this public repository.

Run `./gradlew verifyAll` to validate the project.

Pull requests affecting native sources also run `build-validation.yml` against the same eight
target recipes as the release workflow. The build binds the exact PR revision to the released
FFmpeg rc.11 SDK identity and verified upstream archives, then validates each native package and
assembles its corresponding-source archive. One-day Actions artifacts retain these review builds
under commit-specific `0.1.0-validation.<revision>` versions. They are staging evidence; the gated
release workflow still controls publication and device-matrix acceptance. This validation uses
standard hosted runners only in the public runtime repository.

## Local macvk processing validation

The optional `kmediampv_embedded_macvk_processing_api_version() == 1` capability lets
an embedding host process RGB frames before scaling, projection and overlays. The
LGPL runtime owns the Vulkan/Metal texture and timeline handoff; host callbacks own
the optional processing implementation. The contract uses linear BT.2020
RGBA16Float, with 1.0 representing 100 nits, and an owned output texture on the
same Metal device. The runtime restores the source color representation before
returning to libplacebo. Ordinary hosts without the protocol retain normal playback.

After building the pinned macOS prefix and packaging its dylibs, run:

```sh
python3 native/tests/run_metal_interop.py \
  --include /absolute/path/to/prefix/include \
  --lib /absolute/path/to/packaged/dylibs
```

This real-GPU fixture checks SDR/PQ/extended-linear values, alpha, independent input
luminance and gamut, output size changes, 288 queued frames, bypass/invalid-output
recovery and teardown. It does not measure player throughput or certify HDR/VR
presentation; those require the consuming player's integration tests. The handoff
uses GPU timeline events and no CPU frame copies or per-frame CPU completion wait.
The host's optional processor may still perform its own readback.

## Local Android Vulkan processing validation

Android `gpu-next` / `androidvk` exposes the optional
`kmediampv_vulkan_processing_api_version() == 1` interface in
`mpv/kmedia_vulkan_api.h`. Register callbacks and set the returned id as
`kmedia-vulkan-processing-id` before opening video. The hook receives decoded RGB
before scaling and native subtitles. It preserves source HLG luminance before
display adaptation and uses linear BT.2020 RGBA16F, with 1.0 equal to 100 nits.

The host records compute work into a borrowed Vulkan command buffer and returns
an image on that device. The runtime consumes it before `end_frame`, then reports
completion after the submission fence. Up to three frames share a 256 MiB texel
budget; a busy pool bypasses processing without waiting. GPU copies and timeline
dependencies connect the two implementations without CPU pixel copies or a
per-frame CPU wait. Device teardown drains pending work. A frame request wakes
paused playback; seek resets suppress the old frame until a new decoded frame
arrives. Unregistering prevents further processing and retains callbacks until
the renderer releases them. See the header for callback ownership and threading.

After building the Android ARM64 prefix, run on an already booted test device:

```sh
python3 native/tests/run_vulkan_interop.py \
  --include /absolute/path/to/prefix/include \
  --lib /absolute/path/to/prefix/lib \
  --glslc /absolute/path/to/ndk/shader-tools/darwin-x86_64/glslc \
  --android-ndk /absolute/path/to/ndk \
  --adb /absolute/path/to/android-sdk/platform-tools/adb \
  --serial emulator-5580 --repeat 20
```

Omit the three Android arguments to run against a local desktop Vulkan prefix.
Each iteration checks 200 libplacebo render/readback frames independently of the
host hook, then SDR/PQ/extended-linear values, 37 HLG source/display cases, alpha,
288 queued resize frames, bypass/recovery and teardown. Readbacks and blocking
waits belong to the fixture. These tests do not certify player video throughput,
HDR presentation, physical devices or the consuming Android client integration.

The Android emulator's `gfxstream (MoltenVK)` driver reports the llvmpipe driver
id. Its render/readback baseline reproduces stale results without the existing
MoltenVK completion-fence workaround. The source patch extends that upstream
workaround to this exact driver name; physical Android drivers keep their usual
path. The upstream diagnosis is [MoltenVK issue 2697](https://github.com/KhronosGroup/MoltenVK/issues/2697).
