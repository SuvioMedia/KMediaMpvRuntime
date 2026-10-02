/* SPDX-License-Identifier: LGPL-2.1-or-later */
#ifndef KMEDIA_VULKAN_INTEROP_H
#define KMEDIA_VULKAN_INTEROP_H
#include <libplacebo/shaders/custom.h>
#include "kmedia_vulkan_api.h"
struct kmp_vulkan_interop;
/* Serialized on the renderer thread; the borrowed pl_gpu/device outlives this context. */
struct kmp_vulkan_interop *kmp_vulkan_interop_create(pl_gpu gpu, const struct kmp_vk_callbacks *callbacks);
void kmp_vulkan_interop_destroy(struct kmp_vulkan_interop **context);
void kmp_vulkan_interop_poll(struct kmp_vulkan_interop *context);
bool kmp_vulkan_interop_pending(struct kmp_vulkan_interop *context);
bool kmp_vulkan_interop_failed(struct kmp_vulkan_interop *context);
/* Constant opaque black in the hook's color space; no borrowed source pixels or interop
 * resources are needed. The returned shader belongs to the renderer's dispatch. */
struct pl_hook_res kmp_vulkan_interop_blank(const struct pl_hook_params *params);
struct pl_hook_res kmp_vulkan_interop_process(struct kmp_vulkan_interop *context,
    const struct pl_hook_params *params, const struct pl_color_space *source_color,
    int64_t pts_us, uint64_t frame_id, uint64_t source_revision);
#endif
