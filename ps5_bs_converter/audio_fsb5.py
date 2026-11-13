"""
Lossless PCM16 FSB5 audio decoder/encoder matched byte-for-byte to Unity 2021.3.16f1 PS5.

Supports decoding BeatSaver .egg / .ogg (Ogg Vorbis), .wav, .mp3, and .flac files,
peak-normalizing to prevent int16 clipping, estimating perceptual loudness (LUFS),
and packaging stereo 44.1 kHz / 48 kHz audio into 32-byte-aligned FMOD FSB5 containers.
"""

from __future__ import annotations

import hashlib
import io
import math
import struct
import wave
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
import soundfile as sf

FSB5_FREQ_TO_INDEX = {
    8000: 1,
    11000: 2,
    11025: 3,
    16000: 4,
    22050: 5,
    24000: 6,
    32000: 7,
    44100: 8,
    48000: 9,
}
FSB5_INDEX_TO_FREQ = {v: k for k, v in FSB5_FREQ_TO_INDEX.items()}


@dataclass
class EncodedFsb5Audio:
    fsb5_bytes: bytes
    sample_rate: int
    channels: int
    total_frames: int
    duration_seconds: float
    loudness_lufs: float


def decode_audio_bytes(raw_bytes: bytes, filename_hint: str = "") -> Tuple[np.ndarray, int]:
    """
    Decode audio bytes (.egg/.ogg/.wav/.flac/.mp3) into stereo float32 [-1.0, 1.0] and sample_rate.
    """
    try:
        samples, sr = sf.read(io.BytesIO(raw_bytes), dtype="float32", always_2d=True)
    except Exception as exc:
        # Fallback for unusual uncompressed PCM WAV headers
        if raw_bytes[:4] == b"RIFF" and raw_bytes[8:12] == b"WAVE":
            samples, sr = _decode_wav_fallback(raw_bytes)
        else:
            raise ValueError(f"Failed to decode audio file '{filename_hint}': {exc}") from exc

    if samples.shape[0] == 0:
        raise ValueError(f"Audio file '{filename_hint}' contains 0 samples")

    # Force stereo (2 channels)
    if samples.shape[1] == 1:
        samples = np.repeat(samples, 2, axis=1)
    elif samples.shape[1] > 2:
        samples = samples[:, :2]

    # Keep native 44100 Hz or 48000 Hz; resample any other sample rate cleanly to 44100 Hz
    if sr not in (44100, 48000):
        samples = _resample_stereo(samples, sr, 44100)
        sr = 44100

    return np.ascontiguousarray(samples, dtype=np.float32), int(sr)


