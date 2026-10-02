/* SPDX-License-Identifier: LGPL-2.1-or-later */
#include <math.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <libplacebo/vulkan.h>
#include <libplacebo/shaders/colorspace.h>
#include <libplacebo/shaders/sampling.h>
#include "kmedia_vulkan_interop.h"

#define SLOT_COUNT 3
#define TEXEL_BUDGET (256ull << 20)
struct shared_image {
    pl_tex texture;
    VkImage image;
    VkImageView view;
};
struct slot {
    VkCommandBuffer command;
    VkFence fence;
    /* Separate producer timelines: libplacebo may use different queues for each image.
     * The host alone signals done, and every next producer waits for that previous release.
     * One timeline shared across independent slots could signal later values prematurely. */
    VkSemaphore input_ready, output_ready, done;
    uint64_t serial;
    struct shared_image input, output;
    bool pending, successful;
    void *lease;
    struct kmp_vk_frame frame;
};
struct kmp_vulkan_interop {
    pl_gpu gpu;
    pl_vulkan vk;
    VkQueue queue;
    VkCommandPool pool;
    struct slot slots[SLOT_COUNT];
    struct kmp_vk_callbacks cb;
    uint64_t generation;
    bool failed;
};
static atomic_uint_fast64_t next_generation = 1;

static void destroy_image(struct kmp_vulkan_interop *p, struct shared_image *image)
{
    if (image->view) vkDestroyImageView(p->vk->device, image->view, NULL);
    pl_tex_destroy(p->gpu, &image->texture);
    *image = (struct shared_image){0};
}
static uint64_t image_bytes(const struct shared_image *image)
{
    return image->texture ? (uint64_t)image->texture->params.w * image->texture->params.h * 8 : 0;
}
static bool ensure_image(struct kmp_vulkan_interop *p, struct shared_image *image, int width, int height)
{
    if (image->texture && image->texture->params.w == width && image->texture->params.h == height)
        return true;
    if (width <= 0 || height <= 0 || (uint32_t)width > p->gpu->limits.max_tex_2d_dim ||
        (uint32_t)height > p->gpu->limits.max_tex_2d_dim)
        return false;
    uint64_t bytes = (uint64_t)width * height * 8;
    for (int i = 0; i < SLOT_COUNT; ++i)
        bytes += image_bytes(&p->slots[i].input) + image_bytes(&p->slots[i].output);
    if (bytes - image_bytes(image) > TEXEL_BUDGET) return false;
    pl_fmt format = pl_find_named_fmt(p->gpu, "rgba16f");
    if (!format) return false;
    struct shared_image next = { .texture = pl_tex_create(p->gpu, pl_tex_params(
        .w = width, .h = height, .format = format, .sampleable = true,
        .renderable = true, .blit_src = true, .blit_dst = true)) };
    if (!next.texture) return false;
    VkFormat vk_format;
    VkImageUsageFlags usage;
    next.image = pl_vulkan_unwrap(p->gpu, next.texture, &vk_format, &usage);
    if (!next.image || vk_format != VK_FORMAT_R16G16B16A16_SFLOAT ||
        !(usage & VK_IMAGE_USAGE_TRANSFER_DST_BIT)) goto fail;
    VkImageViewCreateInfo view = {
        .sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO, .image = next.image,
        .viewType = VK_IMAGE_VIEW_TYPE_2D, .format = vk_format,
        .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1},
    };
    if (vkCreateImageView(p->vk->device, &view, NULL, &next.view) != VK_SUCCESS) goto fail;
    destroy_image(p, image);
    *image = next;
    return true;
fail:
    destroy_image(p, &next);
    return false;
}
static void mark_failed(struct kmp_vulkan_interop *p)
{
    if (!p->failed) { p->failed = true; p->cb.failed(p->cb.opaque); }
}
static void complete(struct kmp_vulkan_interop *p, struct slot *s, bool success)
{
    if (s->lease) p->cb.completed(p->cb.opaque, s->lease, success && s->successful, &s->frame);
    s->lease = NULL;
    s->pending = false;
}
void kmp_vulkan_interop_poll(struct kmp_vulkan_interop *p)
{
    if (!p) return;
    for (int i = 0; i < SLOT_COUNT; ++i) {
        struct slot *s = &p->slots[i];
        if (!s->pending) continue;
        VkResult result = vkGetFenceStatus(p->vk->device, s->fence);
        if (result == VK_NOT_READY) continue;
        if (result != VK_SUCCESS) mark_failed(p);
        if (result == VK_SUCCESS || result == VK_ERROR_DEVICE_LOST)
            complete(p, s, result == VK_SUCCESS);
    }
}
bool kmp_vulkan_interop_pending(struct kmp_vulkan_interop *p)
{
    if (p) for (int i = 0; i < SLOT_COUNT; ++i) if (p->slots[i].pending) return true;
    return false;
}
bool kmp_vulkan_interop_failed(struct kmp_vulkan_interop *p) { return !p || p->failed; }

