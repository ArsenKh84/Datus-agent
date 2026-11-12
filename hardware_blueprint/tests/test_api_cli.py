import base64
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from hwbp.api import create_app
from hwbp.cli import main
from hwbp.image import encode_png_gray

L_SHAPE = [
    [0.0 if (10 <= x < 70 and 30 <= y < 50) or (10 <= x < 30 and 10 <= y < 70) else 1.0 for x in range(80)]
    for y in range(80)
]
PNG_B64 = base64.b64encode(encode_png_gray(L_SHAPE)).decode()


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app(run_async=False))


@pytest.fixture(scope="module")
def pid(client):
    run = client.post(
        "/api/generation-runs",
        json={"prompt": "Smart plant monitor with soil moisture, OLED display and wifi", "idempotencyKey": "k"},
    ).json()
    assert run["status"] == "completed" and all(run["sections"].values())
    return run["projectId"]


def test_generation_run_protocol(client, pid):
    run = client.post("/api/generation-runs", json={"prompt": "ignored", "idempotencyKey": "k"}).json()
    assert run["projectId"] == pid  # idempotent
    polled = client.get(f"/api/generation-runs/{run['id']}", params={"waitMs": 10, "sinceRevision": 999}).json()
    assert polled["phase"] == "done" and polled["projectData"]["projectName"]
    sse = client.get(f"/api/generation-runs/{run['id']}/events").text
    assert "event: progress" in sse and "event: done" in sse
    assert client.post(f"/api/generation-runs/{run['id']}/cancel").json()["status"] == "completed"
    assert client.post("/api/generation-runs", json={}).status_code == 400
    assert client.get("/api/generation-runs/nope").status_code == 404
    assert client.get("/api/features").json()["engineVersion"] and client.get("/api/capabilities").json()["imageToMesh"]


def test_async_run_long_poll():
    c = TestClient(create_app(run_async=True))
    run = c.post("/api/generation-runs", json={"prompt": "LED lamp with knob"}).json()
    rev, status = -1, run["status"]
    for _ in range(50):
        s = c.get(f"/api/generation-runs/{run['id']}", params={"waitMs": 2000, "sinceRevision": rev}).json()
        rev, status = s["revision"], s["status"]
        if status not in ("queued", "running"):
            break
    assert status == "completed" and s["projectId"]


def test_projects_save_chat_and_conflicts(client, pid):
    head = client.get(f"/api/projects/{pid}/head").json()
    proj = client.get(f"/api/projects/{pid}").json()
    assert proj["version"] == head["version"]
    cls = client.post(f"/api/projects/{pid}/chat/classify", json={"message": "add a buzzer"}).json()
    assert cls["mode"] == "edit" and cls["decisionReceipt"]
    ask = client.post(f"/api/projects/{pid}/chat", json={"message": "why the display?"}).json()
    assert ask["mode"] == "ask" and not ask["applied"] and "Display" in ask["answer"]
    for q in ("how long does the battery last?", "what is the cost?", "tell me something"):
        assert client.post(f"/api/projects/{pid}/chat", json={"message": q}).json()["answer"]
    edit = client.post(
        f"/api/projects/{pid}/chat", json={"message": "add a buzzer", "baseVersion": head["version"]}
    ).json()
    assert edit["applied"] and edit["version"] == head["version"] + 1
    stale = client.post(f"/api/projects/{pid}/chat", json={"message": "add a gps", "baseVersion": head["version"]})
    assert stale.status_code == 409 and stale.json()["detail"]["error"] == "version_conflict"
    cur = client.get(f"/api/projects/{pid}").json()
    saved = client.post(
        "/api/projects/save", json={"projectId": pid, "content": cur["content"], "baseVersion": cur["version"]}
    )
    assert saved.json()["version"] == cur["version"] + 1
    assert (
        client.post(
            "/api/projects/save", json={"projectId": pid, "content": cur["content"], "baseVersion": 0}
        ).status_code
        == 409
    )
    assert (
        client.post("/api/projects/save", json={"projectId": "nope", "content": {}, "baseVersion": 1}).status_code
        == 404
    )
    v = client.get(f"/api/projects/{pid}/versions").json()["versions"]
    assert v[0] == 1 and client.post(f"/api/projects/{pid}/restore/1").json()["version"] == v[-1] + 1
    plan = {"addParts": [{"name": "Buzzer 2", "productName": "Active Buzzer Module"}]}
    dry = client.post(f"/api/projects/{pid}/edit-plan", json={"plan": plan, "dryRun": True}).json()
    assert dry["plan"]["addParts"][0]["id"] == "buzzer_2"
    cur = client.get(f"/api/projects/{pid}").json()
    assert (
        client.post(f"/api/projects/{pid}/edit-plan", json={"plan": plan, "baseVersion": cur["version"]}).json()[
            "version"
        ]
        == cur["version"] + 1
    )
    assert client.post(f"/api/projects/{pid}/edit-plan", json={"plan": plan, "baseVersion": 1}).status_code == 409
    assert client.post(f"/api/projects/{pid}/chat", json={"message": "add a gps"}).json()["applied"]
    assert client.get("/api/projects/nope").status_code == 404


