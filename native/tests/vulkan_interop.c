/* SPDX-License-Identifier: LGPL-2.1-or-later */
/* Test-only readback, submissions and waits. The production transport has no CPU pixel copy. */
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <libplacebo/log.h>
#include <libplacebo/vulkan.h>
#include <libplacebo/shaders/sampling.h>
#include "../mpv-patches/video/out/kmedia_vulkan_interop.h"
#include "vulkan_fixture_spirv.h"
#define CHECK(x) assert((x) == VK_SUCCESS)
struct lease {
    VkImage image;
    VkImageView view;
    VkDeviceMemory memory;
    VkBuffer capture;
    VkDeviceMemory capture_memory;
    void *mapped;
    VkDescriptorPool descriptors;
    bool captured;
    VkDeviceSize output_offset;
};
struct fixture {
    pl_gpu gpu;
    pl_vulkan vk;
    pl_dispatch dispatch;
    struct kmp_vulkan_interop *interop;
    VkPipeline pipeline;
    VkPipelineLayout layout;
    VkDescriptorSetLayout bindings;
    VkSampler sampler;
    pl_tex results[32];
    int count, width, height, pending, peak_pending, completed, failed, releases;
    float factor, offset, captured[4], captured_output[4];
    bool bypass, invalid, capture_input;
    struct lease *encoding;
    const struct pl_color_space *source_color;
};
static uint32_t memory_type(struct fixture *f, uint32_t bits, VkMemoryPropertyFlags flags)
{
    VkPhysicalDeviceMemoryProperties props;
    vkGetPhysicalDeviceMemoryProperties(f->vk->phys_device, &props);
    for (uint32_t i = 0; i < props.memoryTypeCount; ++i)
        if ((bits & (1u << i)) && (props.memoryTypes[i].propertyFlags & flags) == flags) return i;
    abort();
}
static void barrier(VkCommandBuffer cmd, VkImage image, VkImageLayout before, VkImageLayout after,
                    VkAccessFlags from, VkAccessFlags to)
{
    VkImageMemoryBarrier b = {.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
        .srcAccessMask = from, .dstAccessMask = to, .oldLayout = before, .newLayout = after,
        .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED, .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
        .image = image, .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,
        0, 0, NULL, 0, NULL, 1, &b);
}
static void capture_input(struct fixture *f, struct lease *l, const struct kmp_vk_frame *frame)
{
    VkDevice dev = f->vk->device;
    l->output_offset = (uint64_t)frame->width * frame->height * 8;
    VkBufferCreateInfo buffer = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
        .size = l->output_offset + 8, .usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT};
    CHECK(vkCreateBuffer(dev, &buffer, NULL, &l->capture));
    VkMemoryRequirements req; vkGetBufferMemoryRequirements(dev, l->capture, &req);
    VkMemoryAllocateInfo allocation = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = req.size,
        .memoryTypeIndex = memory_type(f, req.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)};
    CHECK(vkAllocateMemory(dev, &allocation, NULL, &l->capture_memory));
    CHECK(vkBindBufferMemory(dev, l->capture, l->capture_memory, 0));
    CHECK(vkMapMemory(dev, l->capture_memory, 0, VK_WHOLE_SIZE, 0, &l->mapped));
    VkCommandBuffer cmd = (VkCommandBuffer)(uintptr_t)frame->command_buffer;
    VkImage image = (VkImage)frame->input_image;
    barrier(cmd, image, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
        VK_ACCESS_MEMORY_WRITE_BIT | VK_ACCESS_SHADER_READ_BIT, VK_ACCESS_TRANSFER_READ_BIT);
    VkBufferImageCopy copy = {.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1},
        .imageExtent = {frame->width, frame->height, 1}};
    vkCmdCopyImageToBuffer(cmd, image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, l->capture, 1, &copy);
    barrier(cmd, image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
        VK_ACCESS_TRANSFER_READ_BIT, VK_ACCESS_SHADER_READ_BIT);
    VkMemoryBarrier host = {.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER,
        .srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT, .dstAccessMask = VK_ACCESS_HOST_READ_BIT};
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &host, 0, NULL, 0, NULL);
    l->captured = true;
}
static bool encode(void *opaque, const struct kmp_vk_frame *frame, struct kmp_vk_output *out)
{
    struct fixture *f = opaque;
    assert(!f->encoding && frame->device == (uint64_t)(uintptr_t)f->vk->device);
    assert(frame->device_generation && frame->source_revision == 9 && frame->frame_id == 17 && frame->pts_us == 250000);
    assert(frame->target_width == 128 && frame->target_height == 96);
    if (f->bypass) return false;
    struct lease *l = f->encoding = calloc(1, sizeof(*l)); assert(l);
    f->pending++; if (f->pending > f->peak_pending) f->peak_pending = f->pending;
    assert(f->pending <= 3);
    VkDevice dev = f->vk->device;
    VkCommandBuffer cmd = (VkCommandBuffer)(uintptr_t)frame->command_buffer;
    if (f->capture_input) capture_input(f, l, frame);
    VkImageCreateInfo image = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,
        .imageType = VK_IMAGE_TYPE_2D, .format = VK_FORMAT_R16G16B16A16_SFLOAT,
        .extent = {f->width, f->height, 1}, .mipLevels = 1, .arrayLayers = 1,
        .samples = VK_SAMPLE_COUNT_1_BIT, .tiling = VK_IMAGE_TILING_OPTIMAL,
        .usage = VK_IMAGE_USAGE_STORAGE_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_SAMPLED_BIT};
    CHECK(vkCreateImage(dev, &image, NULL, &l->image));
    VkMemoryRequirements req; vkGetImageMemoryRequirements(dev, l->image, &req);
    VkMemoryAllocateInfo allocation = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = req.size,
        .memoryTypeIndex = memory_type(f, req.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)};
    CHECK(vkAllocateMemory(dev, &allocation, NULL, &l->memory));
    CHECK(vkBindImageMemory(dev, l->image, l->memory, 0));
    VkImageViewCreateInfo view = {.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO, .image = l->image,
        .viewType = VK_IMAGE_VIEW_TYPE_2D, .format = image.format, .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
    CHECK(vkCreateImageView(dev, &view, NULL, &l->view));
    VkDescriptorPoolSize sizes[] = {{VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 1}, {VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, 1}};
    VkDescriptorPoolCreateInfo pool = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO,
        .maxSets = 1, .poolSizeCount = 2, .pPoolSizes = sizes};
    CHECK(vkCreateDescriptorPool(dev, &pool, NULL, &l->descriptors));
    VkDescriptorSetAllocateInfo set_info = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,
        .descriptorPool = l->descriptors, .descriptorSetCount = 1, .pSetLayouts = &f->bindings};
    VkDescriptorSet set; CHECK(vkAllocateDescriptorSets(dev, &set_info, &set));
    VkDescriptorImageInfo input = {.sampler = f->sampler, .imageView = (VkImageView)frame->input_view,
        .imageLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL};
    VkDescriptorImageInfo output = {.imageView = l->view, .imageLayout = VK_IMAGE_LAYOUT_GENERAL};
    VkWriteDescriptorSet writes[] = {
        {.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = set, .dstBinding = 0, .descriptorCount = 1,
         .descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, .pImageInfo = &input},
        {.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = set, .dstBinding = 1, .descriptorCount = 1,
         .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, .pImageInfo = &output},
    };
    vkUpdateDescriptorSets(dev, 2, writes, 0, NULL);
    barrier(cmd, l->image, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_GENERAL, 0, VK_ACCESS_SHADER_WRITE_BIT);
    vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, f->pipeline);
    vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, f->layout, 0, 1, &set, 0, NULL);
    const float values[] = {f->factor, f->offset};
    vkCmdPushConstants(cmd, f->layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof(values), values);
    vkCmdDispatch(cmd, (f->width + 7) / 8, (f->height + 7) / 8, 1);
    barrier(cmd, l->image, VK_IMAGE_LAYOUT_GENERAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
        VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT);
    if (l->captured) {
        barrier(cmd, l->image, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
            VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT);
        VkBufferImageCopy copy = {.bufferOffset = l->output_offset,
            .imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1}, .imageExtent = {1, 1, 1}};
        vkCmdCopyImageToBuffer(cmd, l->image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, l->capture, 1, &copy);
        barrier(cmd, l->image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
            VK_ACCESS_TRANSFER_READ_BIT, VK_ACCESS_SHADER_READ_BIT);
        VkMemoryBarrier host = {.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER,
            .srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT, .dstAccessMask = VK_ACCESS_HOST_READ_BIT};
        vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &host, 0, NULL, 0, NULL);
    }
    *out = (struct kmp_vk_output){.image = (uint64_t)l->image,
        .width = f->invalid ? 0 : f->width, .height = f->height};
    return true;
}
static void *end_frame(void *opaque) { struct fixture *f = opaque; struct lease *l = f->encoding; f->encoding = NULL; return l; }
static void completed(void *opaque, void *value, bool success, const struct kmp_vk_frame *frame)
{
    struct fixture *f = opaque; struct lease *l = value; VkDevice dev = f->vk->device;
    assert(frame->device_generation && f->pending > 0);
    if (l->captured && success) {
        const _Float16 *pixels = l->mapped;
        for (int i = 0; i < 4; ++i) {
            f->captured[i] = pixels[i];
            f->captured_output[i] = pixels[l->output_offset / 2 + i];
        }
    }
    if (l->mapped) vkUnmapMemory(dev, l->capture_memory);
    if (l->capture) vkDestroyBuffer(dev, l->capture, NULL);
    if (l->capture_memory) vkFreeMemory(dev, l->capture_memory, NULL);
    vkDestroyDescriptorPool(dev, l->descriptors, NULL);
    vkDestroyImageView(dev, l->view, NULL);
    vkDestroyImage(dev, l->image, NULL); vkFreeMemory(dev, l->memory, NULL);
    free(l); f->pending--; f->completed++;
}
static void failed(void *opaque) { ((struct fixture *)opaque)->failed++; }
static void release_device(void *opaque, uint64_t generation) { assert(generation); ((struct fixture *)opaque)->releases++; }
static void create_pipeline(struct fixture *f)
{
    VkDevice dev = f->vk->device;
    VkDescriptorSetLayoutBinding bindings[] = {
        {.binding = 0, .descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, .descriptorCount = 1, .stageFlags = VK_SHADER_STAGE_COMPUTE_BIT},
        {.binding = 1, .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, .descriptorCount = 1, .stageFlags = VK_SHADER_STAGE_COMPUTE_BIT},
    };
    VkDescriptorSetLayoutCreateInfo layout = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
        .bindingCount = 2, .pBindings = bindings};
    CHECK(vkCreateDescriptorSetLayout(dev, &layout, NULL, &f->bindings));
    VkPushConstantRange push = {.stageFlags = VK_SHADER_STAGE_COMPUTE_BIT, .size = 8};
    VkPipelineLayoutCreateInfo pipeline_layout = {.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,
        .setLayoutCount = 1, .pSetLayouts = &f->bindings, .pushConstantRangeCount = 1, .pPushConstantRanges = &push};
    CHECK(vkCreatePipelineLayout(dev, &pipeline_layout, NULL, &f->layout));
    VkShaderModuleCreateInfo module_info = {.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,
        .codeSize = sizeof(fixture_spirv), .pCode = fixture_spirv};
    VkShaderModule module; CHECK(vkCreateShaderModule(dev, &module_info, NULL, &module));
    VkComputePipelineCreateInfo pipeline = {.sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
        .stage = {.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, .stage = VK_SHADER_STAGE_COMPUTE_BIT,
            .module = module, .pName = "main"}, .layout = f->layout};
    CHECK(vkCreateComputePipelines(dev, VK_NULL_HANDLE, 1, &pipeline, NULL, &f->pipeline));
    vkDestroyShaderModule(dev, module, NULL);
    VkSamplerCreateInfo sampler = {.sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO,
        .magFilter = VK_FILTER_NEAREST, .minFilter = VK_FILTER_NEAREST,
        .addressModeU = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
        .addressModeV = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
        .addressModeW = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE};
    CHECK(vkCreateSampler(dev, &sampler, NULL, &f->sampler));
}
static pl_tex get_texture(void *opaque, int width, int height)
{
    struct fixture *f = opaque; assert(f->count < 32);
    pl_tex tex = pl_tex_create(f->gpu, pl_tex_params(.w = width, .h = height,
        .format = pl_find_named_fmt(f->gpu, "rgba32f"), .sampleable = true, .renderable = true, .host_readable = true));
    assert(tex); f->results[f->count++] = tex; return tex;
}
static struct pl_hook_res process(struct fixture *f, const float rgba[4], struct pl_color_space color, int width, int height)
{
    size_t count = (size_t)width * height * 4;
    float *pixels = malloc(count * sizeof(float)); assert(pixels);
    for (size_t i = 0; i < count; ++i) pixels[i] = rgba[i % 4];
    pl_tex input = pl_tex_create(f->gpu, pl_tex_params(.w = width, .h = height,
        .format = pl_find_named_fmt(f->gpu, "rgba32f"), .sampleable = true, .initial_data = pixels));
    free(pixels); assert(input);
    pl_dispatch_reset_frame(f->dispatch);
    struct pl_hook_res result = {0};
    for (int attempt = 0; attempt < 3; ++attempt) {
        result = kmp_vulkan_interop_process(f->interop, &(struct pl_hook_params){.gpu = f->gpu,
            .dispatch = f->dispatch, .get_tex = get_texture, .priv = f, .tex = input,
            .color = color, .components = 4, .repr = {.sys = PL_COLOR_SYSTEM_RGB, .levels = PL_COLOR_LEVELS_FULL},
            .rect = {0, 0, width, height}, .dst_rect = {0, 0, 128, 96}},
            f->source_color ? f->source_color : &color, 250000, 17, 9);
        if (result.output != PL_HOOK_SIG_NONE || f->bypass || f->invalid) break;
        /* Test-only backpressure drain; normal renderer calls bypass instead of waiting. */
        pl_gpu_finish(f->gpu); kmp_vulkan_interop_poll(f->interop);
        assert(!kmp_vulkan_interop_failed(f->interop));
    }
    pl_tex_destroy(f->gpu, &input); return result;
}
static void check(struct fixture *f, pl_tex tex, const float expected[4], const char *label)
{
    assert(tex);
    size_t count = (size_t)tex->params.w * tex->params.h * 4;
    float *data = calloc(count, sizeof(float)); assert(data);
    assert(pl_tex_download(f->gpu, pl_tex_transfer_params(.tex = tex, .ptr = data)));
    kmp_vulkan_interop_poll(f->interop);
    for (size_t i = 0; i < count; ++i) {
        if (!isfinite(data[i]) || fabsf(data[i] - expected[i % 4]) > 0.003f * fmaxf(1, fabsf(expected[i % 4]))) {
            fprintf(stderr, "%s component %zu: %g expected %g; captured input %g %g %g %g, factor=%g offset=%g; GPU effect result=%g %g %g %g\n", label, i, data[i], expected[i % 4], f->captured[0], f->captured[1], f->captured[2], f->captured[3], f->factor, f->offset, f->captured_output[0], f->captured_output[1], f->captured_output[2], f->captured_output[3]);
            CHECK(vkDeviceWaitIdle(f->vk->device));
            assert(pl_tex_download(f->gpu, pl_tex_transfer_params(.tex = tex, .ptr = data)));
            fprintf(stderr, "Repeated download after device idle: %g %g %g %g\n", data[0], data[1], data[2], data[3]);
            abort();
        }
    }
    free(data);
}
static void clear_results(struct fixture *f)
{
    for (int i = 0; i < f->count; ++i) pl_tex_destroy(f->gpu, &f->results[i]);
    f->count = 0;
}
static void check_input(struct fixture *f, const float expected[4])
{
    for (int c = 0; c < 4; ++c) {
        if (fabsf(f->captured[c] - expected[c]) > 0.002f * fmaxf(1, fabsf(expected[c]))) {
            fprintf(stderr, "Vulkan input channel %d: %g expected %g\n", c, f->captured[c], expected[c]); abort();
        }
    }
}
static float pq_encode(float nits)
{
    const double m1 = 2610.0 / 16384, m2 = 2523.0 / 32;
    const double c1 = 3424.0 / 4096, c2 = 2413.0 / 128, c3 = 2392.0 / 128;
    double y = pow(nits / 10000.0, m1);
    return pow((c1 + c2 * y) / (1 + c3 * y), m2);
}

