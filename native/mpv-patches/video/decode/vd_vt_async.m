/*
 * SPDX-License-Identifier: LGPL-2.1-or-later
 *
 * Pipelined asynchronous VideoToolbox decoder for KMediaMpv on Apple platforms.
 *
 * FFmpeg's legacy VideoToolbox hwaccel waits for every submitted frame before
 * returning it to mpv. This decoder keeps compressed frames in flight and
 * hands the resulting CVPixelBuffers to mpv without a pixel copy.
 */

#import <CoreMedia/CoreMedia.h>
#import <CoreVideo/CoreVideo.h>
#import <VideoToolbox/VideoToolbox.h>

#include <libavcodec/avcodec.h>
#include <libavcodec/codec_par.h>
#include <libavcodec/packet.h>
#include <libavutil/hdr_dynamic_metadata.h>
#include <libavutil/hwcontext.h>
#include <libavutil/mastering_display_metadata.h>
#include <libavutil/pixdesc.h>
#include <libplacebo/utils/libav.h>

#include <string.h>

#include "common/codecs.h"
#include "common/common.h"
#include "common/msg.h"
#include "demux/packet.h"
#include "demux/packet_pool.h"
#include "demux/stheader.h"
#include "filters/f_decoder_wrapper.h"
#include "filters/filter.h"
#include "filters/filter_internal.h"
#include "filters/frame.h"
#include "osdep/threads.h"
#include "video/hwdec.h"
#include "video/img_format.h"
#include "video/mp_image.h"

#include "vd_vt_async.h"

#define VT_ASYNC_MAX_INFLIGHT 10
#define VT_ASYNC_REORDER_DEPTH 4
#define VT_ASYNC_MAX_PARAMETER_SETS 64

struct vt_parameter_sets {
    const uint8_t *pointers[VT_ASYNC_MAX_PARAMETER_SETS];
    size_t sizes[VT_ASYNC_MAX_PARAMETER_SETS];
    size_t count;
    int nal_length;
};

struct vt_job {
    struct vt_job *next;
    uint64_t generation;
    CVPixelBufferRef pixel;
    double pts;
    double dts;
    double duration;
    struct pl_hdr_metadata hdr;
};

struct priv {
    struct mp_decoder public;
    struct mp_codec_params *codec;
    struct mp_filter *filter;

    VTDecompressionSessionRef session;
    CMVideoFormatDescriptionRef format;
    CMVideoCodecType codec_type;
    OSType output_format;
    AVBufferRef *hw_frames_ctx;

    mp_mutex lock;
    struct vt_job *ready_head;
    int ready_count;
    int inflight;
    uint64_t generation;
    bool eof_received;
    bool destroying;
    bool hwdec_notified;
    bool color_notified;
};

static CMTime make_time(double value)
{
    return value == MP_NOPTS_VALUE ? kCMTimeInvalid
                                   : CMTimeMakeWithSeconds(value, 1000000000);
}

static void release_pixel(void *arg)
{
    CVPixelBufferRelease((CVPixelBufferRef)arg);
}

static void free_job(struct vt_job *job)
{
    if (!job)
        return;
    if (job->pixel)
        CVPixelBufferRelease(job->pixel);
    free(job);
}

static void clear_ready_locked(struct priv *p)
{
    while (p->ready_head) {
        struct vt_job *job = p->ready_head;
        p->ready_head = job->next;
        free_job(job);
    }
    p->ready_count = 0;
}

static void decoder_callback(void *decompression_output_refcon,
                             void *source_frame_refcon,
                             OSStatus status,
                             VTDecodeInfoFlags flags,
                             CVImageBufferRef image_buffer,
                             CMTime presentation_time_stamp,
                             CMTime presentation_duration)
{
    struct priv *p = decompression_output_refcon;
    struct vt_job *job = source_frame_refcon;

    if (image_buffer)
        job->pixel = CVPixelBufferRetain((CVPixelBufferRef)image_buffer);
    if (CMTIME_IS_NUMERIC(presentation_time_stamp))
        job->pts = CMTimeGetSeconds(presentation_time_stamp);
    if (CMTIME_IS_NUMERIC(presentation_duration))
        job->duration = CMTimeGetSeconds(presentation_duration);

    mp_mutex_lock(&p->lock);
    p->inflight--;
    bool keep = !p->destroying && job->generation == p->generation &&
                status == noErr && job->pixel &&
                !(flags & kVTDecodeInfo_FrameDropped);
    if (keep) {
        struct vt_job **cursor = &p->ready_head;
        while (*cursor &&
               (job->pts == MP_NOPTS_VALUE ||
                ((*cursor)->pts != MP_NOPTS_VALUE &&
                 (*cursor)->pts <= job->pts)))
            cursor = &(*cursor)->next;
        job->next = *cursor;
        *cursor = job;
        p->ready_count++;
    }
    mp_mutex_unlock(&p->lock);

    if (!keep)
        free_job(job);
    mp_filter_wakeup(p->filter);
}

static const uint8_t *codec_extradata(struct mp_codec_params *codec, int *size)
{
    const uint8_t *data = codec->extradata;
    *size = codec->extradata_size;
    if ((!data || *size <= 0) && codec->lav_codecpar) {
        data = codec->lav_codecpar->extradata;
        *size = codec->lav_codecpar->extradata_size;
    }
    return data;
}

static uint16_t read_be16(const uint8_t *data)
{
    return ((uint16_t)data[0] << 8) | data[1];
}

static bool add_parameter_set(struct vt_parameter_sets *sets,
                              const uint8_t *data, size_t size)
{
    if (!size || sets->count >= VT_ASYNC_MAX_PARAMETER_SETS)
        return false;
    sets->pointers[sets->count] = data;
    sets->sizes[sets->count] = size;
    sets->count++;
    return true;
}

