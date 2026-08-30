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

#import <QuartzCore/QuartzCore.h>

#include <math.h>

#include <mpv/client.h>

#include "options/options.h"
#include "video/out/gpu/context.h"

#include "common.h"
#include "context.h"
#include "utils.h"

struct priv {
    struct mpvk_ctx vk;
    CAMetalLayer *layer;
    int width;
    int height;
};

// Private, versioned capability consumed by KMediaPlayer. Stock mpv has no
// application-owned CAMetalLayer contract for iOS.
MPV_EXPORT int kmediampv_embedded_iosvk_api_version(void);

MPV_EXPORT int kmediampv_embedded_iosvk_api_version(void)
{
    return 1;
}

static bool layer_size(struct priv *p, int *width, int *height)
{
    if (!p || !p->layer)
        return false;
    CGSize size = p->layer.drawableSize;
    if (!isfinite(size.width) || !isfinite(size.height))
        return false;
    *width = MAX(2, (int)llround(size.width));
    *height = MAX(2, (int)llround(size.height));
    return true;
}

static bool update_geometry(struct ra_ctx *ctx, int *events)
{
    struct priv *p = ctx->priv;
    int width = 0;
    int height = 0;
    if (!layer_size(p, &width, &height))
        return false;

    bool changed = width != p->width || height != p->height;
    p->width = width;
    p->height = height;
    if (changed && events)
        *events |= VO_EVENT_RESIZE | VO_EVENT_EXPOSE;
    return !ctx->swapchain || !changed || ra_vk_ctx_resize(ctx, width, height);
}

static void ios_vk_uninit(struct ra_ctx *ctx)
{
    struct priv *p = ctx->priv;
    if (!p)
        return;
    ra_vk_ctx_uninit(ctx);
    mpvk_uninit(&p->vk);
    [p->layer release];
    p->layer = nil;
}

static void ios_vk_swap_buffers(struct ra_ctx *ctx)
{
    (void)ctx;
}

static void ios_vk_get_vsync(struct ra_ctx *ctx, struct vo_vsync_info *info)
{
    (void)ctx;
    (void)info;
}

static int ios_vk_color_depth(struct ra_ctx *ctx)
{
    (void)ctx;
    return 0;
}

static bool ios_vk_check_visible(struct ra_ctx *ctx)
{
    (void)ctx;
    return true;
}

static bool ios_vk_init(struct ra_ctx *ctx)
{
    struct priv *p = ctx->priv = talloc_zero(ctx, struct priv);
    struct mpvk_ctx *vk = &p->vk;
    int msgl = ctx->opts.probing ? MSGL_V : MSGL_ERR;

    if (ctx->vo->opts->WinID <= 0) {
        MP_MSG(ctx, msgl, "Embedded iosvk requires a positive CAMetalLayer wid.\n");
        goto error;
    }
    CAMetalLayer *layer = (CAMetalLayer *)(intptr_t)ctx->vo->opts->WinID;
    if (![layer isKindOfClass:[CAMetalLayer class]]) {
        MP_MSG(ctx, msgl, "Embedded iosvk wid is not a CAMetalLayer.\n");
        goto error;
    }
    p->layer = [layer retain];
    if (!layer_size(p, &p->width, &p->height))
        goto error;
    if (!mpvk_init(vk, ctx, VK_EXT_METAL_SURFACE_EXTENSION_NAME))
        goto error;

    VkMetalSurfaceCreateInfoEXT surface_info = {
        .sType = VK_STRUCTURE_TYPE_METAL_SURFACE_CREATE_INFO_EXT,
        .pNext = NULL,
        .flags = 0,
        .pLayer = p->layer,
    };
    struct ra_ctx_params params = {
        .swap_buffers = ios_vk_swap_buffers,
        .get_vsync = ios_vk_get_vsync,
        .color_depth = ios_vk_color_depth,
        .check_visible = ios_vk_check_visible,
    };
    VkResult result = vkCreateMetalSurfaceEXT(
        vk->vkinst->instance, &surface_info, NULL, &vk->surface);
    if (result != VK_SUCCESS) {
        MP_MSG(ctx, msgl, "Failed creating the embedded iOS Metal surface.\n");
        goto error;
    }
    if (!ra_vk_ctx_init(ctx, vk, params, VK_PRESENT_MODE_FIFO_KHR))
        goto error;
    if (!ra_vk_ctx_resize(ctx, p->width, p->height))
        goto error;
    return true;

error:
    ios_vk_uninit(ctx);
    return false;
}

static bool ios_vk_reconfig(struct ra_ctx *ctx)
{
    return update_geometry(ctx, NULL);
}

static int ios_vk_control(struct ra_ctx *ctx, int *events, int request, void *arg)
{
    struct priv *p = ctx->priv;
    switch (request) {
    case VOCTRL_CHECK_EVENTS:
        return update_geometry(ctx, events) ? VO_TRUE : VO_ERROR;
    case VOCTRL_GET_HIDPI_SCALE:
        *(double *)arg = p->layer.contentsScale > 0.0
            ? p->layer.contentsScale : 1.0;
        return VO_TRUE;
    case VOCTRL_GET_WINDOW_ID:
        *(int64_t *)arg = (int64_t)(intptr_t)p->layer;
        return VO_TRUE;
    case VOCTRL_UPDATE_RENDER_OPTS:
        return VO_TRUE;
    }
    return VO_NOTIMPL;
}

const struct ra_ctx_fns ra_ctx_vulkan_ios = {
    .type        = "vulkan",
    .name        = "iosvk",
    .description = "embedded iOS/Vulkan (via MoltenVK/Metal)",
    .reconfig    = ios_vk_reconfig,
    .control     = ios_vk_control,
    .init        = ios_vk_init,
    .uninit      = ios_vk_uninit,
};
