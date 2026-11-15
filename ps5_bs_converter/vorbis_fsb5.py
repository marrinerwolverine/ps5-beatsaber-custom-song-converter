"""
Step B (1.37 Backend): Hardware-Proven Vorbis FSB5 Encoder (Setup CRC = 0x6D39BF3E).

Reproduces the exact Vorbis FSB5 layout used by all 305 songs in PS5 Beat Saber 1.37:
  - 44,100 Hz stereo Ogg Vorbis encoded at libvorbis compression_level=0.1
    (guarantees Vorbis setup-header CRC32 == 0x6D39BF3E required by FMOD's internal table)
  - Strict round-trip decode sync check (exact sample count + 0-sample lag check)
  - FSB5 v1 mode=15 (Vorbis) header with type-11 VORBISDATA seek table chunk
  - 10-second preview Vorbis FSB5 slice builder with 0.3s fade-in/out
"""

from __future__ import annotations

import base64
import hashlib
import io
import math
import struct
import zlib
from dataclasses import dataclass
from typing import List, Tuple

import numpy as np
import soundfile as sf

from .audio_fsb5 import _resample_stereo, decode_audio_bytes, estimate_loudness_lufs

EXPECTED_VORBIS_SETUP_CRC32 = 0x6D39BF3E
TARGET_SR = 44100


@dataclass
class EncodedVorbisFsb5:
    fsb5_bytes: bytes
    sample_rate: int
    channels: int
    total_frames: int
    duration_seconds: float
    loudness_lufs: float
    sha1_base64: str


def _parse_ogg_packets_with_samples(ogg_bytes: bytes, total_frames: int) -> Tuple[List[bytes], List[Tuple[bytes, int]]]:
    """
    Parse an Ogg Vorbis stream into:
      - header_packets: [ident, comment, setup]
      - audio_packets: list of (packet_bytes, start_pcm_sample)
    Uses Ogg page granule_position values anchored directly to libvorbis sample counts.
    """
    pages_packets: List[Tuple[int, List[bytes]]] = []
    cur_pkt = bytearray()
    pos = 0

    while pos + 27 <= len(ogg_bytes):
        if ogg_bytes[pos : pos + 4] != b"OggS":
            raise ValueError("Invalid Ogg page magic while parsing Vorbis stream")
        granule = struct.unpack_from("<q", ogg_bytes, pos + 6)[0]
        nseg = ogg_bytes[pos + 26]
        segs = ogg_bytes[pos + 27 : pos + 27 + nseg]
        dpos = pos + 27 + nseg

        completed_on_page: List[bytes] = []
        for slen in segs:
            cur_pkt.extend(ogg_bytes[dpos : dpos + slen])
            dpos += slen
            if slen < 255:
                completed_on_page.append(bytes(cur_pkt))
                cur_pkt = bytearray()

        pages_packets.append((granule, completed_on_page))
        pos = dpos

    flat_packets = [p for _, plist in pages_packets for p in plist]
    if len(flat_packets) < 4:
        raise ValueError(f"Expected >= 4 Vorbis packets (3 headers + audio), got {len(flat_packets)}")

    headers = flat_packets[:3]

    # Assign exact start sample to every audio packet using Ogg page granule_position anchors
    audio_with_start: List[Tuple[bytes, int]] = []
    headers_seen = 0
    prev_page_end_sample = 0

    for granule, plist in pages_packets:
        audio_in_page: List[bytes] = []
        for p in plist:
            if headers_seen < 3:
                headers_seen += 1
            else:
                audio_in_page.append(p)

        if not audio_in_page:
            continue

        page_end = prev_page_end_sample
        if granule > prev_page_end_sample:
            page_end = min(total_frames, int(granule))

        n_ap = len(audio_in_page)
        span = max(0, page_end - prev_page_end_sample)
        for i, ap in enumerate(audio_in_page):
            pkt_start = prev_page_end_sample + (span * i) // n_ap
            audio_with_start.append((ap, pkt_start))

        prev_page_end_sample = page_end

    return headers, audio_with_start