def test_parts_bom_export_publish(client, pid):
    assert client.get("/api/parts/search", params={"q": "oled"}).json()["results"][0]["id"] == "ssd1306-096"
    assert client.get("/api/parts/bme280").json()["mpn"] == "BME280" and client.get("/api/parts/zzz").status_code == 404
    assert client.post("/api/parts/canonicalize", json={"mpn": "SG90"}).json()["catalogId"] == "sg90-servo"
    enr = client.post(
        "/api/parts/enrich-transient", json={"name": "Mystery Hall Sensor", "pins": ["VCC", "GND", "OUT"]}
    ).json()
    assert enr["classification"]["type"] == "sensor" and enr["researchQueries"]
    s = client.get(f"/api/projects/{pid}/suggestions").json()["suggestions"]
    assert s and client.get(f"/api/projects/{pid}/suggestions", params={"nodeId": "zzz"}).status_code == 404
    assert client.get(f"/api/projects/{pid}/suggestions", params={"nodeId": "display"}).json()["suggestions"]
    bom = client.get(f"/api/projects/{pid}/bom", params={"buildQty": 10}).json()
    assert bom["buildQty"] == 10 and bom["totalUsd"] > 0
    assert client.get(f"/api/projects/{pid}/bom.csv").text.startswith("node_id")
    assert client.get(f"/api/projects/{pid}/wiring.svg", params={"dark": True}).text.startswith("<svg")
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/projects/{pid}/export.zip").content))
    assert "viewer.html" in z.namelist()
    tok = client.post("/api/publish", json={"projectId": pid}).json()["token"]
    assert client.get("/api/published", params={"token": tok}).json()["publishedProjectId"] == pid
    assert client.get("/api/published/head", params={"token": tok}).json()["contentRevision"] >= 1
    assert client.get("/api/published", params={"token": "bad"}).status_code == 404
    rails = client.post("/api/wiring/infer-rails", json={"electricalNodes": [{"id": "a", "pins": ["GND"]}, {"id": "b", "pins": ["GND"]}],
                                                         "connections": [{"type": "power", "source": "a", "target": "b"}]}).json()  # fmt: skip
    assert rails["addElectricalConnections"][0]["type"] == "ground"


