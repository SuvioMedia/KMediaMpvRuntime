#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Fail closed unless bundled libmpv renders external ASS text with our font."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import multiprocessing
import os
import struct
import tempfile
import time
from multiprocessing.connection import Connection
from pathlib import Path


WIDTH = 320
HEIGHT = 180
BRIGHT_CHANNEL_THRESHOLD = 24
MAXIMUM_BASELINE_BRIGHT_PIXELS = 0
MINIMUM_ASS_BRIGHT_PIXELS = 4000
MINIMUM_BRIGHT_PIXEL_DELTA = 4000
FILE_LOAD_TIMEOUT_SECONDS = 4.0
RENDER_TIMEOUT_SECONDS = 4.0
CASE_TIMEOUT_SECONDS = 12.0
FONT_FIXTURE_NAME = "kmediampv-generated-minimal-ttf-test-fixture"
FONT_FIXTURE_LICENSE = "LGPL-2.1-or-later"
FONT_FIXTURE_SHA256 = "0c72b55755fd749e9bc5390df43b6ee44ca6315fbbcce276637de99134e83b01"
FONT_FAMILY = "KMediaMpvSmoke"
SUBTITLE_TEXT = "AAAA"

MPV_RENDER_PARAM_API_TYPE = 1
MPV_RENDER_PARAM_SW_SIZE = 17
MPV_RENDER_PARAM_SW_FORMAT = 18
MPV_RENDER_PARAM_SW_STRIDE = 19
MPV_RENDER_PARAM_SW_POINTER = 20
MPV_RENDER_UPDATE_FRAME = 1
MPV_EVENT_END_FILE = 7
MPV_EVENT_FILE_LOADED = 8


class SmokeFailure(RuntimeError):
    """A deliberately path-free smoke-test failure."""


class RenderParam(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("data", ctypes.c_void_p)]


class Event(ctypes.Structure):
    _fields_ = [
        ("event_id", ctypes.c_int),
        ("error", ctypes.c_int),
        ("reply_userdata", ctypes.c_uint64),
        ("data", ctypes.c_void_p),
    ]


def fail(code: str) -> None:
    raise SmokeFailure(code)


def configure_api(library: ctypes.CDLL) -> None:
    library.mpv_create.argtypes = []
    library.mpv_create.restype = ctypes.c_void_p
    library.mpv_set_option_string.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
    ]
    library.mpv_set_option_string.restype = ctypes.c_int
    library.mpv_initialize.argtypes = [ctypes.c_void_p]
    library.mpv_initialize.restype = ctypes.c_int
    library.mpv_command.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_char_p),
    ]
    library.mpv_command.restype = ctypes.c_int
    library.mpv_wait_event.argtypes = [ctypes.c_void_p, ctypes.c_double]
    library.mpv_wait_event.restype = ctypes.POINTER(Event)
    library.mpv_render_context_create.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(RenderParam),
    ]
    library.mpv_render_context_create.restype = ctypes.c_int
    library.mpv_render_context_update.argtypes = [ctypes.c_void_p]
    library.mpv_render_context_update.restype = ctypes.c_uint64
    library.mpv_render_context_render.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(RenderParam),
    ]
    library.mpv_render_context_render.restype = ctypes.c_int
    library.mpv_render_context_free.argtypes = [ctypes.c_void_p]
    library.mpv_render_context_free.restype = None
    library.mpv_terminate_destroy.argtypes = [ctypes.c_void_p]
    library.mpv_terminate_destroy.restype = None


def set_option(library: ctypes.CDLL, handle: int, name: str, value: str) -> None:
    status = int(
        library.mpv_set_option_string(
            handle,
            name.encode("utf-8"),
            value.encode("utf-8"),
        )
    )
    if status < 0:
        fail("mpv-option-rejected")


def command(library: ctypes.CDLL, handle: int, *arguments: str) -> None:
    encoded = [argument.encode("utf-8") for argument in arguments]
    values = (ctypes.c_char_p * (len(encoded) + 1))(*encoded, None)
    if int(library.mpv_command(handle, values)) < 0:
        fail("mpv-command-failed")


def wait_for_file(library: ctypes.CDLL, handle: int) -> None:
    deadline = time.monotonic() + FILE_LOAD_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        event = library.mpv_wait_event(handle, 0.05).contents
        if event.event_id == MPV_EVENT_FILE_LOADED:
            return
        if event.event_id == MPV_EVENT_END_FILE:
            fail("p6-load-ended-before-ready")
        if event.error < 0:
            fail("mpv-event-error")
    fail("p6-load-timeout")


