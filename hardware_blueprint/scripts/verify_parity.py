"""Feature-parity checklist: every blueprint.io endpoint / AI action / UI feature, mapped to hwbp, verified one by one.

Run:  uv run python scripts/verify_parity.py            (prints the table, exits 1 if any implemented check fails)
      uv run python scripts/verify_parity.py --write    (also rewrites docs/PARITY.md)

Status: DONE (implemented + check passes) · FAIL (implemented, check fails) · MISSING (not implemented yet) ·
N/A (deliberately not replicated — reason given).
"""

from __future__ import annotations

import base64
import io
import json
import sys
import traceback
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from hwbp.api import create_app  # noqa: E402
from hwbp.image import encode_png_gray  # noqa: E402

L_SHAPE = [
    [0.0 if (10 <= x < 70 and 30 <= y < 50) or (10 <= x < 30 and 10 <= y < 70) else 1.0 for x in range(80)]
    for y in range(80)
]
PNG = encode_png_gray(L_SHAPE)
PNG_B64 = base64.b64encode(PNG).decode()


@dataclass
class Item:
    area: str
    blueprint: str
    hwbp: str
    check: Callable[["Ctx"], str] | None = None
    na: str | None = None  # reason when deliberately not replicated


class Ctx:
    """Shared fixtures: one app, one generated project."""

    def __init__(self) -> None:
        self.app = create_app(run_async=False)
        self.c = TestClient(self.app)
        run = self.c.post(
            "/api/generation-runs",
            json={"prompt": "Smart plant monitor with soil moisture, OLED display, buzzer and wifi"},
        ).json()
        self.run = run
        self.pid = run["projectId"]

    def project(self) -> dict:
        return self.c.get(f"/api/projects/{self.pid}").json()


def ok(cond: bool, detail: str) -> str:
    if not cond:
        raise AssertionError(detail)
    return detail


ITEMS: list[Item] = []


def item(area: str, blueprint: str, hwbp: str, na: str | None = None):
    def deco(fn: Callable[[Ctx], str] | None):
        ITEMS.append(Item(area, blueprint, hwbp, fn, na))
        return fn

    return deco


def missing(area: str, blueprint: str, hwbp: str = "—") -> None:
    ITEMS.append(Item(area, blueprint, hwbp, None, None))


def not_applicable(area: str, blueprint: str, reason: str) -> None:
    ITEMS.append(Item(area, blueprint, "—", None, reason))


# =====================================================================================================================
# 1. Generation runs & AI pipeline
# =====================================================================================================================
@item("Generation", "POST /api/generation-runs (prompt, idempotencyKey)", "api.start_run → Engine.run")
def _(x: Ctx) -> str:
    again = x.c.post("/api/generation-runs", json={"prompt": "zzz", "idempotencyKey": "same"}).json()
    again2 = x.c.post("/api/generation-runs", json={"prompt": "yyy", "idempotencyKey": "same"}).json()
    return ok(x.run["status"] == "completed" and again["id"] == again2["id"], "completed; idempotency key dedupes")


@item("Generation", "GET /api/generation-runs/:id?waitMs&sinceRevision (long-poll)", "api.poll_run")
def _(x: Ctx) -> str:
    s = x.c.get(f"/api/generation-runs/{x.run['id']}", params={"waitMs": 50, "sinceRevision": 999}).json()
    return ok(s["phase"] == "done" and s["revision"] >= 7, f"phase={s['phase']} revision={s['revision']}")


@item(
    "Generation",
    "Run phases planning→questions→electrical→mechanical→revealing→guide→done",
    "engine.PHASES / Engine.run events",
)
def _(x: Ctx) -> str:
    from hwbp.engine import PHASES, Engine

    phases = [e["phase"] for e in Engine().run("LED lamp with knob")]
    return ok(phases == list(PHASES), " → ".join(phases))


@item("Generation", "sections{parts,wiring,mech,info,guide} unlock tabs", "Run.public().sections")
def _(x: Ctx) -> str:
    return ok(all(x.run["sections"].values()), str(x.run["sections"]))


@item("Generation", "POST /api/generation-runs/:id/cancel", "api.cancel_run")
def _(x: Ctx) -> str:
    return ok(x.c.post(f"/api/generation-runs/{x.run['id']}/cancel").status_code == 200, "200")


@item("Generation", "SSE progress (ping/queued/running/done/error)", "GET /api/generation-runs/:id/events")
def _(x: Ctx) -> str:
    t = x.c.get(f"/api/generation-runs/{x.run['id']}/events").text
    return ok("event: progress" in t and "event: done" in t, "progress + done events")


@item("Generation", "questionRounds (clarifying multiple-choice questions)", "engine.clarifying_questions")
def _(x: Ctx) -> str:
    qs = x.run["questionRounds"][0]["questions"]
    return ok(len(qs) >= 1 and all("options" in q for q in qs), f"{len(qs)} question(s)")


@item(
    "Generation",
    "POST /api/generation-runs/:id/questions {revision, qa} (answer → regenerate)",
    "api_ext.answer_questions",
)
def _(x: Ctx) -> str:
    r = x.c.post("/api/generation-runs", json={"prompt": "plant monitor with oled"}).json()
    q = r["questionRounds"][0]["questions"][0]
    r2 = x.c.post(
        f"/api/generation-runs/{r['id']}/questions",
        json={"revision": r["revision"], "qa": [{"id": q["id"], "answer": q["options"][0]}]},
    ).json()
    return ok(
        r2["status"] == "completed" and r2["projectId"] == r["projectId"],
        f"{q['id']}={q['options'][0]} → same project regenerated",
    )


