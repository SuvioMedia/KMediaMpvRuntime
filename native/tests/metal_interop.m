/* SPDX-License-Identifier: LGPL-2.1-or-later */
#import <Metal/Metal.h>
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <libplacebo/log.h>
#include <libplacebo/vulkan.h>
#include "../mpv-patches/video/out/kmedia_metal_interop.h"

struct fixture {
    pl_gpu gpu;
    pl_dispatch dispatch;
    struct kmp_metal_interop *interop;
    id<MTLComputePipelineState> pipeline;
    pl_tex results[32];
    int count;
    int width, height;
    float factor;
    float offset;
    bool bypass;
    bool invalid;
    bool capture_input;
    id<MTLBuffer> captured_input;
    const struct pl_color_space *source_color;
};

static pl_tex get_texture(void *opaque, int width, int height)
{
    struct fixture *f = opaque;
    assert(f->count < 32);
    pl_tex tex = pl_tex_create(f->gpu, pl_tex_params(
        .w = width, .h = height, .format = pl_find_named_fmt(f->gpu, "rgba32f"),
        .sampleable = true, .renderable = true, .storable = true, .host_readable = true));
    assert(tex);
    f->results[f->count++] = tex;
    return tex;
}

static void *encode(void *opaque, void *buffer, void *source)
{
    struct fixture *f = opaque;
    if (f->bypass)
        return NULL;
    id<MTLTexture> input = source;
    if (f->capture_input) {
        [f->captured_input release];
        f->captured_input = [input.device newBufferWithLength:input.width * input.height * 8
            options:MTLResourceStorageModeShared];
        id<MTLBlitCommandEncoder> blit = [(id<MTLCommandBuffer>)buffer blitCommandEncoder];
        [blit copyFromTexture:input sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0, 0, 0)
            sourceSize:MTLSizeMake(input.width, input.height, 1) toBuffer:f->captured_input
            destinationOffset:0 destinationBytesPerRow:input.width * 8
            destinationBytesPerImage:input.width * input.height * 8];
        [blit endEncoding];
    }
    MTLTextureDescriptor *desc = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:f->invalid ? MTLPixelFormatBGRA8Unorm : MTLPixelFormatRGBA16Float
        width:f->width height:f->height mipmapped:NO];
    desc.storageMode = MTLStorageModePrivate;
    desc.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    id<MTLTexture> result = [input.device newTextureWithDescriptor:desc];
    assert(result);
    if (!f->invalid) {
        id<MTLComputeCommandEncoder> encoder = [(id<MTLCommandBuffer>)buffer computeCommandEncoder];
        [encoder setComputePipelineState:f->pipeline];
        [encoder setTexture:input atIndex:0];
        [encoder setTexture:result atIndex:1];
        float adjustment[2] = {f->factor, f->offset};
        [encoder setBytes:adjustment length:sizeof(adjustment) atIndex:0];
        [encoder dispatchThreads:MTLSizeMake(f->width, f->height, 1)
            threadsPerThreadgroup:MTLSizeMake(8, 8, 1)];
        [encoder endEncoding];
    }
    return result;
}

static struct pl_hook_res process(struct fixture *f, const float rgba[4],
                                 struct pl_color_space color, int width, int height)
{
    size_t count = (size_t)width * height * 4;
    float *data = malloc(count * sizeof(float));
    assert(data);
    for (size_t i = 0; i < count; i++)
        data[i] = rgba[i % 4];
    pl_tex input = pl_tex_create(f->gpu, pl_tex_params(
        .w = width, .h = height, .format = pl_find_named_fmt(f->gpu, "rgba32f"),
        .sampleable = true, .initial_data = data));
    assert(input);
    free(data);
    pl_dispatch_reset_frame(f->dispatch);
    struct pl_hook_res result = kmp_metal_interop_process(f->interop,
        &(struct pl_hook_params) {
            .gpu = f->gpu, .dispatch = f->dispatch, .get_tex = get_texture, .priv = f,
            .tex = input, .color = color, .components = 4,
            .repr = { .sys = PL_COLOR_SYSTEM_RGB, .levels = PL_COLOR_LEVELS_FULL },
            .rect = {0, 0, width, height},
        }, f->source_color ? f->source_color : &color, encode, f);
    pl_tex_destroy(f->gpu, &input);
    return result;
}

