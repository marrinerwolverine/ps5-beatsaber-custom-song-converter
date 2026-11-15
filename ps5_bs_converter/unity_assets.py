"""
Binary parser and byte-exact serializer for PS5 Beat Saber Unity 2021.3.x (.assets v22) files.

Handles:
- SerializedFile v22 reading, PPtr resolution across Media/*.assets, and atomic 8-byte-aligned writing
- BeatmapLevelSO, PreviewBeatmapLevelSO, BeatmapDataSO (UTF-16LE gzipped JSON), AudioClip, and PerceivedLoudnessPerLevel
"""

from __future__ import annotations

import gzip
import os
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


def read_aligned_str(buf: bytes, offset: int) -> Tuple[str, int]:
    if offset + 4 > len(buf):
        raise ValueError(f"String length out of bounds at {offset}/{len(buf)}")
    length = struct.unpack_from("<I", buf, offset)[0]
    end = offset + 4 + length
    if end > len(buf):
        raise ValueError(f"String payload ({length}B) out of bounds at {offset}/{len(buf)}")
    text = buf[offset + 4 : end].decode("utf-8")
    if end % 4:
        end += 4 - (end % 4)
    return text, end


def write_aligned_str(text: str) -> bytes:
    raw = text.encode("utf-8")
    pad = (4 - (len(raw) % 4)) % 4
    return struct.pack("<I", len(raw)) + raw + (b"\x00" * pad)


def read_aligned_bytes(buf: bytes, offset: int) -> Tuple[bytes, int]:
    if offset + 4 > len(buf):
        raise ValueError(f"Byte array length out of bounds at {offset}/{len(buf)}")
    length = struct.unpack_from("<I", buf, offset)[0]
    end = offset + 4 + length
    if end > len(buf):
        raise ValueError(f"Byte array payload ({length}B) out of bounds at {offset}/{len(buf)}")
    payload = bytes(buf[offset + 4 : end])
    if end % 4:
        end += 4 - (end % 4)
    return payload, end


def write_aligned_bytes(payload: bytes) -> bytes:
    pad = (4 - (len(payload) % 4)) % 4
    return struct.pack("<I", len(payload)) + payload + (b"\x00" * pad)


def read_pptr(buf: bytes, offset: int) -> Tuple[Tuple[int, int], int]:
    return struct.unpack_from("<iq", buf, offset), offset + 12


def write_pptr(pptr: Tuple[int, int]) -> bytes:
    return struct.pack("<iq", pptr[0], pptr[1])


@dataclass
class AssetObject:
    index: int
    path_id: int
    byte_start: int
    byte_size: int
    type_id: int
    class_id: int
    blob: bytes


