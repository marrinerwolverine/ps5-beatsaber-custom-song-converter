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
    free_slots = mgr.list_slots(free_only=True)
    assert len(free_slots) == 50


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

        # 6. DLC Slot with PreviewBeatmapLevelSO + *Preview AudioClip (BoulevardOfBrokenDreams)
        mgr.install_custom_song(v2_zip, "BoulevardOfBrokenDreams")

        # Verify all 5 installed slots pass strict FSB5 + UTF-16LE JSON + alignment checks!
        verified = mgr.verify_all_installed()
        assert len(verified) == 5

        # Test Printable HTML export
        html_out = os.path.join(sandbox_base, "lineup.html")
        mgr.export_song_list_html(html_out)
        assert os.path.isfile(html_out) and "Cyber Pulse V4" in open(html_out, encoding="utf-8").read()

        # Surgical single-slot restore of SpookyBeat while keeping the other 4 custom songs intact
        mgr.restore_slot("SpookyBeat")
        assert not mgr.slots["SpookyBeat"].is_custom
        assert mgr.slots["SpookyBeat"].current_song_name == "Spooky Beat"
        assert len(mgr.verify_all_installed()) == 4

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
