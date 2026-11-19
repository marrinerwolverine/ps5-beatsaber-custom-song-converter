"""
Hardware-Proven PS5 Beat Saber 1.37 (01.037.000 / Unity 2022.3.33f1) Backend.

Implements the complete 1.37 pipeline proven on PS5 hardware:
  - Step A & C: Song bundle rewriter (Standard-only slot guard, tempo flattening,
    V3 -> V4.0.0 beatmap/lightshow/audio.gz UTF-8 gzipped TextAssets, 0x243 LZ4HC repack)
  - Step B: Vorbis FSB5 audio (0x6D39BF3E setup CRC + 0-sample sync check, in vorbis_fsb5.py)
  - Step D: Byte-exact level-pack BeatmapLevelSO + Preview AudioClip CAB splicer & 0x243 writer
  - Step E: Same-length Addressables aa/catalog.json m_Crc = 0 patcher
  - Step F: Backport ampr_emu.index recorded file-size updater (same length, skipped if absent)
  - Step G: Optional PS5 Standard-4KB BC7 SongArtworks cover art paster (in covers_v137.py)
"""

from __future__ import annotations

import base64
import copy
import gzip
import json
import os
import re
import struct
import zlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import lz4.block
import UnityPy

from .beatmap_converter import DIFF_NAMES, ParsedDifficultyChart
from .vorbis_fsb5 import EncodedVorbisFsb5

CLASSIC_LIGHT_STAGES_137 = {
    "DefaultEnvironment",
    "OriginsEnvironment",
    "TriangleEnvironment",
    "NiceEnvironment",
    "BigMirrorEnvironment",
    "DragonsEnvironment",
    "KDAEnvironment",
    "MonstercatEnvironment",
    "CrabRaveEnvironment",
    "PanicEnvironment",
    "RocketEnvironment",
    "GreenDayEnvironment",
    "GreenDayGrenadeEnvironment",
    "TimbalandEnvironment",
    "FitBeatEnvironment",
    "LinkinParkEnvironment",
    "BTSEnvironment",
    "KaleidoscopeEnvironment",
    "InterscopeEnvironment",
    "SkrillexEnvironment",
    "BillieEnvironment",
    "HalloweenEnvironment",
    "GagaEnvironment",
}


# ==============================================================================
# UnityFS v8 (0x243 LZ4HC Level-12) Container Reader & Byte-Exact Writer
# ==============================================================================

@dataclass
class UnityFsNode:
    name: str
    flags: int
    data: bytes


