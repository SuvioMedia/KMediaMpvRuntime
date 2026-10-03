/* SPDX-License-Identifier: LGPL-2.1-or-later */
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <math.h>
#include <stdlib.h>
#include <mpv/client.h>
#include "config.h"
#include "kmedia_metal_processing.h"
#include "kmedia_metal_interop.h"

// Optional private host protocol v1. No JNI or processing-module dependency
// enters libmpv. Callbacks must not synchronously call the mpv client API.
@interface NSObject (KMediaMpvMetalProcessing)
- (int)kmediampvProcessingApiVersion;
- (BOOL)kmediampvProcessingEnabled;
- (BOOL)kmediampvBeginProcessing:(id<MTLDevice>)device width:(int)width height:(int)height
    hdr:(BOOL)hdr pts:(int64_t)pts frameID:(uint64_t)frame_id
    targetWidth:(int)target_width targetHeight:(int)target_height;
- (id<MTLTexture>)kmediampvProcess:(id<MTLCommandBuffer>)command input:(id<MTLTexture>)input;
- (void)kmediampvProcessingFailed;
// Optional geometry extension; v1 hosts without these selectors keep their existing behavior.
- (BOOL)kmediampvOwnsSourceGeometry;
- (void)kmediampvSourceWidth:(int)width height:(int)height
    cropLeft:(int)left cropTop:(int)top cropRight:(int)right cropBottom:(int)bottom
    rotation:(int)rotation verticalFlip:(BOOL)flip aspectNum:(int)num aspectDen:(int)den;
@end

struct kmp_metal_processing {
    pl_gpu gpu;
    NSObject *host;
    struct kmp_metal_interop *interop;
    struct pl_hook hook;
    double pts;
    uint64_t frame_id;
    struct pl_color_space source_color;
    bool geometry_supported, owns_geometry, geometry_valid;
    struct kmp_metal_source_geometry geometry;
    bool enabled;
    bool attempted;
    bool encoded;
};

#if HAVE_COCOA
MPV_EXPORT int kmediampv_embedded_macvk_processing_api_version(void);
MPV_EXPORT int kmediampv_embedded_macvk_processing_api_version(void) { return 1; }
#endif
#if HAVE_IOS_VULKAN
MPV_EXPORT int kmediampv_embedded_iosvk_processing_api_version(void);
MPV_EXPORT int kmediampv_embedded_iosvk_processing_api_version(void) { return 1; }
MPV_EXPORT int kmediampv_embedded_iosvk_geometry_api_version(void);
MPV_EXPORT int kmediampv_embedded_iosvk_geometry_api_version(void) { return 1; }
#endif

static void *encode(void *opaque, void *command, void *input)
{
    struct kmp_metal_processing *p = opaque;
    id<MTLTexture> result = [p->host kmediampvProcess:command input:input];
    p->encoded = result != nil;
    return result;
}

static struct pl_hook_res fallback(struct kmp_metal_processing *p,
                                   const struct pl_hook_params *params)
{
    return p->owns_geometry ? kmp_metal_interop_blank(params) : (struct pl_hook_res){0};
}

static struct pl_hook_res process(void *opaque, const struct pl_hook_params *params)
{
    struct kmp_metal_processing *p = opaque;
    if (!p->enabled)
        return fallback(p, params);
    if (p->owns_geometry && (!p->geometry_valid ||
        p->geometry.width != params->tex->params.w || p->geometry.height != params->tex->params.h)) {
        [p->host kmediampvProcessingFailed];
        return fallback(p, params);
    }
    if (!p->attempted) {
        p->attempted = true;
        p->interop = kmp_metal_interop_create(p->gpu);
    }
    if (kmp_metal_interop_failed(p->interop)) {
        [p->host kmediampvProcessingFailed];
        return fallback(p, params);
    }
    @autoreleasepool {
        if (p->owns_geometry) {
            const struct kmp_metal_source_geometry *g = &p->geometry;
            [p->host kmediampvSourceWidth:g->width height:g->height
                cropLeft:g->crop_x0 cropTop:g->crop_y0 cropRight:g->crop_x1 cropBottom:g->crop_y1
                rotation:g->rotation_degrees verticalFlip:g->vertical_flip
                aspectNum:g->pixel_aspect_num aspectDen:g->pixel_aspect_den];
        }
        if (![p->host kmediampvBeginProcessing:kmp_metal_interop_device(p->interop)
            width:params->tex->params.w height:params->tex->params.h
            hdr:pl_color_space_is_hdr(params->orig_color ?: &params->color)
            pts:(int64_t)llround(p->pts * 1000000) frameID:p->frame_id
            targetWidth:abs(pl_rect_w(params->dst_rect))
            targetHeight:abs(pl_rect_h(params->dst_rect))])
            return fallback(p, params);
        p->encoded = false;
        struct pl_hook_res result = kmp_metal_interop_process(p->interop, params, &p->source_color, encode, p);
        if (result.output == PL_HOOK_SIG_NONE && p->encoded)
            [p->host kmediampvProcessingFailed];
        return result.output == PL_HOOK_SIG_NONE ? fallback(p, params) : result;
    }
}

