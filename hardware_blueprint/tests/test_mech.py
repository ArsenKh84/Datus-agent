import pytest

from hwbp.mech import format_dims, is_layout_part, parse_dims, shape_of, skyline_pack, solve_layout
from hwbp.models import MechanicalConnection, Node, ProjectContent, Vec3


@pytest.mark.parametrize(
    "text,expected",
    [
        ("155x85x25mm", (155.0, 25.0, 85.0)),  # reference axis convention: 3rd number is height (Y)
        ("10 × 20 × 0.5 mm", (10.0, 1.0, 20.0)),  # each value clamped to >= 1 mm
        ("M2.5x6mm", None),
        ("0x5x5", None),
        (None, None),
        ("about 5 cm", None),
    ],
)
def test_parse_dims(text, expected):
    assert parse_dims(text) == expected


def test_format_dims_inverts_parse():
    assert parse_dims(format_dims(30, 4, 20)) == (30, 4, 20)


def test_layout_part_rules():
    assert not is_layout_part(Node(id="a", name="M3 screws", type="structural"))
    assert not is_layout_part(Node(id="b", name="Glue", type="misc"))
    assert is_layout_part(Node(id="c", name="Chassis", type="structural"))
    assert shape_of(Node(id="w", name="Rubber wheel", type="mechanism")) == "cylinder"


def test_solver_reproduces_reference_semantics(reference_content):
    lay = solve_layout(reference_content)
    p = lay["parts"]
    assert set(p) == {"base_plate", "shell", "board", "board_mount", "sensor"}  # screws excluded
    assert lay["cadEligible"] == 5
    assert p["base_plate"]["pos"] == [0.0, 1.5, 0.0]
    assert p["shell"]["pos"] == [0.0, 13.0, 0.0]
    assert p["board_mount"]["pos"] == [0.0, 6.0, 0.0]
    assert p["board"]["pos"] == [0.0, 11.0, 0.0]  # held on top of its mount (mountsFor, implicit)
    assert p["sensor"]["pos"] == [-40.0, 13.0, 0.0]  # delta = centre(target) - centre(source)
    assert lay["bbox"]["size"] == [124.0, 23.0, 64.0]
    assert [(r["id"], r["depth"]) for r in lay["tree"]] == [
        ("base_plate", 0), ("board_mount", 1), ("board", 2), ("shell", 1), ("sensor", 2)]  # fmt: skip
    assert lay["collisions"] == []


def test_solver_implicit_rules_and_components():
    c = ProjectContent(
        nodes=[Node(id="big", name="Box", type="enclosure", dimensions="100x100x10"),
               Node(id="small", name="Board", type="mcu", dimensions="20x20x2"),
               Node(id="wide", name="Panel", type="structural", dimensions="200x50x2"),
               Node(id="lone", name="Loner", type="sensor", dimensions="10x10x10")],
        mechanical_connections=[MechanicalConnection(source="small", target="big", delta=None),
                                MechanicalConnection(source="wide", target="big", delta=None)],
    )  # fmt: skip
    lay = solve_layout(c)
    p = lay["parts"]
    assert p["small"]["pos"][1] == pytest.approx(10 + 1)  # 'top' rule: fits inside the footprint
    assert p["wide"]["pos"][0] == pytest.approx(50 + 5 + 100)  # 'beside' rule (+X, 5 mm gap)
    assert p["lone"]["pos"][0] > 150  # separate component packed to the right
    assert solve_layout(ProjectContent())["parts"] == {}


def test_collisions_detected_between_unrelated_parts():
    c = ProjectContent(
        nodes=[Node(id="root", name="Plate", type="structural", dimensions="100x100x2"),
               Node(id="a", name="A", type="module", dimensions="30x30x10"),
               Node(id="b", name="B", type="module", dimensions="30x30x10")],
        mechanical_connections=[MechanicalConnection(source="a", target="root", delta=Vec3(x=0, y=-6, z=0)),
                                MechanicalConnection(source="b", target="root", delta=Vec3(x=-10, y=-6, z=0))],
    )  # fmt: skip
    assert solve_layout(c)["collisions"] == [["a", "b"]]


def test_skyline_pack_has_no_overlaps():
    items = [(f"p{i}", w, d) for i, (w, d) in enumerate([(40, 20), (30, 30), (10, 50), (25, 25), (60, 10), (15, 15)])]
    pos, used_w, used_d = skyline_pack(items, 70, gap=2)
    rects = [(pos[i][0], pos[i][1], pos[i][0] + w, pos[i][1] + d) for i, w, d in items]
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            a, b = rects[i], rects[j]
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]
    assert used_w <= 72 and used_d > 0
    pos2, w2, _ = skyline_pack([("huge", 200, 10)], 50)
    assert pos2["huge"] == (0.0, 0.0) and w2 >= 200


def test_generated_designs_have_no_interference(projects):
    for name, c in projects.items():
        assert c.mech_layout["collisions"] == [], name
        assert c.mech_layout["bbox"]["size"][1] > 0


def test_form_factor_controls_enclosure_aspect(engine):
    c = engine.generate(
        "desk clock with OLED display and wifi", form_factor={"widthMm": 200, "depthMm": 50, "targetWidthMm": 160}
    )
    w, h, d = c.mech_layout["enclosure"]["outer"]
    assert w / d == pytest.approx(4, rel=0.25) and w >= 160
