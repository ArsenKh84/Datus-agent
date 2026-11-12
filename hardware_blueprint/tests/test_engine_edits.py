import json

import pytest

from hwbp.catalog import Catalog
from hwbp.classify import classify_message, classify_part
from hwbp.edits import (
    EditOp,
    ProjectStore,
    VersionConflict,
    apply_ops,
    apply_plan,
    message_to_ops,
    project_summary,
    validate_plan,
)
from hwbp.engine import PHASES, apply_answers, clarifying_questions, project_id_for
from hwbp.enrichment import (
    CatalogSourcing,
    NexarProvider,
    SearchLinkSourcing,
    canonicalize,
    enrich_node,
    research_queries,
    spec_sheet,
)
from hwbp.export import export_zip, guide_markdown, render_viewer, wiring_svg
from hwbp.firmware import generate_firmware
from hwbp.instructions import PHASES as GUIDE_PHASES
from hwbp.instructions import tools_for
from hwbp.layout import layered_layout, render_svg
from hwbp.models import Node, ProjectContent, canonical_type, type_sort_key
from hwbp.requirements import Requirements


# ------------------------------------------------------------------ engine invariants on every sample design
def test_document_invariants(projects):
    for name, c in projects.items():
        ids = {n.id for n in c.nodes}
        assert len(ids) == len(c.nodes), name
        pins = {n.id: set(n.pins or []) for n in c.nodes}
        for conn in c.electrical_connections:
            assert conn.source in ids and conn.target in ids, (name, conn)
            if conn.type == "data" and conn.source_pin and conn.target_pin:
                if c.node(conn.source).type == "mcu":
                    assert conn.source_pin in pins[conn.source], (name, conn)
                if c.node(conn.target).type == "mcu":
                    assert conn.target_pin in pins[conn.target], (name, conn)
        for m in c.mechanical_connections:
            assert m.source in ids and m.target in ids
        for st in c.instruction_steps:
            for sub in st.sub_steps:
                assert set(sub.part_ids) <= ids, (name, sub)
        assert [s.id for s in c.instruction_steps] == [p for p, _ in GUIDE_PHASES]
        assert all(canonical_type(n.type) == n.type for n in c.nodes)
        assert c.bom["estimateTotalUsd"] > 0 and c.bom["rollup"]
        assert c.wiring_layout["nodes"] and c.firmware
        assert json.loads(json.dumps(c.dump()))["projectName"] == c.project_name


def test_run_emits_reference_phases(engine):
    events = list(engine.run("Smart plant monitor with soil moisture and OLED display"))
    assert [e["phase"] for e in events] == list(PHASES)
    assert [e["revision"] for e in events] == list(range(1, len(PHASES) + 1))
    assert events[-1]["status"] == "completed" and events[1]["questions"]


def test_questions_and_answers():
    req = Requirements(prompt="a plant monitor", power_source="wall")
    qs = {q.id for q in clarifying_questions(req)}
    assert qs == {"power_source", "budget_usd"}
    r2 = apply_answers(
        Requirements(prompt="x", power_source="battery"),
        {"power_source": "solar", "runtime_hours": "24", "budget_usd": "50"},
    )
    assert (r2.power_source, r2.runtime_hours, r2.budget_usd) == ("solar", 24.0, 50.0)
    r3 = apply_answers(r2, {"budget_usd": "no limit", "runtime_hours": "soon", "power_source": "nuclear"})
    assert r3.budget_usd is None and r3.runtime_hours == 24.0 and r3.power_source == "solar"
    assert len(project_id_for("x")) == 12


def test_firmware_matches_pin_allocation(projects):
    c = projects["weather"]
    fw = c.firmware
    assert "lib_deps" in fw["platformio.ini"] and "Wire.begin" in fw["src/main.cpp"]
    pins_h = fw["include/pins.h"]
    for conn in c.electrical_connections:
        if conn.type == "data" and c.node(conn.source).type == "mcu":
            assert conn.source_pin.replace("GPIO", "").replace("GP", "") in pins_h
    assert "driveRamp" in projects["car"].firmware["src/main.cpp"]  # soft-start for the motor budget


def test_firmware_linux_sbc(engine):
    from hwbp.design import Design, Part

    d = Design(Requirements(prompt="x"))
    d.parts.append(Part(id="main_controller", catalog=engine.catalog.get("rpi-zero-2w"), role="mcu", name="MCU"))
    d.pin_map = {"led": {"DIN": "GPIO18"}}
    assert "LED_DIN = 18" in generate_firmware(d)["main.py"]
    assert generate_firmware(Design(Requirements(prompt="x"))) == {}


