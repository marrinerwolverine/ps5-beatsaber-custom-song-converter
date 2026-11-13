"""
PS5 Beat Saber (PSVR2) Game Dump Manager, Safe Backup/Restore Engine,
Smart .resource Tail Allocator, Integrity Verifier, and FTP Deployer.
"""

from __future__ import annotations

import datetime
import ftplib
import io
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .audio_fsb5 import encode_audio_file_to_fsb5, verify_fsb5_header
from .beatmap_converter import (
    DIFF_NAMES,
    CustomSongPackage,
    ParsedDifficultyChart,
    ensure_all_five_difficulties,
    generate_mode_from_standard,
    load_custom_map,
    serialize_v3_chart_json,
)
from .unity_assets import (
    AudioClipData,
    BeatmapDataObject,
    BeatmapLevelData,
    PerceivedLoudnessTable,
    PreviewBeatmapLevelData,
    SerializedAssetsFile,
    read_aligned_str,
    read_pptr,
)

CHAR_OBJECT_TO_MODE = {
    "StandardBeatmapCharacteristic": "Standard",
    "OneColorBeatmapCharacteristic": "OneSaber",
    "NoArrowsBeatmapCharacteristic": "NoArrows",
    "90DegreeBeatmapCharacteristic": "90Degree",
    "360DegreeBeatmapCharacteristic": "360Degree",
    "LegacyStandardBeatmapCharacteristic": "LegacyStandard",
}

TRACKED_ASSETS_FILES = [
    "sharedassets48.assets",
    "sharedassets43.assets",
    "sharedassets44.assets",
    "sharedassets21.assets",
]

TRACKED_RESOURCE_FILES = [
    "sharedassets48.resource",
    "sharedassets43.resource",
    "sharedassets44.resource",
    "sharedassets21.resource",
]


@dataclass
class SongSlotInfo:
    level_id: str
    pack_id: str
    pack_name: str
    is_free_base_game: bool
    stock_song_name: str
    stock_song_sub_name: str
    stock_song_author: str
    stock_level_author: str
    stock_bpm: float
    current_song_name: str
    current_song_author: str
    current_bpm: float
    level_assets_file: str
    level_path_id: int
    audio_assets_file: str
    audio_path_id: int
    preview_assets_file: Optional[str]
    preview_path_id: Optional[int]
    modes: List[str] = field(default_factory=list)
    is_custom: bool = False
    installed_info: Optional[dict] = None


def find_ps5_game_root(start_path: str) -> str:
    """
    Resolve a user-supplied folder (parent folder, PPSA*-app folder, or Media folder)
    to the exact PS5 app root containing Media/sharedassets48.assets.
    """
    p = os.path.abspath(start_path)
    if os.path.isfile(os.path.join(p, "sharedassets48.assets")):
        return os.path.dirname(p)
    if os.path.isfile(os.path.join(p, "Media", "sharedassets48.assets")):
        return p
    if os.path.isdir(p):
        for entry in sorted(os.listdir(p)):
            sub = os.path.join(p, entry)
            if os.path.isdir(sub) and os.path.isfile(os.path.join(sub, "Media", "sharedassets48.assets")):
                return sub
            if os.path.isdir(sub):
                for sub2_name in sorted(os.listdir(sub)):
                    sub2 = os.path.join(sub, sub2_name)
                    if os.path.isdir(sub2) and os.path.isfile(
                        os.path.join(sub2, "Media", "sharedassets48.assets")
                    ):
                        return sub2
    raise FileNotFoundError(
        f"Could not find PS5 Beat Saber 'Media/sharedassets48.assets' under: {start_path}"
    )


