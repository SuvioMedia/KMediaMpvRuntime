#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Apply exact, fail-closed native runtime patches to pinned source trees."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

REPLACEMENTS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "mpv": (
        (
            "video/out/vulkan/context_mac.m",
            "f187908a080bfbd494abd44bb31df9feb3c18e6b1e0e4dbf49731f64435a1116",
            "native/mpv-patches/video/out/vulkan/context_mac.m",
        ),
    ),
}

PATCHES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "libplacebo": (
        ("src/meson.build", "lib = library('placebo', sources,", "lib = library('kmediampv_placebo', sources,"),
        ("src/meson.build", "  soversion: apiver,\n", ""),
        (
            "src/glsl/meson.build",
            "  vulkan_lib_dirs = []\n",
            "  vulkan_lib_dirs = [join_paths(get_option('prefix'), get_option('libdir'))]\n",
        ),
        (
            "src/glsl/meson.build",
            "    glslang_deps += cxx.find_library('glslang', required: required, static: static)\n",
            "    glslang_deps += cxx.find_library(\n"
            "      'glslang', required: required, static: static, dirs: vulkan_lib_dirs\n"
            "    )\n",
        ),
    ),
    # Exact, fail-closed backports from upstream mpv after v0.41.0:
    # - 7c25b3e4: vo_gpu_next: fix upload synchronization
    # - 98c3ae4a: vulkan/context: don't activate descriptor buffers after ffmpeg 8.1
    "mpv": (
        (
            "video/out/vo_gpu_next.c",
            "        ok = pl_tex_upload(p->gpu, &(struct pl_tex_transfer_params) {\n"
            "            .tex        = entry->tex,\n"
            "            .rc         = { .x1 = item->packed_w, .y1 = item->packed_h, },\n"
            "            .row_pitch  = item->packed->stride[0],\n"
            "            .ptr        = item->packed->planes[0],\n"
            "        });\n"
            "        if (!ok) {\n"
            "            MP_ERR(vo, \"Failed uploading OSD texture!\\n\");\n"
            "            break;\n"
            "        }\n",
            "        struct pl_tex_transfer_params upload_params = {\n"
            "            .tex        = entry->tex,\n"
            "            .rc         = { .x1 = item->packed_w, .y1 = item->packed_h, },\n"
            "            .row_pitch  = item->packed->stride[0],\n"
            "            .ptr        = item->packed->planes[0],\n"
            "        };\n"
            "        // Keep the image alive until it's fully read.\n"
            "        if (p->gpu->limits.callbacks) {\n"
            "            upload_params.callback = talloc_free;\n"
            "            upload_params.priv = mp_image_new_ref(item->packed);\n"
            "        }\n"
            "        ok = pl_tex_upload(p->gpu, &upload_params);\n"
            "        if (!ok) {\n"
            "            MP_ERR(vo, \"Failed uploading OSD texture!\\n\");\n"
            "            talloc_free(upload_params.priv);\n"
            "            break;\n"
            "        }\n",
        ),
        (
            "video/out/vo_gpu_next.c",
            "            } else if (gpu->limits.callbacks) {\n"
            "                data[n].callback = talloc_free;\n"
            "                data[n].priv = mp_image_new_ref(mpi);\n"
            "            }\n",
            "            }\n"
            "            // Keep the image alive until it's fully read.\n"
            "            if (gpu->limits.callbacks) {\n"
            "                mp_assert(!data[n].callback);\n"
            "                data[n].callback = talloc_free;\n"
            "                mp_assert(!data[n].priv);\n"
            "                data[n].priv = mp_image_new_ref(mpi);\n"
            "            }\n",
        ),
        (
            "video/out/vo_gpu_next.c",
            "            if (!pl_upload_plane(gpu, plane, &tex[n], &data[n])) {\n"
            "                MP_ERR(vo, \"Failed uploading frame!\\n\");\n"
            "                talloc_free(data[n].priv);\n"
            "                talloc_free(mpi);\n"
            "                return false;\n"
            "            }\n",
            "            if (!pl_upload_plane(gpu, plane, &tex[n], &data[n])) {\n"
            "                MP_ERR(vo, \"Failed uploading frame!\\n\");\n"
            "                talloc_free(data[n].priv);\n"
            "                talloc_free(mpi);\n"
            "                return false;\n"
            "            }\n\n"
            "            // Without async callback support, we have to poll...\n"
            "            if (!gpu->limits.callbacks && data[n].buf)\n"
            "                while (pl_buf_poll(gpu, data[n].buf, UINT64_MAX));\n",
        ),
        (
            "video/out/vo_gpu_next.c",
            "static void uninit(struct vo *vo)\n"
            "{\n"
            "    struct priv *p = vo->priv;\n"
            "    pl_queue_destroy(&p->queue); // destroy this first\n",
            "static void uninit(struct vo *vo)\n"
            "{\n"
            "    struct priv *p = vo->priv;\n\n"
            "    // Drain any in-flight uploads.\n"
            "    if (p->gpu)\n"
            "        pl_gpu_finish(p->gpu);\n\n"
            "    pl_queue_destroy(&p->queue); // destroy this first\n",
        ),
        (
            "video/out/vulkan/context.c",
            "    const char *opt_extensions[] = {\n"
            "        VK_EXT_DESCRIPTOR_BUFFER_EXTENSION_NAME,\n",
            "    const char *opt_extensions[] = {\n"
            "#if LIBAVUTIL_VERSION_INT < AV_VERSION_INT(60, 26, 0)\n"
            "        VK_EXT_DESCRIPTOR_BUFFER_EXTENSION_NAME,\n"
            "#endif\n",
        ),
        (
            "video/out/vulkan/context.c",
            "    VkPhysicalDeviceDescriptorBufferFeaturesEXT descriptor_buffer_feature = {\n"
            "        .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DESCRIPTOR_BUFFER_FEATURES_EXT,\n"
            "        .pNext = &dynamic_rendering_feature,\n"
            "        .descriptorBuffer = true,\n"
            "        .descriptorBufferPushDescriptors = true,\n"
            "    };\n\n"
            "    VkPhysicalDeviceShaderAtomicFloatFeaturesEXT atomic_float_feature = {\n"
            "        .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SHADER_ATOMIC_FLOAT_FEATURES_EXT,\n"
            "        .pNext = &descriptor_buffer_feature,\n"
            "        .shaderBufferFloat32Atomics = true,\n"
            "        .shaderBufferFloat32AtomicAdd = true,\n"
            "    };\n",
            "#if LIBAVUTIL_VERSION_INT < AV_VERSION_INT(60, 26, 0)\n"
            "    VkPhysicalDeviceDescriptorBufferFeaturesEXT descriptor_buffer_feature = {\n"
            "        .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DESCRIPTOR_BUFFER_FEATURES_EXT,\n"
            "        .pNext = &dynamic_rendering_feature,\n"
            "        .descriptorBuffer = true,\n"
            "        .descriptorBufferPushDescriptors = true,\n"
            "    };\n"
            "#endif\n\n"
            "    VkPhysicalDeviceShaderAtomicFloatFeaturesEXT atomic_float_feature = {\n"
            "        .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SHADER_ATOMIC_FLOAT_FEATURES_EXT,\n"
            "#if LIBAVUTIL_VERSION_INT < AV_VERSION_INT(60, 26, 0)\n"
            "        .pNext = &descriptor_buffer_feature,\n"
            "#else\n"
            "        .pNext = &dynamic_rendering_feature,\n"
            "#endif\n"
            "        .shaderBufferFloat32Atomics = true,\n"
            "        .shaderBufferFloat32AtomicAdd = true,\n"
            "    };\n",
        ),
        ("meson.build", "libmpv = library('mpv', sources,", "libmpv = library('kmediampv_mpv', sources,"),
        ("meson.build", "                 version: client_api_version, install: get_option('libmpv'),\n", "                 install: get_option('libmpv'),\n"),
        (
            "meson.build",
            "if darwin\n"
            "    path_source = files('osdep/path-darwin.c')\n"
            "    timer_source = files('osdep/timer-darwin.c')\n"
            "endif\n",
            "if darwin\n"
            "    path_source = files('osdep/path-darwin.c')\n"
            "    timer_source = files('osdep/timer-darwin.c')\n"
            "    sources += files('osdep/utils-mac.c')\n"
            "endif\n",
        ),
        (
            "meson.build",
            "    sources += files('osdep/language-mac.c',\n"
            "                     'osdep/utils-mac.c',\n"
            "                     'osdep/mac/app_bridge.m',\n"
            "                     'player/clipboard/clipboard-mac.m')\n",
            "    sources += files('osdep/language-mac.c',\n"
            "                     'osdep/mac/app_bridge.m')\n",
        ),
        (
            "player/clipboard/clipboard.c",
            "#if HAVE_COCOA\n"
            "    &clipboard_backend_mac,\n",
            "#if HAVE_COCOA && HAVE_SWIFT\n"
            "    &clipboard_backend_mac,\n",
        ),
        (
            "player/main.c",
            "#if HAVE_COCOA\n"
            "    cocoa_set_input_context(NULL);\n"
            "#endif\n",
            "#if HAVE_COCOA && HAVE_SWIFT\n"
            "    cocoa_set_input_context(NULL);\n"
            "#endif\n",
        ),
        (
            "player/main.c",
            "#if HAVE_COCOA\n"
            "    cocoa_set_input_context(mpctx->input);\n"
            "#endif\n",
            "#if HAVE_COCOA && HAVE_SWIFT\n"
            "    cocoa_set_input_context(mpctx->input);\n"
            "#endif\n",
        ),
        (
            "player/main.c",
            "#if HAVE_COCOA\n"
            "    mpv_handle *ctx = mp_new_client(mpctx->clients, \"mac\");\n"
            "    cocoa_set_mpv_handle(ctx);\n"
            "#endif\n",
            "#if HAVE_COCOA && HAVE_SWIFT\n"
            "    mpv_handle *ctx = mp_new_client(mpctx->clients, \"mac\");\n"
            "    cocoa_set_mpv_handle(ctx);\n"
            "#endif\n",
        ),
        (
            "input/input.c",
            "#if HAVE_COCOA\n"
            "    struct input_opts *opts = ictx->opts;\n",
            "#if HAVE_COCOA && HAVE_SWIFT\n"
            "    struct input_opts *opts = ictx->opts;\n",
        ),
        (
            "meson.build",
            "if features['cocoa'] and features['vulkan'] and features['swift']\n"
            "    swift_sources += files('video/out/mac_common.swift',\n"
            "                           'video/out/mac/metal_layer.swift')\n"
            "    sources += files('video/out/vulkan/context_mac.m')\n"
            "endif\n",
            "if features['cocoa'] and features['vulkan']\n"
            "    sources += files('video/out/vulkan/context_mac.m')\n"
            "endif\n",
        ),
        (
            "video/out/gpu/context.c",
            "#if HAVE_COCOA && HAVE_SWIFT\n"
            "    &ra_ctx_vulkan_mac,\n"
            "#endif\n",
            "#if HAVE_COCOA\n"
            "    &ra_ctx_vulkan_mac,\n"
            "#endif\n",
        ),
        (
            "video/out/gpu/context.c",
            "extern const struct ra_ctx_fns ra_ctx_vulkan_mac;\n",
            "extern const struct ra_ctx_fns ra_ctx_vulkan_mac;\n"
            "extern const struct ra_ctx_fns ra_ctx_vulkan_ios;\n",
        ),
        (
            "video/out/gpu/context.c",
            "#if HAVE_COCOA\n"
            "    &ra_ctx_vulkan_mac,\n"
            "#endif\n"
            "#endif\n\n"
            "// OpenGL contexts:\n",
            "#if HAVE_COCOA\n"
            "    &ra_ctx_vulkan_mac,\n"
            "#endif\n"
            "#if HAVE_IOS_VULKAN\n"
            "    &ra_ctx_vulkan_ios,\n"
            "#endif\n"
            "#endif\n\n"
            "// OpenGL contexts:\n",
        ),
        (
            "video/out/vulkan/common.h",
            "#if HAVE_COCOA\n"
            "#define VK_USE_PLATFORM_METAL_EXT\n"
            "#endif\n",
            "#if HAVE_COCOA || HAVE_IOS_VULKAN\n"
            "#define VK_USE_PLATFORM_METAL_EXT\n"
            "#endif\n",
        ),
        (
            "meson.build",
            "if features['vulkan'] and features['x11']\n"
            "     sources += files('video/out/vulkan/context_xlib.c')\n"
            "endif\n\n"
            "features += {'vk-khr-display': vulkan.type_name() == 'internal' or\n",
            "if features['vulkan'] and features['x11']\n"
            "     sources += files('video/out/vulkan/context_xlib.c')\n"
            "endif\n\n"
            "ios_vulkan = false\n"
            "if darwin\n"
            "    ios_vulkan_frameworks = dependency(\n"
            "        'appleframeworks',\n"
            "        modules: ['UIKit', 'Metal', 'QuartzCore'],\n"
            "        required: false,\n"
            "    )\n"
            "    ios_vulkan = features['vulkan'] and ios_vulkan_frameworks.found()\n"
            "    if ios_vulkan\n"
            "        dependencies += ios_vulkan_frameworks\n"
            "        sources += files('video/out/vulkan/context_ios.m')\n"
            "    endif\n"
            "endif\n"
            "features += {'ios-vulkan': ios_vulkan}\n\n"
            "features += {'vk-khr-display': vulkan.type_name() == 'internal' or\n",
        ),
        (
            "meson.build",
            "if features['videotoolbox-gl'] or features['videotoolbox-pl'] or features['ios-gl']\n"
            "    sources += files('video/out/hwdec/hwdec_vt.c')\n"
            "endif\n",
            "if features['videotoolbox-gl'] or features['videotoolbox-pl'] or features['ios-gl']\n"
            "    dependencies += dependency('appleframeworks',\n"
            "        modules: ['CoreMedia', 'CoreVideo', 'VideoToolbox'])\n"
            "    sources += files('video/out/hwdec/hwdec_vt.c',\n"
            "                     'video/decode/vd_vt_async.m')\n"
            "endif\n",
        ),
        (
            "video/decode/vd_lavc.c",
            "#include \"video/out/vo.h\"\n\n"
            "#include \"options/m_option.h\"\n",
            "#include \"video/out/vo.h\"\n\n"
            "#if HAVE_VIDEOTOOLBOX_GL || HAVE_VIDEOTOOLBOX_PL || HAVE_IOS_GL\n"
            "#include \"video/decode/vd_vt_async.h\"\n"
            "#endif\n\n"
            "#include \"options/m_option.h\"\n",
        ),
        (
            "video/decode/vd_lavc.c",
            "static struct mp_decoder *create(struct mp_filter *parent,\n"
            "                                 struct mp_codec_params *codec,\n"
            "                                 const char *decoder)\n"
            "{\n"
            "    struct mp_filter *vd = mp_filter_create(parent, &vd_lavc_filter);\n",
            "static struct mp_decoder *create(struct mp_filter *parent,\n"
            "                                 struct mp_codec_params *codec,\n"
            "                                 const char *decoder)\n"
            "{\n"
            "#if HAVE_VIDEOTOOLBOX_GL || HAVE_VIDEOTOOLBOX_PL || HAVE_IOS_GL\n"
            "    if (strcmp(decoder, VT_ASYNC_HEVC_DECODER) == 0 ||\n"
            "        strcmp(decoder, VT_ASYNC_H264_DECODER) == 0)\n"
            "        return vd_vt_async_create(parent, codec, decoder);\n"
            "#endif\n\n"
            "    struct mp_filter *vd = mp_filter_create(parent, &vd_lavc_filter);\n",
        ),
        (
            "video/decode/vd_lavc.c",
            "static void add_decoders(struct mp_decoder_list *list)\n"
            "{\n"
            "    mp_add_lavc_decoders(list, AVMEDIA_TYPE_VIDEO);\n"
            "}\n",
            "static void add_decoders(struct mp_decoder_list *list)\n"
            "{\n"
            "#if HAVE_VIDEOTOOLBOX_GL || HAVE_VIDEOTOOLBOX_PL\n"
            "    mp_add_decoder(list, \"hevc\", VT_ASYNC_HEVC_DECODER,\n"
            "                   \"HEVC via pipelined VideoToolbox\");\n"
            "    mp_add_decoder(list, \"h264\", VT_ASYNC_H264_DECODER,\n"
            "                   \"H.264 via pipelined VideoToolbox\");\n"
            "#endif\n"
            "    mp_add_lavc_decoders(list, AVMEDIA_TYPE_VIDEO);\n"
            "#if HAVE_IOS_GL && !HAVE_VIDEOTOOLBOX_GL && !HAVE_VIDEOTOOLBOX_PL\n"
            "    mp_add_decoder(list, \"hevc\", VT_ASYNC_HEVC_DECODER,\n"
            "                   \"HEVC via pipelined VideoToolbox\");\n"
            "    mp_add_decoder(list, \"h264\", VT_ASYNC_H264_DECODER,\n"
            "                   \"H.264 via pipelined VideoToolbox\");\n"
            "#endif\n"
            "}\n",
        ),
    ),
}


ADDITIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "mpv": (
        (
            "video/out/vulkan/context_ios.m",
            "native/mpv-patches/video/out/vulkan/context_ios.m",
        ),
        (
            "video/decode/vd_vt_async.m",
            "native/mpv-patches/video/decode/vd_vt_async.m",
        ),
        (
            "video/decode/vd_vt_async.h",
            "native/mpv-patches/video/decode/vd_vt_async.h",
        ),
    ),
}


def apply_patches(sources: Path) -> dict[str, object]:
    records: list[dict[str, str]] = []
    for component, replacements in REPLACEMENTS.items():
        component_root = sources / component
        if not component_root.is_dir() or component_root.is_symlink():
            raise ValueError(f"missing extracted source directory: {component}")
        for relative, expected_hash, repository_relative in replacements:
            source = ROOT / repository_relative
            destination = component_root / relative
            if not source.is_file() or source.is_symlink():
                raise ValueError(f"missing repository patch source: {repository_relative}")
            if not destination.is_file() or destination.is_symlink():
                raise ValueError(f"replacement target is missing: {component}/{relative}")
            original = destination.read_bytes()
            before_hash = hashlib.sha256(original).hexdigest()
            if before_hash != expected_hash:
                raise ValueError(f"replacement input changed for {component}/{relative}")
            data = source.read_bytes()
            destination.write_bytes(data)
            records.append(
                {
                    "component": component,
                    "path": relative,
                    "beforeSha256": before_hash,
                    "afterSha256": hashlib.sha256(data).hexdigest(),
                }
            )
    for component, patches in PATCHES.items():
        component_root = sources / component
        if not component_root.is_dir() or component_root.is_symlink():
            raise ValueError(f"missing extracted source directory: {component}")
        for relative, before, after in patches:
            path = component_root / relative
            text = path.read_text(encoding="utf-8")
            if text.count(before) != 1:
                raise ValueError(f"patch context is not unique for {component}/{relative}")
            before_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            patched = text.replace(before, after, 1)
            path.write_text(patched, encoding="utf-8")
            records.append(
                {
                    "component": component,
                    "path": relative,
                    "beforeSha256": before_hash,
                    "afterSha256": hashlib.sha256(patched.encode("utf-8")).hexdigest(),
                }
            )
    for component, additions in ADDITIONS.items():
        component_root = sources / component
        if not component_root.is_dir() or component_root.is_symlink():
            raise ValueError(f"missing extracted source directory: {component}")
        for relative, repository_relative in additions:
            source = ROOT / repository_relative
            destination = component_root / relative
            if not source.is_file() or source.is_symlink():
                raise ValueError(f"missing repository patch source: {repository_relative}")
            if not destination.parent.is_dir() or destination.parent.is_symlink():
                raise ValueError(f"unsafe source destination parent: {component}/{relative}")
            if destination.exists() or destination.is_symlink():
                raise ValueError(f"added source already exists: {component}/{relative}")
            data = source.read_bytes()
            destination.write_bytes(data)
            records.append(
                {
                    "component": component,
                    "path": relative,
                    "beforeSha256": hashlib.sha256(b"").hexdigest(),
                    "afterSha256": hashlib.sha256(data).hexdigest(),
                }
            )
    return {"schemaVersion": 1, "patches": records}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--record", type=Path, required=True)
    args = parser.parse_args()
    result = apply_patches(args.sources.resolve(strict=True))
    args.record.parent.mkdir(parents=True, exist_ok=True)
    args.record.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
