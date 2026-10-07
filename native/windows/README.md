# Embedded ANGLE on Windows

The OpenGL render API can be hosted by a private ANGLE module. The D3D11 hardware
interop must obtain EGL entry points from that render context's resolver, rather
than loading another `LIBEGL.DLL` and querying its unrelated thread-local state.
The patch captures those entry points during initialization and retains them per
GL context. Ordinary windowed mpv callers retain the standard dynamic loader.

The Windows recipe enables `egl-angle` and D3D11 hardware interop, without linking
ANGLE or enabling its windowed video output. The embedding player supplies EGL
and GLES. The D3D11 device registered by this interop enables upstream `d3d11vpp`,
including its NVIDIA and Intel driver extensions. No NVIDIA redistributable is
required.

`include/` contains header-only ANGLE/Khronos declarations from the immutable
revision recorded in `headers.json`. The original copyright and license notices
remain in each file. The builder checks every header's SHA-256 before compiling.
These headers and the patch recipe are included in corresponding source.

Integration acceptance in KMediaPlayer's `MpvWindowsScalingPlaybackTest` covers
paused D3D11 rendering, 2x/3x scaling, seek, filter preservation, HDR fallback and
source replacement. Driver acceptance is logged; it does not establish whether
the driver's AI enhancement was applied to an individual frame.