@item("Generation", "POST /api/generation-runs/:id/retry {revision}", "api_ext.retry_run")
def _(x: Ctx) -> str:
    return ok(x.c.post(f"/api/generation-runs/{x.run['id']}/retry", json={}).json()["status"] == "completed", "re-ran")


@item("Generation", "GET /api/generation-runs?recoverable=true (list resumable runs)", "api_ext.list_runs")
def _(x: Ctx) -> str:
    allr = x.c.get("/api/generation-runs").json()["runs"]
    return ok(
        allr and x.c.get("/api/generation-runs", params={"recoverable": True}).status_code == 200,
        f"{len(allr)} runs listed",
    )


@item(
    "Generation",
    "POST /api/generation-input-images (upload → {id,url,hash,mimeType}) + GET /api/input-images/:id",
    "services.InputImages",
)
def _(x: Ctx) -> str:
    img = x.c.post("/api/generation-input-images", json={"image": "data:image/png;base64," + PNG_B64}).json()
    return ok(
        x.c.get(img["url"]).content == PNG and len(img["hash"]) == 64, f"id {img['id'][:8]} hash {img['hash'][:8]}"
    )


@item(
    "Generation",
    "imageAnalysis {summary, components, uncertainties} on the run snapshot",
    "Run.image_analysis (+ silhouette)",
)
def _(x: Ctx) -> str:
    r = x.c.post(
        "/api/generation-runs", json={"prompt": "lamp", "inputImages": [{"mime": "image/png", "data": PNG_B64}]}
    ).json()
    return ok(r["imageAnalysis"]["silhouette"]["aspect"] > 0, str(r["imageAnalysis"]["silhouette"]))


not_applicable(
    "Generation",
    "modelProvider/modelTier (gemini lite|max|ultra, openai tiers)",
    "hwbp is deterministic; tiers only matter for an LLM backend (see RequirementExtractor/VisionModel seams)",
)


@item("Picture", "Picture input → blueprint (UPLOAD IMAGE)", "image.brief_from_image + inputImages on runs")
def _(x: Ctx) -> str:
    r = x.c.post(
        "/api/generation-runs",
        json={"prompt": "desk clock with display", "inputImages": [{"mime": "image/png", "data": PNG_B64}]},
    ).json()
    return ok(
        r["projectData"]["requirements"]["form_factor"]["aspect"] > 0,
        f"form factor {r['projectData']['requirements']['form_factor']}",
    )


@item(
    "Picture",
    "Picture → CAD/mesh (extrude with holes / relief / revolve)",
    "POST /api/image-to-mesh, image.image_to_mesh",
)
def _(x: Ctx) -> str:
    reps = [
        x.c.post("/api/image-to-mesh", json={"image": PNG_B64, "mode": m}).json()["report"]
        for m in ("extrude", "relief", "revolve")
    ]
    return ok(all(r["watertight"] for r in reps), "3 modes watertight")


@item("Picture", "DRAW (sketch canvas → picture input)", "studio.html canvas → /api/generation-input-images")
def _(x: Ctx) -> str:
    html = x.c.get("/").text
    return ok(
        'id="draw"' in html and "toDataURL" in html and "generation-input-images" in html, "canvas → PNG → upload → run"
    )


# AI actions (/api/gemini) — deterministic equivalents
@item("AI actions", "planFollowUp (edit plan from chat)", "edits.message_to_ops + POST /api/projects/:id/chat")
def _(x: Ctx) -> str:
    from hwbp.edits import message_to_ops
    from hwbp.models import ProjectContent

    mode, ops = message_to_ops(
        "add a gps", ProjectContent.model_validate(x.project()["content"]), x.app.state.hwbp.engine.catalog
    )
    return ok(mode == "edit" and ops, f"{[o.op for o in ops]}")


@item("AI actions", "askFollowUp (answer questions about the design)", "api._answer via POST /chat")
def _(x: Ctx) -> str:
    a = x.c.post(f"/api/projects/{x.pid}/chat", json={"message": "why the display?"}).json()
    return ok(a["mode"] == "ask" and a["answer"], a["answer"][:60])


@item("AI actions", "generateGuide (tools, assumptions, 4 phases)", "instructions.build_instructions")
def _(x: Ctx) -> str:
    st = x.project()["content"]["instructionSteps"]
    return ok(
        [s["id"] for s in st] == ["fabricate", "wire", "bringup", "assemble"],
        f"{sum(len(s['subSteps']) for s in st)} sub-steps",
    )


@item("AI actions", "generateInfo (project description)", "Engine._describe → projectDescription")
def _(x: Ctx) -> str:
    return ok(bool(x.project()["content"]["projectDescription"]), x.project()["content"]["projectDescription"][:60])


@item("AI actions", "enhanceElectricalConnections / infer-rails", "wiring.infer_rails + ERC (wiring.validate)")
def _(x: Ctx) -> str:
    r = x.c.post("/api/wiring/infer-rails", json={"electricalNodes": [{"id": "a", "pins": ["GND"]}, {"id": "b", "pins": ["GND", "VCC"]}],
                                                    "connections": [{"type": "power", "source": "a", "target": "c"}]}).json()  # fmt: skip
    return ok(r["addElectricalConnections"], f"{len(r['addElectricalConnections'])} added")


@item("AI actions", "searchParts (catalog search)", "GET /api/parts/search")
def _(x: Ctx) -> str:
    return ok(x.c.get("/api/parts/search", params={"q": "servo"}).json()["results"], "results")


