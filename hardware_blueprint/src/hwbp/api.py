"""HTTP protocol server (FastAPI) — mirrors the observed blueprint.io API surface.

Generation uses the reference transport: ``POST /api/generation-runs`` then long-poll
``GET /api/generation-runs/{id}?waitMs=&sinceRevision=`` (an SSE variant is provided too). Project saves use
optimistic concurrency (``baseVersion`` -> 409 ``version_conflict``). Everything is in-memory; swap
``ProjectStore`` / ``RunStore`` for a database in production. Run: ``uvicorn hwbp.api:app``.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from dataclasses import dataclass, field

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import StreamingResponse

from hwbp import __version__
from hwbp import mesh as M
from hwbp.cad import CadJobQueue, InsufficientCredits, Wallet, billing_key, cad_eligible
from hwbp.classify import classify_message, classify_part
from hwbp.edits import ProjectStore, VersionConflict, apply_ops, apply_plan, message_to_ops, validate_plan
from hwbp.engine import Engine
from hwbp.enrichment import canonicalize, enrich_node, research_queries
from hwbp.export import export_zip, wiring_svg
from hwbp.image import VisionModel, brief_from_image, image_to_mesh
from hwbp.mech import solve_layout
from hwbp.models import Node, ProjectContent
from hwbp.pricing import build_bom
from hwbp.requirements import Requirements
from hwbp.selection import suggest
from hwbp.services import (
    TURN_TTL_S,
    Accounts,
    ChatLog,
    Collaboration,
    Community,
    InputImages,
    Notes,
    ServiceError,
    Turn,
    diff_plan,
)
from hwbp.wiring import infer_rails

SECTIONS_BY_PHASE = {
    "planning": [],
    "questions": [],
    "electrical": ["parts", "wiring"],
    "mechanical": ["parts", "wiring", "mech"],
    "revealing": ["parts", "wiring", "mech", "info"],
    "guide": ["parts", "wiring", "mech", "info", "guide"],
    "done": ["parts", "wiring", "mech", "info", "guide"],
}


@dataclass
class Run:
    id: str
    prompt: str
    idempotency_key: str | None
    status: str = "queued"  # queued | running | waiting_for_input | completed | failed | cancelled
    phase: str = "planning"
    revision: int = 0
    project_id: str | None = None
    project: dict | None = None
    messages: list[str] = field(default_factory=list)
    questions: list[dict] = field(default_factory=list)
    failure: str | None = None
    form_factor: dict | None = None
    owner: str = "local-user"
    image_analysis: dict | None = None
    answers: dict | None = None
    created_at: float = field(default_factory=time.time)
    cond: threading.Condition = field(default_factory=threading.Condition)

    def public(self) -> dict:
        return {
            "id": self.id, "status": self.status, "phase": self.phase, "revision": self.revision, "projectId": self.project_id,
            "sections": {s: s in SECTIONS_BY_PHASE.get(self.phase, []) for s in ("parts", "wiring", "mech", "info", "guide")},
            "messages": self.messages, "questionRounds": [{"questions": self.questions}] if self.questions else [],
            "failure": self.failure, "projectData": self.project, "imageAnalysis": self.image_analysis,
            "createdAt": self.created_at,
        }  # fmt: skip


class State:
    def __init__(self, engine: Engine | None = None) -> None:
        self.engine = engine or Engine()
        self.projects = ProjectStore()
        self.runs: dict[str, Run] = {}
        self.idem: dict[str, str] = {}
        self.published: dict[str, dict] = {}
        self.cad = CadJobQueue(run_async=False)
        self.wallet = Wallet(plan="pro")
        self.wallet.grant_monthly()
        self.built: set[str] = set()
        self.accounts = Accounts(default_plan_for={DEFAULT_USER: "pro"})
        self.collab = Collaboration()
        self.community = Community()
        self.notes = Notes()
        self.images = InputImages()
        self.chat = ChatLog()
        self.image_variant: dict[str, int] = {}
        self.cad_revision: dict[str, int] = {}
        self.providers: list | None = None  # SourcingProvider list for /api/parts/nexar-sourcing (None = catalog)


DEFAULT_USER = "local-user"


def current_user(x_user_id: str | None = Header(None)) -> str:
    """Identity from the ``X-User-Id`` header (plug real auth here). Defaults to a single local user."""
    return (x_user_id or DEFAULT_USER).strip() or DEFAULT_USER


def create_app(engine: Engine | None = None, run_async: bool = True, vision: VisionModel | None = None) -> FastAPI:
    app = FastAPI(title="hwbp protocol server", version=__version__)
    st = State(engine)
    app.state.hwbp = st
    from hwbp.api_ext import register

    register(app, st, run_async=run_async, vision=vision)  # first: specific paths win over /api/parts/{id}

    # ------------------------------------------------------------------ meta
    @app.get("/api/features")
    def features() -> dict:
        return {
            "proTrial": {"enabled": False, "days": 0},
            "projectCollabSaleEnabled": False,
            "engineVersion": __version__,
        }

    @app.get("/api/capabilities")
    def capabilities() -> dict:
        return {"imageToMesh": True, "imageToBlueprint": True, "cad": True, "export": True, "clearChatHistory": True}

    # ------------------------------------------------------------------ generation runs (long-poll)
    @app.post("/api/generation-runs")
    def start_run(body: dict = Body(...), user: str = Depends(current_user)) -> dict:
        key = body.get("idempotencyKey")
        if key and key in st.idem:
            return st.runs[st.idem[key]].public()
        prompt = (body.get("prompt") or "").strip()
        form_factor, analysis = None, None
        for img in body.get("inputImages") or []:
            if isinstance(img, str):  # id from POST /api/generation-input-images
                try:
                    stored = st.images.get(img)
                except ServiceError as e:
                    raise HTTPException(e.status, {"error": e.code, "message": e.message}) from e
                data, mime = stored["bytes"], stored["mimeType"]
            else:
                data, mime = base64.b64decode(img.get("data") or ""), img.get("mime", "image/png")
            try:
                brief = brief_from_image(data, mime, user_prompt=prompt or None, vision=vision)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            prompt = brief.prompt
            analysis = dict(brief.analysis)
            if brief.silhouette:
                form_factor = {"widthMm": brief.silhouette.width_mm, "depthMm": brief.silhouette.depth_mm,
                               "aspect": brief.silhouette.aspect, "fillRatio": brief.silhouette.fill_ratio}  # fmt: skip
                analysis["silhouette"] = form_factor
        if not prompt:
            raise HTTPException(400, "prompt or inputImages required")
        try:
            st.accounts.consume_credit(user)
        except ServiceError as e:
            raise HTTPException(e.status, {"error": e.code, "message": e.message}) from e
        run = Run(id=uuid.uuid4().hex, prompt=prompt, idempotency_key=key, form_factor=form_factor, owner=user,
                  image_analysis=analysis, answers=body.get("answers"))  # fmt: skip
        st.runs[run.id] = run
        st.community.counters["projectsCreated"] += 1
        if key:
            st.idem[key] = run.id
        answers = body.get("answers")
        if run_async:
            threading.Thread(target=_execute, args=(st, run, answers), daemon=True).start()
        else:
            _execute(st, run, answers)
        return run.public()

    @app.get("/api/generation-runs/{run_id}")
    def poll_run(run_id: str, waitMs: int = 0, sinceRevision: int = -1) -> dict:  # noqa: N803 - wire names
        run = _run(st, run_id)
        deadline = time.time() + min(max(waitMs, 0), 30000) / 1000
        with run.cond:
            while run.revision <= sinceRevision and run.status in ("queued", "running") and time.time() < deadline:
                run.cond.wait(timeout=max(0.0, deadline - time.time()))
        return run.public()

    @app.get("/api/generation-runs/{run_id}/events")
    def run_events(run_id: str) -> StreamingResponse:
        run = _run(st, run_id)

        def stream():
            seen = -1
            while True:
                with run.cond:
                    if run.revision <= seen and run.status in ("queued", "running"):
                        run.cond.wait(timeout=15)
                    snap = run.public()
                if snap["revision"] > seen:
                    seen = snap["revision"]
                    light = {k: v for k, v in snap.items() if k != "projectData"}
                    yield f"event: progress\ndata: {json.dumps(light)}\n\n"
                if snap["status"] not in ("queued", "running"):
                    yield f"event: done\ndata: {json.dumps({'status': snap['status'], 'projectId': snap['projectId']})}\n\n"
                    return
                yield "event: ping\ndata: {}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/api/generation-runs/{run_id}/cancel")
    def cancel_run(run_id: str) -> dict:
        run = _run(st, run_id)
        with run.cond:
            if run.status in ("queued", "running", "waiting_for_input"):
                run.status = "cancelled"
                run.revision += 1
                run.cond.notify_all()
        return run.public()

    # ------------------------------------------------------------------ projects
    @app.get("/api/projects/{pid}")
    def get_project(pid: str) -> dict:
        p = _project(st, pid)
        return {"id": p.id, "version": p.revision, "content": p.content.dump(), "updatedAt": p.updated_at}

    @app.get("/api/projects/{pid}/head")
    def head(pid: str) -> dict:
        p = _project(st, pid)
        active = next(
            (r.id for r in st.runs.values() if r.project_id == pid and r.status in ("queued", "running")), None
        )
        return {"version": p.revision, "updatedAt": p.updated_at, "activeRun": active, "historyEpoch": st.chat.epoch.get(pid, 0),
                "cadRevision": st.cad_revision.get(pid, 0), "author": p.owner_id}  # fmt: skip

    @app.post("/api/projects/save")
    def save(body: dict = Body(...), user: str = Depends(current_user)) -> dict:
        pid = body.get("projectId")
        if pid in st.projects:
            _require(st, pid, user, "editor")
        content = ProjectContent.model_validate(body.get("content") or {})
        try:
            p = st.projects.save(pid, content, int(body.get("baseVersion", -1)))
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except VersionConflict as e:
            raise HTTPException(409, {"error": "version_conflict", "currentVersion": e.current}) from e
        return {"version": p.revision}

    @app.get("/api/projects/{pid}/versions")
    def versions(pid: str) -> dict:
        _project(st, pid)
        return {"versions": st.projects.versions(pid)}

    @app.post("/api/projects/{pid}/restore/{version}")
    def restore(pid: str, version: int) -> dict:
        p = st.projects.restore(pid, version)
        return {"version": p.revision}

    # ------------------------------------------------------------------ chat / edits
    @app.post("/api/projects/{pid}/chat/classify")
    def chat_classify(pid: str, body: dict = Body(...)) -> dict:
        p = _project(st, pid)
        c = classify_message(body.get("message", ""), p.content)
        return {
            "mode": c.mode,
            "intent": c.intent,
            "targets": c.targets,
            "confidence": c.confidence,
            "decisionReceipt": c.decision_receipt,
        }

    @app.post("/api/projects/{pid}/chat")
    def chat(pid: str, body: dict = Body(...), user: str = Depends(current_user)) -> dict:
        """Ask -> answer turn. Edit -> applied immediately (``autoApprove``, default) or an ``awaiting_approval`` turn
        carrying a reference-format plan preview (``autoApprove: false``), approved via /chat/{turn}/approve."""
        p = _project(st, pid)
        _require(st, pid, user, "viewer")
        message = body.get("message", "")
        base = int(body.get("baseVersion", p.revision))
        mode, ops = message_to_ops(message, p.content, st.engine.catalog)
        turn = Turn(
            id=uuid.uuid4().hex[:12],
            prompt=message,
            mode=mode,
            actor=user,
            status="running",
            base_version=base,
            created_at=time.time(),
        )
        if mode == "ask" or not ops:
            turn.status, turn.result_kind = "completed", "answer" if mode == "ask" else "clarification"
            turn.response_text = (
                _answer(p.content, message)
                if mode == "ask"
                else "I could not map that request to a design change — try naming a part or feature."
            )
            st.chat.add(pid, turn)
            return {
                "mode": mode,
                "applied": False,
                "ops": [],
                "answer": turn.response_text,
                "version": p.revision,
                "turn": turn.public(),
            }
        _require(st, pid, user, "editor")
        content = _plan_preview(st, p.content, ops, turn)
        if body.get("autoApprove", True) is False:
            st.chat.add(pid, turn)
            return {"mode": "edit", "applied": False, "ops": turn.ops, "version": p.revision, "turn": turn.public()}
        try:
            saved = st.projects.save(pid, content, base)
        except VersionConflict as e:
            turn.status, turn.failure_code = "outdated", "version_conflict"
            st.chat.add(pid, turn)
            raise HTTPException(409, {"error": "version_conflict", "currentVersion": e.current}) from e
        turn.status, turn.result_kind, turn.approver, turn.preview = "completed", "edit", user, None
        st.chat.add(pid, turn)
        return {"mode": "edit", "applied": True, "ops": turn.ops, "version": saved.revision, "turn": turn.public()}

    @app.post("/api/projects/{pid}/edit-plan")
    def edit_plan(pid: str, body: dict = Body(...), user: str = Depends(current_user)) -> dict:
        p = _project(st, pid)
        _require(st, pid, user, "editor")
        plan, rep = validate_plan(p.content, body.get("plan") or {}, st.engine.catalog)
        if body.get("dryRun"):
            return {"plan": plan, "repaired": rep.repaired, "dropped": rep.dropped}
        new = apply_plan(p.content, plan)
        new.mech_layout = solve_layout(new)
        try:
            saved = st.projects.save(pid, new, int(body.get("baseVersion", p.revision)))
        except VersionConflict as e:
            raise HTTPException(409, {"error": "version_conflict", "currentVersion": e.current}) from e
        return {"version": saved.revision, "repaired": rep.repaired, "dropped": rep.dropped}

    @app.get("/api/projects/{pid}/suggestions")
    def suggestions(pid: str, nodeId: str | None = None) -> dict:  # noqa: N803
        p = _project(st, pid)
        req = Requirements.model_validate(p.content.requirements or {"prompt": p.content.original_prompt})
        design = st.engine.design_for(req)
        if nodeId and not design.has(nodeId):
            raise HTTPException(404, f"unknown node {nodeId}")
        return {"suggestions": [s.dump() for s in suggest(design, st.engine.catalog, nodeId)]}

    # ------------------------------------------------------------------ parts
    @app.get("/api/parts/search")
    def parts_search(q: str, type: str | None = None, limit: int = 10) -> dict:  # noqa: A002
        return {
            "results": [
                {"id": p.id, "name": p.name, "type": p.type, "score": s}
                for p, s in st.engine.catalog.search(q, type, limit)
            ]
        }

    @app.get("/api/parts/{part_id}")
    def part(part_id: str) -> dict:
        try:
            return st.engine.catalog.get(part_id).model_dump()
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    @app.post("/api/parts/canonicalize")
    def parts_canonicalize(body: dict = Body(...)) -> dict:
        c = canonicalize(body, st.engine.catalog)
        return {
            "catalogId": c.part.id,
            "matchedBy": c.matched_by,
            "confidence": c.confidence,
            "part": c.part.model_dump(),
        }

    @app.post("/api/parts/enrich-transient")
    def parts_enrich(body: dict = Body(...)) -> dict:
        raw = dict(body.get("node") or body)
        raw.setdefault("id", "_".join((raw.get("name") or "part").lower().split()))
        raw.setdefault("type", "other")
        node = Node.model_validate(raw)
        c = canonicalize(raw, st.engine.catalog)
        cls = classify_part(node.name, node.description or "", bool(node.pins))
        enrich_node(node, c.part)
        return {"node": node.dump(), "classification": cls.__dict__, "researchQueries": research_queries(node).queries}

    @app.post("/api/wiring/infer-rails")
    def wiring_infer_rails(body: dict = Body(...)) -> dict:
        return {
            "addElectricalConnections": [
                c.dump() for c in infer_rails(body.get("electricalNodes") or [], body.get("connections") or [])
            ]
        }

    # ------------------------------------------------------------------ BOM / wiring / export
    @app.get("/api/projects/{pid}/bom")
    def bom(pid: str, buildQty: int = Query(1, ge=1, le=100000)) -> dict:  # noqa: N803
        b = build_bom(_project(st, pid).content.nodes, buildQty)
        return {"lines": [ln.__dict__ for ln in b.lines], "rollup": b.rollup(), "partsTotalUsd": b.parts_total_usd,
                "shippingUsd": b.shipping_usd, "totalUsd": b.total_usd, "buildQty": buildQty}  # fmt: skip

    @app.get("/api/projects/{pid}/bom.csv")
    def bom_csv(pid: str, buildQty: int = 1) -> Response:  # noqa: N803
        return Response(build_bom(_project(st, pid).content.nodes, buildQty).to_csv(), media_type="text/csv")

    @app.get("/api/projects/{pid}/wiring.svg")
    def wiring(pid: str, dark: bool = False) -> Response:
        return Response(wiring_svg(_project(st, pid).content, dark), media_type="image/svg+xml")

    @app.get("/api/projects/{pid}/export.zip")
    def export(pid: str, buildQty: int = 1) -> Response:  # noqa: N803
        data = export_zip(_project(st, pid).content, buildQty)
        return Response(
            data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{pid}.zip"'}
        )

    # ------------------------------------------------------------------ CAD
    @app.post("/api/cad-jobs/part")
    def cad_part(body: dict = Body(...)) -> dict:
        p = _project(st, body.get("projectId", ""))
        node = p.content.node(body.get("nodeId", ""))
        if node is None:
            raise HTTPException(404, "unknown node")
        try:
            st.wallet.charge([billing_key(node)], st.built)
        except InsufficientCredits as e:
            raise HTTPException(402, {"error": "insufficient_credits", "message": str(e)}) from e
        try:
            job = st.cad.submit(
                p.id, p.content, node.id, body.get("mode", "create"), body.get("instruction"), body.get("parameters")
            )
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        if job.status == "succeeded":
            st.built.add(billing_key(node))
            st.cad_revision[p.id] = st.cad_revision.get(p.id, 0) + 1
        return job.public()

    @app.get("/api/cad-jobs/{job_id}")
    def cad_job(job_id: str) -> dict:
        if job_id not in st.cad.jobs:
            raise HTTPException(404, "unknown job")
        return st.cad.jobs[job_id].public()

    @app.get("/api/cad-jobs/{job_id}/artifacts/{name}")
    def cad_artifact(job_id: str, name: str) -> Response:
        job = st.cad.jobs.get(job_id)
        if job is None or name not in job.artifacts:
            raise HTTPException(404, "unknown artifact")
        media = {
            "model.stl": "model/stl",
            "model.glb": "model/gltf-binary",
            "model.obj": "text/plain",
            "model.step": "model/step",
        }.get(name, "application/octet-stream")
        return Response(job.artifacts[name], media_type=media)

    @app.get("/api/cad-wallet")
    def cad_wallet(projectId: str | None = None) -> dict:  # noqa: N803
        out = {"balance": st.wallet.balance, "unlimited": st.wallet.unlimited}
        if projectId and projectId in st.projects:
            c = st.projects.get(projectId).content
            out["eligible"] = len(cad_eligible(c))
            out["billable"] = len({billing_key(n) for n in cad_eligible(c)} - st.built)
        return out

    # ------------------------------------------------------------------ picture -> mesh
    @app.post("/api/image-to-mesh")
    def img_to_mesh(body: dict = Body(...)) -> dict:
        data = base64.b64decode(body.get("image") or "")
        try:
            mesh, report = image_to_mesh(
                data, body.get("mode", "extrude"), float(body.get("widthMm", 100)), float(body.get("heightMm", 10))
            )
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        fmt = body.get("format", "stl")
        blob = (
            M.to_stl_binary(mesh)
            if fmt == "stl"
            else M.to_glb([M.SceneNode("part", mesh)])
            if fmt == "glb"
            else M.to_obj([mesh]).encode()
        )
        return {"report": report, "format": fmt, "data": base64.b64encode(blob).decode()}

    # ------------------------------------------------------------------ publish / share
    @app.post("/api/publish")
    def publish(body: dict = Body(...), user: str = Depends(current_user)) -> dict:
        p = _project(st, body.get("projectId", ""))
        _require(st, p.id, user, "owner")
        e = st.community.publish(p.id, user, p.content, p.revision, image_url=f"/api/projects/{p.id}/image.svg")
        return {"token": e["token"], "url": f"/s/{e['token']}"}

    @app.get("/api/published")
    def published(token: str) -> dict:
        try:
            return st.community.get(token)
        except ServiceError as e:
            raise HTTPException(404, "not found") from e

    @app.get("/api/published/head")
    def published_head(token: str) -> dict:
        d = published(token)
        return {
            "publishedProjectId": d["publishedProjectId"],
            "contentRevision": d["contentRevision"],
            "starCount": d["starCount"],
        }

    return app


def _execute(st: State, run: Run, answers: dict | None) -> None:
    try:
        for ev in st.engine.run(run.prompt, answers=answers, form_factor=run.form_factor):
            with run.cond:
                if run.status == "cancelled":
                    return
                run.status = "running" if ev["status"] != "completed" else "completed"
                run.phase = ev["phase"]
                run.revision += 1
                run.messages.append(ev["message"])
                if ev.get("questions"):
                    run.questions = ev["questions"]
                content: ProjectContent | None = ev.get("projectData")
                if content is not None:
                    run.project = content.dump()
                    if ev["phase"] == "done":
                        pid = run.project_id or uuid.uuid4().hex[:12]
                        if pid in st.projects:  # re-run (questions / retry): new version of the same project
                            st.projects.save(pid, content, st.projects.get(pid).revision)
                        else:
                            st.projects.create(pid, content, owner_id=run.owner)
                            st.collab.set_owner(pid, run.owner)
                        run.project_id = pid
                run.cond.notify_all()
    except Exception as e:  # noqa: BLE001 - surfaced to the client as run.failure
        with run.cond:
            run.status, run.failure = "failed", f"{type(e).__name__}: {e}"
            run.revision += 1
            run.cond.notify_all()


def _run(st: State, run_id: str) -> Run:
    if run_id not in st.runs:
        raise HTTPException(404, "unknown run")
    return st.runs[run_id]


def _project(st: State, pid: str):
    try:
        return st.projects.get(pid)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


def _answer(content: ProjectContent, question: str) -> str:
    """Deterministic answers from the design document (an LLM can replace this)."""
    q = question.lower()
    for n in content.nodes:
        if n.name.lower() in q or n.id.replace("_", " ") in q or (n.product_name and n.product_name.lower() in q):
            why = "; ".join(n.rationale or []) or "selected by the engine"
            return f"{n.name} ({n.product_name or n.type}): {why}."
    if "battery" in q or "runtime" in q:
        return f"Estimated runtime: {(content.power or {}).get('runtimeHours')} h."
    if "cost" in q or "price" in q:
        return f"Estimated parts cost: ${(content.bom or {}).get('estimateTotalUsd')}."
    return content.project_description or "No answer available."


def _require(st: State, pid: str, user: str, role: str) -> str:
    try:
        return st.collab.require(pid, user, role)
    except ServiceError as e:
        raise HTTPException(e.status, {"error": e.code, "message": e.message}) from e


def _plan_preview(st: State, content: ProjectContent, ops: list, turn: "Turn") -> ProjectContent:
    """Regenerate with the requested overrides and attach a reference-format plan + 15-minute approval deadline."""
    req = apply_ops(Requirements.model_validate(content.requirements or {"prompt": content.original_prompt}), ops)
    new = st.engine.generate(requirements=req)
    turn.ops = [o.__dict__ for o in ops]
    turn.plan = diff_plan(content, new)
    turn.preview = new
    turn.status = "awaiting_approval"
    turn.deadline = time.time() + TURN_TTL_S
    return new


app = create_app()