class SerializedAssetsFile:
    """Represents a Unity SerializedFile format v22 (.assets or CAB-*) on PS4/PS5."""

    def __init__(self, path: Optional[str] = None, raw_bytes: Optional[bytes] = None, name: str = "CAB"):
        self.path = path or ""
        self.filename = os.path.basename(path) if path else name
        self.dirty = False
        if raw_bytes is not None:
            self._load_bytes(raw_bytes, self.filename)
        elif path is not None:
            with open(path, "rb") as f:
                self._load_bytes(f.read(), path)
        else:
            raise ValueError("Either path or raw_bytes must be provided")

    @classmethod
    def from_bytes(cls, raw_bytes: bytes, name: str = "CAB") -> "SerializedAssetsFile":
        return cls(path=None, raw_bytes=raw_bytes, name=name)

    def _load(self, path: str) -> None:
        with open(path, "rb") as f:
            self._load_bytes(f.read(), path)

    def _load_bytes(self, orig: bytes, label: str) -> None:
        if len(orig) < 64:
            raise ValueError(f"File too small to be a Unity SerializedFile: {label}")

        version = struct.unpack_from(">I", orig, 8)[0]
        endian = orig[16]
        if version != 22 or endian != 0:
            raise ValueError(
                f"Unsupported SerializedFile version={version} endian={endian} in {label} (expected v22 LE)"
            )

        self.header20 = orig[:20]
        self.meta_size = struct.unpack_from(">I", orig, 20)[0]
        self.file_size, self.data_offset, self.unk64 = struct.unpack_from(">QQQ", orig, 24)

        pos = 48
        ver_end = orig.index(b"\x00", pos)
        self.unity_version = orig[pos:ver_end].decode("ascii")
        pos = ver_end + 1
        self.target_platform = struct.unpack_from("<i", orig, pos)[0]
        pos += 4
        self.enable_type_tree = orig[pos]
        pos += 1
        if self.enable_type_tree != 0:
            raise ValueError(f"Unexpected TypeTree enabled in {path}")

        tcount = struct.unpack_from("<i", orig, pos)[0]
        pos += 4
        self.types: List[Tuple[int, int, int, Optional[bytes], bytes]] = []
        for _ in range(tcount):
            cid = struct.unpack_from("<i", orig, pos)[0]
            pos += 4
            stripped = orig[pos]
            pos += 1
            sidx = struct.unpack_from("<h", orig, pos)[0]
            pos += 2
            sid = None
            if cid == 114:
                sid = orig[pos : pos + 16]
                pos += 16
            ohash = orig[pos : pos + 16]
            pos += 16
            self.types.append((cid, stripped, sidx, sid, ohash))

        self._meta_before_objs = orig[48:pos]

        ocount = struct.unpack_from("<i", orig, pos)[0]
        pos += 4
        self.objects: List[AssetObject] = []
        self.by_path_id: Dict[int, AssetObject] = {}
        for idx in range(ocount):
            if pos % 4:
                pos += 4 - (pos % 4)
            pid, bstart, bsz, tid = struct.unpack_from("<qQIi", orig, pos)
            pos += 24
            cid = self.types[tid][0]
            abs_off = self.data_offset + bstart
            blob = orig[abs_off : abs_off + bsz]
            obj = AssetObject(
                index=idx,
                path_id=pid,
                byte_start=bstart,
                byte_size=bsz,
                type_id=tid,
                class_id=cid,
                blob=blob,
            )
            self.objects.append(obj)
            self.by_path_id[pid] = obj

        self._meta_after_objs = orig[pos : 48 + self.meta_size]

        # Parse externals so PPtrs with fileID > 0 resolve across Media/*.assets
        epos = pos
        scount = struct.unpack_from("<i", orig, epos)[0]
        epos += 4
        for _ in range(scount):
            epos += 4
            if epos % 4:
                epos += 4 - (epos % 4)
            epos += 8
        ecount = struct.unpack_from("<i", orig, epos)[0]
        epos += 4
        self.externals: List[str] = []
        for _ in range(ecount):
            epos = orig.index(b"\x00", epos) + 1 + 20
            end_p = orig.index(b"\x00", epos)
            ext_path = orig[epos:end_p].decode("utf-8")
            epos = end_p + 1
            self.externals.append(os.path.basename(ext_path))

    def set_object_blob(self, path_id: int, new_blob: bytes) -> None:
        obj = self.by_path_id[path_id]
        obj.blob = bytes(new_blob)
        obj.byte_size = len(obj.blob)
        self.dirty = True

    def to_bytes(self) -> bytes:
        # Preserve original object stream layout order, 8-byte aligned per Unity v22 rules
        ordered = sorted(self.objects, key=lambda o: o.byte_start)
        data_buf = bytearray()
        new_starts: Dict[int, int] = {}
        new_sizes: Dict[int, int] = {}

        for obj in ordered:
            rem = len(data_buf) % 8
            if rem:
                data_buf += b"\x00" * (8 - rem)
            new_starts[obj.index] = len(data_buf)
            new_sizes[obj.index] = len(obj.blob)
            data_buf += obj.blob

        meta_buf = bytearray(self._meta_before_objs)
        meta_buf += struct.pack("<i", len(self.objects))
        for obj in self.objects:
            abs_mpos = 48 + len(meta_buf)
            if abs_mpos % 4:
                meta_buf += b"\x00" * (4 - (abs_mpos % 4))
            meta_buf += struct.pack(
                "<qQIi",
                obj.path_id,
                new_starts[obj.index],
                new_sizes[obj.index],
                obj.type_id,
            )
        meta_buf += self._meta_after_objs

        if len(meta_buf) != self.meta_size:
            raise RuntimeError(
                f"Metadata size changed ({len(meta_buf)} != {self.meta_size}) in {self.filename}"
            )

        total_file_size = self.data_offset + len(data_buf)
        out = bytearray(self.header20)
        out += struct.pack(">IQQQ", len(meta_buf), total_file_size, self.data_offset, self.unk64)
        out += meta_buf
        if len(out) > self.data_offset:
            raise RuntimeError(f"Metadata overflowed data_offset in {self.filename}")
        out += b"\x00" * (self.data_offset - len(out))
        out += data_buf
        return bytes(out)

    def save(self, target_path: Optional[str] = None) -> None:
        dest = target_path or self.path
        payload = self.to_bytes()
        tmp_path = dest + ".tmp_write"
        with open(tmp_path, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, dest)
        if dest == self.path:
            # Refresh byte_start values to match written state
            self._load(self.path)
            self.dirty = False


