"""
Step G (1.37 Backend): PS5 'Standard 4KB' BC7 Untile/Retile & SongArtworks Cover Paster.

On PS5 Beat Saber 1.37, every song cover is a 160x160 sprite inside one 4096x4096 BC7
SpriteAtlas texture ('SongArtworks', 13 mips, 22,372,352 bytes in sharedassets_assets_all_*.bundle's .resS).
Layout verified against real hardware dump:
  - Data order in .resS: [4 KB tail (mips 7..12)][mip6][mip5][mip4][mip3][mip2][mip1][mip0 (16 MB)]
  - PS5 'Standard 4KB' tiling: 16-byte BC7 blocks inside 4 KB tiles (16x16 blocks = 256 blocks),
    block index bits (LSB -> MSB): y0, y1, x0, x1, y2, x2, y3, x3
  - Unity texture rows run bottom-up (images are flipped vertically before pasting)
  - Only the touched 4x4 BC7 blocks across mips 0..6 are re-encoded via etcpak
"""

from __future__ import annotations

import io
from typing import Dict, Tuple

import etcpak
import UnityPy
from PIL import Image

from .bundle_v137 import UnityFsBundle


def _ps5_tile_block_offset(bx: int, by: int, blocks_per_row: int) -> int:
    """Byte offset of 16-byte BC7 block (bx, by) inside a PS5 Standard-4KB tiled mip."""
    tiles_per_row = max(1, blocks_per_row // 16)
    tx, ix = divmod(bx, 16)
    ty, iy = divmod(by, 16)
    blk_in_tile = (
        ((iy & 1) << 0)
        | (((iy >> 1) & 1) << 1)
        | ((ix & 1) << 2)
        | (((ix >> 1) & 1) << 3)
        | (((iy >> 2) & 1) << 4)
        | (((ix >> 2) & 1) << 5)
        | (((iy >> 3) & 1) << 6)
        | (((ix >> 3) & 1) << 7)
    )
    return ((ty * tiles_per_row + tx) * 256 + blk_in_tile) * 16


def _mip_slices_4096_bc7() -> Dict[int, Tuple[int, int, int]]:
    """
    Return {mip_level: (byte_start_in_22MB_blob, byte_len, pixel_dim)} for mips 0..6.
    Order inside blob: 4096B tail, mip6 (64px), mip5 (128px), ..., mip1 (2048px), mip0 (4096px).
    """
    cursor = 4096  # 4 KB packed tail for mips 7..12
    out: Dict[int, Tuple[int, int, int]] = {}
    for mip in range(6, -1, -1):
        dim = 4096 >> mip
        blks = (dim // 4) * (dim // 4)
        sz = blks * 16
        out[mip] = (cursor, sz, dim)
        cursor += sz
    return out


def find_sprite_atlas_rects(sharedassets_bundle_bytes: bytes) -> Tuple[Dict[int, Tuple[int, int, int, int]], int, int]:
    """
    Inspect sharedassets_assets_all_*.bundle and return:
      - sprite_rects: {sprite_path_id: (x, y, width, height)} for all sprites in SongArtworks
      - tex_offset: byte offset of SongArtworks BC7 data inside .resS
      - tex_size: byte length (22,372,352)
    """
    env = UnityPy.load(sharedassets_bundle_bytes)
    bf = list(env.files.values())[0]
    cab = bf.files[[k for k in bf.files if not k.endswith((".resource", ".resS"))][0]]

    atlas_tt = None
    tex_offset = 0
    tex_size = 0

    for pid, rdr in cab.objects.items():
        if rdr.type.name == "SpriteAtlas":
            tt = rdr.read_typetree()
            if "SongArtworks" in tt.get("m_Name", ""):
                atlas_tt = tt
        elif rdr.type.name == "Texture2D":
            tt = rdr.read_typetree()
            if "SongArtworks" in tt.get("m_Name", ""):
                sd = tt.get("m_StreamData") or {}
                tex_offset = int(sd.get("offset", 0))
                tex_size = int(sd.get("size", 0))

    if atlas_tt is None or tex_size == 0:
        raise RuntimeError("Could not locate SongArtworks SpriteAtlas / Texture2D in sharedassets bundle")

    rdk_to_rect = {}
    for k, v in atlas_tt.get("m_RenderDataMap", []):
        tr = v.get("textureRect") or {}
        rdk_to_rect[str(k)] = (
            int(round(float(tr.get("x", 0)))),
            int(round(float(tr.get("y", 0)))),
            int(round(float(tr.get("width", 160)))),
            int(round(float(tr.get("height", 160)))),
        )

    sprite_rects: Dict[int, Tuple[int, int, int, int]] = {}
    for sp_pptr in atlas_tt.get("m_PackedSprites", []):
        sp_pid = int(sp_pptr.get("m_PathID", 0))
        if sp_pid in cab.objects:
            sp_tt = cab.objects[sp_pid].read_typetree()
            key_str = str(sp_tt.get("m_RenderDataKey"))
            if key_str in rdk_to_rect:
                sprite_rects[sp_pid] = rdk_to_rect[key_str]

    return sprite_rects, tex_offset, tex_size


def patch_sharedassets_covers(
    pristine_sharedassets_bytes: bytes,
    covers_by_sprite_pid: Dict[int, bytes],
) -> bytes:
    """
    Paste one or more custom cover images (keyed by Sprite path_id) into mips 0..6
    of SongArtworks inside sharedassets_assets_all_*.bundle.
    Leaves CAB and .resource byte-identical; updates only touched BC7 blocks in .resS.
    """
    if not covers_by_sprite_pid:
        return pristine_sharedassets_bytes

    sprite_rects, tex_offset, tex_size = find_sprite_atlas_rects(pristine_sharedassets_bytes)
    ub = UnityFsBundle.from_bytes(pristine_sharedassets_bytes)
    resS_node = next(n for n in ub.nodes if n.name.endswith(".resS"))
    resS_buf = bytearray(resS_node.data)

    mip_table = _mip_slices_4096_bc7()
    bc7_params = etcpak.BC7CompressBlockParams()

    for sp_pid, img_bytes in covers_by_sprite_pid.items():
        if sp_pid not in sprite_rects:
            continue
        rx, ry, rw, rh = sprite_rects[sp_pid]
        base_img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")

        for mip, (m_off, _m_sz, m_dim) in mip_table.items():
            mx = rx >> mip
            my = ry >> mip
            mw = max(4, rw >> mip)
            mh = max(4, rh >> mip)
            # Align to 4x4 BC7 block grid
            bx0 = mx // 4
            by0 = my // 4
            bw = max(1, mw // 4)
            bh = max(1, mh // 4)

            resized = base_img.resize((bw * 4, bh * 4), Image.Resampling.LANCZOS)
            flipped = resized.transpose(Image.FLIP_TOP_BOTTOM)
            rgba_bytes = flipped.tobytes("raw", "RGBA")
            encoded_bc7 = etcpak.compress_bc7(rgba_bytes, bw * 4, bh * 4, bc7_params)

            blocks_per_row = m_dim // 4
            abs_mip_base = tex_offset + m_off
            for ly in range(bh):
                for lx in range(bw):
                    src_blk = (ly * bw + lx) * 16
                    dst_blk = abs_mip_base + _ps5_tile_block_offset(bx0 + lx, by0 + ly, blocks_per_row)
                    resS_buf[dst_blk : dst_blk + 16] = encoded_bc7[src_blk : src_blk + 16]

    resS_node.data = bytes(resS_buf)
    rebuilt_bytes, _ = ub.to_bytes_and_crc()
    return rebuilt_bytes