static bool parse_hevc_parameter_sets(const uint8_t *data, size_t data_size,
                                      struct vt_parameter_sets *sets)
{
    if (!data || data_size < 23 || data[0] != 1)
        return false;

    sets->nal_length = 1 + (data[21] & 0x03);
    if (sets->nal_length == 3)
        return false;

    bool has_vps = false;
    bool has_sps = false;
    bool has_pps = false;
    size_t offset = 23;
    unsigned int array_count = data[22];
    for (unsigned int array = 0; array < array_count; array++) {
        if (offset > data_size || data_size - offset < 3)
            return false;
        unsigned int type = data[offset] & 0x3f;
        unsigned int nalu_count = read_be16(data + offset + 1);
        offset += 3;
        for (unsigned int nalu = 0; nalu < nalu_count; nalu++) {
            if (offset > data_size || data_size - offset < 2)
                return false;
            size_t size = read_be16(data + offset);
            offset += 2;
            if (!size || size > data_size - offset)
                return false;
            if (type == 32 || type == 33 || type == 34 ||
                type == 39 || type == 40)
            {
                if (!add_parameter_set(sets, data + offset, size))
                    return false;
                has_vps |= type == 32;
                has_sps |= type == 33;
                has_pps |= type == 34;
            }
            offset += size;
        }
    }
    return has_vps && has_sps && has_pps;
}

static bool parse_avc_parameter_sets(const uint8_t *data, size_t data_size,
                                     struct vt_parameter_sets *sets)
{
    if (!data || data_size < 7 || data[0] != 1)
        return false;

    sets->nal_length = 1 + (data[4] & 0x03);
    if (sets->nal_length == 3)
        return false;

    size_t offset = 6;
    unsigned int sps_count = data[5] & 0x1f;
    if (!sps_count)
        return false;
    for (unsigned int sps = 0; sps < sps_count; sps++) {
        if (offset > data_size || data_size - offset < 2)
            return false;
        size_t size = read_be16(data + offset);
        offset += 2;
        if (!size || size > data_size - offset ||
            !add_parameter_set(sets, data + offset, size))
            return false;
        offset += size;
    }

    if (offset >= data_size)
        return false;
    unsigned int pps_count = data[offset++];
    if (!pps_count)
        return false;
    for (unsigned int pps = 0; pps < pps_count; pps++) {
        if (offset > data_size || data_size - offset < 2)
            return false;
        size_t size = read_be16(data + offset);
        offset += 2;
        if (!size || size > data_size - offset ||
            !add_parameter_set(sets, data + offset, size))
            return false;
        offset += size;
    }
    return true;
}

static int hevc_config_bit_depth(struct mp_codec_params *codec)
{
    if (!codec->codec || strcmp(codec->codec, "hevc") != 0)
        return 0;

    int size = 0;
    const uint8_t *data = codec_extradata(codec, &size);
    if (!data || size < 19 || data[0] != 1)
        return 0;

    // ISO/IEC 14496-15 HEVCDecoderConfigurationRecord stores the luma and
    // chroma bit-depth-minus-eight values in bytes 17 and 18. FFmpeg's
    // AVCodecParameters may leave profile/format unknown until a decoder has
    // parsed the SPS, which is too late for selecting the VideoToolbox output.
    int luma_depth = 8 + (data[17] & 0x07);
    int chroma_depth = 8 + (data[18] & 0x07);
    return luma_depth > chroma_depth ? luma_depth : chroma_depth;
}

static bool is_ten_bit(struct mp_codec_params *codec)
{
    if (hevc_config_bit_depth(codec) > 8)
        return true;

    const AVCodecParameters *lav = codec->lav_codecpar;
    if (lav) {
        if (lav->bits_per_raw_sample > 8 ||
            lav->profile == AV_PROFILE_HEVC_MAIN_10)
            return true;
        const AVPixFmtDescriptor *descriptor = av_pix_fmt_desc_get(lav->format);
        if (descriptor) {
            for (int n = 0; n < descriptor->nb_components; n++) {
                if (descriptor->comp[n].depth > 8)
                    return true;
            }
        }
    }
    return (codec->codec_profile && strstr(codec->codec_profile, "10")) ||
           (codec->format_name && strstr(codec->format_name, "10"));
}

static OSType select_output_format(struct mp_codec_params *codec)
{
    bool full_range = codec->repr.levels == PL_COLOR_LEVELS_FULL;
    if (codec->lav_codecpar &&
        codec->lav_codecpar->color_range == AVCOL_RANGE_JPEG)
        full_range = true;

    if (is_ten_bit(codec)) {
        return full_range
             ? kCVPixelFormatType_420YpCbCr10BiPlanarFullRange
             : kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange;
    }
    return full_range
         ? kCVPixelFormatType_420YpCbCr8BiPlanarFullRange
         : kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange;
}

static const void *coded_side_data(const AVCodecParameters *codec,
                                   enum AVPacketSideDataType type,
                                   size_t minimum_size)
{
    if (!codec)
        return NULL;
    const AVPacketSideData *side = av_packet_side_data_get(
        codec->coded_side_data, codec->nb_coded_side_data, type);
    return side && side->size >= minimum_size ? side->data : NULL;
}

static void map_stream_properties(struct priv *p,
                                  struct mp_image_params *params)
{
    params->color = p->codec->color;
    params->repr = p->codec->repr;
    params->chroma_location = p->codec->chroma_location;