@item("AI actions", "researchPartSection (specs/sourcing/docs queries)", "enrichment.spec_sheet + research_queries")
def _(x: Ctx) -> str:
    r = x.c.post("/api/parts/enrich-transient", json={"name": "BME280"}).json()
    return ok(r["researchQueries"] and r["node"]["research"]["specs"], f"{len(r['node']['research']['specs'])} specs")


@item(
    "AI actions",
    "generateSubStepDetail {summary, steps[], tip} (instruction 'View details')",
    "ideas.substep_detail, GET …/instructions/:step/:sub/detail",
)
def _(x: Ctx) -> str:
    sub = x.project()["content"]["instructionSteps"][1]["subSteps"][0]
    d = x.c.get(f"/api/projects/{x.pid}/instructions/wire/{sub['id']}/detail").json()
    return ok(d["summary"] and d["steps"] and d["tip"], f"{len(d['steps'])} steps + tip")


@item(
    "AI actions",
    "generate/enrich/healMechanicalConnections for foreign docs",
    "mech.heal_mechanical / enrich_mechanical, POST …/mech/heal|enrich",
)
def _(x: Ctx) -> str:
    from hwbp.mech import heal_mechanical
    from hwbp.models import ProjectContent

    doc = ProjectContent.model_validate({"nodes": [{"id": "p", "name": "Plate", "type": "structural", "dimensions": "100x60x3"},
                                                   {"id": "b", "name": "Board", "type": "mcu", "dimensions": "40x20x4"}]})  # fmt: skip
    rep = heal_mechanical(doc)
    healed = x.c.post(f"/api/projects/{x.pid}/mech/heal").status_code == 200
    return ok(rep["orphansBefore"] and not rep["orphansAfter"] and healed, f"orphans {rep['orphansBefore']} → attached")


@item(
    "AI actions",
    "generateSuggestedPrompts (edit / ask prompt chips)",
    "ideas.suggested_prompts, GET …/suggested-prompts",
)
def _(x: Ctx) -> str:
    sp = x.c.get(f"/api/projects/{x.pid}/suggested-prompts").json()
    return ok(sp["edit"] and sp["ask"] and sp["hash"].startswith("v1::"), sp["prompts"][0])


@item("AI actions", "refreshPartDescriptions", "ideas.refresh_part_descriptions, POST …/part-descriptions/refresh")
def _(x: Ctx) -> str:
    r = x.c.post(f"/api/projects/{x.pid}/part-descriptions/refresh").json()
    return ok(r["updated"] > 0, f"{r['updated']} descriptions")


@item("AI actions", "generateIdeas (NEED AN IDEA? wizard)", "ideas.generate_ideas, POST /api/ideas")
def _(x: Ctx) -> str:
    r = x.c.post("/api/ideas", json={"answers": {"interests": ["robotics"], "budget": 50}}).json()
    return ok(r["projects"], r["projects"][0]["name"])


@item(
    "AI actions",
    "generateProjectImage / image rerolls (project hero image)",
    "render.project_image, /image.svg, /image-rerolls",
)
def _(x: Ctx) -> str:
    rr = x.c.post(f"/api/projects/{x.pid}/image-rerolls").json()
    applied = x.c.post(f"/api/projects/{x.pid}/image-rerolls/{rr['id']}/apply").json()
    return ok(
        x.c.get(rr["url"]).text.startswith("<svg") and applied["variant"] == rr["variant"],
        f"variant {rr['variant']} hash {rr['hash'][:8]}",
    )


@item("AI actions", "generateStepImage (per-step illustration)", "render.step_image, GET …/steps/:step/:sub/image.svg")
def _(x: Ctx) -> str:
    sub = x.project()["content"]["instructionSteps"][3]["subSteps"][0]
    svg = x.c.get(f"/api/projects/{x.pid}/steps/assemble/{sub['id']}/image.svg").text
    return ok(svg.startswith("<svg") and 'opacity="0.18"' in svg, "highlighted step render")


not_applicable(
    "AI actions",
    "setContext / resetSession",
    "server-side LLM session state; hwbp is stateless per request (project_summary builds context on demand)",
)


# =====================================================================================================================
# 2. Projects, chat, collaboration
# =====================================================================================================================
@item("Projects", "GET /api/projects/:id + /head", "api.get_project / api.head")
def _(x: Ctx) -> str:
    h = x.c.get(f"/api/projects/{x.pid}/head").json()
    return ok(h["version"] == x.project()["version"], f"version {h['version']}")


@item("Projects", "POST /api/projects/save (baseVersion → 409 version_conflict)", "api.save + ProjectStore")
def _(x: Ctx) -> str:
    p = x.project()
    bad = x.c.post("/api/projects/save", json={"projectId": x.pid, "content": p["content"], "baseVersion": 0})
    good = x.c.post(
        "/api/projects/save", json={"projectId": x.pid, "content": p["content"], "baseVersion": p["version"]}
    )
    return ok(bad.status_code == 409 and good.status_code == 200, "409 on stale, 200 on current")


@item("Projects", "versions / restore", "GET /versions, POST /restore/:v")
def _(x: Ctx) -> str:
    v = x.c.get(f"/api/projects/{x.pid}/versions").json()["versions"]
    return ok(len(v) >= 2 and x.c.post(f"/api/projects/{x.pid}/restore/1").status_code == 200, f"{len(v)} versions")