# ==============================================================================
# Typed Object Parsers & Serializers
# ==============================================================================

@dataclass
class DifficultyBeatmapEntry:
    difficulty: int          # 0=Easy, 1=Normal, 2=Hard, 3=Expert, 4=ExpertPlus
    rank: int                # 1, 3, 5, 7, 9
    njs: float               # _noteJumpMovementSpeed
    njs_offset: float        # _noteJumpStartBeatOffset
    env_idx: int             # _environmentNameIdx
    beatmap_data: Tuple[int, int]  # PPtr<BeatmapDataSO>


@dataclass
class DifficultyBeatmapSetEntry:
    characteristic: Tuple[int, int]  # PPtr<BeatmapCharacteristicSO>
    difficulties: List[DifficultyBeatmapEntry] = field(default_factory=list)


@dataclass
class BeatmapLevelData:
    head28: bytes
    m_name: str
    level_id: str
    song_name: str
    song_sub_name: str
    song_author_name: str
    level_author_name: str
    audio_clip: Tuple[int, int]
    bpm: float
    song_time_offset: float
    shuffle: float
    shuffle_period: float
    preview_start_time: float
    preview_duration: float
    cover_image: Tuple[int, int]
    environment_info: Tuple[int, int]
    all_directions_environment_info: Tuple[int, int]
    environment_infos: List[Tuple[int, int]]
    difficulty_sets: List[DifficultyBeatmapSetEntry]
    tail: bytes

    @classmethod
    def from_bytes(cls, blob: bytes) -> "BeatmapLevelData":
        head28 = blob[:28]
        m_name, off = read_aligned_str(blob, 28)
        level_id, off = read_aligned_str(blob, off)
        song_name, off = read_aligned_str(blob, off)
        song_sub, off = read_aligned_str(blob, off)
        song_author, off = read_aligned_str(blob, off)
        level_author, off = read_aligned_str(blob, off)
        audio_clip, off = read_pptr(blob, off)
        bpm, time_off, shuffle, shuffle_per, prev_start, prev_dur = struct.unpack_from("<6f", blob, off)
        off += 24
        cover_img, off = read_pptr(blob, off)
        env_info, off = read_pptr(blob, off)
        all_dir_env, off = read_pptr(blob, off)
        env_cnt = struct.unpack_from("<I", blob, off)[0]
        off += 4
        env_list: List[Tuple[int, int]] = []
        for _ in range(env_cnt):
            ep, off = read_pptr(blob, off)
            env_list.append(ep)
        sets_cnt = struct.unpack_from("<I", blob, off)[0]
        off += 4
        sets: List[DifficultyBeatmapSetEntry] = []
        for _ in range(sets_cnt):
            char_pptr, off = read_pptr(blob, off)
            dcnt = struct.unpack_from("<I", blob, off)[0]
            off += 4
            diffs: List[DifficultyBeatmapEntry] = []
            for _ in range(dcnt):
                diff, rank, njs, njs_off, env_idx = struct.unpack_from("<iiffi", blob, off)
                off += 20
                bdata_pptr, off = read_pptr(blob, off)
                diffs.append(
                    DifficultyBeatmapEntry(
                        difficulty=diff,
                        rank=rank,
                        njs=njs,
                        njs_offset=njs_off,
                        env_idx=env_idx,
                        beatmap_data=bdata_pptr,
                    )
                )
            sets.append(DifficultyBeatmapSetEntry(characteristic=char_pptr, difficulties=diffs))
        tail = blob[off:]
        if len(tail) != 8:
            raise ValueError(f"Unexpected BeatmapLevelSO tail ({len(tail)}B) in {m_name}")
        return cls(
            head28=head28,
            m_name=m_name,
            level_id=level_id,
            song_name=song_name,
            song_sub_name=song_sub,
            song_author_name=song_author,
            level_author_name=level_author,
            audio_clip=audio_clip,
            bpm=bpm,
            song_time_offset=time_off,
            shuffle=shuffle,
            shuffle_period=shuffle_per,
            preview_start_time=prev_start,
            preview_duration=prev_dur,
            cover_image=cover_img,
            environment_info=env_info,
            all_directions_environment_info=all_dir_env,
            environment_infos=env_list,
            difficulty_sets=sets,
            tail=tail,
        )

    def to_bytes(self) -> bytes:
        out = bytearray(self.head28)
        out += write_aligned_str(self.m_name)
        out += write_aligned_str(self.level_id)
        out += write_aligned_str(self.song_name)
        out += write_aligned_str(self.song_sub_name)
        out += write_aligned_str(self.song_author_name)
        out += write_aligned_str(self.level_author_name)
        out += write_pptr(self.audio_clip)
        out += struct.pack(
            "<6f",
            self.bpm,
            self.song_time_offset,
            self.shuffle,
            self.shuffle_period,
            self.preview_start_time,
            self.preview_duration,
        )
        out += write_pptr(self.cover_image)
        out += write_pptr(self.environment_info)
        out += write_pptr(self.all_directions_environment_info)
        out += struct.pack("<I", len(self.environment_infos))
        for ep in self.environment_infos:
            out += write_pptr(ep)
        out += struct.pack("<I", len(self.difficulty_sets))
        for s in self.difficulty_sets:
            out += write_pptr(s.characteristic)
            out += struct.pack("<I", len(s.difficulties))
            for df in s.difficulties:
                out += struct.pack("<iiffi", df.difficulty, df.rank, df.njs, df.njs_offset, df.env_idx)
                out += write_pptr(df.beatmap_data)
        out += self.tail
        return bytes(out)