def test_cad_endpoints(client, pid):
    wallet = client.get("/api/cad-wallet", params={"projectId": pid}).json()
    assert wallet["eligible"] >= wallet["billable"] > 0
    job = client.post("/api/cad-jobs/part", json={"projectId": pid, "nodeId": "enclosure_base"}).json()
    assert job["status"] == "succeeded"
    assert client.get(f"/api/cad-jobs/{job['id']}").json()["artifacts"] == [
        "model.glb",
        "model.obj",
        "model.step",
        "model.stl",
    ]
    stl = client.get(f"/api/cad-jobs/{job['id']}/artifacts/model.stl")
    assert stl.headers["content-type"] == "model/stl" and len(stl.content) > 84
    assert client.get(f"/api/cad-jobs/{job['id']}/artifacts/nope").status_code == 404
    assert client.get("/api/cad-jobs/nope").status_code == 404
    assert client.post("/api/cad-jobs/part", json={"projectId": pid, "nodeId": "fastener_kit"}).status_code == 422
    assert client.post("/api/cad-jobs/part", json={"projectId": pid, "nodeId": "zzz"}).status_code == 404
    app = create_app(run_async=False)
    c2 = TestClient(app)
    p2 = c2.post("/api/generation-runs", json={"prompt": "LED lamp with knob, wall powered"}).json()["projectId"]
    app.state.hwbp.wallet.balance = 0
    assert c2.post("/api/cad-jobs/part", json={"projectId": p2, "nodeId": "enclosure_base"}).status_code == 402


def test_picture_endpoints(client):
    r = client.post(
        "/api/image-to-mesh", json={"image": PNG_B64, "mode": "extrude", "widthMm": 60, "heightMm": 5}
    ).json()
    assert r["report"]["watertight"] and base64.b64decode(r["data"])[80:84]
    for fmt in ("glb", "obj"):
        assert client.post("/api/image-to-mesh", json={"image": PNG_B64, "format": fmt}).json()["format"] == fmt
    assert client.post("/api/image-to-mesh", json={"image": PNG_B64, "mode": "bad"}).status_code == 422
    run = client.post("/api/generation-runs", json={"prompt": "desk clock with display and wifi",
                                                     "inputImages": [{"mime": "image/png", "data": PNG_B64}]}).json()  # fmt: skip
    assert run["status"] == "completed" and run["projectData"]["requirements"]["form_factor"][
        "aspect"
    ] == pytest.approx(1.0, rel=0.05)


def test_failed_run_is_reported():
    from hwbp.engine import Engine

    class Boom(Engine):
        def run(self, *a, **k):
            raise RuntimeError("kaput")
            yield  # pragma: no cover

    c = TestClient(create_app(engine=Boom(), run_async=False))
    run = c.post("/api/generation-runs", json={"prompt": "x"}).json()
    assert run["status"] == "failed" and "kaput" in run["failure"]


def test_cli(tmp_path, capsys):
    out = tmp_path / "o"
    img = tmp_path / "shape.png"
    img.write_bytes(encode_png_gray(L_SHAPE))
    assert main(["generate", "desk lamp with LED strip and knob", "--image", str(img), "-o", str(out)]) == 0
    assert {"project.json", "wiring.svg", "guide.md", "export.zip", "viewer.html"} <= {p.name for p in out.iterdir()}
    assert main(["layout", str(out / "project.json")]) == 0
    assert '"bbox"' in capsys.readouterr().out
    assert main(["bom", str(out / "project.json"), "--qty", "3", "--estimates"]) == 0
    assert capsys.readouterr().out.startswith("node_id")
    for suffix, magic in ((".stl", None), (".glb", b"glTF"), (".obj", b"#")):
        target = tmp_path / f"part{suffix}"
        assert main(["image2mesh", str(img), "-o", str(target), "--mode", "extrude"]) == 0
        assert json.loads(capsys.readouterr().out.strip())["watertight"]
        if magic:
            assert target.read_bytes().startswith(magic)


def test_picture_only_run_needs_vision_model():
    c = TestClient(create_app(run_async=False))
    r = c.post("/api/generation-runs", json={"inputImages": [{"mime": "image/png", "data": PNG_B64}]})
    assert r.status_code == 400 and "vision model" in r.json()["detail"]

    class Vision:
        def describe(self, image, mime, instruction):
            return {"name": "Clock", "prompt": "desk clock with OLED display and wifi", "summary": "an L-shaped clock"}

    c2 = TestClient(create_app(run_async=False, vision=Vision()))
    run = c2.post("/api/generation-runs", json={"inputImages": [{"mime": "image/png", "data": PNG_B64}]}).json()
    assert run["status"] == "completed" and "ui.display" in run["projectData"]["requirements"]["capabilities"]
