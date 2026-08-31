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
