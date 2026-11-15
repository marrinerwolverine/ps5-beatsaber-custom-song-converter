"""
BeatSaver Custom Song Loader, Schema Converter (V2 / V3 / V4 -> PS5 V3.3.0),
Auto-Difficulty Generator (Easy..Expert+), and Multi-Mode Generator (OneSaber, NoArrows, 90/360).
"""

from __future__ import annotations

import copy
import io
import json
import math
import os
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

DIFF_NAMES = ["Easy", "Normal", "Hard", "Expert", "ExpertPlus"]
DIFF_NAME_TO_INT = {name.lower(): idx for idx, name in enumerate(DIFF_NAMES)}
DIFF_NAME_TO_INT["expert+"] = 4
DIFF_INT_TO_RANK = {0: 1, 1: 3, 2: 5, 3: 7, 4: 9}
DEFAULT_NJS_BY_DIFF = {0: 10.0, 1: 10.0, 2: 12.0, 3: 15.0, 4: 17.0}

V2_ROTATION_DEGREES = {
    0: -60.0,
    1: -45.0,
    2: -30.0,
    3: -15.0,
    4: 15.0,
    5: 30.0,
    6: 45.0,
    7: 60.0,
}

# Angle in degrees for standard Beat Saber cut directions (0=Up, 1=Down, 2=Left, 3=Right, 4=UL, 5=UR, 6=DL, 7=DR)
CUT_DIR_ANGLES = {
    0: 180.0,
    1: 0.0,
    2: 270.0,
    3: 90.0,
    4: 225.0,
    5: 135.0,
    6: 315.0,
    7: 45.0,
}


@dataclass
class ParsedDifficultyChart:
    characteristic: str       # "Standard", "OneSaber", "NoArrows", "90Degree", "360Degree"
    difficulty: int           # 0..4
    njs: float
    njs_offset: float
    v3_dict: Dict[str, Any]
    is_generated: bool = False


@dataclass
class CustomSongPackage:
    song_name: str
    song_sub_name: str
    song_author_name: str
    level_author_name: str
    bpm: float
    song_time_offset: float
    shuffle: float
    shuffle_period: float
    preview_start_time: float
    preview_duration: float
    audio_filename: str
    audio_bytes: bytes
    # charts[characteristic][difficulty_int] = ParsedDifficultyChart
    charts: Dict[str, Dict[int, ParsedDifficultyChart]] = field(default_factory=dict)
    source_label: str = ""
    environment_name: str = ""
    custom_colors: Optional[Dict[str, Tuple[float, float, float]]] = None
    cover_bytes: Optional[bytes] = None

    def native_summary(self) -> str:
        parts = []
        for mode, dmap in sorted(self.charts.items()):
            dnames = [DIFF_NAMES[d] for d in sorted(dmap.keys()) if not dmap[d].is_generated]
            if dnames:
                parts.append(f"{mode}: {', '.join(dnames)}")
        return " | ".join(parts) if parts else "No charts"


class _VirtualMapArchive:
    """Case-insensitive reader over either a .zip archive or a local directory."""

    def __init__(self, source: str | bytes):
        self._files: Dict[str, bytes] = {}
        self._lower_map: Dict[str, str] = {}
        self.label = ""

        if isinstance(source, bytes):
            self.label = "memory.zip"
            self._load_zip(io.BytesIO(source))
        elif os.path.isdir(source):
            self.label = source
            self._load_dir(source)
        elif os.path.isfile(source):
            self.label = source
            with open(source, "rb") as f:
                self._load_zip(io.BytesIO(f.read()))
        else:
            raise FileNotFoundError(f"Map source not found: {source}")

    def _load_zip(self, bio: io.BytesIO) -> None:
        with zipfile.ZipFile(bio, "r") as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            # Detect common root prefix if info.dat lives inside a subfolder
            info_candidates = [n for n in names if os.path.basename(n).lower() == "info.dat"]
            if not info_candidates:
                raise ValueError("No Info.dat found in BeatSaver zip archive")
            best_info = min(info_candidates, key=len)
            prefix = best_info[: len(best_info) - len(os.path.basename(best_info))]
            for name in names:
                rel = name[len(prefix) :] if prefix and name.startswith(prefix) else name
                rel_norm = rel.replace("\\", "/").lstrip("/")
                if not rel_norm:
                    continue
                data = zf.read(name)
                self._files[rel_norm] = data
                self._lower_map[rel_norm.lower()] = rel_norm

    def _load_dir(self, folder: str) -> None:
        # Check if Info.dat is directly inside folder or one subfolder deep
        root = folder
        direct = [fn for fn in os.listdir(folder) if fn.lower() == "info.dat"]
        if not direct:
            for sub in os.listdir(folder):
                subp = os.path.join(folder, sub)
                if os.path.isdir(subp) and any(fn.lower() == "info.dat" for fn in os.listdir(subp)):
                    root = subp
                    break
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root).replace("\\", "/")
                with open(full, "rb") as f:
                    self._files[rel] = f.read()
                self._lower_map[rel.lower()] = rel

    def read_bytes(self, rel_name: str) -> bytes:
        norm = rel_name.replace("\\", "/").lstrip("/")
        if norm in self._files:
            return self._files[norm]
        low = norm.lower()
        if low in self._lower_map:
            return self._files[self._lower_map[low]]
        base = os.path.basename(low)
        for k_low, k_orig in self._lower_map.items():
            if os.path.basename(k_low) == base:
                return self._files[k_orig]
        raise FileNotFoundError(f"File '{rel_name}' not found in map package ({self.label})")

    def read_json(self, rel_name: str) -> Dict[str, Any]:
        raw = self.read_bytes(rel_name)
        text = raw.decode("utf-8-sig")
        return json.loads(text)

    def has_file(self, rel_name: str) -> bool:
        try:
            self.read_bytes(rel_name)
            return True
        except FileNotFoundError:
            return False

    def find_audio_fallback(self) -> Tuple[str, bytes]:
        for ext in (".egg", ".ogg", ".wav", ".mp3", ".flac"):
            for k_low, k_orig in self._lower_map.items():
                if k_low.endswith(ext) and "preview" not in k_low:
                    return k_orig, self._files[k_orig]
        raise FileNotFoundError("Could not find song audio (.egg/.ogg/.wav/.mp3/.flac) in map package")


