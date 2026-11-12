import base64
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from hwbp.api import create_app
from hwbp.image import encode_png_gray

L_SHAPE = [
    [0.0 if (10 <= x < 70 and 30 <= y < 50) or (10 <= x < 30 and 10 <= y < 70) else 1.0 for x in range(80)]
    for y in range(80)
]
PNG_B64 = base64.b64encode(encode_png_gray(L_SHAPE)).decode()
BOB = {"X-User-Id": "bob"}
EVE = {"X-User-Id": "eve"}


@pytest.fixture()
def env():
    app = create_app(run_async=False)
    c = TestClient(app)
    run = c.post(
        "/api/generation-runs", json={"prompt": "Smart plant monitor with soil moisture, OLED display and wifi"}
    ).json()
    return app, c, run, run["projectId"]


def test_run_followups_images_and_listing(env):
    app, c, run, pid = env
    img = c.post(
        "/api/generation-input-images", json={"image": "data:image/png;base64," + PNG_B64, "draftKey": "draft-1"}
    ).json()
    assert img["mimeType"] == "image/png" and c.get(img["url"]).content.startswith(b"\x89PNG")
    r = c.post("/api/generation-runs", json={"prompt": "desk clock with display", "inputImages": [img["id"]]}).json()
    assert r["imageAnalysis"]["silhouette"]["aspect"] > 0
    assert c.post("/api/generation-runs", json={"prompt": "x", "inputImages": ["missing"]}).status_code == 404
    assert c.post("/api/generation-input-images", json={"image": ""}).status_code == 400
    assert c.get("/api/input-images/zzz").status_code == 404
    q = run["questionRounds"][0]["questions"]
    stale = c.post(f"/api/generation-runs/{run['id']}/questions", json={"revision": 0, "qa": []})
    assert stale.status_code == 409
    again = c.post(
        f"/api/generation-runs/{run['id']}/questions",
        json={"revision": run["revision"], "qa": [{"question": q[0]["question"], "answer": "battery"}]},
    ).json()
    assert (
        again["status"] == "completed"
        and again["projectId"] == pid
        and again["projectData"]["requirements"]["power_source"] == "battery"
    )
    assert c.get(f"/api/projects/{pid}").json()["version"] == 2  # regenerated into the same project
    retried = c.post(f"/api/generation-runs/{run['id']}/retry", json={}).json()
    assert retried["status"] == "completed"
    assert c.post(f"/api/generation-runs/{run['id']}/retry", json={"revision": 0}).status_code == 409
    assert c.post("/api/generation-runs/zzz/retry", json={}).status_code == 404
    runs = c.get("/api/generation-runs").json()["runs"]
    assert len(runs) == 2 and "projectData" not in runs[0]  # the missing-image request never created a run
    assert c.get("/api/generation-runs", params={"recoverable": True}).json()["runs"] == []
    assert c.get("/api/generation-runs", headers=BOB).json()["runs"] == []


def test_free_plan_credits_gate_generation():
    c = TestClient(create_app(run_async=False))
    assert c.get("/api/get-credits", headers=BOB).json()["weekly_credits_remaining"] == 10
    for _ in range(10):
        assert c.post("/api/consume-credit", headers=BOB).status_code == 200
    r = c.post("/api/generation-runs", json={"prompt": "lamp"}, headers=BOB)
    assert r.status_code == 402 and r.json()["detail"]["error"] == "no_credits"
    assert c.post("/api/billing/plan", json={"plan": "pro"}, headers=BOB).json()["simulated"]
    assert c.get("/api/check-subscription", headers=BOB).json() == {"plan": "pro", "active": True}
    assert c.post("/api/generation-runs", json={"prompt": "lamp"}, headers=BOB).json()["status"] == "completed"