def generated_p6() -> bytes:
    return f"P6\n{WIDTH} {HEIGHT}\n255\n".encode("ascii") + bytes(
        WIDTH * HEIGHT * 3
    )


def padded_sfnt_table(data: bytes) -> bytes:
    return data + bytes((-len(data)) % 4)


def sfnt_checksum(data: bytes) -> int:
    padded = padded_sfnt_table(data)
    words = struct.unpack(f">{len(padded) // 4}I", padded)
    return sum(words) & 0xFFFFFFFF


def generated_font_name_table() -> bytes:
    # These strings and the simple rectangle glyph are authored by KMediaMpv;
    # no glyph, outline, or metadata is copied from a third-party font.
    names = {
        0: "Copyright 2026 KMediaMpv contributors",
        1: FONT_FAMILY,
        2: "Regular",
        4: f"{FONT_FAMILY} Regular",
        5: "Version 1.000",
        6: f"{FONT_FAMILY}-Regular",
        13: f"Licensed under {FONT_FIXTURE_LICENSE}",
    }
    encoded = {
        name_id: value.encode("utf-16-be") for name_id, value in names.items()
    }
    records = bytearray()
    strings = bytearray()
    for name_id in sorted(encoded):
        value = encoded[name_id]
        records += struct.pack(
            ">HHHHHH",
            3,
            1,
            0x0409,
            name_id,
            len(value),
            len(strings),
        )
        strings += value
    return (
        struct.pack(">HHH", 0, len(encoded), 6 + 12 * len(encoded))
        + records
        + strings
    )


def generated_font_cmap_table() -> bytes:
    # U+0041 maps to our sole real glyph. U+FFFF is the required sentinel.
    segment_count = 2
    subtable = struct.pack(
        ">HHHHHHH",
        4,
        16 + 8 * segment_count,
        0,
        segment_count * 2,
        4,
        1,
        0,
    )
    subtable += struct.pack(">2H", 0x0041, 0xFFFF)
    subtable += struct.pack(">H", 0)
    subtable += struct.pack(">2H", 0x0041, 0xFFFF)
    subtable += struct.pack(">2h", -64, 1)
    subtable += struct.pack(">2H", 0, 0)
    return struct.pack(">HHHHI", 0, 1, 3, 1, 12) + subtable


def generated_font_fixture() -> bytes:
    """Build the project-owned minimal TrueType fixture using only stdlib."""

    empty_glyph = struct.pack(">hhhhhH", 0, 0, 0, 0, 0, 0)
    rectangle_glyph = struct.pack(">hhhhhHH", 1, 100, 0, 900, 800, 3, 0)
    rectangle_glyph += bytes((1, 1, 1, 1))
    rectangle_glyph += struct.pack(">4h", 100, 800, 0, -800)
    rectangle_glyph += struct.pack(">4h", 0, 0, 800, 0)
    glyphs = empty_glyph + rectangle_glyph

    tables = {
        b"OS/2": struct.pack(
            ">HhHHH11h10s4I4sHHH3hHH",
            0,
            500,
            400,
            5,
            0,
            650,
            600,
            0,
            75,
            650,
            600,
            0,
            350,
            50,
            250,
            0,
            bytes(10),
            1,
            0,
            0,
            0,
            b"KMMP",
            0x40,
            0x0041,
            0x0041,
            800,
            -200,
            0,
            800,
            200,
        ),
        b"cmap": generated_font_cmap_table(),
        b"glyf": glyphs,
        b"head": struct.pack(
            ">IIIIHHQQhhhhHHhhh",
            0x00010000,
            0x00010000,
            0,
            0x5F0F3CF5,
            3,
            1000,
            0,
            0,
            0,
            0,
            1000,
            800,
            0,
            8,
            2,
            0,
            0,
        ),
        b"hhea": struct.pack(
            ">I3hH11hH",
            0x00010000,
            800,
            -200,
            0,
            1000,
            0,
            0,
            1000,
            1,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            2,
        ),
        b"hmtx": struct.pack(">HhHh", 1000, 0, 1000, 0),
        b"loca": struct.pack(
            ">HHH",
            0,
            len(empty_glyph) // 2,
            len(glyphs) // 2,
        ),
        b"maxp": struct.pack(
            ">IH13H",
            0x00010000,
            2,
            4,
            1,
            0,
            0,
            1,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        ),
        b"name": generated_font_name_table(),
        b"post": struct.pack(
            ">IIhhIIIII",
            0x00030000,
            0,
            -100,
            50,
            0,
            0,
            0,
            0,
            0,
        ),
    }

    tags = sorted(tables)
    table_count = len(tags)
    search_power = 8
    header = struct.pack(
        ">IHHHH",
        0x00010000,
        table_count,
        search_power * 16,
        3,
        table_count * 16 - search_power * 16,
    )
    directory = bytearray()
    payload = bytearray()
    offset = 12 + 16 * table_count
    offsets: dict[bytes, int] = {}
    for tag in tags:
        data = tables[tag]
        offsets[tag] = offset
        directory += struct.pack(">4sIII", tag, sfnt_checksum(data), offset, len(data))
        block = padded_sfnt_table(data)
        payload += block
        offset += len(block)

    font = bytearray(header + directory + payload)
    adjustment = (0xB1B0AFBA - sfnt_checksum(font)) & 0xFFFFFFFF
    struct.pack_into(">I", font, offsets[b"head"] + 8, adjustment)
    if sfnt_checksum(font) != 0xB1B0AFBA:
        fail("generated-font-sfnt-checksum-invalid")
    return bytes(font)


