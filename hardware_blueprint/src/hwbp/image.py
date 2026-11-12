"""Picture -> mesh / CAD and picture -> blueprint inputs.

Deterministic, dependency-free pipeline (Pillow is used for JPEG/WebP when installed; PNG/PGM/PPM are
decoded natively):

    decode -> grayscale -> Otsu threshold -> largest connected component (object mask)
           -> boundary trace (Moore neighbour) -> Ramer-Douglas-Peucker simplification
           -> scale to millimetres -> one of:
                extrude  : polygon prism (2.5D brackets, plates, logos, enclosure outlines)
                relief   : height map from brightness (lithophane / embossed panel)
                revolve  : lathe the silhouette's half-width profile (vases, knobs, bottles)

Picture -> blueprint: a ``VisionModel`` (any multimodal LLM) turns the image into a requirements prompt
following ``VISION_PROMPT``; the silhouette analysis supplies the device form factor (outline + aspect)
that the mechanical stage uses for the enclosure footprint.
"""

from __future__ import annotations

import io
import math
import struct
import zlib
from dataclasses import dataclass, field
from typing import Protocol

from hwbp import mesh as M


# ------------------------------------------------------------------------------ decoding
@dataclass
class Image:
    width: int
    height: int
    gray: list[list[float]]  # rows of 0..1 luminance
    alpha: list[list[float]] | None = None


def decode(data: bytes) -> Image:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _decode_png(data)
    if data[:2] in (b"P5", b"P6", b"P2", b"P3"):
        return _decode_pnm(data)
    try:  # optional Pillow for JPEG / WebP / GIF / BMP
        from PIL import Image as PILImage  # type: ignore[import-not-found]
    except ImportError as e:
        raise ValueError("unsupported image format (PNG/PGM/PPM built in; install Pillow for JPEG/WebP)") from e
    img = PILImage.open(io.BytesIO(data)).convert("LA")
    w, h = img.size
    px = list(img.getdata())
    gray = [[px[y * w + x][0] / 255 for x in range(w)] for y in range(h)]
    alpha = [[px[y * w + x][1] / 255 for x in range(w)] for y in range(h)]
    return Image(w, h, gray, alpha)


def _decode_pnm(data: bytes) -> Image:
    tokens, i = [], 0
    while len(tokens) < 4:
        while data[i : i + 1].isspace():
            i += 1
        if data[i : i + 1] == b"#":
            while data[i : i + 1] not in (b"\n", b""):
                i += 1
            continue
        j = i
        while not data[j : j + 1].isspace():
            j += 1
        tokens.append(data[i:j])
        i = j
    magic, w, h, maxv = tokens[0], int(tokens[1]), int(tokens[2]), int(tokens[3])
    body = data[i + 1 :]
    ch = 3 if magic in (b"P6", b"P3") else 1
    if magic in (b"P5", b"P6"):
        vals = list(body[: w * h * ch])
    else:
        vals = [int(t) for t in body.split()][: w * h * ch]
    gray = []
    for y in range(h):
        row = []
        for x in range(w):
            k = (y * w + x) * ch
            row.append((_lum(vals[k], vals[k + 1], vals[k + 2]) if ch == 3 else vals[k]) / maxv)
        gray.append(row)
    return Image(w, h, gray)


def _lum(r: float, g: float, b: float) -> float:
    return 0.299 * r + 0.587 * g + 0.114 * b


