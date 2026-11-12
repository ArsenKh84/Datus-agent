import io
import zipfile

import pytest

from hwbp import mesh as M
from hwbp.export import FILE_KINDS, export_cad, export_file, render_viewer, viewer_extras
from hwbp.ideas import (
    apply_descriptions,
    generate_ideas,
    refresh_part_descriptions,
    substep_detail,
    suggested_prompts,
    topology_hash,
)
from hwbp.layout import enriched_wiring_layout
from hwbp.mech import apply_joints, derive_joints, enrich_mechanical, explode_layout, heal_mechanical, solve_layout
from hwbp.models import ProjectContent
from hwbp.pricing import build_bom, merge_by_product
from hwbp.render import part_icon, project_image, render_iso, step_image


def _overlaps(layout):
    parts = layout["parts"]
    ids = sorted(parts)
    n = 0
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            pa, pb = parts[a], parts[b]
            if all(
                min(pa["pos"][k] + pa["size"][k] / 2, pb["pos"][k] + pb["size"][k] / 2)
                - max(pa["pos"][k] - pa["size"][k] / 2, pb["pos"][k] - pb["size"][k] / 2)
                > 1e-3
                for k in range(3)
            ):
                n += 1
    return n


def test_explode_separates_every_part(projects):
    for name in ("radar", "car", "arm"):
        lay = projects[name].mech_layout
        ex = explode_layout(lay)
        assert ex["exploded"] and _overlaps(ex) == 0, name
        assert ex["bbox"]["size"][1] >= lay["bbox"]["size"][1]
    assert explode_layout({"parts": {}})["exploded"]


def test_joints_slider_and_revolute(projects):
    lay = projects["radar"].mech_layout
    joints = derive_joints(lay)
    assert joints and all(j["type"] == "rigid" for j in joints)
    lid = next(j for j in joints if j["child"] == "enclosure_lid")
    lid.update(type="slider", axis=[0, 1, 0], pose=20)
    posed = apply_joints(lay, joints)
    assert posed["parts"]["enclosure_lid"]["pos"][1] == pytest.approx(lay["parts"]["enclosure_lid"]["pos"][1] + 20)
    assert posed["parts"]["display"]["pos"][1] == pytest.approx(lay["parts"]["display"]["pos"][1] + 20)  # subtree moves
    lid.update(type="revolute", axis=[1, 0, 0], pose=90)
    posed = apply_joints(lay, joints)
    w, h, d = lay["parts"]["enclosure_lid"]["size"]
    assert posed["parts"]["enclosure_lid"]["extent"] == pytest.approx([w, d, h], abs=1e-3)
    assert len(posed["parts"]["enclosure_lid"]["rotation"]) == 9
    with pytest.raises(ValueError):
        apply_joints(lay, [{"parent": "a", "child": "b", "type": "ball"}])


def test_heal_and_enrich_foreign_document():
    doc = ProjectContent.model_validate({
        "nodes": [{"id": "plate", "name": "Plate", "type": "structural", "dimensions": "100x60x3"},
                  {"id": "board", "name": "Board", "type": "mcu", "dimensions": "40x20x4"},
                  {"id": "cam", "name": "Cam", "type": "sensor", "dimensions": "20x20x10"},
                  {"id": "screws", "name": "M3 screws", "type": "misc"}],
        "mechanicalConnections": [{"source": "board", "target": "plate", "label": "screws", "delta": None}]})  # fmt: skip
    rep = heal_mechanical(doc)
    assert rep["orphansBefore"] == ["cam"] and rep["orphansAfter"] == [] and rep["failed"] == 0
    assert all(c.delta is not None for c in doc.mechanical_connections)
    assert solve_layout(doc)["collisions"] == []
    assert enrich_mechanical(doc) == {"assigned": 0, "failed": 0}


def test_renders(projects):
    c = projects["radar"]
    svg, h = project_image(c)
    assert svg.startswith("<svg") and len(h) == 64 and c.project_name in svg
    assert project_image(c, variant=1)[1] != h and project_image(c)[1] == h  # deterministic rerolls
    step = step_image(c, ["main_controller"])
    assert 'opacity="0.18"' in step and 'data-part="main_controller" opacity="0.92"' in step
    assert render_iso({}).startswith("<svg") and "<polygon" not in render_iso({})
    assert "<svg" in part_icon("ESP32", "mcu") and "<svg" in part_icon("?", None)
    assert 'opacity="0.38"' in svg  # shells drawn translucent (x-ray)


