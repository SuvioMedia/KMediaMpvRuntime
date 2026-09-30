/* SPDX-License-Identifier: LGPL-2.1-or-later */
#import <Metal/Metal.h>

#include <stdint.h>
#include <stdlib.h>
#include <stdatomic.h>

#define VK_USE_PLATFORM_METAL_EXT
#include <libplacebo/vulkan.h>
#include <libplacebo/shaders/colorspace.h>
#include <libplacebo/shaders/sampling.h>

#include "kmedia_metal_interop.h"

struct shared_texture {
    id<MTLTexture> metal;
    pl_tex texture;
};

@interface KMPMetalInteropStatus : NSObject {
@public
    atomic_bool failed;
}
@end
@implementation KMPMetalInteropStatus
- (instancetype)init {
    self = [super init];
    if (self) atomic_init(&failed, false);
    return self;
}
@end

struct kmp_metal_interop {
    pl_gpu gpu;
    pl_vulkan vk;
    id<MTLDevice> device;
    id<MTLCommandQueue> queue;
    id<MTLSharedEvent> event;
    id<MTLCommandBuffer> last_command;
    KMPMetalInteropStatus *status;
    VkSemaphore semaphore;
    uint64_t value;
    struct shared_texture input, output;
    PFN_vkDestroySemaphore destroy_semaphore;
};

static void destroy_texture(struct kmp_metal_interop *p, struct shared_texture *t)
{
    pl_tex_destroy(p->gpu, &t->texture);
    [t->metal release];
    t->metal = nil;
}

static bool ensure_texture(struct kmp_metal_interop *p, struct shared_texture *t,
                           int width, int height)
{
    if (t->texture && t->texture->params.w == width && t->texture->params.h == height)
        return true;
    if (width <= 0 || height <= 0 || (uint32_t)width > p->gpu->limits.max_tex_2d_dim ||
        (uint32_t)height > p->gpu->limits.max_tex_2d_dim)
        return false;

    pl_fmt format = pl_find_named_fmt(p->gpu, "rgba16f");
    if (!format)
        return false;
    MTLTextureDescriptor *desc = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA16Float
        width:width height:height mipmapped:NO];
    desc.storageMode = MTLStorageModePrivate;
    desc.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite |
                 MTLTextureUsageRenderTarget;
    id<MTLTexture> metal = [p->device newTextureWithDescriptor:desc];
    if (!metal)
        return false;
    pl_tex texture = pl_tex_create(p->gpu, pl_tex_params(
        .w = width, .h = height, .format = format,
        .sampleable = true, .renderable = true, .storable = true,
        .import_handle = PL_HANDLE_MTL_TEX,
        .shared_mem.handle.handle = metal,
    ));
    if (!texture) {
        [metal release];
        return false;
    }
    destroy_texture(p, t);
    *t = (struct shared_texture) { .metal = metal, .texture = texture };
    return true;
}

struct kmp_metal_interop *kmp_metal_interop_create(pl_gpu gpu)
{
    pl_vulkan vk = pl_vulkan_get(gpu);
    if (!vk || !(gpu->import_caps.tex & PL_HANDLE_MTL_TEX))
        return NULL;
    PFN_vkGetDeviceProcAddr get = (PFN_vkGetDeviceProcAddr)
        vk->get_proc_addr(vk->instance, "vkGetDeviceProcAddr");
    if (!get)
        return NULL;
    PFN_vkExportMetalObjectsEXT export = (PFN_vkExportMetalObjectsEXT)
        get(vk->device, "vkExportMetalObjectsEXT");
    PFN_vkCreateSemaphore create = (PFN_vkCreateSemaphore)
        get(vk->device, "vkCreateSemaphore");
    PFN_vkDestroySemaphore destroy = (PFN_vkDestroySemaphore)
        get(vk->device, "vkDestroySemaphore");
    if (!export || !create || !destroy)
        return NULL;
    struct kmp_metal_interop *p = calloc(1, sizeof(*p));
    if (!p)
        return NULL;
    p->gpu = gpu;
    p->vk = vk;
    p->destroy_semaphore = destroy;
    p->status = [[KMPMetalInteropStatus alloc] init];
    VkExportMetalDeviceInfoEXT device = {
        .sType = VK_STRUCTURE_TYPE_EXPORT_METAL_DEVICE_INFO_EXT,
    };
    VkExportMetalObjectsInfoEXT objects = {
        .sType = VK_STRUCTURE_TYPE_EXPORT_METAL_OBJECTS_INFO_EXT,
        .pNext = &device,
    };
    export(vk->device, &objects);
    p->device = [device.mtlDevice retain];
    p->queue = [p->device newCommandQueueWithMaxCommandBufferCount:3];
    if (!p->device || !p->queue)
        goto fail;

