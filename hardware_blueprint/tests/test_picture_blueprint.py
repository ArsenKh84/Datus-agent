"""Picture -> blueprint: offline front-panel detection, the 'what should it do?' flow, and the Claude adapter."""

import base64
import json
import math
import random

import pytest
from fastapi.testclient import TestClient

from hwbp.api import create_app
from hwbp.catalog import default_catalog
from hwbp.cli import main
from hwbp.image import (
    analyze_picture,
    brief_from_image,
    compose_prompt,
    encode_png_gray,
    feature_phrase,
    function_options,
)
from hwbp.vision import BRIEF_SCHEMA, ClaudeVision, VisionError, vision_from_env

W, H = 480, 360


def _canvas():
    return [[1.0] * W for _ in range(H)]


def _rect_stroke(c, x0, y0, x1, y1, t=10):
    for y in range(y0, y1):
        for x in range(x0, x1):
            if x < x0 + t or x >= x1 - t or y < y0 + t or y >= y1 - t:
                c[y][x] = 0.05


def _circle_stroke(c, cx, cy, r, t=6):
    for y in range(cy - r - 1, cy + r + 2):
        for x in range(cx - r - 1, cx + r + 2):
            if r - t <= math.hypot(x - cx, y - cy) <= r:
                c[y][x] = 0.05


def _fill_rect(c, x0, y0, x1, y1, v):
    for y in range(y0, y1):
        for x in range(x0, x1):
            c[y][x] = v


def _fill_circle(c, cx, cy, r, v):
    for y in range(cy - r, cy + r + 1):
        for x in range(cx - r, cx + r + 1):
            if math.hypot(x - cx, y - cy) <= r:
                c[y][x] = v


def device_drawing() -> bytes:
    """Outline drawing (studio DRAW pad style): device border, a display rectangle and three round buttons."""
    c = _canvas()
    _rect_stroke(c, 40, 40, 440, 320)
    _rect_stroke(c, 80, 70, 300, 230, 8)
    for cx in (120, 200, 280):
        _circle_stroke(c, cx, 275, 16)
    return encode_png_gray(c)


def silhouette_with_cutouts() -> bytes:
    c = _canvas()
    _fill_rect(c, 40, 40, 440, 320, 0.1)
    _fill_rect(c, 100, 70, 360, 220, 1.0)  # display window
    _fill_circle(c, 150, 270, 14, 1.0)  # buttons
    _fill_circle(c, 220, 270, 14, 1.0)
    _fill_rect(c, 380, 150, 395, 230, 1.0)  # long slot
    _fill_circle(c, 310, 268, 40, 1.0)  # medium round opening (~4.5% of the front)
    return encode_png_gray(c)


def plain_outline() -> bytes:
    c = _canvas()
    _rect_stroke(c, 60, 60, 420, 300)
    return encode_png_gray(c)


def photo_like() -> bytes:
    random.seed(1)
    c = [
        [
            min(1, max(0, 0.5 + 0.3 * math.sin(x / 40) * math.cos(y / 50) + random.uniform(-0.15, 0.15)))
            for x in range(W)
        ]
        for y in range(H)
    ]
    _fill_rect(c, 150, 100, 330, 260, 0.15)
    return encode_png_gray(c)


# ------------------------------------------------------------------ offline analysis
def test_drawing_with_display_and_buttons():
    a = analyze_picture(device_drawing())
    assert a["kind"] == "drawing" and a["shape"] == "rectangular" and a["outlineDrawing"]
    assert a["capabilities"] == ["display", "buttons"] and a["buttonCount"] == 3
    kinds = [f["kind"] for f in a["features"]]
    assert kinds.count("display_window") == 1 and kinds.count("button") == 3
    disp = a["features"][0]
    assert disp["kind"] == "display_window" and disp["sizeMm"][0] == pytest.approx(55, abs=3)
    assert 0.25 < a["displayFraction"] < 0.4
    assert "display window" in a["summary"] and "3 push buttons" in a["summary"]
    assert feature_phrase(a) == " with a display and 3 buttons"


def test_silhouette_cutouts_and_questions():
    a = analyze_picture(silhouette_with_cutouts())
    kinds = sorted(f["kind"] for f in a["features"])
    assert kinds == ["button", "button", "display_window", "round_opening", "slot"]
    assert a["buttonCount"] == 2
    joined = " ".join(a["uncertainties"])
    assert "USB port" in joined and "rotary knob" in joined