def test_projects_list_permissions_and_chat_turns(env):
    app, c, run, pid = env
    assert [p["id"] for p in c.get("/api/projects").json()["projects"]] == [pid]
    assert c.get("/api/projects", headers=BOB).json()["projects"] == []
    content = c.get(f"/api/projects/{pid}").json()["content"]
    assert (
        c.post(
            "/api/projects/save", json={"projectId": pid, "content": content, "baseVersion": 1}, headers=BOB
        ).status_code
        == 403
    )
    assert c.post(f"/api/projects/{pid}/chat", json={"message": "add a buzzer"}, headers=BOB).status_code == 403
    # preview -> approve
    t = c.post(f"/api/projects/{pid}/chat", json={"message": "add a buzzer", "autoApprove": False}).json()
    assert not t["applied"] and t["turn"]["status"] == "awaiting_approval"
    plan = t["turn"]["plan"]
    assert any(p["id"].startswith("buzzer") for p in plan["addParts"]) and plan["kind"] == "plan"
    snap = c.get(f"/api/projects/{pid}/chat").json()
    assert snap["turns"][-1]["id"] == t["turn"]["id"] and snap["version"] == 1
    ok = c.post(f"/api/projects/{pid}/chat/{t['turn']['id']}/approve", json={}).json()
    assert ok["turn"]["status"] == "completed" and ok["version"] == 2
    assert c.post(f"/api/projects/{pid}/chat/{t['turn']['id']}/approve", json={}).status_code == 409
    # preview -> outdated after someone else saves
    t2 = c.post(f"/api/projects/{pid}/chat", json={"message": "add a gps", "autoApprove": False}).json()["turn"]
    cur = c.get(f"/api/projects/{pid}").json()
    c.post("/api/projects/save", json={"projectId": pid, "content": cur["content"], "baseVersion": cur["version"]})
    assert c.post(f"/api/projects/{pid}/chat/{t2['id']}/approve", json={}).json()["turn"]["status"] == "outdated"
    retried = c.post(f"/api/projects/{pid}/chat/{t2['id']}/retry", json={}).json()["turn"]
    assert retried["status"] == "awaiting_approval"
    assert c.post(f"/api/projects/{pid}/chat/{t2['id']}/dismiss", json={}).json()["turn"]["status"] == "cancelled"
    assert c.post(f"/api/projects/{pid}/chat/{t2['id']}/fly", json={}).status_code == 400
    assert c.post(f"/api/projects/{pid}/chat/{t2['id']}/approve", json={"revision": 0}).status_code == 409
    ask = c.post(f"/api/projects/{pid}/chat", json={"message": "why the display?"}).json()
    assert c.post(f"/api/projects/{pid}/chat/{ask['turn']['id']}/retry", json={}).status_code == 400
    clar = c.post(f"/api/projects/{pid}/chat", json={"message": "make it nicer"}).json()
    assert clar["turn"]["resultKind"] in ("clarification", "answer")
    assert c.post(f"/api/projects/{pid}/chat/clear", json={"baseVersion": 0}).status_code == 409
    cleared = c.post(f"/api/projects/{pid}/chat/clear", json={}).json()
    assert cleared["historyEpoch"] == 1 and c.get(f"/api/projects/{pid}/chat").json()["turns"] == []
    assert c.get(f"/api/projects/{pid}/head").json()["historyEpoch"] == 1


def test_websocket_events(env):
    app, c, run, pid = env
    with c.websocket_connect(f"/api/projects/{pid}/events") as ws:
        ws.send_json({"type": "authenticate", "userId": "local-user"})
        assert ws.receive_json()["type"] == "ready"
        assert ws.receive_json() == {"type": "presence", "users": ["local-user"]}
        cur = c.get(f"/api/projects/{pid}").json()
        c.post("/api/projects/save", json={"projectId": pid, "content": cur["content"], "baseVersion": cur["version"]})
        assert ws.receive_json() == {"type": "project_changed", "version": 2}
        c.put(f"/api/projects/{pid}/readme", json={"text": "hi", "baseVersion": 0})
        assert ws.receive_json()["type"] == "readme_changed"
        ws.send_text("ping")
        assert ws.receive_json() == {"type": "pong"}
    with c.websocket_connect("/api/projects/nope/events") as ws:
        assert ws.receive_json() == {"type": "error", "error": "not_found"}


