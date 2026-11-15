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
from .auto_mapper import generate_track_from_audio_file
from .beatmap_converter import (
    DIFF_NAMES,
    CustomSongPackage,
    ParsedDifficultyChart,
    ensure_all_five_difficulties,
    generate_mode_from_standard,
    load_custom_map,
    serialize_v3_chart_json,
)
from .beatsaver_client import download_beatsaver_zip
from .bundle_v137 import (
    CLASSIC_LIGHT_STAGES_137,
    UnityFsBundle,
    build_v137_song_bundle,
    patch_v137_pack_bundle,
    set_catalog_crc_zero_same_length,
    update_ampr_index_sizes,
)
from .covers_v137 import patch_sharedassets_covers
from .vorbis_fsb5 import encode_song_and_preview_vorbis_fsb5
from .stage_modder import STAGE_MESH_TARGETS, build_unity_ps5_mesh_blob, load_wavefront_obj
from .unity_assets import (
    AudioClipData,
    BeatmapDataObject,
    BeatmapLevelData,
    ColorSchemeData,
    EnvironmentInfoEntry,
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
    "resources.assets",
    "sharedassets3.assets",
    "sharedassets48.assets",
    "sharedassets43.assets",
    "sharedassets44.assets",
    "sharedassets21.assets",
]

COLOR_THEME_PRESETS: Dict[str, Dict[str, Tuple[float, float, float, float]]] = {
    "Cyberpunk Neon (Pink & Cyan)": {
        "saber_a": (1.00, 0.12, 0.48, 1.0),
        "saber_b": (0.00, 0.94, 1.00, 1.0),
        "env_color_0": (1.00, 0.10, 0.55, 1.0),
        "env_color_1": (0.00, 0.85, 1.00, 1.0),
        "env_color_0_boost": (0.72, 0.00, 1.00, 1.0),
        "env_color_1_boost": (0.00, 1.00, 0.75, 1.0),
        "obstacles_color": (0.95, 0.05, 0.45, 1.0),
    },
    "Synthwave Sunset (Orange & Violet)": {
        "saber_a": (1.00, 0.42, 0.00, 1.0),
        "saber_b": (0.72, 0.16, 1.00, 1.0),
        "env_color_0": (1.00, 0.28, 0.12, 1.0),
        "env_color_1": (0.62, 0.12, 0.98, 1.0),
        "env_color_0_boost": (1.00, 0.75, 0.00, 1.0),
        "env_color_1_boost": (0.00, 0.88, 1.00, 1.0),
        "obstacles_color": (0.55, 0.08, 0.85, 1.0),
    },
    "Emerald Matrix (Lime & Gold)": {
        "saber_a": (0.00, 0.96, 0.40, 1.0),
        "saber_b": (1.00, 0.82, 0.05, 1.0),
        "env_color_0": (0.00, 0.85, 0.32, 1.0),
        "env_color_1": (0.95, 0.78, 0.10, 1.0),
        "env_color_0_boost": (0.10, 1.00, 0.70, 1.0),
        "env_color_1_boost": (1.00, 0.45, 0.00, 1.0),
        "obstacles_color": (0.00, 0.65, 0.25, 1.0),
    },
    "Miku Stage (Sakura Pink & Teal)": {
        "saber_a": (1.00, 0.30, 0.65, 1.0),
        "saber_b": (0.00, 0.90, 0.80, 1.0),
        "env_color_0": (1.00, 0.35, 0.70, 1.0),
        "env_color_1": (0.00, 0.85, 0.78, 1.0),
        "env_color_0_boost": (1.00, 0.80, 0.20, 1.0),
        "env_color_1_boost": (0.20, 0.65, 1.00, 1.0),
        "obstacles_color": (0.00, 0.70, 0.65, 1.0),
    },
    "Blood Moon & Glacier (Crimson & Ice)": {
        "saber_a": (0.95, 0.06, 0.14, 1.0),
        "saber_b": (0.52, 0.86, 1.00, 1.0),
        "env_color_0": (0.90, 0.05, 0.12, 1.0),
        "env_color_1": (0.45, 0.80, 1.00, 1.0),
        "env_color_0_boost": (1.00, 0.35, 0.05, 1.0),
        "env_color_1_boost": (0.80, 0.95, 1.00, 1.0),
        "obstacles_color": (0.75, 0.04, 0.10, 1.0),
    },
}

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
    pack_bundle_file: Optional[str] = None
    cover_sprite_pid: Optional[int] = None
    is_standard_5diff: bool = True


def _safe_replace_file(tmp_path: str, dst_path: str) -> None:
    """Replace dst_path atomically on Windows even if dst_path carried a read-only attribute from an exFAT/ISO dump."""
    if os.path.isfile(dst_path):
        try:
            os.chmod(dst_path, 0o666)
        except Exception:
            pass
    os.replace(tmp_path, dst_path)


def _is_valid_media_dir(media_dir: str) -> bool:
    return (
        os.path.isfile(os.path.join(media_dir, "sharedassets48.assets"))
        or os.path.isdir(os.path.join(media_dir, "StreamingAssets", "BeatmapLevelsData"))
    )


def find_ps5_game_root(start_path: str) -> str:
    """
    Resolve a user-supplied folder (parent folder, PPSA*-app/patch folder, or Media folder)
    to the exact app root containing either Media/sharedassets48.assets (v1.29-1.34)
    or Media/StreamingAssets/BeatmapLevelsData (v1.35 / v1.37 / v1.40+).
    """
    p = os.path.abspath(start_path)
    if _is_valid_media_dir(p):
        return os.path.dirname(p)
    if _is_valid_media_dir(os.path.join(p, "Media")):
        return p
    if os.path.isdir(p):
        for entry in sorted(os.listdir(p)):
            sub = os.path.join(p, entry)
            if os.path.isdir(sub) and _is_valid_media_dir(os.path.join(sub, "Media")):
                return sub
            if os.path.isdir(sub):
                for sub2_name in sorted(os.listdir(sub)):
                    sub2 = os.path.join(sub, sub2_name)
                    if os.path.isdir(sub2) and _is_valid_media_dir(os.path.join(sub2, "Media")):
                        return sub2
    raise FileNotFoundError(
        f"Could not find Beat Saber Media assets or StreamingAssets/BeatmapLevelsData under: {start_path}"
    )