def generated_ass() -> str:
    return rf"""[Script Info]
ScriptType: v4.00+
PlayResX: {WIDTH}
PlayResY: {HEIGHT}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{FONT_FAMILY},48,&H0000FFFF,&H0000FFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:10.00,Default,,0,0,0,,{{\fn{FONT_FAMILY}\fs48\pos(40,50)\bord0\shad0\1c&H00FFFF&}}{SUBTITLE_TEXT}
"""


def render_case(
    library_path: str,
    with_subtitle: bool,
) -> dict[str, int]:
    with tempfile.TemporaryDirectory(prefix="kmediampv-ass-smoke-") as temporary:
        root = Path(temporary)
        video = root / "generated.ppm"
        subtitle = root / "generated.ass"
        fonts = root / "fonts"
        font = fonts / "generated-fixture.ttf"
        fonts.mkdir()
        video.write_bytes(generated_p6())
        subtitle.write_text(generated_ass(), encoding="utf-8")
        font_bytes = generated_font_fixture()
        if hashlib.sha256(font_bytes).hexdigest() != FONT_FIXTURE_SHA256:
            fail("generated-font-hash-mismatch")
        font.write_bytes(font_bytes)
        if hashlib.sha256(font.read_bytes()).hexdigest() != FONT_FIXTURE_SHA256:
            fail("generated-font-copy-hash-mismatch")

        try:
            if os.name == "nt":
                if not hasattr(os, "add_dll_directory"):
                    fail("secure-dll-directory-unavailable")
                dll_directory = os.add_dll_directory(str(Path(library_path).parent))
                library = ctypes.WinDLL(library_path)
            else:
                dll_directory = None
                library = ctypes.CDLL(
                    library_path,
                    mode=os.RTLD_NOW | os.RTLD_LOCAL,
                )
        except OSError:
            fail("bundled-libmpv-load-failed")
        configure_api(library)

        handle = library.mpv_create()
        if not handle:
            fail("mpv-create-failed")
        render_context = ctypes.c_void_p()
        try:
            options = (
                ("config", "no"),
                ("terminal", "no"),
                ("audio", "no"),
                ("pause", "yes"),
                ("vo", "libmpv"),
                ("hwdec", "no"),
                ("vd", "ppm"),
                ("image-display-duration", "10"),
                ("sub-auto", "no"),
                ("embeddedfonts", "no"),
                ("osd-level", "0"),
                ("sub-font-provider", "none"),
                ("sub-fonts-dir", str(fonts)),
            )
            for name, value in options:
                set_option(library, handle, name, value)
            if int(library.mpv_initialize(handle)) < 0:
                fail("mpv-initialize-failed")

            api_type = ctypes.c_char_p(b"sw")
            create_parameters = (RenderParam * 2)(
                RenderParam(
                    MPV_RENDER_PARAM_API_TYPE,
                    ctypes.cast(api_type, ctypes.c_void_p),
                ),
                RenderParam(0, None),
            )
            status = int(
                library.mpv_render_context_create(
                    ctypes.byref(render_context), handle, create_parameters
                )
            )
            if status < 0 or not render_context:
                fail("software-render-context-failed")

            command(library, handle, "loadfile", str(video), "replace")
            wait_for_file(library, handle)
            if with_subtitle:
                command(library, handle, "sub-add", str(subtitle), "select")

            stride = WIDTH * 4
            storage = (ctypes.c_ubyte * (stride * HEIGHT + 63))()
            aligned_address = (ctypes.addressof(storage) + 63) & ~63
            surface_size = (ctypes.c_int * 2)(WIDTH, HEIGHT)
            surface_format = ctypes.c_char_p(b"rgb0")
            surface_stride = ctypes.c_size_t(stride)
            render_parameters = (RenderParam * 5)(
                RenderParam(
                    MPV_RENDER_PARAM_SW_SIZE,
                    ctypes.cast(surface_size, ctypes.c_void_p),
                ),
                RenderParam(
                    MPV_RENDER_PARAM_SW_FORMAT,
                    ctypes.cast(surface_format, ctypes.c_void_p),
                ),
                RenderParam(
                    MPV_RENDER_PARAM_SW_STRIDE,
                    ctypes.cast(ctypes.byref(surface_stride), ctypes.c_void_p),
                ),
                RenderParam(MPV_RENDER_PARAM_SW_POINTER, aligned_address),
                RenderParam(0, None),
            )

            deadline = time.monotonic() + RENDER_TIMEOUT_SECONDS
            render_count = 0
            maximum_bright_pixels = 0
            maximum_channel = 0
            while time.monotonic() < deadline:
                event = library.mpv_wait_event(handle, 0.01).contents
                if event.error < 0:
                    fail("mpv-render-event-error")
                update = int(library.mpv_render_context_update(render_context))
                if update & MPV_RENDER_UPDATE_FRAME or render_count == 0:
                    status = int(
                        library.mpv_render_context_render(
                            render_context, render_parameters
                        )
                    )
                    if status < 0:
                        fail("software-render-failed")
                    pixels = ctypes.string_at(aligned_address, stride * HEIGHT)
                    bright_pixels = 0
                    frame_maximum = 0
                    for offset in range(0, len(pixels), 4):
                        red, green, blue = pixels[offset : offset + 3]
                        frame_maximum = max(frame_maximum, red, green, blue)
                        if (
                            red > BRIGHT_CHANNEL_THRESHOLD
                            or green > BRIGHT_CHANNEL_THRESHOLD
                            or blue > BRIGHT_CHANNEL_THRESHOLD
                        ):
                            bright_pixels += 1
                    maximum_bright_pixels = max(
                        maximum_bright_pixels, bright_pixels
                    )
                    maximum_channel = max(maximum_channel, frame_maximum)
                    render_count += 1
                if render_count >= 3:
                    break
            if render_count < 1:
                fail("no-rendered-frame")
            return {
                "renderCount": render_count,
                "maximumBrightPixels": maximum_bright_pixels,
                "maximumChannel": maximum_channel,
            }
        finally:
            if render_context:
                library.mpv_render_context_free(render_context)
            library.mpv_terminate_destroy(handle)