struct kmp_vulkan_interop *kmp_vulkan_interop_create(pl_gpu gpu, const struct kmp_vk_callbacks *cb)
{
    pl_vulkan vk = pl_vulkan_get(gpu);
    if (!vk || !cb || !cb->encode || !cb->end_frame || !cb->completed || !cb->release_device || !cb->failed)
        return NULL;
    uint32_t queue_count = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(vk->phys_device, &queue_count, NULL);
    VkQueueFamilyProperties *queues = calloc(queue_count, sizeof(*queues));
    if (!queues) return NULL;
    vkGetPhysicalDeviceQueueFamilyProperties(vk->phys_device, &queue_count, queues);
    bool supports_compute = vk->queue_graphics.index < queue_count &&
        (queues[vk->queue_graphics.index].queueFlags & VK_QUEUE_COMPUTE_BIT);
    free(queues);
    if (!supports_compute) return NULL;
    struct kmp_vulkan_interop *p = calloc(1, sizeof(*p));
    if (!p) return NULL;
    p->gpu = gpu; p->vk = vk; p->cb = *cb;
    p->generation = atomic_fetch_add(&next_generation, 1);
    vkGetDeviceQueue(vk->device, vk->queue_graphics.index, 0, &p->queue);
    VkCommandPoolCreateInfo pool = {.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
        .queueFamilyIndex = vk->queue_graphics.index, .flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT};
    if (vkCreateCommandPool(vk->device, &pool, NULL, &p->pool) != VK_SUCCESS) goto fail;
    for (int i = 0; i < SLOT_COUNT; ++i) {
        struct slot *s = &p->slots[i];
        VkCommandBufferAllocateInfo allocation = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
            .commandPool = p->pool, .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1};
        if (vkAllocateCommandBuffers(vk->device, &allocation, &s->command) != VK_SUCCESS) goto fail;
        VkFenceCreateInfo fence = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
        if (vkCreateFence(vk->device, &fence, NULL, &s->fence) != VK_SUCCESS) goto fail;
        VkSemaphoreTypeCreateInfo type = {.sType = VK_STRUCTURE_TYPE_SEMAPHORE_TYPE_CREATE_INFO,
            .semaphoreType = VK_SEMAPHORE_TYPE_TIMELINE};
        VkSemaphoreCreateInfo semaphore = {.sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO, .pNext = &type};
        if (vkCreateSemaphore(vk->device, &semaphore, NULL, &s->input_ready) != VK_SUCCESS ||
            vkCreateSemaphore(vk->device, &semaphore, NULL, &s->output_ready) != VK_SUCCESS ||
            vkCreateSemaphore(vk->device, &semaphore, NULL, &s->done) != VK_SUCCESS) goto fail;
    }
    return p;
fail:
    kmp_vulkan_interop_destroy(&p);
    return NULL;
}
void kmp_vulkan_interop_destroy(struct kmp_vulkan_interop **context)
{
    struct kmp_vulkan_interop *p = *context;
    if (!p) return;
    *context = NULL;
    pl_gpu_flush(p->gpu);
    for (int i = 0; i < SLOT_COUNT; ++i) {
        struct slot *s = &p->slots[i];
        if (s->pending) {
            VkResult result = vkWaitForFences(p->vk->device, 1, &s->fence, true, UINT64_MAX);
            complete(p, s, result == VK_SUCCESS);
        }
        destroy_image(p, &s->input);
        destroy_image(p, &s->output);
    }
    /* pl_tex_destroy can defer its final semaphore waits. Finish only on teardown, never per frame. */
    pl_gpu_finish(p->gpu);
    p->cb.release_device(p->cb.opaque, p->generation);
    for (int i = 0; i < SLOT_COUNT; ++i) {
        struct slot *s = &p->slots[i];
        if (s->input_ready) vkDestroySemaphore(p->vk->device, s->input_ready, NULL);
        if (s->output_ready) vkDestroySemaphore(p->vk->device, s->output_ready, NULL);
        if (s->done) vkDestroySemaphore(p->vk->device, s->done, NULL);
        if (s->fence) vkDestroyFence(p->vk->device, s->fence, NULL);
    }
    if (p->pool) vkDestroyCommandPool(p->vk->device, p->pool, NULL);
    free(p);
}

