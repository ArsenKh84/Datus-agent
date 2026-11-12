"""Deterministic images: isometric assembly renders (project hero image, per-step illustrations) and part icons.

Replaces the reference's AI image generation (generateProjectImage / image rerolls / generateStepImage) and part
photos with exact renders of the solved assembly, so pictures always match the design.
"""

from __future__ import annotations

import hashlib
import math
from html import escape

from hwbp.models import TYPE_META, canonical_type

COS30, SIN30 = math.cos(math.radians(30)), math.sin(math.radians(30))


def _iso(x: float, y: float, z: float, yaw: float = 0.0) -> tuple[float, float]:
    """Isometric projection, viewer above the +x/+z corner (screen y grows downward); ``yaw`` rotates the model."""
    if yaw:
        x, z = x * math.cos(yaw) - z * math.sin(yaw), x * math.sin(yaw) + z * math.cos(yaw)
    return (x - z) * COS30, (x + z) * SIN30 - y


def _shade(hex_color: str, k: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return f"#{min(255, int(r * k)):02x}{min(255, int(g * k)):02x}{min(255, int(b * k)):02x}"


def render_iso(layout: dict, highlight: set[str] | None = None, width: int = 640, height: int = 420, variant: int = 0,
               title: str | None = None, dark: bool = False) -> str:  # fmt: skip
    """Painter's-algorithm isometric render of the layout's boxes. ``highlight`` dims all other parts (step images);
    ``variant`` rotates the view (deterministic 'rerolls')."""
    parts = layout.get("parts", {})
    bg, fg = ("#131313", "#f2f2f2") if dark else ("#f6f6f4", "#151515")
    yaw = [0.0, math.pi / 2, math.pi, -math.pi / 2][variant % 4]
    polys = []
    for pid, p in parts.items():
        x, y, z = p["pos"]
        w, h, d = p.get("extent") or p["size"]
        rx, rz = (x * math.cos(yaw) - z * math.sin(yaw), x * math.sin(yaw) + z * math.cos(yaw)) if yaw else (x, z)
        depth = rx + rz + y  # painter's order: far (small) first
        polys.append((depth, pid, p, x, y, z, w, h, d))
    if not polys:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"><rect width="100%" height="100%" fill="{bg}"/></svg>'
    pts = [
        _iso(px + sx * w / 2, py + sy * h / 2, pz + sz * d / 2, yaw)
        for _, _, _, px, py, pz, w, h, d in polys
        for sx in (-1, 1)
        for sy in (-1, 1)
        for sz in (-1, 1)
    ]
    minx, maxx = min(p[0] for p in pts), max(p[0] for p in pts)
    miny, maxy = min(p[1] for p in pts), max(p[1] for p in pts)
    pad = 24
    s = min(
        (width - 2 * pad) / max(1e-6, maxx - minx), (height - 2 * pad - (24 if title else 0)) / max(1e-6, maxy - miny)
    )
    ox = pad - minx * s + ((width - 2 * pad) - (maxx - minx) * s) / 2
    oy = pad + (24 if title else 0) - miny * s

    def P(x: float, y: float, z: float) -> str:
        u, v = _iso(x, y, z, yaw)
        return f"{u * s + ox:.1f},{v * s + oy:.1f}"

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" font-family="Inter,system-ui,sans-serif">',
           f'<rect width="100%" height="100%" fill="{bg}"/>']  # fmt: skip
    if title:
        out.append(f'<text x="{pad}" y="{pad + 6}" font-size="15" font-weight="700" fill="{fg}">{escape(title)}</text>')

    def contains(a, b) -> bool:  # does box a enclose box b
        return all(
            a[3 + k] - a[6 + k] / 2 - 0.01 <= b[3 + k] - b[6 + k] / 2
            and b[3 + k] + b[6 + k] / 2 <= a[3 + k] + a[6 + k] / 2 + 0.01
            for k in range(3)
        )

    shells = {t[1] for t in polys if any(t is not u and contains(t, u) for u in polys)}
    # shells (enclosures, lids over internals) are drawn last and translucent: an x-ray view of the internals
    order = sorted(polys, key=lambda t: (t[1] in shells, t[0]))
    for _, pid, p, x, y, z, w, h, d in order:
        base = TYPE_META.get(canonical_type(p.get("type")), TYPE_META["other"])[1]
        dim = highlight is not None and pid not in highlight
        op = 0.18 if dim else (0.38 if pid in shells else 0.92)
        x0, x1, y0, y1, z0, z1 = x - w / 2, x + w / 2, y - h / 2, y + h / 2, z - d / 2, z + d / 2
        faces = [
            ([(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)], 1.15),
            ([(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)], 0.6),
            ([(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)], 0.85),
            ([(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0)], 0.85),
            ([(x1, y0, z0), (x1, y0, z1), (x1, y1, z1), (x1, y1, z0)], 0.7),
            ([(x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (x0, y1, z0)], 0.7),
        ]

        def fdepth(quad):
            cx, cy, cz = (sum(c[i] for c in quad) / 4 for i in range(3))
            if yaw:
                cx, cz = cx * math.cos(yaw) - cz * math.sin(yaw), cx * math.sin(yaw) + cz * math.cos(yaw)
            return cx + cz + cy

        faces = sorted(faces, key=lambda f: fdepth(f[0]))[3:]  # the 3 faces nearest the viewer are the visible ones
        out.append(f'<g data-part="{escape(pid)}" opacity="{op}">')
        for quad, k in faces:
            out.append(
                f'<polygon points="{" ".join(P(*c) for c in quad)}" fill="{_shade(base, k)}" stroke="{_shade(base, 0.55)}" stroke-width="0.8"/>'
            )
        out.append("</g>")
    out.append("</svg>")
    return "\n".join(out)


def project_image(content, variant: int = 0, dark: bool = False) -> tuple[str, str]:
    """(svg, sha256 hash) — the deterministic project hero image (``projectImageHash`` equivalent)."""
    svg = render_iso(content.mech_layout or {}, title=content.project_name, variant=variant, dark=dark)
    return svg, hashlib.sha256(svg.encode()).hexdigest()


def step_image(content, part_ids: list[str], dark: bool = False) -> str:
    return render_iso(content.mech_layout or {}, highlight=set(part_ids), width=480, height=320, dark=dark)


ICON_GLYPH = {"mcu": "chip", "sensor": "waves", "actuator": "gear", "power": "bolt", "module": "board", "display": "screen",
              "structural": "plate", "enclosure": "box", "mechanism": "gear", "misc": "screw", "3d_printed": "layers", "other": "dot"}  # fmt: skip


def part_icon(name: str, type_: str | None, size: int = 120) -> str:
    """Simple typed icon (SVG) for a part — used where the reference shows a product photo."""
    t = canonical_type(type_)
    c = TYPE_META.get(t, TYPE_META["other"])[1]
    glyph = ICON_GLYPH.get(t, "dot")
    m = size / 2
    shapes = {
        "chip": f'<rect x="{m - 22}" y="{m - 22}" width="44" height="44" rx="4" fill="none" stroke="{c}" stroke-width="4"/>'
        + "".join(
            f'<line x1="{m - 30}" y1="{m - 14 + i * 14}" x2="{m - 22}" y2="{m - 14 + i * 14}" stroke="{c}" stroke-width="3"/>'
            f'<line x1="{m + 22}" y1="{m - 14 + i * 14}" x2="{m + 30}" y2="{m - 14 + i * 14}" stroke="{c}" stroke-width="3"/>'
            for i in range(3)
        ),
        "waves": "".join(
            f'<path d="M{m - r},{m + 10} A{r},{r} 0 0 1 {m + r},{m + 10}" fill="none" stroke="{c}" stroke-width="4"/>'
            for r in (10, 20, 30)
        ),
        "gear": f'<circle cx="{m}" cy="{m}" r="22" fill="none" stroke="{c}" stroke-width="6" stroke-dasharray="8 5"/><circle cx="{m}" cy="{m}" r="7" fill="{c}"/>',
        "bolt": f'<polygon points="{m + 4},{m - 30} {m - 16},{m + 4} {m - 2},{m + 4} {m - 6},{m + 30} {m + 16},{m - 6} {m + 2},{m - 6}" fill="{c}"/>',
        "board": f'<rect x="{m - 30}" y="{m - 20}" width="60" height="40" rx="5" fill="none" stroke="{c}" stroke-width="4"/><circle cx="{m - 20}" cy="{m - 10}" r="3" fill="{c}"/><circle cx="{m + 20}" cy="{m + 10}" r="3" fill="{c}"/>',
        "screen": f'<rect x="{m - 32}" y="{m - 22}" width="64" height="40" rx="4" fill="none" stroke="{c}" stroke-width="4"/><line x1="{m - 10}" y1="{m + 28}" x2="{m + 10}" y2="{m + 28}" stroke="{c}" stroke-width="4"/>',
        "plate": f'<polygon points="{m - 32},{m} {m},{m - 16} {m + 32},{m} {m},{m + 16}" fill="none" stroke="{c}" stroke-width="4"/>',
        "box": f'<polygon points="{m - 28},{m - 10} {m},{m - 24} {m + 28},{m - 10} {m + 28},{m + 16} {m},{m + 30} {m - 28},{m + 16}" fill="none" stroke="{c}" stroke-width="4"/>',
        "screw": f'<rect x="{m - 5}" y="{m - 18}" width="10" height="40" fill="{c}"/><rect x="{m - 14}" y="{m - 26}" width="28" height="10" rx="3" fill="{c}"/>',
        "layers": "".join(
            f'<polygon points="{m - 28},{m + o} {m},{m - 14 + o} {m + 28},{m + o} {m},{m + 14 + o}" fill="none" stroke="{c}" stroke-width="3"/>'
            for o in (-12, 0, 12)
        ),
        "dot": f'<circle cx="{m}" cy="{m}" r="16" fill="{c}"/>',
    }
    label = escape((name or "")[:16])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size + 18}" width="{size}" height="{size + 18}">'
            f'<rect width="100%" height="100%" rx="10" fill="{c}" fill-opacity="0.08"/>{shapes[glyph]}'
            f'<text x="{m}" y="{size + 10}" font-size="11" text-anchor="middle" fill="{c}" font-family="Inter,system-ui,sans-serif">{label}</text></svg>')  # fmt: skip
