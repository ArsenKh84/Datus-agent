"""Project editing: reference-format edit plans + natural-language edits + optimistic revisions.

1. **Edit plans** (wire format observed on blueprint.io): ``{addParts, removeNodeIds, addElectricalConnections,
   removeElectricalConnections, addMechanicalConnections, removeMechanicalConnections,
   newPinsUsedOnExistingNodes}``. ``validate_plan`` repairs what an LLM typically gets wrong (unknown ids,
   undeclared pins, duplicates, dangling connections of removed nodes) and ``apply_plan`` applies it.
2. **Natural-language edits** — ``message_to_ops`` classifies a chat message and maps it to requirement
   overrides (force/exclude parts, add/remove capabilities, power source, budget, counts); ``Engine``
   regenerates deterministically, so unchanged parts keep their ids.
3. **Revisions** — ``ProjectStore.save`` implements optimistic concurrency (409-style ``VersionConflict``).
"""

from __future__ import annotations

import copy
import difflib
import re
import threading
import time
from dataclasses import dataclass, field

from hwbp.catalog import Catalog
from hwbp.classify import classify_message
from hwbp.enrichment import canonicalize
from hwbp.models import ElectricalConnection, MechanicalConnection, Node, Project, ProjectContent, canonical_type
from hwbp.requirements import CAPABILITY_RULES, Requirements, RuleBasedExtractor

PLAN_KEYS = (
    "addParts",
    "removeNodeIds",
    "addElectricalConnections",
    "removeElectricalConnections",
    "addMechanicalConnections",
    "removeMechanicalConnections",
    "newPinsUsedOnExistingNodes",
)


# ------------------------------------------------------------------------------ edit plans
@dataclass
class PlanReport:
    repaired: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


def _resolve_id(ref: str, ids: list[str]) -> str | None:
    if ref in ids:
        return ref
    m = difflib.get_close_matches(ref, ids, n=1, cutoff=0.75)
    return m[0] if m else None


def validate_plan(content: ProjectContent, plan: dict, catalog: Catalog | None = None) -> tuple[dict, PlanReport]:
    plan = {k: copy.deepcopy(plan.get(k) or []) for k in PLAN_KEYS}
    rep = PlanReport()
    ids = [n.id for n in content.nodes]
    removed = set()
    for rid in plan["removeNodeIds"]:
        r = _resolve_id(rid, ids)
        if r is None:
            rep.dropped.append(f"removeNodeIds: unknown {rid}")
        else:
            removed.add(r)
            if r != rid:
                rep.repaired.append(f"removeNodeIds: {rid} -> {r}")
    plan["removeNodeIds"] = sorted(removed)
    new_parts = []
    for raw in plan["addParts"]:
        node = _node_from_add(raw, catalog, ids + [p["id"] for p in new_parts])
        new_parts.append(node)
    plan["addParts"] = new_parts
    live = [i for i in ids if i not in removed] + [p["id"] for p in new_parts]
    pins = {n.id: set(n.pins or []) for n in content.nodes}
    pins.update({p["id"]: set(p.get("pins") or []) for p in new_parts})
    declared: dict[str, set[str]] = {}
    for key in ("addElectricalConnections", "addMechanicalConnections"):
        out, seen = [], set()
        existing = (
            {(c.source, c.target, c.source_pin or "", c.target_pin or "") for c in content.electrical_connections}
            if key.startswith("addE")
            else set()
        )
        for c in plan[key]:
            s, t = _resolve_id(c.get("source", ""), live), _resolve_id(c.get("target", ""), live)
            if s is None or t is None or s == t:
                rep.dropped.append(f"{key}: dangling {c.get('source')}->{c.get('target')}")
                continue
            if (s, t) != (c.get("source"), c.get("target")):
                rep.repaired.append(f"{key}: {c.get('source')}->{c.get('target')} => {s}->{t}")
            c = {**c, "source": s, "target": t}
            sig = (s, t, c.get("sourcePin") or "", c.get("targetPin") or "")
            if sig in seen or sig in existing:
                rep.dropped.append(f"{key}: duplicate {s}->{t}")
                continue
            seen.add(sig)
            if key.startswith("addE"):
                for node_id, pin in ((s, c.get("sourcePin")), (t, c.get("targetPin"))):
                    if pin and pin not in pins.get(node_id, set()):
                        declared.setdefault(node_id, set()).add(pin)
            out.append(c)
        plan[key] = out
    extra = {
        d["nodeId"]: set(d.get("pins") or []) for d in plan["newPinsUsedOnExistingNodes"] if d.get("nodeId") in live
    }
    for node_id, ps in declared.items():
        if not ps <= extra.get(node_id, set()):
            rep.repaired.append(f"declared new pins on {node_id}: {sorted(ps - extra.get(node_id, set()))}")
            extra.setdefault(node_id, set()).update(ps)
    plan["newPinsUsedOnExistingNodes"] = [{"nodeId": k, "pins": sorted(v)} for k, v in sorted(extra.items())]
    return plan, rep


