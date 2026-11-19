"""
Tests for the PS5 backport `.exfat` container reader/writer.

Builds synthetic mini-containers with the exact production layout rules
(AMPRIDX3 master index at 0x3C0000, payload at 0x3E0000, 64KB alignment)
and verifies in-place writes, growing/shrinking relocations, overlap-safe
moves, and index consistency.

Set PS5_BEATSABER_EXFAT to a real backport image to additionally run
read-only checks against it.
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


def build_mini_container(tmp: str, files, spare_bytes=8 * 1024 * 1024) -> str:
    img = os.path.join(tmp, "mini.exfat")
    boot = bytearray(512)
    boot[0:3] = b"\xEB\x76\x90"
    boot[3:11] = b"EXFAT   "
    boot[108] = 9  # 512B sectors
    boot[109] = 7  # 128 sectors/cluster -> 64KB clusters
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
        f.write(b"\x00" * spare_bytes)
    return img


@pytest.fixture()
def mini():
    with tempfile.TemporaryDirectory() as tmp:
        files = [
            ("/app0/Media/one.bin", b"A" * 100_000),
            ("/app0/Media/two.bin", b"B" * 300_000),
            ("/app0/Media/three.bin", b"C" * 50_000),
            ("/app0/sce_sys/param.json", b"{}"),
        ]
        yield build_mini_container(tmp, files)


def test_read_layout_and_paths(mini):
    with ExfatBackportContainer(mini) as c:
        assert c.entry_count == 4
        assert c.exists("Media/one.bin")
        assert c.exists("/app0/Media/one.bin")  # normalized
        assert c.read_bytes("Media/one.bin") == b"A" * 100_000
        assert c.read_bytes("sce_sys/param.json") == b"{}"


def test_inplace_same_cluster_write(mini):
    with ExfatBackportContainer(mini) as c:
        off_before, _ = c.entry_info("Media/one.bin")
        c.write_bytes("Media/one.bin", b"D" * 100_500)  # same 2 clusters
    with ExfatBackportContainer(mini) as c:
        assert c.read_bytes("Media/one.bin") == b"D" * 100_500
        assert c.entry_info("Media/one.bin")[0] == off_before
        assert c.read_bytes("Media/two.bin") == b"B" * 300_000


def test_growing_relocation_moves_tail(mini):
    with ExfatBackportContainer(mini) as c:
        old_two_off = c.entry_info("Media/two.bin")[0]
        c.write_bytes("Media/one.bin", b"E" * 400_000)  # 2 -> 7 clusters
    with ExfatBackportContainer(mini) as c:
        assert c.read_bytes("Media/one.bin") == b"E" * 400_000
        assert c.read_bytes("Media/two.bin") == b"B" * 300_000
        assert c.read_bytes("Media/three.bin") == b"C" * 50_000
        assert c.read_bytes("sce_sys/param.json") == b"{}"
        # later files shifted up by 5 clusters
        assert c.entry_info("Media/two.bin")[0] == old_two_off + 5 * ALIGN


def test_shrinking_relocation(mini):
    with ExfatBackportContainer(mini) as c:
        c.write_bytes("Media/two.bin", b"F" * 4_000)  # 5 -> 1 cluster
    with ExfatBackportContainer(mini) as c:
        assert c.read_bytes("Media/two.bin") == b"F" * 4_000
        assert c.read_bytes("Media/one.bin") == b"A" * 100_000
        assert c.read_bytes("Media/three.bin") == b"C" * 50_000
        assert c.read_bytes("sce_sys/param.json") == b"{}"


def test_overlapping_self_move_is_safe():
    """A later file whose destination overlaps its own source must not corrupt."""
    with tempfile.TemporaryDirectory() as tmp:
        # a.bin grows by ~1.4 clusters -> b.bin shifts up while being ~12
        # clusters long: its own move overlaps itself.
        files = [
            ("/app0/Media/a.bin", b"A" * 100_000),
            ("/app0/Media/b.bin", bytes(range(256)) * 3_000),  # 768,000B distinct
            ("/app0/Media/c.bin", b"C" * 10_000),
        ]
        img = build_mini_container(tmp, files)
        with ExfatBackportContainer(img) as c:
            orig_b = c.read_bytes("Media/b.bin")
            orig_c = c.read_bytes("Media/c.bin")
            c.write_bytes("Media/a.bin", b"X" * 190_000)
        with ExfatBackportContainer(img) as c:
            assert c.read_bytes("Media/a.bin") == b"X" * 190_000
            assert c.read_bytes("Media/b.bin") == orig_b  # overlap-safe memmove!
            assert c.read_bytes("Media/c.bin") == orig_c


def test_container_full_guard():
    with tempfile.TemporaryDirectory() as tmp:
        files = [("/app0/Media/a.bin", b"A" * 100_000)]
        img = build_mini_container(tmp, files, spare_bytes=ALIGN)
        with ExfatBackportContainer(img) as c:
            with pytest.raises(Exception):
                c.write_bytes("Media/a.bin", b"B" * 4 * 1024 * 1024)


@pytest.mark.skipif(not REAL_EXFAT or not os.path.isfile(REAL_EXFAT), reason="no real .exfat image")
def test_real_container_readonly():
    with ExfatBackportContainer(REAL_EXFAT) as c:
        assert c.entry_count > 100
        assert c.exists("Media/StreamingAssets/BeatmapLevelsData/spookybeat")
        assert c.read_head("Media/StreamingAssets/BeatmapLevelsData/spookybeat", 8) == b"UnityFS\x00"
        assert c.read_head("Media/StreamingAssets/aa/catalog.json", 1) == b"{"
        assert c.free_tail > 100 * 1024 * 1024
