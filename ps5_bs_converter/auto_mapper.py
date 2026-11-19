"""
Offline DSP Audio-to-Beatmap Generator ("Make Your Own Track").

Turns any raw audio file (.mp3, .wav, .ogg, .flac, .egg) into a complete, playable
5-difficulty (Easy..Expert+) Beat Saber CustomSongPackage with:
  - Multi-band STFT spectral-flux onset detection (Kick / Mid / Hi-Hat bands)
  - Autocorrelation BPM estimation + exact beat-phase alignment
  - Automatic chorus / high-energy preview window selection
  - Wrist-parity state machine (zero double-downs / triangle resets)
  - Musical V3.3.0 mechanics: Chords on bass drops, Arcs on sustained melodies,
    Burst Chains on sharp accents, safe side-walls, and beat-synced Stage Lightshows
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import zipfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .audio_fsb5 import decode_audio_bytes
from .beatmap_converter import (
    DEFAULT_NJS_BY_DIFF,
    DIFF_INT_TO_RANK,
    DIFF_NAMES,
    CustomSongPackage,
    ParsedDifficultyChart,
    _empty_v330_skeleton,
    _sort_v3_arrays,
)

STYLE_PRESETS = ("Balanced Flow", "Dance / Party", "Tech / High-Intensity", "Joyful / Chilled")

# Forehand (downward) vs Backhand (upward) cut directions:
# 0=Up, 1=Down, 2=Left, 3=Right, 4=UpLeft, 5=UpRight, 6=DownLeft, 7=DownRight, 8=Dot
FOREHAND_DIRS_LEFT = [1, 6, 7, 2]
BACKHAND_DIRS_LEFT = [0, 4, 5, 3]
FOREHAND_DIRS_RIGHT = [1, 7, 6, 3]
BACKHAND_DIRS_RIGHT = [0, 5, 4, 2]


@dataclass
class AudioBeatAnalysis:
    bpm: float
    beat_offset_seconds: float
    duration_seconds: float
    preview_start_seconds: float
    preview_duration_seconds: float
    # Per-subdivision (1/4 beat = 16th note) feature arrays
    grid_beats: np.ndarray        # beat numbers [0.0, 0.25, 0.5, ...]
    onset_total: np.ndarray       # normalized [0..1] onset strength at each 1/4 beat
    onset_low: np.ndarray         # normalized [0..1] kick/bass onset strength
    onset_high: np.ndarray        # normalized [0..1] snare/hat onset strength
    section_energy: np.ndarray    # rolling 4-bar normalized RMS energy [0..1]


def analyze_audio_rhythm(
    samples: np.ndarray,
    sample_rate: int,
    bpm_override: Optional[float] = None,
) -> AudioBeatAnalysis:
    """
    Analyze stereo float32 samples to extract BPM, exact beat phase, chorus timestamp,
    and beat-quantized multi-band onset & energy curves.
    """
    mono = np.mean(samples, axis=1, dtype=np.float32)
    duration = float(len(mono)) / float(sample_rate)

    # Downsample to ~22050 Hz for fast, crisp STFT analysis
    step = 2 if sample_rate >= 40000 else 1
    sig = mono[::step]
    sr = sample_rate / float(step)

    n_fft = 1024
    hop = 256  # ~11.6 ms frame resolution at 22.05 kHz
    if len(sig) < n_fft + hop:
        sig = np.pad(sig, (0, n_fft + hop - len(sig)))

    n_frames = 1 + (len(sig) - n_fft) // hop
    window = np.hanning(n_fft).astype(np.float32)

    # Vectorized strided framing
    frames = np.lib.stride_tricks.as_strided(
        sig,
        shape=(n_frames, n_fft),
        strides=(sig.strides[0] * hop, sig.strides[0]),
    )
    spec = np.abs(np.fft.rfft(frames * window, axis=1)).astype(np.float32)
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sr)

    low_mask = (freqs >= 30.0) & (freqs < 170.0)
    mid_mask = (freqs >= 170.0) & (freqs < 2400.0)
    high_mask = (freqs >= 2400.0) & (freqs <= 8500.0)

    log_spec = np.log1p(10.0 * spec)
    diff = np.maximum(0.0, np.diff(log_spec, axis=0, prepend=log_spec[:1]))

    flux_low = np.mean(diff[:, low_mask], axis=1) if np.any(low_mask) else np.zeros(n_frames, dtype=np.float32)
    flux_mid = np.mean(diff[:, mid_mask], axis=1) if np.any(mid_mask) else np.zeros(n_frames, dtype=np.float32)
    flux_high = np.mean(diff[:, high_mask], axis=1) if np.any(high_mask) else np.zeros(n_frames, dtype=np.float32)
    flux_total = 0.45 * flux_low + 0.35 * flux_mid + 0.20 * flux_high

    frame_rms = np.sqrt(np.mean(np.square(frames), axis=1) + 1e-12)
    fps = sr / float(hop)

    # 1. Estimate BPM via onset autocorrelation across 78..182 BPM (unless overridden)
    if bpm_override and 40.0 <= bpm_override <= 300.0:
        bpm = float(bpm_override)
    else:
        bpm = _estimate_bpm_from_flux(flux_total, fps)

    # 2. Find exact beat phase (beat_offset_seconds in [0, beat_period))
    beat_period = 60.0 / bpm
    beat_offset = _find_best_beat_phase(flux_low * 0.6 + flux_total * 0.4, fps, beat_period)

    # 3. Find best 10s Chorus / Drop preview start in [15%..75%] of track
    prev_dur = min(10.0, max(2.0, duration * 0.4))
    win_frames = max(1, int(round(prev_dur * fps)))
    if n_frames > win_frames * 2:
        s_idx = int(n_frames * 0.15)
        e_idx = max(s_idx + 1, int(n_frames * 0.75) - win_frames)
        csum = np.cumsum(np.insert(frame_rms, 0, 0.0))
        window_sums = csum[s_idx + win_frames : e_idx + win_frames] - csum[s_idx:e_idx]
        best_frame = s_idx + int(np.argmax(window_sums)) if len(window_sums) > 0 else s_idx
        prev_start = round(best_frame / fps, 2)
    else:
        prev_start = 0.0

    # 4. Sample multi-band onsets & rolling energy onto the exact 1/4-beat (16th-note) grid
    usable_dur = max(0.5, duration - beat_offset - 0.25)
    total_beats = usable_dur * (bpm / 60.0)
    num_steps = max(4, int(math.floor(total_beats * 4.0)))
    grid_steps = np.arange(num_steps, dtype=np.float64) * 0.25
    grid_times = beat_offset + grid_steps * beat_period

    # ABSOLUTE-BEAT ANCHORING (fixes auto-made maps arriving early):
    # grid beat b maps to song time beat_offset + b*beat_period, i.e. the
    # beat grid's true phase. Notes written at these beats land exactly on
    # the detected beats of the audio with NO reliance on `_songTimeOffset`
    # (whose sign convention previously shifted notes early by up to
    # 2x the beat phase). `song_time_offset` is now emitted as 0.
    grid_beats = grid_times * (bpm / 60.0)

    half_win = max(1, int(round(0.045 * fps)))  # ±45ms transient capture window

    def _sample_curve_max(curve: np.ndarray) -> np.ndarray:
        out = np.zeros(num_steps, dtype=np.float32)
        for i, t in enumerate(grid_times):
            center = int(round(t * fps))
            lo = max(0, center - half_win)
            hi = min(len(curve), center + half_win + 1)
            if hi > lo:
                out[i] = float(np.max(curve[lo:hi]))
        p95 = float(np.percentile(out, 96)) if len(out) > 0 else 1.0
        if p95 > 1e-6:
            out = np.clip(out / p95, 0.0, 1.0)
        return out

    onset_tot_g = _sample_curve_max(flux_total)
    onset_low_g = _sample_curve_max(flux_low)
    onset_hi_g = _sample_curve_max(flux_high)

    # Rolling 16-beat (4-bar) RMS energy contour
    raw_energy = np.interp(grid_times, np.arange(n_frames) / fps, frame_rms).astype(np.float32)
    smooth_k = max(4, min(len(raw_energy), 64))  # 64 quarter-beats = 16 beats = 4 bars
    kernel = np.ones(smooth_k, dtype=np.float32) / float(smooth_k)
    sec_energy = np.convolve(raw_energy, kernel, mode="same")
    e_min = float(np.percentile(sec_energy, 10))
    e_max = float(np.percentile(sec_energy, 95))
    if e_max - e_min > 1e-6:
        sec_energy = np.clip((sec_energy - e_min) / (e_max - e_min), 0.0, 1.0)
    else:
        sec_energy = np.full_like(sec_energy, 0.6)

    return AudioBeatAnalysis(
        bpm=round(bpm, 2),
        beat_offset_seconds=round(beat_offset, 4),
        duration_seconds=duration,
        preview_start_seconds=prev_start,
        preview_duration_seconds=round(prev_dur, 2),
        grid_beats=grid_beats,
        onset_total=onset_tot_g,
        onset_low=onset_low_g,
        onset_high=onset_hi_g,
        section_energy=sec_energy,
    )


def _estimate_bpm_from_flux(flux: np.ndarray, fps: float) -> float:
    env = flux - float(np.mean(flux))
    norm = float(np.linalg.norm(env))
    if norm < 1e-8:
        return 120.0
    env = env / norm

    # FFT-based autocorrelation
    n = len(env)
    pad_n = 1 << (2 * n - 1).bit_length()
    ac = np.fft.irfft(np.abs(np.fft.rfft(env, n=pad_n)) ** 2)[:n]

    best_bpm = 120.0
    best_score = -1.0
    # Scan candidate BPMs from 84 to 176 in 0.25 BPM increments with perceptual prior around 128 BPM
    for cand_bpm in np.arange(84.0, 176.25, 0.25):
        lag = (60.0 / cand_bpm) * fps
        idx1 = int(round(lag))
        idx2 = int(round(lag * 2.0))
        idx_half = int(round(lag * 0.5))
        if idx1 <= 1 or idx1 >= n:
            continue
        val = float(ac[idx1])
        if idx2 < n:
            val += 0.45 * float(ac[idx2])
        if idx_half > 1 and idx_half < n:
            val += 0.25 * float(ac[idx_half])
        # Gentle Gaussian prior favoring natural human Beat Saber tempo range (115..148 BPM)
        prior = math.exp(-0.5 * ((cand_bpm - 130.0) / 38.0) ** 2)
        score = val * (0.75 + 0.25 * prior)
        if score > best_score:
            best_score = score
            best_bpm = float(cand_bpm)

    # Snap to clean integer BPM if within 0.35 BPM (most studio tracks use exact integer BPM!)
    if abs(best_bpm - round(best_bpm)) <= 0.35:
        best_bpm = float(round(best_bpm))
    return best_bpm


def _find_best_beat_phase(flux: np.ndarray, fps: float, beat_period: float) -> float:
    best_off = 0.0
    best_sum = -1.0
    max_frame = len(flux)
    for step_ms in range(0, int(round(beat_period * 1000.0)), 8):
        off_s = step_ms / 1000.0
        indices = np.round((off_s + np.arange(0, 240) * beat_period) * fps).astype(np.int64)
        indices = indices[(indices >= 0) & (indices < max_frame)]
        if len(indices) == 0:
            continue
        score = float(np.sum(flux[indices]))
        if score > best_sum:
            best_sum = score
            best_off = off_s
    return best_off


# ==============================================================================
# Flow-Aware Procedural Choreography & Lightshow Builder
# ==============================================================================

def _build_lightshow(analysis: AudioBeatAnalysis) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Generate dynamic V3 basicBeatmapEvents + colorBoostBeatmapEvents synced to the music."""
    basic_events: List[Dict[str, Any]] = []
    boost_events: List[Dict[str, Any]] = []

    in_boost = False
    for idx, b in enumerate(analysis.grid_beats):
        sub = idx % 4  # 0 = downbeat, 2 = eighth-note offbeat
        energy = float(analysis.section_energy[idx])
        kick = float(analysis.onset_low[idx])
        hat = float(analysis.onset_high[idx])

        # Flip stage color boost ON during high-energy chorus/drop sections (every 16-beat phrase)
        if idx % 64 == 0:
            want_boost = energy >= 0.68
            if want_boost != in_boost:
                in_boost = want_boost
                boost_events.append({"b": round(float(b), 6), "o": in_boost})

        # Ring rotation on 8-beat / 16-beat boundaries
        if idx % 32 == 0 and b >= 4.0:
            basic_events.append({"b": round(float(b), 6), "et": 8, "i": 0, "f": 1.0})
        if idx % 64 == 0 and b >= 4.0:
            basic_events.append({"b": round(float(b), 6), "et": 9, "i": 0, "f": 1.0})

        # Back lasers (et=0) & Track ring neons (et=1) on strong downbeats/kicks
        if sub == 0 and (kick >= 0.35 or energy >= 0.45):
            color_val = 3 if (int(b // 4) % 2 == 0) else 7  # flash+fade blue / red alternating bars
            basic_events.append({"b": round(float(b), 6), "et": 0, "i": color_val, "f": min(1.0, 0.5 + energy * 0.5)})
            basic_events.append({"b": round(float(b), 6), "et": 4, "i": 7 if color_val == 3 else 3, "f": 0.9})

        # Left/Right rotating lasers (et=2, et=3) + speed (et=12, et=13) on offbeats/hats in energetic sections
        if sub == 2 and energy >= 0.40 and hat >= 0.32:
            c_val = 6 if (idx % 8 == 2) else 2
            basic_events.append({"b": round(float(b), 6), "et": 2, "i": c_val, "f": 0.85})
            basic_events.append({"b": round(float(b), 6), "et": 3, "i": c_val, "f": 0.85})
            speed = min(7, max(1, int(round(energy * 6.0))))
            basic_events.append({"b": round(float(b), 6), "et": 12, "i": speed, "f": 1.0})
            basic_events.append({"b": round(float(b), 6), "et": 13, "i": speed, "f": 1.0})

    return basic_events, boost_events


def _generate_difficulty_v3(
    analysis: AudioBeatAnalysis,
    diff_int: int,
    style: str,
    basic_events: List[Dict[str, Any]],
    boost_events: List[Dict[str, Any]],
) -> ParsedDifficultyChart:
    """
    Build a single difficulty chart (0=Easy..4=ExpertPlus) directly from AudioBeatAnalysis
    with strict forehand/backhand wrist parity.
    """
    v3 = _empty_v330_skeleton()
    v3["basicBeatmapEvents"] = list(basic_events)
    v3["colorBoostBeatmapEvents"] = list(boost_events)
    v3["useNormalEventsAsCompatibleEvents"] = True

    style_mult = (
        1.18
        if style == "Tech / High-Intensity"
        else 0.95
        if style == "Dance / Party"
        else 0.82
        if style == "Joyful / Chilled"
        else 1.0
    )
    wide_dance = style == "Dance / Party"
    joyful = style == "Joyful / Chilled"

    # Target max Notes-Per-Second & allowed grid subdivisions per difficulty
    base_nps = {0: 1.25, 1: 2.15, 2: 3.35, 3: 4.85, 4: 6.60}[diff_int] * style_mult
    bps = max(0.5, analysis.bpm / 60.0)
    min_gap_beats = max(0.25, (bps / base_nps) * 0.78)

    # Allowed 1/4-beat grid indices (sub = idx % 4):
    # Easy: whole/half beats (0, 2); Normal/Hard: whole/half + strong quarters; Expert/E+: full 1/4 grid
    onset_thresh = {0: 0.42, 1: 0.34, 2: 0.27, 3: 0.21, 4: 0.16}[diff_int]

    # Hand parity state: True = next swing is Forehand (Down), False = next swing is Backhand (Up)
    forehand_next = {0: True, 1: True}
    last_beat_hand = {0: -999.0, 1: -999.0}
    last_any_beat = -999.0
    preferred_hand = 1  # Start right-handed
    rng = np.random.default_rng(seed=1337 + diff_int * 101 + int(round(analysis.bpm * 10)))

    notes: List[Dict[str, Any]] = []
    bombs: List[Dict[str, Any]] = []
    obstacles: List[Dict[str, Any]] = []
    sliders: List[Dict[str, Any]] = []
    burst_sliders: List[Dict[str, Any]] = []

    max_song_beat = float(analysis.grid_beats[-1]) if len(analysis.grid_beats) > 0 else 0.0

    for idx, b_val in enumerate(analysis.grid_beats):
        b = float(b_val)
        # Leave 2 seconds of breathing room at the very start & 1 second at the very end
        if b < max(2.0, bps * 1.8) or b > max_song_beat - max(1.5, bps * 1.0):
            continue

        sub = idx % 4
        if diff_int == 0 and sub not in (0, 2):
            continue
        if diff_int == 1 and sub in (1, 3) and float(analysis.section_energy[idx]) < 0.72:
            continue

        energy = float(analysis.section_energy[idx])
        strength = float(analysis.onset_total[idx])
        kick = float(analysis.onset_low[idx])

        # Dynamic threshold adapts to quiet verses vs loud drops so quiet sections still have gentle notes
        eff_thresh = onset_thresh * (1.12 - 0.32 * energy)
        is_downbeat = sub == 0
        if is_downbeat and energy >= 0.35:
            eff_thresh *= 0.78

        local_min_gap = min_gap_beats * (0.72 if energy >= 0.75 else (1.25 if energy < 0.30 else 1.0))
        if diff_int <= 1:
            local_min_gap = max(local_min_gap, 1.0 if diff_int == 0 else 0.5)
        elif diff_int == 2:
            local_min_gap = max(local_min_gap, 0.5 if energy < 0.65 else 0.25)

        if b - last_any_beat < local_min_gap - 1e-4:
            continue
        if strength < eff_thresh and not (is_downbeat and (b - last_any_beat) >= 4.0 and energy >= 0.25):
            continue

        # Decide single note vs 2-hand chord (on strong downbeat kicks in Normal+)
        make_chord = (
            diff_int >= 1
            and is_downbeat
            and kick >= 0.58
            and energy >= 0.50
            and (b - last_beat_hand[0]) >= 0.75
            and (b - last_beat_hand[1]) >= 0.75
        )

        if make_chord:
            hands_to_place = [0, 1]
        else:
            # Alternate hands smoothly on streams, occasionally double-tapping a hand on slow phrases
            other = 1 - preferred_hand
            if (b - last_beat_hand[preferred_hand]) < 0.45 and (b - last_beat_hand[other]) >= 0.24:
                chosen_h = other
            else:
                chosen_h = preferred_hand
            hands_to_place = [chosen_h]
            preferred_hand = 1 - chosen_h

        placed_this_beat: List[Dict[str, Any]] = []
        for h in hands_to_place:
            is_fh = forehand_next[h]
            # Pick lane x (Left hand -> 0,1; Right hand -> 2,3; wide outer lanes on Dance style or strong accents)
            if h == 0:
                x = 0 if (wide_dance or kick >= 0.65 or rng.random() < 0.42) else 1
                dir_pool = FOREHAND_DIRS_LEFT if is_fh else BACKHAND_DIRS_LEFT
            else:
                x = 3 if (wide_dance or kick >= 0.65 or rng.random() < 0.42) else 2
                dir_pool = FOREHAND_DIRS_RIGHT if is_fh else BACKHAND_DIRS_RIGHT

            # Pick row y (0=bottom, 1=middle, 2=top): forehands feel great on y=0..1, backhands on y=1..2
            if diff_int == 0:
                y = 0 if is_fh else 1
                d = dir_pool[0]  # Clean vertical Up/Down on Easy
            elif diff_int == 1:
                y = 0 if is_fh else (1 if rng.random() < 0.7 else 2)
                d = dir_pool[0] if rng.random() < 0.72 else dir_pool[1]
            else:
                if is_fh:
                    y = 0 if rng.random() < 0.68 else 1
                else:
                    y = 2 if (energy >= 0.60 and rng.random() < 0.40) else (1 if rng.random() < 0.65 else 0)
                # Use diagonals & horizontals more on higher difficulties / Dance style
                diag_prob = (
                    0.30 if joyful
                    else (0.55 if (wide_dance or style == "Tech / High-Intensity") else 0.35)
                )
                if len(hands_to_place) == 2:
                    # Keep simultaneous chords parallel/symmetric for comfort!
                    d = 1 if is_fh else 0
                    y = 0 if is_fh else 1
                elif rng.random() < diag_prob:
                    d = int(dir_pool[int(rng.integers(1, len(dir_pool)))])
                else:
                    d = dir_pool[0]

            note_obj = {"b": round(b, 6), "x": int(x), "y": int(y), "c": int(h), "d": int(d), "a": 0}
            notes.append(note_obj)
            placed_this_beat.append(note_obj)
            forehand_next[h] = not is_fh
            last_beat_hand[h] = b

        last_any_beat = b

        # Optional V3 Burst Chain on sharp high-energy single-note accents (Hard+)
        if (
            diff_int >= 2
            and len(placed_this_beat) == 1
            and is_downbeat
            and energy >= 0.72
            and kick >= 0.75
            and rng.random() < (0.18 if style != "Tech / High-Intensity" else 0.30)
        ):
            head = placed_this_beat[0]
            tx = max(0, min(3, head["x"] + (-1 if head["c"] == 0 else 1)))
            ty = max(0, min(2, head["y"] + (0 if head["d"] in (2, 3) else (-1 if head["d"] == 1 else 1))))
            if (tx, ty) != (head["x"], head["y"]):
                burst_sliders.append(
                    {
                        "b": head["b"],
                        "c": head["c"],
                        "x": head["x"],
                        "y": head["y"],
                        "d": head["d"],
                        "tb": round(b + 0.125, 6),
                        "tx": tx,
                        "ty": ty,
                        "sc": 4 if diff_int <= 3 else 5,
                        "s": 0.85,
                    }
                )

        # Optional Side Dodge Walls on 8-beat phrase starts in Hard+ (or Dance mode Normal+)
        min_wall_diff = 1 if wide_dance else 2
        wall_prob = 0.20 if joyful else 0.35
        if diff_int >= min_wall_diff and idx % 32 == 0 and energy >= 0.55 and rng.random() < wall_prob:
            wall_x = 0 if (int(b // 8) % 2 == 0) else 3
            # Ensure we didn't just place a note inside wall_x!
            if all(n["x"] != wall_x for n in placed_this_beat):
                obstacles.append(
                    {"b": round(b, 6), "x": wall_x, "y": 0, "d": 2.0, "w": 1, "h": 5}
                )

        # Optional Bomb accents in Hard+ on off-lanes during medium/high energy
        bomb_prob = 0.05 if joyful else 0.12
        if diff_int >= 2 and len(placed_this_beat) == 1 and sub == 0 and energy >= 0.60 and rng.random() < bomb_prob:
            n0 = placed_this_beat[0]
            bx = 3 - n0["x"]
            by = 0 if n0["y"] > 0 else 2
            bombs.append({"b": round(b, 6), "x": bx, "y": by})

    # Connect sustained melodic gaps (1.5..4.0 beats between same-color notes) with smooth V3 Arcs (Normal+)
    if diff_int >= 1 and len(notes) >= 4:
        by_hand: Dict[int, List[Dict[str, Any]]] = {0: [], 1: []}
        for n in notes:
            by_hand[n["c"]].append(n)
        for h in (0, 1):
            hnotes = by_hand[h]
            for i in range(len(hnotes) - 1):
                a_n = hnotes[i]
                b_n = hnotes[i + 1]
                gap = b_n["b"] - a_n["b"]
                if 1.75 <= gap <= 4.0 and (i % 5 == 0):
                    sliders.append(
                        {
                            "b": a_n["b"],
                            "c": h,
                            "x": a_n["x"],
                            "y": a_n["y"],
                            "d": a_n["d"],
                            "mu": 1.0,
                            "tb": b_n["b"],
                            "tx": b_n["x"],
                            "ty": b_n["y"],
                            "tc": b_n["d"],
                            "tmu": 1.0,
                            "m": 0,
                        }
                    )

    # Guarantee at least a basic chart even on near-silent test tones
    if len(notes) == 0:
        for i, b in enumerate(np.arange(2.0, max(6.0, max_song_beat - 1.0), 2.0)):
            h = i % 2
            d = 1 if (i // 2) % 2 == 0 else 0
            notes.append({"b": round(float(b), 6), "x": 1 if h == 0 else 2, "y": 0 if d == 1 else 1, "c": h, "d": d, "a": 0})

    v3["colorNotes"] = notes
    v3["bombNotes"] = bombs
    v3["obstacles"] = obstacles
    v3["sliders"] = sliders
    v3["burstSliders"] = burst_sliders
    _sort_v3_arrays(v3)

    njs = DEFAULT_NJS_BY_DIFF[diff_int] + (1.5 if style == "Tech / High-Intensity" else 0.0)
    return ParsedDifficultyChart(
        characteristic="Standard",
        difficulty=diff_int,
        njs=float(njs),
        njs_offset=0.0,
        v3_dict=v3,
        is_generated=False,
    )


def parse_title_artist_from_filename(filepath: str) -> Tuple[str, str]:
    """Infer 'Artist - Song Title' or 'Song Title' cleanly from an audio filename."""
    stem = os.path.splitext(os.path.basename(filepath))[0]
    stem = re.sub(r"[_]+", " ", stem).strip()
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return title.strip() or stem, artist.strip() or "Custom Track"
    return stem or "My Custom Track", "Custom Track"


def generate_track_from_audio_file(
    audio_path_or_bytes: str | bytes,
    filename_hint: str = "song.ogg",
    song_name: Optional[str] = None,
    song_author: Optional[str] = None,
    mapper_name: str = "AutoMapper Studio",
    bpm_override: Optional[float] = None,
    style: str = "Balanced Flow",
    environment_name: str = "DefaultEnvironment",
) -> Tuple[CustomSongPackage, AudioBeatAnalysis]:
    """
    Create a complete 5-difficulty CustomSongPackage directly from any audio file or bytes.
    """
    if isinstance(audio_path_or_bytes, str):
        hint = os.path.basename(audio_path_or_bytes)
        with open(audio_path_or_bytes, "rb") as f:
            raw_audio = f.read()
        auto_title, auto_artist = parse_title_artist_from_filename(audio_path_or_bytes)
    else:
        hint = filename_hint
        raw_audio = audio_path_or_bytes
        auto_title, auto_artist = parse_title_artist_from_filename(filename_hint)

    samples, sr = decode_audio_bytes(raw_audio, hint)
    analysis = analyze_audio_rhythm(samples, sr, bpm_override=bpm_override)
    basic_ev, boost_ev = _build_lightshow(analysis)

    std_charts: Dict[int, ParsedDifficultyChart] = {}
    for d in range(5):
        std_charts[d] = _generate_difficulty_v3(analysis, d, style, basic_ev, boost_ev)

    pkg = CustomSongPackage(
        song_name=(song_name or auto_title).strip() or "My Custom Track",
        song_sub_name="",
        song_author_name=(song_author or auto_artist).strip() or "Custom Artist",
        level_author_name=mapper_name.strip() or "AutoMapper Studio",
        bpm=analysis.bpm,
        song_time_offset=0.0,  # absolute-beat anchoring; no time offset needed
        shuffle=0.0,
        shuffle_period=0.5,
        preview_start_time=analysis.preview_start_seconds,
        preview_duration=analysis.preview_duration_seconds,
        audio_filename=hint,
        audio_bytes=raw_audio,
        charts={"Standard": std_charts},
        source_label=f"Auto-Generated ({style})",
        environment_name=environment_name,
    )
    return pkg, analysis


def export_package_to_beatsaver_zip(pkg: CustomSongPackage, out_zip_path: str) -> str:
    """Save a generated (or loaded) CustomSongPackage as a standard BeatSaver .zip archive."""
    ext = os.path.splitext(pkg.audio_filename)[1].lower() or ".ogg"
    song_fn = f"song{ext}"
    diff_entries = []
    std = pkg.charts.get("Standard") or {}

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(song_fn, pkg.audio_bytes)
        for d in sorted(std.keys()):
            dname = DIFF_NAMES[d]
            bfn = f"{dname}Standard.dat"
            diff_entries.append(
                {
                    "_difficulty": dname,
                    "_difficultyRank": DIFF_INT_TO_RANK[d],
                    "_beatmapFilename": bfn,
                    "_noteJumpMovementSpeed": std[d].njs,
                    "_noteJumpStartBeatOffset": std[d].njs_offset,
                }
            )
            zf.writestr(bfn, json.dumps(std[d].v3_dict, separators=(",", ":")))

        info = {
            "_version": "2.0.0",
            "_songName": pkg.song_name,
            "_songSubName": pkg.song_sub_name,
            "_songAuthorName": pkg.song_author_name,
            "_levelAuthorName": pkg.level_author_name,
            "_beatsPerMinute": pkg.bpm,
            "_songTimeOffset": pkg.song_time_offset,
            "_shuffle": pkg.shuffle,
            "_shufflePeriod": pkg.shuffle_period,
            "_previewStartTime": pkg.preview_start_time,
            "_previewDuration": pkg.preview_duration,
            "_songFilename": song_fn,
            "_coverImageFilename": "",
            "_environmentName": getattr(pkg, "environment_name", None) or "DefaultEnvironment",
            "_allDirectionsEnvironmentName": "GlassDesertEnvironment",
            "_difficultyBeatmapSets": [
                {
                    "_beatmapCharacteristicName": "Standard",
                    "_difficultyBeatmaps": diff_entries,
                }
            ],
        }
        zf.writestr("Info.dat", json.dumps(info, indent=2))

    with open(out_zip_path, "wb") as f:
        f.write(zbuf.getvalue())
    return out_zip_path