def _node_from_add(raw: dict, catalog: Catalog | None, taken: list[str]) -> dict:
    node = dict(raw)
    if catalog is not None and (node.get("catalogId") or node.get("productName") or node.get("name")):
        if node.get("catalogId") in catalog:
            canon_part = catalog.get(node["catalogId"])
        else:
            canon_part = canonicalize(node, catalog).part
        node.setdefault("productName", canon_part.name)
        node.setdefault("type", canonical_type(canon_part.type))
        node.setdefault("category", canon_part.category)
        if not canon_part.id.startswith("transient-"):
            node.setdefault("catalogId", canon_part.id)
            node.setdefault("pins", canon_part.all_pins() or None)
    node["type"] = canonical_type(node.get("type"))
    base = re.sub(r"[^a-z0-9]+", "_", (node.get("id") or node.get("name") or "part").lower()).strip("_")
    nid, k = base, 2
    while nid in taken:
        nid, k = f"{base}_{k}", k + 1
    node["id"] = nid
    node.setdefault("name", nid.replace("_", " ").title())
    node.setdefault("quantity", 1)
    return node


def apply_plan(content: ProjectContent, plan: dict) -> ProjectContent:
    """Apply a *validated* plan, returning a new document (input is not mutated)."""
    out = content.model_copy(deep=True)
    removed = set(plan.get("removeNodeIds") or [])
    out.nodes = [n for n in out.nodes if n.id not in removed] + [
        Node.model_validate(p) for p in plan.get("addParts") or []
    ]
    out.electrical_connections = [
        c for c in out.electrical_connections if c.source not in removed and c.target not in removed
    ]
    out.mechanical_connections = [
        c for c in out.mechanical_connections if c.source not in removed and c.target not in removed
    ]
    for rm in plan.get("removeElectricalConnections") or []:
        out.electrical_connections = [c for c in out.electrical_connections if not _conn_match(c, rm)]
    for rm in plan.get("removeMechanicalConnections") or []:
        out.mechanical_connections = [
            c for c in out.mechanical_connections if not (c.source == rm.get("source") and c.target == rm.get("target"))
        ]
    out.electrical_connections += [
        ElectricalConnection.model_validate(c) for c in plan.get("addElectricalConnections") or []
    ]
    out.mechanical_connections += [
        MechanicalConnection.model_validate(c) for c in plan.get("addMechanicalConnections") or []
    ]
    for d in plan.get("newPinsUsedOnExistingNodes") or []:
        n = out.node(d["nodeId"])
        if n is not None:
            n.pins = list(dict.fromkeys((n.pins or []) + list(d.get("pins") or [])))
    for step in out.instruction_steps:
        for sub in step.sub_steps:
            sub.part_ids = [p for p in sub.part_ids if p not in removed]
    return out


def _conn_match(c: ElectricalConnection, rm: dict) -> bool:
    if c.source != rm.get("source") or c.target != rm.get("target"):
        return False
    for attr, key in (("source_pin", "sourcePin"), ("target_pin", "targetPin"), ("type", "type")):
        if rm.get(key) is not None and getattr(c, attr) != rm[key]:
            return False
    return True


