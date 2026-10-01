/* SPDX-License-Identifier: LGPL-2.1-or-later */
#ifndef KMEDIA_METAL_PROCESSING_H
#define KMEDIA_METAL_PROCESSING_H
#include <stdint.h>
#include <libplacebo/shaders/custom.h>

struct kmp_metal_processing;
struct kmp_metal_processing *kmp_metal_processing_create(pl_gpu gpu, int64_t wid);
void kmp_metal_processing_destroy(struct kmp_metal_processing **context);
const struct pl_hook *kmp_metal_processing_hook(struct kmp_metal_processing *context);
bool kmp_metal_processing_frame(struct kmp_metal_processing *context, double pts, uint64_t frame_id,
                               const struct pl_color_space *source_color);

#endif
