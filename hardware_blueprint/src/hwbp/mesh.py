"""Triangle-mesh generation, validation and export (STL / OBJ / GLB).

Every primitive here is built as a closed, consistently oriented (outward normals,
counter-clockwise winding) 2-manifold so the output is directly printable/sliceable.
Units are millimetres. Coordinate convention (matches glTF / three.js): +Y is up,
X is width, Z is depth.
"""

from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass, field
from typing import Iterable

Vec3 = tuple[float, float, float]
Tri = tuple[int, int, int]


@dataclass
class Mesh:
    """Indexed triangle mesh."""

    vertices: list[Vec3] = field(default_factory=list)
    triangles: list[Tri] = field(default_factory=list)
    name: str = "mesh"

    # ------------------------------------------------------------------ building
    def add_vertex(self, v: Vec3) -> int:
        self.vertices.append((float(v[0]), float(v[1]), float(v[2])))
        return len(self.vertices) - 1

    def add_quad(self, a: int, b: int, c: int, d: int) -> None:
        """Add quad a-b-c-d (counter-clockwise seen from outside) as two triangles."""
        self.triangles.append((a, b, c))
        self.triangles.append((a, c, d))

    def merge(self, other: "Mesh") -> "Mesh":
        """Append another mesh (as an additional shell) in place; returns self."""
        off = len(self.vertices)
        self.vertices.extend(other.vertices)
        self.triangles.extend((a + off, b + off, c + off) for a, b, c in other.triangles)
        return self

    def translated(self, dx: float, dy: float, dz: float) -> "Mesh":
        return Mesh([(x + dx, y + dy, z + dz) for x, y, z in self.vertices], list(self.triangles), self.name)

    def scaled(self, sx: float, sy: float, sz: float) -> "Mesh":
        tris = list(self.triangles)
        if sx * sy * sz < 0:  # mirroring flips winding
            tris = [(a, c, b) for a, b, c in tris]
        return Mesh([(x * sx, y * sy, z * sz) for x, y, z in self.vertices], tris, self.name)

    # ------------------------------------------------------------------ metrics
    def bounds(self) -> tuple[Vec3, Vec3]:
        if not self.vertices:
            return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
        xs, ys, zs = zip(*self.vertices)
        return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))

    def size(self) -> Vec3:
        lo, hi = self.bounds()
        return hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]

    def volume(self) -> float:
        """Signed volume via the divergence theorem (positive for outward winding)."""
        total = 0.0
        v = self.vertices
        for a, b, c in self.triangles:
            (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = v[a], v[b], v[c]
            total += x1 * (y2 * z3 - y3 * z2) - x2 * (y1 * z3 - y3 * z1) + x3 * (y1 * z2 - y2 * z1)
        return total / 6.0

    def surface_area(self) -> float:
        return sum(_tri_area(self.vertices[a], self.vertices[b], self.vertices[c]) for a, b, c in self.triangles)

    def is_watertight(self) -> bool:
        """True when every directed edge is matched by exactly one opposite edge (closed, oriented manifold)."""
        edges: dict[tuple[int, int], int] = {}
        for a, b, c in self.triangles:
            for e in ((a, b), (b, c), (c, a)):
                edges[e] = edges.get(e, 0) + 1
        for (u, w), n in edges.items():
            if n != 1 or edges.get((w, u)) != 1:
                return False
        return bool(edges)

    def face_normals(self) -> list[Vec3]:
        out = []
        for a, b, c in self.triangles:
            out.append(_normal(self.vertices[a], self.vertices[b], self.vertices[c]))
        return out


# ---------------------------------------------------------------------- helpers
def _sub(a: Vec3, b: Vec3) -> Vec3:
    return a[0] - b[0], a[1] - b[1], a[2] - b[2]


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]


def _normal(a: Vec3, b: Vec3, c: Vec3) -> Vec3:
    n = _cross(_sub(b, a), _sub(c, a))
    length = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)
    if length == 0:
        return 0.0, 0.0, 0.0
    return n[0] / length, n[1] / length, n[2] / length


