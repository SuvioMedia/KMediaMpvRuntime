/* SPDX-License-Identifier: LGPL-2.1-or-later */
#ifndef KMEDIA_METAL_INTEROP_H
#define KMEDIA_METAL_INTEROP_H

#include <libplacebo/shaders/custom.h>

struct kmp_metal_interop;

// All calls are serialized by the VO thread. The GPU must outlive this context.
struct kmp_metal_interop *kmp_metal_interop_create(pl_gpu gpu);
void kmp_metal_interop_destroy(struct kmp_metal_interop **context);
void *kmp_metal_interop_device(struct kmp_metal_interop *context);
bool kmp_metal_interop_failed(struct kmp_metal_interop *context);

// Borrows an MTLCommandBuffer and RGBA16Float MTLTexture in linear BT.2020,
// where 1.0 = 100 nits. Return an owned (+1) RGBA16Float texture on the same
// device, or NULL to bypass. The bridge consumes the retain in both cases.
// Encode only: the bridge commits the command buffer, including on bypass.
typedef void *(*kmp_metal_encode_fn)(void *opaque, void *command_buffer, void *input);

// GPU-only handoff. Returns a renderer-owned texture or PL_HOOK_SIG_NONE on
// failure/bypass. No CPU pixel copies or per-frame waitUntilCompleted calls.
// source_color is the decoded frame's color before libplacebo's display inference.
struct pl_hook_res kmp_metal_interop_process(
    struct kmp_metal_interop *context, const struct pl_hook_params *params,
    const struct pl_color_space *source_color, kmp_metal_encode_fn encode, void *opaque);

// Safe required-output fallback, including when transport allocation fails.
struct pl_hook_res kmp_metal_interop_blank(const struct pl_hook_params *params);

#endif