@item("Projects", "Edit plan patch format + auto-repair + validate", "edits.validate_plan/apply_plan, POST /edit-plan")
def _(x: Ctx) -> str:
    r = x.c.post(
        f"/api/projects/{x.pid}/edit-plan", json={"plan": {"removeNodeIds": ["dispaly"]}, "dryRun": True}
    ).json()
    return ok(r["plan"]["removeNodeIds"] == ["display"], "fuzzy-repaired dispaly → display")


@item("Projects", "POST /api/projects/:id/chat/classify {mode, decisionReceipt}", "classify.classify_message")
def _(x: Ctx) -> str:
    r = x.c.post(f"/api/projects/{x.pid}/chat/classify", json={"message": "replace the display with an e-paper"}).json()
    return ok(r["mode"] == "edit" and r["intent"] == "swap_part", r["decisionReceipt"])


@item("Projects", "GET /api/projects (list my projects)", "api_ext.my_projects")
def _(x: Ctx) -> str:
    ids = [p["id"] for p in x.c.get("/api/projects").json()["projects"]]
    return ok(x.pid in ids, f"{len(ids)} project(s)")


@item(
    "Projects",
    "Chat turns: GET /chat, awaiting_approval preview, /chat/:turn/{approve|dismiss|retry}, /chat/clear",
    "services.ChatLog + diff_plan",
)
def _(x: Ctx) -> str:
    t = x.c.post(f"/api/projects/{x.pid}/chat", json={"message": "add a gps", "autoApprove": False}).json()["turn"]
    ok(t["status"] == "awaiting_approval" and t["plan"]["addParts"], "preview")
    done = x.c.post(f"/api/projects/{x.pid}/chat/{t['id']}/approve", json={}).json()["turn"]
    snap = x.c.get(f"/api/projects/{x.pid}/chat").json()
    return ok(done["status"] == "completed" and snap["turns"], f"plan '{t['plan']['plan'][:40]}' approved")


@item(
    "Projects",
    "WebSocket /api/projects/:id/events (ready, presence, project/chat/readme changes)",
    "api_ext.project_events",
)
def _(x: Ctx) -> str:
    with x.c.websocket_connect(f"/api/projects/{x.pid}/events") as ws:
        ws.send_json({"type": "authenticate", "userId": "local-user"})
        first, second = ws.receive_json(), ws.receive_json()
        p = x.project()
        x.c.post("/api/projects/save", json={"projectId": x.pid, "content": p["content"], "baseVersion": p["version"]})
        ev = ws.receive_json()
    return ok(
        first["type"] == "ready" and second["type"] == "presence" and ev["type"] == "project_changed",
        f"{first['type']}, {second['type']}, {ev['type']}",
    )


@item("Projects", "Notes / README with readme_conflict (collab/readme)", "services.Notes, GET/PUT …/readme")
def _(x: Ctx) -> str:
    v = x.c.get(f"/api/projects/{x.pid}/readme").json()["version"]
    x.c.put(f"/api/projects/{x.pid}/readme", json={"text": "notes", "baseVersion": v})
    bad = x.c.put(f"/api/projects/{x.pid}/readme", json={"text": "x", "baseVersion": v})
    return ok(
        bad.status_code == 409 and bad.json()["detail"]["error"] == "readme_conflict",
        "409 readme_conflict on stale base",
    )


@item("Collaboration", "members (list / role change / remove)", "services.Collaboration")
def _(x: Ctx) -> str:
    link = x.c.post(f"/api/projects/{x.pid}/share-links", json={"role": "viewer"}).json()
    x.c.post("/api/share-links/redeem", json={"token": link["token"]}, headers={"X-User-Id": "m1"})
    x.c.patch(f"/api/projects/{x.pid}/members/m1", json={"role": "editor"})
    roles = {m["userId"]: m["role"] for m in x.c.get(f"/api/projects/{x.pid}/members").json()["members"]}
    x.c.delete(f"/api/projects/{x.pid}/members/m1")
    left = {m["userId"] for m in x.c.get(f"/api/projects/{x.pid}/members").json()["members"]}
    return ok(roles.get("m1") == "editor" and "m1" not in left, "added → editor → removed")


@item(
    "Collaboration",
    "invitations (create/revoke) + /api/project-invitations/accept",
    "services.Collaboration.invite/accept_invite",
)
def _(x: Ctx) -> str:
    inv = x.c.post(f"/api/projects/{x.pid}/invitations", json={"email": "z@example.com", "role": "editor"}).json()
    acc = x.c.post("/api/project-invitations/accept", json={"token": inv["token"]}, headers={"X-User-Id": "z"}).json()
    return ok(acc == {"projectId": x.pid, "role": "editor"}, "invited → accepted as editor")


@item("Collaboration", "share-links (create/revoke) + /api/share-links/redeem", "services.Collaboration links")
def _(x: Ctx) -> str:
    link = x.c.post(f"/api/projects/{x.pid}/share-links", json={"role": "viewer"}).json()
    x.c.delete(f"/api/projects/{x.pid}/share-links/{link['id']}")
    r = x.c.post("/api/share-links/redeem", json={"token": link["token"]}, headers={"X-User-Id": "q"})
    return ok(r.status_code == 404 and r.json()["detail"]["error"] == "share_link_invalid", "revoked link rejected")


not_applicable("Projects", "/api/broadcast", "purpose not determinable from the client (no payload shape observed)")


