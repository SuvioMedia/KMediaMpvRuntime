/*
 * SPDX-License-Identifier: LGPL-2.1-or-later
 *
 * This file is part of mpv.
 *
 * mpv is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 */

#import <AppKit/AppKit.h>
#import <QuartzCore/QuartzCore.h>

#include <stdint.h>

#include <mpv/client.h>

#include "options/options.h"
#include "video/out/gpu/context.h"

#include "common.h"
#include "context.h"
#include "utils.h"

// KMediaPlayer owns and updates this host on AppKit's main thread. The mpv VO only reads
// immutable references and atomically cached geometry, so Nucleus and mpv never synchronously
// wait for the AppKit queue from their respective render threads.
@interface NSObject (KMediaMpvEmbeddedMacVkHost)
- (CAMetalLayer *)kmediampvMetalLayer;
- (int)kmediampvPixelWidth;
- (int)kmediampvPixelHeight;
- (int)kmediampvDisplayWidth;
- (int)kmediampvDisplayHeight;
- (double)kmediampvScale;
- (double)kmediampvRefreshRate;
- (void)kmediampvRecordPresentation;
- (uint64_t)kmediampvPresentedFrames;
- (BOOL)kmediampvProcessingNeedsFrame;
- (void)kmediampvSetProcessingWakeup:(void (*)(void *))callback context:(void *)context;
@end

struct priv {
    struct mpvk_ctx vk;
    NSView *embedded_view;
    CAMetalLayer *embedded_layer;
    int width;
    int height;
};

// Private, versioned capability consumed by KMediaPlayer. Stock mpv has no embedded macvk
// contract and must never be treated as if its historical macOS wid documentation still held.
MPV_EXPORT int kmediampv_embedded_macvk_api_version(void);
MPV_EXPORT uint64_t kmediampv_embedded_macvk_presented_frames(int64_t wid);

MPV_EXPORT int kmediampv_embedded_macvk_api_version(void)
{
    return 4;
}

MPV_EXPORT uint64_t kmediampv_embedded_macvk_presented_frames(int64_t wid)
{
    if (wid <= 0)
        return UINT64_MAX;

    @autoreleasepool {
        NSObject *host = (NSObject *)(intptr_t)wid;
        if (![host respondsToSelector:@selector(kmediampvPresentedFrames)])
            return UINT64_MAX;
        return [host kmediampvPresentedFrames];
    }
}

static bool attach_prepared_host(struct priv *p, int64_t wid)
{
    if (!p || wid <= 0)
        return false;

    @autoreleasepool {
        NSView *view = (NSView *)(intptr_t)wid;
        if (![view isKindOfClass:[NSView class]] ||
            ![view respondsToSelector:@selector(kmediampvMetalLayer)] ||
            ![view respondsToSelector:@selector(kmediampvPixelWidth)] ||
            ![view respondsToSelector:@selector(kmediampvPixelHeight)] ||
            ![view respondsToSelector:@selector(kmediampvDisplayWidth)] ||
            ![view respondsToSelector:@selector(kmediampvDisplayHeight)] ||
            ![view respondsToSelector:@selector(kmediampvScale)] ||
            ![view respondsToSelector:@selector(kmediampvRefreshRate)] ||
            ![view respondsToSelector:@selector(kmediampvRecordPresentation)] ||
            ![view respondsToSelector:@selector(kmediampvPresentedFrames)])
            return false;

        CAMetalLayer *layer = [view kmediampvMetalLayer];
        if (![layer isKindOfClass:[CAMetalLayer class]])
            return false;

        p->embedded_view = [view retain];
        p->embedded_layer = [layer retain];
        return true;
    }
}

static void detach_prepared_host(struct priv *p)
{
    if (!p)
        return;
    @autoreleasepool {
        [p->embedded_layer release];
        [p->embedded_view release];
        p->embedded_layer = nil;
        p->embedded_view = nil;
    }
}

struct geometry_context {
    int width;
    int height;
    int display_width;
    int display_height;
    double scale;
    double fps;
};

static bool query_geometry(struct priv *p, struct geometry_context *geometry)
{
    if (!p || !geometry || !p->embedded_view || !p->embedded_layer)
        return false;

    NSView *view = p->embedded_view;
    geometry->width = [view kmediampvPixelWidth];
    geometry->height = [view kmediampvPixelHeight];
    geometry->display_width = [view kmediampvDisplayWidth];
    geometry->display_height = [view kmediampvDisplayHeight];
    geometry->scale = [view kmediampvScale];
    geometry->fps = [view kmediampvRefreshRate];
    return geometry->width > 0 && geometry->height > 0;
}

static bool update_geometry(struct ra_ctx *ctx, int *events)
{
    struct priv *p = ctx->priv;
    struct geometry_context geometry = {0};
    if (!query_geometry(p, &geometry))
        return false;

    bool changed = geometry.width != p->width || geometry.height != p->height;
    p->width = geometry.width;
    p->height = geometry.height;
    if (changed && events)
        *events |= VO_EVENT_RESIZE | VO_EVENT_EXPOSE;
    return !ctx->swapchain || ra_vk_ctx_resize(ctx, p->width, p->height);
}