def _normalize_characteristic_name(raw_name: str) -> str:
    clean = raw_name.strip()
    mapping = {
        "standard": "Standard",
        "onesaber": "OneSaber",
        "onecolor": "OneSaber",
        "noarrows": "NoArrows",
        "90degree": "90Degree",
        "360degree": "360Degree",
        "lawless": "Lawless",
        "lightshow": "Lightshow",
    }
    return mapping.get(clean.lower(), clean)


def load_custom_map(source: str | bytes) -> CustomSongPackage:
    """Load any BeatSaver map (.zip path, .zip bytes, or folder) supporting Info.dat v2 and v4."""
    vfs = _VirtualMapArchive(source)
    info = vfs.read_json("Info.dat")

    schema_ver = str(info.get("version") or info.get("_version") or "2.0.0")
    if schema_ver.startswith("4."):
        return _load_v4_package(vfs, info)
    return _load_v2_info_package(vfs, info)


def _load_v2_info_package(vfs: _VirtualMapArchive, info: Dict[str, Any]) -> CustomSongPackage:
    song_name = str(info.get("_songName") or "Custom Song").strip()
    song_sub = str(info.get("_songSubName") or "").strip()
    song_author = str(info.get("_songAuthorName") or "Unknown Artist").strip()
    level_author = str(info.get("_levelAuthorName") or "").strip()
    bpm = float(info.get("_beatsPerMinute") or 120.0)
    if bpm <= 0:
        bpm = 120.0
    song_time_offset = float(info.get("_songTimeOffset") or 0.0)
    shuffle = float(info.get("_shuffle") or 0.0)
    shuffle_period = float(info.get("_shufflePeriod") or 0.5)
    prev_start = float(info.get("_previewStartTime") or 12.0)
    prev_dur = float(info.get("_previewDuration") or 10.0)

    audio_fn = str(info.get("_songFilename") or "")
    if audio_fn and vfs.has_file(audio_fn):
        audio_bytes = vfs.read_bytes(audio_fn)
    else:
        audio_fn, audio_bytes = vfs.find_audio_fallback()

    charts: Dict[str, Dict[int, ParsedDifficultyChart]] = {}
    for bset in info.get("_difficultyBeatmapSets", []):
        char_name = _normalize_characteristic_name(str(bset.get("_beatmapCharacteristicName") or "Standard"))
        for diff_entry in bset.get("_difficultyBeatmaps", []):
            diff_str = str(diff_entry.get("_difficulty") or "").lower()
            if diff_str not in DIFF_NAME_TO_INT:
                continue
            diff_int = DIFF_NAME_TO_INT[diff_str]
            bfn = str(diff_entry.get("_beatmapFilename") or "")
            if not bfn or not vfs.has_file(bfn):
                continue
            raw_chart = vfs.read_json(bfn)
            v3_dict = convert_chart_dict_to_v330(raw_chart, base_bpm=bpm)
            njs = float(diff_entry.get("_noteJumpMovementSpeed") or 0.0)
            if njs <= 0.0:
                njs = DEFAULT_NJS_BY_DIFF[diff_int]
            njs_off = float(diff_entry.get("_noteJumpStartBeatOffset") or 0.0)
            charts.setdefault(char_name, {})[diff_int] = ParsedDifficultyChart(
                characteristic=char_name,
                difficulty=diff_int,
                njs=njs,
                njs_offset=njs_off,
                v3_dict=v3_dict,
                is_generated=False,
            )

    # If mapper only put charts under Lawless, fall back to Standard so the song is playable
    if "Standard" not in charts and "Lawless" in charts:
        charts["Standard"] = {
            d: ParsedDifficultyChart(
                characteristic="Standard",
                difficulty=c.difficulty,
                njs=c.njs,
                njs_offset=c.njs_offset,
                v3_dict=copy.deepcopy(c.v3_dict),
                is_generated=False,
            )
            for d, c in charts["Lawless"].items()
        }

    if not charts:
        raise ValueError(f"No playable difficulty beatmaps found in '{song_name}'")

    env_name = str(info.get("_environmentName") or "").strip()
    cover_fn = str(info.get("_coverImageFilename") or "").strip()
    cover_bytes: Optional[bytes] = None
    if cover_fn and vfs.has_file(cover_fn):
        cover_bytes = vfs.read_bytes(cover_fn)
    else:
        for ext in (".jpg", ".jpeg", ".png"):
            for k_low, k_orig in vfs._lower_map.items():
                if k_low.endswith(ext):
                    cover_bytes = vfs._files[k_orig]
                    break
            if cover_bytes:
                break
    custom_colors: Optional[Dict[str, Tuple[float, float, float]]] = None
    # Extract SongCore / BeatSaver custom RGB colors if mapper included them in Info.dat
    for bset in info.get("_difficultyBeatmapSets", []):
        for diff_entry in bset.get("_difficultyBeatmaps", []):
            cdata = diff_entry.get("_customData") or {}
            found = {}
            for key in ("_colorLeft", "_colorRight", "_envColorLeft", "_envColorRight", "_obstacleColor"):
                val = cdata.get(key)
                if isinstance(val, dict) and all(k in val for k in ("r", "g", "b")):
                    found[key] = (float(val["r"]), float(val["g"]), float(val["b"]))
            if found:
                custom_colors = found
                break
        if custom_colors:
            break

    return CustomSongPackage(
        song_name=song_name,
        song_sub_name=song_sub,
        song_author_name=song_author,
        level_author_name=level_author,
        bpm=bpm,
        song_time_offset=song_time_offset,
        shuffle=shuffle,
        shuffle_period=shuffle_period,
        preview_start_time=prev_start,
        preview_duration=prev_dur,
        audio_filename=audio_fn,
        audio_bytes=audio_bytes,
        charts=charts,
        source_label=vfs.label,
        environment_name=env_name,
        custom_colors=custom_colors,
        cover_bytes=cover_bytes,
    )


