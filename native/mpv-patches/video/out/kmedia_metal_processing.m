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
@end

struct kmp_metal_processing {
    pl_gpu gpu;
    NSObject *host;
    struct kmp_metal_interop *interop;
    struct pl_hook hook;
    double pts;
    uint64_t frame_id;
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
#endif

static void *encode(void *opaque, void *command, void *input)
{
    struct kmp_metal_processing *p = opaque;
    id<MTLTexture> result = [p->host kmediampvProcess:command input:input];
    p->encoded = result != nil;
    return result;
}

static struct pl_hook_res process(void *opaque, const struct pl_hook_params *params)
{
    struct kmp_metal_processing *p = opaque;
    if (!p->enabled)
        return (struct pl_hook_res){0};
    if (!p->attempted) {
        p->attempted = true;
        p->interop = kmp_metal_interop_create(p->gpu);
    }
    if (kmp_metal_interop_failed(p->interop)) {
        [p->host kmediampvProcessingFailed];
        return (struct pl_hook_res){0};
    }
    @autoreleasepool {
        if (![p->host kmediampvBeginProcessing:kmp_metal_interop_device(p->interop)
            width:params->tex->params.w height:params->tex->params.h
            hdr:pl_color_space_is_hdr(params->orig_color ?: &params->color)
            pts:(int64_t)llround(p->pts * 1000000) frameID:p->frame_id
            targetWidth:abs(pl_rect_w(params->dst_rect))
            targetHeight:abs(pl_rect_h(params->dst_rect))])
            return (struct pl_hook_res){0};
        p->encoded = false;
        struct pl_hook_res result = kmp_metal_interop_process(p->interop, params, encode, p);
        if (result.output == PL_HOOK_SIG_NONE && p->encoded)
            [p->host kmediampvProcessingFailed];
        return result;
    }
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

bool kmp_metal_processing_frame(struct kmp_metal_processing *p, double pts, uint64_t frame_id)
{
    if (!p)
        return false;
    p->pts = pts;
    p->frame_id = frame_id;
    p->enabled = isfinite(pts) && pts > -1e10 && [p->host kmediampvProcessingEnabled];
    return p->enabled;
}