def _decode_png(data: bytes) -> Image:
    pos, idat, palette, trns = 8, b"", None, None
    w = h = depth = ctype = interlace = 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"PLTE":
            palette = [tuple(chunk[i : i + 3]) for i in range(0, len(chunk), 3)]
        elif kind == b"tRNS":
            trns = chunk
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break
    if depth != 8 or interlace:
        raise ValueError("only 8-bit, non-interlaced PNG is supported natively (install Pillow for others)")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    stride = w * channels
    raw = zlib.decompress(idat)
    rows: list[bytearray] = []
    prev = bytearray(stride)
    i = 0
    for _ in range(h):
        ftype, line = raw[i], bytearray(raw[i + 1 : i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - channels] if x >= channels else 0
            b = prev[x]
            c = prev[x - channels] if x >= channels else 0
            if ftype == 1:
                line[x] = (line[x] + a) & 255
            elif ftype == 2:
                line[x] = (line[x] + b) & 255
            elif ftype == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(line)
        prev = line
    gray, alpha = [], []
    for line in rows:
        grow, arow = [], []
        for x in range(w):
            px = line[x * channels : (x + 1) * channels]
            if ctype == 0:
                g, a = px[0], 255
            elif ctype == 2:
                g, a = _lum(*px), 255
            elif ctype == 3:
                r, gg, b = palette[px[0]]
                g = _lum(r, gg, b)
                a = trns[px[0]] if trns and px[0] < len(trns) else 255
            elif ctype == 4:
                g, a = px[0], px[1]
            else:
                g, a = _lum(px[0], px[1], px[2]), px[3]
            grow.append(g / 255)
            arow.append(a / 255)
        gray.append(grow)
        alpha.append(arow)
    return Image(w, h, gray, alpha if ctype in (3, 4, 6) else None)


def encode_png_gray(rows: list[list[float]]) -> bytes:
    """Minimal 8-bit grayscale PNG encoder (used for tests and mask previews)."""
    h, w = len(rows), len(rows[0])
    raw = b"".join(b"\x00" + bytes(max(0, min(255, int(round(v * 255)))) for v in row) for row in rows)

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


# ------------------------------------------------------------------------------ segmentation
def downsample(img: Image, max_side: int = 256) -> Image:
    scale = max(img.width, img.height) / max_side
    if scale <= 1:
        return img
    w, h = max(1, int(img.width / scale)), max(1, int(img.height / scale))
    gray = [
        [img.gray[min(img.height - 1, int(y * scale))][min(img.width - 1, int(x * scale))] for x in range(w)]
        for y in range(h)
    ]
    alpha = None
    if img.alpha:
        alpha = [
            [img.alpha[min(img.height - 1, int(y * scale))][min(img.width - 1, int(x * scale))] for x in range(w)]
            for y in range(h)
        ]
    return Image(w, h, gray, alpha)


def otsu_threshold(img: Image) -> float:
    hist = [0] * 256
    for row in img.gray:
        for v in row:
            hist[min(255, int(v * 255))] += 1
    total = img.width * img.height
    sum_all = sum(i * c for i, c in enumerate(hist))
    w_b = sum_b = 0.0
    best, thr, thr_hi = -1.0, 127, 127
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b, m_f = sum_b / w_b, (sum_all - sum_b) / w_f
        between = w_b * w_f * (m_b - m_f) ** 2
        if between > best + 1e-9:
            best, thr, thr_hi = between, t, t
        elif abs(between - best) <= 1e-9:
            thr_hi = t  # plateau (e.g. a pure two-level image): take its midpoint
    return ((thr + thr_hi) / 2) / 255


def object_mask(img: Image) -> list[list[bool]]:
    """Foreground mask: alpha if the image has transparency, else Otsu with border-majority polarity."""
    if img.alpha and any(a < 0.5 for row in img.alpha for a in row):
        mask = [[a >= 0.5 for a in row] for row in img.alpha]
    else:
        t = otsu_threshold(img)
        dark = [[v <= t for v in row] for row in img.gray]
        border = [dark[0][x] for x in range(img.width)] + [dark[-1][x] for x in range(img.width)]
        border += [dark[y][0] for y in range(img.height)] + [dark[y][-1] for y in range(img.height)]
        bg_dark = sum(border) > len(border) / 2
        mask = [[(not d) if bg_dark else d for d in row] for row in dark]
    return largest_component(mask)


def largest_component(mask: list[list[bool]]) -> list[list[bool]]:
    h, w = len(mask), len(mask[0])
    label = [[0] * w for _ in range(h)]
    best_id, best_n, cur = 0, 0, 0
    for y in range(h):
        for x in range(w):
            if mask[y][x] and not label[y][x]:
                cur += 1
                n, stack = 0, [(x, y)]
                label[y][x] = cur
                while stack:
                    cx, cy = stack.pop()
                    n += 1
                    for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                        if 0 <= nx < w and 0 <= ny < h and mask[ny][nx] and not label[ny][nx]:
                            label[ny][nx] = cur
                            stack.append((nx, ny))
                if n > best_n:
                    best_id, best_n = cur, n
    return [[label[y][x] == best_id and best_id != 0 for x in range(w)] for y in range(h)]


# ------------------------------------------------------------------------------ contour + polygon
def trace_boundary(mask: list[list[bool]]) -> list[tuple[float, float]]:
    """Moore-neighbour tracing of the outer boundary (pixel centres), clockwise in image coordinates."""
    h, w = len(mask), len(mask[0])
    start = next(((x, y) for y in range(h) for x in range(w) if mask[y][x]), None)
    if start is None:
        return []
    nbrs = [(-1, 0), (-1, -1), (0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1)]  # W, NW, N, NE, E, SE, S, SW

    def inside(x: int, y: int) -> bool:
        return 0 <= x < w and 0 <= y < h and mask[y][x]

    contour = [start]
    cur, back = start, 0  # entered from the west (scan order guarantees west is background)
    for _ in range(4 * w * h):
        found = False
        for k in range(8):
            d = (back + 1 + k) % 8
            nx, ny = cur[0] + nbrs[d][0], cur[1] + nbrs[d][1]
            if inside(nx, ny):
                back = (d + 4) % 8
                cur = (nx, ny)
                found = True
                break
        if not found:  # isolated pixel
            break
        if cur == start:
            break
        contour.append(cur)
    return [(float(x), float(y)) for x, y in contour]


def rdp(points: list[tuple[float, float]], eps: float) -> list[tuple[float, float]]:
    """Ramer-Douglas-Peucker simplification (iterative, open polyline)."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = points[a], points[b]
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy) or 1e-12
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            px, py = points[i]
            dist = abs(dy * px - dx * py + x2 * y1 - y2 * x1) / norm if (dx or dy) else math.hypot(px - x1, py - y1)
            if dist > best:
                best, idx = dist, i
        if best > eps and idx > 0:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(points, keep) if k]


def simplify_closed(points: list[tuple[float, float]], eps: float) -> list[tuple[float, float]]:
    if len(points) < 4:
        return points
    # split at the farthest point from the first to keep the closed shape stable
    far = max(range(len(points)), key=lambda i: math.dist(points[0], points[i]))
    a = rdp(points[: far + 1], eps)
    b = rdp(points[far:] + [points[0]], eps)
    poly = a[:-1] + b[:-1]
    return poly if len(poly) >= 3 else points


def signed_area(poly: list[tuple[float, float]]) -> float:
    return 0.5 * sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]))


def ear_clip(poly: list[tuple[float, float]]) -> list[tuple[int, int, int]]:
    """Triangulate a simple CCW polygon. O(n^2); robust for traced outlines (collinear points removed)."""
    idx = list(range(len(poly)))
    tris: list[tuple[int, int, int]] = []

    def is_convex(a, b, c) -> bool:
        (ax, ay), (bx, by), (cx, cy) = poly[a], poly[b], poly[c]
        return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax) > 1e-12

    def in_tri(p, a, b, c) -> bool:
        (px, py) = poly[p]

        def s(i, j):
            (x1, y1), (x2, y2) = poly[i], poly[j]
            return (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)

        return s(a, b) >= 0 and s(b, c) >= 0 and s(c, a) >= 0

    guard = 0
    while len(idx) > 3 and guard < 10 * len(poly) ** 2:
        guard += 1
        n = len(idx)
        for k in range(n):
            a, b, c = idx[k - 1], idx[k], idx[(k + 1) % n]
            if not is_convex(a, b, c):
                continue
            corners = {poly[a], poly[b], poly[c]}
            if any(in_tri(p, a, b, c) for p in idx if p not in (a, b, c) and poly[p] not in corners):
                continue
            tris.append((a, b, c))
            idx.pop(k)
            break
        else:  # degenerate remainder: fan it
            break
    if len(idx) >= 3:
        for k in range(1, len(idx) - 1):
            tris.append((idx[0], idx[k], idx[k + 1]))
    return tris


def find_holes(mask: list[list[bool]], min_px: int = 6) -> list[list[list[bool]]]:
    """Background regions fully enclosed by the object (not touching the image border)."""
    h, w = len(mask), len(mask[0])
    seen = [[False] * w for _ in range(h)]
    holes = []
    for y in range(h):
        for x in range(w):
            if mask[y][x] or seen[y][x]:
                continue
            comp, stack, border = [], [(x, y)], False
            seen[y][x] = True
            while stack:
                cx, cy = stack.pop()
                comp.append((cx, cy))
                border |= cx in (0, w - 1) or cy in (0, h - 1)
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < w and 0 <= ny < h and not mask[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = True
                        stack.append((nx, ny))
            if not border and len(comp) >= min_px:
                hm = [[False] * w for _ in range(h)]
                for cx, cy in comp:
                    hm[cy][cx] = True
                holes.append(hm)
    return holes


def _segments_cross(p1, p2, q1, q2) -> bool:
    """Proper intersection of segments p1p2 and q1q2 (shared endpoints do not count)."""
    if p1 in (q1, q2) or p2 in (q1, q2):
        return False

    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(v) < 1e-12 else (1 if v > 0 else -1)

    o1, o2, o3, o4 = orient(p1, p2, q1), orient(p1, p2, q2), orient(q1, q2, p1), orient(q1, q2, p2)
    return o1 != o2 and o3 != o4 and 0 not in (o1, o2, o3, o4)


def bridge_holes(outer: list[tuple[float, float]], holes: list[list[tuple[float, float]]]):
    """Merge CW holes into a CCW outer polygon via bridge edges (earcut technique).

    Returns (points, refs) where refs[i] = (loop index, vertex index); loop 0 is the outer polygon.
    """
    loops = [outer] + holes
    merged: list[tuple[int, int]] = [(0, i) for i in range(len(outer))]
    for k in sorted(range(1, len(loops)), key=lambda k: -max(p[0] for p in loops[k])):
        hole = loops[k]
        mi = max(range(len(hole)), key=lambda i: (hole[i][0], -hole[i][1]))
        mp = hole[mi]
        edges = [(loops[a][i], loops[b][j]) for (a, i), (b, j) in zip(merged, merged[1:] + merged[:1])]
        edges += [(hole[i], hole[(i + 1) % len(hole)]) for i in range(len(hole))]
        best = None
        for pos, (lp, ip) in enumerate(merged):
            cand = loops[lp][ip]
            d = math.dist(mp, cand)
            if best is not None and d >= best[0]:
                continue
            if not any(_segments_cross(mp, cand, e0, e1) for e0, e1 in edges):
                best = (d, pos)
        if best is None:  # no visible vertex (degenerate input): skip this hole
            continue
        pos = best[1]
        ins = [(k, (mi + t) % len(hole)) for t in range(len(hole) + 1)]
        merged = merged[: pos + 1] + ins + [merged[pos]] + merged[pos + 1 :]
    return [loops[lp][ip] for lp, ip in merged], merged


# ------------------------------------------------------------------------------ mesh builders
def extrude_polygon(
    poly: list[tuple[float, float]],
    height: float,
    name: str = "extrusion",
    holes: list[list[tuple[float, float]]] | None = None,
) -> M.Mesh:
    """Prism from a 2D polygon (+ optional holes) in the XZ plane (Y up), centred vertically. Watertight."""
    if signed_area(poly) < 0:
        poly = poly[::-1]
    loops = [poly] + [h if signed_area(h) < 0 else h[::-1] for h in (holes or []) if len(h) >= 3]
    pts, refs = bridge_holes(loops[0], loops[1:])
    tris = ear_clip(pts)
    m = M.Mesh(name=name)
    y0, y1 = -height / 2, height / 2
    bot = [[m.add_vertex((x, y0, -z)) for x, z in loop] for loop in loops]  # z flipped: image rows grow down
    top = [[m.add_vertex((x, y1, -z)) for x, z in loop] for loop in loops]
    for a, b, c in tris:
        (la, ia), (lb, ib), (lc, ic) = refs[a], refs[b], refs[c]
        m.triangles.append((top[la][ia], top[lc][ic], top[lb][ib]))
        m.triangles.append((bot[la][ia], bot[lb][ib], bot[lc][ic]))
    for li, loop in enumerate(loops):
        n = len(loop)
        for i in range(n):
            j = (i + 1) % n
            m.add_quad(bot[li][i], top[li][i], top[li][j], bot[li][j])
    if m.volume() < 0:
        m.triangles = [(a, c, b) for a, b, c in m.triangles]
    return m


def relief_mesh(
    img: Image, width_mm: float, base_mm: float = 1.0, relief_mm: float = 3.0, max_cells: int = 120, invert: bool = True
) -> M.Mesh:
    """Height-map solid: top surface from brightness (dark = high when ``invert``), flat bottom, side walls."""
    small = downsample(img, max_cells)
    gw, gh = small.width, small.height
    cell = width_mm / max(1, gw - 1)
    m = M.Mesh(name="relief")
    top = [[0] * gw for _ in range(gh)]
    bot = [[0] * gw for _ in range(gh)]
    for y in range(gh):
        for x in range(gw):
            v = small.gray[y][x]
            hgt = base_mm + relief_mm * ((1 - v) if invert else v)
            px, pz = x * cell - width_mm / 2, y * cell - (gh - 1) * cell / 2
            top[y][x] = m.add_vertex((px, hgt, pz))
            bot[y][x] = m.add_vertex((px, 0.0, pz))
    for y in range(gh - 1):
        for x in range(gw - 1):
            m.add_quad(top[y][x], top[y + 1][x], top[y + 1][x + 1], top[y][x + 1])
            m.add_quad(bot[y][x], bot[y][x + 1], bot[y + 1][x + 1], bot[y + 1][x])
    for x in range(gw - 1):  # z = min edge (row 0) and z = max edge (last row)
        m.add_quad(bot[0][x], top[0][x], top[0][x + 1], bot[0][x + 1])
        m.add_quad(bot[gh - 1][x + 1], top[gh - 1][x + 1], top[gh - 1][x], bot[gh - 1][x])
    for y in range(gh - 1):
        m.add_quad(bot[y + 1][0], top[y + 1][0], top[y][0], bot[y][0])
        m.add_quad(bot[y][gw - 1], top[y][gw - 1], top[y + 1][gw - 1], bot[y + 1][gw - 1])
    if m.volume() < 0:
        m.triangles = [(a, c, b) for a, b, c in m.triangles]
    return m


def revolve_profile(mask: list[list[bool]], width_mm: float, segments: int = 48) -> M.Mesh:
    """Lathe: half-width of the silhouette per row (about the mask's centre column) revolved around +Y.

    Uniform scale from ``width_mm`` (silhouette's widest extent), so the height keeps the picture's aspect.
    """
    h, w = len(mask), len(mask[0])
    rows = [y for y in range(h) if any(mask[y])]
    if not rows:
        raise ValueError("empty silhouette")
    xs = [x for y in rows for x in range(w) if mask[y][x]]
    cx = (min(xs) + max(xs)) / 2
    y_top, y_bot = rows[0], rows[-1]
    scale = width_mm / max(1, (max(xs) - min(xs) + 1))
    profile = []
    step = max(1, (y_bot - y_top) // 64)
    for y in list(range(y_top, y_bot + 1, step)) + ([y_bot] if (y_bot - y_top) % step else []):
        cols = [x for x in range(w) if mask[y][x]]
        r = (max(abs(c - cx) for c in cols) + 0.5) * scale if cols else 0.0
        profile.append((max(r, 0.01), (y_bot - y) * scale))
    profile.sort(key=lambda t: t[1])
    m = M.Mesh(name="revolve")
    rings = []
    for r, y in profile:
        rings.append(
            [
                m.add_vertex((r * math.cos(2 * math.pi * i / segments), y, r * math.sin(2 * math.pi * i / segments)))
                for i in range(segments)
            ]
        )
    for a, b in zip(rings, rings[1:]):
        for i in range(segments):
            j = (i + 1) % segments
            m.add_quad(a[i], b[i], b[j], a[j])
    cb = m.add_vertex((0.0, profile[0][1], 0.0))
    ct = m.add_vertex((0.0, profile[-1][1], 0.0))
    for i in range(segments):
        j = (i + 1) % segments
        m.triangles.append((cb, rings[0][i], rings[0][j]))
        m.triangles.append((ct, rings[-1][j], rings[-1][i]))
    if m.volume() < 0:
        m.triangles = [(a, c, b) for a, b, c in m.triangles]
    return m


# ------------------------------------------------------------------------------ public API
@dataclass
class Silhouette:
    outline_mm: list[tuple[float, float]]
    width_mm: float
    depth_mm: float
    aspect: float
    fill_ratio: float  # silhouette area / bbox area (1.0 = rectangle, ~0.785 = ellipse)
    pixels: tuple[int, int] = (0, 0)
    vertices: int = 0
    holes_mm: list[list[tuple[float, float]]] = field(default_factory=list)


def silhouette(
    data: bytes, width_mm: float = 100.0, tolerance_px: float = 1.5, max_side: int = 256
) -> tuple[Silhouette, list[list[bool]], Image]:
    img = downsample(decode(data), max_side)
    mask = object_mask(img)
    contour = trace_boundary(mask)
    if len(contour) < 3:
        raise ValueError("no object found in the image")
    poly = simplify_closed(contour, tolerance_px)
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    bw, bh = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
    scale = width_mm / bw
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    outline = [((x - cx) * scale, (y - cy) * scale) for x, y in poly]
    if signed_area(outline) < 0:
        outline = outline[::-1]
    area_px = sum(v for row in mask for v in row)
    holes = []
    for hm in find_holes(mask):
        hp = simplify_closed(trace_boundary(hm), tolerance_px)
        if len(hp) >= 3:
            holes.append([((x - cx) * scale, (y - cy) * scale) for x, y in hp])
    return (
        Silhouette(
            outline,
            round(bw * scale, 2),
            round(bh * scale, 2),
            round(bw / bh, 3),
            round(area_px / (bw * bh), 3),
            (img.width, img.height),
            len(outline),
            holes,
        ),
        mask,
        img,
    )


def image_to_mesh(
    data: bytes,
    mode: str = "extrude",
    width_mm: float = 100.0,
    height_mm: float = 10.0,
    tolerance_px: float = 1.5,
    fill: str | bool = "auto",
) -> tuple[M.Mesh, dict]:
    """Picture -> watertight mesh. Returns (mesh, report).

    ``fill``: True drops holes (solid silhouette), False keeps them, "auto" keeps them unless the holes cover more
    than half of the outline's area — then the picture is an *outline drawing* (a sketched border) and is filled.
    """
    sil, mask, img = silhouette(data, width_mm, tolerance_px)
    holes = sil.holes_mm
    if fill == "auto":
        outer = abs(signed_area(sil.outline_mm)) or 1.0
        fill = sum(abs(signed_area(h)) for h in holes) / outer > 0.5
    if fill:
        holes = []
    if mode == "extrude":
        m = extrude_polygon(sil.outline_mm, height_mm, name="extrusion", holes=holes)
    elif mode == "relief":
        m = relief_mesh(img, width_mm, base_mm=max(0.6, height_mm * 0.25), relief_mm=height_mm * 0.75)
    elif mode == "revolve":
        m = revolve_profile(mask, width_mm)
    else:
        raise ValueError("mode must be extrude | relief | revolve")
    report = {
        "mode": mode,
        "outlineVertices": sil.vertices,
        "holes": len(holes),
        "filledOutline": bool(fill) and bool(sil.holes_mm),
        "silhouette": {
            "widthMm": sil.width_mm,
            "depthMm": sil.depth_mm,
            "aspect": sil.aspect,
            "fillRatio": sil.fill_ratio,
        },
        "triangles": len(m.triangles),
        "watertight": m.is_watertight(),
        "volumeMm3": round(m.volume(), 1),
        "sizeMm": [round(v, 2) for v in m.size()],
    }
    return m, report


# ------------------------------------------------------------------------------ picture -> blueprint
VISION_PROMPT = """You are a hardware product analyst. Look at the image (a photo, render or sketch of a device idea).
Return ONLY JSON: {"name": str, "prompt": str, "summary": str, "components": [str], "uncertainties": [str],
"features": [str], "approxSizeMm": {"w": num, "d": num, "h": num} | null, "materials": [str], "confidence": 0..1}.
"prompt" must describe what the device does, its sensors/actuators/displays/controls, how it is powered and its
form factor, in one paragraph suitable for a hardware design engine. "components" lists parts you can see or infer;
"uncertainties" lists what cannot be determined from the image (ask the user about these)."""


class VisionModel(Protocol):
    def describe(self, image: bytes, mime: str, instruction: str) -> dict: ...


@dataclass
class ImageBrief:
    prompt: str
    name: str | None = None
    features: list[str] = field(default_factory=list)
    size_mm: dict | None = None
    silhouette: Silhouette | None = None
    source: str = "user"  # user | vision
    analysis: dict = field(default_factory=dict)  # {summary, components, uncertainties} (reference imageAnalysis)


def brief_from_image(
    data: bytes, mime: str = "image/png", user_prompt: str | None = None, vision: VisionModel | None = None
) -> ImageBrief:
    """Combine an optional user prompt, an optional vision-model description and the silhouette form factor."""
    try:
        sil, _, _ = silhouette(data)
    except ValueError:
        sil = None
    desc: dict = {}
    if vision is not None:
        desc = vision.describe(data, mime, VISION_PROMPT) or {}
    prompt = " ".join(x for x in (user_prompt, desc.get("prompt")) if x).strip()
    if not prompt:
        raise ValueError("picture-to-blueprint needs a vision model or a text prompt describing the device's function")
    analysis = {k: desc[k] for k in ("summary", "components", "uncertainties") if k in desc}
    return ImageBrief(prompt=prompt, name=desc.get("name"), features=list(desc.get("features") or []), size_mm=desc.get("approxSizeMm"),
                      silhouette=sil, source="vision" if desc else "user", analysis=analysis)  # fmt: skip