static bool convert_gamut(pl_shader sh, enum pl_color_primaries from, enum pl_color_primaries to)
{
    pl_matrix3x3 m = pl_get_color_mapping_matrix(pl_raw_primaries_get(from), pl_raw_primaries_get(to), PL_INTENT_RELATIVE_COLORIMETRIC);
    float columns[9];
    for (int y = 0; y < 3; ++y) for (int x = 0; x < 3; ++x) columns[x * 3 + y] = m.m[y][x];
    return pl_shader_custom(sh, &(struct pl_custom_shader){.input = PL_SHADER_SIG_COLOR, .output = PL_SHADER_SIG_COLOR,
        .body = "color.rgb = kmp_gamut * color.rgb;", .description = "KMedia linear gamut conversion",
        .variables = &(struct pl_shader_var){.var = pl_var_mat3("kmp_gamut"), .data = columns}, .num_variables = 1});
}
static struct pl_color_space processing_color(const struct pl_hook_params *hp, const struct pl_color_space *source)
{
    struct pl_color_space color = hp->color;
    if (color.transfer != PL_COLOR_TRC_LINEAR && !pl_color_transfer_is_hdr(color.transfer)) {
        // SDR models use reference white = 100 nits. libplacebo's inferred display white
        // (usually 203 nits) must not push their normalized input above one and clip it.
        // Use the same reference for the inverse conversion, leaving hook metadata intact.
        color.hdr.min_luma = PL_COLOR_HDR_BLACK;
        color.hdr.max_luma = 100;
    }
    if (source && source->transfer == PL_COLOR_TRC_HLG && color.transfer == PL_COLOR_TRC_HLG) {
        color.hdr.min_luma = source->hdr.min_luma > 0 ? source->hdr.min_luma : PL_COLOR_HDR_BLACK;
        color.hdr.max_luma = source->hdr.max_luma;
    }
    return color;
}
static bool convert_input(const struct pl_hook_params *hp, const struct pl_color_space *color, pl_tex target)
{
    pl_shader sh = pl_dispatch_begin(hp->dispatch);
    bool ok = pl_shader_sample_direct(sh, pl_sample_src(.tex = hp->tex));
    pl_shader_linearize(sh, color);
    ok &= convert_gamut(sh, hp->color.primaries, PL_COLOR_PRIM_BT_2020);
    ok &= pl_shader_custom(sh, &(struct pl_custom_shader){.input = PL_SHADER_SIG_COLOR, .output = PL_SHADER_SIG_COLOR,
        .body = "color.rgb *= 203.0 / 100.0;", .description = "KMedia linear BT.2020 / 100 nits"});
    if (ok) return pl_dispatch_finish(hp->dispatch, pl_dispatch_params(.shader = &sh, .target = target));
    pl_dispatch_abort(hp->dispatch, &sh);
    return false;
}
static struct pl_hook_res convert_output(const struct pl_hook_params *hp, const struct pl_color_space *color, pl_tex source)
{
    int width = source->params.w, height = source->params.h;
    pl_tex output = hp->get_tex(hp->priv, width, height);
    if (!output) return (struct pl_hook_res){0};
    pl_shader sh = pl_dispatch_begin(hp->dispatch);
    bool ok = pl_shader_sample_direct(sh, pl_sample_src(.tex = source));
    ok &= pl_shader_custom(sh, &(struct pl_custom_shader){.input = PL_SHADER_SIG_COLOR, .output = PL_SHADER_SIG_COLOR,
        .body = "color.rgb *= 100.0 / 203.0;", .description = "KMedia absolute luminance to libplacebo"});
    ok &= convert_gamut(sh, PL_COLOR_PRIM_BT_2020, hp->color.primaries);
    pl_shader_delinearize(sh, color);
    if (!ok) { pl_dispatch_abort(hp->dispatch, &sh); return (struct pl_hook_res){0}; }
    if (!pl_dispatch_finish(hp->dispatch, pl_dispatch_params(.shader = &sh, .target = output))) return (struct pl_hook_res){0};
    pl_rect2df rect = hp->rect;
    float sx = (float)width / hp->tex->params.w, sy = (float)height / hp->tex->params.h;
    rect.x0 *= sx; rect.x1 *= sx; rect.y0 *= sy; rect.y1 *= sy;
    return (struct pl_hook_res){.output = PL_HOOK_SIG_TEX, .tex = output,
        .repr = hp->repr, .color = hp->color, .components = hp->components, .rect = rect};
}
struct pl_hook_res kmp_vulkan_interop_blank(const struct pl_hook_params *hp)
{
    pl_shader sh = pl_dispatch_begin(hp->dispatch);
    // Do not allocate an intermediate texture: this also works when the optional transport
    // has exhausted its image budget. RGB hooks run after range/matrix normalization.
    if (!pl_shader_custom(sh, &(struct pl_custom_shader){
            .input = PL_SHADER_SIG_NONE, .output = PL_SHADER_SIG_COLOR,
            .output_w = hp->tex->params.w, .output_h = hp->tex->params.h,
            .body = "color = vec4(0.0, 0.0, 0.0, 1.0);",
            .description = "KMedia required output unavailable"})) {
        pl_dispatch_abort(hp->dispatch, &sh);
        return (struct pl_hook_res){.failed = true};
    }
    return (struct pl_hook_res){.output = PL_HOOK_SIG_COLOR, .sh = sh,
        .repr = hp->repr, .color = hp->color, .components = hp->components, .rect = hp->rect};
}
static bool hold(struct kmp_vulkan_interop *p, struct shared_image *image, VkImageLayout layout, VkSemaphore sem, uint64_t value)
{
    return pl_vulkan_hold_ex(p->gpu, pl_vulkan_hold_params(.tex = image->texture, .layout = layout,
        .qf = VK_QUEUE_FAMILY_IGNORED, .semaphore = {sem, value}));
}
static void release(struct kmp_vulkan_interop *p, struct shared_image *image, VkImageLayout layout, VkSemaphore sem, uint64_t value)
{
    pl_vulkan_release_ex(p->gpu, pl_vulkan_release_params(.tex = image->texture, .layout = layout,
        .qf = VK_QUEUE_FAMILY_IGNORED, .semaphore = {sem, value}));
}
static void barrier(VkCommandBuffer command, VkImage image, VkImageLayout before, VkImageLayout after,
                    VkAccessFlags source, VkAccessFlags target)
{
    VkImageMemoryBarrier b = {.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
        .oldLayout = before, .newLayout = after, .srcAccessMask = source, .dstAccessMask = target,
        .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED, .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
        .image = image, .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
    vkCmdPipelineBarrier(command, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,
        0, 0, NULL, 0, NULL, 1, &b);
}
struct pl_hook_res kmp_vulkan_interop_process(struct kmp_vulkan_interop *p,
    const struct pl_hook_params *hp, const struct pl_color_space *source_color,
    int64_t pts_us, uint64_t frame_id, uint64_t source_revision)
{
    struct pl_hook_res bypass = {0};
    if (!p || !hp || hp->gpu != p->gpu || !hp->tex || !hp->dispatch || !hp->get_tex || p->failed) return bypass;
    kmp_vulkan_interop_poll(p);
    if (p->failed) return bypass;
    struct slot *s = NULL;
    for (int i = 0; i < SLOT_COUNT; ++i) if (!p->slots[i].pending) { s = &p->slots[i]; break; }
    if (!s) return bypass;
    if (++s->serial == UINT64_MAX) { mark_failed(p); return bypass; }
    struct pl_color_space color = processing_color(hp, source_color);
    if (!ensure_image(p, &s->input, hp->tex->params.w, hp->tex->params.h) ||
        !convert_input(hp, &color, s->input.texture)) { p->cb.failed(p->cb.opaque); return bypass; }
    if (vkResetCommandBuffer(s->command, 0) != VK_SUCCESS || vkResetFences(p->vk->device, 1, &s->fence) != VK_SUCCESS) {
        mark_failed(p); return bypass;
    }
    VkCommandBufferBeginInfo begin = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
        .flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT};
    if (vkBeginCommandBuffer(s->command, &begin) != VK_SUCCESS) { mark_failed(p); return bypass; }
    bool input_held = hold(p, &s->input, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, s->input_ready, s->serial);
    if (!input_held) { vkResetCommandBuffer(s->command, 0); mark_failed(p); return bypass; }
    s->frame = (struct kmp_vk_frame){.physical_device = (uint64_t)(uintptr_t)p->vk->phys_device,
        .device = (uint64_t)(uintptr_t)p->vk->device, .device_generation = p->generation,
        .command_buffer = (uint64_t)(uintptr_t)s->command,
        .input_image = (uint64_t)s->input.image, .input_view = (uint64_t)s->input.view,
        .source_revision = source_revision, .frame_id = frame_id, .pts_us = pts_us,
        .width = hp->tex->params.w, .height = hp->tex->params.h,
        .target_width = abs(pl_rect_w(hp->dst_rect)), .target_height = abs(pl_rect_h(hp->dst_rect)),
        .hdr = pl_color_space_is_hdr(source_color ? source_color : &hp->color)};
    struct kmp_vk_output output = {0};
    bool encoded = p->cb.encode(p->cb.opaque, &s->frame, &output);
    bool valid = encoded && output.image && output.width > 0 && output.height > 0;
    bool output_held = valid && ensure_image(p, &s->output, output.width, output.height) &&
        hold(p, &s->output, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, s->output_ready, s->serial);
    s->successful = valid && output_held;
    if (s->successful) {
        VkImage source = (VkImage)output.image;
        barrier(s->command, source, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
            VK_ACCESS_MEMORY_WRITE_BIT | VK_ACCESS_SHADER_READ_BIT, VK_ACCESS_TRANSFER_READ_BIT);
        VkImageCopy copy = {.srcSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1},
            .dstSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1}, .extent = {output.width, output.height, 1}};
        vkCmdCopyImage(s->command, source, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, s->output.image,
            VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &copy);
        barrier(s->command, source, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
            VK_ACCESS_TRANSFER_READ_BIT, VK_ACCESS_SHADER_READ_BIT);
        barrier(s->command, s->output.image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
            VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT);
    }
    s->lease = p->cb.end_frame(p->cb.opaque);
    bool submit = vkEndCommandBuffer(s->command) == VK_SUCCESS;
    VkSemaphore waits[2] = {s->input_ready, s->output_ready};
    uint64_t values[2] = {s->serial, s->serial};
    VkPipelineStageFlags stages[2] = {VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT};
    VkTimelineSemaphoreSubmitInfo timeline = {.sType = VK_STRUCTURE_TYPE_TIMELINE_SEMAPHORE_SUBMIT_INFO,
        .waitSemaphoreValueCount = output_held ? 2 : 1, .pWaitSemaphoreValues = values,
        .signalSemaphoreValueCount = 1, .pSignalSemaphoreValues = &s->serial};
    VkSubmitInfo info = {.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .pNext = &timeline,
        .waitSemaphoreCount = output_held ? 2 : 1, .pWaitSemaphores = waits, .pWaitDstStageMask = stages,
        .commandBufferCount = 1, .pCommandBuffers = &s->command, .signalSemaphoreCount = 1, .pSignalSemaphores = &s->done};
    /* hold() flushes producer commands. Serialize submission with libplacebo's queue users. */
    if (submit) {
        p->vk->lock_queue(p->vk, p->vk->queue_graphics.index, 0);
        submit = vkQueueSubmit(p->queue, 1, &info, s->fence) == VK_SUCCESS;
        p->vk->unlock_queue(p->vk, p->vk->queue_graphics.index, 0);
    }
    release(p, &s->input, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, submit ? s->done : s->input_ready, s->serial);
    if (output_held) release(p, &s->output, submit ? VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL : VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
        submit ? s->done : s->output_ready, s->serial);
    if (!submit) {
        vkResetCommandBuffer(s->command, 0);
        complete(p, s, false);
        mark_failed(p);
        return bypass;
    }
    s->pending = true;
    if (!s->successful) {
        if (encoded) p->cb.failed(p->cb.opaque);
        return bypass;
    }
    struct pl_hook_res result = convert_output(hp, &color, s->output.texture);
    if (result.output == PL_HOOK_SIG_NONE) { s->successful = false; p->cb.failed(p->cb.opaque); }
    return result;
}