class UnityFsBundle:
    """Reads and writes UnityFS v8 AssetBundles in the exact 0x243 layout used by PS5 1.37."""

    def __init__(
        self,
        format_version: int,
        player_version: str,
        engine_version: str,
        flags: int,
        nodes: List[UnityFsNode],
    ):
        self.format_version = format_version
        self.player_version = player_version
        self.engine_version = engine_version
        self.flags = flags
        self.nodes = nodes

    @staticmethod
    def _read_cstr(buf: bytes, pos: int) -> Tuple[str, int]:
        end = buf.index(b"\x00", pos)
        return buf[pos:end].decode("utf-8", errors="replace"), end + 1

    @classmethod
    def from_bytes(cls, buf: bytes) -> "UnityFsBundle":
        if len(buf) < 48 or not buf.startswith(b"UnityFS\x00"):
            raise ValueError("Not a valid UnityFS AssetBundle")

        _sig, pos = cls._read_cstr(buf, 0)
        fmt_ver = struct.unpack_from(">I", buf, pos)[0]
        pos += 4
        player_ver, pos = cls._read_cstr(buf, pos)
        engine_ver, pos = cls._read_cstr(buf, pos)
        _file_sz, comp_info_sz, uncomp_info_sz, flags = struct.unpack_from(">QIII", buf, pos)
        pos += 20

        if fmt_ver >= 7:
            pos = (pos + 15) & ~15

        if flags & 0x80:
            info_raw = buf[len(buf) - comp_info_sz :]
            data_start = pos
        else:
            info_raw = buf[pos : pos + comp_info_sz]
            data_start = pos + comp_info_sz

        if flags & 0x200:
            data_start = (data_start + 15) & ~15

        comp_type = flags & 0x3F
        if comp_type in (2, 3):
            info_dec = lz4.block.decompress(info_raw, uncompressed_size=uncomp_info_sz)
        elif comp_type == 0:
            info_dec = info_raw
        else:
            raise ValueError(f"Unsupported UnityFS header compression={comp_type}")

        r = 16
        blk_cnt = struct.unpack_from(">I", info_dec, r)[0]
        r += 4
        blocks = []
        for _ in range(blk_cnt):
            u_sz, c_sz, b_flg = struct.unpack_from(">IIH", info_dec, r)
            r += 10
            blocks.append((u_sz, c_sz, b_flg))

        node_cnt = struct.unpack_from(">I", info_dec, r)[0]
        r += 4
        node_defs = []
        for _ in range(node_cnt):
            n_off, n_sz, n_flg = struct.unpack_from(">QQI", info_dec, r)
            r += 20
            n_name, r = cls._read_cstr(info_dec, r)
            node_defs.append((n_name, n_off, n_sz, n_flg))

        # If blocks use LZMA (mode 1, e.g. pristine song bundles), extract nodes via UnityPy
        if any((b_flg & 0x3F) == 1 for _, _, b_flg in blocks):
            env = UnityPy.load(buf)
            bf = list(env.files.values())[0]
            nodes = []
            for nm, _noff, _nsz, nflg in node_defs:
                sub = bf.files[nm]
                raw_n = sub.save() if hasattr(sub, "objects") else bytes(sub.bytes)
                nodes.append(UnityFsNode(name=nm, flags=nflg, data=raw_n))
            return cls(fmt_ver, player_ver, engine_ver, flags, nodes)

        stream = bytearray()
        cursor = data_start
        for u_sz, c_sz, b_flg in blocks:
            chunk = buf[cursor : cursor + c_sz]
            cursor += c_sz
            b_comp = b_flg & 0x3F
            if b_comp in (2, 3):
                stream.extend(lz4.block.decompress(chunk, uncompressed_size=u_sz))
            elif b_comp == 0:
                stream.extend(chunk)
            else:
                raise ValueError(f"Unsupported UnityFS block compression={b_comp}")

        nodes = [
            UnityFsNode(name=nm, flags=nf, data=bytes(stream[noff : noff + nsz]))
            for nm, noff, nsz, nf in node_defs
        ]
        return cls(fmt_ver, player_ver, engine_ver, flags, nodes)

    def to_bytes_and_crc(self) -> Tuple[bytes, int]:
        """
        Write UnityFS v8 in exact 0x243 layout (LZ4HC level 12, 128 KiB blocks, raw when unshrunk).
        Verified to reproduce unmodified 1.37 pack bundles byte-for-byte.
        """
        stream = bytearray()
        node_records = []
        for n in self.nodes:
            node_records.append((n.name, len(stream), len(n.data), n.flags))
            stream.extend(n.data)

        dec_crc = zlib.crc32(stream) & 0xFFFFFFFF

        block_size = 0x20000  # 128 KiB
        block_entries = []
        comp_stream = bytearray()
        for off in range(0, len(stream), block_size):
            chunk = bytes(stream[off : off + block_size])
            comp = lz4.block.compress(chunk, mode="high_compression", compression=12, store_size=False)
            if len(comp) < len(chunk):
                block_entries.append((len(chunk), len(comp), 3))
                comp_stream.extend(comp)
            else:
                block_entries.append((len(chunk), len(chunk), 0))
                comp_stream.extend(chunk)

        info_buf = bytearray(16)
        info_buf += struct.pack(">I", len(block_entries))
        for u_sz, c_sz, b_flg in block_entries:
            info_buf += struct.pack(">IIH", u_sz, c_sz, b_flg)
        info_buf += struct.pack(">I", len(node_records))
        for nm, noff, nsz, nflg in node_records:
            info_buf += struct.pack(">QQI", noff, nsz, nflg) + nm.encode("utf-8") + b"\x00"

        info_comp = lz4.block.compress(
            bytes(info_buf), mode="high_compression", compression=12, store_size=False
        )

        out_flags = 0x243  # LZ4HC (3) | DirectoryInfo (0x40) | Align16 (0x200)

        out = bytearray()
        out += b"UnityFS\x00"
        out += struct.pack(">I", self.format_version)
        out += self.player_version.encode("utf-8") + b"\x00"
        out += self.engine_version.encode("utf-8") + b"\x00"
        size_pos = len(out)
        out += struct.pack(">QIII", 0, len(info_comp), len(info_buf), out_flags)
        if self.format_version >= 7:
            out += b"\x00" * ((16 - (len(out) % 16)) % 16)
        out += info_comp
        if out_flags & 0x200:
            out += b"\x00" * ((16 - (len(out) % 16)) % 16)
        out += comp_stream

        struct.pack_into(">Q", out, size_pos, len(out))
        return bytes(out), dec_crc


# ==============================================================================
# Step A & C: Tempo Flattening + V3 -> V4.0.0 Beatmap & Lightshow JSON
# ==============================================================================

def _build_tempo_flattener(bpm_events: List[Dict[str, Any]], base_bpm: float):
    """
    Build a function mapping variable-BPM beat positions into constant-base_bpm beats
    so every note/wall/arc/event occurs at the exact same timestamp in seconds.
    """
    if not bpm_events:
        return lambda b: float(b)

    segs = sorted(bpm_events, key=lambda e: float(e.get("b", 0.0)))
    # Anchor beat 0.0 at base_bpm unless overridden at beat 0
    knots: List[Tuple[float, float, float]] = []  # (start_beat, start_sec, bpm)
    cur_b = 0.0
    cur_t = 0.0
    cur_m = float(base_bpm if base_bpm > 0 else 120.0)

    for ev in segs:
        eb = max(0.0, float(ev.get("b", 0.0)))
        em = float(ev.get("m", cur_m))
        if em <= 0.0:
            continue
        if eb > cur_b:
            knots.append((cur_b, cur_t, cur_m))
            cur_t += (eb - cur_b) * (60.0 / cur_m)
            cur_b = eb
        cur_m = em
    knots.append((cur_b, cur_t, cur_m))

    scale = float(base_bpm) / 60.0

    def flatten(beat_val: float) -> float:
        b = max(0.0, float(beat_val))
        # Binary/linear scan over segments
        sb, st, sm = knots[0]
        for kb, kt, km in knots:
            if b >= kb:
                sb, st, sm = kb, kt, km
            else:
                break
        sec = st + (b - sb) * (60.0 / sm)
        return round(sec * scale, 6)

    return flatten


