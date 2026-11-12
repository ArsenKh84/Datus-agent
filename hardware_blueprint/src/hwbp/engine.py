"""Pipeline orchestrator: prompt (+ optional picture) -> complete project document.

Phases mirror the reference generation run: planning -> questions -> electrical -> mechanical ->
revealing -> guide -> done. ``Engine.run`` is a generator of progress events (each carries ``phase``,
``status``, ``revision`` and a partial ``projectData`` snapshot once available) so it can back a long-poll
endpoint, an SSE stream or a CLI progress bar. ``Engine.generate`` runs it to completion.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterator

from hwbp import __version__
from hwbp.cad import build_mesh, feature_spec
from hwbp.catalog import Catalog, default_catalog
from hwbp.design import Design, Part
from hwbp.enrichment import CatalogSourcing, SearchLinkSourcing, SourcingProvider, enrich_node
from hwbp.firmware import generate_firmware
from hwbp.instructions import build_instructions
from hwbp.layout import layered_layout
from hwbp.mech import format_dims, solve_layout, synthesize_mechanics
from hwbp.models import ImagePromptSnapshot, Node, ProjectContent, canonical_type
from hwbp.power import design_power
from hwbp.pricing import build_bom, print_cost
from hwbp.requirements import RequirementExtractor, Requirements, RuleBasedExtractor
from hwbp.selection import select_components, suggest, unit_price
from hwbp.wiring import aggregate_edges, build_data_connections, validate

PHASES = ("planning", "questions", "electrical", "mechanical", "revealing", "guide", "done")
METAL_USD_PER_CM3 = {"titanium": 0.45, "aluminum": 0.03, "steel": 0.02}
MACHINING_FEE_USD = 25.0


@dataclass
class Question:
    id: str
    question: str
    options: list[str]
    default: str


def clarifying_questions(req: Requirements) -> list[Question]:
    """Questions worth asking when the prompt leaves a high-impact decision to defaults."""
    qs = []
    p = req.prompt.lower()
    if not any(
        w in p for w in ("battery", "portable", "wall", "usb", "solar", "plug", "mains", "wearable", "handheld")
    ):
        qs.append(
            Question(
                "power_source", "How should the device be powered?", ["battery", "wall", "solar"], req.power_source
            )
        )
    if req.power_source != "wall" and not any(w in p for w in ("hour", "day", "week", "month")):
        qs.append(
            Question(
                "runtime_hours",
                "How long should it run per charge?",
                ["4", "24", "168"],
                str(int(req.runtime_hours or 4)),
            )
        )
    if req.budget_usd is None and "cost" not in req.priorities:
        qs.append(
            Question("budget_usd", "Target electronics budget (USD)?", ["25", "50", "100", "no limit"], "no limit")
        )
    return qs


def apply_answers(req: Requirements, answers: dict[str, str]) -> Requirements:
    data = req.model_dump()
    for k, v in answers.items():
        if k == "power_source" and v in ("battery", "wall", "solar"):
            data["power_source"] = v
        elif k == "runtime_hours":
            try:
                data["runtime_hours"] = float(v)
            except ValueError:
                pass
        elif k == "budget_usd":
            data["budget_usd"] = None if str(v).startswith("no") else float(v)
    return Requirements.model_validate(data)


@dataclass
class Engine:
    catalog: Catalog = field(default_factory=default_catalog)
    extractor: RequirementExtractor = field(default_factory=RuleBasedExtractor)
    providers: list[SourcingProvider] = field(default_factory=lambda: [CatalogSourcing(), SearchLinkSourcing()])

    # ------------------------------------------------------------------ public
    def generate(self, prompt: str | None = None, requirements: Requirements | None = None, **kw) -> ProjectContent:
        last = None
        for ev in self.run(prompt, requirements=requirements, **kw):
            last = ev
        return last["projectData"]

    def run(self, prompt: str | None = None, requirements: Requirements | None = None, answers: dict[str, str] | None = None,
            form_factor: dict | None = None) -> Iterator[dict]:  # fmt: skip
        rev = 0

        def ev(phase: str, status: str, message: str, content: ProjectContent | None = None, **data) -> dict:
            nonlocal rev
            rev += 1
            return {
                "phase": phase,
                "status": status,
                "message": message,
                "revision": rev,
                "projectData": content,
                **data,
            }

        req = requirements or self.extractor.extract(prompt or "")
        if form_factor:
            req.form_factor = form_factor
        yield ev(
            "planning",
            "running",
            f"Understood: {', '.join(req.capabilities) or 'no explicit features'}",
            requirements=req.model_dump(),
        )
        qs = clarifying_questions(req)
        if answers:
            req = apply_answers(req, answers)
        yield ev(
            "questions",
            "running",
            f"{len(qs)} clarifying question(s); defaults applied",
            questions=[q.__dict__ for q in qs],
        )
        design = Design(req)
        select_components(design, self.catalog)
        build_data_connections(design)
        design_power(design, self.catalog)
        validate(design)
        yield ev(
            "electrical",
            "running",
            f"{len(design.parts)} parts, {len(design.connections)} connections",
            self._content(design, partial=True),
        )
        synthesize_mechanics(design, self.catalog)
        yield ev(
            "mechanical",
            "running",
            f"enclosure {design.mech_layout.get('enclosure', {}).get('outer')}",
            self._content(design, partial=True),
        )
        content = self._content(design, partial=False)
        yield ev("revealing", "running", "BOM, wiring and assembly layouts computed", content)
        pre, steps = build_instructions(design)
        content.instruction_preamble, content.instruction_steps = pre, steps
        content.firmware = generate_firmware(design)
        yield ev("guide", "running", f"{sum(len(s.sub_steps) for s in steps)} build steps", content)
        yield ev("done", "completed", "project ready", content)

    def design_for(self, req: Requirements) -> Design:
        """Electrical + mechanical design without document assembly (used by edits/tests)."""
        design = Design(req)
        select_components(design, self.catalog)
        build_data_connections(design)
        design_power(design, self.catalog)
        validate(design)
        synthesize_mechanics(design, self.catalog)
        return design

    # ------------------------------------------------------------------ document assembly
    def _content(self, design: Design, partial: bool) -> ProjectContent:
        req = design.requirements
        nodes = [self._node(p, design) for p in design.parts]
        content = ProjectContent(
            project_name=req.name,
            original_prompt=req.prompt,
            notes=req.tags,
            nodes=nodes,
            electrical_connections=list(design.connections),
            mechanical_connections=list(design.mech_connections),
            engine_version=__version__,
            requirements=req.model_dump(),
            issues=list(design.issues),
            power=design.power_summary,
        )
        if partial:
            return content
        content.plan = self._plan(design)
        content.project_description = self._describe(design)
        content.image_prompt_snapshot = ImagePromptSnapshot(tags=req.tags, design_hash=req.design_hash, description=req.prompt,
                                                            technical_description=content.project_description)  # fmt: skip
        content.suggestions = suggest(design, self.catalog)
        bom = build_bom(content.nodes)
        est = build_bom(content.nodes, use_offers=False)
        content.bom = {
            "lines": [ln.__dict__ for ln in bom.lines],
            "rollup": est.rollup(),
            "partsTotalUsd": bom.parts_total_usd,
            "shippingUsd": bom.shipping_usd,
            "totalUsd": bom.total_usd,
            "estimateTotalUsd": est.parts_total_usd,
        }
        content.wiring_layout = self._wiring_layout(content)
        mech = solve_layout(content)
        mech["enclosure"] = design.mech_layout.get("enclosure")
        content.mech_layout = mech
        for a, b in mech.get("collisions", []):
            design.issue("warning", "mech_interference", f"{a} and {b} overlap in the assembly.", [a, b])
        content.issues = list(design.issues)
        return content

    def _node(self, p: Part, design: Design) -> Node:
        cat = p.catalog
        t = canonical_type(cat.type)
        dims = p.dims_override or cat.dims_mm
        custom = cat.id.startswith("custom-")
        node = Node(
            id=p.id,
            name=p.name,
            type=t,
            subtype=cat.type if cat.type != t else None,
            category=cat.category,
            part_id=None if custom else cat.id,
            catalog_id=None if custom else cat.id,
            product_name=None if custom else cat.name,
            quantity=p.quantity,
            dimensions=format_dims(*dims) if dims else None,
            pins=self._pins(p, design) if cat.category == "electrical" else None,
            description=p.description or cat.description or None,
            material=p.material or cat.material,
            print_settings=p.print_settings,
            mounts_for=p.mounts_for,
            role=p.role,
            rationale=p.reasons[:6] or None,
        )
        if custom:
            node.estimated_cost = self._custom_cost(node)
        else:
            node.estimated_cost = round(unit_price(cat, p.quantity), 2)
            enrich_node(node, cat, self.providers)
        return node

    @staticmethod
    def _pins(p: Part, design: Design) -> list[str]:
        if p.catalog.type == "mcu":
            used = sorted(
                {mp for m in design.pin_map.values() for mp in m.values()},
                key=lambda s: (s.rstrip("0123456789"), int("".join(c for c in s if c.isdigit()) or -1)),
            )
            supply = [
                c.source_pin for c in design.connections if c.source == p.id and c.type == "power" and c.source_pin
            ]
            supply += [
                c.target_pin for c in design.connections if c.target == p.id and c.type == "power" and c.target_pin
            ]
            out = used + sorted(set(supply)) + ["GND"]
            seen: set[str] = set()
            return [x for x in out if not (x in seen or seen.add(x))]
        return p.catalog.all_pins()

    @staticmethod
    def _custom_cost(node: Node) -> float:
        mesh = build_mesh(feature_spec(node), name=node.id)
        t = canonical_type(node.type)
        mat = (node.material or "").lower()
        if t == "structural" and any(m in mat for m in METAL_USD_PER_CM3):
            rate = next(v for k, v in METAL_USD_PER_CM3.items() if k in mat)
            return round(mesh.volume() / 1000 * rate * 3 + MACHINING_FEE_USD, 2)  # 3x stock waste
        return max(0.5, print_cost(mesh.volume(), mesh.surface_area(), node.material, node.print_settings)["cost_usd"])

    @staticmethod
    def _wiring_layout(content: ProjectContent) -> dict:
        elec = [n for n in content.nodes if n.category == "electrical"]
        ids = [n.id for n in elec]
        edges = [
            e for e in aggregate_edges(content.electrical_connections) if e["source"] in ids and e["target"] in ids
        ]
        lay = layered_layout(ids, edges)
        lay["nodeMeta"] = {
            n.id: {"name": n.name, "product": n.product_name, "type": n.type, "pins": n.pins or []} for n in elec
        }
        return lay

    @staticmethod
    def _plan(design: Design) -> str:
        lines = []
        for p in design.parts:
            if p.catalog.category != "electrical":
                continue
            why = "; ".join(p.reasons[:2])
            lines.append(f"- {p.name}: {p.catalog.name}" + (f" — {why}" if why else ""))
        ps = design.power_summary
        if ps.get("battery"):
            lines.append(
                f"- Power: {ps['battery']['chemistry']} {ps['battery']['wh']} Wh, est. runtime {ps.get('runtimeHours')} h"
            )
        enc = design.mech_layout.get("enclosure")
        if enc:
            w, h, d = enc["outer"]
            lines.append(f"- Enclosure: {w:g}×{d:g}×{h:g} mm in {enc['material']}")
        return "\n".join(lines)

    @staticmethod
    def _describe(design: Design) -> str:
        req = design.requirements
        mcu = design.mcu
        periph = [
            p.catalog.name
            for p in design.parts
            if p.catalog.category == "electrical" and p.catalog.type not in ("mcu", "power", "battery", "passive")
        ]
        uniq = list(dict.fromkeys(periph))
        src = design.power_summary.get("source", req.power_source)
        txt = f"{req.name} built around a {mcu.catalog.name if mcu else 'controller'}"
        if uniq:
            txt += f" with {', '.join(uniq[:5])}"
        txt += f"; {src}-powered"
        if design.power_summary.get("runtimeHours"):
            txt += f" (≈{design.power_summary['runtimeHours']} h per charge)"
        enc = design.mech_layout.get("enclosure")
        if enc:
            txt += f", housed in a {enc['material']} enclosure"
        return txt + "."


def project_id_for(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:12]