    const AVCodecParameters *lav = p->codec->lav_codecpar;
    if (!lav)
        return;
    if (lav->color_primaries != AVCOL_PRI_UNSPECIFIED)
        params->color.primaries = pl_primaries_from_av(lav->color_primaries);
    if (lav->color_trc != AVCOL_TRC_UNSPECIFIED)
        params->color.transfer = pl_transfer_from_av(lav->color_trc);
    if (lav->color_space != AVCOL_SPC_UNSPECIFIED)
        params->repr.sys = pl_system_from_av(lav->color_space);
    if (lav->color_range != AVCOL_RANGE_UNSPECIFIED)
        params->repr.levels = pl_levels_from_av(lav->color_range);
    if (lav->chroma_location != AVCHROMA_LOC_UNSPECIFIED)
        params->chroma_location = pl_chroma_from_av(lav->chroma_location);

#ifdef PL_HAVE_LAV_HDR
    pl_map_hdr_metadata(&params->color.hdr, &(struct pl_av_hdr_metadata) {
        .mdm = coded_side_data(lav, AV_PKT_DATA_MASTERING_DISPLAY_METADATA,
                               sizeof(AVMasteringDisplayMetadata)),
        .clm = coded_side_data(lav, AV_PKT_DATA_CONTENT_LIGHT_LEVEL,
                               sizeof(AVContentLightMetadata)),
        .dhp = coded_side_data(lav, AV_PKT_DATA_DYNAMIC_HDR10_PLUS,
                               sizeof(AVDynamicHDRPlus)),
    });
#endif
}

static void add_format_description_color_extensions(
    struct priv *p, CFMutableDictionaryRef extensions)
{
    const AVCodecParameters *lav = p->codec->lav_codecpar;
    enum AVColorPrimaries primaries = lav
        ? lav->color_primaries : AVCOL_PRI_UNSPECIFIED;
    enum AVColorTransferCharacteristic transfer = lav
        ? lav->color_trc : AVCOL_TRC_UNSPECIFIED;
    enum AVColorSpace matrix = lav
        ? lav->color_space : AVCOL_SPC_UNSPECIFIED;
    enum AVColorRange range = lav
        ? lav->color_range : AVCOL_RANGE_UNSPECIFIED;

    if (primaries == AVCOL_PRI_UNSPECIFIED)
        primaries = pl_primaries_to_av(p->codec->color.primaries);
    if (transfer == AVCOL_TRC_UNSPECIFIED)
        transfer = pl_transfer_to_av(p->codec->color.transfer);
    if (matrix == AVCOL_SPC_UNSPECIFIED)
        matrix = pl_system_to_av(p->codec->repr.sys);
    if (range == AVCOL_RANGE_UNSPECIFIED)
        range = pl_levels_to_av(p->codec->repr.levels);

    MP_VERBOSE(p->filter,
               "VideoToolbox source color metadata: primaries=%d transfer=%d "
               "matrix=%d range=%d.\n",
               primaries, transfer, matrix, range);

    // CMVideoFormatDescriptionCreate() does not reliably derive VUI color
    // properties from an hvcC/avcC atom alone. Supplying the demuxed values as
    // format-description extensions prevents VideoToolbox from labelling HDR
    // P010 output as the Rec.709 default and propagates them to CVPixelBuffers.
    CFStringRef value =
        CVColorPrimariesGetStringForIntegerCodePoint(primaries);
    if (value)
        CFDictionarySetValue(extensions,
                             kCMFormatDescriptionExtension_ColorPrimaries,
                             value);
    value = CVTransferFunctionGetStringForIntegerCodePoint(transfer);
    if (value)
        CFDictionarySetValue(extensions,
                             kCMFormatDescriptionExtension_TransferFunction,
                             value);
    value = CVYCbCrMatrixGetStringForIntegerCodePoint(matrix);
    if (value)
        CFDictionarySetValue(extensions,
                             kCMFormatDescriptionExtension_YCbCrMatrix,
                             value);
    if (range != AVCOL_RANGE_UNSPECIFIED) {
        CFDictionarySetValue(extensions,
                             kCMFormatDescriptionExtension_FullRangeVideo,
                             range == AVCOL_RANGE_JPEG ? kCFBooleanTrue
                                                       : kCFBooleanFalse);
    }
}

static CFStringRef image_buffer_string_attachment(CVPixelBufferRef pixel,
                                                  CFStringRef key)
{
    CFTypeRef value = CVBufferGetAttachment(pixel, key, NULL);
    return value && CFGetTypeID(value) == CFStringGetTypeID()
         ? (CFStringRef)value : NULL;
}

static CFStringRef format_description_string_extension(
    CMFormatDescriptionRef format, CFStringRef key)
{
    CFPropertyListRef value = CMFormatDescriptionGetExtension(format, key);
    return value && CFGetTypeID(value) == CFStringGetTypeID()
         ? (CFStringRef)value : NULL;
}

static void map_color_strings(CFStringRef primaries_value,
                              CFStringRef transfer_value,
                              CFStringRef matrix_value,
                              struct mp_image_params *params)
{
    if (primaries_value) {
        int code =
            CVColorPrimariesGetIntegerCodePointForString(primaries_value);
        if (code >= 0 && code < AVCOL_PRI_NB) {
            enum pl_color_primaries primaries =
                pl_primaries_from_av((enum AVColorPrimaries)code);
            if (primaries != PL_COLOR_PRIM_UNKNOWN &&
                params->color.primaries == PL_COLOR_PRIM_UNKNOWN)
                params->color.primaries = primaries;
        }
    }

    if (transfer_value) {
        int code =
            CVTransferFunctionGetIntegerCodePointForString(transfer_value);
        if (code >= 0 && code < AVCOL_TRC_NB) {
            enum pl_color_transfer transfer =
                pl_transfer_from_av((enum AVColorTransferCharacteristic)code);
            if (transfer != PL_COLOR_TRC_UNKNOWN &&
                params->color.transfer == PL_COLOR_TRC_UNKNOWN)
                params->color.transfer = transfer;
        }
    }