class PS5BeatSaberManager:
    """Manages custom song installation, backups, restoration, and verification on a PS5 dump."""

    def __init__(self, game_path: str, backup_root: Optional[str] = None):
        self.app_root = find_ps5_game_root(game_path)
        self.media_dir = os.path.join(self.app_root, "Media")
        self.levels_bundle_dir = os.path.join(self.media_dir, "StreamingAssets", "BeatmapLevelsData")
        self.aa_ps5_dir = os.path.join(self.media_dir, "StreamingAssets", "aa", "PS5")
        self.catalog_json_path = os.path.join(self.media_dir, "StreamingAssets", "aa", "catalog.json")
        self.ampr_index_path = os.path.join(self.app_root, "ampr_emu.index")

        # Section 8.1 Detection:
        # if Media/sharedassets48.assets exists -> 1.12 path (unchanged)
        # elif StreamingAssets/BeatmapLevelsData/ + aa/catalog.json -> 1.37 backend
        if os.path.isfile(os.path.join(self.media_dir, "sharedassets48.assets")):
            self.engine_mode = "assets_v134"
        elif os.path.isdir(self.levels_bundle_dir) and os.path.isfile(self.catalog_json_path):
            self.engine_mode = "bundle_v137"
        else:
            raise FileNotFoundError(f"Unrecognized Beat Saber layout in {self.app_root}")

        self.title_id, self.content_version = self._read_param_json()

        # Store backups OUTSIDE PPSA*-app; isolate 1.37 backups from 1.12 backups cleanly
        if backup_root:
            self.backup_dir = os.path.abspath(backup_root)
        else:
            parent_dir = os.path.dirname(self.app_root)
            if self.engine_mode == "bundle_v137":
                # Keep outside PPSA*-app even when nested like "PPSA15035-app 1.37/PPSA15035-app"
                bak_key = f"{self.title_id}_{self.content_version}_v137"
                self.backup_dir = os.path.join(parent_dir, "_ps5_custom_converter_backups", bak_key)
            else:
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
        self._v137_pristine_slots_cache: Optional[List[dict]] = None
        self.environments: Dict[str, EnvironmentInfoEntry] = {}
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
        """Back up original .assets / catalog.json files and record original .resource sizes before first edit."""
        os.makedirs(self.backup_dir, exist_ok=True)
        changed = False

        if self.engine_mode == "bundle_v137":
            for sub in ("songs_orig", "packs_orig", "previews", "covers"):
                os.makedirs(os.path.join(self.backup_dir, sub), exist_ok=True)
            cat_bak = os.path.join(self.backup_dir, "catalog.json.orig")
            if os.path.isfile(self.catalog_json_path) and not os.path.isfile(cat_bak):
                if log_fn:
                    log_fn("Backing up pristine Addressables aa/catalog.json...")
                shutil.copy2(self.catalog_json_path, cat_bak)
                changed = True
            idx_bak = os.path.join(self.backup_dir, "ampr_emu.index.orig")
            if os.path.isfile(self.ampr_index_path) and not os.path.isfile(idx_bak):
                if log_fn:
                    log_fn("Backing up pristine ampr_emu.index...")
                shutil.copy2(self.ampr_index_path, idx_bak)
                changed = True
            if changed or not os.path.isfile(self.state_path):
                self._save_state()
            return

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
        if self.engine_mode == "bundle_v137":
            return os.path.isfile(os.path.join(self.backup_dir, "catalog.json.orig"))
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
        self.environments.clear()
        self.slots.clear()
        self._loudness_pid = None

        # 0. Discover all playable Stage Environments (EnvironmentInfoSO) in resources.assets
        if os.path.isfile(os.path.join(self.media_dir, "resources.assets")):
            import struct as _st0
            res_orig = self._get_pristine_assets("resources.assets")
            for obj in res_orig.objects:
                if obj.class_id == 114 and len(obj.blob) >= 80:
                    try:
                        nm, o1 = read_aligned_str(obj.blob, 28)
                        if ("Environment" in nm) and not nm.endswith("SceneInfo") and not nm.endswith("Type"):
                            disp_name, o2 = read_aligned_str(obj.blob, o1)
                            if o2 + 28 <= len(obj.blob):
                                (_, cs_pid), o3 = read_pptr(obj.blob, o2)
                                _si_pptr, o4 = read_pptr(obj.blob, o3)
                                ser_name, _ = read_aligned_str(obj.blob, o4)
                                if ser_name.endswith("Environment"):
                                    self.environments[ser_name] = EnvironmentInfoEntry(
                                        path_id=obj.path_id,
                                        object_name=nm,
                                        display_name=disp_name,
                                        serialized_name=ser_name,
                                        color_scheme_path_id=cs_pid,
                                        is_all_directions=(ser_name == "GlassDesertEnvironment"),
                                    )
                    except Exception:
                        pass

        if self.engine_mode == "bundle_v137":
            self._refresh_v137_bundle_catalog()
            return

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

    def _refresh_v137_bundle_catalog(self) -> None:
        """
        Index Standard-only 5-difficulty slots across all *_pack_assets_all_*.bundle files in aa/PS5/
        and populate classic-light stages for 1.37.
        """
        # Populate 1.37 classic-light stages
        for idx, sname in enumerate(sorted(CLASSIC_LIGHT_STAGES_137), start=1):
            disp = sname.replace("Environment", "")
            self.environments[sname] = EnvironmentInfoEntry(
                path_id=idx,
                object_name=sname,
                display_name=disp,
                serialized_name=sname,
                color_scheme_path_id=0,
                is_all_directions=False,
            )

        if self._v137_pristine_slots_cache is None:
            import UnityPy

            cached_rows: List[dict] = []
            if os.path.isdir(self.aa_ps5_dir):
                for fn in sorted(os.listdir(self.aa_ps5_dir)):
                    if "_pack_assets_all_" not in fn or fn.startswith("debug_"):
                        continue
                    bak_pack = os.path.join(self.backup_dir, "packs_orig", fn + ".orig")
                    src_pack = bak_pack if os.path.isfile(bak_pack) else os.path.join(self.aa_ps5_dir, fn)
                    with open(src_pack, "rb") as _pf:
                        env = UnityPy.load(_pf.read())
                    bf = list(env.files.values())[0]
                    cab = bf.files[[k for k in bf.files if not k.endswith((".resource", ".resS"))][0]]

                    pack_id = fn.split("_pack_assets_all_")[0]
                    pack_name = pack_id
                    is_free = pack_id.lower().startswith(("ostvol", "extras", "camellia"))
                    levels_in_pack = []

                    for _pid, rdr in cab.objects.items():
                        if rdr.type.name != "MonoBehaviour":
                            continue
                        tt = rdr.read_typetree()
                        if "_packID" in tt and "_packName" in tt:
                            pack_id = str(tt["_packID"])
                            pack_name = str(tt["_packName"])
                            if int(tt.get("_packBuyOption", 1)) == 0:
                                is_free = True
                        elif "_levelID" in tt and "_previewDifficultyBeatmapSets" in tt:
                            levels_in_pack.append(tt)

                    for ltt in levels_in_pack:
                        lid = str(ltt["_levelID"])
                        song_fn = lid.lower()
                        if not os.path.isfile(os.path.join(self.levels_bundle_dir, song_fn)):
                            continue
                        psets = ltt.get("_previewDifficultyBeatmapSets") or []
                        is_std_5 = (
                            len(psets) == 1
                            and len(psets[0].get("_previewDifficultyBeatmaps") or []) == 5
                        )
                        cov_pid = int((ltt.get("_coverImage") or {}).get("m_PathID", 0))
                        cached_rows.append(
                            {
                                "level_id": lid,
                                "song_fn": song_fn,
                                "pack_fn": fn,
                                "pack_id": pack_id,
                                "pack_name": pack_name,
                                "is_free": is_free,
                                "song_name": str(ltt.get("_songName", lid)),
                                "song_sub": str(ltt.get("_songSubName", "")),
                                "song_author": str(ltt.get("_songAuthorName", "")),
                                "level_author": str(ltt.get("_levelAuthorName", "")),
                                "bpm": float(ltt.get("_beatsPerMinute", 120.0)),
                                "cover_pid": cov_pid,
                                "is_std_5": is_std_5,
                            }
                        )
            self._v137_pristine_slots_cache = cached_rows

        installed_map = self.state.get("installed_slots", {})
        for row in self._v137_pristine_slots_cache:
            # Section 8.2 / 9: Only list Standard-only 5-difficulty slots on 1.37
            if not row["is_std_5"]:
                continue
            lid = row["level_id"]
            inst = installed_map.get(lid)
            self.slots[lid] = SongSlotInfo(
                level_id=lid,
                pack_id=row["pack_id"],
                pack_name=row["pack_name"],
                is_free_base_game=row["is_free"],
                stock_song_name=row["song_name"],
                stock_song_sub_name=row["song_sub"],
                stock_song_author=row["song_author"],
                stock_level_author=row["level_author"],
                stock_bpm=row["bpm"],
                current_song_name=inst["custom_song_name"] if inst else row["song_name"],
                current_song_author=inst["custom_song_author"] if inst else row["song_author"],
                current_bpm=float(inst["bpm"]) if inst else row["bpm"],
                level_assets_file=row["song_fn"],
                level_path_id=0,
                audio_assets_file=row["song_fn"],
                audio_path_id=0,
                preview_assets_file=None,
                preview_path_id=None,
                modes=["Standard"],
                is_custom=bool(inst),
                installed_info=inst,
                pack_bundle_file=row["pack_fn"],
                cover_sprite_pid=row["cover_pid"],
                is_standard_5diff=True,
            )

    def list_slots(
        self,
        free_only: bool = False,
        pack_id: Optional[str] = None,
        pack_category: str = "all",
    ) -> List[SongSlotInfo]:
        items = list(self.slots.values())
        cat = "free" if free_only else (pack_category or "all").lower()
        if cat == "free":
            items = [s for s in items if s.is_free_base_game]
        elif cat == "dlc":
            items = [s for s in items if not s.is_free_base_game]
        if pack_id:
            items = [s for s in items if s.pack_id.lower() == pack_id.lower()]
        items.sort(key=lambda s: (0 if s.is_free_base_game else 1, s.pack_name, s.stock_song_name))
        return items

    def list_environments(self, include_all_directions: bool = False) -> List[EnvironmentInfoEntry]:
        envs = [
            e for e in self.environments.values()
            if (include_all_directions or not e.is_all_directions)
            and e.serialized_name != "MultiplayerEnvironment"
        ]
        envs.sort(key=lambda e: e.display_name.lower())
        return envs

    def resolve_environment_choice(self, choice: Optional[str]) -> Optional[EnvironmentInfoEntry]:
        if not choice:
            return None
        q = choice.strip()
        if q in self.environments:
            return self.environments[q]
        low = q.lower()
        for env in self.environments.values():
            if env.serialized_name.lower() == low or env.display_name.lower() == low:
                return env
        return None

    def _find_external_file_id(self, host_assets_file: str, external_filename: str) -> Optional[int]:
        af = self._get_pristine_assets(host_assets_file)
        for idx, ext_name in enumerate(af.externals, start=1):
            if ext_name.lower() == external_filename.lower():
                return idx
        return None

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
        environment_override: str = "auto",
        install_cover_art: bool = False,
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
                if self.engine_mode == "bundle_v137" and self._v137_pristine_slots_cache:
                    multi = [
                        r for r in self._v137_pristine_slots_cache
                        if r["level_id"].lower() == target_level_id.lower() and not r["is_std_5"]
                    ]
                    if multi:
                        raise ValueError(
                            f"1.37 slot guard: '{multi[0]['level_id']}' is a multi-mode slot and cannot be replaced on 1.37 yet."
                        )
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

        if self.engine_mode == "bundle_v137":
            return self._install_v137_bundle_song(
                slot=slot,
                pkg=pkg,
                final_title=final_title,
                final_artist=final_artist,
                final_mapper=final_mapper,
                auto_generate_difficulties=auto_generate_difficulties,
                environment_override=environment_override,
                install_cover_art=install_cover_art,
                log=log,
            )

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

        # Resolve target Stage Environment (auto = match mapper's stage if known on PS5, keep = keep slot stage)
        chosen_env: Optional[EnvironmentInfoEntry] = None
        env_choice = (environment_override or "auto").strip()
        if env_choice.lower() == "auto":
            mapper_env = getattr(pkg, "environment_name", "") or ""
            if mapper_env:
                chosen_env = self.resolve_environment_choice(mapper_env)
                if chosen_env and chosen_env.is_all_directions:
                    chosen_env = None
        elif env_choice.lower() not in ("keep", "slot", "default_slot", ""):
            chosen_env = self.resolve_environment_choice(env_choice)
            if chosen_env is None:
                raise ValueError(f"Unknown PS5 stage environment '{environment_override}'")

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
            if chosen_env is not None:
                prev_res_fid = self._find_external_file_id(slot.preview_assets_file, "resources.assets")
                if prev_res_fid is not None:
                    prev_lvl_data.environment_info = (prev_res_fid, chosen_env.path_id)
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
        if chosen_env is not None:
            lvl_res_fid = self._find_external_file_id(slot.level_assets_file, "resources.assets")
            if lvl_res_fid is not None:
                lvl_data.environment_info = (lvl_res_fid, chosen_env.path_id)
                log(f"Stage Arena set to '{chosen_env.display_name}' ({chosen_env.serialized_name})")

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
            "stage_environment": chosen_env.serialized_name if chosen_env else "Slot Default",
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

    def _install_v137_bundle_song(
        self,
        slot: SongSlotInfo,
        pkg: CustomSongPackage,
        final_title: str,
        final_artist: str,
        final_mapper: str,
        auto_generate_difficulties: bool,
        environment_override: str,
        install_cover_art: bool,
        log: Callable[[str], None],
    ) -> SongSlotInfo:
        """
        Complete 1.37 Hardware-Proven Install Pipeline (Steps A, B, C, D, E, F, and optional G).
        """
        song_fn = slot.level_assets_file
        pack_fn = slot.pack_bundle_file
        if not pack_fn:
            raise RuntimeError(f"Missing pack bundle mapping for 1.37 slot '{slot.level_id}'")

        live_song_path = os.path.join(self.levels_bundle_dir, song_fn)
        bak_song_path = os.path.join(self.backup_dir, "songs_orig", song_fn + ".orig")
        if not os.path.isfile(bak_song_path):
            log(f"Backing up pristine song bundle '{song_fn}'...")
            shutil.copy2(live_song_path, bak_song_path)

        live_pack_path = os.path.join(self.aa_ps5_dir, pack_fn)
        bak_pack_path = os.path.join(self.backup_dir, "packs_orig", pack_fn + ".orig")
        if not os.path.isfile(bak_pack_path):
            log(f"Backing up pristine level-pack bundle '{pack_fn}'...")
            shutil.copy2(live_pack_path, bak_pack_path)

        # Step B: Encode Vorbis FSB5 (0x6D39BF3E setup CRC + 0-sample sync check) + 10s preview
        log("Encoding Vorbis FSB5 audio (0x6D39BF3E setup CRC + 0-offset sync check)...")
        main_fsb, prev_fsb = encode_song_and_preview_vorbis_fsb5(
            pkg.audio_bytes,
            filename_hint=pkg.audio_filename,
            preview_start=pkg.preview_start_time,
            preview_duration=10.0,
        )
        log(
            f"Encoded Vorbis FSB5: {main_fsb.duration_seconds:.1f}s @ 44.1 kHz "
            f"({len(main_fsb.fsb5_bytes) / (1024 * 1024):.2f} MB, {main_fsb.loudness_lufs:.1f} LUFS)"
        )

        # Prepare 5 Standard difficulties
        base_std = pkg.charts.get("Standard") or next(iter(pkg.charts.values()))
        if auto_generate_difficulties:
            std_5 = ensure_all_five_difficulties(base_std, pkg.bpm)
        else:
            avail = sorted(base_std.keys())
            std_5 = {d: base_std.get(d, base_std[min(avail, key=lambda x: abs(x - d))]) for d in range(5)}

        # Steps A & C: Rewrite slot song bundle (tempo-flattened v4.0.0 beatmaps, lightshows, audio.gz)
        log(f"Building v1.37 song bundle '{song_fn}' (v4.0.0 JSON + tempo flattening)...")
        with open(bak_song_path, "rb") as f:
            pristine_song_bytes = f.read()
        new_song_bytes, diff_stats = build_v137_song_bundle(
            pristine_song_bytes, main_fsb, std_5, pkg.bpm
        )
        tmp_s = live_song_path + ".tmp_write"
        with open(tmp_s, "wb") as f:
            f.write(new_song_bytes)
            f.flush()
            os.fsync(f.fileno())
        _safe_replace_file(tmp_s, live_song_path)

        # Persist preview .fsb & optional cover image in backup_dir for incremental pack rebuilds
        prev_cache_path = os.path.join(self.backup_dir, "previews", f"{slot.level_id}.preview.fsb")
        with open(prev_cache_path, "wb") as f:
            f.write(prev_fsb.fsb5_bytes)

        has_cover = False
        cov_cache_path = os.path.join(self.backup_dir, "covers", f"{slot.level_id}.cover.bin")
        if install_cover_art and pkg.cover_bytes and slot.cover_sprite_pid:
            with open(cov_cache_path, "wb") as f:
                f.write(pkg.cover_bytes)
            has_cover = True
        elif os.path.isfile(cov_cache_path) and not install_cover_art:
            os.remove(cov_cache_path)

        eff_stage = environment_override
        if eff_stage.lower() == "auto":
            eff_stage = pkg.environment_name if pkg.environment_name in CLASSIC_LIGHT_STAGES_137 else "Slot Default"

        self.state.setdefault("installed_slots", {})[slot.level_id] = {
            "level_id": slot.level_id,
            "pack_id": slot.pack_id,
            "pack_name": slot.pack_name,
            "pack_bundle_file": pack_fn,
            "song_bundle_file": song_fn,
            "stock_song_name": slot.stock_song_name,
            "stock_song_author": slot.stock_song_author,
            "custom_song_name": final_title,
            "custom_song_sub_name": pkg.song_sub_name,
            "custom_song_author": final_artist,
            "custom_level_author": final_mapper,
            "bpm": round(pkg.bpm, 2),
            "lufs": round(main_fsb.loudness_lufs, 2),
            "duration_seconds": round(main_fsb.duration_seconds, 4),
            "preview_duration": round(prev_fsb.duration_seconds, 4),
            "environment_override": environment_override,
            "mapper_environment": pkg.environment_name,
            "stage_environment": eff_stage,
            "diff_stats": {str(k): v for k, v in diff_stats.items()},
            "cover_sprite_pid": slot.cover_sprite_pid,
            "has_custom_cover": has_cover,
            "audio_size": len(main_fsb.fsb5_bytes),
            "bundle_size": len(new_song_bytes),
            "charts_installed": [f"Standard/{DIFF_NAMES[d]}({diff_stats[d]['notes_count']}n)" for d in range(5)],
            "modified_assets": [],
            "installed_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }

        # Steps D, E, F, and optional G: Rebuild touched pack, optional covers, catalog CRC=0, and ampr_emu.index!
        self._sync_v137_packs_catalog_and_index(touched_packs={pack_fn}, rebuild_covers=has_cover, log_fn=log)
        self._save_state()
        self.refresh_catalog()
        self.verify_slot(slot.level_id)
        log(f"Verified & installed 1.37 song '{final_title}' -> '{slot.stock_song_name}' [{slot.pack_name}]!")
        return self.slots[slot.level_id]

    def _find_v137_sharedassets_bundle(self) -> Optional[str]:
        if not os.path.isdir(self.aa_ps5_dir):
            return None
        for fn in os.listdir(self.aa_ps5_dir):
            if fn.startswith("sharedassets_assets_all_") and fn.endswith(".bundle"):
                return fn
        return None

    def _sync_v137_packs_catalog_and_index(
        self,
        touched_packs: set[str],
        rebuild_covers: bool = False,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        Execute Steps D, E, F (and optional Step G) coherently across the 1.37 dump.
        """
        def log(msg: str) -> None:
            if log_fn:
                log_fn(msg)

        installed = self.state.get("installed_slots", {})

        # Group active custom slots by their pack_bundle_file
        by_pack: Dict[str, Dict[str, Dict[str, Any]]] = {}
        active_covers: Dict[int, bytes] = {}
        for lid, info in installed.items():
            pfn = info.get("pack_bundle_file")
            if not pfn:
                continue
            prev_p = os.path.join(self.backup_dir, "previews", f"{lid}.preview.fsb")
            prev_bytes = open(prev_p, "rb").read() if os.path.isfile(prev_p) else b""
            dstats_int = {int(k): v for k, v in (info.get("diff_stats") or {}).items()}
            by_pack.setdefault(pfn, {})[lid] = {
                "song_name": info["custom_song_name"],
                "song_sub_name": info.get("custom_song_sub_name", ""),
                "song_author_name": info["custom_song_author"],
                "level_author_name": info.get("custom_level_author", ""),
                "bpm": info["bpm"],
                "lufs": info.get("lufs", -10.0),
                "duration": info["duration_seconds"],
                "preview_duration": info.get("preview_duration", 10.0),
                "environment_override": info.get("environment_override", "auto"),
                "mapper_environment": info.get("mapper_environment", ""),
                "preview_fsb_bytes": prev_bytes,
                "diff_stats": dstats_int,
            }
            if info.get("has_custom_cover") and info.get("cover_sprite_pid"):
                cov_p = os.path.join(self.backup_dir, "covers", f"{lid}.cover.bin")
                if os.path.isfile(cov_p):
                    active_covers[int(info["cover_sprite_pid"])] = open(cov_p, "rb").read()

        # Step D: Rebuild each touched pack bundle (or restore pristine if 0 custom slots remain in that pack!)
        for pfn in sorted(touched_packs):
            bak_p = os.path.join(self.backup_dir, "packs_orig", pfn + ".orig")
            live_p = os.path.join(self.aa_ps5_dir, pfn)
            if not os.path.isfile(bak_p):
                continue
            slot_edits = by_pack.get(pfn, {})
            if not slot_edits:
                log(f"Restoring pristine pack bundle '{pfn}' (no custom slots left in pack)...")
                if os.path.isfile(live_p):
                    try:
                        os.chmod(live_p, 0o666)
                    except Exception:
                        pass
                shutil.copy2(bak_p, live_p)
            else:
                log(f"Patching level-pack bundle '{pfn}' ({len(slot_edits)} custom song(s))...")
                pristine_pk = open(bak_p, "rb").read()
                new_pk = patch_v137_pack_bundle(pristine_pk, slot_edits)
                tmp_pk = live_p + ".tmp_write"
                with open(tmp_pk, "wb") as f:
                    f.write(new_pk)
                    f.flush()
                    os.fsync(f.fileno())
                _safe_replace_file(tmp_pk, live_p)

        # Optional Step G: Rebuild sharedassets SongArtworks BC7 cover atlas if requested
        sa_bundle_fn = self._find_v137_sharedassets_bundle()
        if sa_bundle_fn and (rebuild_covers or os.path.isfile(os.path.join(self.backup_dir, "packs_orig", sa_bundle_fn + ".orig"))):
            live_sa = os.path.join(self.aa_ps5_dir, sa_bundle_fn)
            bak_sa = os.path.join(self.backup_dir, "packs_orig", sa_bundle_fn + ".orig")
            if active_covers:
                if not os.path.isfile(bak_sa):
                    log(f"Backing up pristine '{sa_bundle_fn}' before cover paste...")
                    shutil.copy2(live_sa, bak_sa)
                if rebuild_covers:
                    log(f"Pasting {len(active_covers)} custom cover(s) into PS5 tiled BC7 SongArtworks atlas...")
                    patched_sa = patch_sharedassets_covers(open(bak_sa, "rb").read(), active_covers)
                    tmp_sa = live_sa + ".tmp_write"
                    with open(tmp_sa, "wb") as f:
                        f.write(patched_sa)
                    _safe_replace_file(tmp_sa, live_sa)
            elif os.path.isfile(bak_sa):
                if os.path.isfile(live_sa):
                    os.chmod(live_sa, 0o666)
                shutil.copy2(bak_sa, live_sa)

        # Step E: Same-length aa/catalog.json m_Crc = 0 for active modified packs (+ sharedassets if covers active)
        cat_bak = os.path.join(self.backup_dir, "catalog.json.orig")
        if os.path.isfile(cat_bak) and os.path.isfile(self.catalog_json_path):
            crc_off_targets = list(by_pack.keys())
            if active_covers and sa_bundle_fn:
                crc_off_targets.append(sa_bundle_fn)
            orig_cat_txt = open(cat_bak, "r", encoding="utf-8").read()
            new_cat_txt = set_catalog_crc_zero_same_length(orig_cat_txt, crc_off_targets)
            tmp_c = self.catalog_json_path + ".tmp_write"
            with open(tmp_c, "w", encoding="utf-8", newline="") as f:
                f.write(new_cat_txt)
            _safe_replace_file(tmp_c, self.catalog_json_path)
            log(f"Updated aa/catalog.json (m_Crc=0 on {len(crc_off_targets)} bundle(s), exact length preserved).")

        # Step F: Update recorded file sizes in ampr_emu.index (if backport index exists at app_root)
        idx_bak = os.path.join(self.backup_dir, "ampr_emu.index.orig")
        if os.path.isfile(idx_bak) and os.path.isfile(self.ampr_index_path):
            size_updates: Dict[str, int] = {}
            for lid, info in installed.items():
                sfn = info.get("song_bundle_file") or lid.lower()
                sp = os.path.join(self.levels_bundle_dir, sfn)
                if os.path.isfile(sp):
                    size_updates[f"Media/StreamingAssets/BeatmapLevelsData/{sfn}"] = os.path.getsize(sp)
            for pfn in by_pack.keys():
                pp = os.path.join(self.aa_ps5_dir, pfn)
                if os.path.isfile(pp):
                    size_updates[f"Media/StreamingAssets/aa/PS5/{pfn}"] = os.path.getsize(pp)
            if active_covers and sa_bundle_fn:
                sap = os.path.join(self.aa_ps5_dir, sa_bundle_fn)
                if os.path.isfile(sap):
                    size_updates[f"Media/StreamingAssets/aa/PS5/{sa_bundle_fn}"] = os.path.getsize(sap)

            orig_idx_bytes = open(idx_bak, "rb").read()
            new_idx_bytes = update_ampr_index_sizes(orig_idx_bytes, size_updates)
            tmp_i = self.ampr_index_path + ".tmp_write"
            with open(tmp_i, "wb") as f:
                f.write(new_idx_bytes)
                f.flush()
                os.fsync(f.fileno())
            _safe_replace_file(tmp_i, self.ampr_index_path)
            log(f"Updated ampr_emu.index recorded file sizes ({len(size_updates)} file entries synced).")

    # ==========================================================================
    # Batch Pack & BeatSaver .bplist Playlist Importer (Great for Full DLC Packs!)
    # ==========================================================================

    def install_batch_into_pack(
        self,
        playlist_or_folder_or_files: str | List[str],
        target_pack_id: str,
        environment_override: str = "auto",
        auto_generate_difficulties: bool = True,
        auto_generate_modes: bool = True,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> List[SongSlotInfo]:
        """
        Fill slots in `target_pack_id` (e.g. 'Queen', 'BTS', 'TheRollingStones', 'OstVol1')
        from a BeatSaver .bplist playlist file, a directory of .zip/.mp3/.ogg/.wav files,
        or an explicit list of file paths.
        """
        def log(msg: str) -> None:
            if log_fn:
                log_fn(msg)

        pack_slots = self.list_slots(pack_id=target_pack_id, pack_category="all")
        if not pack_slots:
            raise ValueError(f"No song slots found for pack '{target_pack_id}'")

        # Prefer replacing Stock slots first before overwriting already-custom slots in that pack
        ordered_slots = [s for s in pack_slots if not s.is_custom] + [s for s in pack_slots if s.is_custom]

        sources: List[Tuple[str, str]] = []  # ("zip"|"audio"|"beatsaver", path_or_key)
        if isinstance(playlist_or_folder_or_files, list):
            for p in playlist_or_folder_or_files:
                if p.lower().endswith(".zip"):
                    sources.append(("zip", p))
                elif p.lower().endswith((".mp3", ".wav", ".ogg", ".flac", ".egg")):
                    sources.append(("audio", p))
        elif os.path.isdir(playlist_or_folder_or_files):
            for fn in sorted(os.listdir(playlist_or_folder_or_files)):
                fp = os.path.join(playlist_or_folder_or_files, fn)
                if not os.path.isfile(fp):
                    continue
                if fn.lower().endswith(".zip"):
                    sources.append(("zip", fp))
                elif fn.lower().endswith((".mp3", ".wav", ".ogg", ".flac", ".egg")):
                    sources.append(("audio", fp))
        elif os.path.isfile(playlist_or_folder_or_files) and playlist_or_folder_or_files.lower().endswith(
            (".bplist", ".json")
        ):
            with open(playlist_or_folder_or_files, "r", encoding="utf-8-sig") as f:
                pl = json.load(f)
            for item in pl.get("songs") or []:
                key = str(item.get("key") or "").strip()
                hsh = str(item.get("hash") or "").strip()
                if key:
                    sources.append(("beatsaver", key))
                elif hsh:
                    sources.append(("beatsaver", hsh))
        else:
            raise ValueError("Batch source must be a .bplist playlist, a folder of .zip/music files, or file list.")

        if not sources:
            raise ValueError("No songs found in the selected batch/playlist source.")

        count = min(len(sources), len(ordered_slots))
        log(
            f"Batch-installing {count} song(s) into pack '{pack_slots[0].pack_name}' "
            f"({len(pack_slots)} total slots in pack)..."
        )

        installed_results: List[SongSlotInfo] = []
        for idx in range(count):
            kind, val = sources[idx]
            target_slot = ordered_slots[idx]
            log(f"[{idx + 1}/{count}] Preparing slot '{target_slot.stock_song_name}'...")
            if kind == "beatsaver":
                zpath = download_beatsaver_zip(val)
                map_src: str | CustomSongPackage = zpath
            elif kind == "audio":
                pkg, _ = generate_track_from_audio_file(val)
                map_src = pkg
            else:
                map_src = val

            updated = self.install_custom_song(
                map_source=map_src,
                target_level_id=target_slot.level_id,
                auto_generate_difficulties=auto_generate_difficulties,
                auto_generate_modes=auto_generate_modes,
                environment_override=environment_override,
                log_fn=log_fn,
            )
            installed_results.append(updated)

        log(f"Batch complete! Installed {len(installed_results)} custom song(s) into '{pack_slots[0].pack_name}'.")
        return installed_results

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
        if self.engine_mode == "bundle_v137":
            song_fn = slot.level_assets_file
            pack_fn = slot.pack_bundle_file or ""
            bak_s = os.path.join(self.backup_dir, "songs_orig", song_fn + ".orig")
            dst_s = os.path.join(self.levels_bundle_dir, song_fn)
            if os.path.isfile(bak_s):
                if os.path.isfile(dst_s):
                    try:
                        os.chmod(dst_s, 0o666)
                    except Exception:
                        pass
                shutil.copy2(bak_s, dst_s)
            for p_del in (
                os.path.join(self.backup_dir, "previews", f"{slot.level_id}.preview.fsb"),
                os.path.join(self.backup_dir, "covers", f"{slot.level_id}.cover.bin"),
            ):
                if os.path.isfile(p_del):
                    os.remove(p_del)
            had_cover = bool((self.state.get("installed_slots", {}).get(slot.level_id) or {}).get("has_custom_cover"))
            self.state.get("installed_slots", {}).pop(slot.level_id, None)
            self._sync_v137_packs_catalog_and_index(
                touched_packs={pack_fn} if pack_fn else set(),
                rebuild_covers=had_cover,
                log_fn=log_fn,
            )
            self._save_state()
            self.refresh_catalog()
            if log_fn:
                log_fn(f"Restored 1.37 song '{slot.stock_song_name}' [{slot.pack_name}] back to stock.")
            return

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

        if self.engine_mode == "bundle_v137":
            sdir = os.path.join(self.backup_dir, "songs_orig")
            if os.path.isdir(sdir):
                for fn in os.listdir(sdir):
                    if fn.endswith(".orig"):
                        shutil.copy2(os.path.join(sdir, fn), os.path.join(self.levels_bundle_dir, fn[:-5]))
            pdir = os.path.join(self.backup_dir, "packs_orig")
            if os.path.isdir(pdir):
                for fn in os.listdir(pdir):
                    if fn.endswith(".orig"):
                        shutil.copy2(os.path.join(pdir, fn), os.path.join(self.aa_ps5_dir, fn[:-5]))
            cat_bak = os.path.join(self.backup_dir, "catalog.json.orig")
            if os.path.isfile(cat_bak) and os.path.isfile(self.catalog_json_path):
                shutil.copy2(cat_bak, self.catalog_json_path)
            idx_bak = os.path.join(self.backup_dir, "ampr_emu.index.orig")
            if os.path.isfile(idx_bak) and os.path.isfile(self.ampr_index_path):
                shutil.copy2(idx_bak, self.ampr_index_path)
            for sub in ("previews", "covers"):
                sub_p = os.path.join(self.backup_dir, sub)
                if os.path.isdir(sub_p):
                    for fn in os.listdir(sub_p):
                        os.remove(os.path.join(sub_p, fn))

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
        self.state["modified_stage_themes"] = {}
        self.state["modified_stage_meshes"] = {}
        self.state["extra_modified_assets"] = []
        self._save_state()
        self.refresh_catalog()
        if log_fn:
            log_fn("All PS5 Beat Saber files restored to 100% original factory state.")

    # ==========================================================================
    # Stage & Saber Neon Color Theme Customizer (Console "Chroma-Lite")
    # ==========================================================================

    def apply_stage_color_theme(
        self,
        environment_choice: str,
        theme_name_or_colors: str | Dict[str, Tuple[float, float, float, float]],
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> str:
        """
        Customize the Left/Right Saber, Neon Stage Lights, Boost Lights, and Wall colors
        for a PS5 stage's ColorSchemeSO inside resources.assets (safely backed up).
        """
        env = self.resolve_environment_choice(environment_choice)
        if env is None:
            raise ValueError(f"Unknown PS5 stage '{environment_choice}'")

        if isinstance(theme_name_or_colors, str):
            if theme_name_or_colors not in COLOR_THEME_PRESETS:
                raise ValueError(
                    f"Unknown color preset '{theme_name_or_colors}'. "
                    f"Available: {', '.join(COLOR_THEME_PRESETS.keys())}"
                )
            palette = COLOR_THEME_PRESETS[theme_name_or_colors]
            theme_label = theme_name_or_colors
        else:
            palette = theme_name_or_colors
            theme_label = "Custom RGB"

        self.ensure_pristine_backup(log_fn=log_fn)

        orig_res = self._get_pristine_assets("resources.assets")
        cs = ColorSchemeData.from_bytes(orig_res.by_path_id[env.color_scheme_path_id].blob)

        if "saber_a" in palette:
            cs.saber_a = palette["saber_a"]
        if "saber_b" in palette:
            cs.saber_b = palette["saber_b"]
        if "env_color_0" in palette:
            cs.env_color_0 = palette["env_color_0"]
        if "env_color_1" in palette:
            cs.env_color_1 = palette["env_color_1"]
        if "obstacles_color" in palette:
            cs.obstacles_color = palette["obstacles_color"]
        if "env_color_0_boost" in palette:
            cs.supports_boost = 1
            cs.env_color_0_boost = palette["env_color_0_boost"]
        if "env_color_1_boost" in palette:
            cs.supports_boost = 1
            cs.env_color_1_boost = palette["env_color_1_boost"]

        live_res = self._get_live_assets("resources.assets")
        live_res.set_object_blob(env.color_scheme_path_id, cs.to_bytes())
        live_res.save()

        self.state.setdefault("modified_stage_themes", {})[env.serialized_name] = theme_label
        if "resources.assets" not in self.state.get("extra_modified_assets", []):
            self.state.setdefault("extra_modified_assets", []).append("resources.assets")
        self._save_state()

        if log_fn:
            log_fn(f"Applied '{theme_label}' neon saber & stage colors to '{env.display_name}'!")
        return env.display_name

    def restore_stage_colors(
        self,
        environment_choice: Optional[str] = None,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Restore original ColorSchemeSO colors for one stage or all stages."""
        if not self.has_backup():
            return
        orig_res = self._get_pristine_assets("resources.assets")
        live_res = self._get_live_assets("resources.assets")

        targets = (
            [self.resolve_environment_choice(environment_choice)]
            if environment_choice
            else list(self.environments.values())
        )
        seen_cs = set()
        for env in targets:
            if env and env.color_scheme_path_id not in seen_cs and env.color_scheme_path_id in orig_res.by_path_id:
                seen_cs.add(env.color_scheme_path_id)
                live_res.set_object_blob(
                    env.color_scheme_path_id,
                    orig_res.by_path_id[env.color_scheme_path_id].blob,
                )
                self.state.get("modified_stage_themes", {}).pop(env.serialized_name, None)

        if live_res.dirty:
            live_res.save()
        self._save_state()
        if log_fn:
            log_fn("Restored original stage & saber color scheme(s).")

    def replace_stage_mesh_from_obj(
        self,
        target_key: str,
        obj_path_or_text: str,
        is_path: bool = True,
        scale: float = 1.0,
        offset: Tuple[float, float, float] = (0.0, 0.0, 0.0),
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> str:
        """
        Import a custom Wavefront .obj 3D model into a PS5 stage Mesh (e.g. 'platform',
        'left_building', 'right_building', 'back_columns' in sharedassets3.assets).
        """
        if target_key not in STAGE_MESH_TARGETS:
            raise ValueError(
                f"Unknown mesh target '{target_key}'. Available: {', '.join(STAGE_MESH_TARGETS.keys())}"
            )
        assets_fn, pid, mesh_name, desc = STAGE_MESH_TARGETS[target_key]
        self.ensure_pristine_backup(log_fn=log_fn)

        geom = load_wavefront_obj(obj_path_or_text, is_path=is_path, scale=scale, offset=offset)
        mesh_blob = build_unity_ps5_mesh_blob(mesh_name, geom)

        live_af = self._get_live_assets(assets_fn)
        live_af.set_object_blob(pid, mesh_blob)
        live_af.save()

        self.state.setdefault("modified_stage_meshes", {})[target_key] = {
            "mesh_name": mesh_name,
            "vertices": len(geom.positions),
            "triangles": len(geom.indices) // 3,
        }
        if assets_fn not in self.state.get("extra_modified_assets", []):
            self.state.setdefault("extra_modified_assets", []).append(assets_fn)
        self._save_state()

        if log_fn:
            log_fn(
                f"Injected 3D .obj into '{desc}' ({mesh_name}: {len(geom.positions)} verts, "
                f"{len(geom.indices) // 3} tris)!"
            )
        return desc

    def restore_stage_meshes(self, log_fn: Optional[Callable[[str], None]] = None) -> None:
        """Restore all modified 3D stage meshes back to stock."""
        if not self.has_backup():
            return
        for tkey, (assets_fn, pid, _, _) in STAGE_MESH_TARGETS.items():
            if not os.path.isfile(os.path.join(self.media_dir, assets_fn)):
                continue
            orig_af = self._get_pristine_assets(assets_fn)
            live_af = self._get_live_assets(assets_fn)
            if pid in orig_af.by_path_id:
                live_af.set_object_blob(pid, orig_af.by_path_id[pid].blob)
        for assets_fn in {v[0] for v in STAGE_MESH_TARGETS.values()}:
            if assets_fn in self._live_assets and self._live_assets[assets_fn].dirty:
                self._live_assets[assets_fn].save()
        self.state["modified_stage_meshes"] = {}
        self._save_state()
        if log_fn:
            log_fn("Restored all 3D stage meshes back to original stock.")

    # ==========================================================================
    # Integrity Verification ("No mistakes")
    # ==========================================================================

    def verify_slot(self, level_id: str) -> dict:
        slot = self.slots[level_id]
        if self.engine_mode == "bundle_v137":
            import gzip as _gz
            import UnityPy as _upy

            bpath = os.path.join(self.levels_bundle_dir, slot.level_assets_file)
            ub = UnityFsBundle.from_bytes(open(bpath, "rb").read())
            cab_n = next(n for n in ub.nodes if not n.name.endswith((".resource", ".resS")))
            res_n = next(n for n in ub.nodes if n.name.endswith(".resource"))
            fsb_meta = verify_fsb5_header(res_n.data)
            env = _upy.load(cab_n.data)
            sf_cab = list(env.files.values())[0] if hasattr(env, "files") and env.files else env.file
            charts_cnt = 0
            dur = 0.0
            for _pid, rdr in sf_cab.objects.items():
                if rdr.type.name == "AudioClip":
                    tt = rdr.read_typetree()
                    dur = float(tt["m_Length"])
                elif rdr.type.name == "TextAsset":
                    tt = rdr.read_typetree()
                    if ".beatmap.gz" in tt.get("m_Name", ""):
                        jd = json.loads(_gz.decompress(tt["m_Script"].encode("utf-8", "surrogateescape")).decode("utf-8"))
                        cn_rows = jd.get("colorNotesData") or []
                        for ev in jd.get("colorNotes") or []:
                            idx = int(ev.get("i", 0))
                            if cn_rows and not (0 <= idx < len(cn_rows)):
                                raise RuntimeError(f"v4 colorNote index out of range in {tt['m_Name']}")
                        charts_cnt += 1
            return {
                "level_id": level_id,
                "song_name": slot.current_song_name,
                "audio_source": slot.level_assets_file,
                "audio_codec": fsb_meta["codec"],
                "sample_rate": fsb_meta["sample_rate"],
                "duration": round(dur, 2),
                "charts_verified": charts_cnt,
            }

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
        extra_assets = self.state.get("extra_modified_assets", [])
        if not installed and not extra_assets:
            raise RuntimeError("No custom songs or stage customizations are currently installed to deploy.")

        if self.engine_mode == "bundle_v137":
            self._deploy_v137_via_ftp(ps5_ip, ps5_port, remote_app_dir, log)
            return

        remote_media = remote_app_dir.rstrip("/") + "/Media"
        modified_assets = set(extra_assets)
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

    def _deploy_v137_via_ftp(
        self,
        ps5_ip: str,
        ps5_port: int,
        remote_app_dir: str,
        log: Callable[[str], None],
    ) -> None:
        """
        Step H (1.37 Backend): Whole-file FTP push with SHA-1 read-back verification,
        pushing `ampr_emu.index` strictly LAST so recorded sizes never precede file arrival.
        """
        import hashlib

        installed = self.state.get("installed_slots", {})
        base_remote = remote_app_dir.rstrip("/")

        # Ordered upload queue: song bundles -> pack bundles -> optional sharedassets -> catalog.json -> ampr_emu.index LAST
        uploads: List[Tuple[str, str]] = []
        packs_set = set()
        any_covers = False
        for lid, info in sorted(installed.items()):
            sfn = info.get("song_bundle_file") or lid.lower()
            uploads.append(
                (
                    os.path.join(self.levels_bundle_dir, sfn),
                    f"{base_remote}/Media/StreamingAssets/BeatmapLevelsData/{sfn}",
                )
            )
            if info.get("pack_bundle_file"):
                packs_set.add(info["pack_bundle_file"])
            if info.get("has_custom_cover"):
                any_covers = True

        for pfn in sorted(packs_set):
            uploads.append(
                (
                    os.path.join(self.aa_ps5_dir, pfn),
                    f"{base_remote}/Media/StreamingAssets/aa/PS5/{pfn}",
                )
            )

        sa_fn = self._find_v137_sharedassets_bundle()
        if any_covers and sa_fn:
            uploads.append(
                (
                    os.path.join(self.aa_ps5_dir, sa_fn),
                    f"{base_remote}/Media/StreamingAssets/aa/PS5/{sa_fn}",
                )
            )

        if os.path.isfile(self.catalog_json_path):
            uploads.append(
                (
                    self.catalog_json_path,
                    f"{base_remote}/Media/StreamingAssets/aa/catalog.json",
                )
            )

        if os.path.isfile(self.ampr_index_path):
            uploads.append((self.ampr_index_path, f"{base_remote}/ampr_emu.index"))

        log(f"Connecting to PS5 FTP at {ps5_ip}:{ps5_port} ({len(uploads)} files, index last)...")
        with ftplib.FTP() as ftp:
            ftp.connect(ps5_ip, ps5_port, timeout=20)
            ftp.login("anonymous", "")

            for idx, (local_p, remote_p) in enumerate(uploads, start=1):
                local_bytes = open(local_p, "rb").read()
                local_sha1 = hashlib.sha1(local_bytes).hexdigest()
                log(
                    f"[{idx}/{len(uploads)}] Uploading {os.path.basename(local_p)} "
                    f"({len(local_bytes) / (1024 * 1024):.2f} MB)..."
                )
                ftp.storbinary(f"STOR {remote_p}", io.BytesIO(local_bytes), blocksize=262144)

                # Read back & compare SHA-1
                read_back = io.BytesIO()
                ftp.retrbinary(f"RETR {remote_p}", read_back.write, blocksize=262144)
                remote_sha1 = hashlib.sha1(read_back.getvalue()).hexdigest()
                if remote_sha1 != local_sha1:
                    raise RuntimeError(
                        f"FTP SHA-1 verification failed for {remote_p}: expected {local_sha1}, got {remote_sha1}"
                    )

        log(f"1.37 FTP push complete! All {len(uploads)} files verified via SHA-1 read-back.")
