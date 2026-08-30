// SPDX-License-Identifier: LGPL-2.1-or-later

#pragma once

struct mp_codec_params;
struct mp_decoder;
struct mp_filter;

#define VT_ASYNC_HEVC_DECODER "hevc-videotoolbox-async"
#define VT_ASYNC_H264_DECODER "h264-videotoolbox-async"

struct mp_decoder *vd_vt_async_create(struct mp_filter *parent,
                                      struct mp_codec_params *codec,
                                      const char *decoder);