def _strip_zero_defaults(d: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in d.items():
        if v == 0 or v == 0.0:
            continue
        out[k] = v
    return out


def _intern_v4_row(rows: List[Dict[str, Any]], cache: Dict[str, int], row: Dict[str, Any]) -> int:
    clean = _strip_zero_defaults(row)
    key = json.dumps(clean, sort_keys=True, separators=(",", ":"))
    idx = cache.get(key)
    if idx is None:
        idx = len(rows)
        rows.append(clean)
        cache[key] = idx
    return idx


def convert_v3_to_v4_beatmap_and_lightshow(
    v3_dict: Dict[str, Any],
    base_bpm: float,
    beat_shift: float = 0.0,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, int]]:
    """
    Convert a V3.3.0 chart dict into Beat Saber 1.37's V4.0.0 beatmap JSON + V4.0.0 lightshow JSON,
    with tempo flattened to `base_bpm` and all property rows deduplicated cleanly.

    `beat_shift` optionally slides every flattened beat later (used to honor
    negative `_songTimeOffset` values from community maps, which cannot be
    expressed as a negative sample anchor).

    Returns (v4_beatmap_dict, v4_lightshow_dict, counts_dict).
    """
    flatten = _build_tempo_flattener(v3_dict.get("bpmEvents") or [], base_bpm)
    if beat_shift:
        _base_flatten = flatten

        def flatten(b):  # noqa: F811
            return round(_base_flatten(b) + beat_shift, 6)

    cn_data: List[Dict[str, Any]] = []
    cn_cache: Dict[str, int] = {}
    color_notes: List[Dict[str, Any]] = []

    for n in sorted(v3_dict.get("colorNotes") or [], key=lambda x: float(x.get("b", 0.0))):
        fb = flatten(float(n.get("b", 0.0)))
        idx = _intern_v4_row(
            cn_data,
            cn_cache,
            {
                "x": int(n.get("x", 0)),
                "y": int(n.get("y", 0)),
                "c": int(n.get("c", 0)),
                "d": int(n.get("d", 0)),
                "a": int(n.get("a", 0)),
            },
        )
        ev = _strip_zero_defaults({"b": fb, "i": idx})
        if "b" not in ev and fb == 0.0:
            ev["b"] = 0.0
        color_notes.append(ev)

    bn_data: List[Dict[str, Any]] = []
    bn_cache: Dict[str, int] = {}
    bomb_notes: List[Dict[str, Any]] = []
    for bm in sorted(v3_dict.get("bombNotes") or [], key=lambda x: float(x.get("b", 0.0))):
        fb = flatten(float(bm.get("b", 0.0)))
        idx = _intern_v4_row(bn_data, bn_cache, {"x": int(bm.get("x", 0)), "y": int(bm.get("y", 0))})
        ev = _strip_zero_defaults({"b": fb, "i": idx})
        if "b" not in ev and fb == 0.0:
            ev["b"] = 0.0
        bomb_notes.append(ev)

    ob_data: List[Dict[str, Any]] = []
    ob_cache: Dict[str, int] = {}
    obstacles: List[Dict[str, Any]] = []
    for ob in sorted(v3_dict.get("obstacles") or [], key=lambda x: float(x.get("b", 0.0))):
        b0 = float(ob.get("b", 0.0))
        b1 = b0 + max(0.01, float(ob.get("d", 0.01)))
        fb0 = flatten(b0)
        fdur = round(max(0.01, flatten(b1) - fb0), 6)
        idx = _intern_v4_row(
            ob_data,
            ob_cache,
            {
                "d": fdur,
                "x": int(ob.get("x", 0)),
                "y": int(ob.get("y", 0)),
                "w": int(ob.get("w", 1)),
                "h": int(ob.get("h", 5)),
            },
        )
        ev = _strip_zero_defaults({"b": fb0, "i": idx})
        if "b" not in ev and fb0 == 0.0:
            ev["b"] = 0.0
        obstacles.append(ev)

    arcs_data: List[Dict[str, Any]] = []
    arcs_cache: Dict[str, int] = {}
    arcs: List[Dict[str, Any]] = []
    for sl in sorted(v3_dict.get("sliders") or [], key=lambda x: float(x.get("b", 0.0))):
        hb = flatten(float(sl.get("b", 0.0)))
        tb = max(hb, flatten(float(sl.get("tb", hb))))
        c = int(sl.get("c", 0))
        hi = _intern_v4_row(
            cn_data,
            cn_cache,
            {"x": int(sl.get("x", 0)), "y": int(sl.get("y", 0)), "c": c, "d": int(sl.get("d", 0))},
        )
        ti = _intern_v4_row(
            cn_data,
            cn_cache,
            {"x": int(sl.get("tx", 0)), "y": int(sl.get("ty", 0)), "c": c, "d": int(sl.get("tc", 0))},
        )
        ai = _intern_v4_row(
            arcs_data,
            arcs_cache,
            {
                "m": round(float(sl.get("mu", 1.0)), 4),
                "tm": round(float(sl.get("tmu", 1.0)), 4),
                "a": int(sl.get("m", 0)),
            },
        )
        arcs.append(_strip_zero_defaults({"hb": hb, "hi": hi, "tb": tb, "ti": ti, "ai": ai}))

    ch_data: List[Dict[str, Any]] = []
    ch_cache: Dict[str, int] = {}
    chains: List[Dict[str, Any]] = []
    for bs in sorted(v3_dict.get("burstSliders") or [], key=lambda x: float(x.get("b", 0.0))):
        hb = flatten(float(bs.get("b", 0.0)))
        tb = max(hb, flatten(float(bs.get("tb", hb))))
        c = int(bs.get("c", 0))
        hi = _intern_v4_row(
            cn_data,
            cn_cache,
            {"x": int(bs.get("x", 0)), "y": int(bs.get("y", 0)), "c": c, "d": int(bs.get("d", 0))},
        )
        ci = _intern_v4_row(
            ch_data,
            ch_cache,
            {
                "tx": int(bs.get("tx", 0)),
                "ty": int(bs.get("ty", 0)),
                "c": max(2, int(bs.get("sc", 2))),
                "s": round(float(bs.get("s", 1.0)), 4),
            },
        )
        chains.append(_strip_zero_defaults({"hb": hb, "tb": tb, "i": hi, "ci": ci}))

    v4_beatmap = {
        "version": "4.0.0",
        "colorNotes": color_notes,
        "bombNotes": bomb_notes,
        "obstacles": obstacles,
        "chains": chains,
        "arcs": arcs,
        "colorNotesData": cn_data,
        "bombNotesData": bn_data,
        "obstaclesData": ob_data,
        "chainsData": ch_data,
        "arcsData": arcs_data,
        "spawnRotations": [],
        "spawnRotationsData": [],
    }

    # Build v4.0.0 Lightshow JSON
    be_data: List[Dict[str, Any]] = []
    be_cache: Dict[str, int] = {}
    basic_events: List[Dict[str, Any]] = []
    for ev in sorted(v3_dict.get("basicBeatmapEvents") or [], key=lambda x: float(x.get("b", 0.0))):
        fb = flatten(float(ev.get("b", 0.0)))
        idx = _intern_v4_row(
            be_data,
            be_cache,
            {
                "t": int(ev.get("et", 0)),
                "i": int(ev.get("i", 0)),
                "f": round(float(ev.get("f", 1.0)), 4),
            },
        )
        basic_events.append(_strip_zero_defaults({"b": fb, "i": idx}))

    cb_data: List[Dict[str, Any]] = []
    cb_cache: Dict[str, int] = {}
    boost_events: List[Dict[str, Any]] = []
    for cb in sorted(v3_dict.get("colorBoostBeatmapEvents") or [], key=lambda x: float(x.get("b", 0.0))):
        fb = flatten(float(cb.get("b", 0.0)))
        idx = _intern_v4_row(cb_data, cb_cache, {"b": 1 if cb.get("o") else 0})
        boost_events.append(_strip_zero_defaults({"b": fb, "i": idx}))

    v4_lightshow = {
        "version": "4.0.0",
        "waypoints": [],
        "waypointsData": [],
        "basicEvents": basic_events,
        "basicEventsData": be_data,
        "colorBoostEvents": boost_events,
        "colorBoostEventsData": cb_data,
        "eventBoxGroups": [],
        "indexFilters": [],
        "lightColorEventBoxes": [],
        "lightColorEvents": [],
        "lightRotationEventBoxes": [],
        "lightRotationEvents": [],
        "lightTranslationEventBoxes": [],
        "lightTranslationEvents": [],
        "fxEventBoxes": [],
        "floatFxEvents": [],
        "basicEventTypesWithKeywords": {"d": []},
        "useNormalEventsAsCompatibleEvents": True,
    }

    # Cuttable count in Beat Saber = colorNotes + (sliceCount - 1) per burstSlider chain
    extra_chain_cuts = sum(max(0, int(bs.get("sc", 2)) - 1) for bs in (v3_dict.get("burstSliders") or []))
    counts = {
        "notes_count": len(color_notes),
        "obstacles_count": len(obstacles),
        "bombs_count": len(bomb_notes),
        "cuttable_count": len(color_notes) + extra_chain_cuts,
    }
    return v4_beatmap, v4_lightshow, counts