    VkExportMetalObjectCreateInfoEXT metal = {
        .sType = VK_STRUCTURE_TYPE_EXPORT_METAL_OBJECT_CREATE_INFO_EXT,
        .exportObjectType = VK_EXPORT_METAL_OBJECT_TYPE_METAL_SHARED_EVENT_BIT_EXT,
    };
    VkSemaphoreTypeCreateInfo timeline = {
        .sType = VK_STRUCTURE_TYPE_SEMAPHORE_TYPE_CREATE_INFO,
        .pNext = &metal,
        .semaphoreType = VK_SEMAPHORE_TYPE_TIMELINE,
    };
    VkSemaphoreCreateInfo semaphore = {
        .sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO,
        .pNext = &timeline,
    };
    if (create(vk->device, &semaphore, NULL, &p->semaphore) != VK_SUCCESS)
        goto fail;
    VkExportMetalSharedEventInfoEXT event = {
        .sType = VK_STRUCTURE_TYPE_EXPORT_METAL_SHARED_EVENT_INFO_EXT,
        .semaphore = p->semaphore,
    };
    objects.pNext = &event;
    export(vk->device, &objects);
    p->event = [event.mtlSharedEvent retain];
    if (!p->event)
        goto fail;
    return p;
fail:
    kmp_metal_interop_destroy(&p);
    return NULL;
}

void kmp_metal_interop_destroy(struct kmp_metal_interop **context)
{
    struct kmp_metal_interop *p = *context;
    if (!p)
        return;
    *context = NULL;
    // Flush reads dependent on Metal and retire imported images before the
    // shared timeline. This is teardown only, never the per-frame handoff.
    pl_gpu_flush(p->gpu);
    [p->last_command waitUntilCompleted];
    [p->last_command release];
    destroy_texture(p, &p->input);
    destroy_texture(p, &p->output);
    pl_gpu_finish(p->gpu);
    if (p->semaphore)
        p->destroy_semaphore(p->vk->device, p->semaphore, NULL);
    [p->event release];
    [p->queue release];
    [p->device release];
    [p->status release];
    free(p);
}

void *kmp_metal_interop_device(struct kmp_metal_interop *p)
{
    return p ? p->device : NULL;
}

bool kmp_metal_interop_failed(struct kmp_metal_interop *p)
{
    return !p || atomic_load_explicit(&p->status->failed, memory_order_acquire);
}

static bool hold(struct kmp_metal_interop *p, pl_tex texture)
{
    return pl_vulkan_hold_ex(p->gpu, pl_vulkan_hold_params(
        .tex = texture, .layout = VK_IMAGE_LAYOUT_GENERAL,
        .qf = VK_QUEUE_FAMILY_EXTERNAL,
        .semaphore = { p->semaphore, ++p->value },
    ));
}

static void release(struct kmp_metal_interop *p, pl_tex texture)
{
    pl_vulkan_release_ex(p->gpu, pl_vulkan_release_params(
        .tex = texture, .layout = VK_IMAGE_LAYOUT_GENERAL,
        .qf = VK_QUEUE_FAMILY_EXTERNAL,
        .semaphore = { p->semaphore, p->value },
    ));
}

static bool convert_gamut(pl_shader sh, enum pl_color_primaries from, enum pl_color_primaries to)
{
    pl_matrix3x3 matrix = pl_get_color_mapping_matrix(
        pl_raw_primaries_get(from), pl_raw_primaries_get(to), PL_INTENT_RELATIVE_COLORIMETRIC);
    // GLSL constructors are column-major; libplacebo's matrix is row-major.
    // Absolute luminance changes units only. Do not tone-map or clip HDR here.
    float columns[9] = {
        matrix.m[0][0], matrix.m[1][0], matrix.m[2][0],
        matrix.m[0][1], matrix.m[1][1], matrix.m[2][1],
        matrix.m[0][2], matrix.m[1][2], matrix.m[2][2],
    };
    return pl_shader_custom(sh, &(struct pl_custom_shader) {
        .input = PL_SHADER_SIG_COLOR, .output = PL_SHADER_SIG_COLOR,
        .body = "color.rgb = kmp_gamut * color.rgb;",
        .description = "KMedia linear gamut conversion",
        .variables = &(struct pl_shader_var) {
            .var = pl_var_mat3("kmp_gamut"), .data = columns,
        },
        .num_variables = 1,
    });
}

static bool convert_input(struct kmp_metal_interop *p, const struct pl_hook_params *hp)
{
    pl_shader sh = pl_dispatch_begin(hp->dispatch);
    bool ok = pl_shader_sample_direct(sh, pl_sample_src(.tex = hp->tex));
    pl_shader_linearize(sh, &hp->color);
    ok &= convert_gamut(sh, hp->color.primaries, PL_COLOR_PRIM_BT_2020);
    ok &= pl_shader_custom(sh, &(struct pl_custom_shader) {
        .input = PL_SHADER_SIG_COLOR, .output = PL_SHADER_SIG_COLOR,
        .body = "color.rgb *= 203.0 / 100.0;",
        .description = "KMedia linear BT.2020 / 100 nits",
    });
    if (ok)
        return pl_dispatch_finish(hp->dispatch, pl_dispatch_params(
            .shader = &sh, .target = p->input.texture));
    pl_dispatch_abort(hp->dispatch, &sh);
    return false;
}