static void mac_vk_uninit(struct ra_ctx *ctx)
{
    struct priv *p = ctx->priv;
    if ([p->embedded_view respondsToSelector:@selector(kmediampvSetProcessingWakeup:context:)])
        [p->embedded_view kmediampvSetProcessingWakeup:NULL context:NULL];
    ra_vk_ctx_uninit(ctx);
    mpvk_uninit(&p->vk);
    detach_prepared_host(p);
}

static void processing_wakeup(void *context)
{
    // The host has a concrete filter/result update. Request a redraw directly;
    // a level-triggered EXPOSE event can keep the paused VO/core spinning while
    // an earlier redraw is still pending, without drawing the newly ready graph.
    vo_redraw(context);
}

static void mac_vk_swap_buffers(struct ra_ctx *ctx)
{
    struct priv *p = ctx->priv;
    [p->embedded_view kmediampvRecordPresentation];
}

static void mac_vk_get_vsync(struct ra_ctx *ctx, struct vo_vsync_info *info)
{
    (void)ctx;
    (void)info;
}

static int mac_vk_color_depth(struct ra_ctx *ctx)
{
    (void)ctx;
    return 0;
}

static bool mac_vk_check_visible(struct ra_ctx *ctx)
{
    (void)ctx;
    return true;
}

static bool mac_vk_init(struct ra_ctx *ctx)
{
    struct priv *p = ctx->priv = talloc_zero(ctx, struct priv);
    struct mpvk_ctx *vk = &p->vk;
    int msgl = ctx->opts.probing ? MSGL_V : MSGL_ERR;

    if (ctx->vo->opts->WinID <= 0) {
        MP_MSG(ctx, msgl, "Embedded macvk requires a positive NSView wid.\n");
        goto error;
    }
    if (!mpvk_init(vk, ctx, VK_EXT_METAL_SURFACE_EXTENSION_NAME))
        goto error;

    if (!attach_prepared_host(p, ctx->vo->opts->WinID)) {
        MP_MSG(ctx, msgl, "Embedded macvk wid is not a prepared KMediaPlayer Metal host.\n");
        goto error;
    }
    if ([p->embedded_view respondsToSelector:@selector(kmediampvSetProcessingWakeup:context:)])
        [p->embedded_view kmediampvSetProcessingWakeup:processing_wakeup context:ctx->vo];
    VkMetalSurfaceCreateInfoEXT mac_info = {
        .sType = VK_STRUCTURE_TYPE_METAL_SURFACE_CREATE_INFO_EXT,
        .pNext = NULL,
        .flags = 0,
        .pLayer = p->embedded_layer,
    };
    struct ra_ctx_params params = {
        .swap_buffers = mac_vk_swap_buffers,
        .get_vsync = mac_vk_get_vsync,
        .color_depth = mac_vk_color_depth,
        .check_visible = mac_vk_check_visible,
    };

    VkResult res = vkCreateMetalSurfaceEXT(
        vk->vkinst->instance, &mac_info, NULL, &vk->surface);
    if (res != VK_SUCCESS) {
        MP_MSG(ctx, msgl, "Failed creating embedded Metal surface.\n");
        goto error;
    }
    if (!ra_vk_ctx_init(ctx, vk, params, VK_PRESENT_MODE_FIFO_KHR))
        goto error;
    if (!update_geometry(ctx, NULL))
        goto error;
    return true;

error:
    mac_vk_uninit(ctx);
    return false;
}

static bool mac_vk_reconfig(struct ra_ctx *ctx)
{
    return update_geometry(ctx, NULL);
}

static int mac_vk_control(struct ra_ctx *ctx, int *events, int request, void *arg)
{
    struct priv *p = ctx->priv;
    struct geometry_context geometry = {0};
    switch (request) {
    case VOCTRL_CHECK_EVENTS:
        return update_geometry(ctx, events) ? VO_TRUE : VO_ERROR;
    case VOCTRL_GET_DISPLAY_FPS:
        if (!query_geometry(p, &geometry))
            return VO_NOTIMPL;
        if (geometry.fps <= 0.0)
            return VO_NOTIMPL;
        *(double *)arg = geometry.fps;
        return VO_TRUE;
    case VOCTRL_GET_HIDPI_SCALE:
        if (!query_geometry(p, &geometry))
            return VO_NOTIMPL;
        *(double *)arg = geometry.scale > 0.0 ? geometry.scale : 1.0;
        return VO_TRUE;
    case VOCTRL_GET_DISPLAY_RES:
        if (!query_geometry(p, &geometry))
            return VO_NOTIMPL;
        ((int *)arg)[0] = geometry.display_width;
        ((int *)arg)[1] = geometry.display_height;
        return geometry.display_width > 0 && geometry.display_height > 0
            ? VO_TRUE : VO_NOTIMPL;
    case VOCTRL_GET_WINDOW_ID:
        *(int64_t *)arg = (int64_t)(intptr_t)p->embedded_view;
        return VO_TRUE;
    case VOCTRL_UPDATE_RENDER_OPTS:
        return VO_TRUE;
    }
    return VO_NOTIMPL;
}

const struct ra_ctx_fns ra_ctx_vulkan_mac = {
    .type        = "vulkan",
    .name        = "macvk",
    .description = "embedded mac/Vulkan (via MoltenVK/Metal)",
    .reconfig    = mac_vk_reconfig,
    .control     = mac_vk_control,
    .init        = mac_vk_init,
    .uninit      = mac_vk_uninit,
};