# ==============================================================================
# Byte-Exact CAB Splicer (Used for Step D Level-Packs & Step A Song Bundles!)
# ==============================================================================

def splice_cab_objects_exact(
    cab_raw: bytes,
    object_replacements: Dict[int, bytes],
) -> bytes:
    """
    Splice modified serialized object blobs into a Unity SerializedFile v22 CAB
    while keeping every untouched object, TypeTree, and header byte 100% identical.

    Self-test guarantee: if `object_replacements` is empty (or maps pids to identical bytes),
    returns `cab_raw` byte-for-byte.
    """
    if not object_replacements:
        return cab_raw

    meta_size = struct.unpack_from(">I", cab_raw, 20)[0]
    _file_size, data_offset, unk64 = struct.unpack_from(">QQQ", cab_raw, 24)

    # Locate object table inside metadata region using UnityPy's exact parsed object list
    env = UnityPy.load(cab_raw)
    sf_file = list(env.files.values())[0] if hasattr(env, "files") and env.files else env.file
    objs_sorted = sorted(
        [(pid, rdr.byte_start, rdr.byte_size, rdr.type_id) for pid, rdr in sf_file.objects.items()],
        key=lambda x: x[1],
    )

    # Locate each object's 24-byte (<qQIi) record in metadata [48 : 48 + meta_size]
    meta_end = 48 + meta_size
    rec_positions: Dict[int, int] = {}
    for pid, abs_bstart, bsize, tid in objs_sorted:
        rel_bstart = abs_bstart - data_offset
        needle = struct.pack("<qQIi", pid, rel_bstart, bsize, tid)
        pos = cab_raw.find(needle, 48, meta_end)
        if pos < 0:
            raise RuntimeError(f"CAB object record not found in metadata table for pid={pid}")
        rec_positions[pid] = pos

    # Rebuild data stream preserving original inter-object padding for untouched objects,
    # and 8-byte aligning after any resized object
    new_data = bytearray()
    new_rel_starts: Dict[int, int] = {}
    new_sizes: Dict[int, int] = {}

    any_size_changed = False
    for idx, (pid, abs_bstart, bsize, _tid) in enumerate(objs_sorted):
        orig_rel = abs_bstart - data_offset
        if not any_size_changed:
            # Copy exact original padding bytes prior to this object!
            if orig_rel > len(new_data):
                new_data.extend(cab_raw[data_offset + len(new_data) : data_offset + orig_rel])
        else:
            if len(new_data) % 8:
                new_data.extend(b"\x00" * (8 - (len(new_data) % 8)))

        orig_blob = cab_raw[abs_bstart : abs_bstart + bsize]
        if pid in object_replacements:
            blob = object_replacements[pid]
            if len(blob) != len(orig_blob):
                any_size_changed = True
        else:
            blob = orig_blob

        new_rel_starts[pid] = len(new_data)
        new_sizes[pid] = len(blob)
        new_data.extend(blob)

    out = bytearray(cab_raw[:data_offset])
    for pid, _abs_bstart, _bsize, tid in objs_sorted:
        rpos = rec_positions[pid]
        struct.pack_into("<qQIi", out, rpos, pid, new_rel_starts[pid], new_sizes[pid], tid)

    total_len = data_offset + len(new_data)
    struct.pack_into(">IQQQ", out, 20, meta_size, total_len, data_offset, unk64)
    out.extend(new_data)
    return bytes(out)


