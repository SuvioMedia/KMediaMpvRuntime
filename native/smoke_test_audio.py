#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Prove that the selected desktop libmpv audio output advances playback."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import multiprocessing
import os
import struct
import sys
import tempfile
import time
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from desktop_common import DesktopPolicyError, load_desktop_policy  # noqa: E402


SAMPLE_RATE_HZ = 48_000
CHANNELS = 2
BITS_PER_SAMPLE = 16
DURATION_MILLISECONDS = 2_000
MINIMUM_ADVANCE_SECONDS = 0.25
FILE_LOAD_TIMEOUT_SECONDS = 5.0
PLAYBACK_TIMEOUT_SECONDS = 8.0
WORKER_TIMEOUT_SECONDS = 16.0
SILENT_WAV_SHA256 = "e3a90286b3b5420103c5d188beff5aeb90737b9ee66c505ee43582c8af92a32b"

MPV_FORMAT_DOUBLE = 5
MPV_EVENT_END_FILE = 7
MPV_EVENT_FILE_LOADED = 8


class AudioSmokeFailure(RuntimeError):
    """A deliberately path-free audio smoke failure."""


def fail(code: str) -> None:
    raise AudioSmokeFailure(code)


def generated_silent_wav() -> bytes:
    """Return a deterministic stereo PCM S16LE WAV containing only zero samples."""

    frame_count = SAMPLE_RATE_HZ * DURATION_MILLISECONDS // 1_000
    block_align = CHANNELS * BITS_PER_SAMPLE // 8
    data_size = frame_count * block_align
    byte_rate = SAMPLE_RATE_HZ * block_align
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + data_size)
        + b"WAVE"
        + b"fmt "
        + struct.pack(
            "<IHHIIHH",
            16,
            1,
            CHANNELS,
            SAMPLE_RATE_HZ,
            byte_rate,
            block_align,
            BITS_PER_SAMPLE,
        )
        + b"data"
        + struct.pack("<I", data_size)
    )
    return header + bytes(data_size)


def fixture_evidence() -> dict[str, object]:
    fixture = generated_silent_wav()
    actual_hash = hashlib.sha256(fixture).hexdigest()
    if actual_hash != SILENT_WAV_SHA256:
        fail("silent-wav-hash-mismatch")
    return {
        "format": "WAVE",
        "sampleRate": SAMPLE_RATE_HZ,
        "channels": CHANNELS,
        "durationMilliseconds": DURATION_MILLISECONDS,
        "generated": True,
    }


def _exact_keys(value: Any, expected: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        fail(code)
    return value


def validate_audio_evidence(
    evidence: object,
    policy: dict[str, Any],
    platform: str,
) -> dict[str, Any]:
    """Validate the closed, device-name-free evidence schema."""

    document = _exact_keys(
        evidence,
        {
            "schemaVersion",
            "platform",
            "backend",
            "backendAvailable",
            "playbackBackend",
            "fixture",
            "currentAo",
            "timePositionAdvanced",
            "passed",
        },
        "audio-evidence-schema-invalid",
    )
    platforms = policy.get("supportedPlatforms")
    if not isinstance(platforms, dict) or platform not in platforms:
        fail("audio-platform-unsupported")
    expected_backend = platforms[platform].get("audioBackend")
    expected_playback_backend = platforms[platform].get("audioSmokeBackend")
    if (
        type(document.get("schemaVersion")) is not int
        or document["schemaVersion"] != 2
        or document.get("platform") != platform
        or document.get("backend") != expected_backend
        or document.get("backendAvailable") is not True
        or document.get("playbackBackend") != expected_playback_backend
        or document.get("currentAo") != expected_playback_backend
    ):
        fail("audio-evidence-identity-invalid")
    if document.get("fixture") != fixture_evidence():
        fail("audio-fixture-evidence-invalid")
    if (
        document.get("timePositionAdvanced") is not True
        or document.get("passed") is not True
    ):
        fail("audio-evidence-not-passing")
    return document


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
    library.mpv_wait_event.restype = ctypes.c_void_p
    library.mpv_get_property.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_void_p,
    ]
    library.mpv_get_property.restype = ctypes.c_int
    library.mpv_get_property_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    library.mpv_get_property_string.restype = ctypes.c_void_p
    library.mpv_free.argtypes = [ctypes.c_void_p]
    library.mpv_free.restype = None
    library.mpv_terminate_destroy.argtypes = [ctypes.c_void_p]
    library.mpv_terminate_destroy.restype = None


