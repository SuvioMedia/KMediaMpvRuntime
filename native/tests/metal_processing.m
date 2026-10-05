/* SPDX-License-Identifier: LGPL-2.1-or-later */
/* Real host/frame policy; deterministic transport failures. GPU transport has a separate fixture. */
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <assert.h>
#include <stdio.h>
#include "../mpv-patches/video/out/kmedia_metal_processing.h"
#include "../mpv-patches/video/out/kmedia_metal_interop.h"

static bool unavailable, fail_creation, bypass, invalid;
static int encodes, blanks, failures, destroyed, geometry_calls;
struct kmp_metal_interop { int unused; };
static struct kmp_metal_interop transport;
struct kmp_metal_interop *kmp_metal_interop_create(pl_gpu gpu)
{ (void)gpu; return fail_creation ? NULL : &transport; }
void kmp_metal_interop_destroy(struct kmp_metal_interop **p) { *p = NULL; destroyed++; }
void *kmp_metal_interop_device(struct kmp_metal_interop *p) { assert(p); return NULL; }
bool kmp_metal_interop_failed(struct kmp_metal_interop *p) { return !p || unavailable; }
struct pl_hook_res kmp_metal_interop_blank(const struct pl_hook_params *hp)
{ (void)hp; blanks++; return (struct pl_hook_res){.output = PL_HOOK_SIG_COLOR}; }
struct pl_hook_res kmp_metal_interop_process(struct kmp_metal_interop *p,
    const struct pl_hook_params *hp, const struct pl_color_space *color,
    kmp_metal_encode_fn encode, void *opaque)
{
    (void)hp; (void)color; assert(p); encodes++;
    void *output = encode(opaque, NULL, NULL);
    return (struct pl_hook_res){.output = output && !invalid ? PL_HOOK_SIG_TEX : PL_HOOK_SIG_NONE};
}

