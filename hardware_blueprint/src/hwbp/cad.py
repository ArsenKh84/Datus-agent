"""CAD: parametric part definitions -> meshes, CAD job lifecycle, credit wallet, assembly export.

Reference behaviour (observed): the client POSTs a CAD job per part ``{projectId, nodeId, mode, instruction,
parameters, modelTier}``, polls every 2.5 s through queued -> running -> succeeded|failed|canceled, and
receives artifacts ``model.glb`` / ``model.stl`` / ``model.step``; the GLB replaces the placeholder box.
Generation itself is server-side (an LLM writes a CAD script).

Here generation is deterministic: each layout part gets a *feature spec* (a tiny parametric CSG-free
recipe) chosen from its type/role, evaluated by ``hwbp.mesh`` into watertight meshes. An LLM-backed
generator can be plugged in via ``CadGenerator`` and must return a ``Mesh``.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Protocol

from hwbp import mesh as M
from hwbp.mech import is_layout_part, parse_dims, shape_of
from hwbp.models import TYPE_META, Node, ProjectContent, canonical_type
from hwbp.pricing import print_cost

JOB_STATES = ("queued", "running", "succeeded", "failed", "canceled")
CREDIT_BUNDLES = {"small": (20, 10.0), "medium": (120, 50.0), "large": (560, 200.0)}  # credits, USD
PLAN_MONTHLY_CREDITS = {"basic": 5, "pro": 40, "ultra": None}  # None = unlimited


# ------------------------------------------------------------------------------ feature specs
def feature_spec(node: Node, content: ProjectContent | None = None) -> dict:
    """Pick a parametric recipe for a node. All sizes mm; (w, h, d) = X, Y(up), Z."""
    w, h, d = parse_dims(node.dimensions) or (20.0, 10.0, 20.0)
    t = canonical_type(node.type)
    name = (node.name or "").lower()
    if t == "enclosure" or "enclosure base" in name or ("shell" in name and t == "3d_printed"):
        return {"op": "open_box", "w": w, "h": h, "d": d, "wall": 2.0}
    if "lid" in name or "bezel" in name:
        windows = _windows_for(node, content)
        if windows:
            ww, wd = windows[0]
            return {"op": "frame", "w": w, "d": d, "t": h, "hole_w": min(ww, w - 8), "hole_d": min(wd, d - 8)}
        return {"op": "box", "w": w, "h": h, "d": d}
    if "mount" in name and t == "3d_printed":
        return {
            "op": "standoff_plate",
            "w": w,
            "d": d,
            "plate_t": 2.0,
            "post_h": max(2.0, h - 2.0),
            "post_r": 2.5,
            "inset": 4.0,
        }
    if shape_of(node) == "cylinder":
        return {"op": "cylinder", "r": min(w, d) / 2, "h": h}
    if t == "structural":
        return {"op": "box", "w": w, "h": h, "d": d}
    return {"op": "box", "w": w, "h": h, "d": d}


def _windows_for(node: Node, content: ProjectContent | None) -> list[tuple[float, float]]:
    if content is None:
        return []
    out = []
    for n in content.nodes:
        if canonical_type(n.type) == "display":
            dims = parse_dims(n.dimensions)
            if dims and (n.mounts_for == node.id or "lid" in (node.name or "").lower() or node.mounts_for == n.id):
                out.append((dims[0] - 6, dims[2] - 6))  # visible area ≈ module minus 3 mm border
    return out


def build_mesh(spec: dict, name: str = "part") -> M.Mesh:
    op = spec["op"]
    if op == "box":
        return M.box(spec["w"], spec["h"], spec["d"], name=name)
    if op == "open_box":
        return M.open_box(spec["w"], spec["h"], spec["d"], spec["wall"], name=name)
    if op == "frame":
        return M.rect_frame(spec["w"], spec["d"], spec["t"], spec["hole_w"], spec["hole_d"], name=name)
    if op == "cylinder":
        return M.cylinder(spec["r"], spec["h"], name=name)
    if op == "standoff_plate":
        m = M.standoff_plate(spec["w"], spec["d"], spec["plate_t"], spec["post_h"], spec["post_r"], spec["inset"])
        lo, hi = m.bounds()  # re-centre vertically
        m = m.translated(0, -(lo[1] + hi[1]) / 2, 0)
        m.name = name
        return m
    raise ValueError(f"unknown CAD op {op!r}")


class CadGenerator(Protocol):
    def generate(self, node: Node, instruction: str | None, parameters: dict) -> M.Mesh: ...


@dataclass
class ParametricGenerator:
    content: ProjectContent | None = None

    def generate(self, node: Node, instruction: str | None, parameters: dict) -> M.Mesh:
        spec = {**feature_spec(node, self.content), **(parameters or {})}
        return build_mesh(spec, name=node.id)


# ------------------------------------------------------------------------------ eligibility & credits
def cad_eligible(content: ProjectContent) -> list[Node]:
    """Parts that get CAD (same rule as the reference 'CAD n/N' counter), de-duplicated for billing."""
    return [n for n in content.nodes if is_layout_part(n)]


def billing_key(node: Node) -> str:
    """Identical parts are billed once: partId > productName > name > id."""
    return node.part_id or node.product_name or node.name or node.id


@dataclass
class Wallet:
    plan: str = "basic"
    balance: int = 0
    ledger: list[dict] = field(default_factory=list)

    @property
    def unlimited(self) -> bool:
        return PLAN_MONTHLY_CREDITS.get(self.plan, 0) is None

    def grant_monthly(self) -> None:
        n = PLAN_MONTHLY_CREDITS.get(self.plan, 0)
        if n:
            self._post(n, "monthly_grant")

    def buy(self, bundle: str) -> None:
        credits, usd = CREDIT_BUNDLES[bundle]
        self._post(credits, f"purchase:{bundle}:${usd:.0f}")

    def charge(self, keys: list[str], already_built: set[str]) -> int:
        """Charge 1 credit per *distinct* billing key not built before; raises if insufficient."""
        new = sorted(set(keys) - already_built)
        if self.unlimited:
            return 0
        if len(new) > self.balance:
            raise InsufficientCredits(f"need {len(new)} credits, have {self.balance}")
        if new:
            self._post(-len(new), "cad:" + ",".join(new))
        return len(new)

    def _post(self, delta: int, memo: str) -> None:
        self.balance += delta
        self.ledger.append({"delta": delta, "memo": memo, "balance": self.balance, "ts": time.time()})


class InsufficientCredits(RuntimeError):
    pass


# ------------------------------------------------------------------------------ jobs
@dataclass
class CadJob:
    id: str
    project_id: str
    node_id: str
    mode: str = "create"  # create | edit
    instruction: str | None = None
    parameters: dict = field(default_factory=dict)
    status: str = "queued"
    error: str | None = None
    artifacts: dict[str, bytes] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def public(self) -> dict:
        return {
            "id": self.id, "projectId": self.project_id, "nodeId": self.node_id, "mode": self.mode, "status": self.status,
            "error": self.error, "artifacts": sorted(self.artifacts), "stats": self.stats, "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }  # fmt: skip


class CadJobQueue:
    """In-process job queue. ``run_async=False`` executes inline (tests/CLI); True uses a worker thread."""

    def __init__(
        self, generator_factory: Callable[[ProjectContent], CadGenerator] | None = None, run_async: bool = False
    ):
        self.jobs: dict[str, CadJob] = {}
        self._factory = generator_factory or (lambda content: ParametricGenerator(content))
        self._async = run_async
        self._lock = threading.Lock()

    def submit(self, project_id: str, content: ProjectContent, node_id: str, mode: str = "create",
               instruction: str | None = None, parameters: dict | None = None) -> CadJob:  # fmt: skip
        node = content.node(node_id)
        if node is None:
            raise KeyError(f"unknown node {node_id}")
        if not is_layout_part(node):
            raise ValueError(f"{node_id} is not CAD-eligible (fasteners/misc parts are bought, not modelled)")
        job = CadJob(id=uuid.uuid4().hex, project_id=project_id, node_id=node_id, mode=mode, instruction=instruction,
                     parameters=parameters or {})  # fmt: skip
        with self._lock:
            self.jobs[job.id] = job
        if self._async:
            threading.Thread(target=self._run, args=(job, content), daemon=True).start()
        else:
            self._run(job, content)
        return job

    def cancel(self, job_id: str) -> CadJob:
        job = self.jobs[job_id]
        if job.status in ("queued", "running"):
            job.status, job.updated_at = "canceled", time.time()
        return job

    def _run(self, job: CadJob, content: ProjectContent) -> None:
        if job.status == "canceled":
            return
        job.status, job.updated_at = "running", time.time()
        try:
            node = content.node(job.node_id)
            mesh = self._factory(content).generate(node, job.instruction, job.parameters)
            if not mesh.is_watertight():
                raise ValueError("generated mesh is not watertight")
            if job.status == "canceled":
                return
            color = _rgba(TYPE_META.get(canonical_type(node.type), TYPE_META["other"])[1])
            job.artifacts = {
                "model.stl": M.to_stl_binary(mesh),
                "model.obj": M.to_obj([mesh]).encode(),
                "model.glb": M.to_glb([M.SceneNode(node.id, mesh, (0.0, 0.0, 0.0), color)]),
            }
            vol, area = mesh.volume(), mesh.surface_area()
            job.stats = {"triangles": len(mesh.triangles), "volumeMm3": round(vol, 1), "areaMm2": round(area, 1),
                         "size": [round(v, 2) for v in mesh.size()], "sha256": hashlib.sha256(job.artifacts["model.stl"]).hexdigest()}  # fmt: skip
            if canonical_type(node.type) in ("3d_printed", "enclosure"):
                job.stats["print"] = print_cost(vol, area, node.material, node.print_settings)
            job.status = "succeeded"
        except Exception as e:  # noqa: BLE001 - job errors are reported, not raised
            job.status, job.error = "failed", f"{type(e).__name__}: {e}"
        job.updated_at = time.time()


def _rgba(hex_color: str, alpha: float = 1.0) -> tuple[float, float, float, float]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255, alpha


# ------------------------------------------------------------------------------ assembly
def assembly_meshes(content: ProjectContent, layout: dict, generator: CadGenerator | None = None) -> list[M.SceneNode]:
    """One SceneNode per placed part, positioned by the solved layout (electrical parts translucent)."""
    gen = generator or ParametricGenerator(content)
    out = []
    for nid, info in layout.get("parts", {}).items():
        node = content.node(nid)
        if node is None:
            continue
        if canonical_type(node.type) in ("3d_printed", "enclosure", "structural", "mechanism"):
            mesh = gen.generate(node, None, {})
            alpha = 0.55
        else:
            w, h, d = info["size"]
            mesh = M.cylinder(min(w, d) / 2, h, name=nid) if info["shape"] == "cylinder" else M.box(w, h, d, name=nid)
            alpha = 0.85
        out.append(M.SceneNode(nid, mesh, tuple(info["pos"]), _rgba(info["color"], alpha)))
    return out


def export_assembly(content: ProjectContent, layout: dict) -> dict[str, bytes]:
    nodes = assembly_meshes(content, layout)
    placed = [n.mesh.translated(*n.translation) for n in nodes]
    for m, n in zip(placed, nodes):
        m.name = n.name
    return {"assembly.glb": M.to_glb(nodes), "assembly.obj": M.to_obj(placed).encode()}


def part_mesh_json(content: ProjectContent, layout: dict) -> dict:
    """Compact per-part triangle data for the HTML viewer (positions rounded to 0.01 mm)."""
    out = {}
    for sn in assembly_meshes(content, layout):
        out[sn.name] = {
            "pos": list(sn.translation),
            "color": list(sn.color),
            "v": [round(c, 2) for v in sn.mesh.vertices for c in v],
            "f": [i for t in sn.mesh.triangles for i in t],
        }
    return out
