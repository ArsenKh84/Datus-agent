"""Server routes added for the web UI: picture analysis preview, project meta, per-part CAD status, site pages."""

import base64

from fastapi.testclient import TestClient

from hwbp.api import create_app
from tests.test_picture_blueprint import device_drawing

PNG = "data:image/png;base64," + base64.b64encode(device_drawing()).decode()


def _project(c: TestClient) -> str:
    run = c.post("/api/generation-runs", json={"prompt": "Portable radar with a touchscreen display, battery powered"})
    return run.json()["projectId"]


def test_image_analysis_preview():
    c = TestClient(create_app(run_async=False))
    img = c.post("/api/generation-input-images", json={"image": PNG}).json()
    a = c.post(f"/api/input-images/{img['id']}/analysis").json()
    assert a["detected"]["buttonCount"] == 3 and len(a["functionOptions"]) == 4 and a["visionModel"] is False
    assert c.post("/api/input-images/nope/analysis").status_code == 404
    bad = c.post(
        "/api/generation-input-images",
        json={"image": base64.b64encode(b"P5\n1 1\n255\n\x00").decode(), "mime": "image/x-portable-graymap"},
    )
    assert bad.status_code == 200
    assert c.post(f"/api/input-images/{bad.json()['id']}/analysis").status_code in (200, 422)


def test_project_meta_role_author_publication():
    c = TestClient(create_app(run_async=False))
    pid = _project(c)
    m = c.get(f"/api/projects/{pid}/meta").json()
    assert m["role"] == "owner" and m["author"]["username"] == "local-user" and m["published"] is None
    c.post("/api/set-username", json={"username": "maker_one"})
    c.post("/api/publish", json={"projectId": pid})
    m = c.get(f"/api/projects/{pid}/meta").json()
    assert m["author"]["username"] == "maker_one" and m["published"]["url"].startswith("/s/")
    assert c.get(f"/api/projects/{pid}/meta", headers={"X-User-Id": "stranger"}).json()["role"] is None
    feed = c.get("/api/community").json()["projects"]
    assert feed[0]["author"]["username"] == "maker_one" and feed[0]["ownerId"] == "local-user"
    c.post("/api/star", json={"token": feed[0]["token"]})
    assert c.get("/api/community/starred").json()["projects"][0]["author"]["username"] == "maker_one"
    assert c.get("/api/trending").json()[0]["author"]["username"] == "maker_one"


def test_project_cad_jobs_status():
    c = TestClient(create_app(run_async=False))
    pid = _project(c)
    s = c.get(f"/api/projects/{pid}/cad-jobs").json()
    assert s["done"] == 0 and len(s["eligible"]) >= 2 and s["jobs"] == {} and s["wallet"]["balance"] > 0
    node = s["eligible"][0]
    job = c.post("/api/cad-jobs/part", json={"projectId": pid, "nodeId": node}).json()
    s = c.get(f"/api/projects/{pid}/cad-jobs").json()
    assert s["done"] == 1 and s["jobs"][node]["id"] == job["id"] and "model.step" in s["jobs"][node]["artifacts"]


def test_site_pages_and_retry_guard():
    c = TestClient(create_app(run_async=False))
    for path in ("/community", "/projects", "/pricing", "/faq", "/about", "/account", "/invitations", "/join"):
        r = c.get(path)
        assert r.status_code == 200 and 'nav class="links"' in r.text, path
    assert 'aria-current="page"' in c.get("/").text
    picture = {"mime": "image/png", "data": PNG.split(",", 1)[1]}
    run = c.post("/api/generation-runs", json={"inputImages": [picture]}).json()
    assert run["status"] == "waiting_for_input"
    r = c.post(f"/api/generation-runs/{run['id']}/retry", json={})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "answer_required"
    assert c.get("/api/stats").json()["projectsCreated"] == 1