@dataclass
class PreviewBeatmapLevelData:
    head28: bytes
    m_name: str
    level_id: str
    song_name: str
    song_sub_name: str
    song_author_name: str
    level_author_name: str
    preview_audio_clip: Tuple[int, int]
    bpm: float
    song_time_offset: float
    shuffle: float
    shuffle_period: float
    preview_start_time: float
    preview_duration: float
    song_duration: float
    cover_image: Tuple[int, int]
    environment_info: Tuple[int, int]
    all_directions_environment_info: Tuple[int, int]
    tail: bytes

    @classmethod
    def from_bytes(cls, blob: bytes) -> "PreviewBeatmapLevelData":
        head28 = blob[:28]
        m_name, off = read_aligned_str(blob, 28)
        level_id, off = read_aligned_str(blob, off)
        song_name, off = read_aligned_str(blob, off)
        song_sub, off = read_aligned_str(blob, off)
        song_author, off = read_aligned_str(blob, off)
        level_author, off = read_aligned_str(blob, off)
        prev_clip, off = read_pptr(blob, off)
        bpm, time_off, shuffle, shuffle_per, prev_start, prev_dur, song_dur = struct.unpack_from(
            "<7f", blob, off
        )
        off += 28
        cover_img, off = read_pptr(blob, off)
        env_info, off = read_pptr(blob, off)
        all_dir_env, off = read_pptr(blob, off)
        tail = blob[off:]
        return cls(
            head28=head28,
            m_name=m_name,
            level_id=level_id,
            song_name=song_name,
            song_sub_name=song_sub,
            song_author_name=song_author,
            level_author_name=level_author,
            preview_audio_clip=prev_clip,
            bpm=bpm,
            song_time_offset=time_off,
            shuffle=shuffle,
            shuffle_period=shuffle_per,
            preview_start_time=prev_start,
            preview_duration=prev_dur,
            song_duration=song_dur,
            cover_image=cover_img,
            environment_info=env_info,
            all_directions_environment_info=all_dir_env,
            tail=tail,
        )

    def to_bytes(self) -> bytes:
        out = bytearray(self.head28)
        out += write_aligned_str(self.m_name)
        out += write_aligned_str(self.level_id)
        out += write_aligned_str(self.song_name)
        out += write_aligned_str(self.song_sub_name)
        out += write_aligned_str(self.song_author_name)
        out += write_aligned_str(self.level_author_name)
        out += write_pptr(self.preview_audio_clip)
        out += struct.pack(
            "<7f",
            self.bpm,
            self.song_time_offset,
            self.shuffle,
            self.shuffle_period,
            self.preview_start_time,
            self.preview_duration,
            self.song_duration,
        )
        out += write_pptr(self.cover_image)
        out += write_pptr(self.environment_info)
        out += write_pptr(self.all_directions_environment_info)
        out += self.tail
        return bytes(out)