def _tri_area(a: Vec3, b: Vec3, c: Vec3) -> float:
    n = _cross(_sub(b, a), _sub(c, a))
    return 0.5 * math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)


# ---------------------------------------------------------------------- primitives
def box(w: float, h: float, d: float, name: str = "box", center: Vec3 = (0.0, 0.0, 0.0)) -> Mesh:
    """Axis-aligned box of size w (x) * h (y) * d (z) centred on ``center``."""
    if min(w, h, d) <= 0:
        raise ValueError(f"box dimensions must be positive, got {w}x{h}x{d}")
    cx, cy, cz = center
    x0, x1, y0, y1, z0, z1 = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2, cz - d / 2, cz + d / 2
    m = Mesh(name=name)
    p = [m.add_vertex(v) for v in _box_corners(x0, x1, y0, y1, z0, z1)]
    _box_faces(m, p)
    return m


def _box_corners(x0: float, x1: float, y0: float, y1: float, z0: float, z1: float) -> list[Vec3]:
    return [
        (x0, y0, z0),
        (x1, y0, z0),
        (x1, y1, z0),
        (x0, y1, z0),
        (x0, y0, z1),
        (x1, y0, z1),
        (x1, y1, z1),
        (x0, y1, z1),
    ]


def _box_faces(m: Mesh, p: list[int]) -> None:
    m.add_quad(p[0], p[3], p[2], p[1])  # -z
    m.add_quad(p[4], p[5], p[6], p[7])  # +z
    m.add_quad(p[0], p[1], p[5], p[4])  # -y
    m.add_quad(p[3], p[7], p[6], p[2])  # +y
    m.add_quad(p[0], p[4], p[7], p[3])  # -x
    m.add_quad(p[1], p[2], p[6], p[5])  # +x


def cylinder(
    radius: float, height: float, segments: int = 24, name: str = "cylinder", center: Vec3 = (0, 0, 0)
) -> Mesh:
    """Closed cylinder along +Y."""
    if radius <= 0 or height <= 0 or segments < 3:
        raise ValueError("cylinder needs radius>0, height>0, segments>=3")
    cx, cy, cz = center
    y0, y1 = cy - height / 2, cy + height / 2
    m = Mesh(name=name)
    bot = [m.add_vertex(_ring_pt(cx, y0, cz, radius, i, segments)) for i in range(segments)]
    top = [m.add_vertex(_ring_pt(cx, y1, cz, radius, i, segments)) for i in range(segments)]
    cb, ct = m.add_vertex((cx, y0, cz)), m.add_vertex((cx, y1, cz))
    for i in range(segments):
        j = (i + 1) % segments
        m.add_quad(bot[i], top[i], top[j], bot[j])
        m.triangles.append((cb, bot[i], bot[j]))
        m.triangles.append((ct, top[j], top[i]))
    return m


def tube(
    outer_r: float, inner_r: float, height: float, segments: int = 24, name: str = "tube", center: Vec3 = (0, 0, 0)
) -> Mesh:
    """Hollow cylinder (e.g. a standoff with a screw hole) along +Y."""
    if not 0 < inner_r < outer_r:
        raise ValueError("tube needs 0 < inner_r < outer_r")
    cx, cy, cz = center
    y0, y1 = cy - height / 2, cy + height / 2
    m = Mesh(name=name)
    ob = [m.add_vertex(_ring_pt(cx, y0, cz, outer_r, i, segments)) for i in range(segments)]
    ot = [m.add_vertex(_ring_pt(cx, y1, cz, outer_r, i, segments)) for i in range(segments)]
    ib = [m.add_vertex(_ring_pt(cx, y0, cz, inner_r, i, segments)) for i in range(segments)]
    it = [m.add_vertex(_ring_pt(cx, y1, cz, inner_r, i, segments)) for i in range(segments)]
    for i in range(segments):
        j = (i + 1) % segments
        m.add_quad(ob[i], ot[i], ot[j], ob[j])  # outer wall
        m.add_quad(ib[j], it[j], it[i], ib[i])  # inner wall (faces inward to the hole)
        m.add_quad(ot[i], it[i], it[j], ot[j])  # top annulus
        m.add_quad(ob[j], ib[j], ib[i], ob[i])  # bottom annulus
    return m