# =====================================================================================================================
# 3. Community
# =====================================================================================================================
@item(
    "Community",
    "POST /api/publish, GET /api/published?token, /published/head",
    "api.publish / published / published_head",
)
def _(x: Ctx) -> str:
    tok = x.c.post("/api/publish", json={"projectId": x.pid}).json()["token"]
    return ok(x.c.get("/api/published/head", params={"token": tok}).status_code == 200, "token works")


@item("Community", "POST /api/unpublish", "services.Community.unpublish")
def _(x: Ctx) -> str:
    tok = x.c.post("/api/publish", json={"projectId": x.pid}).json()["token"]
    x.c.post("/api/unpublish", json={"projectId": x.pid})
    gone = x.c.get("/api/published", params={"token": tok}).status_code == 404
    x.c.post("/api/publish", json={"projectId": x.pid})
    return ok(gone, "unpublished → 404")


@item("Community", "GET /api/community (page, limit, sort trending|new|stars, search)", "services.Community.list")
def _(x: Ctx) -> str:
    r = [x.c.get("/api/community", params={"sort": s}).json()["total"] for s in ("trending", "new", "stars")]
    return ok(min(r) >= 1 and x.c.get("/api/community", params={"q": "zzzz"}).json()["total"] == 0, f"totals {r}")


@item("Community", "GET /api/community/suggest (search suggestions)", "services.Community.suggest")
def _(x: Ctx) -> str:
    name = x.project()["content"]["projectName"]
    return ok(name in x.c.get("/api/community/suggest", params={"q": name[:4]}).json()["suggestions"], name)


@item("Community", "POST /api/star + GET /api/community/starred", "services.Community.star/starred")
def _(x: Ctx) -> str:
    tok = x.c.post("/api/publish", json={"projectId": x.pid}).json()["token"]
    s = x.c.post("/api/star", json={"token": tok}, headers={"X-User-Id": "fan"}).json()
    return ok(
        s["starred"] and x.c.get("/api/community/starred", headers={"X-User-Id": "fan"}).json()["projects"],
        f"starCount {s['starCount']}",
    )


@item("Community", "GET /api/trending", "services.Community.trending_score (HN-style gravity)")
def _(x: Ctx) -> str:
    return ok(x.c.get("/api/trending", params={"limit": 4}).json(), "ranked")


@item("Community", "GET /api/stats {projectsCreated, accountsCreated, publishedProjects}", "api_ext.stats")
def _(x: Ctx) -> str:
    s = x.c.get("/api/stats").json()
    return ok(s["projectsCreated"] >= 1 and s["publishedProjects"] >= 1, str(s))


@item("Community", "COPY a published project into my projects", "POST /api/projects/copy")
def _(x: Ctx) -> str:
    tok = x.c.post("/api/publish", json={"projectId": x.pid}).json()["token"]
    cp = x.c.post("/api/projects/copy", json={"token": tok}, headers={"X-User-Id": "copier"}).json()
    mine = [p["id"] for p in x.c.get("/api/projects", headers={"X-User-Id": "copier"}).json()["projects"]]
    return ok(cp["projectId"] in mine, "copied to copier's projects")


# =====================================================================================================================
# 4. Parts & sourcing
# =====================================================================================================================
@item("Parts", "POST /api/parts/canonicalize", "enrichment.canonicalize")
def _(x: Ctx) -> str:
    return ok(
        x.c.post("/api/parts/canonicalize", json={"mpn": "SG90"}).json()["catalogId"] == "sg90-servo",
        "SG90 → sg90-servo",
    )


@item("Parts", "POST /api/parts/enrich-transient", "enrichment.enrich_node")
def _(x: Ctx) -> str:
    r = x.c.post("/api/parts/enrich-transient", json={"name": "Mystery Hall Sensor"}).json()
    return ok(r["classification"]["type"] == "sensor", "classified + enriched")


@item("Parts", "GET /api/parts/:id", "catalog.get")
def _(x: Ctx) -> str:
    return ok(x.c.get("/api/parts/bme280").json()["mpn"] == "BME280", "BME280")


@item("Parts", "Component suggestions / alternatives", "selection.suggest, GET /suggestions")
def _(x: Ctx) -> str:
    return ok(x.c.get(f"/api/projects/{x.pid}/suggestions").json()["suggestions"], "ranked")


@item("Parts", "POST /api/parts/nexar-sourcing {partIds}", "api_ext.nexar_sourcing (+ NexarProvider with transport)")
def _(x: Ctx) -> str:
    d = x.c.post("/api/parts/nexar-sourcing", json={"partIds": ["bme280"]}).json()["data"]["bme280"]
    return ok(d["sourcing"] and d["cadFiles"], f"{len(d['sourcing'])} offers")


@item(
    "Parts",
    "POST /api/parts/{amazon,affiliate,adafruit}-links {partIds} → {links}",
    "api_ext.links (plain search URLs)",
)
def _(x: Ctx) -> str:
    ls = [
        x.c.post(f"/api/parts/{k}-links", json={"partIds": ["sg90-servo"]}).json()["links"]["sg90-servo"]
        for k in ("amazon", "affiliate", "adafruit")
    ]
    return ok(all("SG90" in u for u in ls), "3 link kinds")


@item("Parts", "GET /api/parts/:id/photo and /api/parts/image?name= (part images)", "render.part_icon")
def _(x: Ctx) -> str:
    url = x.c.get("/api/parts/image", params={"name": "ESP32", "type": "mcu"}).json()["imageUrl"]
    return ok(
        x.c.get(url).text.startswith("<svg") and x.c.get("/api/parts/bme280/photo").status_code == 200,
        "typed SVG icons",
    )