// Independently encoded BT.2100 reference at 1000 nits, before display adaptation.
static void hlg_encode(const float nits[4], float signal[4])
{
    double y = (0.2627 * nits[0] + 0.6780 * nits[1] + 0.0593 * nits[2]) / 1000;
    double factor = y > 0 ? pow(y, (1.0 - 1.2) / 1.2) / 1000 : 0;
    for (int c = 0; c < 3; c++) {
        double scene = nits[c] * factor;
        signal[c] = scene <= 1.0 / 12 ? sqrt(3 * scene)
            : 0.17883277 * log(12 * scene - 0.28466892) + 0.55991073;
    }
    signal[3] = nits[3];
}

int main(int argc, char **argv)
{
    pl_log log = pl_log_create(PL_API_VER, pl_log_params(.log_cb = pl_log_simple, .log_level = PL_LOG_WARN));
    pl_vulkan vk = pl_vulkan_create(log, pl_vulkan_params(.get_proc_addr = vkGetInstanceProcAddr));
    assert(vk);
    VkPhysicalDeviceDriverProperties driver = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DRIVER_PROPERTIES};
    VkPhysicalDeviceProperties2 properties = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2, .pNext = &driver};
    vkGetPhysicalDeviceProperties2(vk->phys_device, &properties);
    printf("Vulkan fixture device: %s; driver ID %d, %s, %s\n", properties.properties.deviceName,
        driver.driverID, driver.driverName, driver.driverInfo);
    fflush(stdout);
    if (argc == 2 && (!strcmp(argv[1], "--download-baseline") || !strcmp(argv[1], "--render-baseline"))) {
        bool render = !strcmp(argv[1], "--render-baseline");
        pl_dispatch dispatch = pl_dispatch_create(log, vk->gpu);
        for (int frame = 0; frame < 200; ++frame) {
            float pixels[64 * 48 * 4], data[64 * 48 * 4];
            for (size_t i = 0; i < sizeof(pixels) / sizeof(float); ++i)
                pixels[i] = frame * 0.03125f + (i % 4) * 0.125f;
            pl_tex tex = pl_tex_create(vk->gpu, pl_tex_params(.w = 64, .h = 48,
                .format = pl_find_named_fmt(vk->gpu, "rgba32f"), .host_readable = true, .sampleable = true,
                .initial_data = pixels));
            assert(tex);
            if (render) {
                pl_tex output = pl_tex_create(vk->gpu, pl_tex_params(.w = 64, .h = 48,
                    .format = pl_find_named_fmt(vk->gpu, "rgba32f"), .host_readable = true, .renderable = true));
                pl_dispatch_reset_frame(dispatch);
                pl_shader sh = pl_dispatch_begin(dispatch);
                assert(pl_shader_sample_direct(sh, pl_sample_src(.tex = tex)));
                assert(pl_shader_custom(sh, &(struct pl_custom_shader){.input = PL_SHADER_SIG_COLOR,
                    .output = PL_SHADER_SIG_COLOR, .body = "color *= 0.5;"}));
                assert(pl_dispatch_finish(dispatch, pl_dispatch_params(.shader = &sh, .target = output)));
                pl_tex_destroy(vk->gpu, &tex); tex = output;
                for (size_t i = 0; i < sizeof(pixels) / sizeof(float); ++i) pixels[i] *= 0.5;
            }
            assert(pl_tex_download(vk->gpu, pl_tex_transfer_params(.tex = tex, .ptr = data)));
            if (memcmp(pixels, data, sizeof(pixels))) {
                fprintf(stderr, "libplacebo-only %s/download mismatch at frame %d: %g %g %g %g expected %g %g %g %g\n",
                    render ? "render" : "upload", frame, data[0], data[1], data[2], data[3], pixels[0], pixels[1], pixels[2], pixels[3]);
                CHECK(vkDeviceWaitIdle(vk->device));
                assert(pl_tex_download(vk->gpu, pl_tex_transfer_params(.tex = tex, .ptr = data)));
                fprintf(stderr, "Repeated download after device idle: %g %g %g %g\n", data[0], data[1], data[2], data[3]);
                abort();
            }
            pl_tex_destroy(vk->gpu, &tex);
        }
        pl_dispatch_destroy(&dispatch); pl_vulkan_destroy(&vk); pl_log_destroy(&log);
        printf("PASS: libplacebo-only %s/download baseline, 200 frames\n", render ? "render" : "upload");
        return 0;
    }
    struct fixture f = {.gpu = vk->gpu, .vk = vk, .width = 64, .height = 48, .factor = 1, .capture_input = true};
    f.dispatch = pl_dispatch_create(log, f.gpu);
    create_pipeline(&f);
    const struct kmp_vk_callbacks cb = {.version = 1, .size = sizeof(cb), .opaque = &f,
        .encode = encode, .end_frame = end_frame, .completed = completed, .failed = failed, .release_device = release_device};
    f.interop = kmp_vulkan_interop_create(f.gpu, &cb); assert(f.interop);
    struct pl_color_space linear = {
        .primaries = PL_COLOR_PRIM_BT_2020, .transfer = PL_COLOR_TRC_LINEAR,
        .hdr = {.min_luma = 0, .max_luma = 1000},
    };
    const float extended[4] = {-0.1, 1.0, 4.9261084, 0.375};
    struct pl_hook_res result = process(&f, extended, linear, 64, 48);
    assert(result.output == PL_HOOK_SIG_TEX);
    assert(result.color.primaries == PL_COLOR_PRIM_BT_2020);
    assert(result.color.transfer == PL_COLOR_TRC_LINEAR);
    assert(result.color.hdr.max_luma == 1000);
    check(&f, result.tex, extended, "extended linear/HDR identity and alpha");
    clear_results(&f);

    f.offset = 1;
    result = process(&f, (float[4]){0, 0, 0, 0.375}, linear, 64, 48);
    check(&f, result.tex, (float[4]){100.0 / 203, 100.0 / 203, 100.0 / 203, 0.375},
          "one plugin unit is 100 nits");
    f.offset = 0;
    clear_results(&f);

    // Independent BT.709 -> BT.2020 D65 reference, pure red at 100 nits.
    struct pl_color_space sdr = {
        .primaries = PL_COLOR_PRIM_BT_709, .transfer = PL_COLOR_TRC_SRGB,
        .hdr = {.min_luma = 0, .max_luma = 100},
    };
    f.capture_input = true;
    result = process(&f, (float[4]){1, 0, 0, 1}, sdr, 64, 48);
    check(&f, result.tex, (float[4]){1, 0, 0, 1},
          "SDR gamut and 100 nit reference");
    check_input(&f, (float[4]){0.6274039, 0.0690973, 0.0163914, 1});
    clear_results(&f);

    // Display white and black inferred by libplacebo must not alter the SDR model domain.
    const enum pl_color_transfer sdr_transfers[] = {
        PL_COLOR_TRC_SRGB, PL_COLOR_TRC_BT_1886, PL_COLOR_TRC_GAMMA22, PL_COLOR_TRC_GAMMA24,
    };
    const float sdr_peaks[] = {100, 203, 400};
    for (int t = 0; t < 4; ++t) for (int peak = 0; peak < 3; ++peak) {
        sdr.transfer = sdr_transfers[t];
        sdr.hdr.max_luma = sdr_peaks[peak];
        sdr.hdr.min_luma = 0.203;
        result = process(&f, (float[4]){1, 1, 1, 1}, sdr, 64, 48);
        check(&f, result.tex, (float[4]){1, 1, 1, 1}, "SDR display-white identity");
        check_input(&f, (float[4]){1, 1, 1, 1});
        assert(result.color.hdr.max_luma == sdr_peaks[peak]);
        clear_results(&f);
    }
    sdr.transfer = PL_COLOR_TRC_SRGB;
    result = process(&f, (float[4]){0.5, 0.5, 0.5, 1}, sdr, 64, 48);
    check(&f, result.tex, (float[4]){0.5, 0.5, 0.5, 1}, "SDR mid-gray identity");
    check_input(&f, (float[4]){0.21404114, 0.21404114, 0.21404114, 1});
    clear_results(&f);

    struct pl_color_space pq = linear;
    pq.transfer = PL_COLOR_TRC_PQ;
    float value = pq_encode(1000);
    result = process(&f, (float[4]){value, value, value, 1}, pq, 64, 48);
    check(&f, result.tex, (float[4]){value, value, value, 1}, "PQ 1000 nits");
    check_input(&f, (float[4]){10, 10, 10, 1});
    clear_results(&f);

    struct pl_color_space hlg = linear;
    hlg.transfer = PL_COLOR_TRC_HLG;
    f.source_color = &hlg;
    const float hlg_nits[][4] = {
        {100, 100, 100, 1}, {1000, 1000, 1000, 1},
        {1000, 100, 30, 0.375}, {20, 600, 80, 1},
    };
    const float target_peaks[] = {400, 1000, 4000};
    const float target_blacks[] = {0.001, 0.203, 1};
    const float gains[] = {1, 0.5, 4};
    for (int target = 0; target < 3; target++) {
        struct pl_color_space adapted = hlg;
        adapted.hdr.min_luma = target_blacks[target];
        adapted.hdr.max_luma = target_peaks[target];
        for (int sample = 0; sample < 4; sample++) {
            float signal[4], expected[4];
            hlg_encode(hlg_nits[sample], signal);
            for (int c = 0; c < 4; c++)
                expected[c] = c == 3 ? hlg_nits[sample][c] : hlg_nits[sample][c] / 100;
            for (int gain = 0; gain < 3; gain++) {
                float output_nits[4], output_signal[4];
                f.factor = gains[gain];
                for (int c = 0; c < 4; c++)
                    output_nits[c] = hlg_nits[sample][c] * (c == 3 ? 1 : f.factor);
                hlg_encode(output_nits, output_signal);
                result = process(&f, signal, adapted, 64, 48);
                check(&f, result.tex, output_signal, "HLG exposure precedes display adaptation");
                check_input(&f, expected);
                assert(result.color.hdr.min_luma == adapted.hdr.min_luma);
                assert(result.color.hdr.max_luma == adapted.hdr.max_luma);
                clear_results(&f);
            }
        }
    }
    f.factor = 1;
    hlg.hdr.max_luma = 2000;
    struct pl_color_space adapted = hlg;
    adapted.hdr.max_luma = 400;
    adapted.hdr.min_luma = 0.203;
    result = process(&f, (float[4]){1, 1, 1, 1}, adapted, 64, 48);
    check(&f, result.tex, (float[4]){1, 1, 1, 1}, "HLG explicit source peak round trip");
    check_input(&f, (float[4]){20, 20, 20, 1});
    clear_results(&f);
    f.source_color = NULL;
    f.capture_input = false;

    // Queue frames before reading any result. Alternate both pool sizes
    // and colors, and verify older outputs after the shared pool is reused.
    for (int round = 0; round < 12; round++) {
        for (int frame = 0; frame < 24; frame++) {
            f.width = frame % 2 ? 127 : 64;
            f.height = frame % 2 ? 71 : 48;
            f.factor = (frame + 1) / 24.0;
            result = process(&f, (float[4]){0.25, 0.5, 1.25, 0.75}, linear,
                             frame % 3 ? 64 : 96, 48);
            assert(result.output == PL_HOOK_SIG_TEX);
            assert(result.rect.x1 == f.width && result.rect.y1 == f.height);
        }
        for (int frame = 0; frame < 24; frame++) {
            float factor = (frame + 1) / 24.0;
            check(&f, f.results[frame], (float[4]){0.25f * factor, 0.5f * factor, 1.25f * factor, 0.75},
                  "queued frame isolation and resize");
        }
        clear_results(&f);
    }
    f.bypass = true;
    result = process(&f, extended, linear, 64, 48);
    assert(result.output == PL_HOOK_SIG_NONE);
    f.bypass = false;
    f.invalid = true;
    result = process(&f, extended, linear, 64, 48);
    assert(result.output == PL_HOOK_SIG_NONE);
    f.invalid = false;
    f.factor = 1;
    result = process(&f, extended, linear, 64, 48);
    check(&f, result.tex, extended, "recover after bypass and incompatible output");
    clear_results(&f);
    // Teardown with submitted GPU work still in flight.
    result = process(&f, extended, linear, 64, 48);
    assert(result.output == PL_HOOK_SIG_TEX);
    kmp_vulkan_interop_destroy(&f.interop);
    clear_results(&f);
    assert(!f.pending && f.peak_pending <= 3 && f.releases == 1);
    assert(f.failed == 1); // deliberate invalid output, then successful recovery
    vkDestroySampler(vk->device, f.sampler, NULL);
    vkDestroyPipeline(vk->device, f.pipeline, NULL);
    vkDestroyPipelineLayout(vk->device, f.layout, NULL);
    vkDestroyDescriptorSetLayout(vk->device, f.bindings, NULL);
    pl_dispatch_destroy(&f.dispatch); pl_vulkan_destroy(&vk); pl_log_destroy(&log);
    printf("PASS: Vulkan handoff; SDR/PQ/extended linear; 37 HLG source/display cases; alpha; 288 queued resize frames; bypass/recovery; teardown; peak pending %d; completions %d\n", f.peak_pending, f.completed);
    return 0;
}