def _gzip_utf8_compact(obj: Dict[str, Any]) -> bytes:
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return gzip.compress(raw, compresslevel=6, mtime=0)


# ==============================================================================
# Step A: Rewrite 1.37 Song Bundle (build_slot)
# ==============================================================================

def build_v137_song_bundle(
    pristine_song_bundle_bytes: bytes,
    main_fsb: EncodedVorbisFsb5,
    std_5_charts: Dict[int, ParsedDifficultyChart],
    base_bpm: float,
    song_time_offset: float = 0.0,
) -> Tuple[bytes, Dict[int, Dict[str, Any]]]:
    """
    Rewrite a 1.37 Standard-only song bundle with Vorbis FSB5 audio, tempo-flattened
    v4.0.0 beatmaps, v4.0.0 lightshows, and <Song>.audio.gz.

    `song_time_offset` (community maps' `_songTimeOffset`, seconds) is honored:
    positive values anchor beat 0 at sample si = offset*44100 via sparse
    `bpmData`/`lufsData` entries (matching the real game files' convention of
    omitting zero-default fields); negative values shift all notes later.

    Returns (new_song_bundle_bytes, per_diff_stats_for_pack_patch).
    """
    ub = UnityFsBundle.from_bytes(pristine_song_bundle_bytes)
    cab_node = next(n for n in ub.nodes if not n.name.endswith((".resource", ".resS")))
    res_node = next(n for n in ub.nodes if n.name.endswith(".resource"))

    env = UnityPy.load(cab_node.data)
    sf_cab = list(env.files.values())[0] if hasattr(env, "files") and env.files else env.file

    # 1. Inspect BeatmapLevelDataSO to enforce the Standard-only 5-difficulty slot guard
    level_data_tt = None
    for pid, rdr in sf_cab.objects.items():
        if rdr.type.name == "MonoBehaviour":
            tt = rdr.read_typetree()
            if "_difficultyBeatmapSets" in tt and "_audioDataAsset" in tt:
                level_data_tt = tt
                break

    if level_data_tt is None:
        raise ValueError("Missing BeatmapLevelDataSO in 1.37 song bundle")

    dsets = level_data_tt.get("_difficultyBeatmapSets") or []
    if len(dsets) != 1 or dsets[0].get("_beatmapCharacteristicSerializedName") != "Standard":
        modes_found = [s.get("_beatmapCharacteristicSerializedName", "?") for s in dsets]
        raise ValueError(
            f"1.37 slot guard: multi-mode slot ({', '.join(modes_found)}) is not supported yet; "
            "please pick one of the 208 Standard-only slots."
        )

    dentries = dsets[0].get("_difficultyBeatmaps") or []
    diffs_present = sorted(int(d.get("_difficulty", -1)) for d in dentries)
    if diffs_present != [0, 1, 2, 3, 4]:
        raise ValueError(f"1.37 slot guard: expected difficulties 0..4, got {diffs_present}")

    # Convert all 5 Standard difficulties to v4.0.0 beatmaps & lightshows
    sto = float(song_time_offset or 0.0)
    if sto > 0.0:
        anchor_sample = max(0, int(round(sto * 44100)))
        beat_shift = 0.0
    elif sto < 0.0:
        anchor_sample = 0
        beat_shift = (-sto) * (base_bpm / 60.0)
    else:
        anchor_sample = 0
        beat_shift = 0.0

    v4_beatmaps: Dict[int, Dict[str, Any]] = {}
    v4_lightshows: Dict[int, Dict[str, Any]] = {}
    diff_stats: Dict[int, Dict[str, Any]] = {}
    max_flat_beat = 0.0

    for d in range(5):
        chart = std_5_charts[d]
        v4_bm, v4_ls, counts = convert_v3_to_v4_beatmap_and_lightshow(
            chart.v3_dict, base_bpm, beat_shift=beat_shift
        )
        v4_beatmaps[d] = v4_bm
        v4_lightshows[d] = v4_ls
        diff_stats[d] = {
            "njs": float(chart.njs),
            "njs_offset": float(chart.njs_offset),
            **counts,
        }
        for cn in v4_bm["colorNotes"]:
            max_flat_beat = max(max_flat_beat, float(cn.get("b", 0.0)))

    # Map beatmap TextAsset path_ids & lightshow TextAsset path_ids (shared lightshow -> hardest difficulty!)
    beatmap_pid_to_diff: Dict[int, int] = {}
    lightshow_pid_to_max_diff: Dict[int, int] = {}
    for entry in dentries:
        d = int(entry["_difficulty"])
        bm_pid = int(entry["_beatmapAsset"]["m_PathID"])
        ls_pid = int(entry["_lightshowAsset"]["m_PathID"])
        beatmap_pid_to_diff[bm_pid] = d
        if ls_pid not in lightshow_pid_to_max_diff or d > lightshow_pid_to_max_diff[ls_pid]:
            lightshow_pid_to_max_diff[ls_pid] = d

    audio_data_pid = int(level_data_tt["_audioDataAsset"]["m_PathID"])
    audio_clip_pid = int(level_data_tt["_audioClip"]["m_PathID"])

    total_beats = max(main_fsb.duration_seconds * (base_bpm / 60.0), max_flat_beat + 2.0)
    # Sparse audio.gz matching the real 1.37 game files: zero-default fields
    # (`si`/`sb`) are omitted entirely; a nonzero `_songTimeOffset` anchors
    # beat 0 at sample `anchor_sample` via `si`.
    if anchor_sample:
        bpm_entry = {"si": anchor_sample, "ei": main_fsb.total_frames, "eb": round(total_beats, 4)}
        lufs_entry = {"si": anchor_sample, "ei": main_fsb.total_frames, "l": round(main_fsb.loudness_lufs, 2)}
    else:
        bpm_entry = {"ei": main_fsb.total_frames, "eb": round(total_beats, 4)}
        lufs_entry = {"ei": main_fsb.total_frames, "l": round(main_fsb.loudness_lufs, 2)}
    audio_gz_dict = {
        "version": "4.0.0",
        "songChecksum": main_fsb.sha1_base64,
        "songSampleCount": main_fsb.total_frames,
        "songFrequency": 44100,
        "bpmData": [bpm_entry],
        "lufsData": [lufs_entry],
    }

    replacements: Dict[int, bytes] = {}

    # Update AudioClip
    ac_rdr = sf_cab.objects[audio_clip_pid]
    ac_tt = ac_rdr.read_typetree()
    ac_tt["m_CompressionFormat"] = 1  # Vorbis
    ac_tt["m_LoadType"] = 1
    ac_tt["m_Channels"] = 2
    ac_tt["m_Frequency"] = 44100
    ac_tt["m_BitsPerSample"] = 16
    ac_tt["m_Length"] = float(main_fsb.duration_seconds)
    ac_tt["m_Resource"]["m_Offset"] = 0
    ac_tt["m_Resource"]["m_Size"] = len(main_fsb.fsb5_bytes)
    ac_rdr.save_typetree(ac_tt)
    replacements[audio_clip_pid] = ac_rdr.get_raw_data()

    # Update audio.gz TextAsset
    ad_rdr = sf_cab.objects[audio_data_pid]
    ad_tt = ad_rdr.read_typetree()
    ad_tt["m_Script"] = _gzip_utf8_compact(audio_gz_dict).decode("utf-8", "surrogateescape")
    ad_rdr.save_typetree(ad_tt)
    replacements[audio_data_pid] = ad_rdr.get_raw_data()

    # Update 5 Beatmap TextAssets
    for bm_pid, d in beatmap_pid_to_diff.items():
        rdr = sf_cab.objects[bm_pid]
        tt = rdr.read_typetree()
        tt["m_Script"] = _gzip_utf8_compact(v4_beatmaps[d]).decode("utf-8", "surrogateescape")
        rdr.save_typetree(tt)
        replacements[bm_pid] = rdr.get_raw_data()

    # Update Lightshow TextAssets
    for ls_pid, max_d in lightshow_pid_to_max_diff.items():
        rdr = sf_cab.objects[ls_pid]
        tt = rdr.read_typetree()
        tt["m_Script"] = _gzip_utf8_compact(v4_lightshows[max_d]).decode("utf-8", "surrogateescape")
        rdr.save_typetree(tt)
        replacements[ls_pid] = rdr.get_raw_data()

    cab_node.data = splice_cab_objects_exact(cab_node.data, replacements)
    res_node.data = main_fsb.fsb5_bytes

    new_bundle_bytes, _ = ub.to_bytes_and_crc()
    return new_bundle_bytes, diff_stats


