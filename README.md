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