@dataclass
class BeatmapDataObject:
    """Represents BeatmapDataSO (gzipped UTF-16LE JSON beatmap payload)."""
    head28: bytes
    m_name: str
    json_data: str
    gzipped_utf16le_bytes: bytes

    @classmethod
    def from_bytes(cls, blob: bytes) -> "BeatmapDataObject":
        head28 = blob[:28]
        m_name, off = read_aligned_str(blob, 28)
        json_str, off = read_aligned_str(blob, off)
        gz_bytes, off = read_aligned_bytes(blob, off)
        if off != len(blob):
            raise ValueError(f"Trailing bytes in BeatmapDataSO {m_name}: {off} != {len(blob)}")
        return cls(head28=head28, m_name=m_name, json_data=json_str, gzipped_utf16le_bytes=gz_bytes)

    def decode_json_text(self) -> str:
        raw = gzip.decompress(self.gzipped_utf16le_bytes)
        return raw.decode("utf-16-le")

    def set_json_text(self, json_text: str) -> None:
        raw_utf16 = json_text.encode("utf-16-le")
        self.json_data = ""
        self.gzipped_utf16le_bytes = gzip.compress(raw_utf16, compresslevel=6, mtime=0)

    def to_bytes(self) -> bytes:
        out = bytearray(self.head28)
        out += write_aligned_str(self.m_name)
        out += write_aligned_str(self.json_data)
        out += write_aligned_bytes(self.gzipped_utf16le_bytes)
        return bytes(out)


@dataclass
class TextAssetData:
    """Represents a Unity TextAsset (class_id=49) used inside v1.35/v1.37+ song bundles."""
    name: str
    script_bytes: bytes

    @classmethod
    def from_bytes(cls, blob: bytes) -> "TextAssetData":
        name, off = read_aligned_str(blob, 0)
        script_bytes, off2 = read_aligned_bytes(blob, off)
        if off2 != len(blob):
            raise ValueError(f"Trailing bytes in TextAsset '{name}': {off2} != {len(blob)}")
        return cls(name=name, script_bytes=script_bytes)

    def to_bytes(self) -> bytes:
        return write_aligned_str(self.name) + write_aligned_bytes(self.script_bytes)


@dataclass
class AudioClipData:
    name: str
    load_type: int
    channels: int
    frequency: int
    bits_per_sample: int
    length: float
    flags12: bytes
    source: str
    offset: int
    size: int
    compression_format: int  # 0 = PCM, 1 = Vorbis

    @classmethod
    def from_bytes(cls, blob: bytes) -> "AudioClipData":
        name, off = read_aligned_str(blob, 0)
        load_type, channels, freq, bits, length = struct.unpack_from("<iiiif", blob, off)
        flags12 = blob[off + 20 : off + 32]
        src, off2 = read_aligned_str(blob, off + 32)
        m_off, m_sz, comp_fmt = struct.unpack_from("<QQi", blob, off2)
        if off2 + 20 != len(blob):
            raise ValueError(f"Unexpected AudioClip size in {name}: {off2 + 20} != {len(blob)}")
        return cls(
            name=name,
            load_type=load_type,
            channels=channels,
            frequency=freq,
            bits_per_sample=bits,
            length=length,
            flags12=flags12,
            source=src,
            offset=m_off,
            size=m_sz,
            compression_format=comp_fmt,
        )

    def to_bytes(self) -> bytes:
        out = bytearray(write_aligned_str(self.name))
        out += struct.pack(
            "<iiiif",
            self.load_type,
            self.channels,
            self.frequency,
            self.bits_per_sample,
            self.length,
        )
        out += self.flags12
        out += write_aligned_str(self.source)
        out += struct.pack("<QQi", self.offset, self.size, self.compression_format)
        return bytes(out)


@dataclass
class PerceivedLoudnessTable:
    head28: bytes
    m_name: str
    entries: List[Tuple[str, float, str]]  # (level_id, loudness_lufs, checksum)

    @classmethod
    def from_bytes(cls, blob: bytes) -> "PerceivedLoudnessTable":
        head28 = blob[:28]
        m_name, off = read_aligned_str(blob, 28)
        cnt = struct.unpack_from("<I", blob, off)[0]
        off += 4
        entries: List[Tuple[str, float, str]] = []
        for _ in range(cnt):
            lid, off = read_aligned_str(blob, off)
            loudness = struct.unpack_from("<f", blob, off)[0]
            off += 4
            checksum, off = read_aligned_str(blob, off)
            entries.append((lid, loudness, checksum))
        if off != len(blob):
            raise ValueError(f"Trailing bytes in PerceivedLoudnessPerLevel: {off} != {len(blob)}")
        return cls(head28=head28, m_name=m_name, entries=entries)

    def set_loudness(self, level_id: str, loudness_lufs: float) -> None:
        for i, (lid, _, chk) in enumerate(self.entries):
            if lid == level_id:
                self.entries[i] = (lid, float(loudness_lufs), chk)
                return
        self.entries.append((level_id, float(loudness_lufs), ""))

    def to_bytes(self) -> bytes:
        out = bytearray(self.head28)
        out += write_aligned_str(self.m_name)
        out += struct.pack("<I", len(self.entries))
        for lid, loudness, chk in self.entries:
            out += write_aligned_str(lid)
            out += struct.pack("<f", loudness)
            out += write_aligned_str(chk)
        return bytes(out)