def test_tools_and_guide(projects):
    assert "3D printer" in tools_for("3D print the lid", "fabricate")
    assert tools_for("Something", "bringup") == ["Digital multimeter"]
    md = guide_markdown(projects["car"])
    assert md.startswith("# ") and "## 1." in md and "Design review" in md


# ------------------------------------------------------------------ layout
def test_layered_layout_has_no_node_overlap_and_handles_cycles():
    ids = ["a", "b", "c", "d", "e"]
    edges = [{"source": "a", "target": "b", "type": "power"}, {"source": "b", "target": "c", "type": "data"},
             {"source": "a", "target": "d", "type": "data"}, {"source": "d", "target": "e", "type": "data"},
             {"source": "e", "target": "a", "type": "data"}, {"source": "a", "target": "c", "type": "data", "label": "spi ×2"},
             {"source": "a", "target": "zz", "type": "data"}]  # fmt: skip
    lay = layered_layout(ids, edges)
    boxes = list(lay["nodes"].values())
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            assert (
                a["x"] + a["w"] <= b["x"]
                or b["x"] + b["w"] <= a["x"]
                or a["y"] + a["h"] <= b["y"]
                or b["y"] + b["h"] <= a["y"]
            )
    assert len(lay["edges"]) == 6 and any(e["reversed"] for e in lay["edges"])
    svg = render_svg(lay, {"a": {"name": "A", "type": "mcu", "pins": ["VERYLONGPIN", "GND"]}}, dark=True)
    assert svg.startswith("<svg") and "VERYLO…" in svg and "spi ×2" in svg


# ------------------------------------------------------------------ edits & classification
def test_classify_message_contract():
    cases = {
        "should I use a bigger battery?": ("ask", "question"),
        "can you add a GPS?": ("edit", "add_part"),
        "replace the display with an e-paper": ("edit", "swap_part"),
        "add two more servos": ("edit", "set_quantity"),
        "it should run for 48 hours": ("edit", "modify"),
        "make it cheaper": ("edit", "reduce_cost"),
        "Why is the battery so small": ("ask", "question"),
        "remove the radar please": ("edit", "remove_part"),
        "power it from solar": ("edit", "change_power"),
        "hello": ("ask", "question"),
    }
    for msg, (mode, intent) in cases.items():
        r = classify_message(msg)
        assert (r.mode, r.intent) == (mode, intent), msg


def test_classify_part_types():
    assert classify_part("ESP32-WROOM dev board").type == "mcu"
    assert classify_part("M3x10 screw").type == "misc"
    assert classify_part("3D printed bracket").type == "3d_printed"
    assert classify_part("18650 cell").subtype == "battery"
    assert classify_part("DRV8833 motor driver").subtype == "driver"
    assert classify_part("mystery widget", has_pins=True).category == "electrical"
    assert classify_part("mystery widget").type == "other"
    assert (
        canonical_type("battery") == "power"
        and canonical_type("fastener") == "misc"
        and canonical_type(None) == "other"
    )
    assert type_sort_key("mcu") < type_sort_key("misc") < type_sort_key("whatever")


def test_message_to_ops_and_regeneration(engine, projects):
    c = projects["radar"]
    mode, ops = message_to_ops("replace the display with an e-paper display", c, engine.catalog)
    assert mode == "edit" and ops[0].op == "force" and ops[0].args["catalogId"] == "epaper-29"
    new = engine.generate(requirements=apply_ops(Requirements.model_validate(c.requirements), ops))
    assert new.node("display").catalog_id == "epaper-29" and new.power["runtimeHours"] > c.power["runtimeHours"]
    assert message_to_ops("what does the boost converter do?", c, engine.catalog) == ("ask", [])
    _, ops = message_to_ops("remove the radar", c, engine.catalog)
    assert ops == [EditOp("remove_capability", {"capability": "sense.presence"})]
    _, ops = message_to_ops("remove the charger", c, engine.catalog)
    assert ops[0].op == "exclude"
    _, ops = message_to_ops("make it solar powered", c, engine.catalog)
    assert ops[0].args["source"] == "solar"
    _, ops = message_to_ops("make it cheaper", c, engine.catalog)
    assert ops[0].op == "set_budget" and ops[0].args["usd"] < c.bom["estimateTotalUsd"]
    _, ops = message_to_ops("add a buzzer", c, engine.catalog)
    assert ops[0].args == {"capability": "output.buzzer"}
    _, ops = message_to_ops("add two more servos", projects["arm"], engine.catalog)
    assert ops[0].args["count"] == 6
    _, ops = message_to_ops("it should run for 48 hours", c, engine.catalog)
    assert ops[-1] == EditOp("set_runtime", {"hours": 48.0})
    r = apply_ops(Requirements(prompt="x", capabilities=["ui.display"], counts={"ui.display": 1}),
                  [EditOp("exclude", {"catalogId": "a"}), EditOp("remove_capability", {"capability": "ui.display"}),
                   EditOp("set_power", {"source": "battery"}), EditOp("set_count", {"capability": "input.button", "count": 3})])  # fmt: skip
    assert r.exclude == ["a"] and r.capabilities == ["input.button"] and r.runtime_hours == 4.0