def test_readme_collaboration_and_sharing(env):
    app, c, run, pid = env
    assert c.get(f"/api/projects/{pid}/readme").json()["version"] == 0
    assert c.put(f"/api/projects/{pid}/readme", json={"text": "notes", "baseVersion": 0}).json()["version"] == 1
    conflict = c.put(f"/api/projects/{pid}/readme", json={"text": "x", "baseVersion": 0})
    assert conflict.status_code == 409 and conflict.json()["detail"]["error"] == "readme_conflict"
    assert c.get(f"/api/projects/{pid}/readme", headers=BOB).status_code == 403
    inv = c.post(f"/api/projects/{pid}/invitations", json={"email": "bob@x.io", "role": "editor"}).json()
    assert c.get(f"/api/projects/{pid}/invitations").json()["invitations"][0]["email"] == "bob@x.io"
    assert c.post(f"/api/projects/{pid}/invitations", json={"email": "bob@x.io", "role": "viewer"}).status_code == 429
    assert c.post("/api/project-invitations/accept", json={"token": inv["token"]}, headers=BOB).json() == {
        "projectId": pid,
        "role": "editor",
    }
    assert c.post("/api/project-invitations/accept", json={"token": "bad"}, headers=BOB).status_code == 404
    assert (
        c.put(f"/api/projects/{pid}/readme", json={"text": "bob edit", "baseVersion": 1}, headers=BOB).json()["version"]
        == 2
    )
    link = c.post(f"/api/projects/{pid}/share-links", json={"role": "viewer"}).json()
    assert c.get(f"/api/projects/{pid}/share-links").json()["shareLinks"][0]["id"] == link["id"]
    assert c.post("/api/share-links/redeem", json={"token": link["token"]}, headers=EVE).json()["role"] == "viewer"
    members = {m["userId"]: m["role"] for m in c.get(f"/api/projects/{pid}/members").json()["members"]}
    assert members == {"local-user": "owner", "bob": "editor", "eve": "viewer"}
    assert c.patch(f"/api/projects/{pid}/members/eve", json={"role": "editor"}, headers=BOB).status_code == 403
    assert c.patch(f"/api/projects/{pid}/members/eve", json={"role": "editor"}).status_code == 200
    assert c.patch(f"/api/projects/{pid}/members/local-user", json={"role": "viewer"}).status_code == 400
    assert c.delete(f"/api/projects/{pid}/members/eve").status_code == 200
    assert c.delete(f"/api/projects/{pid}/share-links/{link['id']}").json()["revoked"]
    assert c.post("/api/share-links/redeem", json={"token": link["token"]}, headers=EVE).status_code == 404
    inv2 = c.post(f"/api/projects/{pid}/invitations", json={"email": "z@x.io", "role": "viewer"}).json()
    assert c.delete(f"/api/projects/{pid}/invitations/{inv2['id']}").json()["revoked"]
    assert pid in [p["id"] for p in c.get("/api/projects", headers=BOB).json()["projects"]]


def test_community_flow(env):
    app, c, run, pid = env
    assert c.post("/api/publish", json={"projectId": pid}, headers=BOB).status_code == 403
    tok = c.post("/api/publish", json={"projectId": pid}).json()["token"]
    assert c.get("/api/community").json()["total"] == 1
    assert c.get("/api/community", params={"sort": "nope"}).status_code == 400
    assert c.get("/api/community/suggest", params={"q": "smart"}).json()["suggestions"]
    assert c.post("/api/star", json={"token": tok}, headers=BOB).json() == {"starred": True, "starCount": 1}
    assert c.get("/api/community/starred", headers=BOB).json()["projects"][0]["token"] == tok
    assert c.post("/api/star", json={"token": "bad"}).status_code == 404
    assert c.get("/api/trending", params={"limit": 2}).json()[0]["token"] == tok
    stats = c.get("/api/stats").json()
    assert stats["publishedProjects"] == 1 and stats["projectsCreated"] >= 1 and stats["accountsCreated"] >= 1
    copied = c.post("/api/projects/copy", json={"token": tok}, headers=BOB).json()
    assert c.get(f"/api/projects/{copied['projectId']}").json()["content"]["projectName"].endswith("(copy)")
    assert c.post("/api/unpublish", json={"projectId": pid}).json() == {"unpublished": True}
    assert c.get("/api/published", params={"token": tok}).status_code == 404


