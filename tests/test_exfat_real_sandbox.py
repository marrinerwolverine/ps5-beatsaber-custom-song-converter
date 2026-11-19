"""
Real-container sandbox tests for the PS5 backport `.exfat` workflow.

Builds a miniature `.exfat` container (identical layout rules to the real
backport image) from files streamed out of the user's real image, then runs
a full 1.37 install -> verify -> restore cycle against it.

Set PS5_BEATSABER_EXFAT to the real image path to enable.
"""

import os
import struct
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ps5_bs_converter.exfat_container import (  # noqa: E402
    ALIGN,
    DATA_BASE,
    MAGIC,
    MASTER_INDEX_OFFSET,
    ExfatBackportContainer,
    roundup_64k,
)

REAL_EXFAT = os.environ.get("PS5_BEATSABER_EXFAT", "")


def build_container_from_files(tmp: str, files, spare_mb: int = 256) -> str:
    img = os.path.join(tmp, "sandbox.exfat")
    boot = bytearray(512)
    boot[0:3] = b"\xEB\x76\x90"
    boot[3:11] = b"EXFAT   "
    boot[108] = 9
    boot[109] = 7
    ents = b""
    names = b""
    str_off = 0
    for p, d in files:
        nb = p.encode()
        ents += struct.pack("<IIQQ", str_off, len(nb), len(d), 0)
        names += nb
        str_off += len(nb)
    hdr = MAGIC + struct.pack("<II", 3, 24) + struct.pack("<Q", len(files))
    hdr += struct.pack("<QQQ", len(names), 0, 0)
    hdr += b"\x00" * (0x30 - len(hdr))
    body = bytearray()
    for _, d in files:
        body += d
        body += b"\x00" * (roundup_64k(len(d)) - len(d))
    with open(img, "wb") as f:
        f.write(boot)
        f.seek(MASTER_INDEX_OFFSET)
        f.write(hdr + ents + names)
        f.seek(DATA_BASE)
        f.write(body)
        f.write(b"\x00" * (spare_mb * 1024 * 1024))
    return img


@pytest.fixture(scope="module")
def sandbox_container():
    """Mini container holding spookybeat + extras pack + catalog from the real image."""
    if not (REAL_EXFAT and os.path.isfile(REAL_EXFAT)):
        pytest.skip("no real .exfat image (set PS5_BEATSABER_EXFAT)")
    with ExfatBackportContainer(REAL_EXFAT) as c:
        song = c.read_bytes("Media/StreamingAssets/BeatmapLevelsData/spookybeat")
        pack = c.read_bytes(
            "Media/StreamingAssets/aa/PS5/extras_pack_assets_all_8986fcaa44deefd8da254e2c3f5c54dd.bundle"
        )
        shared = c.read_bytes(
            "Media/StreamingAssets/aa/PS5/sharedassets_assets_all_53fc2f8174795f60bfb2f9b17f9d3478.bundle"
        )
        catalog = c.read_bytes("Media/StreamingAssets/aa/catalog.json")
        param = c.read_bytes("sce_sys/param.json")

    files = [
        ("/app0/sce_sys/param.json", param),
        ("/app0/Media/StreamingAssets/aa/catalog.json", catalog),
        ("/app0/Media/StreamingAssets/aa/PS5/sharedassets_assets_all_53fc2f8174795f60bfb2f9b17f9d3478.bundle", shared),
        ("/app0/Media/StreamingAssets/aa/PS5/extras_pack_assets_all_8986fcaa44deefd8da254e2c3f5c54dd.bundle", pack),
        ("/app0/Media/StreamingAssets/BeatmapLevelsData/spookybeat", song),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        yield build_container_from_files(tmp, files)


@pytest.fixture()
def sandbox_paths(sandbox_container):
    with tempfile.TemporaryDirectory() as btmp:
        yield sandbox_container, btmp


def test_container_manager_install_verify_restore(sandbox_paths):
    from ps5_bs_converter.game_manager import PS5BeatSaberManager
    from ps5_bs_converter.beatmap_converter import load_custom_map
    from tests.test_ps5_converter import _make_v2_beatsaver_zip

    img, btmp = sandbox_paths

    mgr = PS5BeatSaberManager(img, backup_root=os.path.join(btmp, "backups"))
    assert mgr.engine_mode == "bundle_v137"
    assert "SpookyBeat" in mgr.slots
    assert not mgr.slots["SpookyBeat"].is_custom

    with open(os.path.join(btmp, "map.zip"), "wb") as f:
        f.write(_make_v2_beatsaver_zip(duration_s=6.0))
    pkg = load_custom_map(os.path.join(btmp, "map.zip"))

    orig_song = mgr._v137_read("Media/StreamingAssets/BeatmapLevelsData/spookybeat")
    orig_pack = mgr._v137_read(
        "Media/StreamingAssets/aa/PS5/extras_pack_assets_all_8986fcaa44deefd8da254e2c3f5c54dd.bundle"
    )
    orig_catalog = mgr._v137_read("Media/StreamingAssets/aa/catalog.json")

    updated = mgr.install_custom_song(pkg, "SpookyBeat", environment_override="KDAEnvironment")
    assert updated.is_custom
    v = mgr.verify_slot("SpookyBeat")
    assert v["audio_codec"] == 15
    assert v["charts_verified"] == 5

    # The container index stays consistent: every entry's bytes still readable
    # and spookybeat grew or shrank in its slot with offsets intact.
    with ExfatBackportContainer(img) as c:
        assert c.read_head("Media/StreamingAssets/aa/catalog.json", 1) == b"{"
        assert c.read_head("Media/StreamingAssets/BeatmapLevelsData/spookybeat", 7) == b"UnityFS"
        new_song_size = c.getsize("Media/StreamingAssets/BeatmapLevelsData/spookybeat")

    # Restore returns everything to byte-identical stock.
    mgr.restore_slot("SpookyBeat")
    assert mgr._v137_read("Media/StreamingAssets/BeatmapLevelsData/spookybeat") == orig_song
    assert mgr._v137_read(
        "Media/StreamingAssets/aa/PS5/extras_pack_assets_all_8986fcaa44deefd8da254e2c3f5c54dd.bundle"
    ) == orig_pack
    assert mgr._v137_read("Media/StreamingAssets/aa/catalog.json") == orig_catalog
    assert not mgr.slots["SpookyBeat"].is_custom
    mgr.close()