def case_worker(
    connection: Connection,
    library_path: str,
    with_subtitle: bool,
) -> None:
    try:
        result = render_case(library_path, with_subtitle)
        payload: dict[str, object] = {"ok": True, "result": result}
    except SmokeFailure as error:
        payload = {"ok": False, "error": str(error)}
    except BaseException:
        payload = {"ok": False, "error": "unexpected-worker-failure"}
    try:
        connection.send(payload)
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        connection.close()


def run_case(library_path: str, with_subtitle: bool) -> dict[str, int]:
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(
        target=case_worker,
        args=(sending, library_path, with_subtitle),
        daemon=True,
    )
    process.start()
    sending.close()
    process.join(CASE_TIMEOUT_SECONDS)
    if process.is_alive():
        process.terminate()
        process.join(1.0)
        if process.is_alive():
            process.kill()
            process.join(1.0)
        receiving.close()
        fail("case-hard-timeout")
    if process.exitcode != 0 or not receiving.poll(0.25):
        receiving.close()
        fail("case-worker-exit")
    payload = receiving.recv()
    receiving.close()
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        error = payload.get("error") if isinstance(payload, dict) else None
        fail(error if isinstance(error, str) and error else "case-worker-failed")
    result = payload.get("result")
    if not isinstance(result, dict):
        fail("case-result-invalid")
    return result


