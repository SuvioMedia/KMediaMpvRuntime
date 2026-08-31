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

Run `./gradlew verifyAll` before publishing.

## Publishing

This public LGPL runtime is built only on GitHub-hosted runners and published
to Maven Central. It is not published to the private Suvio kkRepo.

Publication is deliberately split into two manual workflows:

1. `Build immutable KMediaMpvRuntime release` builds and inspects every public
   native target, emits corresponding source, SDK, SBOM, Apple, and Maven
   assets, then creates an immutable GitHub Release.
2. `Publish existing KMediaMpvRuntime release to Maven Central` downloads that
   exact release, verifies its checksum and POM boundary, signs the closed
   Maven inventory, and submits it to Central Portal. The default submission
   mode waits for manual approval in Central.

The Central workflow uses the protected `maven-central` GitHub environment and
requires `MAVEN_CENTRAL_USERNAME`, `MAVEN_CENTRAL_PASSWORD`,
`MAVEN_SIGNING_KEY`, and `MAVEN_SIGNING_PASSWORD` as environment or repository
secrets. Neither workflow runs automatically on a tag or commit.