# ------------------------------------------------------------------------------ LLM context
def project_summary(content: ProjectContent, include_mounts: bool = True) -> str:
    """Compact text view of a project for LLM prompts (edit planning, Q&A, suggestions)."""
    lines = [f'Project: "{content.project_name}"', f"Nodes ({len(content.nodes)}):"]
    for n in content.nodes:
        extra = (
            [f"quantity: {n.quantity}"]
            + ([f"pins: [{', '.join(n.pins)}]"] if n.pins else [])
            + ([f"dims: {n.dimensions}"] if n.dimensions else [])
        )
        lines.append(f"  - {n.id}: {n.name} ({n.category}/{n.type}) — " + "; ".join(extra))
    lines.append(f"Electrical connections ({len(content.electrical_connections)}):")
    for c in content.electrical_connections:
        bits = [c.type] + [x for x in (c.protocol, c.voltage) if x]
        pins = f", {c.source_pin}→{c.target_pin}" if c.source_pin and c.target_pin else ""
        lines.append(f"  - {c.source} → {c.target} ({', '.join(bits)}{pins})")
    lines.append(f"Mechanical connections ({len(content.mechanical_connections)}):")
    lines += [f"  - {c.source} ↔ {c.target} ({c.label})" for c in content.mechanical_connections]
    if include_mounts:
        mounts = [n for n in content.nodes if n.mounts_for]
        if mounts:
            lines.append(f"Mounts ({len(mounts)}):")
            lines += [f"  - {n.id}: {n.name} mounts {n.mounts_for}" for n in mounts]
    touched = {c.source for c in content.electrical_connections + content.mechanical_connections} | {
        c.target for c in content.electrical_connections + content.mechanical_connections
    }
    lonely = [n.id for n in content.nodes if n.id not in touched]
    if lonely:
        lines.append(f"Disconnected nodes ({len(lonely)}) — these have ZERO connections: {', '.join(lonely)}")
    return "\n".join(lines)


# ------------------------------------------------------------------------------ natural-language edits
@dataclass
class EditOp:
    op: str  # force | exclude | add_capability | remove_capability | set_power | set_budget | set_count | set_runtime
    args: dict


def message_to_ops(message: str, content: ProjectContent, catalog: Catalog) -> tuple[str, list[EditOp]]:
    """Return (mode, ops). mode == "ask" means: answer, don't edit."""
    cls = classify_message(message, content)
    if cls.mode == "ask":
        return "ask", []
    text = message.lower()
    ops: list[EditOp] = []
    mentioned = RuleBasedExtractor().extract(message)
    targets = cls.targets
    if cls.intent == "swap_part" and targets:
        tail = re.split(r"\b(with|for|to)\b", text, maxsplit=1)[-1]
        node = content.node(targets[0])
        tfilter = canonical_type(node.type) if node else None
        hits = [p for p, _ in catalog.search(tail, limit=10) if not tfilter or canonical_type(p.type) == tfilter]
        if hits and node is not None and node.role:
            ops.append(EditOp("force", {"role": node.role.split("#")[0], "catalogId": hits[0].id}))
    elif cls.intent == "remove_part":
        for t in targets:
            node = content.node(t)
            if (
                node
                and node.role
                and "." in node.role
                and not node.role.startswith(("power.", "mech.", "driver.", "logic."))
            ):
                ops.append(EditOp("remove_capability", {"capability": node.role.split("#")[0]}))
            elif node and node.catalog_id:
                ops.append(EditOp("exclude", {"catalogId": node.catalog_id}))
        for cap in mentioned.capabilities:
            if not targets:
                ops.append(EditOp("remove_capability", {"capability": cap}))
    elif cls.intent == "change_power":
        ops.append(EditOp("set_power", {"source": mentioned.power_source}))
    elif cls.intent == "reduce_cost":
        ops.append(EditOp("set_budget", {"usd": mentioned.budget_usd or _current_cost(content) * 0.8}))
    elif cls.intent == "set_quantity":
        words = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6"}
        m = re.search(r"(\d+|one|two|three|four|five|six)\s+(more|fewer|less)\s+(\w+)", text)
        if m:  # relative: current count +/- n
            caps = [c for c, pats in CAPABILITY_RULES.items() if any(re.search(p, m.group(3)) for p in pats)]
            sign = 1 if m.group(2) == "more" else -1
            for cap in caps[:1]:
                cur = len([n for n in content.nodes if (n.role or "").startswith(cap)])
                ops.append(
                    EditOp(
                        "set_count",
                        {"capability": cap, "count": max(0, cur + sign * int(words.get(m.group(1), m.group(1))))},
                    )
                )
        else:  # absolute: "use 6 servos"
            for cap, n in mentioned.counts.items():
                ops.append(EditOp("set_count", {"capability": cap, "count": n}))
    if not ops and mentioned.capabilities and cls.intent in ("add_part", "modify"):
        ops += [EditOp("add_capability", {"capability": c}) for c in mentioned.capabilities]
    if mentioned.runtime_hours and re.search(r"\b(hours?|days?|weeks?)\b", text):
        ops.append(EditOp("set_runtime", {"hours": mentioned.runtime_hours}))
    return "edit", ops