def test_plain_outline_and_photo_add_no_parts():
    a = analyze_picture(plain_outline())
    assert a["features"] == [] and a["capabilities"] == [] and feature_phrase(a) == ""
    p = analyze_picture(photo_like())
    assert p["kind"] == "photo" and p["capabilities"] == [] and p["features"] == []
    assert "vision model" in p["uncertainties"][0]


def test_function_options_follow_the_picture():
    assert "display and 3 buttons" in function_options(analyze_picture(device_drawing()))[0]
    assert function_options({"capabilities": ["display"]})[0].startswith("A desk clock")
    assert "remote control with 2 buttons" in function_options({"capabilities": ["buttons"], "buttonCount": 2})[0]
    assert "rotary knob" in function_options({"shape": "round", "aspect": 1.0})[0]
    assert "light bar" in function_options({"shape": "rectangular", "aspect": 4.0})[0]
    assert function_options(None)[0].startswith("A temperature")


def test_compose_prompt_adds_missing_parts_only():
    det = {"capabilities": ["display", "buttons"], "buttonCount": 3}
    assert compose_prompt("weather station", det) == "weather station with a display and 3 buttons"
    assert (
        compose_prompt("weather station with a screen and buttons", det) == "weather station with a screen and buttons"
    )
    assert compose_prompt("lamp", None) == "lamp"


def test_brief_offline_drawing():
    b = brief_from_image(device_drawing())
    assert b.needs_function and b.source == "picture" and b.detected["buttonCount"] == 3
    assert b.analysis["uncertainties"][0].startswith("What should the device do")
    b = brief_from_image(device_drawing(), user_prompt="weather station")
    assert not b.needs_function and b.prompt == "weather station with a display and 3 buttons"
    assert b.analysis["components"][1] == "3 push buttons"


# ------------------------------------------------------------------ API flow
def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_picture_only_run_waits_then_generates_with_drawn_parts():
    c = TestClient(create_app(run_async=False))
    run = c.post("/api/generation-runs", json={"inputImages": [{"mime": "image/png", "data": _b64(device_drawing())}]})
    run = run.json()
    assert run["status"] == "waiting_for_input" and run["phase"] == "questions" and run["projectId"] is None
    q = run["questionRounds"][0]["questions"][0]
    assert q["id"] == "device_function" and q["freeText"] and len(q["options"]) == 4
    assert run["imageAnalysis"]["detected"]["buttonCount"] == 3 and "silhouette" in run["imageAnalysis"]
    bad = c.post(f"/api/generation-runs/{run['id']}/questions", json={"revision": run["revision"], "qa": []})
    assert bad.status_code == 422 and bad.json()["detail"]["error"] == "answer_required"
    done = c.post(
        f"/api/generation-runs/{run['id']}/questions",
        json={"revision": run["revision"], "qa": [{"id": "device_function", "answer": "weather station"}]},
    ).json()
    assert done["status"] == "completed" and done["projectId"]
    req = done["projectData"]["requirements"]
    assert req["prompt"] == "weather station with a display and 3 buttons"
    assert "ui.display" in req["capabilities"] and req["counts"]["input.button"] == 3
    assert req["form_factor"]["displayFraction"] > 0.3
    types = [n["type"] for n in done["projectData"]["nodes"]]
    assert "display" in types
    display = next(n for n in done["projectData"]["nodes"] if n["type"] == "display")
    assert "display.small" not in default_catalog().get(display["partId"]).capabilities  # big window → no 0.96" OLED


def test_prompt_plus_drawing_adds_detected_parts():
    c = TestClient(create_app(run_async=False))
    run = c.post(
        "/api/generation-runs",
        json={"prompt": "smart thermostat", "inputImages": [{"mime": "image/png", "data": _b64(device_drawing())}]},
    ).json()
    assert run["status"] == "completed"
    assert run["projectData"]["requirements"]["prompt"] == "smart thermostat with a display and 3 buttons"


def test_failing_vision_model_falls_back_offline():
    class Broken:
        def describe(self, image, mime, instruction):
            raise ConnectionError("no network")

    c = TestClient(create_app(run_async=False, vision=Broken()))
    run = c.post("/api/generation-runs", json={"inputImages": [{"mime": "image/png", "data": _b64(device_drawing())}]})
    run = run.json()
    assert run["status"] == "waiting_for_input"
    assert run["imageAnalysis"]["uncertainties"][0].startswith("Vision model unavailable (ConnectionError)")


# ------------------------------------------------------------------ Claude adapter (real SDK, mocked transport)
anthropic = pytest.importorskip("anthropic")
httpx2 = pytest.importorskip("httpx2")