@interface LegacyHost : NSObject {
@public
    bool requested, begin;
    int begins;
}
@end
@implementation LegacyHost
- (int)kmediampvProcessingApiVersion { return 1; }
- (BOOL)kmediampvProcessingEnabled { return requested; }
- (void)kmediampvProcessingFailed { failures++; }
- (BOOL)kmediampvBeginProcessing:(id<MTLDevice>)device width:(int)width height:(int)height
    hdr:(BOOL)hdr pts:(int64_t)pts frameID:(uint64_t)frame
    targetWidth:(int)tw targetHeight:(int)th {
    (void)device; assert(width == 320 && height == 240 && hdr && pts == 250000 && frame > 0);
    assert(tw == 640 && th == 360); begins++; return begin;
}
- (id<MTLTexture>)kmediampvProcess:(id<MTLCommandBuffer>)command input:(id<MTLTexture>)input {
    (void)command; (void)input; return bypass ? nil : (id<MTLTexture>)self;
}
@end
@interface GeometryHost : LegacyHost { @public bool owns; }
@end
@implementation GeometryHost
- (BOOL)kmediampvOwnsSourceGeometry { return owns; }
- (void)kmediampvSourceWidth:(int)width height:(int)height
    cropLeft:(int)left cropTop:(int)top cropRight:(int)right cropBottom:(int)bottom
    rotation:(int)rotation verticalFlip:(BOOL)flip aspectNum:(int)num aspectDen:(int)den {
    assert(width == 320 && height == 240 && left == 16 && top == 24 && right == 304 && bottom == 216);
    assert(rotation == 90 && flip && num == 4 && den == 3); geometry_calls++;
}
@end
static void frame(struct kmp_metal_processing *p) {
    const struct pl_color_space color = {.primaries = PL_COLOR_PRIM_BT_2020, .transfer = PL_COLOR_TRC_PQ};
    kmp_metal_processing_frame(p, .25, 3, &color);
}
static enum pl_hook_sig draw(struct kmp_metal_processing *p) {
    const struct pl_hook *hook = kmp_metal_processing_hook(p);
    struct pl_tex_t texture = {.params = {.w = 320, .h = 240}};
    struct pl_hook_params params = {.tex = &texture, .dst_rect = {0, 0, 640, 360},
        .color = {.primaries = PL_COLOR_PRIM_BT_2020, .transfer = PL_COLOR_TRC_PQ}};
    struct pl_hook_res result = hook->hook(hook->priv, &params);
    assert(!result.failed); return result.output;
}
int main(void) {
    @autoreleasepool {
        LegacyHost *legacy = [LegacyHost new]; legacy->begin = true;
        struct kmp_metal_processing *p = kmp_metal_processing_create((pl_gpu)(uintptr_t)1, (intptr_t)legacy);
        assert(p); frame(p); assert(draw(p) == PL_HOOK_SIG_NONE && !encodes);
        legacy->requested = true; frame(p); assert(draw(p) == PL_HOOK_SIG_TEX && encodes == 1);
        bypass = true; assert(draw(p) == PL_HOOK_SIG_NONE);
        assert(!kmp_metal_processing_owns_geometry(p) && !geometry_calls && !blanks);
        kmp_metal_processing_destroy(&p); [legacy release]; bypass = false;

        GeometryHost *host = [GeometryHost new]; host->owns = true; host->begin = true;
        p = kmp_metal_processing_create((pl_gpu)(uintptr_t)1, (intptr_t)host);
        assert(!kmp_metal_processing_owns_geometry(p)); frame(p);
        assert(kmp_metal_processing_owns_geometry(p) && draw(p) == PL_HOOK_SIG_COLOR);
        struct kmp_metal_source_geometry g = {320, 240, 16, 24, 304, 216, 90, 1, 4, 3};
        kmp_metal_processing_source_geometry(p, &g);
        assert(draw(p) == PL_HOOK_SIG_TEX && geometry_calls == 1); // effects disabled, geometry active
        host->begin = false; assert(draw(p) == PL_HOOK_SIG_COLOR); // seek/preparation barrier
        host->begin = true; bypass = true; assert(draw(p) == PL_HOOK_SIG_COLOR);
        bypass = false; invalid = true; assert(draw(p) == PL_HOOK_SIG_COLOR);
        invalid = false; unavailable = true; assert(draw(p) == PL_HOOK_SIG_COLOR);
        unavailable = false; frame(p); assert(draw(p) == PL_HOOK_SIG_COLOR); // metadata cannot leak to next frame
        struct kmp_metal_source_geometry bad[] = {g,g,g,g,g};
        bad[0].crop_x1 = 321; bad[1].rotation_degrees = 91; bad[2].pixel_aspect_den = 0;
        bad[3].vertical_flip = 2; bad[4].width = 640;
        for (size_t i = 0; i < sizeof(bad)/sizeof(*bad); ++i) {
            kmp_metal_processing_source_geometry(p, &bad[i]); assert(draw(p) == PL_HOOK_SIG_COLOR);
        }
        kmp_metal_processing_source_geometry(p, &g); assert(draw(p) == PL_HOOK_SIG_TEX);
        host->owns = false; assert(kmp_metal_processing_owns_geometry(p)); // latched at frame boundary
        frame(p); assert(!kmp_metal_processing_owns_geometry(p) && draw(p) == PL_HOOK_SIG_NONE);
        kmp_metal_processing_destroy(&p);
        host->owns = true; fail_creation = true;
        p = kmp_metal_processing_create((pl_gpu)(uintptr_t)1, (intptr_t)host);
        frame(p); kmp_metal_processing_source_geometry(p, &g); assert(draw(p) == PL_HOOK_SIG_COLOR);
        kmp_metal_processing_destroy(&p); [host release]; assert(destroyed == 3);
        printf("Metal geometry policy PASS: legacy bypass, disabled effects, barriers, faults, metadata and recovery\n");
    }
}