def _load_v4_package(vfs: _VirtualMapArchive, info: Dict[str, Any]) -> CustomSongPackage:
    song_meta = info.get("song") or {}
    audio_meta = info.get("audio") or {}
    song_name = str(song_meta.get("title") or "Custom Song").strip()
    song_sub = str(song_meta.get("subTitle") or "").strip()
    song_author = str(song_meta.get("author") or "Unknown Artist").strip()

    bpm = float(audio_meta.get("bpm") or 120.0)
    if bpm <= 0:
        bpm = 120.0
    prev_start = float(audio_meta.get("previewStartTime") or 12.0)
    prev_dur = float(audio_meta.get("previewDuration") or 10.0)

    audio_fn = str(audio_meta.get("songFilename") or "")
    if audio_fn and vfs.has_file(audio_fn):
        audio_bytes = vfs.read_bytes(audio_fn)
    else:
        audio_fn, audio_bytes = vfs.find_audio_fallback()

    # Optional V4 AudioData.dat BPM events
    v4_bpm_events: List[Dict[str, Any]] = []
    audio_data_fn = str(audio_meta.get("audioDataFilename") or "")
    if audio_data_fn and vfs.has_file(audio_data_fn):
        try:
            adata = vfs.read_json(audio_data_fn)
            sr = float(adata.get("songFrequency") or 44100.0)
            for seg in adata.get("bpmData", []):
                si = float(seg.get("si", 0))
                ei = float(seg.get("ei", 0))
                sb = float(seg.get("sb", 0.0))
                eb = float(seg.get("eb", 0.0))
                dur_sec = (ei - si) / sr if sr > 0 else 0.0
                if dur_sec > 1e-6 and eb > sb:
                    seg_bpm = ((eb - sb) * 60.0) / dur_sec
                    v4_bpm_events.append({"b": round(sb, 6), "m": round(seg_bpm, 4)})
        except Exception:
            pass

    mappers_seen: List[str] = []
    charts: Dict[str, Dict[int, ParsedDifficultyChart]] = {}
    for db in info.get("difficultyBeatmaps", []):
        char_name = _normalize_characteristic_name(str(db.get("characteristic") or "Standard"))
        diff_str = str(db.get("difficulty") or "").lower()
        if diff_str not in DIFF_NAME_TO_INT:
            continue
        diff_int = DIFF_NAME_TO_INT[diff_str]
        authors = (db.get("beatmapAuthors") or {}).get("mappers") or []
        for a in authors:
            if a and str(a) not in mappers_seen:
                mappers_seen.append(str(a))

        bfn = str(db.get("beatmapDataFilename") or "")
        if not bfn or not vfs.has_file(bfn):
            continue
        raw_chart = vfs.read_json(bfn)
        light_fn = str(db.get("lightshowDataFilename") or "")
        light_dict = vfs.read_json(light_fn) if (light_fn and vfs.has_file(light_fn)) else None

        v3_dict = convert_v4_chart_to_v330(raw_chart, light_dict=light_dict, bpm_events=v4_bpm_events)
        njs = float(db.get("noteJumpMovementSpeed") or 0.0)
        if njs <= 0.0:
            njs = DEFAULT_NJS_BY_DIFF[diff_int]
        njs_off = float(db.get("noteJumpStartBeatOffset") or 0.0)
        charts.setdefault(char_name, {})[diff_int] = ParsedDifficultyChart(
            characteristic=char_name,
            difficulty=diff_int,
            njs=njs,
            njs_offset=njs_off,
            v3_dict=v3_dict,
            is_generated=False,
        )

    if "Standard" not in charts and "Lawless" in charts:
        charts["Standard"] = {
            d: ParsedDifficultyChart(
                characteristic="Standard",
                difficulty=c.difficulty,
                njs=c.njs,
                njs_offset=c.njs_offset,
                v3_dict=copy.deepcopy(c.v3_dict),
                is_generated=False,
            )
            for d, c in charts["Lawless"].items()
        }

    if not charts:
        raise ValueError(f"No playable V4 beatmaps found in '{song_name}'")

    env_names = info.get("environmentNames") or []
    env_name = str(env_names[0]).strip() if env_names else ""
    cover_fn = str(song_meta.get("coverImageFilename") or info.get("coverImageFilename") or "").strip()
    cover_bytes = vfs.read_bytes(cover_fn) if (cover_fn and vfs.has_file(cover_fn)) else None

    return CustomSongPackage(
        song_name=song_name,
        song_sub_name=song_sub,
        song_author_name=song_author,
        level_author_name=", ".join(mappers_seen),
        bpm=bpm,
        song_time_offset=0.0,
        shuffle=0.0,
        shuffle_period=0.5,
        preview_start_time=prev_start,
        preview_duration=prev_dur,
        audio_filename=audio_fn,
        audio_bytes=audio_bytes,
        charts=charts,
        source_label=vfs.label,
        environment_name=env_name,
        cover_bytes=cover_bytes,
    )


# ==============================================================================
# Coordinate & Schema Sanitization Helpers
# ==============================================================================