not_applicable(
    "Parts",
    "Affiliate tags (amazon tag, eBay campid, AliExpress affiliate)",
    "monetisation of a specific company; hwbp emits plain search links",
)
not_applicable("Parts", "/api/part-files/<hash>.pdf datasheet proxy", "hwbp links datasheets at the source")


# =====================================================================================================================
# 5. BOM / pricing
# =====================================================================================================================
@item("BOM", "INFO cost tree category → type → part, ~$ estimates", "pricing.Bom.rollup, use_offers=False")
def _(x: Ctx) -> str:
    return ok(x.project()["content"]["bom"]["rollup"], "rollup present")


@item("BOM", "PARTS table qty / unit / subtotal; BOM CSV export", "GET /bom, /bom.csv")
def _(x: Ctx) -> str:
    return ok(x.c.get(f"/api/projects/{x.pid}/bom.csv").text.startswith("node_id"), "csv")


@item("BOM", "(extra) offers with price breaks/MOQ/stock + vendor consolidation, N builds", "pricing.build_bom")
def _(x: Ctx) -> str:
    b = x.c.get(f"/api/projects/{x.pid}/bom", params={"buildQty": 100}).json()
    return ok(b["buildQty"] == 100 and b["totalUsd"] > 0, f"${b['totalUsd']} for 100 builds")


@item("BOM", "PARTS rows merged by productName (correctly summed)", "pricing.merge_by_product, GET …/bom/merged")
def _(x: Ctx) -> str:
    rows = x.c.get(f"/api/projects/{x.pid}/bom/merged").json()["rows"]
    return ok(all(abs(r["subtotalUsd"] - r["unitUsd"] * r["qty"]) < 0.02 for r in rows), f"{len(rows)} rows")


# =====================================================================================================================
# 6. Wiring
# =====================================================================================================================
@item(
    "Wiring",
    "Simple diagram: aggregated edges (voltage / 'proto ×n'), colours, legend",
    "wiring.aggregate_edges + layout.layered_layout + SVG",
)
def _(x: Ctx) -> str:
    svg = x.c.get(f"/api/projects/{x.pid}/wiring.svg").text
    return ok(svg.startswith("<svg") and "DATA" in svg, "svg with legend")


@item("Wiring", "Validation: orphan / unpowered parts", "wiring.validate (+ electrical ERC extras)")
def _(x: Ctx) -> str:
    from hwbp.design import Design, Part
    from hwbp.requirements import Requirements
    from hwbp.wiring import validate

    d = Design(Requirements(prompt="x"))
    d.add(Part(id="s", catalog=x.app.state.hwbp.engine.catalog.get("bme280"), role="r", name="S"))
    validate(d)
    return ok(any(i.code == "orphan" for i in d.issues), "orphan flagged")


@item(
    "Wiring",
    "Enriched mode: per-pin edges + rail nodes (rail:5V, rail:3.3V, rail:GND)",
    "layout.enriched_wiring_layout",
)
def _(x: Ctx) -> str:
    lay = x.c.get(f"/api/projects/{x.pid}/wiring/layout", params={"mode": "enriched"}).json()
    rails = sorted(n for n in lay["nodes"] if n.startswith("rail:"))
    return ok("rail:GND" in rails, ", ".join(rails))


@item(
    "Wiring",
    "Viewer: category / DATA / POWER / GROUND toggles, zoom & pan, simple/enriched switch",
    "viewer.html setupWiring",
)
def _(x: Ctx) -> str:
    html = x.c.get(f"/p/{x.pid}").text
    return ok(
        all(k in html for k in ('data-edge="', 'data-cat="', 'data-zoom="in"', 'data-mode="enriched"', "onwheel")),
        "toggles + zoom + pan + mode",
    )


# =====================================================================================================================
# 7. Mechanical
# =====================================================================================================================
@item("Mech", "Dimension rule, participation, BFS tree, delta solver, bbox", "mech.solve_layout")
def _(x: Ctx) -> str:
    lay = x.project()["content"]["mechLayout"]
    return ok(lay["bbox"]["size"][0] > 0 and lay["tree"], f"bbox {lay['bbox']['size']}")


@item("Mech", "3D view with assembly tree & dimension overlay", "viewer.html Mech tab (three.js)")
def _(x: Ctx) -> str:
    from hwbp.export import render_viewer
    from hwbp.models import ProjectContent

    return ok("setupMech" in render_viewer(ProjectContent.model_validate(x.project()["content"])), "viewer has 3D tab")


@item("Mech", "Explode view", "mech.explode_layout + viewer Explode button")
def _(x: Ctx) -> str:
    ex = x.c.get(f"/api/projects/{x.pid}/mech/explode").json()
    return ok(ex["exploded"] and 'id="explode"' in x.c.get(f"/p/{x.pid}").text, f"bbox {ex['bbox']['size']}")


@item("Mech", "Joints (rigid / revolute / slider with pose) — assembly v2", "mech.derive_joints / apply_joints")
def _(x: Ctx) -> str:
    j = x.c.get(f"/api/projects/{x.pid}/mech/joints").json()["joints"]
    j[0].update(type="revolute", axis=[1, 0, 0], pose=90)
    posed = x.c.post(f"/api/projects/{x.pid}/mech/joints", json={"joints": j}).json()
    return ok(len(posed["parts"][j[0]["child"]]["rotation"]) == 9, f"{len(j)} joints; revolute 90° applied")


