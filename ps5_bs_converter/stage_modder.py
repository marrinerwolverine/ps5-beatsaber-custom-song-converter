"""
PS5 Beat Saber 3D Stage Mesh Modder (.obj -> Unity 2021.3.16f1 SerializedFile v22 Mesh).

On PS5 (target_platform=44), Unity Mesh (class_id=43) vertex and index buffers use standard
unswizzled little-endian IEEE-754 float32 streams and uint16/uint32 triangle indices.
By replacing Mesh geometry inside stage sharedassets (such as sharedassets3.assets) while
keeping existing GameObject/MeshRenderer bindings, custom 3D .obj models automatically inherit
Beat Saber's native PS5 ps5nggc neon/metallic/mirror shaders without needing Sony's shader SDK.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import List, Tuple

from .unity_assets import read_aligned_str, write_aligned_str

# Replaceable architectural & platform meshes in sharedassets3.assets (Default / BigMirror / Nice / Origins shared base)
STAGE_MESH_TARGETS = {
    "platform": ("sharedassets3.assets", 51, "PlayersPlaceCore", "Standing Floor Platform (3m x 2m)"),
    "left_building": ("sharedassets3.assets", 50, "LeftNearBuilding", "Left Stage Architecture / Pillar"),
    "right_building": ("sharedassets3.assets", 52, "RightNearBuilding", "Right Stage Architecture / Pillar"),
    "back_columns": ("sharedassets3.assets", 49, "GameBackColumns", "Far Background Columns"),
}


@dataclass
class ObjMeshGeometry:
    positions: List[Tuple[float, float, float]]
    normals: List[Tuple[float, float, float]]
    uvs: List[Tuple[float, float]]
    indices: List[int]  # triangle indices (multiple of 3)


def load_wavefront_obj(
    obj_path_or_text: str,
    is_path: bool = True,
    scale: float = 1.0,
    offset: Tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> ObjMeshGeometry:
    """
    Parse a Wavefront .obj 3D model (triangulating quads/n-gons and computing smooth/face normals
    if normals are omitted) ready for Unity PS5 Mesh injection.
    """
    if is_path:
        with open(obj_path_or_text, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    else:
        lines = obj_path_or_text.splitlines()

    raw_v: List[Tuple[float, float, float]] = []
    raw_vt: List[Tuple[float, float]] = []
    raw_vn: List[Tuple[float, float, float]] = []
    faces: List[List[Tuple[int, int, int]]] = []

    ox, oy, oz = offset
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        cmd = parts[0]
        if cmd == "v" and len(parts) >= 4:
            # Flip X for Unity's left-handed coordinate system convention
            raw_v.append((-float(parts[1]) * scale + ox, float(parts[2]) * scale + oy, float(parts[3]) * scale + oz))
        elif cmd == "vt" and len(parts) >= 3:
            raw_vt.append((float(parts[1]), float(parts[2])))
        elif cmd == "vn" and len(parts) >= 4:
            raw_vn.append((-float(parts[1]), float(parts[2]), float(parts[3])))
        elif cmd == "f" and len(parts) >= 4:
            verts = []
            for tok in parts[1:]:
                 chunks = tok.split("/")
                 vi = int(chunks[0])
                 ti = int(chunks[1]) if len(chunks) > 1 and chunks[1] else 0
                 ni = int(chunks[2]) if len(chunks) > 2 and chunks[2] else 0
                 vi = vi - 1 if vi > 0 else len(raw_v) + vi
                 ti = (ti - 1 if ti > 0 else len(raw_vt) + ti) if ti != 0 else -1
                 ni = (ni - 1 if ni > 0 else len(raw_vn) + ni) if ni != 0 else -1
                 verts.append((vi, ti, ni))
            # Fan-triangulate polygon (reversing winding due to X-axis handedness flip)
            for i in range(1, len(verts) - 1):
                faces.append([verts[0], verts[i + 1], verts[i]])

    if not raw_v or not faces:
        raise ValueError("Wavefront .obj contains no valid vertices or faces.")

    out_pos: List[Tuple[float, float, float]] = []
    out_nrm: List[Tuple[float, float, float]] = []
    out_uv: List[Tuple[float, float]] = []
    out_idx: List[int] = []
    vert_map = {}

    for tri in faces:
        p0, p1, p2 = raw_v[tri[0][0]], raw_v[tri[1][0]], raw_v[tri[2][0]]
        ux, uy, uz = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
        vx, vy, vz = p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        nlen = math.sqrt(nx * nx + ny * ny + nz * nz)
        face_n = (nx / nlen, ny / nlen, nz / nlen) if nlen > 1e-8 else (0.0, 1.0, 0.0)

        for vi, ti, ni in tri:
            nrm = raw_vn[ni] if (0 <= ni < len(raw_vn)) else face_n
            uv = raw_vt[ti] if (0 <= ti < len(raw_vt)) else (0.0, 0.0)
            key = (vi, round(nrm[0], 5), round(nrm[1], 5), round(nrm[2], 5), round(uv[0], 5), round(uv[1], 5))
            idx = vert_map.get(key)
            if idx is None:
                idx = len(out_pos)
                vert_map[key] = idx
                out_pos.append(raw_v[vi])
                out_nrm.append(nrm)
                out_uv.append(uv)
            out_idx.append(idx)

    if len(out_pos) > 65535:
        raise ValueError(f"Model has {len(out_pos)} vertices (exceeds 65,535 uint16 mesh limit; decimate first).")

    return ObjMeshGeometry(positions=out_pos, normals=out_nrm, uvs=out_uv, indices=out_idx)


def build_unity_ps5_mesh_blob(mesh_name: str, geom: ObjMeshGeometry) -> bytes:
    """
    Serialize an inline Unity 2021.3.16f1 PS5 Mesh (class_id=43) binary object.
    Matches the exact 48-byte interleaved vertex layout (Pos3f + Norm3f + Tan4f + UV2f)
    verified on PlayersPlaceCore in sharedassets3.assets.
    """
    vcount = len(geom.positions)
    icount = len(geom.indices)

    xs = [p[0] for p in geom.positions]
    ys = [p[1] for p in geom.positions]
    zs = [p[2] for p in geom.positions]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    min_z, max_z = min(zs), max(zs)
    cx, cy, cz = (min_x + max_x) * 0.5, (min_y + max_y) * 0.5, (min_z + max_z) * 0.5
    ex, ey, ez = (max_x - min_x) * 0.5, (max_y - min_y) * 0.5, (max_z - min_z) * 0.5
    aabb_bytes = struct.pack("<6f", cx, cy, cz, ex, ey, ez)

    out = bytearray()
    out += write_aligned_str(mesh_name)

    # m_SubMeshes (1 SubMesh = 48 bytes)
    out += struct.pack("<I", 1)
    out += struct.pack(
        "<6I",
        0,       # firstByte
        icount,  # indexCount
        0,       # topology = Triangles
        0,       # baseVertex
        0,       # firstVertex
        vcount,  # vertexCount
    )
    out += aabb_bytes

    # m_Shapes (vertices=0, shapes=0, channels=0, fullWeights=0) + m_BindPose(0) + m_BoneNameHashes(0)
    # + m_RootBoneNameHash(0) + m_BonesAABB(0) + m_VariableBoneCountWeights(0) = 9 uint32 zeros (36 bytes)
    out += b"\x00" * 36

    # m_MeshCompression(0), m_IsReadable(1), m_KeepVertices(1), m_KeepIndices(1) + m_IndexFormat(0 = UInt16)
    out += struct.pack("<4Bi", 0, 1, 1, 1, 0)

    # m_IndexBuffer (byte[] of uint16 indices, 4-byte aligned)
    idx_raw = struct.pack(f"<{icount}H", *geom.indices)
    idx_pad = (4 - (len(idx_raw) % 4)) % 4
    out += struct.pack("<I", len(idx_raw)) + idx_raw + (b"\x00" * idx_pad)

    # m_VertexData: vertexCount + 14 ChannelInfo structs + interleaved stream bytes
    out += struct.pack("<I", vcount)
    out += struct.pack("<I", 14)
    channels = [
        (0, 0, 0, 3),   # 0: Position (float32 x 3)
        (0, 12, 0, 3),  # 1: Normal   (float32 x 3)
        (0, 24, 0, 4),  # 2: Tangent  (float32 x 4)
        (0, 0, 0, 0),   # 3: Color    (unused)
        (0, 40, 0, 2),  # 4: UV0      (float32 x 2)
    ] + [(0, 0, 0, 0)] * 9
    for st_i, off_i, fmt_i, dim_i in channels:
        out += struct.pack("<4B", st_i, off_i, fmt_i, dim_i)

    vbuf = bytearray()
    for (px, py, pz), (nx, ny, nz), (u, v) in zip(geom.positions, geom.normals, geom.uvs):
        # Compute orthonormal tangent vector perpendicular to normal
        if abs(ny) < 0.99:
            tx, ty, tz = nz, 0.0, -nx
        else:
            tx, ty, tz = 1.0, 0.0, 0.0
        tlen = math.sqrt(tx * tx + ty * ty + tz * tz) or 1.0
        vbuf += struct.pack("<12f", px, py, pz, nx, ny, nz, tx / tlen, ty / tlen, tz / tlen, -1.0, u, v)

    vpad = (4 - (len(vbuf) % 4)) % 4
    out += struct.pack("<I", len(vbuf)) + vbuf + (b"\x00" * vpad)

    # m_CompressedMesh (164 zero bytes in Unity 2021.3.16f1 PS5)
    out += b"\x00" * 164

    # m_LocalAABB (24B) + m_MeshUsageFlags(4B) + m_BakedConvex(4B) + m_BakedTri(4B) + m_MeshMetrics(8B) + m_StreamData(16B)
    out += aabb_bytes
    out += struct.pack("<3I2f", 0, 0, 0, 1.0, 1.0)
    out += b"\x00" * 16  # inline m_StreamData (offset=0:u64, size=0:u32, path="":u32)

    return bytes(out)