def _ring_pt(cx: float, y: float, cz: float, r: float, i: int, n: int) -> Vec3:
    # Angle order chosen so that quads (bot_i, top_i, top_j, bot_j) face outward (verified by signed volume).
    a = 2 * math.pi * i / n
    return cx + r * math.cos(a), y, cz + r * math.sin(a)


def rect_frame(
    w: float, d: float, thickness: float, hole_w: float, hole_d: float, name: str = "frame", hole_offset=(0.0, 0.0)
) -> Mesh:
    """Flat plate (in XZ, thickness along Y, centred at origin) with a rectangular through-hole.

    Used for lids with display windows and for bezels.
    """
    ox, oz = hole_offset
    if not (0 < hole_w and 0 < hole_d):
        raise ValueError("hole must have positive size")
    if abs(ox) + hole_w / 2 >= w / 2 or abs(oz) + hole_d / 2 >= d / 2:
        raise ValueError("hole must lie strictly inside the plate")
    y0, y1 = -thickness / 2, thickness / 2
    m = Mesh(name=name)
    outer = [(-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2)]
    inner = [(ox - hole_w / 2, oz - hole_d / 2), (ox + hole_w / 2, oz - hole_d / 2)]
    inner += [(ox + hole_w / 2, oz + hole_d / 2), (ox - hole_w / 2, oz + hole_d / 2)]
    ob = [m.add_vertex((x, y0, z)) for x, z in outer]
    ot = [m.add_vertex((x, y1, z)) for x, z in outer]
    ib = [m.add_vertex((x, y0, z)) for x, z in inner]
    it = [m.add_vertex((x, y1, z)) for x, z in inner]
    for i in range(4):
        j = (i + 1) % 4
        m.add_quad(ot[i], ot[j], ob[j], ob[i])  # outer side walls
        m.add_quad(it[j], it[i], ib[i], ib[j])  # hole walls
        m.add_quad(it[i], it[j], ot[j], ot[i])  # top ring
        m.add_quad(ib[j], ib[i], ob[i], ob[j])  # bottom ring
    return m


def open_box(w: float, h: float, d: float, wall: float, floor: float | None = None, name: str = "shell") -> Mesh:
    """Open-top enclosure shell (tray) centred at origin; outer size w*h*d, wall thickness ``wall``.

    Built as a single closed manifold: outer skin + top rim + inner skin + inner floor.
    """
    floor = wall if floor is None else floor
    if wall <= 0 or 2 * wall >= min(w, d) or floor >= h:
        raise ValueError("invalid wall/floor thickness for open_box")
    x0, x1, z0, z1 = -w / 2, w / 2, -d / 2, d / 2
    y0, y1 = -h / 2, h / 2
    ix0, ix1, iz0, iz1, iy0 = x0 + wall, x1 - wall, z0 + wall, z1 - wall, y0 + floor
    m = Mesh(name=name)
    o = [m.add_vertex(v) for v in _box_corners(x0, x1, y0, y1, z0, z1)]
    i = [m.add_vertex(v) for v in _box_corners(ix0, ix1, iy0, y1, iz0, iz1)]
    # outer skin (no top)
    m.add_quad(o[0], o[3], o[2], o[1])
    m.add_quad(o[4], o[5], o[6], o[7])
    m.add_quad(o[0], o[1], o[5], o[4])
    m.add_quad(o[0], o[4], o[7], o[3])
    m.add_quad(o[1], o[2], o[6], o[5])
    # top rim between outer and inner top rectangles (outer top: 3,2,6,7 ; inner top: 3,2,6,7)
    m.add_quad(o[3], o[7], i[7], i[3])
    m.add_quad(o[7], o[6], i[6], i[7])
    m.add_quad(o[6], o[2], i[2], i[6])
    m.add_quad(o[2], o[3], i[3], i[2])
    # inner skin: faces point into the cavity (reverse of a box's outward faces)
    m.add_quad(i[0], i[1], i[2], i[3])  # inner -z wall, normal +z
    m.add_quad(i[4], i[7], i[6], i[5])  # inner +z wall, normal -z
    m.add_quad(i[0], i[4], i[5], i[1])  # inner floor, normal +y
    m.add_quad(i[0], i[3], i[7], i[4])  # inner -x wall, normal +x
    m.add_quad(i[1], i[5], i[6], i[2])  # inner +x wall, normal -x
    return m


