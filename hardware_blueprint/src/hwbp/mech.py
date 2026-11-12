"""Mechanical layout: dimension parsing, assembly tree, placement solver, enclosure synthesis.

Two halves:

* **Solver (reference-compatible)** — given any project document (ours or one exported from blueprint.io)
  compute 3D placements exactly like the reference MECH tab:
    - ``parse_dims("A x B x C")`` -> width X = A, depth Z = B, height Y = C (each ≥ 1 mm), default 20×10×20
    - participating parts: type ∉ {misc, other} and name !~ screw|bolt|nut|washer|standoff|wire|cable|jumper
    - ``mountsFor`` adds an implicit connection; connections are undirected for the tree
    - root = largest-volume ``enclosure`` part, else ``structural``, else any part; BFS in id order
    - explicit ``delta`` = centre(target) − centre(source) (world mm); root's bottom face sits on y = 0
    - implicit placement: mount pairs -> hold (stack); child fits parent footprint -> top; else beside (+X, 5 mm)
* **Synthesis (ours)** — generate the mechanical design for an engine ``Design``: pack boards on a floor
  (skyline bin packing), size an enclosure (walls, standoffs, clearance, lid with display windows), add a
  chassis for drive robots / links for servo arms, fasteners, and emit nodes + delta connections that the
  solver (and the reference viewer) reproduce exactly.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from hwbp.design import Design, Part
from hwbp.models import TYPE_META, MechanicalConnection, Node, ProjectContent, Vec3, canonical_type

DIM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)", re.I)
EXCLUDED_NAME = re.compile(r"screw|bolt|nut|washer|standoff|wire|cable|jumper", re.I)
CYLINDER_NAME = re.compile(
    r"\b(wheel|caster|pulley|shaft|axle|rod|roller|drum|spool|18650|coin ?cell|cr2032|bearing)\b", re.I
)
DEFAULT_DIMS = (20.0, 10.0, 20.0)  # (w, h, d)
BESIDE_GAP = 5.0
WALL, FLOOR, LID_T, STANDOFF_H, CLEARANCE = 2.0, 2.0, 2.0, 5.0, 4.0


# ------------------------------------------------------------------------------ parsing
def parse_dims(text: str | None) -> tuple[float, float, float] | None:
    """'A x B x C' -> (w=A, h=C, d=B) in mm (reference axis convention: 3rd number is vertical)."""
    if not text:
        return None
    m = DIM_RE.search(text)
    if not m:
        return None
    a, b, c = (float(g) for g in m.groups())
    if min(a, b, c) <= 0 or not all(math.isfinite(v) for v in (a, b, c)):
        return None
    return max(1.0, a), max(1.0, c), max(1.0, b)


def format_dims(w: float, h: float, d: float) -> str:
    """Inverse of parse_dims: (w, h, d) -> 'w x d x h mm' (reference order)."""
    return f"{w:g}x{d:g}x{h:g}mm"


def is_layout_part(node: Node) -> bool:
    return canonical_type(node.type) not in ("misc", "other") and not EXCLUDED_NAME.search(node.name or "")


def shape_of(node: Node) -> str:
    return "cylinder" if CYLINDER_NAME.search(node.name or "") else "box"


# ------------------------------------------------------------------------------ solver
@dataclass
class Placed:
    id: str
    w: float
    h: float
    d: float
    pos: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    parent: str | None = None
    explicit: bool = False

    @property
    def he(self) -> tuple[float, float, float]:
        return self.w / 2, self.h / 2, self.d / 2

    def bounds(self) -> tuple[list[float], list[float]]:
        hx, hy, hz = self.he
        x, y, z = self.pos
        return [x - hx, y - hy, z - hz], [x + hx, y + hy, z + hz]


def solve_layout(content: ProjectContent) -> dict:
    """Compute placements for every layout part of a project document. Returns a JSON-able dict."""
    nodes = {n.id: n for n in content.nodes if is_layout_part(n)}
    dims = {i: parse_dims(n.dimensions) or DEFAULT_DIMS for i, n in nodes.items()}
    edges: dict[frozenset, MechanicalConnection] = {}
    for c in sorted(content.mechanical_connections, key=lambda c: f"{c.source}|{c.target}"):
        if c.source in nodes and c.target in nodes and c.source != c.target:
            edges.setdefault(frozenset((c.source, c.target)), c)
    mount_pairs: set[frozenset] = set()
    for n in nodes.values():
        if n.mounts_for and n.mounts_for in nodes and n.mounts_for != n.id:
            key = frozenset((n.id, n.mounts_for))
            if key not in edges:  # explicit connections win; only synthesise when the pair has none
                mount_pairs.add(key)
                edges[key] = MechanicalConnection(source=n.id, target=n.mounts_for, label="mount", delta=None)
    adj: dict[str, list[str]] = {i: [] for i in nodes}
    for key in edges:
        a, b = sorted(key)
        adj[a].append(b)
        adj[b].append(a)
    for i in adj:
        adj[i].sort()
    placed: dict[str, Placed] = {}
    components = _components(sorted(nodes), adj)
    offset_x = 0.0
    tree_rows: list[dict] = []
    for comp in components:
        root = _pick_root(comp, nodes, dims)
        w, h, d = dims[root]
        placed[root] = Placed(root, w, h, d, [0.0, h / 2, 0.0])
        queue = [root]
        seen = {root}
        while queue:
            p = queue.pop(0)
            pp = placed[p]
            kids = [n for n in adj[p] if n not in seen]
            beside_x = pp.pos[0] + pp.w / 2
            for n in kids:
                seen.add(n)
                c = edges[frozenset((p, n))]
                w, h, d = dims[n]
                child = Placed(n, w, h, d, parent=p)
                if c.delta is not None and frozenset((p, n)) not in mount_pairs:
                    s = 1.0 if c.source == p else -1.0
                    dv = (c.delta.x, c.delta.y, c.delta.z)
                    child.pos = [pp.pos[k] + s * dv[k] for k in range(3)]
                    child.explicit = True
                elif frozenset((p, n)) in mount_pairs:
                    up = nodes[p].mounts_for == n  # parent is the mount -> child sits on top of it
                    child.pos = [pp.pos[0], pp.pos[1] + (1 if up else -1) * (pp.h / 2 + h / 2), pp.pos[2]]
                elif w <= pp.w and d <= pp.d:
                    child.pos = [pp.pos[0], pp.pos[1] + pp.h / 2 + h / 2, pp.pos[2]]
                else:
                    child.pos = [beside_x + BESIDE_GAP + w / 2, (pp.pos[1] - pp.h / 2) + h / 2, pp.pos[2]]
                    beside_x += BESIDE_GAP + w
                placed[n] = child
                queue.append(n)
        # normalise component: min y = 0, pack along X with 30 mm gaps
        comp_ids = [i for i in comp if i in placed]
        lo = [min(placed[i].bounds()[0][k] for i in comp_ids) for k in range(3)]
        hi = [max(placed[i].bounds()[1][k] for i in comp_ids) for k in range(3)]
        shift = [offset_x - lo[0] if offset_x else 0.0, -lo[1], 0.0]
        for i in comp_ids:
            placed[i].pos = [placed[i].pos[k] + shift[k] for k in range(3)]
        offset_x = (hi[0] + shift[0]) + 30.0
        tree_rows += _tree_rows(root, placed, nodes)
    if not placed:
        return {
            "parts": {},
            "bbox": {"min": [0, 0, 0], "max": [0, 0, 0], "size": [0, 0, 0]},
            "tree": [],
            "collisions": [],
        }
    lo = [min(p.bounds()[0][k] for p in placed.values()) for k in range(3)]
    hi = [max(p.bounds()[1][k] for p in placed.values()) for k in range(3)]
    return {
        "parts": {
            i: {
                "pos": [round(v, 3) for v in p.pos],
                "size": [p.w, p.h, p.d],
                "parent": p.parent,
                "shape": shape_of(nodes[i]),
                "type": canonical_type(nodes[i].type),
                "color": TYPE_META.get(canonical_type(nodes[i].type), TYPE_META["other"])[1],
            }
            for i, p in placed.items()
        },
        "bbox": {
            "min": [round(v, 3) for v in lo],
            "max": [round(v, 3) for v in hi],
            "size": [round(hi[k] - lo[k], 3) for k in range(3)],
        },
        "tree": tree_rows,
        "collisions": find_collisions(placed),
        "cadEligible": len(placed),
    }


def _components(ids: list[str], adj: dict[str, list[str]]) -> list[list[str]]:
    seen: set[str] = set()
    comps = []
    for i in ids:
        if i in seen:
            continue
        comp, q = [], [i]
        seen.add(i)
        while q:
            u = q.pop(0)
            comp.append(u)
            for v in adj[u]:
                if v not in seen:
                    seen.add(v)
                    q.append(v)
        comps.append(comp)
    return comps


def _pick_root(comp: list[str], nodes: dict[str, Node], dims: dict) -> str:
    for t in ("enclosure", "structural"):
        cands = [i for i in comp if canonical_type(nodes[i].type) == t]
        if cands:
            break
    else:
        cands = comp
    return max(sorted(cands), key=lambda i: dims[i][0] * dims[i][1] * dims[i][2])


def _tree_rows(root: str, placed: dict[str, Placed], nodes: dict[str, Node]) -> list[dict]:
    children: dict[str, list[str]] = {}
    for p in placed.values():
        if p.parent:
            children.setdefault(p.parent, []).append(p.id)
    rows = []

    def walk(i: str, depth: int) -> None:
        rows.append({"id": i, "name": nodes[i].name, "depth": depth, "type": canonical_type(nodes[i].type)})
        for c in sorted(children.get(i, [])):
            walk(c, depth + 1)

    walk(root, 0)
    return rows


def find_collisions(placed: dict[str, Placed], tol: float = 0.5) -> list[list[str]]:
    """AABB interference between parts that are not parent/child (contained mounts are expected)."""
    items = sorted(placed.values(), key=lambda p: p.id)
    out = []
    for i, a in enumerate(items):
        la, ha = a.bounds()
        for b in items[i + 1 :]:
            if a.parent == b.id or b.parent == a.id or a.parent == b.parent and a.parent is None:
                continue
            lb, hb = b.bounds()
            if all(min(ha[k], hb[k]) - max(la[k], lb[k]) > tol for k in range(3)):
                if _contains(a, b) or _contains(b, a):
                    continue  # part inside an enclosure/shell is intended
                out.append([a.id, b.id])
    return out


def _contains(outer: Placed, inner: Placed) -> bool:
    lo, hi = outer.bounds()
    li, hi2 = inner.bounds()
    return all(lo[k] - 0.01 <= li[k] and hi2[k] <= hi[k] + 0.01 for k in range(3))


# ------------------------------------------------------------------------------ synthesis
PANEL_TYPES = {"display", "input"}
EXTERNAL_KINDS = {"servo", "dc_motor", "stepper", "pump"}


def _dims_of(part: Part) -> tuple[float, float, float]:
    return part.dims_override or tuple(part.catalog.dims_mm or DEFAULT_DIMS)  # type: ignore[return-value]


def skyline_pack(
    items: list[tuple[str, float, float]], max_width: float, gap: float = 4.0
) -> tuple[dict[str, tuple[float, float]], float, float]:
    """Pack rectangles (id, w, d) into a strip of ``max_width`` using the skyline bottom-left heuristic.

    Returns ({id: (x_min, z_min)}, used_width, used_depth).
    """
    skyline = [(0.0, max_width, 0.0)]  # segments (x0, x1, height)
    pos: dict[str, tuple[float, float]] = {}
    for pid, w, d in sorted(items, key=lambda t: (-(t[1] * t[2]), t[0])):
        w, d = w + gap, d + gap
        best = None
        for i, (x0, _, _) in enumerate(skyline):
            if x0 + w > max_width + 1e-9:
                continue
            # height of the skyline under [x0, x0 + w)
            y = max(h for (a, b, h) in skyline if a < x0 + w and b > x0)
            if best is None or (y, x0) < (best[0], best[1]):
                best = (y, x0, i)
        if best is None:  # wider than the strip: place at the top
            y = max(h for _, _, h in skyline)
            best = (y, 0.0, 0)
            w = max(w, max_width)
        y, x0, _ = best
        pos[pid] = (x0, y)
        new = []
        for a, b, h in skyline:
            if b <= x0 or a >= x0 + w:
                new.append((a, b, h))
            else:
                if a < x0:
                    new.append((a, x0, h))
                if b > x0 + w:
                    new.append((x0 + w, b, h))
        new.append((x0, min(x0 + w, max_width), y + d))
        skyline = sorted(new)
    used_w = max((pos[p][0] + w for p, w, _ in items), default=0.0)
    used_d = max((pos[p][1] + d for p, _, d in items), default=0.0)
    return pos, used_w, used_d


def synthesize_mechanics(design: Design, catalog) -> None:
    """Create enclosure/chassis/mount/fastener parts and delta connections for ``design``."""
    req = design.requirements
    electrical = [p for p in design.parts if p.catalog.category == "electrical"]
    internal = [p for p in electrical if p.catalog.type not in PANEL_TYPES and not _external(p)]
    panel = [p for p in electrical if p.catalog.type in PANEL_TYPES and not _external(p)]
    external = [p for p in electrical if _external(p)]
    floor_items = [(p.id, _dims_of(p)[0], _dims_of(p)[2]) for p in internal]
    total_area = sum(w * d for _, w, d in floor_items) or 400.0
    strip = max(
        max((w for _, w, _ in floor_items), default=40.0),
        max((_dims_of(p)[0] for p in panel), default=0.0),
        math.sqrt(total_area * 1.6),
    )
    pos2d, used_w, used_d = skyline_pack(floor_items, strip)
    panel_items = [(p.id, _dims_of(p)[0], _dims_of(p)[2]) for p in panel]
    panel2d, pan_w, pan_d = (
        skyline_pack(panel_items, max(strip, max((w for _, w, _ in panel_items), default=0.0)))
        if panel_items
        else ({}, 0.0, 0.0)
    )
    inner_w = max(used_w, pan_w, 40.0)
    inner_d = max(used_d, pan_d, 30.0)
    tallest = max((_dims_of(p)[1] for p in internal), default=10.0)
    panel_h = max((_dims_of(p)[1] for p in panel), default=0.0)  # panel parts hang under the lid
    outer_w = _round5(inner_w + 2 * WALL + 2 * CLEARANCE)
    outer_d = _round5(inner_d + 2 * WALL + 2 * CLEARANCE)
    outer_h = _round5(FLOOR + STANDOFF_H + tallest + CLEARANCE + panel_h)
    ff = req.form_factor or {}
    if ff.get("widthMm") and ff.get("depthMm"):  # picture silhouette: keep the drawn aspect ratio, never shrink
        aspect = float(ff["widthMm"]) / float(ff["depthMm"])
        if outer_w / outer_d < aspect:
            outer_w = _round5(outer_d * aspect)
        else:
            outer_d = _round5(outer_w / aspect)
        if ff.get("targetWidthMm"):
            k = max(1.0, float(ff["targetWidthMm"]) / outer_w)
            outer_w, outer_d = _round5(outer_w * k), _round5(outer_d * k)
    material = _enclosure_material(req.materials)
    infill = "25% infill, 0.2mm layer" if "robustness" not in req.priorities else "40% infill, 0.15mm layer"
    base = design.add(
        Part(id="enclosure_base", catalog=_virtual("enclosure", "Enclosure Base"), role="mech.enclosure", name="Enclosure Base",
             dims_override=(outer_w, outer_h, outer_d), material=material, print_settings=infill,
             description=f"Printed tray {outer_w:g}×{outer_d:g}×{outer_h:g} mm, {WALL:g} mm walls, integrated M2.5 bosses.")
    )  # fmt: skip
    windows = [(_dims_of(p)[0], _dims_of(p)[2]) for p in panel if p.catalog.type == "display"]
    lid = design.add(
        Part(id="enclosure_lid", catalog=_virtual("3d_printed", "Enclosure Lid"), role="mech.lid", name="Enclosure Lid",
             dims_override=(outer_w, LID_T, outer_d), material=material, print_settings=infill,
             description="Lid" + (f" with {len(windows)} display window(s)" if windows else "") + "; screws into base bosses.")
    )  # fmt: skip
    # absolute positions (base centre at y = outer_h/2)
    abs_pos: dict[str, tuple[float, float, float]] = {
        base.id: (0.0, outer_h / 2, 0.0),
        lid.id: (0.0, outer_h + LID_T / 2, 0.0),
    }
    x0 = -inner_w / 2
    z0 = -inner_d / 2
    floor_y = FLOOR + STANDOFF_H
    for pid, (px, pz) in pos2d.items():
        w, h, d = _dims_of(design.part(pid))
        abs_pos[pid] = (x0 + px + w / 2, floor_y + h / 2, z0 + pz + d / 2)
    px0, pz0 = -pan_w / 2, -pan_d / 2
    for pid, (px, pz) in panel2d.items():
        w, h, d = _dims_of(design.part(pid))
        abs_pos[pid] = (px0 + px + w / 2, outer_h - h / 2, pz0 + pz + d / 2)
    root = base
    if any(p.catalog.drive and p.catalog.drive.kind == "dc_motor" for p in external):
        root = _robot_chassis(design, external, abs_pos, outer_w, outer_d, material)
        lift = abs_pos[root.id][1] + _dims_of(root)[1] / 2
        for pid in list(abs_pos):
            if pid != root.id and pid not in [p.id for p in external] and not pid.startswith("wheel"):
                x, y, z = abs_pos[pid]
                abs_pos[pid] = (x, y + lift, z)
    if any(p.catalog.drive and p.catalog.drive.kind == "servo" for p in external):
        _servo_arm(design, external, abs_pos, outer_h, root)
    cursor = outer_w / 2 + 10.0
    for p in external:
        if p.id not in abs_pos:  # pumps, strips, probes, panels: a row beside the enclosure, outside
            w, h, d = _dims_of(p)
            if w > 300:  # long strips: shown as a 200 mm stub
                p.dims_override = (min(w, 200.0), h, d)
                w = 200.0
            abs_pos[p.id] = (cursor + w / 2, h / 2, 0.0)
            cursor += w + 10.0
    # tree edges: internal & panel -> base/lid, others -> root; emit delta = centre(target) - centre(source)
    conns: list[MechanicalConnection] = []

    def link(child: str, parent: str, label: str) -> None:
        cx, cy, cz = abs_pos[child]
        px, py, pz = abs_pos[parent]
        conns.append(
            MechanicalConnection(
                source=child,
                target=parent,
                label=label,
                delta=Vec3(x=round(px - cx, 3), y=round(py - cy, 3), z=round(pz - cz, 3)),
            )
        )

    if root is not base:
        link(base.id, root.id, "M3 screws")
    link(lid.id, base.id, "M2.5 screws")
    for p in internal:
        link(p.id, base.id, "M2.5 standoffs" if _area(p) >= 300 else "adhesive pad")
    for p in panel:
        link(p.id, lid.id, "panel mount")
    for p in external:
        parent = next((c.target for c in conns if c.source == p.id), None)
        if parent is None and p.id in abs_pos:
            link(p.id, p.mounts_for or root.id, "bracket")
    for extra in [p for p in design.parts if p.role.startswith("mech.") and p.id not in (base.id, lid.id, root.id)]:
        if extra.id in abs_pos and not any(c.source == extra.id for c in conns):
            link(extra.id, extra.mounts_for or root.id, "M3 screws")
    design.mech_connections = conns
    _add_fasteners(design, catalog, internal, panel)
    design.mech_layout = {
        "enclosure": {
            "outer": [outer_w, outer_h, outer_d],
            "inner": [inner_w, inner_d],
            "wall": WALL,
            "material": material,
        }
    }


def _external(p: Part) -> bool:
    cat = p.catalog
    if cat.drive and cat.drive.kind in EXTERNAL_KINDS:
        return True
    return (
        cat.type in ("lighting",)
        or "power.solar" in cat.capabilities
        or "sense.soil_moisture" in cat.capabilities
        or "sense.leak" in cat.capabilities
    )


def _robot_chassis(
    design: Design, external: list[Part], abs_pos: dict, outer_w: float, outer_d: float, material: str
) -> Part:
    motors = [p for p in external if p.catalog.drive and p.catalog.drive.kind == "dc_motor"]
    mw, mh, md = _dims_of(motors[0])
    cw, cd = _round5(max(outer_w + 20, 2 * md + 60)), _round5(max(outer_d + 40, 120.0))
    plate = design.add(
        Part(id="chassis_plate", catalog=_virtual("structural", "Chassis Plate"), role="mech.chassis", name="Chassis Plate",
             dims_override=(cw, 4.0, cd), material="Aluminum" if "aluminum" in design.requirements.materials else "PETG",
             print_settings="40% infill, 0.2mm layer", description="Robot base plate with motor bracket slots.")
    )  # fmt: skip
    clearance = mh + 6
    abs_pos[plate.id] = (0.0, clearance + 2.0, 0.0)
    corners = [(-1, -1), (1, -1), (-1, 1), (1, 1)] if len(motors) >= 4 else [(-1, 0), (1, 0)]
    for (sx, sz), m in zip(corners, motors):
        w, h, d = _dims_of(m)
        abs_pos[m.id] = (sx * (cw / 2 - w / 2), clearance - h / 2, sz * (cd / 2 - d / 2 - 5) if sz else 0.0)
    return plate


def _servo_arm(design: Design, external: list[Part], abs_pos: dict, outer_h: float, root: Part) -> None:
    servos = [p for p in external if p.catalog.drive and p.catalog.drive.kind == "servo"]
    y = outer_h + LID_T
    prev = root.id
    for i, s in enumerate(servos):
        w, h, d = _dims_of(s)
        abs_pos[s.id] = (0.0, y + h / 2, 0.0)
        s.mounts_for = prev
        y += h
        if i < len(servos) - 1:
            link = design.add(
                Part(id=f"arm_link_{i + 1}", catalog=_virtual("3d_printed", f"Arm Link {i + 1}"), role="mech.link", name=f"Arm Link {i + 1}",
                     dims_override=(16.0, 60.0, 8.0), material="PETG", print_settings="40% infill, 0.2mm layer",
                     description="Printed link between servo horns.", mounts_for=s.id)
            )  # fmt: skip
            abs_pos[link.id] = (0.0, y + 30.0, 0.0)
            y += 60.0
            prev = link.id


def _add_fasteners(design: Design, catalog, internal: list[Part], panel: list[Part]) -> None:
    boards = [p for p in internal + panel if _area(p) >= 300]
    screws = 4 * len(boards) + 4  # 4 per board + 4 for the lid
    if screws and "m25-screws" in catalog:
        design.add(Part(id="fastener_kit", catalog=catalog.get("m25-screws"), role="mech.fastener", name="M2.5 Mounting Screws",
                        quantity=screws, reasons=[f"4 per board ({len(boards)}) + 4 lid screws"]))  # fmt: skip
    if boards and "m25-standoffs" in catalog:
        design.add(Part(id="pcb_standoffs", catalog=catalog.get("m25-standoffs"), role="mech.fastener", name="M2.5 Brass Standoffs",
                        quantity=4 * len(boards), reasons=["lift boards 5 mm off the floor for wiring clearance"]))  # fmt: skip


def _area(p: Part) -> float:
    w, _, d = _dims_of(p)
    return w * d


def _round5(v: float) -> float:
    return float(math.ceil(v / 5.0) * 5)


def _enclosure_material(materials: list[str]) -> str:
    if "carbon fiber" in materials:
        return "Carbon Fiber PETG"
    for m in ("asa", "abs", "petg", "pla", "tpu", "nylon"):
        if m in materials:
            return m.upper()
    return "PETG"


def _virtual(type_: str, name: str):
    """A catalog stub for generated (custom-fabricated) parts."""
    from hwbp.models import CatalogPart

    return CatalogPart(id=f"custom-{type_}", type=type_, category="mechanical", name=name, popularity=1.0)