static void check(struct fixture *f, pl_tex tex, const float expected[4], const char *label)
{
    size_t count = (size_t)tex->params.w * tex->params.h * 4;
    float *data = calloc(count, sizeof(float));
    assert(data);
    assert(pl_tex_download(f->gpu, pl_tex_transfer_params(.tex = tex, .ptr = data)));
    for (size_t i = 0; i < count; i++) {
        float tolerance = 0.003f * fmaxf(1.0f, fabsf(expected[i % 4]));
        if (!isfinite(data[i]) || fabsf(data[i] - expected[i % 4]) > tolerance) {
            fprintf(stderr, "%s pixel %zu: %.8g expected %.8g\n", label, i / 4, data[i], expected[i % 4]);
            abort();
        }
    }
    free(data);
}

static void clear_results(struct fixture *f)
{
    for (int i = 0; i < f->count; i++)
        pl_tex_destroy(f->gpu, &f->results[i]);
    f->count = 0;
}

static void check_input(struct fixture *f, const float expected[4])
{
    assert(f->captured_input);
    const __fp16 *data = f->captured_input.contents;
    for (int i = 0; i < 4; i++) {
        if (fabsf(data[i] - expected[i]) > 0.002f * fmaxf(1, fabsf(expected[i]))) {
            fprintf(stderr, "Metal input channel %d: %.8g expected %.8g\n", i, (float)data[i], expected[i]);
            abort();
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

int main(void)
{
    @autoreleasepool {
        pl_log log = pl_log_create(PL_API_VER, pl_log_params(
            .log_cb = pl_log_simple, .log_level = PL_LOG_WARN));
        pl_vulkan vk = pl_vulkan_create(log, pl_vulkan_params(.get_proc_addr = vkGetInstanceProcAddr));
        assert(vk);
        struct fixture f = {.gpu = vk->gpu, .width = 64, .height = 48, .factor = 1};
        f.dispatch = pl_dispatch_create(log, f.gpu);
        f.interop = kmp_metal_interop_create(f.gpu);
        assert(f.interop);
        id<MTLDevice> device = kmp_metal_interop_device(f.interop);
        NSError *error = nil;
        id<MTLLibrary> library = [device newLibraryWithSource:
            @"#include <metal_stdlib>\nusing namespace metal;\n"
             "kernel void effect(texture2d<float, access::read> src [[texture(0)]],"
             "texture2d<float, access::write> dst [[texture(1)]], constant float2 &adjust [[buffer(0)]],"
             "uint2 p [[thread_position_in_grid]]) {"
             "if (p.x >= dst.get_width() || p.y >= dst.get_height()) return;"
             "float4 c = src.read(uint2(p.x * src.get_width() / dst.get_width(), p.y * src.get_height() / dst.get_height()));"
             "c.rgb = c.rgb * adjust.x + adjust.y; dst.write(c, p); }"
            options:nil error:&error];
        if (!library) { fprintf(stderr, "Metal compilation: %s\n", error.localizedDescription.UTF8String); abort(); }
        id<MTLFunction> function = [library newFunctionWithName:@"effect"];
        f.pipeline = [device newComputePipelineStateWithFunction:function error:&error];
        assert(f.pipeline);
        [function release]; [library release];

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
        // A required-output failure must produce opaque black without a working transport.
        pl_tex blank_target = get_texture(&f, 17, 9);
        struct pl_hook_res blank = kmp_metal_interop_blank(&(struct pl_hook_params){
            .gpu = f.gpu, .dispatch = f.dispatch, .tex = blank_target, .color = linear,
            .components = 4, .rect = {0, 0, 17, 9},
            .repr = {.sys = PL_COLOR_SYSTEM_RGB, .levels = PL_COLOR_LEVELS_FULL},
        });
        assert(blank.output == PL_HOOK_SIG_COLOR && !blank.failed);
        assert(pl_dispatch_finish(f.dispatch, pl_dispatch_params(.shader = &blank.sh, .target = blank_target)));
        check(&f, blank_target, (float[4]){0, 0, 0, 1}, "required output is opaque black");
        clear_results(&f);
        // Teardown with submitted GPU work still in flight.
        result = process(&f, extended, linear, 64, 48);
        assert(result.output == PL_HOOK_SIG_TEX);
        kmp_metal_interop_destroy(&f.interop);
        clear_results(&f);
        [f.pipeline release];
        [f.captured_input release];
        pl_dispatch_destroy(&f.dispatch);
        pl_vulkan_destroy(&vk);
        pl_log_destroy(&log);
        puts("PASS: Metal/Vulkan GPU handoff; SDR/PQ/extended linear; 37 HLG source/display cases; alpha; 288 queued resize frames; bypass; opaque-black required output; teardown");
    }
    return 0;
}