class PS5BeatSaberManager:
    """Manages custom song installation, backups, restoration, and verification on a PS5 dump."""

    def __init__(self, game_path: str, backup_root: Optional[str] = None):
        self.app_root = find_ps5_game_root(game_path)
        self.media_dir = os.path.join(self.app_root, "Media")
        self.title_id, self.content_version = self._read_param_json()

        # Store backups OUTSIDE PPSA*-app so extra backup files are never copied onto PS5 /app0
        if backup_root:
            self.backup_dir = os.path.abspath(backup_root)
        else:
            parent_dir = os.path.dirname(self.app_root)
            self.backup_dir = os.path.join(
                parent_dir, "_ps5_custom_converter_backups", self.title_id or "PPSA_BEATSABER"
            )
        self.state_path = os.path.join(self.backup_dir, "converter_state.json")

        self._live_assets: Dict[str, SerializedAssetsFile] = {}
        self._pristine_assets: Dict[str, SerializedAssetsFile] = {}
        self.state: dict = self._load_state()
        self._char_map: Dict[Tuple[str, int], str] = {}
        self._preview_index: Dict[str, Tuple[str, int]] = {}
        self._loudness_pid: Optional[int] = None
        self.slots: Dict[str, SongSlotInfo] = {}
        self.refresh_catalog()

    def _read_param_json(self) -> Tuple[str, str]:
        param_path = os.path.join(self.app_root, "sce_sys", "param.json")
        if os.path.isfile(param_path):
            try:
                with open(param_path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                return str(d.get("titleId") or "PPSA15035"), str(d.get("contentVersion") or "01.012.000")
            except Exception:
                pass
        return "PPSA15035", "Unknown"

    def _load_state(self) -> dict:
        if os.path.isfile(self.state_path):
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {
            "title_id": self.title_id,
            "content_version": self.content_version,
            "original_resource_sizes": {},
            "installed_slots": {},
            "resource_allocations": {},
        }

    def _save_state(self) -> None:
        os.makedirs(self.backup_dir, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    def ensure_pristine_backup(self, log_fn: Optional[Callable[[str], None]] = None) -> None:
        """Back up original .assets files and record original .resource byte lengths before first edit."""
        os.makedirs(self.backup_dir, exist_ok=True)
        changed = False

        for fn in TRACKED_ASSETS_FILES:
            src = os.path.join(self.media_dir, fn)
            dst = os.path.join(self.backup_dir, fn + ".orig")
            if os.path.isfile(src) and not os.path.isfile(dst):
                if log_fn:
                    log_fn(f"Creating pristine backup of {fn}...")
                shutil.copy2(src, dst)
                changed = True

        orig_sizes = self.state.setdefault("original_resource_sizes", {})
        for rfn in TRACKED_RESOURCE_FILES:
            rp = os.path.join(self.media_dir, rfn)
            if os.path.isfile(rp) and rfn not in orig_sizes:
                orig_sizes[rfn] = os.path.getsize(rp)
                changed = True

        if changed or not os.path.isfile(self.state_path):
            self._save_state()

    def has_backup(self) -> bool:
        return os.path.isfile(os.path.join(self.backup_dir, "sharedassets48.assets.orig"))

    def _get_live_assets(self, filename: str) -> SerializedAssetsFile:
        if filename not in self._live_assets:
            self._live_assets[filename] = SerializedAssetsFile(os.path.join(self.media_dir, filename))
        return self._live_assets[filename]

    def _get_pristine_assets(self, filename: str) -> SerializedAssetsFile:
        if filename not in self._pristine_assets:
            bak = os.path.join(self.backup_dir, filename + ".orig")
            if os.path.isfile(bak):
                self._pristine_assets[filename] = SerializedAssetsFile(bak)
            else:
                self._pristine_assets[filename] = self._get_live_assets(filename)
        return self._pristine_assets[filename]

    def _resolve_pptr(
        self, base_file: str, pptr: Tuple[int, int], use_pristine: bool = False
    ) -> Tuple[str, int]:
        fid, pid = pptr
        if fid == 0:
            return base_file, pid
        af = self._get_pristine_assets(base_file) if use_pristine else self._get_live_assets(base_file)
        return af.externals[fid - 1], pid

    def refresh_catalog(self) -> None:
        """Scan all 28 LevelPacks and 234 song slots from the PS5 dump."""
        self._live_assets.clear()
        self._pristine_assets.clear()
        self._char_map.clear()
        self._preview_index.clear()
        self.slots.clear()
        self._loudness_pid = None

        sa48_live = self._get_live_assets("sharedassets48.assets")
        sa48_orig = self._get_pristine_assets("sharedassets48.assets")

        # 1. Index BeatmapCharacteristics & PerceivedLoudnessPerLevel & Free Packs
        free_pack_pids = set()
        level_pack_pids = []

        for obj in sa48_orig.objects:
            if obj.class_id != 114 or len(obj.blob) < 32:
                continue
            try:
                m_name, off = read_aligned_str(obj.blob, 28)
            except Exception:
                continue

            if m_name == "AllBeatmapCharacteristicsCollection":
                import struct as _st
                cnt = _st.unpack_from("<I", obj.blob, off)[0]
                off += 4
                for _ in range(cnt):
                    cpptr, off = read_pptr(obj.blob, off)
                    cfile, cpid = self._resolve_pptr("sharedassets48.assets", cpptr, use_pristine=True)
                    cobj = self._get_pristine_assets(cfile).by_path_id[cpid]
                    cname, _ = read_aligned_str(cobj.blob, 28)
                    self._char_map[(cfile, cpid)] = CHAR_OBJECT_TO_MODE.get(cname, cname)

            elif m_name in ("DefaultAlwaysOwnedContent", "OstAndExtrasOnlyLevelPackCollection"):
                import struct as _st
                cnt = _st.unpack_from("<I", obj.blob, off)[0]
                off += 4
                for _ in range(cnt):
                    ppptr, off = read_pptr(obj.blob, off)
                    if ppptr[0] == 0:
                        free_pack_pids.add(ppptr[1])

            elif m_name == "PerceivedLoudnessPerLevel":
                self._loudness_pid = obj.path_id

            elif m_name.endswith("LevelPack") and not m_name.endswith("PreviewBeatmapLevelPack"):
                level_pack_pids.append(obj.path_id)

        # 2. Index PreviewBeatmapLevelSOs across sharedassets48.assets and sharedassets21.assets
        for pfile in ("sharedassets48.assets", "sharedassets21.assets"):
            if not os.path.isfile(os.path.join(self.media_dir, pfile)):
                continue
            af_orig = self._get_pristine_assets(pfile)
            for obj in af_orig.objects:
                if obj.class_id == 114 and len(obj.blob) >= 64:
                    try:
                        nm, _ = read_aligned_str(obj.blob, 28)
                        if nm.endswith("PreviewLevel") and "PreviewBeatmapLevel" not in nm:
                            pdata = PreviewBeatmapLevelData.from_bytes(obj.blob)
                            self._preview_index[pdata.level_id] = (pfile, obj.path_id)
                    except Exception:
                        pass

        # 3. Walk all LevelPacks -> LevelCollections -> BeatmapLevelSOs
        import struct as _st
        installed_map = self.state.get("installed_slots", {})

        for pack_pid in sorted(level_pack_pids):
            pblob = sa48_orig.by_path_id[pack_pid].blob
            _, off = read_aligned_str(pblob, 28)
            pack_id, off = read_aligned_str(pblob, off)
            pack_name, off = read_aligned_str(pblob, off)
            _short_name, off = read_aligned_str(pblob, off)
            _cov1, off = read_pptr(pblob, off)
            _cov2, off = read_pptr(pblob, off)
            off += 4  # flag int32
            coll_pptr, off = read_pptr(pblob, off)
            cfile, cpid = self._resolve_pptr("sharedassets48.assets", coll_pptr, use_pristine=True)
            cblob = self._get_pristine_assets(cfile).by_path_id[cpid].blob
            _, coff = read_aligned_str(cblob, 28)
            lcnt = _st.unpack_from("<I", cblob, coff)[0]
            coff += 4

            is_free = pack_pid in free_pack_pids
            for _ in range(lcnt):
                lpptr, coff = read_pptr(cblob, coff)
                lfile, lpid = self._resolve_pptr(cfile, lpptr, use_pristine=True)
                orig_lvl = BeatmapLevelData.from_bytes(
                    self._get_pristine_assets(lfile).by_path_id[lpid].blob
                )
                live_lvl = BeatmapLevelData.from_bytes(
                    self._get_live_assets(lfile).by_path_id[lpid].blob
                )
                ac_file, ac_pid = self._resolve_pptr(lfile, orig_lvl.audio_clip, use_pristine=True)

                modes: List[str] = []
                for dset in orig_lvl.difficulty_sets:
                    ch_file, ch_pid = self._resolve_pptr(lfile, dset.characteristic, use_pristine=True)
                    mname = self._char_map.get((ch_file, ch_pid), "Standard")
                    if mname not in modes:
                        modes.append(mname)

                prev_loc = self._preview_index.get(orig_lvl.level_id)
                inst = installed_map.get(orig_lvl.level_id)
                is_custom = bool(inst) or (
                    live_lvl.song_name != orig_lvl.song_name
                    or live_lvl.song_author_name != orig_lvl.song_author_name
                )

                self.slots[orig_lvl.level_id] = SongSlotInfo(
                    level_id=orig_lvl.level_id,
                    pack_id=pack_id,
                    pack_name=pack_name,
                    is_free_base_game=is_free,
                    stock_song_name=orig_lvl.song_name,
                    stock_song_sub_name=orig_lvl.song_sub_name,
                    stock_song_author=orig_lvl.song_author_name,
                    stock_level_author=orig_lvl.level_author_name,
                    stock_bpm=orig_lvl.bpm,
                    current_song_name=live_lvl.song_name,
                    current_song_author=live_lvl.song_author_name,
                    current_bpm=live_lvl.bpm,
                    level_assets_file=lfile,
                    level_path_id=lpid,
                    audio_assets_file=ac_file,
                    audio_path_id=ac_pid,
                    preview_assets_file=prev_loc[0] if prev_loc else None,
                    preview_path_id=prev_loc[1] if prev_loc else None,
                    modes=modes,
                    is_custom=is_custom,
                    installed_info=inst,
                )

    def list_slots(self, free_only: bool = False, pack_id: Optional[str] = None) -> List[SongSlotInfo]:
        items = list(self.slots.values())
        if free_only:
            items = [s for s in items if s.is_free_base_game]
        if pack_id:
            items = [s for s in items if s.pack_id.lower() == pack_id.lower()]
        # Sort Free packs first (OST1..6, Extras, Camellia), then alphabetically by pack & song
        items.sort(key=lambda s: (0 if s.is_free_base_game else 1, s.pack_name, s.stock_song_name))
        return items

    # ==========================================================================
    # Smart .resource Tail Allocator (Zero-Overwrite of Original Audio)
    # ==========================================================================

    def _free_slot_allocations(self, level_id: str) -> None:
        allocs_by_res = self.state.setdefault("resource_allocations", {})
        orig_sizes = self.state.get("original_resource_sizes", {})

        for res_fn, blocks in list(allocs_by_res.items()):
            remaining = [b for b in blocks if b.get("level_id") != level_id]
            allocs_by_res[res_fn] = remaining
            self._trim_resource_tail(res_fn, remaining, orig_sizes.get(res_fn, 0))

    def _trim_resource_tail(self, res_fn: str, active_blocks: List[dict], base_size: int) -> None:
        res_path = os.path.join(self.media_dir, res_fn)
        if not os.path.isfile(res_path):
            return
        if not active_blocks:
            if base_size == 0 and res_fn == "customsongs.resource":
                os.remove(res_path)
                return
            target_len = base_size
        else:
            target_len = max(base_size, max(b["offset"] + b["size"] for b in active_blocks))

        cur_len = os.path.getsize(res_path)
        if cur_len > target_len and target_len >= base_size:
            with open(res_path, "r+b") as f:
                f.truncate(target_len)

    def _write_resource_block(self, res_fn: str, level_id: str, kind: str, payload: bytes) -> int:
        res_path = os.path.join(self.media_dir, res_fn)
        orig_sizes = self.state.get("original_resource_sizes", {})
        base_size = orig_sizes.get(res_fn, 0)
        if not os.path.isfile(res_path):
            with open(res_path, "wb") as f:
                pass

        blocks = self.state.setdefault("resource_allocations", {}).setdefault(res_fn, [])
        blocks.sort(key=lambda b: b["offset"])

        # Look for a reusable gap >= base_size (32-byte aligned)
        need = len(payload)
        chosen_offset: Optional[int] = None
        cursor = (base_size + 31) & ~31
        for b in blocks:
            aligned_cursor = (cursor + 31) & ~31
            if b["offset"] - aligned_cursor >= need:
                chosen_offset = aligned_cursor
                break
            cursor = max(cursor, b["offset"] + b["size"])

        if chosen_offset is None:
            cur_eof = os.path.getsize(res_path)
            start_min = max(base_size, cursor, cur_eof)
            chosen_offset = (start_min + 31) & ~31

        with open(res_path, "r+b") as f:
            cur_eof = f.seek(0, os.SEEK_END)
            if chosen_offset > cur_eof:
                f.write(b"\x00" * (chosen_offset - cur_eof))
            f.seek(chosen_offset)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

        blocks.append(
            {
                "level_id": level_id,
                "kind": kind,
                "offset": chosen_offset,
                "size": need,
            }
        )
        blocks.sort(key=lambda b: b["offset"])
        return chosen_offset

    # ==========================================================================
    # Install Custom Song
    # ==========================================================================

    def install_custom_song(
        self,
        map_source: str | bytes | CustomSongPackage,
        target_level_id: str,
        song_name_override: Optional[str] = None,
        song_author_override: Optional[str] = None,
        level_author_override: Optional[str] = None,
        auto_generate_difficulties: bool = True,
        auto_generate_modes: bool = True,
        use_separate_resource: bool = False,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> SongSlotInfo:
        def log(msg: str) -> None:
            if log_fn:
                log_fn(msg)

        if target_level_id not in self.slots:
            # Case-insensitive lookup convenience
            matches = [k for k in self.slots if k.lower() == target_level_id.lower()]
            if len(matches) == 1:
                target_level_id = matches[0]
            else:
                raise KeyError(f"Unknown PS5 song slot '{target_level_id}'")

        slot = self.slots[target_level_id]
        self.ensure_pristine_backup(log_fn=log)

        if isinstance(map_source, CustomSongPackage):
            pkg = map_source
        else:
            log(f"Reading BeatSaver map package: {map_source if isinstance(map_source, str) else 'zip bytes'}...")
            pkg = load_custom_map(map_source)

        final_title = (song_name_override if song_name_override is not None else pkg.song_name).strip() or pkg.song_name
        final_artist = (song_author_override if song_author_override is not None else pkg.song_author_name).strip()
        final_mapper = (level_author_override if level_author_override is not None else pkg.level_author_name).strip()

        log(f"Loaded '{final_title}' by '{final_artist}' ({pkg.bpm:.1f} BPM, {pkg.native_summary()})")

        # Start from pristine slot objects so repeated replacements never accumulate stale edits
        orig_lvl_af = self._get_pristine_assets(slot.level_assets_file)
        lvl_data = BeatmapLevelData.from_bytes(orig_lvl_af.by_path_id[slot.level_path_id].blob)

        orig_ac_af = self._get_pristine_assets(slot.audio_assets_file)
        ac_data = AudioClipData.from_bytes(orig_ac_af.by_path_id[slot.audio_path_id].blob)

        has_preview_obj = slot.preview_assets_file is not None and slot.preview_path_id is not None
        prev_lvl_data: Optional[PreviewBeatmapLevelData] = None
        prev_ac_file: Optional[str] = None
        prev_ac_pid: Optional[int] = None
        prev_ac_data: Optional[AudioClipData] = None

        if has_preview_obj:
            orig_prev_af = self._get_pristine_assets(slot.preview_assets_file)  # type: ignore[arg-type]
            prev_lvl_data = PreviewBeatmapLevelData.from_bytes(
                orig_prev_af.by_path_id[slot.preview_path_id].blob  # type: ignore[index]
            )
            p_fid, p_pid = prev_lvl_data.preview_audio_clip
            if p_pid != 0:
                prev_ac_file, prev_ac_pid = self._resolve_pptr(
                    slot.preview_assets_file, (p_fid, p_pid), use_pristine=True  # type: ignore[arg-type]
                )
                prev_ac_data = AudioClipData.from_bytes(
                    self._get_pristine_assets(prev_ac_file).by_path_id[prev_ac_pid].blob
                )

        # Encode audio to PS5 lossless PCM16 FSB5
        log("Encoding audio to lossless PS5 PCM16 FSB5...")
        main_fsb5, prev_fsb5 = encode_audio_file_to_fsb5(
            pkg.audio_bytes,
            filename_hint=pkg.audio_filename,
            preview_start=pkg.preview_start_time,
            preview_duration=pkg.preview_duration,
            need_preview_clip=(prev_ac_data is not None),
        )
        log(
            f"Encoded FSB5: {main_fsb5.duration_seconds:.1f}s @ {main_fsb5.sample_rate} Hz stereo "
            f"({len(main_fsb5.fsb5_bytes) / (1024 * 1024):.2f} MB, {main_fsb5.loudness_lufs:.1f} LUFS)"
        )

        # Free any previous custom tail allocation for this slot before writing new FSB5
        self._free_slot_allocations(slot.level_id)

        target_res_fn = "customsongs.resource" if use_separate_resource else ac_data.source
        main_offset = self._write_resource_block(
            target_res_fn, slot.level_id, "main", main_fsb5.fsb5_bytes
        )

        ac_data.channels = 2
        ac_data.frequency = main_fsb5.sample_rate
        ac_data.bits_per_sample = 16
        ac_data.length = float(main_fsb5.duration_seconds)
        ac_data.source = target_res_fn
        ac_data.offset = main_offset
        ac_data.size = len(main_fsb5.fsb5_bytes)
        ac_data.compression_format = 0  # AudioCompressionFormat.PCM (matches PS5 FSB5 codec=2)

        live_ac_af = self._get_live_assets(slot.audio_assets_file)
        live_ac_af.set_object_blob(slot.audio_path_id, ac_data.to_bytes())

        # Clamp preview timestamps cleanly inside song duration
        p_start = pkg.preview_start_time
        p_dur = pkg.preview_duration if pkg.preview_duration > 1.0 else 10.0
        if p_start < 0.0 or p_start + 1.0 >= main_fsb5.duration_seconds:
            p_start = max(0.0, min(main_fsb5.duration_seconds - p_dur, main_fsb5.duration_seconds * 0.25))
        p_dur = max(1.0, min(p_dur, main_fsb5.duration_seconds - p_start))

        # Update optional DLC PreviewBeatmapLevelSO + *Preview AudioClip
        if prev_lvl_data is not None and slot.preview_assets_file is not None and slot.preview_path_id is not None:
            if prev_ac_data is not None and prev_fsb5 is not None and prev_ac_file and prev_ac_pid is not None:
                prev_res_fn = "customsongs.resource" if use_separate_resource else prev_ac_data.source
                prev_offset = self._write_resource_block(
                    prev_res_fn, slot.level_id, "preview", prev_fsb5.fsb5_bytes
                )
                prev_ac_data.channels = 2
                prev_ac_data.frequency = prev_fsb5.sample_rate
                prev_ac_data.bits_per_sample = 16
                prev_ac_data.length = float(prev_fsb5.duration_seconds)
                prev_ac_data.source = prev_res_fn
                prev_ac_data.offset = prev_offset
                prev_ac_data.size = len(prev_fsb5.fsb5_bytes)
                prev_ac_data.compression_format = 0
                self._get_live_assets(prev_ac_file).set_object_blob(prev_ac_pid, prev_ac_data.to_bytes())
                prev_lvl_data.preview_start_time = 0.0
                prev_lvl_data.preview_duration = float(prev_fsb5.duration_seconds)
            else:
                prev_lvl_data.preview_start_time = float(p_start)
                prev_lvl_data.preview_duration = float(p_dur)

            prev_lvl_data.song_name = final_title
            prev_lvl_data.song_sub_name = pkg.song_sub_name
            prev_lvl_data.song_author_name = final_artist
            prev_lvl_data.level_author_name = final_mapper
            prev_lvl_data.bpm = float(pkg.bpm)
            prev_lvl_data.song_time_offset = float(pkg.song_time_offset)
            prev_lvl_data.shuffle = float(pkg.shuffle)
            prev_lvl_data.shuffle_period = float(pkg.shuffle_period)
            prev_lvl_data.song_duration = float(main_fsb5.duration_seconds)
            self._get_live_assets(slot.preview_assets_file).set_object_blob(
                slot.preview_path_id, prev_lvl_data.to_bytes()
            )

        # Prepare Standard 5-difficulty chart table
        base_std = pkg.charts.get("Standard") or next(iter(pkg.charts.values()))
        if auto_generate_difficulties:
            std_5 = ensure_all_five_difficulties(base_std, pkg.bpm)
        else:
            # Fill missing difficulties with closest existing mapper chart so stock notes never leak
            std_5 = {}
            avail = sorted(base_std.keys())
            for d in range(5):
                if d in base_std:
                    std_5[d] = base_std[d]
                else:
                    nearest = min(avail, key=lambda x: abs(x - d))
                    std_5[d] = base_std[nearest]

        # Populate every DifficultyBeatmapSetEntry & BeatmapDataSO attached to this PS5 slot
        log("Converting beatmaps to PS5 V3.3.0 UTF-16LE gzipped format...")
        populated_summary = []
        for dset in lvl_data.difficulty_sets:
            ch_file, ch_pid = self._resolve_pptr(
                slot.level_assets_file, dset.characteristic, use_pristine=True
            )
            mode_name = self._char_map.get((ch_file, ch_pid), "Standard")
            lookup_mode = "Standard" if mode_name == "LegacyStandard" else mode_name

            if lookup_mode in pkg.charts:
                mode_table = (
                    ensure_all_five_difficulties(pkg.charts[lookup_mode], pkg.bpm)
                    if auto_generate_difficulties
                    else {
                        d: pkg.charts[lookup_mode].get(
                            d, pkg.charts[lookup_mode][min(pkg.charts[lookup_mode].keys(), key=lambda x: abs(x - d))]
                        )
                        for d in range(5)
                    }
                )
            elif auto_generate_modes and lookup_mode in ("OneSaber", "NoArrows", "90Degree", "360Degree"):
                mode_table = {
                    d: generate_mode_from_standard(std_5[d], lookup_mode) for d in range(5)
                }
            else:
                mode_table = std_5

            for df in dset.difficulties:
                chart: ParsedDifficultyChart = mode_table[df.difficulty]
                df.njs = float(chart.njs)
                df.njs_offset = float(chart.njs_offset)

                bd_file, bd_pid = self._resolve_pptr(
                    slot.level_assets_file, df.beatmap_data, use_pristine=True
                )
                orig_bd_blob = self._get_pristine_assets(bd_file).by_path_id[bd_pid].blob
                bd_obj = BeatmapDataObject.from_bytes(orig_bd_blob)
                bd_obj.set_json_text(serialize_v3_chart_json(chart.v3_dict))
                self._get_live_assets(bd_file).set_object_blob(bd_pid, bd_obj.to_bytes())

                note_cnt = len(chart.v3_dict.get("colorNotes", []))
                tag = "gen" if chart.is_generated else "native"
                populated_summary.append(f"{mode_name}/{DIFF_NAMES[df.difficulty]}({note_cnt}n,{tag})")

        # Update BeatmapLevelSO metadata & difficulty sets
        lvl_data.song_name = final_title
        lvl_data.song_sub_name = pkg.song_sub_name
        lvl_data.song_author_name = final_artist
        lvl_data.level_author_name = final_mapper
        lvl_data.bpm = float(pkg.bpm)
        lvl_data.song_time_offset = float(pkg.song_time_offset)
        lvl_data.shuffle = float(pkg.shuffle)
        lvl_data.shuffle_period = float(pkg.shuffle_period)
        lvl_data.preview_start_time = float(p_start)
        lvl_data.preview_duration = float(p_dur)

        self._get_live_assets(slot.level_assets_file).set_object_blob(
            slot.level_path_id, lvl_data.to_bytes()
        )

        # Update PerceivedLoudnessPerLevel so in-game volume matches official tracks
        if self._loudness_pid is not None:
            sa48_live = self._get_live_assets("sharedassets48.assets")
            ltable = PerceivedLoudnessTable.from_bytes(sa48_live.by_path_id[self._loudness_pid].blob)
            ltable.set_loudness(slot.level_id, main_fsb5.loudness_lufs)
            sa48_live.set_object_blob(self._loudness_pid, ltable.to_bytes())

        # Flush all modified .assets files atomically
        dirty_files = [af.filename for af in self._live_assets.values() if af.dirty]
        for af in list(self._live_assets.values()):
            if af.dirty:
                log(f"Saving {af.filename} atomically...")
                af.save()

        self.state.setdefault("installed_slots", {})[slot.level_id] = {
            "level_id": slot.level_id,
            "pack_id": slot.pack_id,
            "pack_name": slot.pack_name,
            "stock_song_name": slot.stock_song_name,
            "stock_song_author": slot.stock_song_author,
            "custom_song_name": final_title,
            "custom_song_sub_name": pkg.song_sub_name,
            "custom_song_author": final_artist,
            "custom_level_author": final_mapper,
            "bpm": round(pkg.bpm, 2),
            "duration_seconds": round(main_fsb5.duration_seconds, 2),
            "resource_file": target_res_fn,
            "audio_offset": main_offset,
            "audio_size": len(main_fsb5.fsb5_bytes),
            "charts_installed": populated_summary,
            "modified_assets": dirty_files,
            "installed_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        self._save_state()
        self.refresh_catalog()

        # Run automatic post-install verification ("No mistakes")
        self.verify_slot(slot.level_id)
        log(f"Verified & installed '{final_title}' -> slot '{slot.stock_song_name}' [{slot.pack_name}]!")
        return self.slots[slot.level_id]

    # ==========================================================================
    # Surgical Single-Slot Restore & Full Factory Restore
    # ==========================================================================

    def restore_slot(self, target_level_id: str, log_fn: Optional[Callable[[str], None]] = None) -> None:
        if target_level_id not in self.slots:
            matches = [k for k in self.slots if k.lower() == target_level_id.lower()]
            if len(matches) == 1:
                target_level_id = matches[0]
            else:
                raise KeyError(f"Unknown PS5 song slot '{target_level_id}'")

        if not self.has_backup():
            raise RuntimeError("No backup exists yet — game files are already in stock state.")

        slot = self.slots[target_level_id]
        orig_lvl_af = self._get_pristine_assets(slot.level_assets_file)
        orig_lvl_blob = orig_lvl_af.by_path_id[slot.level_path_id].blob
        orig_lvl = BeatmapLevelData.from_bytes(orig_lvl_blob)

        # Restore BeatmapLevelSO
        self._get_live_assets(slot.level_assets_file).set_object_blob(slot.level_path_id, orig_lvl_blob)

        # Restore main AudioClip
        orig_ac_blob = self._get_pristine_assets(slot.audio_assets_file).by_path_id[slot.audio_path_id].blob
        self._get_live_assets(slot.audio_assets_file).set_object_blob(slot.audio_path_id, orig_ac_blob)

        # Restore all BeatmapDataSOs
        for dset in orig_lvl.difficulty_sets:
            for df in dset.difficulties:
                bd_file, bd_pid = self._resolve_pptr(
                    slot.level_assets_file, df.beatmap_data, use_pristine=True
                )
                orig_bd_blob = self._get_pristine_assets(bd_file).by_path_id[bd_pid].blob
                self._get_live_assets(bd_file).set_object_blob(bd_pid, orig_bd_blob)

        # Restore PreviewBeatmapLevelSO + *Preview AudioClip if present
        if slot.preview_assets_file and slot.preview_path_id is not None:
            orig_prev_blob = (
                self._get_pristine_assets(slot.preview_assets_file).by_path_id[slot.preview_path_id].blob
            )
            self._get_live_assets(slot.preview_assets_file).set_object_blob(
                slot.preview_path_id, orig_prev_blob
            )
            prev_obj = PreviewBeatmapLevelData.from_bytes(orig_prev_blob)
            if prev_obj.preview_audio_clip[1] != 0:
                pac_file, pac_pid = self._resolve_pptr(
                    slot.preview_assets_file, prev_obj.preview_audio_clip, use_pristine=True
                )
                orig_pac_blob = self._get_pristine_assets(pac_file).by_path_id[pac_pid].blob
                self._get_live_assets(pac_file).set_object_blob(pac_pid, orig_pac_blob)

        # Restore PerceivedLoudnessPerLevel entry
        if self._loudness_pid is not None:
            orig_lt = PerceivedLoudnessTable.from_bytes(
                self._get_pristine_assets("sharedassets48.assets").by_path_id[self._loudness_pid].blob
            )
            orig_map = {lid: lufs for lid, lufs, _ in orig_lt.entries}
            if slot.level_id in orig_map:
                sa48_live = self._get_live_assets("sharedassets48.assets")
                live_lt = PerceivedLoudnessTable.from_bytes(sa48_live.by_path_id[self._loudness_pid].blob)
                live_lt.set_loudness(slot.level_id, orig_map[slot.level_id])
                sa48_live.set_object_blob(self._loudness_pid, live_lt.to_bytes())

        for af in list(self._live_assets.values()):
            if af.dirty:
                af.save()

        self._free_slot_allocations(slot.level_id)
        self.state.get("installed_slots", {}).pop(slot.level_id, None)
        self._save_state()
        self.refresh_catalog()
        if log_fn:
            log_fn(f"Restored '{slot.stock_song_name}' [{slot.pack_name}] back to 100% original stock.")

    def restore_all(self, log_fn: Optional[Callable[[str], None]] = None) -> None:
        if not self.has_backup():
            if log_fn:
                log_fn("No custom changes found — game is already 100% original stock.")
            return

        for fn in TRACKED_ASSETS_FILES:
            bak = os.path.join(self.backup_dir, fn + ".orig")
            dst = os.path.join(self.media_dir, fn)
            if os.path.isfile(bak):
                if log_fn:
                    log_fn(f"Restoring original {fn}...")
                shutil.copy2(bak, dst)

        orig_sizes = self.state.get("original_resource_sizes", {})
        for rfn, orig_sz in orig_sizes.items():
            rp = os.path.join(self.media_dir, rfn)
            if os.path.isfile(rp) and os.path.getsize(rp) != orig_sz:
                if log_fn:
                    log_fn(f"Truncating {rfn} back to exact original size ({orig_sz:,} bytes)...")
                with open(rp, "r+b") as f:
                    f.truncate(orig_sz)

        sep_res = os.path.join(self.media_dir, "customsongs.resource")
        if os.path.isfile(sep_res):
            os.remove(sep_res)

        self.state["installed_slots"] = {}
        self.state["resource_allocations"] = {}
        self._save_state()
        self.refresh_catalog()
        if log_fn:
            log_fn("All PS5 Beat Saber files restored to 100% original factory state.")

    # ==========================================================================
    # Integrity Verification ("No mistakes")
    # ==========================================================================

    def verify_slot(self, level_id: str) -> dict:
        slot = self.slots[level_id]
        lvl_af = self._get_live_assets(slot.level_assets_file)
        lvl = BeatmapLevelData.from_bytes(lvl_af.by_path_id[slot.level_path_id].blob)

        ac_af = self._get_live_assets(slot.audio_assets_file)
        ac = AudioClipData.from_bytes(ac_af.by_path_id[slot.audio_path_id].blob)
        res_path = os.path.join(self.media_dir, ac.source)
        if not os.path.isfile(res_path):
            raise RuntimeError(f"Missing audio resource file: {res_path}")
        res_len = os.path.getsize(res_path)
        if ac.offset + ac.size > res_len:
            raise RuntimeError(
                f"AudioClip '{ac.name}' out of bounds: {ac.offset}+{ac.size} > {res_len}"
            )

        with open(res_path, "rb") as rf:
            rf.seek(ac.offset)
            fsb_bytes = rf.read(ac.size)
        fsb_meta = verify_fsb5_header(fsb_bytes)

        checked_charts = 0
        for dset in lvl.difficulty_sets:
            for df in dset.difficulties:
                bd_file, bd_pid = self._resolve_pptr(slot.level_assets_file, df.beatmap_data)
                bd_blob = self._get_live_assets(bd_file).by_path_id[bd_pid].blob
                bd_obj = BeatmapDataObject.from_bytes(bd_blob)
                parsed_json = json.loads(bd_obj.decode_json_text())
                if "version" not in parsed_json and "_version" not in parsed_json:
                    raise RuntimeError(f"Invalid beatmap JSON in {bd_obj.m_name}")
                checked_charts += 1

        return {
            "level_id": level_id,
            "song_name": lvl.song_name,
            "audio_source": ac.source,
            "audio_codec": fsb_meta["codec"],
            "sample_rate": fsb_meta["sample_rate"],
            "duration": round(ac.length, 2),
            "charts_verified": checked_charts,
        }

    def verify_all_installed(self) -> List[dict]:
        results = []
        for lid in sorted(self.state.get("installed_slots", {}).keys()):
            results.append(self.verify_slot(lid))
        return results

    # ==========================================================================
    # Printable Party Song List & Optional PS5 FTP Push
    # ==========================================================================

    def export_song_list_html(self, out_path: str) -> str:
        installed = list(self.state.get("installed_slots", {}).values())
        installed.sort(key=lambda x: (x["pack_name"], x["custom_song_name"]))
        rows = []
        for item in installed:
            mins, secs = divmod(int(round(item.get("duration_seconds", 0))), 60)
            rows.append(
                f"<tr>"
                f"<td><strong>{item['custom_song_name']}</strong></td>"
                f"<td>{item['custom_song_author']}</td>"
                f"<td>{item['bpm']}</td>"
                f"<td>{mins}:{secs:02d}</td>"
                f"<td>{item['pack_name']}</td>"
                f"<td style='color:#9aa4bf'>{item['stock_song_name']}</td>"
                f"</tr>"
            )
        body_rows = "\n".join(rows) or "<tr><td colspan='6'>No custom songs installed yet.</td></tr>"
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PS5 Beat Saber Custom Song Lineup</title>
<style>
  body {{ background:#0d0f17; color:#eef2ff; font-family:Segoe UI,system-ui,sans-serif; padding:32px; }}
  h1 {{ color:#00a8ff; margin-bottom:6px; }}
  .sub {{ color:#ff2a54; font-weight:600; margin-bottom:24px; }}
  table {{ width:100%; border-collapse:collapse; background:#161a28; border-radius:8px; overflow:hidden; }}
  th {{ background:#1f2538; color:#00a8ff; text-align:left; padding:12px 14px; }}
  td {{ padding:10px 14px; border-top:1px solid #252c42; }}
</style>
</head>
<body>
  <h1>PS5 Beat Saber (PSVR2) — Custom Song Lineup</h1>
  <div class="sub">{len(installed)} Custom Songs Installed ({self.title_id})</div>
  <table>
    <thead>
      <tr>
        <th>Custom Song</th><th>Artist</th><th>BPM</th><th>Length</th><th>Music Pack</th><th>Replaced Slot</th>
      </tr>
    </thead>
    <tbody>
      {body_rows}
    </tbody>
  </table>
</body>
</html>"""
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        return out_path

    def deploy_via_ftp(
        self,
        ps5_ip: str,
        ps5_port: int,
        remote_app_dir: str,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        Push only modified .assets files and appended .resource tail bytes to a modded PS5
        running ftpsrv / etaHEN FTP (avoids re-uploading the 2.92 GB base resource!).
        """
        def log(msg: str) -> None:
            if log_fn:
                log_fn(msg)

        installed = self.state.get("installed_slots", {})
        if not installed:
            raise RuntimeError("No custom songs are currently installed to deploy.")

        remote_media = remote_app_dir.rstrip("/") + "/Media"
        modified_assets = set()
        for info in installed.values():
            for afn in info.get("modified_assets", []):
                modified_assets.add(afn)

        log(f"Connecting to PS5 FTP at {ps5_ip}:{ps5_port}...")
        with ftplib.FTP() as ftp:
            ftp.connect(ps5_ip, ps5_port, timeout=15)
            ftp.login("anonymous", "")
            ftp.cwd(remote_media)

            for afn in sorted(modified_assets):
                local_p = os.path.join(self.media_dir, afn)
                log(f"Uploading {afn} ({os.path.getsize(local_p) / (1024 * 1024):.1f} MB)...")
                with open(local_p, "rb") as f:
                    ftp.storbinary(f"STOR {afn}", f, blocksize=262144)

            orig_sizes = self.state.get("original_resource_sizes", {})
            for rfn, blocks in self.state.get("resource_allocations", {}).items():
                if not blocks:
                    continue
                local_rp = os.path.join(self.media_dir, rfn)
                if rfn == "customsongs.resource":
                    log(f"Uploading {rfn} ({os.path.getsize(local_rp) / (1024 * 1024):.1f} MB)...")
                    with open(local_rp, "rb") as f:
                        ftp.storbinary(f"STOR {rfn}", f, blocksize=262144)
                else:
                    base_sz = orig_sizes.get(rfn, 0)
                    total_sz = os.path.getsize(local_rp)
                    tail_len = total_sz - base_sz
                    if tail_len > 0:
                        log(
                            f"Patching tail of {rfn} at offset {base_sz:,} "
                            f"(uploading only {tail_len / (1024 * 1024):.1f} MB instead of 2.9 GB)..."
                        )
                        with open(local_rp, "rb") as f:
                            f.seek(base_sz)
                            tail_bio = io.BytesIO(f.read(tail_len))
                        ftp.storbinary(f"STOR {rfn}", tail_bio, blocksize=262144, rest=base_sz)

        log("FTP push to PS5 completed!")