    if (matrix_value) {
        int code = CVYCbCrMatrixGetIntegerCodePointForString(matrix_value);
        if (code >= 0 && code < AVCOL_SPC_NB) {
            enum pl_color_system system =
                pl_system_from_av((enum AVColorSpace)code);
            if (system != PL_COLOR_SYSTEM_UNKNOWN &&
                params->repr.sys == PL_COLOR_SYSTEM_UNKNOWN)
                params->repr.sys = system;
        }
    }
}

static void map_format_description_color(CMFormatDescriptionRef format,
                                         struct mp_image_params *params)
{
    map_color_strings(
        format_description_string_extension(
            format, kCMFormatDescriptionExtension_ColorPrimaries),
        format_description_string_extension(
            format, kCMFormatDescriptionExtension_TransferFunction),
        format_description_string_extension(
            format, kCMFormatDescriptionExtension_YCbCrMatrix),
        params);

    CFPropertyListRef full_range = CMFormatDescriptionGetExtension(
        format, kCMFormatDescriptionExtension_FullRangeVideo);
    if (params->repr.levels == PL_COLOR_LEVELS_UNKNOWN && full_range &&
        CFGetTypeID(full_range) == CFBooleanGetTypeID())
    {
        params->repr.levels = CFBooleanGetValue((CFBooleanRef)full_range)
                            ? PL_COLOR_LEVELS_FULL : PL_COLOR_LEVELS_LIMITED;
    }
}

static void map_image_buffer_color(CVPixelBufferRef pixel,
                                   struct mp_image_params *params)
{
    map_color_strings(
        image_buffer_string_attachment(pixel,
                                       kCVImageBufferColorPrimariesKey),
        image_buffer_string_attachment(pixel,
                                       kCVImageBufferTransferFunctionKey),
        image_buffer_string_attachment(pixel,
                                       kCVImageBufferYCbCrMatrixKey),
        params);
}

static uint32_t read_be32(const uint8_t *data)
{
    return ((uint32_t)data[0] << 24) | ((uint32_t)data[1] << 16) |
           ((uint32_t)data[2] << 8) | data[3];
}

static const uint8_t *image_buffer_data_attachment(CVPixelBufferRef pixel,
                                                   CFStringRef key,
                                                   CFIndex expected_size)
{
    CFTypeRef value = CVBufferGetAttachment(pixel, key, NULL);
    if (!value || CFGetTypeID(value) != CFDataGetTypeID())
        return NULL;
    CFDataRef data = (CFDataRef)value;
    return CFDataGetLength(data) == expected_size ? CFDataGetBytePtr(data)
                                                   : NULL;
}

static void map_image_buffer_hdr(CVPixelBufferRef pixel,
                                 struct pl_hdr_metadata *hdr)
{
#ifdef PL_HAVE_LAV_HDR
    AVMasteringDisplayMetadata mastering = {0};
    AVContentLightMetadata content_light = {0};
    const AVMasteringDisplayMetadata *mastering_ptr = NULL;
    const AVContentLightMetadata *content_light_ptr = NULL;

    const uint8_t *data = image_buffer_data_attachment(
        pixel, kCVImageBufferMasteringDisplayColorVolumeKey, 24);
    if (data) {
        // HEVC carries mastering primaries as G, B, R. Convert them to the
        // R, G, B order used by AVMasteringDisplayMetadata/libplacebo.
        static const int mapping[3] = {2, 0, 1};
        for (int component = 0; component < 3; component++) {
            int source = mapping[component];
            mastering.display_primaries[component][0] =
                (AVRational){read_be16(data + source * 4), 50000};
            mastering.display_primaries[component][1] =
                (AVRational){read_be16(data + source * 4 + 2), 50000};
        }
        mastering.white_point[0] = (AVRational){read_be16(data + 12), 50000};
        mastering.white_point[1] = (AVRational){read_be16(data + 14), 50000};
        mastering.max_luminance = (AVRational){(int)read_be32(data + 16), 10000};
        mastering.min_luminance = (AVRational){(int)read_be32(data + 20), 10000};
        mastering.has_primaries = 1;
        mastering.has_luminance = 1;
        mastering_ptr = &mastering;
    }

    data = image_buffer_data_attachment(
        pixel, kCVImageBufferContentLightLevelInfoKey, 4);
    if (data) {
        content_light.MaxCLL = read_be16(data);
        content_light.MaxFALL = read_be16(data + 2);
        content_light_ptr = &content_light;
    }

    if (mastering_ptr || content_light_ptr) {
        struct pl_hdr_metadata attached = {0};
        pl_map_hdr_metadata(&attached, &(struct pl_av_hdr_metadata) {
            .mdm = mastering_ptr,
            .clm = content_light_ptr,
        });
        pl_hdr_metadata_merge(hdr, &attached);
    }
#endif
}

static void map_packet_hdr(struct demux_packet *packet,
                           struct pl_hdr_metadata *hdr)
{
#ifdef PL_HAVE_LAV_HDR
    if (!packet->avpacket)
        return;
    size_t mdm_size = 0;
    size_t clm_size = 0;
    size_t dhp_size = 0;
    const AVMasteringDisplayMetadata *mdm = (void *)av_packet_get_side_data(
        packet->avpacket, AV_PKT_DATA_MASTERING_DISPLAY_METADATA, &mdm_size);
    const AVContentLightMetadata *clm = (void *)av_packet_get_side_data(
        packet->avpacket, AV_PKT_DATA_CONTENT_LIGHT_LEVEL, &clm_size);
    const AVDynamicHDRPlus *dhp = (void *)av_packet_get_side_data(
        packet->avpacket, AV_PKT_DATA_DYNAMIC_HDR10_PLUS, &dhp_size);
    pl_map_hdr_metadata(hdr, &(struct pl_av_hdr_metadata) {
        .mdm = mdm_size >= sizeof(*mdm) ? mdm : NULL,
        .clm = clm_size >= sizeof(*clm) ? clm : NULL,
        .dhp = dhp_size >= sizeof(*dhp) ? dhp : NULL,
    });
#endif
}