bool kmp_metal_processing_owns_geometry(struct kmp_metal_processing *p)
{
    return p && p->owns_geometry;
}

void kmp_metal_processing_source_geometry(struct kmp_metal_processing *p,
    const struct kmp_metal_source_geometry *g)
{
    if (!p) return;
    p->geometry_valid = g && g->width > 0 && g->height > 0 &&
        g->crop_x0 >= 0 && g->crop_y0 >= 0 &&
        g->crop_x1 <= g->width && g->crop_y1 <= g->height &&
        g->crop_x1 > g->crop_x0 && g->crop_y1 > g->crop_y0 &&
        g->rotation_degrees >= 0 && g->rotation_degrees <= 270 && g->rotation_degrees % 90 == 0 &&
        (g->vertical_flip == 0 || g->vertical_flip == 1) &&
        g->pixel_aspect_num > 0 && g->pixel_aspect_den > 0;
    if (p->geometry_valid) p->geometry = *g;
}

struct kmp_metal_processing *kmp_metal_processing_create(pl_gpu gpu, int64_t wid)
{
    if (!gpu || wid <= 0)
        return NULL;
    NSObject *host = (NSObject *)(intptr_t)wid;
    if (![host respondsToSelector:@selector(kmediampvProcessingApiVersion)] ||
        [host kmediampvProcessingApiVersion] != 1 ||
        ![host respondsToSelector:@selector(kmediampvProcessingEnabled)] ||
        ![host respondsToSelector:@selector(kmediampvBeginProcessing:width:height:hdr:pts:frameID:targetWidth:targetHeight:)] ||
        ![host respondsToSelector:@selector(kmediampvProcess:input:)] ||
        ![host respondsToSelector:@selector(kmediampvProcessingFailed)])
        return NULL;
    struct kmp_metal_processing *p = calloc(1, sizeof(*p));
    if (!p)
        return NULL;
    p->gpu = gpu;
    p->host = [host retain];
    p->geometry_supported =
        [host respondsToSelector:@selector(kmediampvOwnsSourceGeometry)] &&
        [host respondsToSelector:@selector(kmediampvSourceWidth:height:cropLeft:cropTop:cropRight:cropBottom:rotation:verticalFlip:aspectNum:aspectDen:)];
    p->hook = (struct pl_hook) {
        .stages = PL_HOOK_RGB, .input = PL_HOOK_SIG_TEX, .priv = p,
        .hook = process, .signature = UINT64_C(0x4b4d5050524f4301),
    };
    return p;
}

void kmp_metal_processing_destroy(struct kmp_metal_processing **context)
{
    struct kmp_metal_processing *p = *context;
    if (!p)
        return;
    *context = NULL;
    kmp_metal_interop_destroy(&p->interop);
    [p->host release];
    free(p);
}

const struct pl_hook *kmp_metal_processing_hook(struct kmp_metal_processing *p)
{
    return p ? &p->hook : NULL;
}

bool kmp_metal_processing_frame(struct kmp_metal_processing *p, double pts, uint64_t frame_id,
                               const struct pl_color_space *source_color)
{
    if (!p)
        return false;
    p->pts = pts;
    p->frame_id = frame_id;
    p->source_color = source_color ? *source_color : (struct pl_color_space){0};
    // Enabled latches the host's configuration before ownership is sampled. Seek barriers may
    // disable encoding while ownership remains active: they still require a safe black output.
    bool requested = [p->host kmediampvProcessingEnabled];
    p->owns_geometry = p->geometry_supported && [p->host kmediampvOwnsSourceGeometry];
    p->geometry_valid = false;
    p->enabled = source_color && isfinite(pts) && pts > -1e10 && (requested || p->owns_geometry);
    return p->enabled;
}