@item("Mech", "Per-type visibility toggles in the 3D view", "viewer.html setupMech")
def _(x: Ctx) -> str:
    html = x.c.get(f"/p/{x.pid}").text
    return ok('data-type="' in html and "o.g.visible" in html, "type checkboxes")


# =====================================================================================================================
# 8. CAD & export
# =====================================================================================================================
@item("CAD", "POST /api/cad-jobs/part, GET /cad-jobs/:id, artifacts", "cad.CadJobQueue")
def _(x: Ctx) -> str:
    j = x.c.post("/api/cad-jobs/part", json={"projectId": x.pid, "nodeId": "enclosure_base"}).json()
    return ok(j["status"] == "succeeded", f"artifacts {j['artifacts']}")


@item("CAD", "GET /api/cad-wallet {balance, unlimited}; billing dedupe", "cad.Wallet, billing_key")
def _(x: Ctx) -> str:
    w = x.c.get("/api/cad-wallet", params={"projectId": x.pid}).json()
    return ok("balance" in w and "billable" in w, str(w))


@item("CAD", "GET /api/export/zip (project bundle)", "GET /api/projects/:id/export.zip")
def _(x: Ctx) -> str:
    z = zipfile.ZipFile(io.BytesIO(x.c.get(f"/api/projects/{x.pid}/export.zip").content))
    return ok("project.json" in z.namelist(), f"{len(z.namelist())} files")


@item("CAD", "model.step artifact (STEP export)", "mesh.to_step (faceted B-rep AP214)")
def _(x: Ctx) -> str:
    from steputils import p21

    j = x.c.post("/api/cad-jobs/part", json={"projectId": x.pid, "nodeId": "enclosure_lid"}).json()
    txt = x.c.get(f"/api/cad-jobs/{j['id']}/artifacts/model.step").text
    p21.loads(txt)  # independent ISO-10303-21 parser
    return ok(txt.startswith("ISO-10303-21"), "parsed by steputils")


@item("CAD", "POST /api/export/cad (zip per format with CONTENTS.txt)", "export.export_cad")
def _(x: Ctx) -> str:
    r = x.c.post("/api/export/cad", json={"projectId": x.pid, "formats": ["stl", "step", "glb"]})
    z = zipfile.ZipFile(io.BytesIO(r.content))
    return ok("CONTENTS.txt" in z.namelist(), f"{r.headers['x-downloaded-count']} files")


@item("CAD", "POST /api/export/file (single file export)", "export.export_file")
def _(x: Ctx) -> str:
    from hwbp.export import FILE_KINDS

    sizes = [len(x.c.post("/api/export/file", json={"projectId": x.pid, "kind": k}).content) for k in FILE_KINDS]
    return ok(all(sizes), f"{len(FILE_KINDS)} kinds")


@item("CAD", "CAD credit purchase flow (bundles 20/120/560)", "POST /api/cad-wallet/purchase (simulated payment)")
def _(x: Ctx) -> str:
    before = x.c.get("/api/cad-wallet").json()["balance"]
    after = x.c.post("/api/cad-wallet/purchase", json={"bundle": "medium"}).json()["balance"]
    return ok(after == before + 120, f"{before} → {after} (no payment provider: simulated)")


# =====================================================================================================================
# 9. Account, plans, credits
# =====================================================================================================================
@item("Account", "GET /api/features, /api/capabilities", "api.features / capabilities")
def _(x: Ctx) -> str:
    return ok(x.c.get("/api/features").status_code == 200 and x.c.get("/api/capabilities").status_code == 200, "200")


@item(
    "Account",
    "profile: /api/profile/me, /api/account, set-username, auto-username, set-bio, set-profile-color",
    "services.Accounts",
)
def _(x: Ctx) -> str:
    h = {"X-User-Id": "prof"}
    x.c.post("/api/set-username", json={"username": "prof_x"}, headers=h)
    x.c.post("/api/set-bio", json={"bio": "maker"}, headers=h)
    x.c.post("/api/set-profile-color", json={"color": "#123456"}, headers=h)
    me = x.c.get("/api/profile/me", headers=h).json()
    auto = x.c.post("/api/auto-username", headers={"X-User-Id": "anon"}).json()["username"]
    good = me["username"] == "prof_x" and me["bio"] == "maker" and me["profileColor"] == "#123456"
    return ok(good and auto and x.c.get("/api/account", headers=h).status_code == 200, f"auto: {auto}")


@item("Account", "onboarding/complete, email-preferences", "services.Accounts")
def _(x: Ctx) -> str:
    h = {"X-User-Id": "onb"}
    ob = x.c.post("/api/onboarding/complete", json={"answers": {"role": "student"}}, headers=h).json()["onboarded"]
    prefs = x.c.post("/api/email-preferences", json={"marketing": True}, headers=h).json()
    return ok(ob and prefs["marketing"] is True, "onboarded; prefs saved")


@item(
    "Account",
    "design credits: /api/get-credits, /api/consume-credit, /api/check-subscription, plans free/pro/ultra",
    "services.Accounts credits",
)
def _(x: Ctx) -> str:
    h = {"X-User-Id": "free1"}
    before = x.c.get("/api/get-credits", headers=h).json()["weekly_credits_remaining"]
    after = x.c.post("/api/consume-credit", headers=h).json()["weekly_credits_remaining"]
    return ok(
        before == 10 and after == 9 and x.c.get("/api/check-subscription", headers=h).json()["plan"] == "free",
        "10 → 9 (free plan)",
    )