def test_edit_plan_validation_and_apply(reference_content, catalog):
    plan = {
        "addParts": [
            {"name": "Active Buzzer", "productName": "Active Buzzer Module"},
            {"id": "board", "name": "dup id"},
        ],
        "removeNodeIds": ["sensorr", "ghost"],
        "addElectricalConnections": [
            {
                "type": "data",
                "source": "bord",
                "target": "active_buzzer",
                "protocol": "gpio",
                "sourcePin": "GPIO9",
                "targetPin": "I/O",
            },
            {
                "type": "data",
                "source": "board",
                "target": "active_buzzer",
                "protocol": "gpio",
                "sourcePin": "GPIO9",
                "targetPin": "I/O",
            },
            {"type": "data", "source": "ghost", "target": "board"},
        ],
        "addMechanicalConnections": [
            {"source": "active_buzzer", "target": "shell", "label": "glue", "delta": {"x": 0, "y": 0, "z": 0}}
        ],
        "removeElectricalConnections": [{"source": "board", "target": "sensor", "type": "power"}],
        "removeMechanicalConnections": [{"source": "shell", "target": "base_plate"}],
    }
    vp, rep = validate_plan(reference_content, plan, catalog)
    assert vp["removeNodeIds"] == ["sensor"] and any("sensorr -> sensor" in r for r in rep.repaired)
    assert [p["id"] for p in vp["addParts"]] == ["active_buzzer", "board_2"]
    assert vp["addParts"][0]["catalogId"] == "buzzer-active" and "GND" in vp["addParts"][0]["pins"]
    assert len(vp["addElectricalConnections"]) == 1 and any("duplicate" in d for d in rep.dropped)
    assert {"nodeId": "board", "pins": ["GPIO9"]} in vp["newPinsUsedOnExistingNodes"]
    new = apply_plan(reference_content, vp)
    assert new.node("sensor") is None and "GPIO9" in new.node("board").pins
    assert not any(c.target == "sensor" or c.source == "sensor" for c in new.electrical_connections)
    assert not any(c.source == "shell" and c.target == "base_plate" for c in new.mechanical_connections)
    assert reference_content.node("sensor") is not None  # input not mutated


def test_store_optimistic_concurrency(reference_content):
    st = ProjectStore()
    st.create("p", reference_content)
    assert "p" in st and st.save("p", reference_content, 1).revision == 2
    with pytest.raises(VersionConflict):
        st.save("p", reference_content, 1)
    assert st.versions("p") == [1, 2] and st.restore("p", 1).revision == 3
    with pytest.raises(KeyError):
        st.get("nope")
    with pytest.raises(KeyError):
        st.restore("p", 99)


# ------------------------------------------------------------------ enrichment
def test_canonicalize_paths(catalog):
    assert canonicalize("bme280", catalog).matched_by == "id"
    assert canonicalize({"mpn": "SG90"}, catalog).part.id == "sg90-servo"
    assert canonicalize({"name": "Waveshare display ST7789 panel"}, catalog).matched_by in ("mpn", "search")
    t = canonicalize(
        {"name": "Flux Capacitor Module", "dimensions": "10x20x3mm", "description": "time travel"}, catalog
    )
    assert t.matched_by == "transient" and t.part.id.startswith("transient-") and t.part.dims_mm == (10, 3, 20)
    assert canonicalize("0.96in OLED 128x64 (SSD1306, I2C)", catalog).confidence >= 0.95


NEXAR_FIXTURE = {"data": {"supSearchMpn": {"results": [{"part": {"mpn": "BME280", "bestDatasheet": {"url": "https://ds/bme280.pdf"},
    "sellers": [{"company": {"name": "Mouser"}, "offers": [{"clickUrl": "https://m/1", "inventoryLevel": 500, "moq": 1,
    "prices": [{"quantity": 10, "price": 3.1, "currency": "USD"}, {"quantity": 1, "price": 3.5, "currency": "USD"}]},
    {"clickUrl": "https://m/2", "prices": []}]}]}}]}}}  # fmt: skip


