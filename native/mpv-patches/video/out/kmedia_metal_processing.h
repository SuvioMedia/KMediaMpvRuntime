/* SPDX-License-Identifier: LGPL-2.1-or-later */
#ifndef KMEDIA_METAL_PROCESSING_H
#define KMEDIA_METAL_PROCESSING_H
#include <stdint.h>
#include <libplacebo/shaders/custom.h>

// Decoded, unrotated pixel coordinates. Apply crop, vertical flip, then clockwise rotation.
struct kmp_metal_source_geometry {
    int width, height;
    int crop_x0, crop_y0, crop_x1, crop_y1;
    int rotation_degrees, vertical_flip;
    int pixel_aspect_num, pixel_aspect_den;
};
struct kmp_metal_processing;
bool kmp_metal_processing_owns_geometry(struct kmp_metal_processing *context);
void kmp_metal_processing_source_geometry(struct kmp_metal_processing *context,
    const struct kmp_metal_source_geometry *geometry);
struct kmp_metal_processing *kmp_metal_processing_create(pl_gpu gpu, int64_t wid);
void kmp_metal_processing_destroy(struct kmp_metal_processing **context);
const struct pl_hook *kmp_metal_processing_hook(struct kmp_metal_processing *context);
bool kmp_metal_processing_frame(struct kmp_metal_processing *context, double pts, uint64_t frame_id,
                               const struct pl_color_space *source_color);

#endif