def _sanitize_grid_coord(val: Any, low: int, high: int) -> int:
    v = float(val or 0)
    if v >= 1000.0:
        v = (v - 1000.0) / 1000.0
    elif v <= -1000.0:
        v = (v + 1000.0) / 1000.0
    return max(low, min(high, int(round(v))))


def _sanitize_cut_dir(raw_d: Any, raw_angle: Any = 0) -> Tuple[int, int]:
    d = int(round(float(raw_d or 0)))
    a = int(round(float(raw_angle or 0)))
    if 0 <= d <= 8:
        return d, max(-45, min(45, a))
    if 2000 <= d <= 2360:
        return 8, 0
    if 1000 <= d <= 1360:
        target_deg = (d - 1000) % 360
        best_dir = 1
        best_delta = 999.0
        for std_d, std_deg in CUT_DIR_ANGLES.items():
            diff = ((target_deg - std_deg + 180.0) % 360.0) - 180.0
            if abs(diff) < abs(best_delta):
                best_delta = diff
                best_dir = std_d
        return best_dir, int(round(max(-45.0, min(45.0, best_delta))))
    return 8, 0


def _empty_v330_skeleton() -> Dict[str, Any]:
    return {
        "version": "3.3.0",
        "bpmEvents": [],
        "rotationEvents": [],
        "colorNotes": [],
        "bombNotes": [],
        "obstacles": [],
        "sliders": [],
        "burstSliders": [],
        "waypoints": [],
        "basicBeatmapEvents": [],
        "colorBoostBeatmapEvents": [],
        "lightColorEventBoxGroups": [],
        "lightRotationEventBoxGroups": [],
        "lightTranslationEventBoxGroups": [],
        "vfxEventBoxGroups": [],
        "_fxEventsCollection": {"_il": [], "_fl": []},
        "basicEventTypesWithKeywords": {"d": []},
        "useNormalEventsAsCompatibleEvents": True,
    }


def convert_chart_dict_to_v330(raw: Dict[str, Any], base_bpm: float = 120.0) -> Dict[str, Any]:
    ver = str(raw.get("version") or raw.get("_version") or "")
    if ver.startswith("4.") or "colorNotesData" in raw:
        return convert_v4_chart_to_v330(raw)
    if ver.startswith("3.") or "colorNotes" in raw:
        return _normalize_v3_chart(raw)
    return _convert_v2_chart_to_v330(raw, base_bpm=base_bpm)


def _convert_v2_chart_to_v330(raw: Dict[str, Any], base_bpm: float = 120.0) -> Dict[str, Any]:
    out = _empty_v330_skeleton()

    for n in raw.get("_notes", []):
        b = float(n.get("_time", 0.0))
        if b < 0.0:
            continue
        ntype = int(n.get("_type", 0))
        x = _sanitize_grid_coord(n.get("_lineIndex", 0), 0, 3)
        y = _sanitize_grid_coord(n.get("_lineLayer", 0), 0, 2)
        if ntype in (0, 1):
            d, a = _sanitize_cut_dir(n.get("_cutDirection", 1), 0)
            out["colorNotes"].append({"b": round(b, 6), "x": x, "y": y, "c": ntype, "d": d, "a": a})
        elif ntype == 3:
            out["bombNotes"].append({"b": round(b, 6), "x": x, "y": y})

    for obs in raw.get("_obstacles", []):
        b = float(obs.get("_time", 0.0))
        dur = float(obs.get("_duration", 0.0))
        if dur < 0.0:
            b += dur
            dur = -dur
        if b < 0.0 or dur <= 0.001:
            continue
        raw_x = int(round(float(obs.get("_lineIndex", 0))))
        raw_w = int(round(float(obs.get("_width", 1))))
        if raw_w < 0:
            raw_x += raw_w
            raw_w = -raw_w
        if raw_w <= 0:
            continue
        x = max(0, min(3, raw_x))
        w = max(1, min(4 - x, raw_w))
        otype = int(obs.get("_type", 0))
        if otype == 1:
            y, h = 2, 3
        else:
            y, h = 0, 5
        out["obstacles"].append(
            {"b": round(b, 6), "x": x, "y": y, "d": round(dur, 6), "w": w, "h": h}
        )

    for sl in raw.get("_sliders", []):
        c = int(sl.get("_colorType", 0))
        if c not in (0, 1):
            continue
        hb = float(sl.get("_headTime", 0.0))
        tb = float(sl.get("_tailTime", hb))
        if tb < hb:
            hb, tb = tb, hb
        hx = _sanitize_grid_coord(sl.get("_headLineIndex", 0), 0, 3)
        hy = _sanitize_grid_coord(sl.get("_headLineLayer", 0), 0, 2)
        hd, _ = _sanitize_cut_dir(sl.get("_headCutDirection", 1))
        tx = _sanitize_grid_coord(sl.get("_tailLineIndex", 0), 0, 3)
        ty = _sanitize_grid_coord(sl.get("_tailLineLayer", 0), 0, 2)
        td, _ = _sanitize_cut_dir(sl.get("_tailCutDirection", 1))
        out["sliders"].append(
            {
                "b": round(hb, 6),
                "c": c,
                "x": hx,
                "y": hy,
                "d": hd,
                "mu": float(sl.get("_headControlPointLengthMultiplier", 1.0)),
                "tb": round(tb, 6),
                "tx": tx,
                "ty": ty,
                "tc": td,
                "tmu": float(sl.get("_tailControlPointLengthMultiplier", 1.0)),
                "m": int(sl.get("_sliderMidAnchorMode", 0)),
            }
        )

    for ev in raw.get("_events", []):
        b = float(ev.get("_time", 0.0))
        if b < 0.0:
            b = 0.0
        et = int(ev.get("_type", 0))
        val = int(ev.get("_value", 0))
        fval = float(ev.get("_floatValue", 1.0))
        if et == 100:
            if fval > 0.0:
                out["bpmEvents"].append({"b": round(b, 6), "m": round(fval, 4)})
        elif et == 5:
            out["colorBoostBeatmapEvents"].append({"b": round(b, 6), "o": bool(val == 1)})
        elif et in (14, 15):
            deg = V2_ROTATION_DEGREES.get(val)
            if deg is None and 1000 <= val <= 1720:
                deg = float(val - 1360)
            if deg is not None:
                out["rotationEvents"].append(
                    {"b": round(b, 6), "e": 0 if et == 14 else 1, "r": float(deg)}
                )
        elif 0 <= et <= 19 or 40 <= et <= 43:
            out["basicBeatmapEvents"].append(
                {"b": round(b, 6), "et": et, "i": max(0, val), "f": fval}
            )

    _sort_v3_arrays(out)
    return out