# ==============================================================================
# Step D: Byte-Exact Level-Pack Bundle Patcher (pack_patch + bundle_write)
# ==============================================================================

def patch_v137_pack_bundle(
    pristine_pack_bundle_bytes: bytes,
    slot_edits: Dict[str, Dict[str, Any]],
) -> bytes:
    """
    Apply byte-exact metadata + preview AudioClip edits to a 1.37 *_pack_assets_all_*.bundle.

    `slot_edits` maps `level_id` -> {
        "song_name", "song_sub_name", "song_author_name", "level_author_name",
        "bpm", "lufs", "duration", "environment_override", "mapper_environment",
        "preview_fsb_bytes", "preview_duration", "diff_stats"
    }.
    With `slot_edits={}`, reproduces `pristine_pack_bundle_bytes` byte-for-byte (q0 self-test).
    """
    ub = UnityFsBundle.from_bytes(pristine_pack_bundle_bytes)
    if not slot_edits:
        rebuilt, _ = ub.to_bytes_and_crc()
        return rebuilt

    cab_node = next(n for n in ub.nodes if not n.name.endswith((".resource", ".resS")))
    res_node = next(n for n in ub.nodes if n.name.endswith(".resource"))

    env = UnityPy.load(cab_node.data)
    sf_cab = list(env.files.values())[0] if hasattr(env, "files") and env.files else env.file

    res_buf = bytearray(res_node.data)
    replacements: Dict[int, bytes] = {}

    for pid, rdr in sf_cab.objects.items():
        if rdr.type.name != "MonoBehaviour":
            continue
        tt = rdr.read_typetree()
        lid = tt.get("_levelID")
        if not lid or lid not in slot_edits or "_previewDifficultyBeatmapSets" not in tt:
            continue

        # Round-trip check UnityPy's typetree writer BEFORE making any edits!
        orig_raw = cab_node.data[rdr.byte_start : rdr.byte_start + rdr.byte_size]
        rdr.save_typetree(copy.deepcopy(tt))
        if rdr.get_raw_data() != orig_raw:
            raise RuntimeError(f"TypeTree round-trip check failed on BeatmapLevelSO '{lid}'")

        ed = slot_edits[lid]
        tt["_version"] = 0
        tt["_songName"] = str(ed["song_name"])
        tt["_songSubName"] = str(ed.get("song_sub_name", ""))
        tt["_songAuthorName"] = str(ed["song_author_name"])
        tt["_levelAuthorName"] = str(ed.get("level_author_name", ""))
        tt["_beatsPerMinute"] = float(ed["bpm"])
        tt["_integratedLufs"] = float(ed["lufs"])
        tt["_songTimeOffset"] = 0.0
        tt["_songDuration"] = float(ed["duration"])
        tt["_previewStartTime"] = 0.0
        tt["_previewDuration"] = float(ed["preview_duration"])

        # Stage selection rule (handover Step D):
        # auto -> mapper's stage ONLY if it's a classic-light stage; explicit override -> user's stage
        env_ov = str(ed.get("environment_override") or "auto").strip()
        if env_ov.lower() == "auto":
            m_env = str(ed.get("mapper_environment") or "").strip()
            if m_env in CLASSIC_LIGHT_STAGES_137:
                tt["_environmentName"]["_environmentName"] = m_env
        elif env_ov.lower() not in ("keep", "slot", ""):
            tt["_environmentName"]["_environmentName"] = env_ov

        # Per-difficulty NJS, offset, and note/wall/bomb/cuttable counts
        dstats = ed["diff_stats"]
        pset0 = tt["_previewDifficultyBeatmapSets"][0]["_previewDifficultyBeatmaps"]
        for pentry in pset0:
            d = int(pentry["_difficulty"])
            if d in dstats:
                st = dstats[d]
                pentry["_noteJumpMovementSpeed"] = float(st["njs"])
                pentry["_noteJumpStartBeatOffset"] = float(st["njs_offset"])
                pentry["_notesCount"] = int(st["notes_count"])
                pentry["_obstaclesCount"] = int(st["obstacles_count"])
                pentry["_bombsCount"] = int(st["bombs_count"])
                pentry["_cuttableBeatmapObjectsCount"] = int(st["cuttable_count"])

        rdr.save_typetree(tt)
        replacements[pid] = rdr.get_raw_data()

        # Append 10s Vorbis preview clip to pack .resource & update <Song>Preview AudioClip
        prev_pptr = tt.get("_previewAudioClip") or {}
        prev_pid = int(prev_pptr.get("m_PathID", 0))
        if prev_pid != 0 and prev_pid in sf_cab.objects and ed.get("preview_fsb_bytes"):
            p_fsb: bytes = ed["preview_fsb_bytes"]
            if len(res_buf) % 32:
                res_buf.extend(b"\x00" * (32 - (len(res_buf) % 32)))
            p_off = len(res_buf)
            res_buf.extend(p_fsb)

            ac_rdr = sf_cab.objects[prev_pid]
            orig_ac = cab_node.data[ac_rdr.byte_start : ac_rdr.byte_start + ac_rdr.byte_size]
            ac_tt = ac_rdr.read_typetree()
            ac_rdr.save_typetree(copy.deepcopy(ac_tt))
            if ac_rdr.get_raw_data() != orig_ac:
                raise RuntimeError(f"TypeTree round-trip check failed on Preview AudioClip '{lid}'")

            ac_tt["m_CompressionFormat"] = 1
            ac_tt["m_LoadType"] = 1
            ac_tt["m_Channels"] = 2
            ac_tt["m_Frequency"] = 44100
            ac_tt["m_BitsPerSample"] = 16
            ac_tt["m_Length"] = float(ed["preview_duration"])
            ac_tt["m_Resource"]["m_Offset"] = p_off
            ac_tt["m_Resource"]["m_Size"] = len(p_fsb)
            ac_rdr.save_typetree(ac_tt)
            replacements[prev_pid] = ac_rdr.get_raw_data()

    cab_node.data = splice_cab_objects_exact(cab_node.data, replacements)
    res_node.data = bytes(res_buf)

    rebuilt, _ = ub.to_bytes_and_crc()
    return rebuilt


