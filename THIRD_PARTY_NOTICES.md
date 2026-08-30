<!-- SPDX-License-Identifier: LGPL-2.1-or-later -->

# Third-party notices

KMediaMpv build recipes consume third-party source but do not relicense it.
Every release must include the exact upstream notices and source corresponding
to its binaries. The summary below is not a substitute for those files.

## mpv

mpv is GPL-2.0-or-later by default. Upstream supports an LGPL-targeting build
when GPL-only files are excluded, but warns that `-Dgpl=false` does not by
itself create an LGPL grant and that linked libraries can change the result.
KMediaMpv therefore accepts only the pinned, inspected configuration described
by its release manifest.

The public mpv `client.h` and `render.h` headers carry the ISC license so that
external clients and wrappers can use them. See `LICENSES/ISC-mpv-client-api.txt`.

Official source: https://github.com/mpv-player/mpv

The portable ASS smoke does not import a third-party font. It generates a
minimal TrueType test fixture from project-authored LGPL-2.1-or-later tooling and
Python stdlib only. Its U+0041 cmap entry and rectangle glyph render literal
`AAAA` under the forced `KMediaMpvSmoke` family. The fixture exists only in a
private temporary directory; no font file is added to the native or JVM
payload. Evidence records its fixture license and deterministic SHA-256.

## FFmpeg

FFmpeg is LGPL-2.1-or-later by default, with optional GPL components. KMediaMpv
disables GPL and nonfree features and rejects a runtime whose reported
configuration or linked graph contradicts that policy.

Official source and checklist: https://ffmpeg.org/

## libass

libass is distributed under the ISC license. See `LICENSES/ISC-libass.txt`.

Official source: https://github.com/libass/libass

## Other audited inputs

The current candidate graph dynamically links libplacebo
(LGPL-2.1-or-later), FreeType under the FreeType License, FriBidi
(LGPL-2.1-or-later), and HarfBuzz (MIT). HarfBuzz's USE shaping sources carry
a distinct Microsoft Corporation MIT notice; its exact `src/ms-use/COPYING`
bytes are preserved separately in the evidence and runtime JAR. It also pins
fast_float (header-only), whose upstream source is offered under
Apache-2.0 OR MIT OR BSL-1.0; this build selects Apache-2.0. fast_float
contains code adapted from the Google Wuffs project by Nigel Tao, originally
under Apache-2.0. The exact fast_float README and all three upstream license
files are preserved in the evidence and runtime JAR. Jinja2 (BSD-3-Clause,
build-only) and MarkupSafe (BSD-3-Clause, build-only) are pinned build tools.
libplacebo also consumes the exactly pinned Vulkan-Headers gitlink
(Apache-2.0, header-only). The macOS and iOS graphs enable that Vulkan path and
link the pinned glslang 15.4.0 SPIR-V compiler (BSD-3-Clause) statically into
the replaceable libplacebo library/framework; no separate glslang runtime
library is packaged. Their Vulkan loader is pinned dynamic MoltenVK 1.4.2
(Apache-2.0), selected from Khronos's audited upstream XCFramework distribution
and accompanied by its exact source and license. The macOS runtime repackages
the selected dynamic binary as `libkmediampv_moltenvk.dylib`; the iOS runtime
uses namespaced application-embedded frameworks.
The Windows UCRT64 build statically closes its compiler-support dependencies on
libgcc, libstdc++, and winpthreads and hides those archive symbols from the DLL
export tables. libgcc and libstdc++ are GPL-3.0-or-later with the GCC Runtime
Library Exception 3.1; the applicable texts are retained verbatim at
`LICENSES/GPL-3.0.txt` and
`LICENSES/GCC-Runtime-Library-Exception-3.1.txt`. The exact GCC and winpthreads
package versions and the upstream winpthreads revision are pinned in the
desktop policy and recorded in build evidence. The combined MIT and BSD
winpthreads notice is retained verbatim at
`LICENSES/winpthreads-COPYING.txt`.
Android candidates link the pinned NDK r29 libc++ runtime statically only into
libplacebo; a symbol audit rejects a second runtime owner. The NDK's complete
NOTICE is retained byte-for-byte in the evidence and AAR, and the embedded
runtime remains under its upstream LLVM/Apache terms.
The KMediaMpv wrapper and Android JNI client are proprietary and are not
represented as LGPL-covered code. Their root license contains the limited
own-use permissions needed to modify or replace LGPL components and relink and
debug a working combined executable. Vulkan-Headers, MoltenVK, and the selected
fast_float input remain identified under Apache-2.0; glslang remains
BSD-3-Clause. Their respective terms, notices, and license texts remain in
force. No third-party Apache, BSD, or ISC file is represented as project-authored
LGPL code. The complete LGPL-2.1 text and every applicable third-party license
text accompany the artifacts.
zlib, iconv, and all unlisted auto-detected components are disabled in the
current recipe. Exact versions, source hashes, dependency edges, notices, and
linkage are recorded in `compliance/components` and repeated in every eligible
release's SBOM and evidence bundle.
`upstreamLicenseSummarySpdx` in those manifests is explicitly a non-exhaustive
project-level summary, not a per-file source-package conclusion. The enforced
selected build grant is recorded separately as `builtOutputLicenseSpdx`.

FreeType distribution-documentation acknowledgment: This software is based in part on the work of the FreeType Team.

The checked-in Gradle wrapper is distributed under Apache-2.0; its license is
kept at `gradle/wrapper/LICENSE`. Undocumented or auto-detected dependencies
make a native build ineligible for publication.