def _current_cost(content: ProjectContent) -> float:
    return float(
        (content.bom or {}).get("estimateTotalUsd") or sum((n.estimated_cost or 0) * n.quantity for n in content.nodes)
    )


def apply_ops(req: Requirements, ops: list[EditOp]) -> Requirements:
    data = req.model_dump()
    caps = set(data["capabilities"])
    for o in ops:
        a = o.args
        if o.op == "force":
            data["force"][a["role"]] = a["catalogId"]
        elif o.op == "exclude":
            data["exclude"] = sorted(set(data["exclude"]) | {a["catalogId"]})
        elif o.op == "add_capability":
            caps.add(a["capability"])
        elif o.op == "remove_capability":
            caps.discard(a["capability"])
            data["counts"].pop(a["capability"], None)
        elif o.op == "set_power":
            data["power_source"] = a["source"]
            if a["source"] != "wall" and not data.get("runtime_hours"):
                data["runtime_hours"] = 4.0
        elif o.op == "set_budget":
            data["budget_usd"] = round(float(a["usd"]), 2)
        elif o.op == "set_count":
            caps.add(a["capability"])
            data["counts"][a["capability"]] = int(a["count"])
        elif o.op == "set_runtime":
            data["runtime_hours"] = float(a["hours"])
    data["capabilities"] = sorted(caps)
    return Requirements.model_validate(data)


# ------------------------------------------------------------------------------ revisions
class VersionConflict(RuntimeError):
    def __init__(self, current: int):
        super().__init__(f"version_conflict: current version is {current}")
        self.current = current


class ProjectStore:
    """In-memory project store with optimistic concurrency and version history (swap for a DB)."""

    def __init__(self) -> None:
        self._p: dict[str, Project] = {}
        self._history: dict[str, list[tuple[int, ProjectContent]]] = {}
        self._lock = threading.Lock()

    def create(self, project_id: str, content: ProjectContent, owner_id: str | None = None) -> Project:
        now = _now()
        p = Project(id=project_id, revision=1, owner_id=owner_id, created_at=now, updated_at=now, content=content)
        with self._lock:
            self._p[project_id] = p
            self._history[project_id] = [(1, content)]
        return p

    def get(self, project_id: str) -> Project:
        try:
            return self._p[project_id]
        except KeyError:
            raise KeyError(f"unknown project {project_id}") from None

    def save(self, project_id: str, content: ProjectContent, base_version: int) -> Project:
        with self._lock:
            p = self.get(project_id)
            if base_version != p.revision:
                raise VersionConflict(p.revision)
            p.revision += 1
            p.content = content
            p.updated_at = _now()
            self._history[project_id].append((p.revision, content))
            return p

    def versions(self, project_id: str) -> list[int]:
        return [v for v, _ in self._history.get(project_id, [])]

    def restore(self, project_id: str, version: int) -> Project:
        snap = dict(self._history[project_id]).get(version)
        if snap is None:
            raise KeyError(f"no version {version}")
        return self.save(project_id, snap.model_copy(deep=True), self.get(project_id).revision)

    def __contains__(self, project_id: str) -> bool:
        return project_id in self._p


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
