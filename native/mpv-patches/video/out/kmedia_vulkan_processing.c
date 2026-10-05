/* SPDX-License-Identifier: LGPL-2.1-or-later */
#include <limits.h>
#include <math.h>
#include <stdatomic.h>
#include <stdlib.h>
#include "osdep/threads.h"
#include "vo.h"
#include "kmedia_vulkan_processing.h"

struct registration {
    struct registration *next;
    int64_t id;
    unsigned refs;
    atomic_bool live;
    atomic_bool output_required;
    atomic_bool source_geometry;
    struct vo *vo;
    struct kmp_vk_callbacks cb;
};
static mp_static_mutex registry_lock = MP_STATIC_MUTEX_INITIALIZER;
static struct registration *registrations;
static int64_t next_id = 1;
struct kmp_vulkan_processing {
    struct registration *host;
    struct kmp_vulkan_interop *interop;
    struct pl_hook hook;
    struct pl_color_space color;
    double pts;
    uint64_t frame_id, revision;
    bool enabled, requested, awaiting_new_frame;
    bool owns_geometry, geometry_valid;
    struct kmp_vk_source_geometry geometry;
};

int kmediampv_vulkan_processing_api_version(void) { return 1; }
int64_t kmediampv_vulkan_processing_register(const struct kmp_vk_callbacks *cb)
{
    if (!cb || cb->version != 1 || cb->size != sizeof(*cb) || !cb->enabled || !cb->encode ||
        !cb->end_frame || !cb->completed || !cb->release_device || !cb->failed || !cb->released)
        return 0;
    struct registration *r = calloc(1, sizeof(*r));
    if (!r) return 0;
    r->cb = *cb; r->refs = 1; atomic_init(&r->live, true);
    atomic_init(&r->output_required, false);
    atomic_init(&r->source_geometry, false);
    mp_mutex_lock(&registry_lock);
    if (next_id == INT64_MAX) { mp_mutex_unlock(&registry_lock); free(r); return 0; }
    r->id = next_id++;
    r->next = registrations; registrations = r;
    mp_mutex_unlock(&registry_lock);
    return r->id;
}
static void release_registration(struct registration *r)
{
    mp_mutex_lock(&registry_lock);
    bool last = --r->refs == 0;
    mp_mutex_unlock(&registry_lock);
    if (last) { r->cb.released(r->cb.opaque); free(r); }
}
void kmediampv_vulkan_processing_unregister(int64_t id)
{
    mp_mutex_lock(&registry_lock);
    struct registration **link = &registrations, *r = NULL;
    while (*link && (*link)->id != id) link = &(*link)->next;
    if (*link) { r = *link; *link = r->next; atomic_store(&r->live, false); }
    mp_mutex_unlock(&registry_lock);
    if (r) release_registration(r);
}
void kmediampv_vulkan_processing_request_frame(int64_t id)
{
    mp_mutex_lock(&registry_lock);
    for (struct registration *r = registrations; r; r = r->next) {
        if (r->id == id) {
            /* Keep the VO alive through its thread-safe redraw call. Destruction clears this
             * pointer under the same lock, before destroying either VO or callback state. */
            if (r->vo) vo_redraw(r->vo);
            break;
        }
    }
    mp_mutex_unlock(&registry_lock);
}
int kmediampv_vulkan_processing_set_output_required(int64_t id, bool required)
{
    int result = -1;
    mp_mutex_lock(&registry_lock);
    for (struct registration *r = registrations; r; r = r->next) {
        if (r->id == id) {
            if (atomic_exchange(&r->output_required, required) != required && r->vo)
                vo_redraw(r->vo);
            result = 0;
            break;
        }
    }
    mp_mutex_unlock(&registry_lock);
    return result;
}
int kmediampv_vulkan_processing_set_source_geometry(int64_t id, bool owned)
{
    int result = -1;
    mp_mutex_lock(&registry_lock);
    for (struct registration *r = registrations; r; r = r->next) {
        if (r->id == id) {
            if (atomic_exchange(&r->source_geometry, owned) != owned && r->vo)
                vo_redraw(r->vo);
            result = 0;
            break;
        }
    }
    mp_mutex_unlock(&registry_lock);
    return result;
}
bool kmp_vulkan_processing_owns_geometry(struct kmp_vulkan_processing *p)
{
    return p && p->owns_geometry;
}
void kmp_vulkan_processing_source_geometry(struct kmp_vulkan_processing *p,
    const struct kmp_vk_source_geometry *g)
{
    if (!p) return;
    p->geometry_valid = false;
    if (!g || g->width <= 0 || g->height <= 0 ||
        g->crop_x0 < 0 || g->crop_y0 < 0 || g->crop_x1 > g->width || g->crop_y1 > g->height ||
        g->crop_x1 <= g->crop_x0 || g->crop_y1 <= g->crop_y0 ||
        g->rotation_degrees < 0 || g->rotation_degrees >= 360 || g->rotation_degrees % 90 ||
        (g->vertical_flip != 0 && g->vertical_flip != 1) ||
        g->pixel_aspect_num <= 0 || g->pixel_aspect_den <= 0)
        return;
    p->geometry = *g;
    p->geometry_valid = true;
}
static struct pl_hook_res process(void *opaque, const struct pl_hook_params *params)
{
    struct kmp_vulkan_processing *p = opaque;
    bool required = atomic_load(&p->host->output_required) || p->owns_geometry;
    struct pl_hook_res result = {0};
    if (p->enabled && p->requested && atomic_load(&p->host->live) &&
        (!p->owns_geometry || p->geometry_valid))
        result = kmp_vulkan_interop_process(p->interop, params, &p->color,
            (int64_t)llround(p->pts * 1000000), p->frame_id, p->revision,
            p->owns_geometry ? &p->geometry : NULL);
    if (required && (result.failed || result.output == PL_HOOK_SIG_NONE))
        return kmp_vulkan_interop_blank(params);
    return result;
}
struct kmp_vulkan_processing *kmp_vulkan_processing_create(pl_gpu gpu, int64_t id, struct vo *vo)
{
    if (!gpu || id <= 0 || !vo) return NULL;
    struct kmp_vulkan_processing *p = calloc(1, sizeof(*p));
    if (!p) return NULL;
    mp_mutex_lock(&registry_lock);
    for (struct registration *r = registrations; r; r = r->next) {
        if (r->id == id && !r->vo) { r->refs++; r->vo = vo; p->host = r; break; }
    }
    mp_mutex_unlock(&registry_lock);
    if (!p->host) { free(p); return NULL; }
    p->interop = kmp_vulkan_interop_create(gpu, &p->host->cb);
    if (!p->interop) p->host->cb.failed(p->host->cb.opaque);
    p->revision = 1;
    p->hook = (struct pl_hook){.stages = PL_HOOK_RGB, .input = PL_HOOK_SIG_TEX, .priv = p,
        .hook = process, .signature = UINT64_C(0x4b4d50564b505201)};
    return p;
}
void kmp_vulkan_processing_destroy(struct kmp_vulkan_processing **context)
{
    struct kmp_vulkan_processing *p = *context;
    if (!p) return;
    *context = NULL;
    mp_mutex_lock(&registry_lock);
    p->host->vo = NULL;
    mp_mutex_unlock(&registry_lock);
    kmp_vulkan_interop_destroy(&p->interop);
    release_registration(p->host);
    free(p);
}
const struct pl_hook *kmp_vulkan_processing_hook(struct kmp_vulkan_processing *p) { return p ? &p->hook : NULL; }
bool kmp_vulkan_processing_frame(struct kmp_vulkan_processing *p, double pts, uint64_t frame_id,
                               const struct pl_color_space *color)
{
    if (!p) return false;
    kmp_vulkan_interop_poll(p->interop);
    p->owns_geometry = atomic_load(&p->host->source_geometry);
    p->geometry_valid = false;
    // An empty redraw is not a newly decoded frame and must not open a seek barrier.
    if (!color || !isfinite(pts) || fabs(pts) >= 1e10) {
        p->enabled = p->requested = false;
        return false;
    }
    if (p->awaiting_new_frame && frame_id != p->frame_id) p->awaiting_new_frame = false;
    p->pts = pts; p->frame_id = frame_id; p->color = *color;
    p->requested = !p->awaiting_new_frame &&
        !kmp_vulkan_interop_failed(p->interop) && atomic_load(&p->host->live) && p->host->cb.enabled(p->host->cb.opaque);
    p->enabled = p->requested || atomic_load(&p->host->output_required) || p->owns_geometry;
    return p->enabled;
}
void kmp_vulkan_processing_reset(struct kmp_vulkan_processing *p)
{
    if (p) { p->revision++; p->enabled = p->requested = false; p->awaiting_new_frame = true; }
}
void kmp_vulkan_processing_poll(struct kmp_vulkan_processing *p) { if (p) kmp_vulkan_interop_poll(p->interop); }
bool kmp_vulkan_processing_pending(struct kmp_vulkan_processing *p) { return p && kmp_vulkan_interop_pending(p->interop); }
