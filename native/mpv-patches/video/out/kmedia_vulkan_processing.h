/* SPDX-License-Identifier: LGPL-2.1-or-later */
#ifndef KMEDIA_VULKAN_PROCESSING_H
#define KMEDIA_VULKAN_PROCESSING_H
#include "kmedia_vulkan_interop.h"
struct vo;
struct kmp_vulkan_processing;
struct kmp_vulkan_processing *kmp_vulkan_processing_create(pl_gpu gpu, int64_t id, struct vo *vo);
void kmp_vulkan_processing_destroy(struct kmp_vulkan_processing **context);
const struct pl_hook *kmp_vulkan_processing_hook(struct kmp_vulkan_processing *context);
bool kmp_vulkan_processing_frame(struct kmp_vulkan_processing *context, double pts,
    uint64_t frame_id, const struct pl_color_space *color);
void kmp_vulkan_processing_reset(struct kmp_vulkan_processing *context);
void kmp_vulkan_processing_poll(struct kmp_vulkan_processing *context);
bool kmp_vulkan_processing_pending(struct kmp_vulkan_processing *context);
#endif