def standoff_plate(w: float, d: float, plate_t: float, post_h: float, post_r: float, inset: float) -> Mesh:
    """Mounting plate with four standoff posts (multi-shell mesh, each shell closed)."""
    m = box(w, plate_t, d, name="mount_plate")
    for sx in (-1, 1):
        for sz in (-1, 1):
            post = tube(post_r, post_r * 0.45, post_h, segments=16, name="post")
            m.merge(post.translated(sx * (w / 2 - inset), plate_t / 2 + post_h / 2, sz * (d / 2 - inset)))
    return m


# ---------------------------------------------------------------------- writers
def to_stl_ascii(mesh: Mesh) -> str:
    lines = [f"solid {mesh.name}"]
    for (a, b, c), n in zip(mesh.triangles, mesh.face_normals()):
        lines.append(f"  facet normal {n[0]:.6e} {n[1]:.6e} {n[2]:.6e}")
        lines.append("    outer loop")
        for idx in (a, b, c):
            x, y, z = mesh.vertices[idx]
            lines.append(f"      vertex {x:.6e} {y:.6e} {z:.6e}")
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append(f"endsolid {mesh.name}")
    return "\n".join(lines) + "\n"


def to_stl_binary(mesh: Mesh) -> bytes:
    header = mesh.name.encode("ascii", "replace")[:80].ljust(80, b" ")
    out = bytearray(header)
    out += struct.pack("<I", len(mesh.triangles))
    for (a, b, c), n in zip(mesh.triangles, mesh.face_normals()):
        out += struct.pack("<3f", *n)
        for idx in (a, b, c):
            out += struct.pack("<3f", *mesh.vertices[idx])
        out += b"\x00\x00"
    return bytes(out)


def read_stl_binary(data: bytes) -> Mesh:
    """Parse a binary STL (vertices are de-duplicated)."""
    (count,) = struct.unpack_from("<I", data, 80)
    m = Mesh(name=data[:80].decode("ascii", "replace").strip() or "stl")
    index: dict[Vec3, int] = {}
    off = 84
    for _ in range(count):
        verts = struct.unpack_from("<12f", data, off)[3:]
        tri = []
        for k in range(3):
            v = (round(verts[3 * k], 5), round(verts[3 * k + 1], 5), round(verts[3 * k + 2], 5))
            if v not in index:
                index[v] = m.add_vertex(v)
            tri.append(index[v])
        m.triangles.append((tri[0], tri[1], tri[2]))
        off += 50
    return m


def to_obj(meshes: Iterable[Mesh]) -> str:
    lines = ["# hwbp OBJ export (units: mm)"]
    base = 1
    for mesh in meshes:
        lines.append(f"o {mesh.name}")
        lines.extend(f"v {x:.4f} {y:.4f} {z:.4f}" for x, y, z in mesh.vertices)
        lines.extend(f"f {a + base} {b + base} {c + base}" for a, b, c in mesh.triangles)
        base += len(mesh.vertices)
    return "\n".join(lines) + "\n"


