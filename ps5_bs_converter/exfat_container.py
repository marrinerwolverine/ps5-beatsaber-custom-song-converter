"""
PS5 Backport `.exfat` Container (AMPRIDX3) Reader/Writer.

Beat Saber backport builds are distributed/run as raw exFAT images
(`PPSA15035 Beat Saber (01.037.000).exfat`). The image is an exFAT shell
(boot sector + tiny FAT + upcase table) whose actual payload layout is:

  0x000000  exFAT boot sector (512B sectors, 64KB clusters)
  0x100000  FAT (first entries mark root/upcase chains; payload files use
            size-derived offsets, so the FAT stays ~empty)
  0x200000  cluster 2 (0xFF filler) + upcase table cluster
  0x3C0000  MASTER INDEX (`AMPRIDX3`, ver 3, 24-byte entries):
              header: magic(8) + <II> ver,entry_size + <Q> entry_count
              entries: <IIQQ> name_offset, name_len, file_size, mtime
              then a null-separated ASCII path table (paths like
              `/app0/Media/StreamingAssets/BeatmapLevelsData/<song>`)
  0x3D0000  packer bookkeeping table (NOT required for loading; left untouched)
  0x3E0000  PAYLOAD BLOB: files concatenated in master-index order, each
            rounded up to 64KB cluster alignment (NoFatChain-style).

The loader derives every file's offset from the ordered index sizes:
    offset(i) = DATA_BASE + sum(roundup_64k(size[j]) for j < i)

Because offsets are size-derived, writing a file whose cluster count
changes shifts every later file. This module therefore:

  * writes in place when the new data occupies the same 64KB cluster
    count as the original (no shifting, no data movement), and
  * otherwise re-locates the whole tail at its new derived offsets
    (copying backward when growing so nothing is clobbered), then
  * updates the master index size field in place (byte-length preserved),
    keeping the index and the physical layout perfectly consistent.
"""

from __future__ import annotations

import os
import re
import struct
from typing import Dict, List, Optional, Tuple

MAGIC = b"AMPRIDX3"
MASTER_INDEX_OFFSET = 0x3C0000
DATA_BASE = 0x3E0000
ALIGN = 0x10000  # 64 KB cluster alignment