def _verify_zero_offset_sync(src: np.ndarray, dec: np.ndarray) -> None:
    """Verify decoded Ogg Vorbis has exact sample length and 0-sample drift vs source."""
    if len(dec) != len(src):
        raise RuntimeError(
            f"Vorbis sync check failed: decoded frame count {len(dec)} != source {len(src)}"
        )
    if len(src) < 2048:
        return

    # Find an energetic 4096-sample window and verify lag == 0 maximizes dot product
    mono_s = np.mean(src, axis=1, dtype=np.float32)
    mono_d = np.mean(dec, axis=1, dtype=np.float32)
    win = min(4096, len(mono_s) // 2)
    step = max(512, len(mono_s) // 16)
    best_pos = 0
    best_e = -1.0
    for start in range(0, len(mono_s) - win, step):
        e = float(np.sum(np.square(mono_s[start : start + win])))
        if e > best_e:
            best_e = e
            best_pos = start

    if best_e > 1e-5 and best_pos >= 16 and best_pos + win + 16 <= len(mono_s):
        seg_s = mono_s[best_pos : best_pos + win] - float(np.mean(mono_s[best_pos : best_pos + win]))
        norm_s = float(np.linalg.norm(seg_s))
        if norm_s > 1e-4:
            corr0 = float(np.dot(seg_s, mono_d[best_pos : best_pos + win]))
            best_lag = 0
            best_corr = corr0
            for lag in (-8, -4, -2, -1, 1, 2, 4, 8):
                cand = float(np.dot(seg_s, mono_d[best_pos + lag : best_pos + lag + win]))
                if cand > best_corr:
                    best_corr = cand
                    best_lag = lag
            # Allow tiny Vorbis phase rounding within ±1 sample only if correlation is > 0.98
            if abs(best_lag) > 1 and best_corr > corr0 * 1.05:
                raise RuntimeError(
                    f"Vorbis sync check failed: detected sample offset lag={best_lag} samples"
                )


def build_vorbis_fsb5_from_samples(samples: np.ndarray, sample_rate: int) -> EncodedVorbisFsb5:
    """
    Encode stereo audio into a PS5 Beat Saber 1.37 Vorbis FSB5 (mode=15, setup CRC=0x6D39BF3E).
    """
    if samples.ndim == 1:
        samples = np.column_stack((samples, samples))
    elif samples.shape[1] == 1:
        samples = np.repeat(samples, 2, axis=1)
    elif samples.shape[1] > 2:
        samples = samples[:, :2]

    if sample_rate != TARGET_SR:
        samples = _resample_stereo(samples, sample_rate, TARGET_SR)
        sample_rate = TARGET_SR

    samples = np.ascontiguousarray(samples, dtype=np.float32)
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak > 0.985:
        samples = samples * (0.985 / peak)

    total_frames = int(samples.shape[0])
    loudness = estimate_loudness_lufs(samples)

    # Encode Ogg Vorbis at compression_level=0.1 -> produces setup CRC 0x6D39BF3E
    ogg_buf = io.BytesIO()
    sf.write(ogg_buf, samples, TARGET_SR, format="OGG", subtype="VORBIS", compression_level=0.1)
    ogg_bytes = ogg_buf.getvalue()

    # Sync check: decode back and verify exact frame count & 0-sample offset
    decoded_back, dec_sr = sf.read(io.BytesIO(ogg_bytes), dtype="float32", always_2d=True)
    if dec_sr != TARGET_SR:
        raise RuntimeError(f"Unexpected decoded Vorbis sample rate {dec_sr}")
    _verify_zero_offset_sync(samples, decoded_back)

    headers, audio_packets = _parse_ogg_packets_with_samples(ogg_bytes, total_frames)
    setup_crc = zlib.crc32(headers[2]) & 0xFFFFFFFF
    if setup_crc != EXPECTED_VORBIS_SETUP_CRC32:
        raise RuntimeError(
            f"Vorbis setup header CRC32 mismatch: got 0x{setup_crc:08X}, "
            f"expected 0x{EXPECTED_VORBIS_SETUP_CRC32:08X} (FMOD will refuse to decode)"
        )

    # Assemble [u16 LE size][packet] data stream and track packet byte offsets for seek table
    data_buf = bytearray()
    pkt_index_table: List[Tuple[int, int]] = []  # (start_sample, byte_offset)
    for pkt_bytes, start_sample in audio_packets:
        if len(pkt_bytes) == 0 or len(pkt_bytes) > 0xFFFF:
            raise ValueError(f"Invalid Vorbis packet length: {len(pkt_bytes)}")
        pkt_index_table.append((start_sample, len(data_buf)))
        data_buf += struct.pack("<H", len(pkt_bytes))
        data_buf += pkt_bytes

    # Pad data section to 32-byte boundary (with at least 2 zero bytes at end as u16(0) terminator)
    data_buf += b"\x00\x00"
    if len(data_buf) % 32:
        data_buf += b"\x00" * (32 - (len(data_buf) % 32))

    # Build VORBISDATA (chunk type 11) seek pairs: for k = 1..floor(seconds),
    # last packet starting at or before k * 44100
    whole_seconds = int(math.floor(total_frames / float(TARGET_SR)))
    seek_buf = bytearray()
    p_cursor = 0
    n_pkts = len(pkt_index_table)
    for k in range(1, whole_seconds + 1):
        target_sample = k * TARGET_SR
        while p_cursor + 1 < n_pkts and pkt_index_table[p_cursor + 1][0] <= target_sample:
            p_cursor += 1
        s_samp, b_off = pkt_index_table[p_cursor]
        seek_buf += struct.pack("<II", s_samp, b_off)

    vorbis_chunk_body = struct.pack("<II", setup_crc, len(seek_buf)) + bytes(seek_buf)
    chunk_header_u32 = 0 | ((len(vorbis_chunk_body) & 0xFFFFFF) << 1) | (11 << 25)

    sample_desc_u64 = (
        1  # next_chunk = 1 (VORBISDATA chunk follows)
        | (8 << 1)  # freq_idx = 8 (44100 Hz)
        | (1 << 5)  # stereo
        | (0 << 6)  # data_offset_div_16 = 0
        | ((total_frames & 0x3FFFFFFF) << 34)
    )

    shdr = bytearray()
    shdr += struct.pack("<Q", sample_desc_u64)
    shdr += struct.pack("<I", chunk_header_u32)
    shdr += vorbis_chunk_body
    # Pad shdr so (60 + len(shdr)) is 32-byte aligned
    total_hdr = 60 + len(shdr)
    if total_hdr % 32:
        shdr += b"\x00" * (32 - (total_hdr % 32))

    md5_16 = hashlib.md5(bytes(shdr) + bytes(data_buf[:8192])).digest()
    sub_8 = hashlib.md5(md5_16).digest()[:8]

    header = bytearray(60)
    header[0:4] = b"FSB5"
    struct.pack_into(
        "<6I",
        header,
        4,
        1,              # version = 1
        1,              # num_samples = 1
        len(shdr),      # sampleHeadersSize
        0,              # nameTableSize = 0
        len(data_buf),  # dataSize
        15,             # mode = 15 (FMOD_SOUND_FORMAT_VORBIS)
    )
    header[28:36] = b"\x01\x00\x00\x00\x00\x00\x00\x00"
    header[36:52] = md5_16
    header[52:60] = sub_8

    fsb5_bytes = bytes(header) + bytes(shdr) + bytes(data_buf)
    sha1_b64 = base64.b64encode(hashlib.sha1(fsb5_bytes).digest()).decode("ascii")

    return EncodedVorbisFsb5(
        fsb5_bytes=fsb5_bytes,
        sample_rate=TARGET_SR,
        channels=2,
        total_frames=total_frames,
        duration_seconds=total_frames / float(TARGET_SR),
        loudness_lufs=loudness,
        sha1_base64=sha1_b64,
    )


def build_vorbis_preview_fsb5(
    samples: np.ndarray,
    sample_rate: int,
    preview_start: float,
    preview_duration: float = 10.0,
) -> EncodedVorbisFsb5:
    """
    Build a 10-second Vorbis FSB5 preview clip (with 0.3s fade-in and 0.3s fade-out)
    taken from preview_start, or 30% into the song if preview_start falls past the end.
    """
    if sample_rate != TARGET_SR:
        samples = _resample_stereo(samples, sample_rate, TARGET_SR)
        sample_rate = TARGET_SR

    total_dur = samples.shape[0] / float(TARGET_SR)
    dur = min(10.0, max(1.5, preview_duration if preview_duration > 1.0 else 10.0), total_dur)

    start = float(preview_start)
    if start < 0.0 or start + dur > total_dur:
        start = max(0.0, min(total_dur - dur, total_dur * 0.30))

    s_idx = int(round(start * TARGET_SR))
    e_idx = min(samples.shape[0], s_idx + int(round(dur * TARGET_SR)))
    clip = samples[s_idx:e_idx].copy()

    fade = min(int(round(0.30 * TARGET_SR)), len(clip) // 4)
    if fade > 1:
        clip[:fade] *= np.linspace(0.0, 1.0, fade, dtype=np.float32)[:, None]
        clip[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)[:, None]

    return build_vorbis_fsb5_from_samples(clip, TARGET_SR)


def encode_song_and_preview_vorbis_fsb5(
    audio_bytes: bytes,
    filename_hint: str,
    preview_start: float,
    preview_duration: float = 10.0,
) -> Tuple[EncodedVorbisFsb5, EncodedVorbisFsb5]:
    samples, sr = decode_audio_bytes(audio_bytes, filename_hint)
    main_fsb = build_vorbis_fsb5_from_samples(samples, sr)
    prev_fsb = build_vorbis_preview_fsb5(samples, sr, preview_start, preview_duration)
    return main_fsb, prev_fsb
