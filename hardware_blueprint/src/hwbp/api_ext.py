"""Extended protocol routes: everything beyond generation/projects/parts core — run follow-ups, input images, chat
turns, WebSocket events, notes, collaboration, community, part links/images, AI-action equivalents, mechanical tools,
exports, accounts/credits, and the served web pages (studio, project page, share page)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from urllib.parse import quote_plus

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from hwbp.cad import CREDIT_BUNDLES, cad_eligible
from hwbp.enrichment import CatalogSourcing, enrich_node
from hwbp.export import FILE_KINDS, export_cad, export_file, export_zip, render_viewer
from hwbp.ideas import apply_descriptions, generate_ideas, refresh_part_descriptions, substep_detail, suggested_prompts
from hwbp.image import FUNCTION_QUESTION_ID, analyze_picture, compose_prompt, function_options
from hwbp.layout import enriched_wiring_layout, render_svg
from hwbp.mech import apply_joints, derive_joints, enrich_mechanical, explode_layout, heal_mechanical, solve_layout
from hwbp.models import Node, ProjectContent
from hwbp.pricing import build_bom, merge_by_product
from hwbp.render import part_icon, project_image, step_image
from hwbp.services import ServiceError


def _svc(fn, *a, **k):
    try:
        return fn(*a, **k)
    except ServiceError as e:
        raise HTTPException(e.status, {"error": e.code, "message": e.message}) from e


def register(app: FastAPI, st, run_async: bool = True, vision=None) -> None:  # noqa: C901 - route table
    from hwbp.api import DEFAULT_USER, Run, _execute, _project, _require, current_user

    def save_content(pid: str, content: ProjectContent) -> int:
        return st.projects.save(pid, content, st.projects.get(pid).revision).revision

    def with_author(entry: dict) -> dict:
        prof = st.accounts.get(entry.get("ownerId") or DEFAULT_USER)
        return {**entry, "author": {"username": prof.username or prof.user_id, "profileColor": prof.profile_color}}

    # ================================================================== generation runs: follow-ups & images
    @app.post("/api/generation-input-images")
    def upload_image(body: dict = Body(...)) -> dict:
        return _svc(st.images.add, body.get("image") or "", body.get("mime"), body.get("draftKey"))

    @app.get("/api/input-images/{image_id}")
    def get_image(image_id: str) -> Response:
        item = _svc(st.images.get, image_id)
        return Response(item["bytes"], media_type=item["mimeType"])

    @app.get("/api/generation-runs")
    def list_runs(recoverable: bool = False, user: str = Depends(current_user)) -> dict:
        runs = [r for r in st.runs.values() if r.owner == user]
        if recoverable:
            runs = [r for r in runs if r.status not in ("completed", "cancelled")]
        runs.sort(key=lambda r: -r.created_at)
        return {"runs": [{k: v for k, v in r.public().items() if k != "projectData"} for r in runs]}

    def rerun(run: Run, answers: dict | None) -> dict:
        with run.cond:
            run.status, run.phase, run.failure = "queued", "planning", None
            run.answers = answers if answers is not None else run.answers
            run.revision += 1
            run.cond.notify_all()
        if run_async:
            threading.Thread(target=_execute, args=(st, run, run.answers), daemon=True).start()
        else:
            _execute(st, run, run.answers)
        return run.public()

    def _run_or_404(run_id: str) -> Run:
        if run_id not in st.runs:
            raise HTTPException(404, "unknown run")
        return st.runs[run_id]

    @app.post("/api/generation-runs/{run_id}/questions")
    def answer_questions(run_id: str, body: dict = Body(...)) -> dict:
        run = _run_or_404(run_id)
        if int(body.get("revision", run.revision)) != run.revision:
            raise HTTPException(409, {"error": "stale_revision", "currentRevision": run.revision})
        by_text = {q["question"]: q["id"] for q in run.questions}
        answers = dict(run.answers or {})
        for qa in body.get("qa") or []:
            qid = qa.get("id") or by_text.get(qa.get("question", ""))
            if qid:
                answers[qid] = str(qa.get("answer"))
        if run.status == "waiting_for_input" and not (answers.get(FUNCTION_QUESTION_ID) or "").strip():
            raise HTTPException(422, {"error": "answer_required", "message": "Say what the device should do."})
        if (answers.get(FUNCTION_QUESTION_ID) or "").strip() and (
            run.status == "waiting_for_input" or not run.project_id
        ):
            run.prompt = compose_prompt(answers[FUNCTION_QUESTION_ID], run.detected)
            run.questions = [q for q in run.questions if q.get("id") != FUNCTION_QUESTION_ID]
        return rerun(run, answers)

    @app.post("/api/generation-runs/{run_id}/retry")
    def retry_run(run_id: str, body: dict = Body(default={})) -> dict:
        run = _run_or_404(run_id)
        if body.get("revision") is not None and int(body["revision"]) != run.revision:
            raise HTTPException(409, {"error": "stale_revision", "currentRevision": run.revision})
        if not run.prompt:  # picture-only run still waiting for "what should it do?"
            raise HTTPException(422, {"error": "answer_required", "message": "Say what the device should do."})
        return rerun(run, None)

    @app.post("/api/input-images/{image_id}/analysis")
    def image_analysis(image_id: str) -> dict:
        """Offline picture analysis shown before generating (what the picture says, and what it cannot say)."""
        stored = _svc(st.images.get, image_id)
        try:
            det = analyze_picture(stored["bytes"])
        except ValueError as e:
            raise HTTPException(422, {"error": "unreadable_image", "message": str(e)}) from e
        return {"detected": det, "functionOptions": function_options(det), "visionModel": vision is not None}

    # ================================================================== projects
    @app.get("/api/projects/{pid}/meta")
    def project_meta(pid: str, user: str = Depends(current_user)) -> dict:
        """What the project page needs about the viewer: role, author, publication state."""
        p = _project(st, pid)
        owner = st.accounts.get(p.owner_id or DEFAULT_USER)
        pub = next((e for e in st.community.published.values() if e["publishedProjectId"] == pid), None)
        return {"role": st.collab.role(pid, user), "version": p.revision,
                "author": {"username": owner.username or owner.user_id, "profileColor": owner.profile_color},
                "published": {"token": pub["token"], "url": f"/s/{pub['token']}", "starCount": pub["starCount"]}
                if pub else None}  # fmt: skip

    @app.get("/api/projects/{pid}/cad-jobs")
    def project_cad_jobs(pid: str) -> dict:
        """CAD-eligible parts and the latest job per part (the Mech sidebar's 'CAD n/N' column)."""
        c = _project(st, pid).content
        latest: dict[str, dict] = {}
        for job in sorted(st.cad.jobs.values(), key=lambda j: j.created_at):
            if job.project_id == pid:
                latest[job.node_id] = job.public()
        eligible = [n.id for n in cad_eligible(c)]
        done = sum(1 for nid in eligible if latest.get(nid, {}).get("status") == "succeeded")
        return {"eligible": eligible, "done": done, "jobs": latest,
                "wallet": {"balance": st.wallet.balance, "unlimited": st.wallet.unlimited}}  # fmt: skip

    @app.get("/api/projects")
    def my_projects(user: str = Depends(current_user)) -> dict:
        out = []
        for pid, role in st.collab.projects_of(user):
            if pid in st.projects:
                p = st.projects.get(pid)
                out.append(
                    {
                        "id": pid,
                        "name": p.content.project_name,
                        "version": p.revision,
                        "updatedAt": p.updated_at,
                        "role": role,
                    }
                )
        out.sort(key=lambda r: r["updatedAt"] or "", reverse=True)
        return {"projects": out}

    @app.post("/api/projects/copy")
    def copy_project(body: dict = Body(...), user: str = Depends(current_user)) -> dict:
        entry = _svc(st.community.get, body.get("token", ""))
        content = ProjectContent.model_validate(entry["content"])
        content.project_name = f"{content.project_name} (copy)"
        pid = uuid.uuid4().hex[:12]
        st.projects.create(pid, content, owner_id=user)
        st.collab.set_owner(pid, user)
        return {"projectId": pid, "version": 1}

    # ---- chat turns
    @app.get("/api/projects/{pid}/chat")
    def chat_snapshot(pid: str, user: str = Depends(current_user)) -> dict:
        p = _project(st, pid)
        _require(st, pid, user, "viewer")
        return st.chat.snapshot(pid, p.revision)

    @app.post("/api/projects/{pid}/chat/clear")
    def chat_clear(pid: str, body: dict = Body(default={}), user: str = Depends(current_user)) -> dict:
        p = _project(st, pid)
        _require(st, pid, user, "editor")
        if body.get("baseVersion") is not None and int(body["baseVersion"]) != p.revision:
            raise HTTPException(409, {"error": "version_conflict", "currentVersion": p.revision})
        return {**st.chat.clear(pid), "version": p.revision}

    @app.post("/api/projects/{pid}/chat/{turn_id}/{op}")
    def chat_turn_op(
        pid: str, turn_id: str, op: str, body: dict = Body(default={}), user: str = Depends(current_user)
    ) -> dict:
        from hwbp.api import _plan_preview
        from hwbp.edits import EditOp

        p = _project(st, pid)
        _require(st, pid, user, "editor")
        turn = _svc(st.chat.find, pid, turn_id)
        if body.get("revision") is not None and int(body["revision"]) != turn.revision:
            raise HTTPException(409, {"error": "stale_revision", "currentRevision": turn.revision})
        if op == "approve":
            if turn.status != "awaiting_approval" or turn.preview is None:
                raise HTTPException(409, {"error": "not_awaiting_approval", "status": turn.status})
            if turn.deadline and time.time() > turn.deadline:
                turn.status = "expired"
            elif turn.base_version != p.revision:
                turn.status, turn.failure_code = "outdated", "version_changed"
            else:
                st.projects.save(pid, turn.preview, p.revision)
                turn.status, turn.result_kind, turn.approver, turn.preview = "completed", "edit", user, None
        elif op == "dismiss":
            if turn.status in ("queued", "running", "awaiting_approval"):
                turn.status, turn.failure_code, turn.preview = "cancelled", "dismissed", None
        elif op == "retry":
            if turn.mode != "edit":
                raise HTTPException(400, {"error": "not_an_edit"})
            turn.base_version = p.revision
            _plan_preview(st, p.content, [EditOp(o["op"], o["args"]) for o in turn.ops], turn)
            turn.failure_code = None
        else:
            raise HTTPException(400, {"error": "unknown_op", "op": op})
        turn.revision += 1
        st.chat.bump(pid)
        return {"turn": turn.public(), "version": st.projects.get(pid).revision}

    # ---- live events
    st.presence = {}

    @app.websocket("/api/projects/{pid}/events")
    async def project_events(ws: WebSocket, pid: str) -> None:
        await ws.accept()
        if pid not in st.projects:
            await ws.send_json({"type": "error", "error": "not_found"})
            await ws.close()
            return
        user = DEFAULT_USER
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=0.5)
            if msg.get("type") == "authenticate":
                user = msg.get("userId") or user
        except (asyncio.TimeoutError, json.JSONDecodeError, KeyError):
            pass
        except WebSocketDisconnect:
            return
        pres = st.presence.setdefault(pid, {})
        pres[user] = pres.get(user, 0) + 1

        def state() -> tuple:
            return (
                st.projects.get(pid).revision,
                st.chat.rev.get(pid, 0),
                st.notes.get(pid)["version"],
                tuple(sorted(st.presence.get(pid, {}))),
            )

        last = state()
        await ws.send_json({"type": "ready", "version": last[0], "chatRevision": last[1], "readmeVersion": last[2]})
        await ws.send_json({"type": "presence", "users": list(last[3])})
        recv = asyncio.ensure_future(ws.receive_text())
        try:
            while True:
                done, _ = await asyncio.wait({recv}, timeout=0.05)
                if recv in done:
                    try:
                        text = recv.result()
                    except WebSocketDisconnect:
                        break
                    if text == "ping":
                        await ws.send_json({"type": "pong"})
                    recv = asyncio.ensure_future(ws.receive_text())
                cur = state()
                if cur[0] != last[0]:
                    await ws.send_json({"type": "project_changed", "version": cur[0]})
                if cur[1] != last[1]:
                    await ws.send_json({"type": "chat_changed", "revision": cur[1]})
                if cur[2] != last[2]:
                    await ws.send_json({"type": "readme_changed", "version": cur[2]})
                if cur[3] != last[3]:
                    await ws.send_json({"type": "presence", "users": list(cur[3])})
                last = cur
        except Exception:  # noqa: BLE001 - client went away mid-send
            pass
        finally:
            recv.cancel()
            pres[user] -= 1
            if pres[user] <= 0:
                pres.pop(user, None)

    # ---- notes (collaborative README)
    @app.get("/api/projects/{pid}/readme")
    def get_readme(pid: str, user: str = Depends(current_user)) -> dict:
        _project(st, pid)
        _require(st, pid, user, "viewer")
        return st.notes.get(pid)

    @app.put("/api/projects/{pid}/readme")
    def put_readme(pid: str, body: dict = Body(...), user: str = Depends(current_user)) -> dict:
        _project(st, pid)
        _require(st, pid, user, "editor")
        return _svc(st.notes.put, pid, str(body.get("text", "")), int(body.get("baseVersion", -1)), user)

    # ---- collaboration
    @app.get("/api/projects/{pid}/members")
    def members(pid: str, user: str = Depends(current_user)) -> dict:
        _project(st, pid)
        _require(st, pid, user, "viewer")
        return {"members": st.collab.list_members(pid)}

    @app.patch("/api/projects/{pid}/members/{member}")
    def member_role(pid: str, member: str, body: dict = Body(...), user: str = Depends(current_user)) -> dict:
        _svc(st.collab.change_role, pid, user, member, body.get("role", ""))
        return {"members": st.collab.list_members(pid)}

    @app.delete("/api/projects/{pid}/members/{member}")
    def member_remove(pid: str, member: str, user: str = Depends(current_user)) -> dict:
        _svc(st.collab.remove_member, pid, user, member)
        return {"members": st.collab.list_members(pid)}

    @app.post("/api/projects/{pid}/invitations")
    def invite(pid: str, body: dict = Body(...), user: str = Depends(current_user)) -> dict:
        _project(st, pid)
        inv = _svc(st.collab.invite, pid, user, body.get("email", ""), body.get("role", "viewer"))
        return {"id": inv.id, "email": inv.email, "role": inv.role, "token": inv.token, "expiresAt": inv.expires_at,
                "url": f"/invitations#token={inv.token}"}  # fmt: skip

    @app.get("/api/projects/{pid}/invitations")
    def invitations(pid: str, user: str = Depends(current_user)) -> dict:
        _svc(st.collab.require, pid, user, "owner")
        return {"invitations": [{"id": i.id, "email": i.email, "role": i.role, "expiresAt": i.expires_at}
                                for i in st.collab.invites.values() if i.project_id == pid and not i.revoked]}  # fmt: skip

    @app.delete("/api/projects/{pid}/invitations/{invite_id}")
    def revoke_invite(pid: str, invite_id: str, user: str = Depends(current_user)) -> dict:
        _svc(st.collab.revoke_invite, pid, user, invite_id)
        return {"revoked": True}

    @app.post("/api/project-invitations/accept")
    def accept_invite(body: dict = Body(...), user: str = Depends(current_user)) -> dict:
        return _svc(st.collab.accept_invite, user, body.get("token", ""))

    @app.post("/api/projects/{pid}/share-links")
    def create_link(pid: str, body: dict = Body(default={}), user: str = Depends(current_user)) -> dict:
        _project(st, pid)
        link = _svc(st.collab.create_link, pid, user, body.get("role", "viewer"), body.get("expiresInHours"))
        return {
            "id": link.id,
            "role": link.role,
            "token": link.token,
            "expiresAt": link.expires_at,
            "url": f"/join#token={link.token}",
        }

    @app.get("/api/projects/{pid}/share-links")
    def list_links(pid: str, user: str = Depends(current_user)) -> dict:
        _svc(st.collab.require, pid, user, "owner")
        return {"shareLinks": [{"id": s.id, "role": s.role, "expiresAt": s.expires_at} for s in st.collab.links.values()
                               if s.project_id == pid and not s.revoked]}  # fmt: skip

    @app.delete("/api/projects/{pid}/share-links/{link_id}")
    def revoke_link(pid: str, link_id: str, user: str = Depends(current_user)) -> dict:
        _svc(st.collab.revoke_link, pid, user, link_id)
        return {"revoked": True}

    @app.post("/api/share-links/redeem")
    def redeem(body: dict = Body(...), user: str = Depends(current_user)) -> dict:
        return _svc(st.collab.redeem_link, user, body.get("token", ""))

    # ================================================================== community
    @app.post("/api/unpublish")
    def unpublish(body: dict = Body(...), user: str = Depends(current_user)) -> dict:
        pid = body.get("projectId", "")
        _project(st, pid)
        _require(st, pid, user, "owner")
        return {"unpublished": st.community.unpublish(pid)}

    @app.get("/api/community")
    def community(
        page: int = 0, limit: int = Query(24, ge=1, le=100), sort: str = "trending", q: str | None = None
    ) -> dict:
        out = _svc(st.community.list, page, limit, sort, q)
        out["projects"] = [with_author(e) for e in out["projects"]]
        return out

    @app.get("/api/community/suggest")
    def community_suggest(q: str, limit: int = 8) -> dict:
        return {"suggestions": st.community.suggest(q, limit)}

    @app.post("/api/star")
    def star(body: dict = Body(...), user: str = Depends(current_user)) -> dict:
        return _svc(st.community.star, body.get("token", ""), user)

    @app.get("/api/community/starred")
    def starred(user: str = Depends(current_user)) -> dict:
        return {"projects": [with_author(e) for e in st.community.starred(user)]}

    @app.get("/api/trending")
    def trending(limit: int = Query(4, ge=1, le=50)) -> list:
        return [with_author(e) for e in st.community.list(0, limit, "trending")["projects"]]

    @app.get("/api/stats")
    def stats() -> dict:
        return {**st.community.stats(), "accountsCreated": len(st.accounts.profiles)}

    # ================================================================== parts: sourcing, links, images
    def _parts(ids: list[str]):
        return {i: st.engine.catalog.get(i) for i in ids if i in st.engine.catalog}

    @app.post("/api/parts/nexar-sourcing")
    def nexar_sourcing(body: dict = Body(...)) -> dict:
        providers = st.providers or [CatalogSourcing()]
        out = {}
        for pid, part in _parts(body.get("partIds") or []).items():
            node = Node(id=pid, name=part.name, type=part.type)
            enrich_node(node, part, providers)
            out[pid] = {
                "sourcing": [
                    {
                        "vendor": o.vendor,
                        "product_url": o.url,
                        "price": o.price,
                        "currency": o.currency,
                        "stock_qty": o.stock,
                        "mpn": part.mpn,
                    }
                    for o in node.research.sourcing
                ],  # fmt: skip
                "documents": [d.dump() for d in node.research.documentation_urls],
                "datasheetUrl": node.research.datasheet_url,
                "cadFiles": [
                    {
                        "url": f"https://grabcad.com/library?query={quote_plus(part.mpn or part.name)}",
                        "label": "GrabCAD search",
                    },
                    {
                        "url": f"https://www.snapeda.com/search/?q={quote_plus(part.mpn or part.name)}",
                        "label": "SnapEDA search",
                    },
                ],  # fmt: skip
            }
        return {"data": out}

    LINKS = {
        "amazon": "https://www.amazon.com/s?k={q}",
        "affiliate": "https://www.aliexpress.com/wholesale?SearchText={q}",  # plain search link, no affiliate id
        "adafruit": "https://www.adafruit.com/search?q={q}",
    }

    for kind, tmpl in LINKS.items():

        def make(kind=kind, tmpl=tmpl):
            @app.post(f"/api/parts/{kind}-links", name=f"{kind}_links")
            def links(body: dict = Body(...)) -> dict:
                return {
                    "links": {
                        pid: tmpl.format(q=quote_plus(p.mpn or p.name))
                        for pid, p in _parts(body.get("partIds") or []).items()
                    }
                }

        make()

    @app.get("/api/parts/image")
    def part_image(name: str, type: str | None = None) -> dict:  # noqa: A002
        return {"imageUrl": f"/api/parts/icon.svg?name={quote_plus(name)}&type={quote_plus(type or 'other')}"}

    @app.get("/api/parts/icon.svg")
    def part_icon_svg(name: str = "", type: str | None = None) -> Response:  # noqa: A002
        return Response(part_icon(name, type), media_type="image/svg+xml")

    @app.get("/api/parts/{part_id}/photo")
    def part_photo(part_id: str) -> Response:
        if part_id not in st.engine.catalog:
            raise HTTPException(404, "unknown part")
        p = st.engine.catalog.get(part_id)
        return Response(part_icon(p.name, p.type), media_type="image/svg+xml")

    # ================================================================== AI-action equivalents
    @app.post("/api/ideas")
    def ideas(body: dict = Body(default={})) -> dict:
        return generate_ideas(body.get("answers") or body)

    @app.get("/api/projects/{pid}/suggested-prompts")
    def prompts(pid: str, mode: str = "unified") -> dict:
        return suggested_prompts(_project(st, pid).content, mode)

    @app.post("/api/projects/{pid}/part-descriptions/refresh")
    def refresh_descriptions(pid: str, user: str = Depends(current_user)) -> dict:
        p = _project(st, pid)
        _require(st, pid, user, "editor")
        content = p.content.model_copy(deep=True)
        desc = refresh_part_descriptions(content, st.engine.catalog)
        n = apply_descriptions(content, desc)
        return {"updated": n, "descriptions": desc, "version": save_content(pid, content)}

    @app.get("/api/projects/{pid}/instructions/{step_id}/{sub_id}/detail")
    def step_detail(pid: str, step_id: str, sub_id: str) -> dict:
        try:
            return substep_detail(_project(st, pid).content, step_id, sub_id)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    @app.get("/api/projects/{pid}/image.svg")
    def hero(pid: str, variant: int | None = None, dark: bool = False) -> Response:
        v = st.image_variant.get(pid, 0) if variant is None else variant
        return Response(project_image(_project(st, pid).content, v, dark)[0], media_type="image/svg+xml")

    @app.post("/api/projects/{pid}/image-rerolls")
    def reroll(pid: str, user: str = Depends(current_user)) -> dict:
        _project(st, pid)
        _require(st, pid, user, "editor")
        v = (st.image_variant.get(pid, 0) + 1) % 4
        rid = f"{pid}-{v}"
        return {
            "id": rid,
            "variant": v,
            "hash": project_image(st.projects.get(pid).content, v)[1],
            "url": f"/api/projects/{pid}/image.svg?variant={v}",
        }

    @app.post("/api/projects/{pid}/image-rerolls/{reroll_id}/apply")
    def apply_reroll(pid: str, reroll_id: str, user: str = Depends(current_user)) -> dict:
        p = _project(st, pid)
        _require(st, pid, user, "editor")
        try:
            v = int(reroll_id.rsplit("-", 1)[1]) % 4
        except (IndexError, ValueError) as e:
            raise HTTPException(404, "unknown reroll") from e
        st.image_variant[pid] = v
        content = p.content.model_copy(deep=True)
        content.project_image_url, content.project_image_hash = (
            f"/api/projects/{pid}/image.svg",
            project_image(content, v)[1],
        )
        return {"variant": v, "version": save_content(pid, content)}

    @app.get("/api/projects/{pid}/steps/{step_id}/{sub_id}/image.svg")
    def step_img(pid: str, step_id: str, sub_id: str) -> Response:
        c = _project(st, pid).content
        for s in c.instruction_steps:
            for sub in s.sub_steps:
                if s.id == step_id and sub.id == sub_id:
                    return Response(step_image(c, sub.part_ids), media_type="image/svg+xml")
        raise HTTPException(404, "unknown sub-step")

    # ================================================================== mechanical tools
    @app.post("/api/projects/{pid}/mech/heal")
    def mech_heal(pid: str, user: str = Depends(current_user)) -> dict:
        p = _project(st, pid)
        _require(st, pid, user, "editor")
        content = p.content.model_copy(deep=True)
        rep = heal_mechanical(content)
        content.mech_layout = solve_layout(content)
        return {**rep, "version": save_content(pid, content)}

    @app.post("/api/projects/{pid}/mech/enrich")
    def mech_enrich(pid: str, user: str = Depends(current_user)) -> dict:
        p = _project(st, pid)
        _require(st, pid, user, "editor")
        content = p.content.model_copy(deep=True)
        rep = enrich_mechanical(content)
        return {**rep, "version": save_content(pid, content)}

    @app.get("/api/projects/{pid}/mech/explode")
    def mech_explode(pid: str, factor: float = 0.9) -> dict:
        c = _project(st, pid).content
        return explode_layout(c.mech_layout or solve_layout(c), factor)

    @app.get("/api/projects/{pid}/mech/joints")
    def mech_joints(pid: str) -> dict:
        c = _project(st, pid).content
        stored = (c.model_extra or {}).get("assembly") or {}
        return {"version": 2, "joints": stored.get("joints") or derive_joints(c.mech_layout or solve_layout(c))}

    @app.post("/api/projects/{pid}/mech/joints")
    def mech_pose(pid: str, body: dict = Body(...), user: str = Depends(current_user)) -> dict:
        p = _project(st, pid)
        layout = p.content.mech_layout or solve_layout(p.content)
        try:
            posed = apply_joints(layout, body.get("joints") or derive_joints(layout))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if body.get("save"):
            _require(st, pid, user, "editor")
            content = p.content.model_copy(deep=True)
            content.assembly = {"version": 2, "joints": posed["joints"]}
            posed["version"] = save_content(pid, content)
        return posed

    @app.get("/api/projects/{pid}/wiring/layout")
    def wiring_layout(pid: str, mode: str = "simple") -> dict:
        c = _project(st, pid).content
        if mode == "enriched":
            return enriched_wiring_layout(c)
        if mode != "simple":
            raise HTTPException(400, "mode must be simple|enriched")
        return c.wiring_layout or {}

    @app.get("/api/projects/{pid}/wiring-enriched.svg")
    def wiring_enriched(pid: str, dark: bool = False) -> Response:
        lay = enriched_wiring_layout(_project(st, pid).content)
        return Response(render_svg(lay, lay["nodeMeta"], dark=dark), media_type="image/svg+xml")

    @app.get("/api/projects/{pid}/bom/merged")
    def bom_merged(pid: str, buildQty: int = Query(1, ge=1, le=100000), estimates: bool = True) -> dict:  # noqa: N803
        b = build_bom(_project(st, pid).content.nodes, buildQty, use_offers=not estimates)
        return {"rows": merge_by_product(b.lines), "totalUsd": b.parts_total_usd}

    # ================================================================== exports
    @app.post("/api/export/cad")
    def export_cad_route(body: dict = Body(...)) -> Response:
        p = _project(st, body.get("projectId", ""))
        try:
            data, counts = export_cad(p.content, body.get("formats"), body.get("nodeIds"))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return Response(data, media_type="application/zip", headers={"X-Downloaded-Count": str(counts["downloaded"]),
                        "X-Omitted-Count": str(counts["omitted"]), "Content-Disposition": f'attachment; filename="{p.id}-cad.zip"'})  # fmt: skip

    @app.post("/api/export/file")
    def export_file_route(body: dict = Body(...)) -> Response:
        p = _project(st, body.get("projectId", ""))
        kind = body.get("kind", "")
        if kind not in FILE_KINDS:
            raise HTTPException(400, {"error": "unknown_kind", "kinds": list(FILE_KINDS)})
        data, media = export_file(p.content, kind, int(body.get("buildQty", 1)))
        return Response(
            data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{p.id}-{kind}"'}
        )

    @app.post("/api/export/zip")
    def export_zip_route(body: dict = Body(...)) -> Response:
        p = _project(st, body.get("projectId", ""))
        return Response(export_zip(p.content, int(body.get("buildQty", 1))), media_type="application/zip")

    @app.post("/api/cad-wallet/purchase")
    def buy_credits(body: dict = Body(...)) -> dict:
        bundle = body.get("bundle", "")
        if bundle not in CREDIT_BUNDLES:
            raise HTTPException(
                400,
                {
                    "error": "unknown_bundle",
                    "bundles": {k: {"credits": c, "usd": u} for k, (c, u) in CREDIT_BUNDLES.items()},
                },
            )
        st.wallet.buy(bundle)
        return {"balance": st.wallet.balance, "simulated": True, "note": "no payment provider configured"}

    # ================================================================== account, plans, credits
    @app.get("/api/profile/me")
    def profile(user: str = Depends(current_user)) -> dict:
        return st.accounts.get(user).public()

    @app.get("/api/account")
    def account(user: str = Depends(current_user)) -> dict:
        p = st.accounts.get(user)
        return {**p.public(), "credits": st.accounts.credits(user), "emailPreferences": p.email_prefs,
                "projects": len(st.collab.projects_of(user))}  # fmt: skip

    @app.post("/api/set-username")
    def set_username(body: dict = Body(...), user: str = Depends(current_user)) -> dict:
        return _svc(st.accounts.set_username, user, body.get("username", "")).public()

    @app.post("/api/auto-username")
    def auto_username(user: str = Depends(current_user)) -> dict:
        return _svc(st.accounts.auto_username, user).public()

    @app.post("/api/set-bio")
    def set_bio(body: dict = Body(...), user: str = Depends(current_user)) -> dict:
        return _svc(st.accounts.set_bio, user, body.get("bio", "")).public()

    @app.post("/api/set-profile-color")
    def set_color(body: dict = Body(...), user: str = Depends(current_user)) -> dict:
        return _svc(st.accounts.set_color, user, body.get("color", "")).public()

    @app.post("/api/onboarding/complete")
    def onboarding(body: dict = Body(default={}), user: str = Depends(current_user)) -> dict:
        return st.accounts.complete_onboarding(user, body.get("answers") or body).public()

    @app.get("/api/email-preferences")
    def get_prefs(user: str = Depends(current_user)) -> dict:
        return st.accounts.get(user).email_prefs

    @app.post("/api/email-preferences")
    def set_prefs(body: dict = Body(...), user: str = Depends(current_user)) -> dict:
        return st.accounts.set_email_prefs(user, body)

    @app.api_route("/api/get-credits", methods=["GET", "POST"])
    def get_credits(user: str = Depends(current_user)) -> dict:
        return st.accounts.credits(user)

    @app.post("/api/consume-credit")
    def consume(user: str = Depends(current_user)) -> dict:
        return _svc(st.accounts.consume_credit, user)

    @app.get("/api/check-subscription")
    def subscription(user: str = Depends(current_user)) -> dict:
        p = st.accounts.get(user)
        return {"plan": p.plan, "active": p.plan != "free"}

    @app.post("/api/billing/plan")
    def set_plan(body: dict = Body(...), user: str = Depends(current_user)) -> dict:
        p = _svc(st.accounts.set_plan, user, body.get("plan", ""))
        return {**p.public(), "simulated": True, "note": "no payment provider configured"}

    @app.post("/api/contact-leads")
    def lead(body: dict = Body(...)) -> dict:
        return {"id": _svc(st.accounts.add_lead, body)["id"]}

    # ================================================================== web pages
    @app.get("/", response_class=HTMLResponse)
    def studio() -> str:
        from importlib.resources import files

        return files("hwbp").joinpath("viewer/studio.html").read_text(encoding="utf-8")

    def site_page() -> str:
        from importlib.resources import files

        return files("hwbp").joinpath("viewer/site.html").read_text(encoding="utf-8")

    for path in ("/community", "/projects", "/pricing", "/faq", "/about", "/account", "/invitations", "/join"):
        app.add_api_route(
            path, site_page, methods=["GET"], response_class=HTMLResponse, name=f"page{path.replace('/', '_')}"
        )

    @app.get("/p/{pid}", response_class=HTMLResponse)
    def project_page(pid: str) -> str:
        c = _project(st, pid).content
        return render_viewer(c).replace(
            "window.HWBP_API = null;", f"window.HWBP_API = {json.dumps({'projectId': pid, 'mode': 'project'})};", 1
        )

    @app.get("/s/{token}", response_class=HTMLResponse)
    def share_page(token: str) -> str:
        e = _svc(st.community.get, token)
        c = ProjectContent.model_validate(e["content"])
        return render_viewer(c).replace(
            "window.HWBP_API = null;", f"window.HWBP_API = {json.dumps({'token': token, 'mode': 'share'})};", 1
        )
