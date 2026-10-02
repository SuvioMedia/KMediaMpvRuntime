/* SPDX-License-Identifier: LGPL-2.1-or-later */
/* Real registration/frame policy with deterministic transport faults; GPU pixels are covered
 * separately by vulkan_interop.c. No production callback structure or version is mocked. */
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include "stubs/vo.h"
#include "../mpv-patches/video/out/kmedia_vulkan_processing.h"

static bool requested, unavailable, fail_creation, bypass, invalid, expect_geometry;
static int encodes, blanks, failures, releases, destroyed;
static uint64_t revision;
struct kmp_vulkan_interop { int unused; };
static struct kmp_vulkan_interop transport;
struct kmp_vulkan_interop *kmp_vulkan_interop_create(pl_gpu gpu, const struct kmp_vk_callbacks *cb)
{ (void)gpu; (void)cb; return fail_creation ? NULL : &transport; }
void kmp_vulkan_interop_destroy(struct kmp_vulkan_interop **p) { *p = NULL; destroyed++; }
void kmp_vulkan_interop_poll(struct kmp_vulkan_interop *p) { (void)p; }
bool kmp_vulkan_interop_pending(struct kmp_vulkan_interop *p) { (void)p; return false; }
bool kmp_vulkan_interop_failed(struct kmp_vulkan_interop *p) { return !p || unavailable; }
struct pl_hook_res kmp_vulkan_interop_blank(const struct pl_hook_params *hp)
{ (void)hp; blanks++; return (struct pl_hook_res){.output = PL_HOOK_SIG_COLOR}; }
struct pl_hook_res kmp_vulkan_interop_process(struct kmp_vulkan_interop *p,
    const struct pl_hook_params *hp, const struct pl_color_space *color,
    int64_t pts, uint64_t frame, uint64_t source, const struct kmp_vk_source_geometry *geometry)
{
    (void)hp; (void)color;
    assert(p && pts == 250000 && frame > 0);
    assert((geometry != NULL) == expect_geometry);
    if (geometry)
        assert(geometry->width == 320 && geometry->height == 240 &&
            geometry->crop_x0 == 16 && geometry->crop_y0 == 24 &&
            geometry->crop_x1 == 304 && geometry->crop_y1 == 216 &&
            geometry->rotation_degrees == 90 && geometry->vertical_flip == 1 &&
            geometry->pixel_aspect_num == 4 && geometry->pixel_aspect_den == 3);
    encodes++; revision = source;
    return (struct pl_hook_res){.failed = invalid, .output = bypass ? PL_HOOK_SIG_NONE : PL_HOOK_SIG_TEX};
}
static bool enabled(void *p) { (void)p; return requested; }
static bool encode(void *p, const struct kmp_vk_frame *f, struct kmp_vk_output *o)
{ (void)p; (void)f; (void)o; assert(0); return false; }
static void *end(void *p) { (void)p; assert(0); return NULL; }
static void complete(void *p, void *l, bool ok, const struct kmp_vk_frame *f)
{ (void)p; (void)l; (void)ok; (void)f; assert(0); }
static void release_device(void *p, uint64_t g) { (void)p; (void)g; }
static void failed(void *p) { (void)p; failures++; }
static void released(void *p) { (void)p; releases++; }
static enum pl_hook_sig draw(struct kmp_vulkan_processing *p, uint64_t frame)
{
    const struct pl_color_space color = {.primaries = PL_COLOR_PRIM_BT_2020, .transfer = PL_COLOR_TRC_PQ};
    if (!kmp_vulkan_processing_frame(p, .25, frame, &color)) return PL_HOOK_SIG_NONE;
    const struct pl_hook *hook = kmp_vulkan_processing_hook(p);
    struct pl_hook_res result = hook->hook(hook->priv, NULL);
    assert(!result.failed);
    return result.output;
}
int main(void)
{
    assert(kmediampv_vulkan_processing_api_version() == 1);
    const struct kmp_vk_callbacks cb = {.version = 1, .size = sizeof(cb),
        .enabled = enabled, .encode = encode, .end_frame = end, .completed = complete,
        .release_device = release_device, .failed = failed, .released = released};
    assert(kmediampv_vulkan_processing_set_output_required(-1, true) == -1);
    int64_t id = kmediampv_vulkan_processing_register(&cb);
    assert(id > 0 && !releases);
    struct vo vo = {0};
    struct kmp_vulkan_processing *p = kmp_vulkan_processing_create((pl_gpu)(uintptr_t)1, id, &vo);
    assert(p && draw(p, 1) == PL_HOOK_SIG_NONE && !encodes && !blanks);
    assert(kmediampv_vulkan_processing_set_output_required(id, true) == 0 && vo.redraws == 1);
    assert(kmediampv_vulkan_processing_set_output_required(id, true) == 0 && vo.redraws == 1);
    assert(draw(p, 1) == PL_HOOK_SIG_COLOR && !encodes && blanks == 1);
    requested = true;
    assert(draw(p, 1) == PL_HOOK_SIG_TEX && encodes == 1 && revision == 1);
    bypass = true;
    assert(draw(p, 1) == PL_HOOK_SIG_COLOR && encodes == 2);
    bypass = false; invalid = true;
    assert(draw(p, 1) == PL_HOOK_SIG_COLOR && encodes == 3);
    invalid = false; unavailable = true;
    assert(draw(p, 1) == PL_HOOK_SIG_COLOR && encodes == 3);
    unavailable = false;
    kmp_vulkan_processing_reset(p);
    assert(!kmp_vulkan_processing_frame(p, NAN, 0, NULL));
    assert(draw(p, 1) == PL_HOOK_SIG_COLOR && encodes == 3);
    assert(draw(p, 2) == PL_HOOK_SIG_TEX && encodes == 4 && revision == 2);
    assert(kmediampv_vulkan_processing_set_output_required(id, false) == 0 && vo.redraws == 2);
    bypass = true;
    assert(draw(p, 2) == PL_HOOK_SIG_NONE && encodes == 5);
    bypass = false;
    assert(kmediampv_vulkan_processing_set_output_required(id, true) == 0);
    kmediampv_vulkan_processing_unregister(id);
    assert(!releases && kmediampv_vulkan_processing_set_output_required(id, false) == -1);
    assert(draw(p, 2) == PL_HOOK_SIG_COLOR && encodes == 5);
    kmp_vulkan_processing_destroy(&p);
    assert(!p && releases == 1 && destroyed == 1);
    // Required output can be configured before a VO exists, including transport startup failure.
    id = kmediampv_vulkan_processing_register(&cb);
    assert(kmediampv_vulkan_processing_set_output_required(id, true) == 0);
    fail_creation = true;
    p = kmp_vulkan_processing_create((pl_gpu)(uintptr_t)1, id, &vo);
    assert(p && failures == 1 && draw(p, 1) == PL_HOOK_SIG_COLOR && encodes == 5);
    kmp_vulkan_processing_destroy(&p);
    assert(releases == 1);
    kmediampv_vulkan_processing_unregister(id);
    assert(releases == 2 && destroyed == 2);
    // Metadata ownership itself requires output and survives lack of the separate output flag.
    fail_creation = false;
    requested = true;
    id = kmediampv_vulkan_processing_register(&cb);
    assert(kmediampv_vulkan_processing_set_source_geometry(-1, true) == -1);
    assert(kmediampv_vulkan_processing_set_source_geometry(id, true) == 0);
    p = kmp_vulkan_processing_create((pl_gpu)(uintptr_t)1, id, &vo);
    assert(p && !kmp_vulkan_processing_owns_geometry(p)); // latched at frame boundary
    assert(draw(p, 3) == PL_HOOK_SIG_COLOR && kmp_vulkan_processing_owns_geometry(p));
    const struct kmp_vk_source_geometry geometry = {
        .width = 320, .height = 240, .crop_x0 = 16, .crop_y0 = 24, .crop_x1 = 304, .crop_y1 = 216,
        .rotation_degrees = 90, .vertical_flip = 1, .pixel_aspect_num = 4, .pixel_aspect_den = 3,
    };
    const struct pl_hook *hook = kmp_vulkan_processing_hook(p);
    kmp_vulkan_processing_source_geometry(p, &geometry);
    expect_geometry = true;
    assert(hook->hook(hook->priv, NULL).output == PL_HOOK_SIG_TEX);
    // Invalid/missing metadata never reuses the previous valid image's geometry.
    struct kmp_vk_source_geometry bad = geometry;
    bad.crop_x1 = 321;
    kmp_vulkan_processing_source_geometry(p, &bad);
    assert(hook->hook(hook->priv, NULL).output == PL_HOOK_SIG_COLOR);
    bad = geometry; bad.rotation_degrees = 91;
    kmp_vulkan_processing_source_geometry(p, &bad);
    assert(hook->hook(hook->priv, NULL).output == PL_HOOK_SIG_COLOR);
    bad = geometry; bad.pixel_aspect_den = 0;
    kmp_vulkan_processing_source_geometry(p, &bad);
    assert(hook->hook(hook->priv, NULL).output == PL_HOOK_SIG_COLOR);
    bad = geometry; bad.vertical_flip = 2;
    kmp_vulkan_processing_source_geometry(p, &bad);
    assert(hook->hook(hook->priv, NULL).output == PL_HOOK_SIG_COLOR);
    kmp_vulkan_processing_source_geometry(p, &geometry);
    assert(draw(p, 4) == PL_HOOK_SIG_COLOR); // a new draw invalidates old metadata
    kmp_vulkan_processing_source_geometry(p, &geometry);
    int redraws = vo.redraws;
    assert(kmediampv_vulkan_processing_set_source_geometry(id, false) == 0);
    assert(vo.redraws == redraws + 1 && kmp_vulkan_processing_owns_geometry(p));
    assert(hook->hook(hook->priv, NULL).output == PL_HOOK_SIG_TEX); // current draw stays latched
    expect_geometry = false;
    assert(draw(p, 4) == PL_HOOK_SIG_TEX && !kmp_vulkan_processing_owns_geometry(p));
    kmediampv_vulkan_processing_unregister(id);
    assert(kmediampv_vulkan_processing_set_source_geometry(id, true) == -1);
    kmp_vulkan_processing_destroy(&p);
    assert(releases == 3 && destroyed == 3);
    puts("PASS: ABI 1 registry; required output; geometry ownership/validation/frame isolation; redraw; bypass/failure; seek barrier; unregister; startup failure");
    return 0;
}