def test_parts_links_images(env):
    app, c, run, pid = env
    data = c.post("/api/parts/nexar-sourcing", json={"partIds": ["bme280", "ghost"]}).json()["data"]
    assert list(data) == ["bme280"] and data["bme280"]["sourcing"] and data["bme280"]["cadFiles"]
    for kind in ("amazon", "affiliate", "adafruit"):
        links = c.post(f"/api/parts/{kind}-links", json={"partIds": ["sg90-servo"]}).json()["links"]
        assert "SG90" in links["sg90-servo"]
    url = c.get("/api/parts/image", params={"name": "ESP32", "type": "mcu"}).json()["imageUrl"]
    assert c.get(url).text.startswith("<svg")
    assert c.get("/api/parts/bme280/photo").headers["content-type"].startswith("image/svg")
    assert c.get("/api/parts/ghost/photo").status_code == 404


def test_ai_actions_mech_exports(env):
    app, c, run, pid = env
    assert c.post("/api/ideas", json={"answers": {"interests": ["garden"]}}).json()["projects"]
    assert c.get(f"/api/projects/{pid}/suggested-prompts", params={"mode": "edit"}).json()["prompts"]
    r = c.post(f"/api/projects/{pid}/part-descriptions/refresh").json()
    assert r["updated"] > 0 and r["version"] == 2
    content = c.get(f"/api/projects/{pid}").json()["content"]
    step = content["instructionSteps"][1]["subSteps"][0]
    assert c.get(f"/api/projects/{pid}/instructions/wire/{step['id']}/detail").json()["tip"]
    assert c.get(f"/api/projects/{pid}/instructions/wire/nope/detail").status_code == 404
    assert c.get(f"/api/projects/{pid}/steps/wire/{step['id']}/image.svg").text.startswith("<svg")
    assert c.get(f"/api/projects/{pid}/steps/wire/nope/image.svg").status_code == 404
    hero0 = c.get(f"/api/projects/{pid}/image.svg").text
    rr = c.post(f"/api/projects/{pid}/image-rerolls").json()
    assert rr["variant"] == 1 and c.get(rr["url"]).text != hero0
    assert c.post(f"/api/projects/{pid}/image-rerolls/{rr['id']}/apply").json()["variant"] == 1
    assert c.post(f"/api/projects/{pid}/image-rerolls/bogus/apply").status_code == 404
    assert c.get(f"/api/projects/{pid}").json()["content"]["projectImageHash"] == rr["hash"]
    heal = c.post(f"/api/projects/{pid}/mech/heal").json()
    assert heal["orphansAfter"] == []
    assert c.post(f"/api/projects/{pid}/mech/enrich").json()["failed"] == 0
    assert c.get(f"/api/projects/{pid}/mech/explode").json()["exploded"]
    joints = c.get(f"/api/projects/{pid}/mech/joints").json()["joints"]
    joints[0].update(type="slider", pose=10)
    posed = c.post(f"/api/projects/{pid}/mech/joints", json={"joints": joints, "save": True}).json()
    assert posed["version"] > 1 and c.get(f"/api/projects/{pid}/mech/joints").json()["joints"][0]["pose"] == 10
    assert (
        c.post(
            f"/api/projects/{pid}/mech/joints", json={"joints": [{"parent": "a", "child": "b", "type": "ball"}]}
        ).status_code
        == 400
    )
    assert c.get(f"/api/projects/{pid}/wiring/layout", params={"mode": "enriched"}).json()["mode"] == "enriched"
    assert c.get(f"/api/projects/{pid}/wiring/layout").json()["nodes"]
    assert c.get(f"/api/projects/{pid}/wiring/layout", params={"mode": "x"}).status_code == 400
    assert c.get(f"/api/projects/{pid}/wiring-enriched.svg").text.startswith("<svg")
    assert c.get(f"/api/projects/{pid}/bom/merged").json()["rows"]
    cad = c.post("/api/export/cad", json={"projectId": pid, "formats": ["stl", "step"]})
    assert (
        int(cad.headers["x-downloaded-count"]) > 0
        and "CONTENTS.txt" in zipfile.ZipFile(io.BytesIO(cad.content)).namelist()
    )
    assert c.post("/api/export/cad", json={"projectId": pid, "formats": ["iges"]}).status_code == 400
    assert c.post("/api/export/file", json={"projectId": pid, "kind": "bom.csv"}).text.startswith("node_id")
    assert c.post("/api/export/file", json={"projectId": pid, "kind": "x"}).status_code == 400
    assert zipfile.ZipFile(io.BytesIO(c.post("/api/export/zip", json={"projectId": pid}).content)).namelist()
    job = c.post("/api/cad-jobs/part", json={"projectId": pid, "nodeId": "enclosure_base"}).json()
    assert c.get(f"/api/cad-jobs/{job['id']}/artifacts/model.step").text.startswith("ISO-10303-21")
    assert c.get(f"/api/projects/{pid}/head").json()["cadRevision"] == 1
    assert c.post("/api/cad-wallet/purchase", json={"bundle": "small"}).json()["simulated"]
    assert c.post("/api/cad-wallet/purchase", json={"bundle": "huge"}).status_code == 400