def test_nexar_provider_and_enrich(catalog):
    calls = []
    prov = NexarProvider(transport=lambda q, v: calls.append(v) or NEXAR_FIXTURE)
    offers = prov.offers(catalog.get("bme280"))
    assert calls == [{"q": "BME280", "limit": 3}]
    assert len(offers) == 1 and offers[0].price_breaks[0].qty == 1 and offers[0].stock == 500
    assert NexarProvider.datasheet(NEXAR_FIXTURE) == "https://ds/bme280.pdf" and NexarProvider.parse({}) == []

    class Failing:
        name = "f"

        def offers(self, part):
            raise ConnectionError

    node = Node(id="env", name="Env", type="sensor", quantity=2)
    enrich_node(node, catalog.get("bme280"), [prov, Failing(), CatalogSourcing(), SearchLinkSourcing()])
    assert node.purchase_url and node.estimated_cost and node.dimensions == "15x12x3mm"
    vendors = [o.vendor for o in node.research.sourcing]
    assert vendors[0] in ("AliExpress", "Mouser") and "eBay" in vendors
    labels = {s.label for s in spec_sheet(catalog.get("esp32-s3-devkitc-1"))}
    assert {"pins", "radios", "supply_5V", "width_mm"} <= labels
    assert {s.label for s in spec_sheet(catalog.get("mt3608-boost"))} >= {"topology", "vout", "imax_ma"}
    assert {s.label for s in spec_sheet(catalog.get("lipo-1s-1000"))} >= {"chemistry", "capacity_mah"}
    assert {s.label for s in spec_sheet(catalog.get("sg90-servo"))} >= {"drive", "stall_ma"}
    assert (
        research_queries(Node(id="x", name="Thing", type="display", product_name="ST7789")).queries[0]
        == "ST7789 datasheet controller"
    )
    assert research_queries(Node(id="x", name="Thing", type="other")).queries[0] == "Thing specifications"
    assert (
        NexarProvider(transport=lambda q, v: {}).offers(
            catalog.get("custom-x") if "custom-x" in catalog else type("P", (), {"mpn": None, "name": ""})()
        )
        == []
    )


# ------------------------------------------------------------------ catalog
def test_catalog_api(catalog, tmp_path):
    assert len(catalog) >= 80 and "bme280" in catalog
    assert catalog.resolve("SG90").id == "sg90-servo" and catalog.resolve("zzz unknown") is None
    assert all(p.type == "display" for p, _ in catalog.search("oled", type_filter="display"))
    assert catalog.search("") == [] and catalog.with_capability("sense.")
    with pytest.raises(KeyError):
        catalog.get("nope")
    f = tmp_path / "extra.json"
    f.write_text(json.dumps({"parts": [{"id": "my-part", "type": "sensor", "name": "My Part"}]}))
    extra = Catalog.from_json(f)
    merged = Catalog.from_dicts([]).extend(extra).extend(extra)
    assert len(merged) == 1
    with pytest.raises(ValueError):
        merged.add(extra.get("my-part"))


# ------------------------------------------------------------------ export
def test_export_bundle_and_viewer(projects):
    import io
    import zipfile

    c = projects["radar"]
    z = zipfile.ZipFile(io.BytesIO(export_zip(c, build_qty=5)))
    names = set(z.namelist())
    assert {
        "project.json",
        "bom.csv",
        "wiring.svg",
        "guide.md",
        "viewer.html",
        "cad/assembly.glb",
        "firmware/platformio.ini",
    } <= names
    assert any(n.startswith("cad/parts/") and n.endswith(".stl") for n in names)
    html = render_viewer(c)
    assert (
        "window.HWBP_PROJECT = {" in html
        and "window.HWBP_MESHES = {" in html
        and "</script><" not in html.split("HWBP_MESHES")[1][:50]
    )
    assert "HWBP_MESHES = null" in render_viewer(c, with_meshes=False)
    assert wiring_svg(ProjectContent()).startswith("<svg")


def test_project_summary_for_llm_context(reference_content, projects):
    text = project_summary(reference_content)
    assert text.startswith('Project: "Reference Format Sample"') and "sensor → board (data, uart, TX→RXD)" in text
    assert "board_mount: Controller Mount mounts board" in text and "Disconnected nodes (1)" in text  # the screws
    assert "Disconnected" not in project_summary(projects["radar"]).split("Mechanical")[0]


def test_committed_schema_is_current():
    from pathlib import Path

    from hwbp.models import project_schema

    path = Path(__file__).resolve().parents[1] / "schema" / "project.schema.json"
    assert json.loads(path.read_text()) == project_schema(), (
        "run: uv run python -m hwbp.schema_dump > schema/project.schema.json"
    )
