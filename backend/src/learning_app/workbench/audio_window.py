"""Candidate: verified local handle -> bounded audio-only PCM16 mono 16 kHz.

No URL or application import. The caller owns authorization and the source
version. File hashing reads container bytes; only the selected audio stream is
demuxed/decoded. No image frame or video decoder operation is requested.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import math
import os
import re
import wave
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO, Callable

import av

RATE = 16_000
MAX_WINDOW_MS = 30_000
PREROLL_MS = 2_000
# FFmpeg avformat_find_stream_info can attempt native codec probes while opening
# a mixed container. The AVFormatContext codec whitelist also rejects video
# decoders during that internal probe, before Python stream selection occurs.
AUDIO_CODECS = "pcm_s16le,pcm_s16be,pcm_s24le,pcm_s24be,pcm_s32le,pcm_s32be,pcm_f32le,pcm_f64le,aac,mp3,flac,opus,alac,ac3,eac3,wmav2"


class AudioWindowError(ValueError):
    pass


@dataclass(frozen=True)
class SourceIdentity:
    asset_id: str
    source_version: str
    bytes: int
    sha256: str

    def validate(self):
        if (
            not self.asset_id
            or not self.source_version
            or self.bytes <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", self.sha256)
        ):
            raise AudioWindowError("ASR_SOURCE_IDENTITY_INVALID")


def _digest(handle):
    handle.seek(0)
    digest = hashlib.sha256()
    count = 0
    while block := handle.read(1024 * 1024):
        digest.update(block)
        count += len(block)
    handle.seek(0)
    return count, digest.hexdigest()


@contextmanager
def locked_source(path: Path, identity: SourceIdentity):
    """Path must already be checked by the owning source authority.

    Windows FILE_SHARE_READ deliberately prevents concurrent write/delete while
    this lease exists. Other platforms need a caller-provided immutable lease.
    """
    identity.validate()
    if os.name == "nt":
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
        if handle == wintypes.HANDLE(-1).value:
            raise AudioWindowError("ASR_SOURCE_LEASE_DENIED")
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        source = os.fdopen(fd, "rb")
    else:
        source = path.open("rb")
    try:
        if _digest(source) != (identity.bytes, identity.sha256):
            raise AudioWindowError("ASR_SOURCE_VERSION_CONFLICT")
        yield source
        if _digest(source) != (identity.bytes, identity.sha256):
            raise AudioWindowError("ASR_SOURCE_CHANGED")
    finally:
        source.close()


class BorrowedIO:
    """FFmpeg may close this view, but cannot close the source-version lease."""

    def __init__(self, handle):
        self.handle = handle

    def read(self, n=-1):
        return self.handle.read(n)

    def seek(self, offset, whence=0):
        return self.handle.seek(offset, whence)

    def tell(self):
        return self.handle.tell()

    def readable(self):
        return True

    def seekable(self):
        return True

    def writable(self):
        return False

    def close(self):
        pass


def _deny_open(*args, **kwargs):
    raise AudioWindowError("ASR_EXTERNAL_RESOURCE_DENIED")


def _container(handle):
    handle.seek(0)
    magic = handle.read(16)
    handle.seek(0)
    if magic[:4] in {b"RIFF", b"RF64"} and magic[8:12] == b"WAVE":
        format_name = "wav"
    elif magic[4:8] == b"ftyp":
        format_name = "mov"
    elif magic[:4] == b"\x1aE\xdf\xa3":
        format_name = "matroska"
    elif magic[:4] == b"fLaC":
        format_name = "flac"
    elif magic[:5] == b"OggS\x00":
        format_name = "ogg"
    elif (
        magic[:3] == b"ID3"
        and len(magic) >= 10
        and magic[3] in {2, 3, 4}
        and all(b < 128 for b in magic[6:10])
    ):
        format_name = "mp3"
    elif (
        len(magic) >= 4
        and magic[0] == 0xFF
        and magic[1] & 0xE0 == 0xE0
        and (magic[1] >> 3) & 3 != 1
        and (magic[1] >> 1) & 3 != 0
        and 1 <= magic[2] >> 4 <= 14
        and (magic[2] >> 2) & 3 != 3
    ):
        # MPEG audio frame header; the layer bits exclude AAC ADTS sync.
        format_name = "mp3"
    else:
        raise AudioWindowError("ASR_CONTAINER_UNSUPPORTED")
    try:
        return av.open(
            BorrowedIO(handle),
            mode="r",
            format=format_name,
            io_open=_deny_open,
            timeout=(5.0, 5.0),
            options={
                "protocol_whitelist": "",
                "codec_whitelist": AUDIO_CODECS,
                "analyzeduration": "1",
                "probesize": "32768",
            },
        )
    except AudioWindowError:
        raise
    except Exception as error:
        raise AudioWindowError("ASR_CONTAINER_INVALID") from error


def _audio(container, index):
    # The only stream collection accessed by this module is streams.audio.
    tracks = tuple(container.streams.audio)
    if not tracks:
        raise AudioWindowError("ASR_AUDIO_TRACK_MISSING")
    if type(index) is not int or not 0 <= index < len(tracks):
        raise AudioWindowError("ASR_AUDIO_TRACK_INVALID")
    stream = tracks[index]
    if not isinstance(stream, av.audio.stream.AudioStream) or stream.type != "audio":
        raise AudioWindowError("ASR_AUDIO_STREAM_GUARD")
    # Fixed Vorbis fixture showed a native timestamp discontinuity at transient
    # block transitions. Do not weaken gap checks to label it fully covered.
    if stream.codec_context.name == "vorbis":
        raise AudioWindowError("ASR_AUDIO_CODEC_UNSUPPORTED")
    stream.codec_context.thread_count = 1
    return stream


def _origin(container):
    # Container playback zero, not the first audio sample: preserve delayed audio.
    return Fraction(container.start_time or 0, av.time_base)


def probe_audio(handle: BinaryIO, identity: SourceIdentity, *, track=0, progress=None):
    """Audio packet scan, no decoded frames, no fallback to video duration.

    Metadata duration is reported separately. Packet timestamps bound the audio
    extent if metadata duration is absent. Gaps are not valid speech coverage.
    """
    identity.validate()
    with _container(handle) as container:
        stream = _audio(container, track)
        origin = _origin(container)
        first = last = None
        packets = 0
        complete_packet_durations = True
        for packet in container.demux(stream):
            if packet.stream.index != stream.index or packet.stream.type != "audio":
                raise AudioWindowError("ASR_AUDIO_PACKET_GUARD")
            if packet.pts is None or not packet.size:
                continue
            begin = packet.pts * packet.time_base - origin
            end = begin + (packet.duration or 0) * packet.time_base
            complete_packet_durations = complete_packet_durations and bool(
                packet.duration and packet.duration > 0
            )
            first = begin if first is None else min(first, begin)
            last = end if last is None else max(last, end)
            packets += 1
            if progress and packets % 128 == 1:
                progress(
                    {"type": "probe_progress", "audio_packets": packets, "decoded_audio_samples": 0}
                )
        declared_begin = (
            stream.start_time * stream.time_base - origin if stream.start_time is not None else None
        )
        declared_end = (
            declared_begin + stream.duration * stream.time_base
            if declared_begin is not None and stream.duration is not None
            else None
        )
        if first is None:
            raise AudioWindowError("ASR_AUDIO_EMPTY")
        begin = max(Fraction(0), declared_begin if declared_begin is not None else first)
        end = declared_end if declared_end is not None else last
        basis = (
            "audio_stream_metadata" if declared_end is not None else "audio_packet_timestamp_scan"
        )
        if declared_end is not None and complete_packet_durations and last < declared_end:
            # Ogg/Opus metadata can include pre-skip. Packet granules provide the
            # actual end; do not schedule an artificial extra tail interval.
            end = last
            basis = "audio_stream_metadata_capped_by_packet_extent"
        elif declared_end is None and not complete_packet_durations:
            raise AudioWindowError("ASR_AUDIO_DURATION_UNKNOWN")
        if end is None or end <= begin:
            raise AudioWindowError("ASR_AUDIO_DURATION_UNKNOWN")
        return {
            "source": asdict(identity),
            "track_ordinal": track,
            "stream_index": stream.index,
            "codec": stream.codec_context.name,
            "sample_rate": stream.codec_context.sample_rate,
            "stream_time_base": str(stream.time_base),
            "container_origin_seconds": str(origin),
            "audio_start_seconds": str(begin),
            "audio_end_seconds": str(end),
            "audio_start_ms": math.ceil(begin * 1000),
            "audio_end_ms": math.ceil(end * 1000),
            "extent_basis": basis,
            "declared_audio_end_seconds": str(declared_end) if declared_end is not None else None,
            "packet_extent_start_seconds": str(first),
            "packet_extent_end_seconds": str(last),
            "audio_packets_scanned": packets,
            "decoded_frames": 0,
        }


def planned_windows(probe, window_ms=MAX_WINDOW_MS):
    if type(window_ms) is not int or not 1 <= window_ms <= MAX_WINDOW_MS:
        raise AudioWindowError("ASR_AUDIO_WINDOW_LIMIT")
    # Grid belongs to container playback timeline. Metadata may end between ms.
    end_ms = probe["audio_end_ms"]
    return [
        (start, min(start + window_ms, end_ms))
        for start in range(0, end_ms, window_ms)
        if min(start + window_ms, end_ms) > probe["audio_start_ms"]
    ]


def decode_window(
    handle: BinaryIO,
    identity: SourceIdentity,
    start_ms: int,
    end_ms: int,
    *,
    track=0,
    progress: Callable | None = None,
):
    """Decode a half-open <=30s ownership window, with bounded 2s seek preroll.

    Only owned samples are returned. The decoder may read one final audio frame
    beyond the end to drain the resampler. No adjacent samples enter ASR output.
    Timestamp discontinuities fail rather than create silent artificial audio.
    """
    identity.validate()
    if (
        type(start_ms) is not int
        or type(end_ms) is not int
        or not 0 <= start_ms < end_ms
        or end_ms - start_ms > MAX_WINDOW_MS
    ):
        raise AudioWindowError("ASR_AUDIO_WINDOW_LIMIT")
    requested_start, requested_end = start_ms * 16, end_ms * 16
    chunks = []
    first_output = next_output = None
    native_anchor = native_count = None
    frames = packets = decoded_samples = 0
    metadata = {}
    with _container(handle) as container:
        stream = _audio(container, track)
        origin = _origin(container)
        declared_start = (
            stream.start_time * stream.time_base - origin
            if stream.start_time is not None
            else Fraction(0)
        )
        declared_end = (
            declared_start + stream.duration * stream.time_base
            if stream.duration is not None
            else None
        )
        if declared_end is not None:
            requested_end = min(requested_end, round(declared_end * RATE))
        if requested_end <= requested_start:
            raise AudioWindowError("ASR_AUDIO_WINDOW_EMPTY")
        seek_seconds = max(Fraction(0), Fraction(start_ms - PREROLL_MS, 1000)) + origin
        if seek_seconds > origin:
            container.seek(
                math.floor(seek_seconds / stream.time_base),
                stream=stream,
                backward=True,
                any_frame=False,
            )
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)

        def consume(output):
            nonlocal first_output, next_output
            if not isinstance(output, av.AudioFrame) or output.pts is None:
                raise AudioWindowError("ASR_AUDIO_FRAME_GUARD")
            absolute = output.pts * output.time_base
            output_start = round(absolute * RATE)
            cut_start = max(output_start, requested_start)
            cut_end = min(output_start + output.samples, requested_end)
            if cut_end <= cut_start:
                return
            if next_output is not None and cut_start != next_output:
                raise AudioWindowError("ASR_AUDIO_TIMELINE_GAP")
            pcm = output.to_ndarray().tobytes()
            chunks.append(pcm[(cut_start - output_start) * 2 : (cut_end - output_start) * 2])
            if first_output is None:
                first_output = cut_start
            next_output = cut_end

        done = False
        for packet in container.demux(stream):
            if packet.stream.index != stream.index or packet.stream.type != "audio":
                raise AudioWindowError("ASR_AUDIO_PACKET_GUARD")
            packets += 1
            for frame in packet.decode():
                if (
                    not isinstance(frame, av.AudioFrame)
                    or frame.pts is None
                    or frame.sample_rate <= 0
                ):
                    raise AudioWindowError("ASR_AUDIO_FRAME_GUARD")
                frames += 1
                decoded_samples += frame.samples
                sr = frame.sample_rate
                if (
                    frame.samples > sr * 2
                    or decoded_samples > sr * (end_ms - start_ms + PREROLL_MS + 3000) // 1000
                ):
                    raise AudioWindowError("ASR_AUDIO_DECODE_WORK_LIMIT")
                # Normalize before resampling: MP3's container start is rounded
                # to microseconds, while its audio clock retains exact samples.
                raw_native_pts = round((frame.pts * frame.time_base - origin) * sr)
                if native_anchor is None:
                    if Fraction(raw_native_pts, sr) < Fraction(
                        max(0, start_ms - PREROLL_MS - 1000), 1000
                    ):
                        raise AudioWindowError("ASR_AUDIO_SEEK_RANGE_UNSUPPORTED")
                    # Align seek input to the global 16k output phase. This is
                    # audio-only slicing, not an image/frame extraction path.
                    period = sr // math.gcd(sr, RATE)
                    skip = (-raw_native_pts) % period
                    native_anchor, native_count = raw_native_pts + skip, 0
                    if skip:
                        values = frame.to_ndarray()
                        factor = 1 if frame.format.is_planar else len(frame.layout.channels)
                        frame = av.AudioFrame.from_ndarray(
                            values[:, skip * factor :],
                            format=frame.format.name,
                            layout=frame.layout.name,
                        )
                        frame.sample_rate = sr
                expected_native_pts = native_anchor + native_count
                # Matroska has a coarse 1ms timebase; only rounding jitter is
                # accepted. Actual gaps/discontinuities fail closed.
                if (
                    abs(raw_native_pts - expected_native_pts)
                    > max(1, math.ceil(sr * stream.time_base))
                    and native_count
                ):
                    raise AudioWindowError("ASR_AUDIO_TIMELINE_GAP")
                frame.pts = expected_native_pts
                frame.time_base = Fraction(1, sr)
                native_count += frame.samples
                for output in resampler.resample(frame):
                    consume(output)
                if progress and frames % 16 == 1:
                    progress(
                        {
                            "type": "decode_progress",
                            "audio_packets": packets,
                            "audio_frames": frames,
                            "decoded_audio_samples": decoded_samples,
                            "source_sample_rate": sr,
                        }
                    )
                if Fraction(expected_native_pts + frame.samples, sr) > Fraction(end_ms + 100, 1000):
                    done = True
                    break
            if done:
                break
        for output in resampler.resample(None):
            consume(output)
        metadata = {
            "track_ordinal": track,
            "stream_index": stream.index,
            "codec": stream.codec_context.name,
            "audio_packets_decoded": packets,
            "audio_frames_decoded": frames,
            "decoded_native_samples": decoded_samples,
            "seek_preroll_target_ms": PREROLL_MS,
            "seek_preroll_tolerance_ms": 1000,
            "resampler_postroll_target_ms": 100,
            "decode_work_limit_ms": end_ms - start_ms + PREROLL_MS + 3000,
            "image_frames_requested": 0,
            "selected_stream_type": "audio",
        }
    pcm = b"".join(chunks)
    if not pcm or first_output is None or next_output is None:
        raise AudioWindowError("ASR_AUDIO_WINDOW_EMPTY")
    if len(pcm) != (next_output - first_output) * 2:
        raise AudioWindowError("ASR_AUDIO_SAMPLE_COUNT_INVALID")
    wav = io.BytesIO()
    with wave.open(wav, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(pcm)
    wav_bytes = wav.getvalue()
    return wav_bytes, {
        "source": asdict(identity),
        "requested_start_ms": start_ms,
        "requested_end_ms": end_ms,
        "effective_start_sample": first_output,
        "effective_end_sample": next_output,
        "start_ms": first_output // 16,
        "duration_ms": len(pcm) // 32,
        "sample_count": len(pcm) // 2,
        "sample_rate": RATE,
        "channels": 1,
        "sample_width_bytes": 2,
        "wav_sha256": hashlib.sha256(wav_bytes).hexdigest(),
        "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
        **metadata,
    }