def _normalize_v3_chart(raw: Dict[str, Any]) -> Dict[str, Any]:
    out = _empty_v330_skeleton()

    for bp in raw.get("bpmEvents", []):
        m = float(bp.get("m", 0.0))
        if m > 0:
            out["bpmEvents"].append({"b": round(max(0.0, float(bp.get("b", 0.0))), 6), "m": m})

    for rot in raw.get("rotationEvents", []):
        out["rotationEvents"].append(
            {
                "b": round(max(0.0, float(rot.get("b", 0.0))), 6),
                "e": 1 if int(rot.get("e", 0)) else 0,
                "r": float(rot.get("r", 0.0)),
            }
        )

    for n in raw.get("colorNotes", []):
        b = float(n.get("b", 0.0))
        if b < 0.0:
            continue
        c = int(n.get("c", 0))
        if c not in (0, 1):
            continue
        x = _sanitize_grid_coord(n.get("x", 0), 0, 3)
        y = _sanitize_grid_coord(n.get("y", 0), 0, 2)
        d, a = _sanitize_cut_dir(n.get("d", 0), n.get("a", 0))
        out["colorNotes"].append({"b": round(b, 6), "x": x, "y": y, "c": c, "d": d, "a": a})

    for bm in raw.get("bombNotes", []):
        b = float(bm.get("b", 0.0))
        if b < 0.0:
            continue
        x = _sanitize_grid_coord(bm.get("x", 0), 0, 3)
        y = _sanitize_grid_coord(bm.get("y", 0), 0, 2)
        out["bombNotes"].append({"b": round(b, 6), "x": x, "y": y})

    for obs in raw.get("obstacles", []):
        b = float(obs.get("b", 0.0))
        dur = float(obs.get("d", 0.0))
        if dur < 0.0:
            b += dur
            dur = -dur
        if b < 0.0 or dur <= 0.001:
            continue
        rx = int(round(float(obs.get("x", 0))))
        rw = int(round(float(obs.get("w", 1))))
        if rw < 0:
            rx += rw
            rw = -rw
        if rw <= 0:
            continue
        x = max(0, min(3, rx))
        w = max(1, min(4 - x, rw))
        y = max(0, min(2, int(round(float(obs.get("y", 0))))))
        h = max(1, min(5 - y, int(round(float(obs.get("h", 5))))))
        out["obstacles"].append({"b": round(b, 6), "x": x, "y": y, "d": round(dur, 6), "w": w, "h": h})

    for sl in raw.get("sliders", []):
        c = int(sl.get("c", 0))
        if c not in (0, 1):
            continue
        hb = max(0.0, float(sl.get("b", 0.0)))
        tb = max(hb, float(sl.get("tb", hb)))
        out["sliders"].append(
            {
                "b": round(hb, 6),
                "c": c,
                "x": _sanitize_grid_coord(sl.get("x", 0), 0, 3),
                "y": _sanitize_grid_coord(sl.get("y", 0), 0, 2),
                "d": _sanitize_cut_dir(sl.get("d", 0))[0],
                "mu": float(sl.get("mu", 1.0)),
                "tb": round(tb, 6),
                "tx": _sanitize_grid_coord(sl.get("tx", 0), 0, 3),
                "ty": _sanitize_grid_coord(sl.get("ty", 0), 0, 2),
                "tc": _sanitize_cut_dir(sl.get("tc", 0))[0],
                "tmu": float(sl.get("tmu", 1.0)),
                "m": int(sl.get("m", 0)),
            }
        )

    for bs in raw.get("burstSliders", []):
        c = int(bs.get("c", 0))
        if c not in (0, 1):
            continue
        hb = max(0.0, float(bs.get("b", 0.0)))
        tb = max(hb, float(bs.get("tb", hb)))
        sc = max(2, int(bs.get("sc", 2)))
        out["burstSliders"].append(
            {
                "b": round(hb, 6),
                "c": c,
                "x": _sanitize_grid_coord(bs.get("x", 0), 0, 3),
                "y": _sanitize_grid_coord(bs.get("y", 0), 0, 2),
                "d": _sanitize_cut_dir(bs.get("d", 0))[0],
                "tb": round(tb, 6),
                "tx": _sanitize_grid_coord(bs.get("tx", 0), 0, 3),
                "ty": _sanitize_grid_coord(bs.get("ty", 0), 0, 2),
                "sc": sc,
                "s": float(bs.get("s", 1.0)),
            }
        )

    for ev in raw.get("basicBeatmapEvents", []):
        out["basicBeatmapEvents"].append(
            {
                "b": round(max(0.0, float(ev.get("b", 0.0))), 6),
                "et": int(ev.get("et", 0)),
                "i": max(0, int(ev.get("i", 0))),
                "f": float(ev.get("f", 1.0)),
            }
        )

    for cb in raw.get("colorBoostBeatmapEvents", []):
        out["colorBoostBeatmapEvents"].append(
            {"b": round(max(0.0, float(cb.get("b", 0.0))), 6), "o": bool(cb.get("o", False))}
        )

    # Preserve native v3 group lighting if present
    has_box_groups = False
    for key in (
        "lightColorEventBoxGroups",
        "lightRotationEventBoxGroups",
        "lightTranslationEventBoxGroups",
        "vfxEventBoxGroups",
    ):
        grp = raw.get(key)
        if isinstance(grp, list) and len(grp) > 0:
            out[key] = grp
            has_box_groups = True

    if has_box_groups and isinstance(raw.get("_fxEventsCollection"), dict):
        out["_fxEventsCollection"] = raw["_fxEventsCollection"]

    if isinstance(raw.get("basicEventTypesWithKeywords"), dict):
        out["basicEventTypesWithKeywords"] = raw["basicEventTypesWithKeywords"]

    if "useNormalEventsAsCompatibleEvents" in raw:
        out["useNormalEventsAsCompatibleEvents"] = bool(raw["useNormalEventsAsCompatibleEvents"])
    else:
        out["useNormalEventsAsCompatibleEvents"] = not has_box_groups

    _sort_v3_arrays(out)
    return out


