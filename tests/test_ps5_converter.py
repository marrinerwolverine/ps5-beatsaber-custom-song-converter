"""
Comprehensive automated test suite for PS5 Beat Saber (PSVR2) Custom Song Converter.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import zipfile

import numpy as np
import pytest
import soundfile as sf

from ps5_bs_converter.audio_fsb5 import (
    build_pcm16_fsb5_from_samples,
    decode_audio_bytes,
    encode_audio_file_to_fsb5,
    verify_fsb5_header,
)
from ps5_bs_converter.beatmap_converter import (
    ensure_all_five_difficulties,
    generate_mode_from_standard,
    load_custom_map,
)
from ps5_bs_converter.beatsaver_client import download_beatsaver_zip, extract_beatsaver_id
from ps5_bs_converter.game_manager import PS5BeatSaberManager
from ps5_bs_converter.unity_assets import SerializedAssetsFile

REAL_DUMP_ROOT = os.environ.get("PS5_BEATSABER_DUMP", "")
HAS_REAL_DUMP = bool(REAL_DUMP_ROOT) and os.path.isfile(
    os.path.join(REAL_DUMP_ROOT, "Media", "sharedassets48.assets")
)

REAL_DUMP_137 = os.environ.get("PS5_BEATSABER_DUMP_137", "")
HAS_REAL_DUMP_137 = bool(REAL_DUMP_137) and os.path.isdir(
    os.path.join(REAL_DUMP_137, "Media", "StreamingAssets", "BeatmapLevelsData")
)


def _make_ogg_egg_bytes(duration_s: float = 4.0, sr: int = 44100, channels: int = 2) -> bytes:
    t = np.linspace(0.0, duration_s, int(sr * duration_s), endpoint=False, dtype=np.float32)
    left = 0.5 * np.sin(2.0 * np.pi * 440.0 * t)
    if channels == 1:
        arr = left
    else:
        right = 0.5 * np.sin(2.0 * np.pi * 554.37 * t)
        arr = np.column_stack((left, right))
    buf = io.BytesIO()
    sf.write(buf, arr, sr, format="OGG", subtype="VORBIS")
    return buf.getvalue()


def _make_v2_beatsaver_zip(duration_s: float = 6.0) -> bytes:
    info = {
        "_version": "2.0.0",
        "_songName": "Neon Horizon",
        "_songSubName": "(VIP Mix)",
        "_songAuthorName": "SynthRider",
        "_levelAuthorName": "MapperOne",
        "_beatsPerMinute": 140.0,
        "_songTimeOffset": 0.0,
        "_shuffle": 0.0,
        "_shufflePeriod": 0.5,
        "_previewStartTime": 1.0,
        "_previewDuration": 3.0,
        "_songFilename": "song.egg",
        "_difficultyBeatmapSets": [
            {
                "_beatmapCharacteristicName": "Standard",
                "_difficultyBeatmaps": [
                    {
                        "_difficulty": "ExpertPlus",
                        "_difficultyRank": 9,
                        "_beatmapFilename": "ExpertPlusStandard.dat",
                        "_noteJumpMovementSpeed": 18.0,
                        "_noteJumpStartBeatOffset": -0.25,
                    }
                ],
            }
        ],
    }
    notes = []
    for i in range(32):
        b = 2.0 + i * 0.25
        notes.append(
            {
                "_time": b,
                "_lineIndex": 2500 if i == 5 else (i % 4),      # includes Mapping Extensions precision x!
                "_lineLayer": i % 3,
                "_type": i % 2,
                "_cutDirection": 1045 if i == 7 else (i % 2),  # includes Mapping Extensions angle!
            }
        )
    notes.append({"_time": 6.5, "_lineIndex": 1, "_lineLayer": 0, "_type": 3, "_cutDirection": 0})  # bomb

    chart = {
        "_version": "2.2.0",
        "_notes": notes,
        "_obstacles": [
            {"_time": 4.0, "_lineIndex": 0, "_type": 0, "_duration": 2.0, "_width": 1},
            {"_time": 8.0, "_lineIndex": 3, "_type": 1, "_duration": -1.0, "_width": -2},  # negative wall!
        ],
        "_events": [
            {"_time": 0.0, "_type": 0, "_value": 3, "_floatValue": 1.0},
            {"_time": 2.0, "_type": 5, "_value": 1},
            {"_time": 4.0, "_type": 14, "_value": 4},
        ],
    }

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("NestedFolder/info.dat", json.dumps(info))
        zf.writestr("NestedFolder/ExpertPlusStandard.dat", json.dumps(chart))
        zf.writestr("NestedFolder/song.egg", _make_ogg_egg_bytes(duration_s=duration_s))
    return zbuf.getvalue()


def _make_v4_beatsaver_zip(duration_s: float = 5.0) -> bytes:
    info = {
        "version": "4.0.0",
        "song": {"title": "Cyber Pulse V4", "subTitle": "", "author": "FutureWave"},
        "audio": {
            "songFilename": "track.ogg",
            "songDuration": duration_s,
            "audioDataFilename": "AudioData.dat",
            "bpm": 128.0,
            "lufs": -10.5,
            "previewStartTime": 0.5,
            "previewDuration": 2.5,
        },
        "difficultyBeatmaps": [
            {
                "characteristic": "Standard",
                "difficulty": "Expert",
                "beatmapAuthors": {"mappers": ["V4Mapper"], "lighters": []},
                "noteJumpMovementSpeed": 16.0,
                "noteJumpStartBeatOffset": 0.0,
                "beatmapDataFilename": "ExpertStandard.dat",
                "lightshowDataFilename": "Lightshow.dat",
            }
        ],
    }
    audio_data = {
        "version": "4.0.0",
        "songChecksum": "",
        "songSampleCount": int(duration_s * 44100),
        "songFrequency": 44100,
        "bpmData": [{"si": 0, "ei": int(duration_s * 44100), "sb": 0.0, "eb": (duration_s / 60.0) * 128.0}],
    }
    chart_v4 = {
        "version": "4.0.0",
        "colorNotesData": [
            {"x": 1, "y": 0, "c": 0, "d": 1, "a": 0},
            {"x": 2, "y": 1, "c": 1, "d": 0, "a": 0},
        ],
        "colorNotes": [
            {"b": 2.0 + i * 0.5, "r": 0, "i": i % 2} for i in range(16)
        ],
        "bombNotesData": [{"x": 0, "y": 0}],
        "bombNotes": [{"b": 5.0, "r": 0, "i": 0}],
        "obstaclesData": [{"d": 1.5, "x": 0, "y": 0, "w": 1, "h": 5}],
        "obstacles": [{"b": 3.0, "r": 0, "i": 0}],
        "arcsData": [{"m": 1.0, "tm": 1.0, "a": 0}],
        "arcs": [{"hb": 2.0, "tb": 3.0, "hr": 0, "tr": 0, "hi": 0, "ti": 1, "ai": 0}],
        "chainsData": [{"tx": 2, "ty": 0, "c": 4, "s": 0.9}],
        "chains": [{"hb": 4.0, "tb": 4.25, "hr": 0, "tr": 0, "i": 1, "ci": 0}],
    }
    lightshow = {
        "version": "4.0.0",
        "basicEventsData": [{"t": 1, "i": 3, "f": 1.0}],
        "basicEvents": [{"b": 2.0, "i": 0}],
        "colorBoostEventsData": [{"b": 1}],
        "colorBoostEvents": [{"b": 4.0, "i": 0}],
    }
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("Info.dat", json.dumps(info))
        zf.writestr("AudioData.dat", json.dumps(audio_data))
        zf.writestr("ExpertStandard.dat", json.dumps(chart_v4))
        zf.writestr("Lightshow.dat", json.dumps(lightshow))
        zf.writestr("track.ogg", _make_ogg_egg_bytes(duration_s=duration_s, sr=48000, channels=1))
    return zbuf.getvalue()


# ==============================================================================
# Unit Tests (Audio, Beatmaps, BeatSaver ID Parsing)
# ==============================================================================

def test_extract_beatsaver_id():
    from ps5_bs_converter.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["status"])
    assert args.command == "status"

    assert extract_beatsaver_id("30560") == "30560"
    assert extract_beatsaver_id("https://beatsaver.com/maps/1d6C7c2") == "1d6c7c2"
    assert extract_beatsaver_id("https://bsaber.com/songs/3b2f1/") == "3b2f1"
    with pytest.raises(ValueError):
        extract_beatsaver_id("not-a-map-url!!")


def test_audio_decode_and_fsb5_encoding():
    raw_egg = _make_ogg_egg_bytes(duration_s=2.5, sr=22050, channels=1)
    samples, sr = decode_audio_bytes(raw_egg, "song.egg")
    assert sr == 44100  # auto-resampled from 22050 -> 44100
    assert samples.shape[1] == 2  # mono -> stereo

    enc = build_pcm16_fsb5_from_samples(samples, sr)
    assert len(enc.fsb5_bytes) % 32 == 0
    meta = verify_fsb5_header(enc.fsb5_bytes)
    assert meta["codec"] == 2  # PCM16
    assert meta["sample_rate"] == 44100
    assert meta["channels"] == 2
    assert meta["shdr_size"] == 36
    assert abs(meta["frames"] / 44100.0 - 2.5) < 0.05

    main_fsb, prev_fsb = encode_audio_file_to_fsb5(
        raw_egg, "song.egg", preview_start=0.5, preview_duration=1.2, need_preview_clip=True
    )
    assert prev_fsb is not None
    pmeta = verify_fsb5_header(prev_fsb.fsb5_bytes)
    assert pmeta["codec"] == 2
    assert abs(prev_fsb.duration_seconds - 1.2) < 0.05


def test_v2_and_v4_conversion_and_generators():
    pkg_v2 = load_custom_map(_make_v2_beatsaver_zip())
    assert pkg_v2.song_name == "Neon Horizon"
    std_ep = pkg_v2.charts["Standard"][4]
    v3 = std_ep.v3_dict
    assert v3["version"] == "3.3.0"
    assert v3["useNormalEventsAsCompatibleEvents"] is True
    # Check Mapping Extensions coordinates & negative walls were sanitized
    assert all(0 <= n["x"] <= 3 and 0 <= n["y"] <= 2 and 0 <= n["d"] <= 8 for n in v3["colorNotes"])
    assert all(ob["d"] > 0 and 0 <= ob["x"] <= 3 and 1 <= ob["w"] <= 4 - ob["x"] for ob in v3["obstacles"])
    assert len(v3["rotationEvents"]) == 1

    # Auto-difficulty generation check (Easy < Normal < Hard < Expert <= ExpertPlus)
    all5 = ensure_all_five_difficulties(pkg_v2.charts["Standard"], pkg_v2.bpm)
    assert set(all5.keys()) == {0, 1, 2, 3, 4}
    assert len(all5[0].v3_dict["colorNotes"]) < len(all5[4].v3_dict["colorNotes"])
    assert len(all5[0].v3_dict["bombNotes"]) == 0
    assert len(all5[1].v3_dict["bombNotes"]) == 0

    # Extra-mode generation check
    no_arr = generate_mode_from_standard(all5[4], "NoArrows")
    assert all(n["d"] == 8 for n in no_arr.v3_dict["colorNotes"])

    one_sab = generate_mode_from_standard(all5[4], "OneSaber")
    assert all(n["c"] == 1 for n in one_sab.v3_dict["colorNotes"])

    deg90 = generate_mode_from_standard(all5[4], "90Degree")
    assert len(deg90.v3_dict["rotationEvents"]) > 0

    # V4 package check
    pkg_v4 = load_custom_map(_make_v4_beatsaver_zip())
    assert pkg_v4.song_name == "Cyber Pulse V4"
    v4_std = pkg_v4.charts["Standard"][3].v3_dict
    assert len(v4_std["colorNotes"]) == 16
    assert len(v4_std["sliders"]) == 1
    assert len(v4_std["burstSliders"]) == 1
    assert len(v4_std["bpmEvents"]) == 1


def test_auto_mapper_audio_to_beatmap_and_zip_export(tmp_path):
    from ps5_bs_converter.auto_mapper import (
        export_package_to_beatsaver_zip,
        generate_track_from_audio_file,
    )

    # Synthesize an 8-second 128 BPM electronic drum/bass test track
    sr = 44100
    dur = 8.0
    t = np.linspace(0.0, dur, int(sr * dur), endpoint=False, dtype=np.float32)
    beat_period = 60.0 / 128.0
    phase_in_beat = np.mod(t, beat_period)
    kick_env = np.exp(-phase_in_beat * 28.0)
    kick = 0.7 * kick_env * np.sin(2.0 * np.pi * 65.0 * t)
    lead = 0.25 * np.sin(2.0 * np.pi * 440.0 * t) * (1.0 + 0.5 * (t > 3.5))
    stereo = np.column_stack((kick + lead, kick + lead)).astype(np.float32)

    ogg_buf = io.BytesIO()
    sf.write(ogg_buf, stereo, sr, format="OGG", subtype="VORBIS")

    pkg, analysis = generate_track_from_audio_file(
        ogg_buf.getvalue(),
        filename_hint="SynthArtist - Custom Pulse.ogg",
        style="Dance / Party",
        environment_name="TheWeekndEnvironment",
    )
    assert pkg.song_name == "Custom Pulse"
    assert pkg.song_author_name == "SynthArtist"
    assert abs(analysis.bpm - 128.0) <= 2.5
    std = pkg.charts["Standard"]
    assert set(std.keys()) == {0, 1, 2, 3, 4}
    assert len(std[0].v3_dict["colorNotes"]) <= len(std[4].v3_dict["colorNotes"])
    assert len(std[4].v3_dict["basicBeatmapEvents"]) > 0

    # Export as BeatSaver .zip and reload
    out_zip = str(tmp_path / "generated_track.zip")
    export_package_to_beatsaver_zip(pkg, out_zip)
    reloaded = load_custom_map(out_zip)
    assert reloaded.song_name == "Custom Pulse"
    assert reloaded.environment_name == "TheWeekndEnvironment"
    assert set(reloaded.charts["Standard"].keys()) == {0, 1, 2, 3, 4}


def test_stage_modder_obj_to_ps5_mesh():
    from ps5_bs_converter.stage_modder import build_unity_ps5_mesh_blob, load_wavefront_obj

    quad_obj = "\n".join([
        "v -1.5 0.0 -1.0",
        "v  1.5 0.0 -1.0",
        "v  1.5 0.0  1.0",
        "v -1.5 0.0  1.0",
        "vt 0.0 0.0",
        "vt 1.0 0.0",
        "vt 1.0 1.0",
        "vt 0.0 1.0",
        "vn 0.0 1.0 0.0",
        "f 1/1/1 2/2/1 3/3/1 4/4/1",
    ])
    geom = load_wavefront_obj(quad_obj, is_path=False)
    assert len(geom.positions) == 4
    assert len(geom.indices) == 6
    blob = build_unity_ps5_mesh_blob("PlayersPlaceCore", geom)
    assert len(blob) == 616  # Exact byte parity with PS5 PlayersPlaceCore Mesh!


def test_vorbis_fsb5_setup_crc_and_sync_check():
    """Handover Section 8.2 test: Vorbis FSB5 setup CRC == 0x6D39BF3E and sync check at offset 0."""
    import struct as _st
    from ps5_bs_converter.vorbis_fsb5 import (
        EXPECTED_VORBIS_SETUP_CRC32,
        encode_song_and_preview_vorbis_fsb5,
    )

    egg = _make_ogg_egg_bytes(duration_s=5.0, sr=44100, channels=2)
    main_v, prev_v = encode_song_and_preview_vorbis_fsb5(egg, "song.egg", preview_start=1.0)
    buf = main_v.fsb5_bytes
    assert buf[:4] == b"FSB5"
    ver, nsamp, shsz, nsz, dsz, mode = _st.unpack_from("<6I", buf, 4)
    assert ver == 1 and nsamp == 1 and mode == 15
    assert (60 + shsz) % 32 == 0
    assert dsz % 32 == 0
    setup_crc = _st.unpack_from("<I", buf, 72)[0]
    assert setup_crc == EXPECTED_VORBIS_SETUP_CRC32  # 0x6D39BF3E
    assert abs(prev_v.duration_seconds - 4.0) <= 1.1


def test_v3_to_v4_tempo_flattening_and_indices_in_range():
    """Handover Section 8.2 test: v4 JSON parses with all indices in range + tempo spike flattening."""
    from ps5_bs_converter.bundle_v137 import convert_v3_to_v4_beatmap_and_lightshow

    v3_sample = {
        "version": "3.3.0",
        "bpmEvents": [{"b": 0.0, "m": 120.0}, {"b": 4.0, "m": 100000.0}, {"b": 6.0, "m": 120.0}],
        "colorNotes": [
            {"b": 2.0, "x": 1, "y": 0, "c": 0, "d": 1, "a": 0},
            {"b": 8.0, "x": 2, "y": 1, "c": 1, "d": 0, "a": 0},
        ],
        "bombNotes": [{"b": 3.0, "x": 0, "y": 0}],
        "obstacles": [{"b": 2.0, "x": 0, "y": 0, "d": 2.0, "w": 1, "h": 5}],
        "sliders": [{"b": 2.0, "c": 0, "x": 1, "y": 0, "d": 1, "mu": 1.0, "tb": 8.0, "tx": 2, "ty": 1, "tc": 0, "tmu": 1.0, "m": 0}],
        "burstSliders": [{"b": 8.0, "c": 1, "x": 2, "y": 1, "d": 0, "tb": 8.25, "tx": 3, "ty": 0, "sc": 4, "s": 0.9}],
        "basicBeatmapEvents": [{"b": 2.0, "et": 1, "i": 3, "f": 1.0}],
        "colorBoostBeatmapEvents": [{"b": 8.0, "o": True}],
    }
    v4_bm, v4_ls, counts = convert_v3_to_v4_beatmap_and_lightshow(v3_sample, base_bpm=120.0)
    # Beat 8.0 after a 2-beat 100,000-BPM spike occurs at ~beat 6.0024 in constant 120-BPM time!
    assert abs(v4_bm["colorNotes"][1]["b"] - 6.0) < 0.05
    assert all(0 <= int(n.get("i", 0)) < len(v4_bm["colorNotesData"]) for n in v4_bm["colorNotes"])
    assert all(0 <= int(a.get("ai", 0)) < len(v4_bm["arcsData"]) for a in v4_bm["arcs"])
    assert all(0 <= int(c.get("ci", 0)) < len(v4_bm["chainsData"]) for c in v4_bm["chains"])
    assert all(0 <= int(e.get("i", 0)) < len(v4_ls["basicEventsData"]) for e in v4_ls["basicEvents"])
    assert counts["notes_count"] == 2 and counts["cuttable_count"] == 5


@pytest.mark.skipif(not HAS_REAL_DUMP_137, reason="PS5 1.37 dump not present")
def test_real_v137_sandbox_e2e_pack_q0_catalog_index_and_slot_guard(tmp_path):
    """
    Full 1.37 Handover Verification against real 01.037.000 files in an isolated sandbox:
      1. q0 baseline: pack patcher with 0 edits gives 100% byte-identical output
      2. Slot guard refuses multi-mode slots (FitBeat)
      3. End-to-end install into SpookyBeat (Vorbis FSB5 + v4 JSON + pack edit + cover paste)
      4. catalog.json and ampr_emu.index preserve exact byte length
      5. Single-slot restore returns pack, song bundle, catalog CRC, and index to exact stock bytes
    """
    from PIL import Image
    from ps5_bs_converter.bundle_v137 import patch_v137_pack_bundle

    # 1. q0 zero-edit byte-identical self-test on real extras_pack!
    real_aa_ps5 = os.path.join(REAL_DUMP_137, "Media", "StreamingAssets", "aa", "PS5")
    extras_fn = next(f for f in os.listdir(real_aa_ps5) if f.startswith("extras_pack_assets_all_"))
    shared_fn = next(f for f in os.listdir(real_aa_ps5) if f.startswith("sharedassets_assets_all_"))
    orig_extras_bytes = open(os.path.join(real_aa_ps5, extras_fn), "rb").read()
    assert patch_v137_pack_bundle(orig_extras_bytes, {}) == orig_extras_bytes

    # 2. Construct isolated 1.37 sandbox with spookybeat, fitbeat, extras_pack, sharedassets, catalog, and ampr_emu.index
    app137 = tmp_path / "PPSA15035-app"
    bdata = app137 / "Media" / "StreamingAssets" / "BeatmapLevelsData"
    aa_ps5 = app137 / "Media" / "StreamingAssets" / "aa" / "PS5"
    sce = app137 / "sce_sys"
    bdata.mkdir(parents=True)
    aa_ps5.mkdir(parents=True)
    sce.mkdir(parents=True)

    shutil.copy2(os.path.join(REAL_DUMP_137, "sce_sys", "param.json"), sce / "param.json")
    shutil.copy2(os.path.join(REAL_DUMP_137, "ampr_emu.index"), app137 / "ampr_emu.index")
    shutil.copy2(
        os.path.join(REAL_DUMP_137, "Media", "StreamingAssets", "aa", "catalog.json"),
        app137 / "Media" / "StreamingAssets" / "aa" / "catalog.json",
    )
    for s_name in ("spookybeat", "fitbeat"):
        shutil.copy2(
            os.path.join(REAL_DUMP_137, "Media", "StreamingAssets", "BeatmapLevelsData", s_name),
            bdata / s_name,
        )
    shutil.copy2(os.path.join(real_aa_ps5, extras_fn), aa_ps5 / extras_fn)
    shutil.copy2(os.path.join(real_aa_ps5, shared_fn), aa_ps5 / shared_fn)

    orig_spooky_bytes = (bdata / "spookybeat").read_bytes()
    orig_cat_bytes = (app137 / "Media" / "StreamingAssets" / "aa" / "catalog.json").read_bytes()
    orig_idx_bytes = (app137 / "ampr_emu.index").read_bytes()

    mgr137 = PS5BeatSaberManager(str(app137))
    assert mgr137.engine_mode == "bundle_v137"
    assert "SpookyBeat" in mgr137.slots
    assert "FitBeat" not in mgr137.slots  # Multi-mode excluded from selectable 1.37 list!

    # Slot guard must refuse FitBeat if called directly
    v2_zip = _make_v2_beatsaver_zip(duration_s=3.5)
    with pytest.raises(ValueError, match="multi-mode"):
        mgr137.install_custom_song(v2_zip, "FitBeat")

    # Add a cover image to a custom package & install into SpookyBeat (including Step G cover art!)
    pkg = load_custom_map(v2_zip)
    cov_bio = io.BytesIO()
    Image.new("RGBA", (256, 256), (0, 180, 255, 255)).save(cov_bio, format="PNG")
    pkg.cover_bytes = cov_bio.getvalue()

    updated = mgr137.install_custom_song(
        pkg, "SpookyBeat", environment_override="KDAEnvironment", install_cover_art=True
    )
    assert updated.is_custom
    assert updated.current_song_name == "Neon Horizon"

    # Verify catalog.json & ampr_emu.index kept the EXACT same byte length!
    assert len((app137 / "Media" / "StreamingAssets" / "aa" / "catalog.json").read_bytes()) == len(orig_cat_bytes)
    assert len((app137 / "ampr_emu.index").read_bytes()) == len(orig_idx_bytes)

    vinfo = mgr137.verify_slot("SpookyBeat")
    assert vinfo["audio_codec"] == 15  # Vorbis FSB5!
    assert vinfo["charts_verified"] == 5

    # Surgical single-slot restore of SpookyBeat -> restores song, pack, catalog CRC, and index byte-identically!
    mgr137.restore_slot("SpookyBeat")
    assert (bdata / "spookybeat").read_bytes() == orig_spooky_bytes
    assert (aa_ps5 / extras_fn).read_bytes() == orig_extras_bytes
    assert (app137 / "Media" / "StreamingAssets" / "aa" / "catalog.json").read_bytes() == orig_cat_bytes
    assert (app137 / "ampr_emu.index").read_bytes() == orig_idx_bytes


# ==============================================================================
# Real PS5 Dump Read-Only & Sandboxed End-to-End Integration Tests
# ==============================================================================

@pytest.mark.skipif(not HAS_REAL_DUMP, reason="PS5 dump not present")
def test_real_ps5_dump_readonly_byte_parity():
    """Verify zero-modification byte-exact round-trip on the real PS5 dump."""
    media = os.path.join(REAL_DUMP_ROOT, "Media")
    for fn in ("sharedassets48.assets", "sharedassets43.assets", "sharedassets44.assets", "sharedassets21.assets"):
        p = os.path.join(media, fn)
        with open(p, "rb") as f:
            orig_bytes = f.read()
        af = SerializedAssetsFile(p)
        assert af.to_bytes() == orig_bytes, f"Byte-exact round-trip failed for {fn}"

    mgr = PS5BeatSaberManager(REAL_DUMP_ROOT)
    assert len(mgr.slots) == 234
    assert len(mgr.list_slots(pack_category="all")) == 234
    assert len(mgr.list_slots(pack_category="dlc")) == 184
    assert len(mgr.list_slots(pack_category="free")) == 50


@pytest.mark.skipif(not HAS_REAL_DUMP, reason="PS5 dump not present")
def test_sandbox_end_to_end_install_replace_and_restore():
    """
    Clone .assets + lightweight stub .resource files into Temp\\opencode,
    install across Free, Multi-Mode, External-AudioClip, External-Level, and DLC-Preview slots,
    verify binary integrity, test surgical single-slot restore, and verify 100% byte-exact factory restore.
    """
    sandbox_base = os.path.join(tempfile.gettempdir(), "pytest_ps5_bs_sandbox")
    if os.path.exists(sandbox_base):
        shutil.rmtree(sandbox_base)

    app_dir = os.path.join(sandbox_base, "PPSA15035-app")
    media_dir = os.path.join(app_dir, "Media")
    sce_dir = os.path.join(app_dir, "sce_sys")
    os.makedirs(media_dir, exist_ok=True)
    os.makedirs(sce_dir, exist_ok=True)

    real_media = os.path.join(REAL_DUMP_ROOT, "Media")
    shutil.copy2(os.path.join(REAL_DUMP_ROOT, "sce_sys", "param.json"), os.path.join(sce_dir, "param.json"))

    copied_assets = [
        "resources.assets",
        "sharedassets3.assets",
        "sharedassets21.assets",
        "sharedassets43.assets",
        "sharedassets44.assets",
        "sharedassets48.assets",
    ]
    pristine_hashes = {}
    for fn in copied_assets:
        src = os.path.join(real_media, fn)
        dst = os.path.join(media_dir, fn)
        shutil.copy2(src, dst)
        with open(dst, "rb") as f:
            pristine_hashes[fn] = f.read()

    # Create stub 4096-byte .resource files so we never copy 3 GB during tests
    stub_res_sizes = {
        "sharedassets48.resource": 4096,
        "sharedassets44.resource": 2048,
        "sharedassets43.resource": 2048,
        "sharedassets21.resource": 1024,
    }
    for rfn, rsz in stub_res_sizes.items():
        with open(os.path.join(media_dir, rfn), "wb") as f:
            f.write(b"\xAA" * rsz)

    try:
        mgr = PS5BeatSaberManager(app_dir)
        initial_spooky_title = mgr.slots["SpookyBeat"].stock_song_name
        v2_zip = _make_v2_beatsaver_zip(duration_s=3.5)
        v4_zip = _make_v4_beatsaver_zip(duration_s=3.0)

        # 1. Standard Free Slot (SpookyBeat in Extras)
        s1 = mgr.install_custom_song(v2_zip, "SpookyBeat")
        assert s1.is_custom
        assert s1.current_song_name == "Neon Horizon"

        # 2. Multi-Mode Free Slot (FitBeat: Standard + NoArrows + 90 + 360)
        s2 = mgr.install_custom_song(v2_zip, "FitBeat", song_name_override="FitBeat Custom Party")
        assert s2.current_song_name == "FitBeat Custom Party"

        # 3. Overwrite FitBeat again with V4 map to test clean tail reallocation
        res48_after_first = os.path.getsize(os.path.join(media_dir, "sharedassets48.resource"))
        s2_v4 = mgr.install_custom_song(v4_zip, "FitBeat")
        assert s2_v4.current_song_name == "Cyber Pulse V4"
        res48_after_second = os.path.getsize(os.path.join(media_dir, "sharedassets48.resource"))
        # Second song is shorter (3.0s vs 3.5s), so it reused the freed block without growing the file!
        assert res48_after_second <= res48_after_first

        # 4. External AudioClip Free Slot ($100 Bills -> sharedassets44.assets#98)
        mgr.install_custom_song(v4_zip, "100Bills")

        # 5. External BeatmapLevelSO Free Slot (Escape -> sharedassets43.assets#676)
        mgr.install_custom_song(v2_zip, "Escape")

        # 6. DLC Slot with PreviewBeatmapLevelSO + *Preview AudioClip + Stage Override!
        mgr.install_custom_song(
            v2_zip, "BoulevardOfBrokenDreams", environment_override="TheWeekndEnvironment"
        )
        assert mgr.slots["BoulevardOfBrokenDreams"].installed_info["stage_environment"] == "TheWeekndEnvironment"

        # 7. Test DLC Batch Fill into Queen DLC pack!
        batch_dir = os.path.join(sandbox_base, "batch_zips")
        os.makedirs(batch_dir, exist_ok=True)
        with open(os.path.join(batch_dir, "01_song.zip"), "wb") as bf1:
            bf1.write(v2_zip)
        with open(os.path.join(batch_dir, "02_song.zip"), "wb") as bf2:
            bf2.write(v4_zip)
        batch_res = mgr.install_batch_into_pack(batch_dir, "Queen")
        assert len(batch_res) == 2

        # 8. Test Stage Neon Color Customizer & 3D .obj Platform Mesh Injection!
        assert len(mgr.list_environments()) >= 35
        mgr.apply_stage_color_theme("TheWeekndEnvironment", "Cyberpunk Neon (Pink & Cyan)")
        sample_obj = "v -2 0 -2\nv 2 0 -2\nv 2 0 2\nv -2 0 2\nf 1 2 3 4\n"
        mgr.replace_stage_mesh_from_obj("platform", sample_obj, is_path=False)

        # Verify all 7 installed slots pass strict FSB5 + UTF-16LE JSON + alignment checks!
        verified = mgr.verify_all_installed()
        assert len(verified) == 7

        # Test Printable HTML export
        html_out = os.path.join(sandbox_base, "lineup.html")
        mgr.export_song_list_html(html_out)
        assert os.path.isfile(html_out) and "Cyber Pulse V4" in open(html_out, encoding="utf-8").read()

        # Surgical single-slot restore of SpookyBeat while keeping the other 6 custom songs intact
        mgr.restore_slot("SpookyBeat")
        assert not mgr.slots["SpookyBeat"].is_custom
        assert mgr.slots["SpookyBeat"].current_song_name == initial_spooky_title
        assert len(mgr.verify_all_installed()) == 6

        # Full Factory Restore — verify EVERY .assets file and .resource file matches pristine bytes 100%!
        mgr.restore_all()
        assert len(mgr.verify_all_installed()) == 0
        for fn, orig_bytes in pristine_hashes.items():
            with open(os.path.join(media_dir, fn), "rb") as f:
                assert f.read() == orig_bytes, f"{fn} did not restore byte-identically!"

        for rfn, orig_sz in stub_res_sizes.items():
            rp = os.path.join(media_dir, rfn)
            assert os.path.getsize(rp) == orig_sz
            with open(rp, "rb") as f:
                assert f.read() == (b"\xAA" * orig_sz), f"{rfn} original bytes were altered!"

        # Bonus: Test real BeatSaver download & install in sandbox
        real_bs_zip = download_beatsaver_zip("30560", cache_dir=os.path.join(sandbox_base, "cache"))
        installed_real = mgr.install_custom_song(real_bs_zip, "CrabRave")
        assert installed_real.is_custom
        assert mgr.verify_slot("CrabRave")["audio_codec"] == 2
        mgr.restore_all()
    finally:
        if os.path.exists(sandbox_base):
            shutil.rmtree(sandbox_base, ignore_errors=True)