def test_ideas_prompts_details_descriptions(projects, catalog):
    ideas = generate_ideas({"interests": ["robotics"], "budget": 50, "skill": 2})["projects"]
    assert ideas and "robot" in ideas[0]["description"].lower()
    assert generate_ideas({})["projects"]
    c = projects["radar"]
    sp = suggested_prompts(c)
    assert sp["edit"] and sp["ask"] and sp["hash"].startswith("v1::") and sp["hash"] == topology_hash(c)
    assert suggested_prompts(c, "edit")["prompts"] == sp["edit"] and suggested_prompts(c, "ask")["prompts"] == sp["ask"]
    car = suggested_prompts(projects["car"])
    assert any("current" in p or "battery" in p.lower() or "3.3 V" in p for p in car["edit"])
    d = substep_detail(c, "wire", "wire_1")
    assert d["summary"] and d["steps"] and d["tip"]
    assert substep_detail(c, "fabricate", "fabricate_1")["steps"]
    with pytest.raises(KeyError):
        substep_detail(c, "wire", "nope")
    desc = refresh_part_descriptions(c, catalog)
    assert any("MHz" in x["description"] for x in desc)
    copy = c.model_copy(deep=True)
    assert apply_descriptions(copy, desc) == len(desc)


def test_enriched_wiring_and_merged_bom(projects):
    lay = enriched_wiring_layout(projects["arm"])
    rails = [n for n in lay["nodes"] if n.startswith("rail:")]
    assert "rail:GND" in rails and "rail:5V" in rails and lay["mode"] == "enriched"
    assert all(e["source"] in lay["nodes"] and e["target"] in lay["nodes"] for e in lay["edges"])
    rows = merge_by_product(build_bom(projects["arm"].nodes, use_offers=False).lines)
    servo = next(r for r in rows if "SG90" in r["product"])
    assert (
        servo["qty"] == 4 and len(servo["nodeIds"]) == 4 and servo["subtotalUsd"] == pytest.approx(4 * servo["unitUsd"])
    )


def test_cad_and_file_exports(projects):
    c = projects["radar"]
    data, counts = export_cad(c, ["stl", "step", "glb", "obj"], node_ids=["enclosure_base", "fastener_kit"])
    z = zipfile.ZipFile(io.BytesIO(data))
    assert counts == {"downloaded": 4, "omitted": 1, "parts": 1}
    assert {
        "CAD/STL/enclosure_base.stl",
        "CAD/STEP/enclosure_base.step",
        "CAD/GLB/enclosure_base.glb",
        "CAD/OBJ/enclosure_base.obj",
    } <= set(z.namelist())
    assert "fastener_kit" in z.read("CONTENTS.txt").decode()
    with pytest.raises(ValueError):
        export_cad(c, ["iges"])
    for kind in FILE_KINDS:
        blob, media = export_file(c, kind)
        assert blob and "/" in media, kind
    with pytest.raises(ValueError):
        export_file(c, "nope")
    ex = viewer_extras(c)
    assert (
        ex["enriched"]
        and ex["exploded"]
        and ex["hero"]
        and len(ex["details"]) == sum(len(s.sub_steps) for s in c.instruction_steps)
    )
    html = render_viewer(c)
    assert "window.HWBP_EXTRAS = {" in html and "window.HWBP_API = null;" in html


def test_step_export_parses_with_independent_reader():
    from steputils import p21

    txt = M.to_step([M.open_box(50, 20, 30, 2, name="tray"), M.cylinder(5, 10, name="post")], name="demo")
    f = p21.loads(txt)
    names = [i.entity.name for i in f.data[0].instances.values() if hasattr(i, "entity")]
    expected = len(M.open_box(50, 20, 30, 2).triangles) + len(M.cylinder(5, 10).triangles)
    assert names.count("FACE") == M.step_face_count(txt) == expected
    assert names.count("FACETED_BREP") == 2 and names.count("CLOSED_SHELL") == 2