def roundup_64k(n: int) -> int:
    return ((int(n) + ALIGN - 1) // ALIGN) * ALIGN


class ExfatContainerError(RuntimeError):
    pass


class _Entry:
    __slots__ = ("index", "path", "size", "mtime", "name_off", "name_len", "offset")

    def __init__(self, index: int, path: str, size: int, mtime: int, name_off: int, name_len: int):
        self.index = index
        self.path = path
        self.size = size
        self.mtime = mtime
        self.name_off = name_off
        self.name_len = name_len
        self.offset = 0


class ExfatBackportContainer:
    """
    Read/write access to a PS5 backport `.exfat` game image.
    """

    def __init__(self, image_path: str):
        self.path = os.path.abspath(image_path)
        self._f = open(self.path, "r+b")
        self._parse_boot()
        self._parse_master_index()
        self._compute_offsets()

    # ------------------------------------------------------------------ parse

    def _parse_boot(self) -> None:
        self._f.seek(0)
        boot = self._f.read(512)
        if len(boot) < 512 or boot[3:11] != b"EXFAT   ":
            raise ExfatContainerError(f"{self.path} is not an exFAT image")
        self.sector_size = 1 << boot[108]
        self.cluster_size = self.sector_size * (1 << boot[109])
        if self.cluster_size != ALIGN:
            # Layout constants hold for the standard 64KB-cluster backport images.
            raise ExfatContainerError(
                f"Unexpected exFAT cluster size {self.cluster_size} (expected 64KB)"
            )

    def _parse_master_index(self) -> None:
        self._f.seek(MASTER_INDEX_OFFSET)
        hdr = self._f.read(0x30)
        if len(hdr) < 0x30 or hdr[:8] != MAGIC:
            raise ExfatContainerError(
                "Master AMPRIDX3 index not found at 0x3C0000 (not a backport .exfat container?)"
            )
        ver, entry_size = struct.unpack_from("<II", hdr, 8)
        count = struct.unpack_from("<Q", hdr, 16)[0]
        if ver != 3 or entry_size != 24:
            raise ExfatContainerError(f"Unsupported AMPRIDX3 version={ver} entry_size={entry_size}")
        self.index_header = bytearray(hdr)
        self.entry_count = count
        self._f.seek(MASTER_INDEX_OFFSET + 0x30)
        entries_blob = bytearray(self._f.read(count * 24))
        if len(entries_blob) < count * 24:
            raise ExfatContainerError("Truncated master index entries")
        self._entries_blob = entries_blob
        str_base = MASTER_INDEX_OFFSET + 0x30 + count * 24
        self._f.seek(str_base)
        str_blob = self._f.read(0x20000)

        self.entries: List[_Entry] = []
        self.by_path: Dict[str, _Entry] = {}
        for i in range(count):
            noff, nlen, size, mtime = struct.unpack_from("<IIQQ", entries_blob, i * 24)
            name = str_blob[noff : noff + nlen].decode("utf-8", errors="replace")
            ent = _Entry(i, name, int(size), int(mtime), noff, nlen)
            self.entries.append(ent)
            self.by_path.setdefault(self.normalize(name), ent)
        self._str_base = str_base

    @staticmethod
    def normalize(path: str) -> str:
        p = path.replace("\\", "/").strip("/")
        if p.startswith("app0/"):
            p = p[len("app0/"):]
        return p

    def _compute_offsets(self) -> None:
        off = DATA_BASE
        for ent in self.entries:
            ent.offset = off
            off = roundup_64k(off + ent.size)
        self.payload_end = off
        img_size = os.path.getsize(self.path)
        if self.payload_end > img_size:
            raise ExfatContainerError("Index sizes exceed image size (corrupt container?)")
        self.free_tail = img_size - self.payload_end

    # ------------------------------------------------------------------- read

    def list_paths(self) -> List[str]:
        return [self.normalize(e.path) for e in self.entries]

    def exists(self, rel_path: str) -> bool:
        return self.normalize(rel_path) in self.by_path

    def getsize(self, rel_path: str) -> int:
        ent = self.by_path.get(self.normalize(rel_path))
        if ent is None:
            raise KeyError(rel_path)
        return ent.size

    def read_bytes(self, rel_path: str) -> bytes:
        ent = self.by_path.get(self.normalize(rel_path))
        if ent is None:
            raise KeyError(rel_path)
        self._f.seek(ent.offset)
        data = self._f.read(ent.size)
        if len(data) != ent.size:
            raise ExfatContainerError(f"Short read for {rel_path}")
        return data

    def read_head(self, rel_path: str, n: int) -> bytes:
        ent = self.by_path.get(self.normalize(rel_path))
        if ent is None:
            raise KeyError(rel_path)
        self._f.seek(ent.offset)
        return self._f.read(min(n, ent.size))

    # ------------------------------------------------------------------ write

    def _backup_entry_size(self, ent: _Entry) -> None:
        # keep in-memory blob authoritative
        pass

    def write_bytes(self, rel_path: str, data: bytes) -> None:
        """
        Replace a container file's content, preserving the size-derived
        offset invariant for every other file.
        """
        ent = self.by_path.get(self.normalize(rel_path))
        if ent is None:
            raise KeyError(f"Container has no entry for {rel_path}")
        new_size = len(data)
        old_clusters = roundup_64k(ent.size) // ALIGN
        new_clusters = roundup_64k(new_size) // ALIGN

        if new_clusters == old_clusters:
            self._f.seek(ent.offset)
            self._f.write(data)
            if new_size < ent.size:
                self._f.write(b"\x00" * (ent.size - new_size))
        else:
            self._relocate_tail(ent, data, new_size)

        self._set_entry_size(ent, new_size)
        self._f.flush()
        os.fsync(self._f.fileno())

    def _set_entry_size(self, ent: _Entry, new_size: int) -> None:
        ent.size = int(new_size)
        struct.pack_into("<Q", self._entries_blob, ent.index * 24 + 8, ent.size)
        self._f.seek(MASTER_INDEX_OFFSET + 0x30 + ent.index * 24 + 8)
        self._f.write(struct.pack("<Q", ent.size))

    def _relocate_tail(self, ent: _Entry, data: bytes, new_size: int) -> None:
        """
        Cluster count changed: rewrite this file and every later file at
        their new size-derived offsets, using overlap-safe (memmove-style)
        chunked copies so nothing is ever clobbered mid-read.
        """
        old_offsets = [e.offset for e in self.entries]
        old_sizes = [e.size for e in self.entries]

        new_sizes = list(old_sizes)
        new_sizes[ent.index] = new_size
        new_offsets = []
        off = DATA_BASE
        for sz in new_sizes:
            new_offsets.append(off)
            off = roundup_64k(off + sz)
        new_end = off
        img_size = os.path.getsize(self.path)
        if new_end > img_size:
            raise ExfatContainerError(
                f"Container full: need {new_end - img_size} more bytes "
                f"(free tail={self.free_tail})"
            )

        later = list(range(ent.index + 1, len(self.entries)))
        delta = new_offsets[ent.index + 1] - old_offsets[ent.index + 1] if later else 0

        # 1) copy later files in a clobber-safe order (end->start when growing)
        if delta > 0:
            later.reverse()

        for i in later:
            self._memmove_within_image(old_offsets[i], new_offsets[i], old_sizes[i])

        # 2) write the modified file at its (unchanged) offset; zero only its
        #    own new-slot tail (the vacated remainder now belongs to the
        #    relocated later files and must NOT be touched).
        self._f.seek(ent.offset)
        self._f.write(data)
        pad = roundup_64k(new_size) - new_size
        if pad > 0:
            self._f.write(b"\x00" * pad)

        # 3) zero the vacated gap when the tail shrank
        if delta < 0 and later:
            old_tail = old_offsets[-1] + roundup_64k(old_sizes[-1])
            new_tail = new_offsets[-1] + roundup_64k(new_sizes[-1])
            if new_tail < old_tail:
                self._f.seek(new_tail)
                self._f.write(b"\x00" * min(old_tail - new_tail, 64 * ALIGN))

        # 4) apply new offsets to the in-memory model (sizes unchanged for others)
        for i, e in enumerate(self.entries):
            e.offset = new_offsets[i]
        self.payload_end = new_end
        self.free_tail = img_size - new_end

    def _memmove_within_image(self, src: int, dst: int, size: int) -> None:
        """Overlap-safe chunked copy of `size` bytes from src to dst."""
        if src == dst or size <= 0:
            return
        CH = 1 << 20  # 1MB chunks
        if dst < src or dst >= src + size:
            # forward copy is safe (destination below source, or disjoint)
            moved = 0
            while moved < size:
                n = min(CH, size - moved)
                self._f.seek(src + moved)
                blob = self._f.read(n)
                self._f.seek(dst + moved)
                self._f.write(blob)
                moved += n
        else:
            # overlapping forward move: copy backward to avoid clobbering
            moved = size
            while moved > 0:
                n = min(CH, moved)
                moved -= n
                self._f.seek(src + moved)
                blob = self._f.read(n)
                self._f.seek(dst + moved)
                self._f.write(blob)

    # ------------------------------------------------------- index serialization

    def export_master_index(self) -> bytes:
        """Current master index bytes (header + entries + string table)."""
        self._f.seek(MASTER_INDEX_OFFSET + 0x30 + self.entry_count * 24)
        str_blob = self._f.read(0x20000)
        return bytes(self.index_header) + bytes(self._entries_blob) + bytes(str_blob)

    def entry_info(self, rel_path: str) -> Tuple[int, int]:
        """(offset, size) of a container file."""
        ent = self.by_path.get(self.normalize(rel_path))
        if ent is None:
            raise KeyError(rel_path)
        return ent.offset, ent.size

    def close(self) -> None:
        self._f.close()

    def __enter__(self) -> "ExfatBackportContainer":
        return self

    def __exit__(self, *_a) -> None:
        self.close()

    # ------------------------------------------------------------- discovery

    @staticmethod
    def looks_like_exfat_image(path: str) -> bool:
        try:
            if not os.path.isfile(path):
                return False
            with open(path, "rb") as f:
                boot = f.read(11)
                if len(boot) < 11 or boot[3:11] != b"EXFAT   ":
                    return False
            with open(path, "rb") as f:
                f.seek(MASTER_INDEX_OFFSET)
                return f.read(8) == MAGIC
        except OSError:
            return False


def find_exfat_image(start_path: str) -> Optional[str]:
    """Locate a backport .exfat image at/under the given path."""
    if ExfatBackportContainer.looks_like_exfat_image(start_path):
        return os.path.abspath(start_path)
    if os.path.isdir(start_path):
        for name in sorted(os.listdir(start_path)):
            if name.lower().endswith((".exfat", ".img")):
                cand = os.path.join(start_path, name)
                if ExfatBackportContainer.looks_like_exfat_image(cand):
                    return os.path.abspath(cand)
    return None


def sanitize_rel_for_backup(rel_path: str) -> str:
    """Filesystem-safe backup name for a container-relative path."""
    p = ExfatBackportContainer.normalize(rel_path)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", p)[:180] or "file"