# ==============================================================================
# Step E: Same-Length Addressables aa/catalog.json m_Crc = 0 Patcher
# ==============================================================================

def set_catalog_crc_zero_same_length(
    pristine_catalog_text: str,
    bundle_markers: List[str],
) -> str:
    """
    Set `"m_Crc":0` (padded with spaces to the exact original character length!) inside
    `m_ExtraDataString` for every bundle matching `bundle_markers`.

    Preserves the EXACT byte length of `m_ExtraDataString` and `catalog.json`, so neither
    `m_EntryDataString` offsets nor `ampr_emu.index` size for `catalog.json` ever change!
    """
    if not bundle_markers:
        return pristine_catalog_text

    cat = json.loads(pristine_catalog_text)
    orig_b64 = cat["m_ExtraDataString"]
    ed = bytearray(base64.b64decode(orig_b64))
    markers_low = [m.lower() for m in bundle_markers]

    # Locate matching bundle hashes/names from m_InternalIds as well so markers like
    # "extras_pack_assets_all_8986fcaa..." match even when m_BundleName stores the hash/stem!
    internal_ids = cat.get("m_InternalIds") or []
    extra_markers = set(markers_low)
    for iid in internal_ids:
        iid_low = str(iid).lower()
        for m in markers_low:
            if m in iid_low:
                base = os.path.basename(iid_low)
                stem = base[:-7] if base.endswith(".bundle") else base
                extra_markers.add(base)
                extra_markers.add(stem)
                # Also extract trailing 32-hex bundle hash if present
                hm = re.search(r"([0-9a-f]{32})", base)
                if hm:
                    extra_markers.add(hm.group(1))

    i = 0
    n = len(ed)
    while i < n:
        if ed[i] != 7:
            i += 1
            continue
        try:
            asm_len = ed[i + 1]
            p1 = i + 2 + asm_len
            cls_len = ed[p1]
            p2 = p1 + 1 + cls_len
            js_len = struct.unpack_from("<I", ed, p2)[0]
            p_json = p2 + 4
        except Exception:
            i += 1
            continue

        if js_len <= 0 or js_len > 400000 or p_json + js_len > n:
            i += 1
            continue

        s = ed[p_json : p_json + js_len].decode("utf-16-le", errors="replace")
        s_low = s.lower()
        if any(mk in s_low for mk in extra_markers):
            def _zero_pad_crc(match: re.Match) -> str:
                full = match.group(0)
                prefix = '"m_Crc":0'
                if len(full) >= len(prefix):
                    return prefix + (" " * (len(full) - len(prefix)))
                return full

            s_new = re.sub(r'"m_Crc":\s*\d+', _zero_pad_crc, s, count=1)
            b_new = s_new.encode("utf-16-le")
            if len(b_new) != js_len:
                raise RuntimeError("Same-length catalog CRC edit changed UTF-16LE block length")
            ed[p_json : p_json + js_len] = b_new

        i = p_json + js_len

    new_b64 = base64.b64encode(bytes(ed)).decode("ascii")
    if len(new_b64) != len(orig_b64):
        raise RuntimeError("Same-length catalog base64 length changed unexpectedly")

    patched_text = pristine_catalog_text.replace(orig_b64, new_b64, 1)
    if len(patched_text) != len(pristine_catalog_text):
        raise RuntimeError("Catalog text length changed unexpectedly")
    return patched_text