def _claude(handler):
    client = anthropic.Anthropic(
        api_key="test-key",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    return ClaudeVision(client=client)


def _reply(text: str, stop: str = "end_turn") -> dict:
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}], "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }  # fmt: skip


BRIEF = {
    "name": "Pocket Weather", "prompt": "Handheld weather station with an OLED display, BME280 and a battery",
    "summary": "a small handheld box with a screen", "components": ["OLED", "buttons"], "uncertainties": ["runtime"],
    "features": ["display"], "approxSizeMm": {"w": 80, "d": 50, "h": 20}, "materials": ["PLA"], "confidence": 0.8,
}  # fmt: skip


def test_claude_vision_request_shape_and_parse():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["beta"] = request.headers.get("anthropic-beta")
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json=_reply(json.dumps(BRIEF)))

    out = _claude(handler).describe(device_drawing(), "image/png", "describe it")
    assert out == BRIEF
    body = seen["body"]
    assert seen["url"].endswith("/v1/messages") and "server-side-fallback-2026-07-01" in seen["beta"]
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default"
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": BRIEF_SCHEMA}
    img, txt = body["messages"][0]["content"]
    assert img["type"] == "image" and img["source"]["media_type"] == "image/png" and txt["text"] == "describe it"
    assert base64.b64decode(img["source"]["data"]) == device_drawing()


def test_claude_vision_converts_pnm_and_handles_stops():
    seen = {}

    def ok(request):
        seen["media"] = json.loads(request.content)["messages"][0]["content"][0]["source"]["media_type"]
        return httpx2.Response(200, json=_reply(json.dumps(BRIEF)))

    _claude(ok).describe(b"P2\n2 2\n255\n0 255\n255 0\n", "image/x-portable-graymap", "x")
    assert seen["media"] == "image/png"
    with pytest.raises(VisionError, match="declined"):
        _claude(lambda r: httpx2.Response(200, json=_reply("", "refusal"))).describe(device_drawing(), "image/png", "x")
    with pytest.raises(VisionError, match="cut off"):
        _claude(lambda r: httpx2.Response(200, json=_reply("{", "max_tokens"))).describe(
            device_drawing(), "image/png", "x"
        )
    with pytest.raises(VisionError, match="invalid JSON"):
        _claude(lambda r: httpx2.Response(200, json=_reply("not json"))).describe(device_drawing(), "image/png", "x")


def test_claude_vision_drives_a_full_run():
    vision = _claude(lambda r: httpx2.Response(200, json=_reply(json.dumps(BRIEF))))
    c = TestClient(create_app(run_async=False, vision=vision))
    run = c.post("/api/generation-runs", json={"inputImages": [{"mime": "image/png", "data": _b64(device_drawing())}]})
    run = run.json()
    assert run["status"] == "completed" and run["imageAnalysis"]["summary"] == BRIEF["summary"]
    assert run["projectData"]["requirements"]["prompt"] == BRIEF["prompt"]


def test_vision_from_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert vision_from_env("none") is None and vision_from_env("auto") is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    v = vision_from_env("auto")
    assert isinstance(v, ClaudeVision) and v.model == "claude-opus-5-5"
    monkeypatch.setenv("HWBP_VISION_MODEL", "claude-sonnet-5-5")
    assert vision_from_env("claude").model == "claude-sonnet-5-5"
    with pytest.raises(ValueError):
        vision_from_env("gpt")


def test_cli_picture_only_prints_options(tmp_path, capsys):
    img = tmp_path / "sketch.png"
    img.write_bytes(device_drawing())
    assert main(["generate", "--image", str(img), "--vision", "none", "-o", str(tmp_path / "o")]) == 2
    out = capsys.readouterr().out
    assert "display window" in out and "handheld game console with a display and 3 buttons" in out
    assert (
        main(["generate", "pocket weather station", "--image", str(img), "--vision", "none", "-o", str(tmp_path / "o")])
        == 0
    )
    assert "Prompt from picture: pocket weather station with a display and 3 buttons" in capsys.readouterr().out


def test_drawn_window_size_steers_display_choice():
    from hwbp.engine import Engine

    cat, eng = default_catalog(), Engine()

    def display_caps(frac):
        ff = {"widthMm": 100, "depthMm": 70, "aspect": 1.43, "displayFraction": frac}
        c = eng.generate("weather station with a display and 3 buttons", form_factor=ff)
        return set(cat.get(next(n for n in c.nodes if n.type == "display").part_id).capabilities)

    assert "display.small" not in display_caps(0.31) and "display.small" not in display_caps(0.21)
    assert "display.large" not in display_caps(0.06) and "display.small" in display_caps(0.06)