class MpvEvent(ctypes.Structure):
    _fields_ = [
        ("event_id", ctypes.c_int),
        ("error", ctypes.c_int),
        ("reply_userdata", ctypes.c_uint64),
        ("data", ctypes.c_void_p),
    ]


def set_option(
    library: ctypes.CDLL,
    handle: int,
    name: str,
    value: str,
) -> None:
    status = int(
        library.mpv_set_option_string(
            handle,
            name.encode("utf-8"),
            value.encode("utf-8"),
        )
    )
    if status < 0:
        fail("mpv-audio-option-rejected")


def command(library: ctypes.CDLL, handle: int, *arguments: str) -> None:
    encoded = [argument.encode("utf-8") for argument in arguments]
    values = (ctypes.c_char_p * (len(encoded) + 1))(*encoded, None)
    if int(library.mpv_command(handle, values)) < 0:
        fail("mpv-audio-command-failed")


def wait_event(library: ctypes.CDLL, handle: int, timeout: float) -> MpvEvent:
    pointer = library.mpv_wait_event(handle, timeout)
    if not pointer:
        fail("mpv-audio-event-null")
    return ctypes.cast(pointer, ctypes.POINTER(MpvEvent)).contents


def string_property(
    library: ctypes.CDLL,
    handle: int,
    name: str,
) -> str | None:
    pointer = library.mpv_get_property_string(handle, name.encode("ascii"))
    if not pointer:
        return None
    try:
        return ctypes.string_at(pointer).decode("utf-8")
    except UnicodeDecodeError:
        fail("mpv-audio-property-not-utf8")
    finally:
        library.mpv_free(pointer)


def audio_backend_choices(library: ctypes.CDLL, handle: int) -> set[str]:
    raw = string_property(library, handle, "option-info/ao/choices")
    if raw is None:
        fail("mpv-audio-backend-list-unavailable")
    choices = raw.split(",")
    if (
        not choices
        or any(not choice or choice.strip() != choice for choice in choices)
        or len(set(choices)) != len(choices)
    ):
        fail("mpv-audio-backend-list-invalid")
    return set(choices)


def double_property(
    library: ctypes.CDLL,
    handle: int,
    name: str,
) -> float | None:
    value = ctypes.c_double()
    status = int(
        library.mpv_get_property(
            handle,
            name.encode("ascii"),
            MPV_FORMAT_DOUBLE,
            ctypes.byref(value),
        )
    )
    if status < 0:
        return None
    result = float(value.value)
    return result if math.isfinite(result) else None


def wait_for_file(library: ctypes.CDLL, handle: int) -> None:
    deadline = time.monotonic() + FILE_LOAD_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        event = wait_event(library, handle, 0.05)
        if event.event_id == MPV_EVENT_FILE_LOADED:
            return
        if event.event_id == MPV_EVENT_END_FILE:
            fail("silent-wav-ended-before-ready")
        if event.error < 0:
            fail("mpv-audio-event-error")
    fail("silent-wav-load-timeout")


def run_audio_case(
    library_path: str,
    platform: str,
    backend: str,
    playback_backend: str,
    policy: dict[str, Any],
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="kmediampv-audio-smoke-") as temporary:
        wav = Path(temporary) / "generated-silent.wav"
        fixture = generated_silent_wav()
        if hashlib.sha256(fixture).hexdigest() != SILENT_WAV_SHA256:
            fail("silent-wav-hash-mismatch")
        wav.write_bytes(fixture)

        try:
            if platform.startswith("windows-"):
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
            fail("mpv-audio-create-failed")
        try:
            for name, value in (
                ("config", "no"),
                ("terminal", "no"),
                ("video", "no"),
                ("pause", "yes"),
                ("ao", playback_backend),
                ("volume", "0"),
                ("mute", "yes"),
            ):
                set_option(library, handle, name, value)
            if int(library.mpv_initialize(handle)) < 0:
                fail("mpv-audio-initialize-failed")
            if backend not in audio_backend_choices(library, handle):
                fail("mpv-required-audio-backend-unavailable")
            command(library, handle, "loadfile", str(wav), "replace")
            wait_for_file(library, handle)

            start = double_property(library, handle, "time-pos")
            if start is None or start < 0.0:
                fail("mpv-audio-start-position-unavailable")
            command(library, handle, "set", "pause", "no")

            observed_backend: str | None = None
            end: float | None = None
            deadline = time.monotonic() + PLAYBACK_TIMEOUT_SECONDS
            rounded_start = round(start, 6)
            while time.monotonic() < deadline:
                event = wait_event(library, handle, 0.02)
                if event.error < 0:
                    fail("mpv-audio-event-error")
                current = string_property(library, handle, "current-ao")
                if current is not None:
                    observed_backend = current
                position = double_property(library, handle, "time-pos")
                if position is not None and position >= start:
                    rounded_end = round(position, 6)
                    if rounded_end - rounded_start >= MINIMUM_ADVANCE_SECONDS:
                        end = rounded_end
                if end is not None and observed_backend == playback_backend:
                    break
                if event.event_id == MPV_EVENT_END_FILE:
                    break
            if observed_backend != playback_backend:
                fail("mpv-audio-backend-mismatch")
            if end is None:
                fail("mpv-audio-position-did-not-advance")

            evidence: dict[str, Any] = {
                "schemaVersion": 2,
                "platform": platform,
                "backend": backend,
                "backendAvailable": True,
                "playbackBackend": playback_backend,
                "fixture": fixture_evidence(),
                "currentAo": observed_backend,
                "timePositionAdvanced": True,
                "passed": True,
            }
            return validate_audio_evidence(evidence, policy, platform)
        finally:
            library.mpv_terminate_destroy(handle)