def validate_metrics(baseline: dict[str, int], ass: dict[str, int]) -> int:
    for metrics in (baseline, ass):
        if set(metrics) != {
            "renderCount",
            "maximumBrightPixels",
            "maximumChannel",
        }:
            fail("metric-schema-invalid")
        if (
            not isinstance(metrics["renderCount"], int)
            or metrics["renderCount"] < 1
            or not isinstance(metrics["maximumBrightPixels"], int)
            or metrics["maximumBrightPixels"] < 0
            or not isinstance(metrics["maximumChannel"], int)
            or not 0 <= metrics["maximumChannel"] <= 255
        ):
            fail("metric-value-invalid")
    if baseline["maximumBrightPixels"] > MAXIMUM_BASELINE_BRIGHT_PIXELS:
        fail("baseline-is-not-black")
    if baseline["maximumChannel"] > BRIGHT_CHANNEL_THRESHOLD:
        fail("baseline-channel-is-not-black")
    if ass["maximumBrightPixels"] < MINIMUM_ASS_BRIGHT_PIXELS:
        fail("ass-change-too-small")
    if ass["maximumChannel"] <= BRIGHT_CHANNEL_THRESHOLD:
        fail("ass-has-no-bright-channel")
    delta = ass["maximumBrightPixels"] - baseline["maximumBrightPixels"]
    if delta < MINIMUM_BRIGHT_PIXEL_DELTA:
        fail("ass-delta-too-small")
    return delta


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a generated P6 baseline and external ASS text with bundled libmpv."
    )
    parser.add_argument("--library-dir", type=Path, required=True)
    parser.add_argument("--library", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    try:
        if arguments.platform not in {
            "linux-x86_64",
            "linux-aarch64",
            "macos-aarch64",
            "windows-x86_64",
        }:
            fail("unsupported-platform")
        if (
            not arguments.library
            or Path(arguments.library).name != arguments.library
            or arguments.library in {".", ".."}
        ):
            fail("library-name-invalid")
        if arguments.library_dir.is_symlink():
            fail("library-directory-invalid")
        library_dir = arguments.library_dir.resolve(strict=True)
        library_input = library_dir / arguments.library
        if library_input.is_symlink():
            fail("bundled-library-invalid")
        library = library_input.resolve(strict=True)
        if library.parent != library_dir or not library.is_file():
            fail("bundled-library-invalid")
        font_fixture = generated_font_fixture()
        if hashlib.sha256(font_fixture).hexdigest() != FONT_FIXTURE_SHA256:
            fail("generated-font-hash-mismatch")
        output = arguments.output
        if output.exists() or output.is_symlink():
            fail("output-already-exists")
        output.parent.resolve(strict=True)

        baseline = run_case(str(library), False)
        ass = run_case(str(library), True)
        delta = validate_metrics(baseline, ass)
        evidence = {
            "schemaVersion": 2,
            "platform": arguments.platform,
            "renderer": "libmpv-sw",
            "video": {
                "format": "P6",
                "width": WIDTH,
                "height": HEIGHT,
                "generated": True,
            },
            "subtitle": {
                "format": "ASS-v4.00+",
                "mode": "external-text",
                "text": SUBTITLE_TEXT,
                "fontFamily": FONT_FAMILY,
            },
            "fontFixture": {
                "name": FONT_FIXTURE_NAME,
                "generated": True,
                "family": FONT_FAMILY,
                "licenseSpdx": FONT_FIXTURE_LICENSE,
                "sha256": FONT_FIXTURE_SHA256,
            },
            "brightChannelThreshold": BRIGHT_CHANNEL_THRESHOLD,
            "baseline": baseline,
            "ass": ass,
            "brightPixelDelta": delta,
            "passed": True,
        }

        temporary_output: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=output.parent,
                prefix=".ass-smoke-",
                suffix=".json.tmp",
                delete=False,
            ) as temporary:
                json.dump(evidence, temporary, indent=2, sort_keys=True)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_output = Path(temporary.name)
            os.replace(temporary_output, output)
            temporary_output = None
        finally:
            if temporary_output is not None:
                temporary_output.unlink(missing_ok=True)
    except SmokeFailure as error:
        print(f"ASS smoke test failed: {error}", file=os.sys.stderr)
        return 1
    except BaseException:
        print("ASS smoke test failed: unexpected-supervisor-failure", file=os.sys.stderr)
        return 1

    print("ASS smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
