import math
import struct

import pytest

from hwbp import mesh as M


@pytest.mark.parametrize(
    "mesh,expected",
    [
        (M.box(10, 20, 30), 6000.0),
        (M.open_box(150, 30, 90, 2), 150 * 30 * 90 - 146 * 28 * 86),
        (M.rect_frame(100, 60, 2, 80, 40), (100 * 60 - 80 * 40) * 2),
        (M.rect_frame(100, 60, 2, 20, 10, hole_offset=(20, 10)), (100 * 60 - 20 * 10) * 2),
    ],
)
def test_primitives_are_closed_with_exact_volume(mesh, expected):
    assert mesh.is_watertight()
    assert mesh.volume() == pytest.approx(expected)


def test_round_primitives_positive_volume_close_to_analytic():
    cyl = M.cylinder(3, 10, segments=64)
    assert cyl.is_watertight() and cyl.volume() == pytest.approx(math.pi * 9 * 10, rel=0.01)
    tube = M.tube(3, 1.2, 8, segments=64)
    assert tube.is_watertight() and tube.volume() == pytest.approx(math.pi * (9 - 1.44) * 8, rel=0.01)
    plate = M.standoff_plate(60, 40, 2, 6, 2.5, 4)
    assert plate.is_watertight() and plate.volume() > 60 * 40 * 2


@pytest.mark.parametrize(
    "factory",
    [
        lambda: M.box(0, 1, 1),
        lambda: M.cylinder(0, 1),
        lambda: M.tube(1, 2, 3),
        lambda: M.open_box(10, 10, 10, 6),
        lambda: M.rect_frame(10, 10, 1, 12, 2),
        lambda: M.rect_frame(10, 10, 1, 0, 2),
    ],
)
def test_invalid_primitives_raise(factory):
    with pytest.raises(ValueError):
        factory()


def test_transforms_and_bounds():
    m = M.box(2, 4, 6).translated(1, 2, 3)
    assert m.bounds() == ((0.0, 0.0, 0.0), (2.0, 4.0, 6.0))
    mirrored = M.box(2, 2, 2).scaled(-1, 1, 1)
    assert mirrored.volume() == pytest.approx(8.0)  # winding flipped with the mirror
    assert M.Mesh().bounds() == ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    assert not M.Mesh().is_watertight()
    assert M.box(1, 1, 1).surface_area() == pytest.approx(6.0)
    merged = M.box(1, 1, 1).merge(M.box(1, 1, 1).translated(5, 0, 0))
    assert merged.is_watertight() and merged.volume() == pytest.approx(2.0)


def test_open_mesh_detected():
    m = M.box(1, 1, 1)
    m.triangles.pop()
    assert not m.is_watertight()


def test_stl_binary_roundtrip_and_ascii():
    shell = M.open_box(50, 20, 30, 2)
    data = M.to_stl_binary(shell)
    assert len(data) == 84 + 50 * len(shell.triangles)
    assert struct.unpack_from("<I", data, 80)[0] == len(shell.triangles)
    back = M.read_stl_binary(data)
    assert back.is_watertight() and back.volume() == pytest.approx(shell.volume(), rel=1e-4)
    text = M.to_stl_ascii(M.box(1, 1, 1, name="cube"))
    assert text.startswith("solid cube") and text.count("facet normal") == 12


def test_obj_and_glb():
    obj = M.to_obj([M.box(1, 1, 1, name="a"), M.box(1, 1, 1, name="b")])
    assert obj.count("\no ") == 2 and "f 9 " in obj  # second object's indices are offset
    glb = M.to_glb([M.SceneNode("a", M.box(1, 1, 1)), M.SceneNode("b", M.cylinder(1, 2), (5, 0, 0), (1, 0, 0, 0.5)),
                    M.SceneNode("empty", M.Mesh())])  # fmt: skip
    doc = M.read_glb_json(glb)
    assert [n["name"] for n in doc["nodes"]] == ["a", "b"]
    assert doc["materials"][1]["alphaMode"] == "BLEND"
    assert doc["accessors"][0]["count"] == 36
    assert len(glb) % 4 == 0
    with pytest.raises(ValueError):
        M.read_glb_json(b"nope" + glb[4:])