def case_worker(
    connection: Connection,
    library_path: str,
    platform: str,
    backend: str,
    playback_backend: str,
    policy: dict[str, Any],
) -> None:
    try:
        result = run_audio_case(
            library_path,
            platform,
            backend,
            playback_backend,
            policy,
        )
        payload: dict[str, object] = {"ok": True, "result": result}
    except AudioSmokeFailure as error:
        payload = {"ok": False, "error": str(error)}
    except BaseException:
        payload = {"ok": False, "error": "unexpected-audio-worker-failure"}
    try:
        connection.send(payload)
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        connection.close()


def run_worker(
    library: Path,
    platform: str,
    backend: str,
    playback_backend: str,
    policy: dict[str, Any],
) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(
        target=case_worker,
        args=(
            sending,
            str(library),
            platform,
            backend,
            playback_backend,
            policy,
        ),
        daemon=True,
    )
    process.start()
    sending.close()
    process.join(WORKER_TIMEOUT_SECONDS)
    if process.is_alive():
        process.terminate()
        process.join(1.0)
        if process.is_alive():
            process.kill()
            process.join(1.0)
        receiving.close()
        fail("audio-worker-hard-timeout")
    if process.exitcode != 0 or not receiving.poll(0.25):
        receiving.close()
        fail("audio-worker-exit")
    payload = receiving.recv()
    receiving.close()
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        error = payload.get("error") if isinstance(payload, dict) else None
        fail(error if isinstance(error, str) and error else "audio-worker-failed")
    result = payload.get("result")
    if not isinstance(result, dict):
        fail("audio-worker-result-invalid")
    return validate_audio_evidence(result, policy, platform)


def write_evidence(output: Path, evidence: dict[str, Any]) -> None:
    if output.exists() or output.is_symlink():
        fail("audio-output-already-exists")
    output.parent.resolve(strict=True)
    temporary_output: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=".audio-smoke-",
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--library-dir", type=Path, required=True)
    parser.add_argument("--library", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    try:
        policy = load_desktop_policy(arguments.policy)
        platforms = policy["supportedPlatforms"]
        if arguments.platform not in platforms:
            fail("audio-platform-unsupported")
        if (
            not arguments.library
            or Path(arguments.library).name != arguments.library
            or arguments.library in {".", ".."}
            or arguments.library_dir.is_symlink()
        ):
            fail("audio-library-input-invalid")
        directory = arguments.library_dir.resolve(strict=True)
        library_input = directory / arguments.library
        if library_input.is_symlink():
            fail("audio-library-input-invalid")
        library = library_input.resolve(strict=True)
        if library.parent != directory or not library.is_file():
            fail("audio-library-input-invalid")
        evidence = run_worker(
            library,
            arguments.platform,
            platforms[arguments.platform]["audioBackend"],
            platforms[arguments.platform]["audioSmokeBackend"],
            policy,
        )
        write_evidence(arguments.output, evidence)
    except AudioSmokeFailure as error:
        print(f"Audio smoke test failed: {error}.", file=sys.stderr)
        return 1
    except (DesktopPolicyError, OSError):
        print("Audio smoke test failed.", file=sys.stderr)
        return 1

    print("Audio smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
