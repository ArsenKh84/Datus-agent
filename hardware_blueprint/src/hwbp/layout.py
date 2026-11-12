"""Wiring-diagram layout (layered / Sugiyama) and SVG rendering.

The reference product feeds ELK ``layered`` (direction RIGHT, NETWORK_SIMPLEX placement, LAYER_SWEEP
crossing minimisation). This is a dependency-free implementation of the same family of algorithm:

1. **Cycle removal** — DFS from sources; back edges are reversed (and flagged) so the graph is a DAG.
2. **Layer assignment** — longest path from sources (power source -> regulators -> MCU -> peripherals).
3. **Dummy nodes** — an edge spanning k layers is split into k-1 virtual nodes so crossings are countable.
4. **Crossing minimisation** — iterative barycenter sweeps (down then up), keeping the best ordering by an
   exact bilayer crossing count.
5. **Coordinate assignment** — layers become columns; within a column nodes are stacked in order, then
   relaxed toward the median y of their neighbours while preserving order and min spacing.
6. **Edge routing** — orthogonal 3-segment routes (exit right, vertical jog at the channel midpoint,
   enter left), with per-side port slots so parallel edges don't overlap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape

from hwbp.models import TYPE_META

NODE_W, NODE_H = 200.0, 132.0
LAYER_GAP, NODE_GAP = 140.0, 40.0
EDGE_STYLE = {
    "data": {"color": "#52ae59", "width": 2.0, "dash": None},
    "power": {"color": "#f5c94e", "width": 1.5, "dash": "4 4"},
    "ground": {"color": "#9d9f9e", "width": 1.0, "dash": "2 3"},
}
MAX_PIN_CHIPS = 8


@dataclass
class LNode:
    id: str
    w: float = NODE_W
    h: float = NODE_H
    dummy: bool = False
    layer: int = 0
    order: float = 0.0
    x: float = 0.0
    y: float = 0.0
    meta: dict = field(default_factory=dict)


def _remove_cycles(ids: list[str], edges: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], set[int]]:
    adj: dict[str, list[tuple[int, str]]] = {i: [] for i in ids}
    for k, (a, b) in enumerate(edges):
        adj[a].append((k, b))
    state: dict[str, int] = {}
    reversed_idx: set[int] = set()
    indeg = {i: 0 for i in ids}
    for _a, b in edges:
        indeg[b] += 1
    roots = [i for i in ids if indeg[i] == 0] + ids

    def dfs(u: str) -> None:
        state[u] = 1
        for k, v in adj[u]:
            if state.get(v) == 1:
                reversed_idx.add(k)
            elif v not in state:
                dfs(v)
        state[u] = 2

    for r in roots:
        if r not in state:
            dfs(r)
    dag = [(b, a) if k in reversed_idx else (a, b) for k, (a, b) in enumerate(edges)]
    return dag, reversed_idx


def _longest_path_layers(ids: list[str], dag: list[tuple[str, str]]) -> dict[str, int]:
    preds: dict[str, list[str]] = {i: [] for i in ids}
    for a, b in dag:
        if a != b:
            preds[b].append(a)
    layer: dict[str, int] = {}

    def depth(u: str, seen: frozenset = frozenset()) -> int:
        if u in layer:
            return layer[u]
        if u in seen:  # defensive; DAG guaranteed
            return 0
        layer[u] = 0 if not preds[u] else 1 + max(depth(p, seen | {u}) for p in preds[u])
        return layer[u]

    for i in ids:
        depth(i)
    return layer


def _count_crossings(upper: list[str], lower: list[str], edges: list[tuple[str, str]]) -> int:
    pos_u = {n: i for i, n in enumerate(upper)}
    pos_l = {n: i for i, n in enumerate(lower)}
    segs = sorted((pos_u[a], pos_l[b]) for a, b in edges if a in pos_u and b in pos_l)
    count = 0
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            if segs[i][0] < segs[j][0] and segs[i][1] > segs[j][1]:
                count += 1
    return count


def layered_layout(
    node_ids: list[str], edges: list[dict], sizes: dict[str, tuple[float, float]] | None = None, sweeps: int = 8
) -> dict:
    """Lay out nodes/edges. ``edges`` items need ``source``/``target`` (+ any style keys, copied through)."""
    sizes = sizes or {}
    pairs = [
        (e["source"], e["target"])
        for e in edges
        if e["source"] in node_ids and e["target"] in node_ids and e["source"] != e["target"]
    ]
    dag, rev = _remove_cycles(node_ids, pairs)
    layers = _longest_path_layers(node_ids, dag)
    nodes = {i: LNode(i, *sizes.get(i, (NODE_W, NODE_H)), layer=layers[i]) for i in node_ids}
    # split long edges with dummy nodes
    chains: list[list[str]] = []
    seg_edges: list[tuple[str, str]] = []
    for k, (a, b) in enumerate(dag):
        chain = [a]
        for lyr in range(nodes[a].layer + 1, nodes[b].layer):
            d = f"__d{k}_{lyr}"
            nodes[d] = LNode(d, 0.0, 0.0, dummy=True, layer=lyr)
            chain.append(d)
        chain.append(b)
        chains.append(chain)
        seg_edges += list(zip(chain, chain[1:]))
    n_layers = max((n.layer for n in nodes.values()), default=0) + 1
    by_layer: list[list[str]] = [[] for _ in range(n_layers)]
    for nid in node_ids + [n for n in nodes if nodes[n].dummy]:
        by_layer[nodes[nid].layer].append(nid)
    # crossing minimisation (barycenter sweeps)
    up: dict[str, list[str]] = {n: [] for n in nodes}
    down: dict[str, list[str]] = {n: [] for n in nodes}
    for a, b in seg_edges:
        down[a].append(b)
        up[b].append(a)

    def total_crossings(order: list[list[str]]) -> int:
        return sum(_count_crossings(order[i], order[i + 1], seg_edges) for i in range(len(order) - 1))

    best = [list(lyr) for lyr in by_layer]
    best_c = total_crossings(best)
    cur = [list(lyr) for lyr in by_layer]
    for it in range(sweeps):
        rng = range(1, n_layers) if it % 2 == 0 else range(n_layers - 2, -1, -1)
        for li in rng:
            ref = cur[li - 1] if it % 2 == 0 else cur[li + 1]
            pos = {n: i for i, n in enumerate(ref)}
            nbrs = up if it % 2 == 0 else down
            own = {n: i for i, n in enumerate(cur[li])}  # list is empty during .sort(): snapshot positions

            def bary(n: str, _pos=pos, _nbrs=nbrs, _own=own) -> float:
                ps = [_pos[m] for m in _nbrs[n] if m in _pos]
                return sum(ps) / len(ps) if ps else float(_own[n])

            cur[li].sort(key=bary)
        c = total_crossings(cur)
        if c < best_c:
            best, best_c = [list(lyr) for lyr in cur], c
    # coordinates
    x = 0.0
    for _li, lyr in enumerate(best):
        col_w = max((nodes[n].w for n in lyr), default=0.0)
        y = 0.0
        for n in lyr:
            nodes[n].x = x + (col_w - nodes[n].w) / 2
            nodes[n].y = y
            y += nodes[n].h + (NODE_GAP if not nodes[n].dummy else NODE_GAP / 2)
        x += col_w + LAYER_GAP
    for _ in range(6):  # median relaxation keeping order + spacing
        for lyr in best:
            for i, n in enumerate(lyr):
                nb = up[n] + down[n]
                if not nb:
                    continue
                ys = sorted(nodes[m].y + nodes[m].h / 2 for m in nb)
                target = ys[len(ys) // 2] - nodes[n].h / 2
                lo = nodes[lyr[i - 1]].y + nodes[lyr[i - 1]].h + NODE_GAP if i > 0 else -1e9
                hi = nodes[lyr[i + 1]].y - nodes[n].h - NODE_GAP if i + 1 < len(lyr) else 1e9
                nodes[n].y = min(max(target, lo), hi) if lo <= hi else lo
    min_y = min((n.y for n in nodes.values()), default=0.0)
    for n in nodes.values():
        n.y -= min_y
    # routing with port slots
    out_slots: dict[str, int] = {}
    in_slots: dict[str, int] = {}
    routed = []
    pair_to_chain = {}
    for k, (a, b) in enumerate(pairs):
        pair_to_chain.setdefault((a, b), []).append(k)
    used = {}
    for e in edges:
        a, b = e["source"], e["target"]
        if (a, b) not in pair_to_chain:
            continue
        idx = used.get((a, b), 0)
        used[(a, b)] = idx + 1
        k = pair_to_chain[(a, b)][min(idx, len(pair_to_chain[(a, b)]) - 1)]
        chain = chains[k] if k not in rev else list(reversed(chains[k]))
        pts = _route(chain, nodes, out_slots, in_slots)
        routed.append({**e, "points": pts, "reversed": k in rev})
    real = [nodes[i] for i in node_ids]
    width = max((n.x + n.w for n in real), default=0.0)
    height = max((n.y + n.h for n in real), default=0.0)
    return {
        "nodes": {
            i: {
                "x": round(nodes[i].x, 1),
                "y": round(nodes[i].y, 1),
                "w": nodes[i].w,
                "h": nodes[i].h,
                "layer": nodes[i].layer,
            }
            for i in node_ids
        },
        "edges": routed,
        "width": round(width, 1),
        "height": round(height, 1),
        "crossings": best_c,
    }


def _route(chain: list[str], nodes: dict[str, LNode], out_slots: dict, in_slots: dict) -> list[list[float]]:
    a, b = nodes[chain[0]], nodes[chain[-1]]
    so = out_slots.get(a.id, 0)
    out_slots[a.id] = so + 1
    si = in_slots.get(b.id, 0)
    in_slots[b.id] = si + 1
    forward = a.x <= b.x
    sy = a.y + a.h * (0.3 + 0.12 * (so % 5))
    ty = b.y + b.h * (0.3 + 0.12 * (si % 5))
    sx = a.x + a.w if forward else a.x
    tx = b.x if forward else b.x + b.w
    pts = [[sx, sy]]
    for d in chain[1:-1]:  # pass through dummy nodes
        dn = nodes[d]
        midx = (pts[-1][0] + dn.x) / 2
        pts += [[midx, pts[-1][1]], [midx, dn.y], [dn.x, dn.y]]
    midx = (pts[-1][0] + tx) / 2
    pts += [[midx, pts[-1][1]], [midx, ty], [tx, ty]]
    return [[round(px, 1), round(py, 1)] for px, py in pts]


# ------------------------------------------------------------------------------ SVG
def _chip(label: str) -> str:
    return label if len(label) <= 7 else label[:6] + "…"


def render_svg(layout: dict, node_meta: dict[str, dict], dark: bool = False, margin: float = 24.0) -> str:
    """Render a laid-out wiring diagram. ``node_meta[id]`` = {name, product, type, pins}."""
    bg, fg, card, border = (
        ("#131313", "#f2f2f2", "#1c1c1c", "#3f403f") if dark else ("#ffffff", "#131313", "#fafafa", "#d4d5d4")
    )
    w, h = layout["width"] + 2 * margin, layout["height"] + 2 * margin + 40
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.0f} {h:.0f}" width="{w:.0f}" height="{h:.0f}" font-family="Inter, Raleway, system-ui, sans-serif">',
        f'<rect width="100%" height="100%" fill="{bg}"/>',
        "<defs>"
        + "".join(
            f'<marker id="arr-{t}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="{s["color"]}"/></marker>'
            for t, s in EDGE_STYLE.items()
        )
        + "</defs>",
        f'<g transform="translate({margin},{margin + 40})">',
    ]
    for e in layout["edges"]:
        st = EDGE_STYLE.get(e.get("type", "data"), EDGE_STYLE["data"])
        d = "M" + " L".join(f"{x},{y}" for x, y in e["points"])
        dash = f' stroke-dasharray="{st["dash"]}"' if st["dash"] else ""
        parts.append(
            f'<path d="{d}" fill="none" stroke="{st["color"]}" stroke-width="{st["width"]}"{dash} marker-end="url(#arr-{e.get("type", "data")})"/>'
        )
        if e.get("label"):
            (x1, y1), (x2, y2) = e["points"][len(e["points"]) // 2 - 1], e["points"][len(e["points"]) // 2]
            lx, ly = (x1 + x2) / 2, (y1 + y2) / 2
            tw = 6.2 * len(e["label"]) + 8
            parts.append(
                f'<rect x="{lx - tw / 2:.1f}" y="{ly - 8:.1f}" width="{tw:.1f}" height="14" rx="3" fill="{bg}" stroke="{border}"/>'
            )
            parts.append(
                f'<text x="{lx:.1f}" y="{ly + 2.5:.1f}" font-size="9" font-weight="700" text-anchor="middle" fill="{st["color"]}">{escape(e["label"])}</text>'
            )
    for nid, box in layout["nodes"].items():
        m = node_meta.get(nid, {})
        color = TYPE_META.get(m.get("type", "other"), TYPE_META["other"])[1]
        x, y, bw, bh = box["x"], box["y"], box["w"], box["h"]
        parts.append(
            f'<g data-node="{escape(nid)}"><rect x="{x}" y="{y}" width="{bw}" height="{bh}" rx="8" fill="{card}" stroke="{color}" stroke-width="1.5"/>'
        )
        parts.append(
            f'<text x="{x + 10}" y="{y + 18}" font-size="9" font-weight="700" fill="{color}">{escape(TYPE_META.get(m.get("type", "other"), TYPE_META["other"])[0].upper())}</text>'
        )
        parts.append(
            f'<text x="{x + 10}" y="{y + 36}" font-size="13" font-weight="600" fill="{fg}">{escape(m.get("name", nid))[:26]}</text>'
        )
        parts.append(
            f'<text x="{x + 10}" y="{y + 52}" font-size="10" fill="{fg}" opacity="0.6">{escape(m.get("product") or "")[:30]}</text>'
        )
        for k, pin in enumerate((m.get("pins") or [])[:MAX_PIN_CHIPS]):
            cx, cy = x + 10 + (k % 4) * 46, y + 66 + (k // 4) * 24
            parts.append(
                f'<rect x="{cx}" y="{cy}" width="42" height="18" rx="9" fill="none" stroke="{color}" opacity="0.8"/>'
            )
            parts.append(
                f'<text x="{cx + 21}" y="{cy + 12.5}" font-size="8.5" text-anchor="middle" fill="{color}">{escape(_chip(pin))}</text>'
            )
        parts.append("</g>")
    legend_y = -28
    for i, (t, st) in enumerate(EDGE_STYLE.items()):
        lx = i * 110
        dash = f' stroke-dasharray="{st["dash"]}"' if st["dash"] else ""
        parts.append(
            f'<line x1="{lx}" y1="{legend_y}" x2="{lx + 28}" y2="{legend_y}" stroke="{st["color"]}" stroke-width="{st["width"]}"{dash}/>'
        )
        parts.append(f'<text x="{lx + 34}" y="{legend_y + 4}" font-size="10" fill="{fg}">{t.upper()}</text>')
    parts.append("</g></svg>")
    return "\n".join(parts)


# ------------------------------------------------------------------------------ enriched wiring mode
RAIL_W, RAIL_H = 120.0, 44.0


def enriched_wiring_layout(content) -> dict:
    """Per-connection edges labelled with pins, power/ground routed through synthetic rail nodes
    (``rail:5V``, ``rail:3.3V``, ``rail:GND``) — the reference's "Enrich wiring" view."""
    elec = [n for n in content.nodes if n.category == "electrical"]
    ids = [n.id for n in elec]
    edges: list[dict] = []
    rails: dict[str, str] = {}

    def rail(name: str) -> str:
        rid = f"rail:{name}"
        rails[rid] = name
        return rid

    used_pins: dict[str, set[str]] = {i: set() for i in ids}
    for c in content.electrical_connections:
        if c.source not in used_pins or c.target not in used_pins:
            continue
        if c.source_pin:
            used_pins[c.source].add(c.source_pin)
        if c.target_pin:
            used_pins[c.target].add(c.target_pin)
        if c.type in ("power", "ground"):
            r = rail("GND" if c.type == "ground" else (c.net or c.voltage or "PWR"))
            if not any(e["source"] == c.source and e["target"] == r for e in edges):
                edges.append({"type": c.type, "source": c.source, "target": r, "label": c.source_pin or ""})
            edges.append({"type": c.type, "source": r, "target": c.target, "label": c.target_pin or ""})
        else:
            label = f"{c.source_pin}→{c.target_pin}" if c.source_pin and c.target_pin else (c.protocol or "")
            edges.append({"type": "data", "source": c.source, "target": c.target, "label": label})
    all_ids = ids + sorted(rails)
    sizes = {r: (RAIL_W, RAIL_H) for r in rails}
    lay = layered_layout(all_ids, edges, sizes)
    meta = {
        n.id: {
            "name": n.name,
            "product": n.product_name,
            "type": n.type,
            "pins": sorted(used_pins[n.id]) or (n.pins or []),
        }
        for n in elec
    }
    meta.update({r: {"name": f"{name} rail", "product": "", "type": "power", "pins": []} for r, name in rails.items()})
    lay["nodeMeta"] = meta
    lay["mode"] = "enriched"
    return lay
