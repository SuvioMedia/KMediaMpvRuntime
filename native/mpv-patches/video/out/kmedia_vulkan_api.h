/* SPDX-License-Identifier: LGPL-2.1-or-later */
#ifndef KMEDIA_VULKAN_API_H
#define KMEDIA_VULKAN_API_H
#include <stdbool.h>
#include <stdint.h>
#include <mpv/client.h>

/* Optional processing ABI. All renderer callbacks run on its VO thread. They must not call
 * synchronous mpv APIs. Vulkan handles are borrowed; no private player/JNI implementation is
 * included in the public runtime. RGBA16F linear BT.2020, 1 = 100 nits, texture row zero = top. */
struct kmp_vk_frame {
    uint64_t physical_device, device, device_generation, command_buffer;
    uint64_t input_image, input_view, source_revision, frame_id;
    int64_t pts_us;
    int32_t width, height, target_width, target_height;
    bool hdr;
};
struct kmp_vk_output {
    uint64_t image;
    int32_t width, height;
};
struct kmp_vk_callbacks {
    uint32_t version, size;
    void *opaque;
    bool (*enabled)(void *opaque);
    /* Commands are recording outside a render pass. Input is SHADER_READ_ONLY_OPTIMAL.
     * Output must be a same-device RGBA16F image in that layout, with TRANSFER_SRC usage.
     * Return false to bypass. end_frame is called exactly once after every encode invocation,
     * after the transport has consumed the borrowed output in the same command buffer. */
    bool (*encode)(void *opaque, const struct kmp_vk_frame *frame, struct kmp_vk_output *output);
    void *(*end_frame)(void *opaque);
    /* Invoked only after the fence signals, or after discarding an unsubmitted recording.
     * A non-null end_frame lease is delivered exactly once, including failure/bypass. */
    void (*completed)(void *opaque, void *lease, bool success, const struct kmp_vk_frame *frame);
    void (*release_device)(void *opaque, uint64_t generation);
    void (*failed)(void *opaque);
    /* Registration retains opaque until this final callback, possibly after unregister returns
     * if a renderer still holds it. This callback can also run on the unregister caller's thread.
     * Close the mpv player before releasing application resources. */
    void (*released)(void *opaque);
};

MPV_EXPORT int kmediampv_vulkan_processing_api_version(void);
/* Set the returned positive id as mpv's kmedia-vulkan-processing-id before opening video. */
MPV_EXPORT int64_t kmediampv_vulkan_processing_register(const struct kmp_vk_callbacks *callbacks);
MPV_EXPORT void kmediampv_vulkan_processing_unregister(int64_t id);
/* Thread-safe, coalesced native redraw. The host calls this when a graph is ready/disabled. */
MPV_EXPORT void kmediampv_vulkan_processing_request_frame(int64_t id);
/* Optional ABI 1 extension. When required, a missing processor output produces opaque black
 * instead of bypassing to the source. This includes disabled callbacks and seek barriers.
 * Defaults to false. Thread-safe; returns 0 for a live registration, -1 otherwise.
 * Probe this symbol before enabling host-owned geometry; old ABI 1 runtimes lack it. */
MPV_EXPORT int kmediampv_vulkan_processing_set_output_required(int64_t id, bool required);
#endif