@item("Account", "/api/contact-leads", "services.Accounts.add_lead")
def _(x: Ctx) -> str:
    return ok(x.c.post("/api/contact-leads", json={"email": "a@b.co", "message": "hi"}).json()["id"], "stored")


not_applicable(
    "Account",
    "Stripe checkout / portal / trial eligibility",
    "needs a payment provider account; the credit ledger is implemented without real payments",
)
not_applicable(
    "Account",
    "/api/conversions/registration, PostHog (/api/surveys, product_tours, web_experiments, early_access_features)",
    "third-party analytics / ad pixels",
)


# =====================================================================================================================
# 10. Web UI
# =====================================================================================================================
@item("UI", "Project page tabs Info / Parts / Wiring / Mech / Instructions (+ Code)", "viewer.html")
def _(x: Ctx) -> str:
    from hwbp.export import render_viewer
    from hwbp.models import ProjectContent

    html = render_viewer(ProjectContent.model_validate(x.project()["content"]))
    return ok(all(f'data-tab="{t}"' in html for t in ("info", "parts", "wiring", "mech", "guide", "code")), "6 tabs")


@item(
    "UI",
    "Home / studio page: prompt, upload image, draw, need-an-idea, live progress, community feed, stats",
    "viewer/studio.html served at /",
)
def _(x: Ctx) -> str:
    h = x.c.get("/").text
    need = [
        'id="prompt"',
        'id="file"',
        'id="draw"',
        'id="ideabtn"',
        "sinceRevision",
        "/api/community",
        "/api/stats",
        "/api/projects",
    ]
    return ok(all(n in h for n in need), "all controls present")


@item("UI", "Parts detail panel (specs, all offers, datasheet/docs, alternatives)", "viewer.html detail()")
def _(x: Ctx) -> str:
    h = x.c.get(f"/p/{x.pid}").text
    return ok("Where to buy" in h and "Datasheet" in h and "Alternatives" in h, "expandable rows")


@item("UI", "Chat panel on the project page (preview → approve / dismiss)", "viewer.html refreshChat/sendChat")
def _(x: Ctx) -> str:
    h = x.c.get(f"/p/{x.pid}").text
    return ok('id="chatform"' in h and "autoApprove:false" in h and "data-op" in h, "chat panel wired to turns API")


@item("UI", "Served project page /p/:id and public share page /s/:token", "api_ext.project_page / share_page")
def _(x: Ctx) -> str:
    tok = x.c.post("/api/publish", json={"projectId": x.pid}).json()["token"]
    return ok(x.c.get(f"/p/{x.pid}").status_code == 200 and x.c.get(f"/s/{tok}").status_code == 200, "200 / 200")


@item("UI", "Star / Copy buttons on shared projects", "viewer.html share mode")
def _(x: Ctx) -> str:
    tok = x.c.post("/api/publish", json={"projectId": x.pid}).json()["token"]
    h = x.c.get(f"/s/{tok}").text
    return ok('id="star"' in h and 'id="copy"' in h, "buttons present")


# =====================================================================================================================
def run(write: bool = False) -> int:
    x = Ctx()
    rows, fails = [], 0
    for it in ITEMS:
        if it.na:
            status, detail = "N/A", it.na
        elif it.check is None:
            status, detail = "MISSING", ""
        else:
            try:
                status, detail = "DONE", it.check(x)
            except Exception as e:  # noqa: BLE001
                status, detail = "FAIL", f"{type(e).__name__}: {e}"
                fails += 1
                traceback.print_exc()
        rows.append((it.area, it.blueprint, it.hwbp, status, detail))
    counts = {s: sum(1 for r in rows if r[3] == s) for s in ("DONE", "FAIL", "MISSING", "N/A")}
    width = max(len(r[1]) for r in rows)
    for r in rows:
        sys.stdout.write(f"{r[3]:8} {r[0]:13} {r[1][:width]:{min(width, 95)}}  {r[4][:60]}\n")
    sys.stdout.write(json.dumps(counts) + "\n")
    if write:
        lines = ["# blueprint.io → hwbp feature parity", "",
                 "Generated by `uv run python scripts/verify_parity.py --write` — every DONE row was verified by an executable check.", "",
                 f"**{counts['DONE']} done · {counts['FAIL']} failing · {counts['MISSING']} missing · {counts['N/A']} not applicable**", "",
                 "| Status | Area | blueprint.io feature | hwbp implementation | Verified / reason |", "|---|---|---|---|---|"]  # fmt: skip
        for area, bp, hw, st, det in rows:
            mark = {"DONE": "✅", "FAIL": "❌", "MISSING": "⬜", "N/A": "➖"}[st]
            lines.append(
                f"| {mark} {st} | {area} | {bp.replace('|', '/')} | {hw.replace('|', '/')} | {det.replace('|', '/')} |"
            )
        e2e = ROOT / "docs" / "e2e" / "results.json"
        if e2e.exists():
            res = json.loads(e2e.read_text())
            lines += ["", "## Browser end-to-end (live server)", "",
                      f"`node scripts/e2e_ui.js http://127.0.0.1:8765 docs/e2e` — **{sum(r['ok'] for r in res)} / {len(res)} passed** "
                      "(screenshots in `docs/e2e/`).", "", "| Result | Check | Observed |", "|---|---|---|"]  # fmt: skip
            lines += [
                f"| {'✅' if r['ok'] else '❌'} | {r['name']} | {(r['detail'] or '').replace('|', '/')[:90]} |"
                for r in res
            ]
        (ROOT / "docs" / "PARITY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(run("--write" in sys.argv))