static CFDictionaryRef create_output_attributes(struct priv *p)
{
    int32_t width = p->codec->disp_w;
    int32_t height = p->codec->disp_h;
    int32_t format = p->output_format;

    CFNumberRef width_number = CFNumberCreate(NULL, kCFNumberSInt32Type, &width);
    CFNumberRef height_number = CFNumberCreate(NULL, kCFNumberSInt32Type, &height);
    CFNumberRef format_number = CFNumberCreate(NULL, kCFNumberSInt32Type, &format);
    CFMutableDictionaryRef iosurface = CFDictionaryCreateMutable(
        NULL, 0, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    CFMutableDictionaryRef attributes = CFDictionaryCreateMutable(
        NULL, 0, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);

    if (width_number)
        CFDictionarySetValue(attributes, kCVPixelBufferWidthKey, width_number);
    if (height_number)
        CFDictionarySetValue(attributes, kCVPixelBufferHeightKey, height_number);
    if (format_number)
        CFDictionarySetValue(attributes, kCVPixelBufferPixelFormatTypeKey,
                             format_number);
    if (iosurface)
        CFDictionarySetValue(attributes, kCVPixelBufferIOSurfacePropertiesKey,
                             iosurface);
    CFDictionarySetValue(attributes, kCVPixelBufferMetalCompatibilityKey,
                         kCFBooleanTrue);

    if (width_number)
        CFRelease(width_number);
    if (height_number)
        CFRelease(height_number);
    if (format_number)
        CFRelease(format_number);
    if (iosurface)
        CFRelease(iosurface);
    return attributes;
}

static void log_format_description_color(struct mp_filter *f,
                                         CMFormatDescriptionRef format,
                                         const char *source)
{
    CFStringRef primaries = format_description_string_extension(
        format, kCMFormatDescriptionExtension_ColorPrimaries);
    CFStringRef transfer = format_description_string_extension(
        format, kCMFormatDescriptionExtension_TransferFunction);
    CFStringRef matrix = format_description_string_extension(
        format, kCMFormatDescriptionExtension_YCbCrMatrix);
    MP_VERBOSE(f,
               "VideoToolbox %s format color metadata: primaries=%d "
               "transfer=%d matrix=%d.\n",
               source,
               CVColorPrimariesGetIntegerCodePointForString(primaries),
               CVTransferFunctionGetIntegerCodePointForString(transfer),
               CVYCbCrMatrixGetIntegerCodePointForString(matrix));
}

static bool create_parameter_set_format_description(
    struct priv *p, const uint8_t *extradata, size_t extradata_size,
    CFDictionaryRef extensions)
{
    struct vt_parameter_sets sets = {0};
    bool parsed = p->codec_type == kCMVideoCodecType_HEVC
                ? parse_hevc_parameter_sets(extradata, extradata_size, &sets)
                : parse_avc_parameter_sets(extradata, extradata_size, &sets);
    if (!parsed)
        return false;

    OSStatus status;
    if (p->codec_type == kCMVideoCodecType_HEVC) {
        status = CMVideoFormatDescriptionCreateFromHEVCParameterSets(
            NULL, sets.count, sets.pointers, sets.sizes, sets.nal_length,
            extensions, &p->format);
    } else {
        status = CMVideoFormatDescriptionCreateFromH264ParameterSets(
            NULL, sets.count, sets.pointers, sets.sizes, sets.nal_length,
            &p->format);
    }
    if (status == noErr && p->format)
        return true;
    if (p->format) {
        CFRelease(p->format);
        p->format = NULL;
    }
    return false;
}

static bool create_format_description(struct mp_filter *f, struct priv *p)
{
    int extradata_size = 0;
    const uint8_t *extradata = codec_extradata(p->codec, &extradata_size);

    if (!extradata || extradata_size <= 0 ||
        p->codec->disp_w <= 0 || p->codec->disp_h <= 0)
    {
        MP_ERR(f, "VideoToolbox async requires codec configuration and dimensions "
               "(extradata=%d, display=%dx%d, lav=%p).\n",
               extradata_size, p->codec->disp_w,
               p->codec->disp_h, p->codec->lav_codecpar);
        return false;
    }

    CFMutableDictionaryRef extensions = CFDictionaryCreateMutable(
        NULL, 4, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    CFDataRef atom = NULL;
    CFMutableDictionaryRef atoms = NULL;
    if (!extensions)
        goto fail;

    add_format_description_color_extensions(p, extensions);
    if (create_parameter_set_format_description(
            p, extradata, extradata_size, extensions))
    {
        log_format_description_color(f, p->format, "parameter-set");
        CFRelease(extensions);
        return true;
    }

    // Keep the generic atom path as a compatibility fallback for unusual but
    // valid codec configuration records that CoreMedia cannot reconstruct.
    CFStringRef atom_name = p->codec_type == kCMVideoCodecType_HEVC
                          ? CFSTR("hvcC") : CFSTR("avcC");
    atom = CFDataCreate(NULL, extradata, extradata_size);
    atoms = CFDictionaryCreateMutable(
        NULL, 1, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    if (!atom || !atoms)
        goto fail;
    CFDictionarySetValue(atoms, atom_name, atom);
    CFDictionarySetValue(extensions,
                         kCMFormatDescriptionExtension_SampleDescriptionExtensionAtoms,
                         atoms);

    OSStatus status = CMVideoFormatDescriptionCreate(
        NULL, p->codec_type, p->codec->disp_w, p->codec->disp_h, extensions,
        &p->format);
    CFRelease(extensions);
    CFRelease(atoms);
    CFRelease(atom);
    if (status != noErr || !p->format) {
        MP_ERR(f, "Could not create VideoToolbox format description: %d\n",
               (int)status);
        return false;
    }
    log_format_description_color(f, p->format, "generic-atom");
    return true;

fail:
    if (extensions)
        CFRelease(extensions);
    if (atoms)
        CFRelease(atoms);
    if (atom)
        CFRelease(atom);
    MP_ERR(f, "Could not allocate VideoToolbox format description data.\n");
    return false;
}

static bool create_session(struct mp_filter *f, struct priv *p)
{
    if (!p->format && !create_format_description(f, p))
        return false;

    CFMutableDictionaryRef decoder_spec = CFDictionaryCreateMutable(
        NULL, 1, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    CFDictionaryRef output_attributes = create_output_attributes(p);
    if (!decoder_spec || !output_attributes) {
        if (decoder_spec)
            CFRelease(decoder_spec);
        if (output_attributes)
            CFRelease(output_attributes);
        return false;
    }
    CFDictionarySetValue(
        decoder_spec,
        kVTVideoDecoderSpecification_RequireHardwareAcceleratedVideoDecoder,
        kCFBooleanTrue);

    VTDecompressionOutputCallbackRecord callback = {
        .decompressionOutputCallback = decoder_callback,
        .decompressionOutputRefCon = p,
    };
    OSStatus status = VTDecompressionSessionCreate(
        NULL, p->format, decoder_spec, output_attributes, &callback, &p->session);
    CFRelease(output_attributes);
    CFRelease(decoder_spec);
    if (status != noErr || !p->session) {
        MP_ERR(f, "Could not create asynchronous VideoToolbox session: %d\n",
               (int)status);
        return false;
    }

    VTSessionSetProperty(p->session, kVTDecompressionPropertyKey_RealTime,
                         kCFBooleanTrue);
    CFTypeRef hardware = NULL;
    status = VTSessionCopyProperty(
        p->session,
        kVTDecompressionPropertyKey_UsingHardwareAcceleratedVideoDecoder,
        NULL, &hardware);
    bool hardware_active = status == noErr && hardware &&
                           CFGetTypeID(hardware) == CFBooleanGetTypeID() &&
                           CFBooleanGetValue(hardware);
    if (hardware)
        CFRelease(hardware);
    if (!hardware_active) {
        MP_ERR(f, "VideoToolbox did not provide a hardware decoder.\n");
        VTDecompressionSessionInvalidate(p->session);
        CFRelease(p->session);
        p->session = NULL;
        return false;
    }
    return true;
}

static bool create_hw_frames_context(struct mp_filter *f, struct priv *p)
{
    struct mp_hwdec_ctx *device = mp_filter_load_hwdec_device(
        f, IMGFMT_VIDEOTOOLBOX, AV_HWDEVICE_TYPE_VIDEOTOOLBOX);
    if (!device || !device->av_device_ref) {
        MP_ERR(f, "The selected video output cannot import VideoToolbox frames.\n");
        return false;
    }

    p->hw_frames_ctx = av_hwframe_ctx_alloc(device->av_device_ref);
    if (!p->hw_frames_ctx)
        return false;

    AVHWFramesContext *frames = (void *)p->hw_frames_ctx->data;
    frames->format = AV_PIX_FMT_VIDEOTOOLBOX;
    frames->sw_format = (p->output_format ==
                         kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange ||
                         p->output_format ==
                         kCVPixelFormatType_420YpCbCr10BiPlanarFullRange)
                      ? AV_PIX_FMT_P010 : AV_PIX_FMT_NV12;
    frames->width = p->codec->disp_w;
    frames->height = p->codec->disp_h;
    if (av_hwframe_ctx_init(p->hw_frames_ctx) < 0) {
        MP_ERR(f, "Could not initialize the VideoToolbox frame context.\n");
        av_buffer_unref(&p->hw_frames_ctx);
        return false;
    }
    return true;
}

static void invalidate_session(struct priv *p)
{
    if (!p->session)
        return;
    VTDecompressionSessionWaitForAsynchronousFrames(p->session);
    VTDecompressionSessionInvalidate(p->session);
    CFRelease(p->session);
    p->session = NULL;
}

static CMSampleBufferRef create_sample(struct priv *p,
                                       struct demux_packet *packet)
{
    CMBlockBufferRef block = NULL;
    CMSampleBufferRef sample = NULL;
    OSStatus status = CMBlockBufferCreateWithMemoryBlock(
        NULL, NULL, packet->len, kCFAllocatorDefault, NULL, 0, packet->len, 0,
        &block);
    if (status != noErr || !block)
        return NULL;
    status = CMBlockBufferReplaceDataBytes(packet->buffer, block, 0,
                                           packet->len);
    if (status != noErr)
        goto done;

    CMSampleTimingInfo timing = {
        .duration = make_time(packet->duration),
        .presentationTimeStamp = make_time(packet->pts),
        .decodeTimeStamp = make_time(packet->dts),
    };
    size_t size = packet->len;
    status = CMSampleBufferCreateReady(NULL, block, p->format, 1, 1, &timing,
                                       1, &size, &sample);
    if (status != noErr)
        sample = NULL;
done:
    CFRelease(block);
    return sample;
}

static bool submit_packet(struct mp_filter *f, struct priv *p,
                          struct demux_packet *packet)
{
    CMSampleBufferRef sample = create_sample(p, packet);
    if (!sample) {
        MP_ERR(f, "Could not create compressed VideoToolbox sample.\n");
        return false;
    }

    struct vt_job *job = calloc(1, sizeof(*job));
    if (!job) {
        CFRelease(sample);
        return false;
    }
    job->generation = p->generation;
    job->pts = packet->pts;
    job->dts = packet->dts;
    job->duration = packet->duration;
    map_packet_hdr(packet, &job->hdr);

    mp_mutex_lock(&p->lock);
    p->inflight++;
    mp_mutex_unlock(&p->lock);

    VTDecodeInfoFlags info = 0;
    VTDecodeFrameFlags flags = kVTDecodeFrame_EnableAsynchronousDecompression |
                               kVTDecodeFrame_EnableTemporalProcessing;
    OSStatus status = VTDecompressionSessionDecodeFrame(
        p->session, sample, flags, job, &info);
    CFRelease(sample);
    if (status == noErr)
        return true;

    mp_mutex_lock(&p->lock);
    p->inflight--;
    mp_mutex_unlock(&p->lock);
    free_job(job);
    MP_ERR(f, "Asynchronous VideoToolbox submission failed: %d\n", (int)status);
    return false;
}

static struct mp_image *image_from_job(struct priv *p, struct vt_job *job)
{
    OSType format = CVPixelBufferGetPixelFormatType(job->pixel);
    enum mp_imgfmt sw_format;
    switch (format) {
    case kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange:
    case kCVPixelFormatType_420YpCbCr8BiPlanarFullRange:
        sw_format = IMGFMT_NV12;
        break;
    case kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange:
    case kCVPixelFormatType_420YpCbCr10BiPlanarFullRange:
        sw_format = IMGFMT_P010;
        break;
    default:
        MP_ERR(p->filter, "Unsupported async VideoToolbox pixel format: 0x%08x\n",
               (unsigned int)format);
        return NULL;
    }

    struct mp_image base = {0};
    mp_image_sethwfmt(&base, IMGFMT_VIDEOTOOLBOX, sw_format);
    mp_image_set_size(&base, CVPixelBufferGetWidth(job->pixel),
                      CVPixelBufferGetHeight(job->pixel));
    base.params.p_w = p->codec->par_w > 0 ? p->codec->par_w : 1;
    base.params.p_h = p->codec->par_h > 0 ? p->codec->par_h : 1;
    map_stream_properties(p, &base.params);
    map_format_description_color(p->format, &base.params);
    if (!p->color_notified) {
        CFStringRef primaries = image_buffer_string_attachment(
            job->pixel, kCVImageBufferColorPrimariesKey);
        CFStringRef transfer = image_buffer_string_attachment(
            job->pixel, kCVImageBufferTransferFunctionKey);
        CFStringRef matrix = image_buffer_string_attachment(
            job->pixel, kCVImageBufferYCbCrMatrixKey);
        MP_VERBOSE(p->filter,
                   "VideoToolbox output color attachments: format=0x%08x "
                   "primaries=%d transfer=%d matrix=%d.\n",
                   (unsigned int)format,
                   primaries
                       ? CVColorPrimariesGetIntegerCodePointForString(primaries)
                       : -1,
                   transfer
                       ? CVTransferFunctionGetIntegerCodePointForString(transfer)
                       : -1,
                   matrix
                       ? CVYCbCrMatrixGetIntegerCodePointForString(matrix)
                       : -1);
        p->color_notified = true;
    }
    map_image_buffer_color(job->pixel, &base.params);
    if (base.params.repr.levels == PL_COLOR_LEVELS_UNKNOWN) {
        base.params.repr.levels =
            format == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange ||
            format == kCVPixelFormatType_420YpCbCr10BiPlanarFullRange
          ? PL_COLOR_LEVELS_FULL : PL_COLOR_LEVELS_LIMITED;
    }
    pl_hdr_metadata_merge(&base.params.color.hdr, &job->hdr);
    map_image_buffer_hdr(job->pixel, &base.params.color.hdr);
    base.params.rotate = p->codec->rotate;
    base.params.stereo3d = p->codec->stereo_mode;
    base.params.crop = p->codec->crop;
    if (base.params.crop.x1 <= base.params.crop.x0 ||
        base.params.crop.y1 <= base.params.crop.y0)
    {
        base.params.crop = (struct mp_rect){0, 0, base.w, base.h};
    }
    mp_image_params_guess_csp(&base.params);
    base.pts = job->pts;
    base.dts = job->dts;
    base.pkt_duration = job->duration;
    base.nominal_fps = p->codec->fps;
    base.planes[0] = (uint8_t *)"videotoolbox-async";
    base.planes[3] = (uint8_t *)job->pixel;

    CVPixelBufferRef pixel = job->pixel;
    job->pixel = NULL;
    struct mp_image *image = mp_image_new_custom_ref(&base, pixel, release_pixel);
    if (!image) {
        CVPixelBufferRelease(pixel);
    } else {
        image->hwctx = av_buffer_ref(p->hw_frames_ctx);
        if (!image->hwctx)
            mp_image_unrefp(&image);
    }
    return image;
}

static struct vt_job *pop_ready(struct priv *p)
{
    mp_mutex_lock(&p->lock);
    struct vt_job *job = p->ready_head;
    if (job) {
        p->ready_head = job->next;
        p->ready_count--;
        job->next = NULL;
    }
    mp_mutex_unlock(&p->lock);
    return job;
}

static void queue_counts(struct priv *p, int *inflight, int *ready)
{
    mp_mutex_lock(&p->lock);
    *inflight = p->inflight;
    *ready = p->ready_count;
    mp_mutex_unlock(&p->lock);
}

static void process(struct mp_filter *f)
{
    struct priv *p = f->priv;
    if (!mp_pin_in_needs_data(f->ppins[1]))
        return;

    for (;;) {
        int inflight, ready;
        queue_counts(p, &inflight, &ready);
        if (p->eof_received) {
            if (ready == 0 && inflight == 0) {
                mp_pin_in_write(f->ppins[1], MP_EOF_FRAME);
                return;
            }
        } else if (inflight + ready < VT_ASYNC_MAX_INFLIGHT) {
            struct mp_frame input = mp_pin_out_read(f->ppins[0]);
            if (input.type == MP_FRAME_PACKET) {
                struct demux_packet *packet = input.data;
                submit_packet(f, p, packet);
                demux_packet_pool_push(f->packet_pool, packet);
                mp_filter_internal_mark_progress(f);
                continue;
            }
            if (input.type == MP_FRAME_EOF) {
                p->eof_received = true;
                VTDecompressionSessionFinishDelayedFrames(p->session);
                VTDecompressionSessionWaitForAsynchronousFrames(p->session);
                mp_filter_internal_mark_progress(f);
                continue;
            }
            if (input.type) {
                MP_ERR(f, "Unexpected input for VideoToolbox async decoder.\n");
                mp_frame_unref(&input);
                mp_filter_internal_mark_failed(f);
                return;
            }
        }

        queue_counts(p, &inflight, &ready);
        if (ready < VT_ASYNC_REORDER_DEPTH && inflight > 0 &&
            !p->eof_received)
            return;

        struct vt_job *job = pop_ready(p);
        if (!job)
            return;
        struct mp_image *image = image_from_job(p, job);
        free_job(job);
        if (!image) {
            mp_filter_internal_mark_progress(f);
            continue;
        }
        if (!p->hwdec_notified) {
            MP_INFO(f, "Using pipelined hardware decoding (videotoolbox-async).\n");
            p->hwdec_notified = true;
        }
        mp_pin_in_write(f->ppins[1], MAKE_FRAME(MP_FRAME_VIDEO, image));
        return;
    }
}

static bool restart_session(struct mp_filter *f, struct priv *p)
{
    p->generation++;
    invalidate_session(p);
    mp_mutex_lock(&p->lock);
    clear_ready_locked(p);
    p->inflight = 0;
    mp_mutex_unlock(&p->lock);
    p->eof_received = false;
    p->hwdec_notified = false;
    p->color_notified = false;
    return create_session(f, p);
}

static void reset(struct mp_filter *f)
{
    struct priv *p = f->priv;
    if (!restart_session(f, p))
        mp_filter_internal_mark_failed(f);
}

static int control(struct mp_filter *f, enum dec_ctrl cmd, void *arg)
{
    struct priv *p = f->priv;
    switch (cmd) {
    case VDCTRL_GET_HWDEC:
        *(char **)arg = "videotoolbox-async";
        return CONTROL_TRUE;
    case VDCTRL_GET_BFRAMES:
        *(int *)arg = VT_ASYNC_REORDER_DEPTH;
        return CONTROL_TRUE;
    case VDCTRL_SET_FRAMEDROP:
        return CONTROL_TRUE;
    case VDCTRL_REINIT:
        return restart_session(f, p) ? CONTROL_TRUE : CONTROL_ERROR;
    case VDCTRL_FORCE_HWDEC_FALLBACK:
        return CONTROL_FALSE;
    case VDCTRL_CHECK_FORCED_EOF:
        *(bool *)arg = false;
        return CONTROL_TRUE;
    }
    return CONTROL_UNKNOWN;
}

static void destroy(struct mp_filter *f)
{
    struct priv *p = f->priv;
    mp_mutex_lock(&p->lock);
    p->destroying = true;
    p->generation++;
    mp_mutex_unlock(&p->lock);
    invalidate_session(p);
    mp_mutex_lock(&p->lock);
    clear_ready_locked(p);
    mp_mutex_unlock(&p->lock);
    if (p->format)
        CFRelease(p->format);
    av_buffer_unref(&p->hw_frames_ctx);
    mp_mutex_destroy(&p->lock);
}

static const struct mp_filter_info vt_async_filter = {
    .name = "vd_videotoolbox_async",
    .priv_size = sizeof(struct priv),
    .process = process,
    .reset = reset,
    .destroy = destroy,
};

struct mp_decoder *vd_vt_async_create(struct mp_filter *parent,
                                      struct mp_codec_params *codec,
                                      const char *decoder)
{
    bool hevc = strcmp(decoder, VT_ASYNC_HEVC_DECODER) == 0 &&
                codec->codec && strcmp(codec->codec, "hevc") == 0;
    bool h264 = strcmp(decoder, VT_ASYNC_H264_DECODER) == 0 &&
                codec->codec && strcmp(codec->codec, "h264") == 0;
    if (!hevc && !h264)
        return NULL;

    struct mp_filter *f = mp_filter_create(parent, &vt_async_filter);
    if (!f)
        return NULL;
    mp_filter_add_pin(f, MP_PIN_IN, "in");
    mp_filter_add_pin(f, MP_PIN_OUT, "out");
    f->log = mp_log_new(f, parent->log, NULL);

    struct priv *p = f->priv;
    p->public.f = f;
    p->public.control = control;
    p->codec = codec;
    p->filter = f;
    p->codec_type = hevc ? kCMVideoCodecType_HEVC : kCMVideoCodecType_H264;
    p->output_format = select_output_format(codec);
    mp_mutex_init(&p->lock);

    if (!create_hw_frames_context(f, p) || !create_session(f, p)) {
        talloc_free(f);
        return NULL;
    }
    codec->codec_desc = hevc ? "HEVC (asynchronous VideoToolbox)"
                             : "H.264 (asynchronous VideoToolbox)";
    return &p->public;
}