@dataclass
class EnvironmentInfoEntry:
    path_id: int
    object_name: str
    display_name: str
    serialized_name: str
    color_scheme_path_id: int
    is_all_directions: bool = False


@dataclass
class ColorSchemeData:
    """Represents ColorSchemeSO in resources.assets (148-byte RGBA float32 color payload)."""
    head28: bytes
    m_name: str
    color_scheme_id: str
    localized_name_key: str
    use_non_localized_name: int
    non_localized_name: str
    is_editable: int
    saber_a: Tuple[float, float, float, float]
    saber_b: Tuple[float, float, float, float]
    env_color_0: Tuple[float, float, float, float]
    env_color_1: Tuple[float, float, float, float]
    obstacles_color: Tuple[float, float, float, float]
    env_color_w: Tuple[float, float, float, float]
    supports_boost: int
    env_color_0_boost: Tuple[float, float, float, float]
    env_color_1_boost: Tuple[float, float, float, float]
    env_color_w_boost: Tuple[float, float, float, float]

    @classmethod
    def from_bytes(cls, blob: bytes) -> "ColorSchemeData":
        head28 = blob[:28]
        m_name, off = read_aligned_str(blob, 28)
        cs_id, off = read_aligned_str(blob, off)
        loc_key, off = read_aligned_str(blob, off)
        use_non_loc = struct.unpack_from("<I", blob, off)[0]
        off += 4
        non_loc_name, off = read_aligned_str(blob, off)
        is_edit = struct.unpack_from("<I", blob, off)[0]
        off += 4
        if len(blob) - off != 148:
            raise ValueError(f"Unexpected ColorSchemeSO payload length in {m_name}: {len(blob) - off}")

        def r_rgba(o: int) -> Tuple[Tuple[float, float, float, float], int]:
            return struct.unpack_from("<4f", blob, o), o + 16

        saber_a, off = r_rgba(off)
        saber_b, off = r_rgba(off)
        env0, off = r_rgba(off)
        env1, off = r_rgba(off)
        obs, off = r_rgba(off)
        env_w, off = r_rgba(off)
        sup_boost = struct.unpack_from("<I", blob, off)[0]
        off += 4
        env0_b, off = r_rgba(off)
        env1_b, off = r_rgba(off)
        env_w_b, off = r_rgba(off)
        return cls(
            head28=head28,
            m_name=m_name,
            color_scheme_id=cs_id,
            localized_name_key=loc_key,
            use_non_localized_name=use_non_loc,
            non_localized_name=non_loc_name,
            is_editable=is_edit,
            saber_a=saber_a,
            saber_b=saber_b,
            env_color_0=env0,
            env_color_1=env1,
            obstacles_color=obs,
            env_color_w=env_w,
            supports_boost=sup_boost,
            env_color_0_boost=env0_b,
            env_color_1_boost=env1_b,
            env_color_w_boost=env_w_b,
        )

    def to_bytes(self) -> bytes:
        out = bytearray(self.head28)
        out += write_aligned_str(self.m_name)
        out += write_aligned_str(self.color_scheme_id)
        out += write_aligned_str(self.localized_name_key)
        out += struct.pack("<I", self.use_non_localized_name)
        out += write_aligned_str(self.non_localized_name)
        out += struct.pack("<I", self.is_editable)
        for rgba in (
            self.saber_a,
            self.saber_b,
            self.env_color_0,
            self.env_color_1,
            self.obstacles_color,
            self.env_color_w,
        ):
            out += struct.pack("<4f", *rgba)
        out += struct.pack("<I", self.supports_boost)
        for rgba in (self.env_color_0_boost, self.env_color_1_boost, self.env_color_w_boost):
            out += struct.pack("<4f", *rgba)
        return bytes(out)