# ==============================================================================
# Step F: Backport ampr_emu.index Recorded File-Size Updater
# ==============================================================================

def update_ampr_index_sizes(
    pristine_index_bytes: bytes,
    rel_path_to_size: Dict[str, int],
) -> bytes:
    """
    Update the 8-byte FILE_SIZE field inside `ampr_emu.index` (`AMPRIDX3`) for every
    modified game file without changing the index file's byte length, offsets, or 32 KB table.

    Refuses if any path suffix fails to match uniquely (exactly 1 entry).
    """
    if len(pristine_index_bytes) < 0x30 or pristine_index_bytes[:8] != b"AMPRIDX3":
        raise ValueError("Invalid ampr_emu.index magic (expected AMPRIDX3)")

    _ver, entry_size = struct.unpack_from("<II", pristine_index_bytes, 8)
    entry_count = struct.unpack_from("<Q", pristine_index_bytes, 16)[0]
    if entry_size != 24:
        raise ValueError(f"Unexpected ampr_emu.index entry_size={entry_size} (expected 24)")

    str_base = 0x30 + entry_count * 24
    buf = bytearray(pristine_index_bytes)

    # Pre-decode all entry paths once
    entries: List[Tuple[int, str]] = []
    for idx in range(entry_count):
        noff, nlen, _fsz, _mtime = struct.unpack_from("<IIQQ", buf, 0x30 + idx * 24)
        p_str = buf[str_base + noff : str_base + noff + nlen].decode("utf-8", errors="replace")
        entries.append((idx, p_str.replace("\\", "/")))

    for suffix, new_size in rel_path_to_size.items():
        norm_suf = "/" + suffix.replace("\\", "/").lstrip("/")
        matches = [idx for idx, p_str in entries if p_str.endswith(norm_suf)]
        if len(matches) != 1:
            raise KeyError(
                f"ampr_emu.index suffix '{norm_suf}' matched {len(matches)} entries (expected 1)"
            )
        target_idx = matches[0]
        struct.pack_into("<Q", buf, 0x30 + target_idx * 24 + 8, int(new_size))

    assert len(buf) == len(pristine_index_bytes)
    return bytes(buf)