struct pl_hook_res kmp_metal_interop_process(struct kmp_metal_interop *p,
    const struct pl_hook_params *hp, kmp_metal_encode_fn encode, void *opaque)
{
    struct pl_hook_res bypass = {0};
    if (!p || !hp || hp->gpu != p->gpu || !hp->tex || !hp->dispatch ||
        !hp->get_tex || !encode || p->value > UINT64_MAX - 3 ||
        atomic_load_explicit(&p->status->failed, memory_order_acquire))
        return bypass;
    @autoreleasepool {
        if (!ensure_texture(p, &p->input, hp->tex->params.w, hp->tex->params.h) ||
            !convert_input(p, hp))
            return bypass;
        id<MTLCommandBuffer> command = [p->queue commandBuffer];
        if (!command || !hold(p, p->input.texture))
            return bypass;
        [command encodeWaitForEvent:p->event value:p->value];
        id<MTLTexture> result = encode(opaque, command, p->input.metal);
        bool valid = result && result.device == p->device &&
                     result.pixelFormat == MTLPixelFormatRGBA16Float &&
                     result.textureType == MTLTextureType2D && result.sampleCount == 1 &&
                     result.width <= p->gpu->limits.max_tex_2d_dim &&
                     result.height <= p->gpu->limits.max_tex_2d_dim;
        bool output_held = valid && ensure_texture(p, &p->output, result.width, result.height) &&
                           hold(p, p->output.texture);
        if (output_held) {
            [command encodeWaitForEvent:p->event value:p->value];
            id<MTLBlitCommandEncoder> blit = [command blitCommandEncoder];
            valid = blit != nil;
            if (blit) {
                [blit copyFromTexture:result sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0, 0, 0)
                    sourceSize:MTLSizeMake(result.width, result.height, 1)
                    toTexture:p->output.metal destinationSlice:0 destinationLevel:0
                    destinationOrigin:MTLOriginMake(0, 0, 0)];
                [blit endEncoding];
            }
        }
        [result release];
        uint64_t signal_value = ++p->value;
        [command encodeSignalEvent:p->event value:signal_value];
        id<MTLSharedEvent> event = p->event;
        KMPMetalInteropStatus *status = p->status;
        [command addCompletedHandler:^(id<MTLCommandBuffer> completed) {
            if (completed.status == MTLCommandBufferStatusError) {
                atomic_store_explicit(&status->failed, true, memory_order_release);
                // A failed command buffer may omit its GPU signal. Unblock
                // the Vulkan wait and permanently bypass this failed context.
                if (event.signaledValue < signal_value)
                    event.signaledValue = signal_value;
            }
        }];
        [p->last_command release];
        p->last_command = [command retain];
        [command commit];
        release(p, p->input.texture);
        if (output_held)
            release(p, p->output.texture);
        if (!valid || !output_held)
            return bypass;

        // A renderer-owned copy gives every mixed/interpolated frame its own
        // immutable result while the two shared transport textures are reused.
        int width = p->output.texture->params.w, height = p->output.texture->params.h;
        pl_tex output = hp->get_tex(hp->priv, width, height);
        if (!output)
            return bypass;
        pl_shader sh = pl_dispatch_begin(hp->dispatch);
        if (!pl_shader_sample_direct(sh, pl_sample_src(.tex = p->output.texture)) ||
            !pl_shader_custom(sh, &(struct pl_custom_shader) {
                .input = PL_SHADER_SIG_COLOR, .output = PL_SHADER_SIG_COLOR,
                .body = "color.rgb *= 100.0 / 203.0;",
                .description = "KMedia absolute luminance to libplacebo",
            })) {
            pl_dispatch_abort(hp->dispatch, &sh);
            return bypass;
        }
        if (!convert_gamut(sh, PL_COLOR_PRIM_BT_2020, hp->color.primaries)) {
            pl_dispatch_abort(hp->dispatch, &sh);
            return bypass;
        }
        // libplacebo 7's later color pass assumes the original frame primaries
        // and transfer even though pl_hook_res exposes mutable color metadata.
        // Restore that representation before returning to the RGB stage.
        pl_shader_delinearize(sh, &hp->color);
        if (!pl_dispatch_finish(hp->dispatch, pl_dispatch_params(.shader = &sh, .target = output)))
            return bypass;
        pl_rect2df rect = hp->rect;
        float sx = (float)width / hp->tex->params.w, sy = (float)height / hp->tex->params.h;
        rect.x0 *= sx; rect.x1 *= sx;
        rect.y0 *= sy; rect.y1 *= sy;
        return (struct pl_hook_res) {
            .output = PL_HOOK_SIG_TEX, .tex = output, .repr = hp->repr,
            .color = hp->color, .components = hp->components, .rect = rect,
        };
    }
}