def test_account_endpoints(env):
    app, c, run, pid = env
    assert c.get("/api/profile/me", headers=BOB).json()["plan"] == "free"
    assert (
        c.post("/api/set-username", json={"username": "bob_builder"}, headers=BOB).json()["username"] == "bob_builder"
    )
    assert c.post("/api/set-username", json={"username": "bob_builder"}, headers=EVE).status_code == 409
    assert c.post("/api/auto-username", headers=EVE).json()["username"]
    assert c.post("/api/set-bio", json={"bio": "maker"}, headers=BOB).json()["bio"] == "maker"
    assert c.post("/api/set-profile-color", json={"color": "#112233"}, headers=BOB).json()["profileColor"] == "#112233"
    assert c.post("/api/set-profile-color", json={"color": "blue"}, headers=BOB).status_code == 400
    assert c.post("/api/onboarding/complete", json={"answers": {"role": "student"}}, headers=BOB).json()["onboarded"]
    assert c.post("/api/email-preferences", json={"marketing": True}, headers=BOB).json()["marketing"] is True
    assert c.get("/api/email-preferences", headers=BOB).json()["marketing"] is True
    acct = c.get("/api/account").json()
    assert acct["projects"] == 1 and acct["credits"]["unlimited"]
    assert c.post("/api/contact-leads", json={"email": "a@b.co", "message": "hi"}).json()["id"]
    assert c.post("/api/contact-leads", json={"email": "nope"}).status_code == 400
    assert c.post("/api/consume-credit", headers=EVE).json()["weekly_credits_remaining"] == 9
    assert c.post("/api/get-credits", headers=EVE).json()["weekly_credits_remaining"] == 9


def test_served_pages(env):
    app, c, run, pid = env
    home = c.get("/")
    assert home.status_code == 200 and "Hardware Blueprint Studio" in home.text and "/api/generation-runs" in home.text
    page = c.get(f"/p/{pid}").text
    assert '"mode": "project"' in page and "window.HWBP_PROJECT = {" in page
    tok = c.post("/api/publish", json={"projectId": pid}).json()["token"]
    share = c.get(f"/s/{tok}").text
    assert '"mode": "share"' in share and tok in share
    assert c.get("/p/nope").status_code == 404 and c.get("/s/nope").status_code == 404