@dataclass
class SceneNode:
    """A positioned, coloured mesh in an assembly scene (for GLB export)."""

    name: str
    mesh: Mesh
    translation: Vec3 = (0.0, 0.0, 0.0)
    color: tuple[float, float, float, float] = (0.7, 0.7, 0.7, 1.0)


def to_glb(nodes: list[SceneNode]) -> bytes:
    """Serialise scene nodes to a binary glTF 2.0 (GLB) file with flat-shaded meshes."""
    bin_chunk = bytearray()
    accessors, buffer_views, meshes, gl_nodes, materials = [], [], [], [], []
    for node in nodes:
        # Flat shading: un-index so every triangle gets its own face normal.
        positions: list[float] = []
        normals: list[float] = []
        for (a, b, c), n in zip(node.mesh.triangles, node.mesh.face_normals()):
            for idx in (a, b, c):
                positions.extend(node.mesh.vertices[idx])
                normals.extend(n)
        count = len(positions) // 3
        if count == 0:
            continue
        lo, hi = node.mesh.bounds()
        pos_view = _append_view(bin_chunk, buffer_views, struct.pack(f"<{len(positions)}f", *positions))
        nrm_view = _append_view(bin_chunk, buffer_views, struct.pack(f"<{len(normals)}f", *normals))
        accessors.append(
            {
                "bufferView": pos_view,
                "componentType": 5126,
                "count": count,
                "type": "VEC3",
                "min": list(lo),
                "max": list(hi),
            }
        )
        accessors.append({"bufferView": nrm_view, "componentType": 5126, "count": count, "type": "VEC3"})
        materials.append(
            {
                "name": f"{node.name}_mat",
                "pbrMetallicRoughness": {
                    "baseColorFactor": list(node.color),
                    "metallicFactor": 0.0,
                    "roughnessFactor": 0.8,
                },
                "alphaMode": "BLEND" if node.color[3] < 1 else "OPAQUE",
                "doubleSided": True,
            }
        )
        meshes.append(
            {
                "name": node.name,
                "primitives": [
                    {
                        "attributes": {"POSITION": len(accessors) - 2, "NORMAL": len(accessors) - 1},
                        "material": len(materials) - 1,
                    }
                ],
            }
        )
        gl_nodes.append({"name": node.name, "mesh": len(meshes) - 1, "translation": list(node.translation)})
    gltf = {
        "asset": {"version": "2.0", "generator": "hwbp"},
        "scene": 0,
        "scenes": [{"nodes": list(range(len(gl_nodes)))}],
        "nodes": gl_nodes,
        "meshes": meshes,
        "materials": materials,
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(bin_chunk)}],
    }
    json_bytes = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    json_bytes += b" " * (-len(json_bytes) % 4)
    bin_bytes = bytes(bin_chunk) + b"\x00" * (-len(bin_chunk) % 4)
    total = 12 + 8 + len(json_bytes) + 8 + len(bin_bytes)
    out = struct.pack("<4sII", b"glTF", 2, total)
    out += struct.pack("<I4s", len(json_bytes), b"JSON") + json_bytes
    out += struct.pack("<I4s", len(bin_bytes), b"BIN\x00") + bin_bytes
    return out


def _append_view(buf: bytearray, views: list[dict], data: bytes) -> int:
    buf.extend(b"\x00" * (-len(buf) % 4))
    views.append({"buffer": 0, "byteOffset": len(buf), "byteLength": len(data), "target": 34962})
    buf.extend(data)
    return len(views) - 1


def read_glb_json(data: bytes) -> dict:
    """Return the JSON chunk of a GLB (used for validation/tests)."""
    magic, version, length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF" or version != 2 or length != len(data):
        raise ValueError("not a valid GLB 2.0 file")
    json_len, chunk_type = struct.unpack_from("<I4s", data, 12)
    if chunk_type != b"JSON":
        raise ValueError("first GLB chunk must be JSON")
    return json.loads(data[20 : 20 + json_len].decode("utf-8"))