def convert_v4_chart_to_v330(
    raw: Dict[str, Any],
    light_dict: Optional[Dict[str, Any]] = None,
    bpm_events: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    out = _empty_v330_skeleton()
    if bpm_events:
        out["bpmEvents"] = copy.deepcopy(bpm_events)

    cn_data = raw.get("colorNotesData") or []
    for item in raw.get("colorNotes", []):
        b = float(item.get("b", 0.0))
        idx = int(item.get("i", 0))
        if b < 0.0 or not (0 <= idx < len(cn_data)):
            continue
        meta = cn_data[idx]
        c = int(meta.get("c", 0))
        if c not in (0, 1):
            continue
        d, a = _sanitize_cut_dir(meta.get("d", 1), meta.get("a", 0))
        out["colorNotes"].append(
            {
                "b": round(b, 6),
                "x": _sanitize_grid_coord(meta.get("x", 0), 0, 3),
                "y": _sanitize_grid_coord(meta.get("y", 0), 0, 2),
                "c": c,
                "d": d,
                "a": a,
            }
        )

    bn_data = raw.get("bombNotesData") or []
    for item in raw.get("bombNotes", []):
        b = float(item.get("b", 0.0))
        idx = int(item.get("i", 0))
        if b < 0.0 or not (0 <= idx < len(bn_data)):
            continue
        meta = bn_data[idx]
        out["bombNotes"].append(
            {
                "b": round(b, 6),
                "x": _sanitize_grid_coord(meta.get("x", 0), 0, 3),
                "y": _sanitize_grid_coord(meta.get("y", 0), 0, 2),
            }
        )

    ob_data = raw.get("obstaclesData") or []
    for item in raw.get("obstacles", []):
        b = float(item.get("b", 0.0))
        idx = int(item.get("i", 0))
        if b < 0.0 or not (0 <= idx < len(ob_data)):
            continue
        meta = ob_data[idx]
        dur = float(meta.get("d", 0.0))
        if dur <= 0.001:
            continue
        x = max(0, min(3, int(meta.get("x", 0))))
        w = max(1, min(4 - x, int(meta.get("w", 1))))
        y = max(0, min(2, int(meta.get("y", 0))))
        h = max(1, min(5 - y, int(meta.get("h", 5))))
        out["obstacles"].append({"b": round(b, 6), "x": x, "y": y, "d": round(dur, 6), "w": w, "h": h})

    arcs_data = raw.get("arcsData") or []
    for item in raw.get("arcs", []):
        hb = max(0.0, float(item.get("hb", 0.0)))
        tb = max(hb, float(item.get("tb", hb)))
        hi = int(item.get("hi", 0))
        ti = int(item.get("ti", 0))
        ai = int(item.get("ai", 0))
        if not (0 <= hi < len(cn_data) and 0 <= ti < len(cn_data)):
            continue
        hm = cn_data[hi]
        tm = cn_data[ti]
        am = arcs_data[ai] if 0 <= ai < len(arcs_data) else {}
        c = int(hm.get("c", 0))
        if c not in (0, 1):
            continue
        out["sliders"].append(
            {
                "b": round(hb, 6),
                "c": c,
                "x": _sanitize_grid_coord(hm.get("x", 0), 0, 3),
                "y": _sanitize_grid_coord(hm.get("y", 0), 0, 2),
                "d": _sanitize_cut_dir(hm.get("d", 1))[0],
                "mu": float(am.get("m", 1.0)),
                "tb": round(tb, 6),
                "tx": _sanitize_grid_coord(tm.get("x", 0), 0, 3),
                "ty": _sanitize_grid_coord(tm.get("y", 0), 0, 2),
                "tc": _sanitize_cut_dir(tm.get("d", 1))[0],
                "tmu": float(am.get("tm", 1.0)),
                "m": int(am.get("a", 0)),
            }
        )

    ch_data = raw.get("chainsData") or []
    for item in raw.get("chains", []):
        hb = max(0.0, float(item.get("hb", 0.0)))
        tb = max(hb, float(item.get("tb", hb)))
        hi = int(item.get("i", 0))
        ci = int(item.get("ci", 0))
        if not (0 <= hi < len(cn_data) and 0 <= ci < len(ch_data)):
            continue
        hm = cn_data[hi]
        cm = ch_data[ci]
        c = int(hm.get("c", 0))
        if c not in (0, 1):
            continue
        out["burstSliders"].append(
            {
                "b": round(hb, 6),
                "c": c,
                "x": _sanitize_grid_coord(hm.get("x", 0), 0, 3),
                "y": _sanitize_grid_coord(hm.get("y", 0), 0, 2),
                "d": _sanitize_cut_dir(hm.get("d", 1))[0],
                "tb": round(tb, 6),
                "tx": _sanitize_grid_coord(cm.get("tx", 0), 0, 3),
                "ty": _sanitize_grid_coord(cm.get("ty", 0), 0, 2),
                "sc": max(2, int(cm.get("c", 2))),
                "s": float(cm.get("s", 1.0)),
            }
        )

    sr_data = raw.get("spawnRotationsData") or []
    for item in raw.get("spawnRotations", []):
        b = max(0.0, float(item.get("b", 0.0)))
        idx = int(item.get("i", 0))
        if 0 <= idx < len(sr_data):
            rm = sr_data[idx]
            out["rotationEvents"].append(
                {
                    "b": round(b, 6),
                    "e": 1 if int(rm.get("t", 0)) else 0,
                    "r": float(rm.get("r", 0.0)),
                }
            )

    if light_dict:
        be_data = light_dict.get("basicEventsData") or []
        for item in light_dict.get("basicEvents", []):
            b = max(0.0, float(item.get("b", 0.0)))
            idx = int(item.get("i", 0))
            if 0 <= idx < len(be_data):
                em = be_data[idx]
                out["basicBeatmapEvents"].append(
                    {
                        "b": round(b, 6),
                        "et": int(em.get("t", 0)),
                        "i": max(0, int(em.get("i", 0))),
                        "f": float(em.get("f", 1.0)),
                    }
                )
        cb_data = light_dict.get("colorBoostEventsData") or []
        for item in light_dict.get("colorBoostEvents", []):
            b = max(0.0, float(item.get("b", 0.0)))
            idx = int(item.get("i", 0))
            if 0 <= idx < len(cb_data):
                cm = cb_data[idx]
                out["colorBoostBeatmapEvents"].append(
                    {"b": round(b, 6), "o": bool(cm.get("b", 0))}
                )

    _sort_v3_arrays(out)
    return out


def _sort_v3_arrays(v3: Dict[str, Any]) -> None:
    for key in (
        "bpmEvents",
        "rotationEvents",
        "colorNotes",
        "bombNotes",
        "obstacles",
        "sliders",
        "burstSliders",
        "basicBeatmapEvents",
        "colorBoostBeatmapEvents",
    ):
        v3[key].sort(key=lambda e: float(e.get("b", 0.0)))


# ==============================================================================
# Smart Missing-Difficulty & Extra-Mode Generators
# ==============================================================================

def derive_difficulty_chart(
    src_chart: ParsedDifficultyChart,
    target_diff: int,
    bpm: float,
) -> ParsedDifficultyChart:
    """
    Generate a natural, playable Beat Saber difficulty (0=Easy..4=ExpertPlus)
    from the closest available mapper chart when a BeatSaver map omits lower/higher tiers.
    """
    if target_diff >= src_chart.difficulty:
        # Cloning upward (e.g. Expert -> ExpertPlus)
        return ParsedDifficultyChart(
            characteristic=src_chart.characteristic,
            difficulty=target_diff,
            njs=max(src_chart.njs, DEFAULT_NJS_BY_DIFF[target_diff]),
            njs_offset=src_chart.njs_offset,
            v3_dict=copy.deepcopy(src_chart.v3_dict),
            is_generated=True,
        )

    # Down-scaling difficulty for friends/family playability:
    # Target max Notes-Per-Second (NPS) caps converted into minimum beat spacing
    bps = max(0.5, bpm / 60.0)
    target_nps = {0: 1.35, 1: 2.30, 2: 3.60, 3: 5.20}[target_diff]
    min_beat_gap = max(0.25, round((bps / target_nps) * 4.0) / 4.0)
    allow_chords = target_diff >= 1
    allow_bombs = target_diff >= 2
    allow_crouch_walls = target_diff >= 2
    allow_bursts = target_diff >= 2
    allow_arcs = target_diff >= 1

    base = copy.deepcopy(src_chart.v3_dict)
    src_notes = base.get("colorNotes", [])

    # Group source notes by beat timestamp (within 0.02 beat tolerance)
    clusters: List[List[Dict[str, Any]]] = []
    for n in src_notes:
        if clusters and abs(n["b"] - clusters[-1][0]["b"]) <= 0.02:
            clusters[-1].append(n)
        else:
            clusters.append([n])

    kept_notes: List[Dict[str, Any]] = []
    last_beat = -999.0
    next_hand_toggle = 1  # Alternate hands nicely on Easy single-note reductions
    last_dir_by_hand = {0: -1, 1: -1}

    for cluster in clusters:
        beat = cluster[0]["b"]
        # Prefer notes landing on whole/half beats when thinning
        on_downbeat = abs(beat - round(beat)) < 0.04
        effective_gap = min_beat_gap * (0.85 if on_downbeat else 1.0)
        if beat - last_beat < effective_gap:
            continue

        chosen = copy.deepcopy(cluster)
        if not allow_chords and len(chosen) > 1:
            # Pick note matching alternating hand if present, otherwise first
            match = [c for c in chosen if c["c"] == next_hand_toggle]
            chosen = [match[0] if match else chosen[0]]
            next_hand_toggle = 1 - chosen[0]["c"]
        elif len(chosen) > 2:
            chosen = chosen[:2]

        # Enforce comfortable swing parity & simplify diagonals on Easy
        for note in chosen:
            if target_diff == 0:
                # Keep bottom/middle lanes mostly and orthogonal swings
                if note["y"] == 2:
                    note["y"] = 1
                if note["d"] in (4, 5):
                    note["d"] = 0  # Up
                elif note["d"] in (6, 7):
                    note["d"] = 1  # Down
                note["a"] = 0

            hand = note["c"]
            prev_d = last_dir_by_hand.get(hand, -1)
            # Prevent double-downs / wrist-breakers after note thinning!
            if note["d"] != 8 and note["d"] == prev_d:
                flip_map = {0: 1, 1: 0, 2: 3, 3: 2, 4: 7, 7: 4, 5: 6, 6: 5}
                note["d"] = flip_map.get(note["d"], 1)
            last_dir_by_hand[hand] = note["d"]

        kept_notes.extend(chosen)
        last_beat = beat

    base["colorNotes"] = kept_notes
    if not allow_bombs:
        base["bombNotes"] = []
    elif target_diff == 2:
        # Remove bombs within 1 beat of kept notes on Hard
        note_beats = [n["b"] for n in kept_notes]
        base["bombNotes"] = [
            bm for bm in base.get("bombNotes", [])
            if all(abs(bm["b"] - nb) > 0.75 for nb in note_beats)
        ]

    if not allow_crouch_walls:
        # Keep only side walls (x=0 w=1 or x=3 w=1) so beginners don't have vision blocks/crouches
        base["obstacles"] = [
            ob for ob in base.get("obstacles", [])
            if ob["y"] == 0 and ob["w"] == 1 and ob["x"] in (0, 3)
        ]

    if not allow_bursts:
        base["burstSliders"] = []
    if not allow_arcs:
        base["sliders"] = []

    return ParsedDifficultyChart(
        characteristic=src_chart.characteristic,
        difficulty=target_diff,
        njs=min(src_chart.njs, DEFAULT_NJS_BY_DIFF[target_diff]),
        njs_offset=0.0 if target_diff <= 1 else src_chart.njs_offset,
        v3_dict=base,
        is_generated=True,
    )


def ensure_all_five_difficulties(
    mode_charts: Dict[int, ParsedDifficultyChart],
    bpm: float,
) -> Dict[int, ParsedDifficultyChart]:
    """Ensure Easy(0), Normal(1), Hard(2), Expert(3), ExpertPlus(4) all exist."""
    result = dict(mode_charts)
    existing = sorted(result.keys())
    if not existing:
        return result

    for target_d in range(5):
        if target_d in result:
            continue
        # Prefer closest higher difficulty to thin down; otherwise closest lower difficulty
        higher = [d for d in existing if d > target_d]
        if higher:
            src = result[min(higher)]
        else:
            src = result[max(existing)]
        result[target_d] = derive_difficulty_chart(src, target_d, bpm)
    return result


def generate_mode_from_standard(
    std_chart: ParsedDifficultyChart,
    target_mode: str,
) -> ParsedDifficultyChart:
    """
    Generate OneSaber, NoArrows, 90Degree, or 360Degree from a Standard V3.3.0 chart.
    """
    v3 = copy.deepcopy(std_chart.v3_dict)

    if target_mode == "NoArrows":
        for n in v3["colorNotes"]:
            n["d"] = 8
            n["a"] = 0
        v3["burstSliders"] = []

    elif target_mode == "OneSaber":
        # Right saber = color 1; collapse chords & enforce safe single-arm travel gap
        clusters: List[List[Dict[str, Any]]] = []
        for n in v3["colorNotes"]:
            if clusters and abs(n["b"] - clusters[-1][0]["b"]) <= 0.02:
                clusters[-1].append(n)
            else:
                clusters.append([n])
        one_notes: List[Dict[str, Any]] = []
        last_b = -999.0
        last_d = -1
        for cl in clusters:
            b = cl[0]["b"]
            if b - last_b < 0.22:
                continue
            right_pref = [x for x in cl if x["c"] == 1]
            note = copy.deepcopy(right_pref[0] if right_pref else cl[0])
            note["c"] = 1
            if note["d"] != 8 and note["d"] == last_d and (b - last_b) < 0.75:
                flip_map = {0: 1, 1: 0, 2: 3, 3: 2, 4: 7, 7: 4, 5: 6, 6: 5}
                note["d"] = flip_map.get(note["d"], 1)
            last_d = note["d"]
            last_b = b
            one_notes.append(note)
        v3["colorNotes"] = one_notes
        v3["sliders"] = [dict(s, c=1) for s in v3.get("sliders", []) if s.get("c") == 1]
        v3["burstSliders"] = [dict(s, c=1) for s in v3.get("burstSliders", []) if s.get("c") == 1]

    elif target_mode in ("90Degree", "360Degree"):
        if not v3["rotationEvents"] and v3["colorNotes"]:
            first_b = math.ceil(v3["colorNotes"][0]["b"] / 8.0) * 8.0
            last_b = v3["colorNotes"][-1]["b"]
            cum_angle = 0.0
            max_angle = 45.0 if target_mode == "90Degree" else 180.0
            step = 15.0
            direction = 1.0
            b = first_b
            rot_events = []
            while b < last_b:
                if abs(cum_angle + direction * step) > max_angle:
                    direction = -direction
                delta = direction * step
                cum_angle += delta
                rot_events.append({"b": round(b, 6), "e": 1, "r": delta})
                b += 8.0
            v3["rotationEvents"] = rot_events
            # Remove wide middle walls that conflict with lane rotation
            v3["obstacles"] = [
                ob for ob in v3["obstacles"] if ob["w"] == 1 and ob["x"] in (0, 3)
            ]

    return ParsedDifficultyChart(
        characteristic=target_mode,
        difficulty=std_chart.difficulty,
        njs=std_chart.njs,
        njs_offset=std_chart.njs_offset,
        v3_dict=v3,
        is_generated=True,
    )


def serialize_v3_chart_json(v3_dict: Dict[str, Any]) -> str:
    """Compact deterministic JSON matching PS5 BeatmapDataSO format."""
    return json.dumps(v3_dict, separators=(",", ":"), ensure_ascii=False)