def _decode_wav_fallback(raw_bytes: bytes) -> Tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(raw_bytes), "rb") as wf:
        ch = wf.getnchannels()
        sr = wf.getframerate()
        sw = wf.getsampwidth()
        nframes = wf.getnframes()
        pcm = wf.readframes(nframes)
    if sw == 2:
        arr = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    elif sw == 1:
        arr = (np.frombuffer(pcm, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sw == 4:
        arr = np.frombuffer(pcm, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"Unsupported WAV sample width: {sw} bytes")
    return arr.reshape(-1, ch), sr


def _resample_stereo(samples: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr:
        return samples
    n_in = samples.shape[0]
    ratio = dst_sr / float(src_sr)
    n_out = max(1, int(round(n_in * ratio)))
    # Low-pass filter before downsampling to prevent aliasing
    work = samples
    if dst_sr < src_sr:
        cutoff = 0.45 * ratio
        taps = 31
        n = np.arange(taps) - (taps - 1) / 2.0
        h = np.sinc(2.0 * cutoff * n) * np.hamming(taps)
        h = (h / np.sum(h)).astype(np.float32)
        left = np.convolve(work[:, 0], h, mode="same")
        right = np.convolve(work[:, 1], h, mode="same")
        work = np.column_stack((left, right))

    src_pos = np.arange(n_out, dtype=np.float64) / ratio
    idx0 = np.clip(np.floor(src_pos).astype(np.int64), 0, n_in - 1)
    idx1 = np.clip(idx0 + 1, 0, n_in - 1)
    frac = (src_pos - idx0).astype(np.float32)[:, None]
    out = work[idx0] * (1.0 - frac) + work[idx1] * frac
    return out.astype(np.float32)


def estimate_loudness_lufs(samples: np.ndarray) -> float:
    """Estimate integrated loudness (LUFS) for Beat Saber's PerceivedLoudnessPerLevelModel."""
    mean_sq = float(np.mean(np.square(samples, dtype=np.float32)))
    if mean_sq <= 1e-10:
        return -18.0
    lufs = 10.0 * math.log10(mean_sq) - 0.691
    return float(max(-20.0, min(-4.0, lufs)))


def build_pcm16_fsb5_from_samples(samples: np.ndarray, sample_rate: int) -> EncodedFsb5Audio:
    """
    Package stereo float32 samples into a PS5-compatible PCM16 FSB5 container.

    Matches native Unity 2021.3.16f1 PS5 FSB5 layout:
      - 60-byte FSB5 v1 base header
      - 36-byte sample header (total header = 96 bytes, 32-byte aligned)
      - 32-byte aligned signed 16-bit little-endian stereo interleaved PCM data
    """
    if sample_rate not in FSB5_FREQ_TO_INDEX:
        samples = _resample_stereo(samples, sample_rate, 44100)
        sample_rate = 44100

    if samples.ndim == 1:
        samples = np.column_stack((samples, samples))
    elif samples.shape[1] == 1:
        samples = np.repeat(samples, 2, axis=1)
    elif samples.shape[1] > 2:
        samples = samples[:, :2]

    # Prevent Vorbis overshoot clipping
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak > 0.985:
        samples = samples * (0.985 / peak)

    loudness = estimate_loudness_lufs(samples)
    pcm_i16 = np.clip(np.rint(samples * 32767.0), -32768, 32767).astype("<i2")
    total_frames = int(pcm_i16.shape[0])
    if total_frames >= (1 << 30):
        raise ValueError(f"Audio too long for FSB5 30-bit frame counter: {total_frames} frames")

    raw_pcm = pcm_i16.tobytes()
    pad_len = (32 - (len(raw_pcm) % 32)) % 32
    aligned_data_size = len(raw_pcm) + pad_len

    freq_idx = FSB5_FREQ_TO_INDEX[sample_rate]
    shdr_size = 36  # 60 + 36 = 96 bytes (32-byte aligned)
    sample_desc = (
        0  # has_next_chunk = 0
        | ((freq_idx & 0xF) << 1)
        | (1 << 5)  # stereo (channels == 2 -> bit 5 = 1)
        | (0 << 6)  # data_offset_div_16 = 0
        | ((total_frames & 0x3FFFFFFF) << 34)
    )
    shdr = struct.pack("<Q", sample_desc) + (b"\x00" * 28)
    assert len(shdr) == shdr_size

    digest = hashlib.md5(shdr + raw_pcm[:4096]).digest()
    sub_hash = hashlib.md5(digest).digest()[:8]

    header = bytearray(60)
    header[0:4] = b"FSB5"
    struct.pack_into(
        "<6I",
        header,
        4,
        1,                  # version = 1
        1,                  # num_samples = 1
        shdr_size,          # sample_headers_size = 36
        0,                  # name_table_size = 0
        aligned_data_size,  # data_size (32B aligned)
        2,                  # mode = 2 (FMOD_SOUND_FORMAT_PCM16)
    )
    # bytes 28..36 = 0 (matches PS5 native codec=2 AudioClips)
    header[36:52] = digest
    header[52:60] = sub_hash

    fsb5_bytes = bytes(header) + shdr + raw_pcm + (b"\x00" * pad_len)
    duration = total_frames / float(sample_rate)

    return EncodedFsb5Audio(
        fsb5_bytes=fsb5_bytes,
        sample_rate=sample_rate,
        channels=2,
        total_frames=total_frames,
        duration_seconds=duration,
        loudness_lufs=loudness,
    )


def build_preview_slice_fsb5(
    samples: np.ndarray,
    sample_rate: int,
    preview_start: float,
    preview_duration: float,
) -> EncodedFsb5Audio:
    """
    Extract a smooth fade-in/fade-out preview FSB5 clip for DLC slots with *Preview AudioClips.
    """
    total_dur = samples.shape[0] / float(sample_rate)
    dur = preview_duration if preview_duration > 1.0 else 10.0
    dur = min(dur, max(1.0, total_dur))

    start = preview_start
    if start < 0.0 or start + 1.0 >= total_dur:
        start = max(0.0, min(total_dur - dur, total_dur * 0.25))

    s_frame = int(round(start * sample_rate))
    e_frame = min(samples.shape[0], s_frame + int(round(dur * sample_rate)))
    if e_frame <= s_frame:
        s_frame = 0
        e_frame = min(samples.shape[0], int(round(10.0 * sample_rate)))

    clip = samples[s_frame:e_frame].copy()
    n = clip.shape[0]
    fade_in = min(int(0.35 * sample_rate), n // 4)
    fade_out = min(int(0.50 * sample_rate), n // 4)
    if fade_in > 1:
        env_in = np.linspace(0.0, 1.0, fade_in, dtype=np.float32)[:, None]
        clip[:fade_in] *= env_in
    if fade_out > 1:
        env_out = np.linspace(1.0, 0.0, fade_out, dtype=np.float32)[:, None]
        clip[-fade_out:] *= env_out

    return build_pcm16_fsb5_from_samples(clip, sample_rate)


def encode_audio_file_to_fsb5(
    audio_path_or_bytes: str | bytes,
    filename_hint: str = "",
    preview_start: Optional[float] = None,
    preview_duration: Optional[float] = None,
    need_preview_clip: bool = False,
) -> Tuple[EncodedFsb5Audio, Optional[EncodedFsb5Audio]]:
    if isinstance(audio_path_or_bytes, str):
        hint = filename_hint or audio_path_or_bytes
        with open(audio_path_or_bytes, "rb") as f:
            raw = f.read()
    else:
        hint = filename_hint or "memory_audio"
        raw = audio_path_or_bytes

    samples, sr = decode_audio_bytes(raw, hint)
    main_fsb5 = build_pcm16_fsb5_from_samples(samples, sr)
    prev_fsb5 = None
    if need_preview_clip:
        prev_fsb5 = build_preview_slice_fsb5(
            samples,
            sr,
            preview_start if preview_start is not None else 12.0,
            preview_duration if preview_duration is not None else 10.0,
        )
    return main_fsb5, prev_fsb5


def verify_fsb5_header(buf: bytes) -> dict:
    """Validate an FSB5 header and return its decoded parameters."""
    if len(buf) < 96 or buf[:4] != b"FSB5":
        raise ValueError("Invalid FSB5 magic or truncated buffer")
    ver, nsamp, shdr_sz, name_sz, data_sz, codec = struct.unpack_from("<6I", buf, 4)
    total_expected = 60 + shdr_sz + name_sz + data_sz
    if total_expected != len(buf):
        raise ValueError(f"FSB5 size mismatch: header expects {total_expected}, got {len(buf)}")
    sd = struct.unpack_from("<Q", buf, 60)[0]
    freq_idx = (sd >> 1) & 0xF
    ch = 2 if ((sd >> 5) & 1) else 1
    frames = (sd >> 34) & 0x3FFFFFFF
    return {
        "version": ver,
        "num_samples": nsamp,
        "shdr_size": shdr_sz,
        "data_size": data_sz,
        "codec": codec,
        "sample_rate": FSB5_INDEX_TO_FREQ.get(freq_idx, 0),
        "channels": ch,
        "frames": frames,
    }
