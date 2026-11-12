import math

import pytest

from hwbp import mesh as M
from hwbp.cad import (
    CadJobQueue,
    InsufficientCredits,
    ParametricGenerator,
    Wallet,
    billing_key,
    build_mesh,
    cad_eligible,
    export_assembly,
    feature_spec,
    part_mesh_json,
)
from hwbp.image import (
    brief_from_image,
    decode,
    encode_png_gray,
    extrude_polygon,
    image_to_mesh,
    object_mask,
    otsu_threshold,
    rdp,
    signed_area,
    trace_boundary,
)
from hwbp.models import Node

W = 120


def _img(fn):
    return [[0.0 if fn(x, y) else 1.0 for x in range(W)] for y in range(W)]


L_SHAPE = _img(lambda x, y: (20 <= x < 100 and 70 <= y < 100) or (20 <= x < 50 and 20 <= y < 100))
DISC = _img(lambda x, y: (x - 60) ** 2 + (y - 60) ** 2 <= 40**2)
VASE = _img(lambda x, y: 15 <= y < 110 and abs(x - 60) <= 12 + 18 * math.sin(math.pi * (y - 15) / 95))


# ------------------------------------------------------------------ CAD
def test_feature_specs_cover_part_kinds(reference_content):
    shell = Node(id="e", name="Enclosure Base", type="enclosure", dimensions="100x60x30mm")
    assert feature_spec(shell)["op"] == "open_box"
    assert (
        feature_spec(Node(id="m", name="Controller Mount", type="3d_printed", dimensions="54x29x6mm"))["op"]
        == "standoff_plate"
    )
    assert feature_spec(Node(id="w", name="Drive wheel", type="mechanism", dimensions="65x65x26mm"))["op"] == "cylinder"
    assert feature_spec(Node(id="p", name="Plate", type="structural", dimensions="10x10x2"))["op"] == "box"
    lid = Node(id="lid", name="Enclosure Lid", type="3d_printed", dimensions="100x60x2mm")
    disp = Node(id="d", name="Display", type="display", dimensions="80x40x5mm", mounts_for="lid")
    from hwbp.models import ProjectContent

    spec = feature_spec(lid, ProjectContent(nodes=[lid, disp]))
    assert spec["op"] == "frame" and spec["hole_w"] == 74 and spec["hole_d"] == 34
    for s in (feature_spec(n) for n in reference_content.nodes):
        assert build_mesh(s).is_watertight()
    with pytest.raises(ValueError):
        build_mesh({"op": "torus"})


def test_cad_job_lifecycle(reference_content):
    q = CadJobQueue()
    job = q.submit("p1", reference_content, "shell")
    assert job.status == "succeeded" and set(job.artifacts) == {"model.stl", "model.obj", "model.glb"}
    assert job.stats["print"]["filament"] == "PETG" and job.stats["volumeMm3"] > 0
    assert M.read_stl_binary(job.artifacts["model.stl"]).is_watertight()
    assert q.cancel(job.id).status == "succeeded"  # finished jobs can't be cancelled
    with pytest.raises(ValueError):
        q.submit("p1", reference_content, "screws")  # fasteners are bought, not modelled
    with pytest.raises(KeyError):
        q.submit("p1", reference_content, "nope")

    class Broken:
        def generate(self, node, instruction, parameters):
            raise RuntimeError("boom")

    bad = CadJobQueue(generator_factory=lambda c: Broken()).submit("p1", reference_content, "shell")
    assert bad.status == "failed" and "boom" in bad.error
    assert bad.public()["status"] == "failed"


def test_async_queue(reference_content):
    import time

    q = CadJobQueue(run_async=True)
    job = q.submit("p", reference_content, "base_plate")
    for _ in range(100):
        if job.status == "succeeded":
            break
        time.sleep(0.01)
    assert job.status == "succeeded"


def test_wallet_billing_dedupes_identical_parts(reference_content):
    w = Wallet(plan="basic")
    w.grant_monthly()
    assert w.balance == 5 and not w.unlimited
    keys = [billing_key(n) for n in cad_eligible(reference_content)] + ["Printed Shell"]
    assert w.charge(keys, set()) == 5 and w.balance == 0
    with pytest.raises(InsufficientCredits):
        w.charge(["new"], set())
    w.buy("small")
    assert w.balance == 20 and w.ledger[-1]["memo"].startswith("purchase")
    assert Wallet(plan="ultra").charge(["x"] * 100, set()) == 0


def test_assembly_exports(projects):
    c = projects["radar"]
    files = export_assembly(c, c.mech_layout)
    doc = M.read_glb_json(files["assembly.glb"])
    assert len(doc["nodes"]) == len(c.mech_layout["parts"])
    meshes = part_mesh_json(c, c.mech_layout)
    assert set(meshes) == set(c.mech_layout["parts"]) and all(len(m["f"]) % 3 == 0 for m in meshes.values())
    assert ParametricGenerator(c).generate(c.node("enclosure_base"), None, {"wall": 3.0}).is_watertight()


# ------------------------------------------------------------------ image
def test_png_roundtrip_and_pnm():
    png = encode_png_gray(L_SHAPE)
    img = decode(png)
    assert (img.width, img.height) == (W, W) and img.gray[80][30] == 0.0 and img.gray[0][0] == 1.0
    pgm = b"P2\n# comment\n3 2\n255\n0 128 255\n255 255 0\n"
    g = decode(pgm)
    assert g.width == 3 and g.gray[0][1] == pytest.approx(128 / 255)
    ppm = b"P6\n1 1\n255\n" + bytes([255, 0, 0])
    assert decode(ppm).gray[0][0] == pytest.approx(0.299)
    with pytest.raises(ValueError):
        decode(b"GIF89a....")


def test_png_rgba_palette_and_filters():
    import struct
    import zlib

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    # 2x2 RGBA using filter types 1 (sub) and 4 (paeth); alpha marks the object
    rows = [b"\x01" + bytes([255, 0, 0, 255, 0, 0, 0, 0]), b"\x04" + bytes([0, 0, 0, 0, 0, 0, 0, 0])]
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )
    img = decode(png)
    assert img.alpha[0][0] == 1.0 and img.alpha[0][1] == 1.0 and img.alpha[1][0] == 1.0
    pal = [b"\x00" + bytes([0, 1]), b"\x02" + bytes([0, 0])]
    png2 = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 3, 0, 0, 0))
        + chunk(b"PLTE", bytes([0, 0, 0, 255, 255, 255]))
        + chunk(b"tRNS", bytes([255, 0]))
        + chunk(b"IDAT", zlib.compress(b"".join(pal)))
        + chunk(b"IEND", b"")
    )
    img2 = decode(png2)
    assert img2.gray[0][1] == 1.0 and img2.alpha[0][1] == 0.0
    bad = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 16, 0, 0, 0, 0)) + chunk(b"IEND", b"")
    with pytest.raises(ValueError):
        decode(bad)


def test_segmentation_and_contour():
    img = decode(encode_png_gray(DISC))
    assert 0.0 < otsu_threshold(img) < 1.0
    mask = object_mask(img)
    area = sum(v for row in mask for v in row)
    assert area == pytest.approx(math.pi * 40**2, rel=0.03)
    contour = trace_boundary(mask)
    assert len(contour) > 100
    inverted = decode(encode_png_gray([[1.0 - v for v in row] for row in DISC]))  # light object on dark bg
    assert sum(v for row in object_mask(inverted) for v in row) == pytest.approx(area, rel=0.02)
    assert trace_boundary([[False, False], [False, False]]) == []
    assert rdp([(0, 0), (1, 0.01), (2, 0)], 0.5) == [(0, 0), (2, 0)]


@pytest.mark.parametrize("rows,mode,check", [
    (L_SHAPE, "extrude", lambda m, r: r["volumeMm3"] == pytest.approx((80 * 30 + 30 * 50) * 10, rel=0.06)),
    (DISC, "extrude", lambda m, r: r["volumeMm3"] == pytest.approx(math.pi * 40**2 * 10, rel=0.08)),
    (VASE, "revolve", lambda m, r: r["sizeMm"][1] / r["sizeMm"][0] == pytest.approx(95 / 60, rel=0.08)),
    (L_SHAPE, "relief", lambda m, r: r["sizeMm"][0] == pytest.approx(80, rel=0.01)),
])  # fmt: skip
def test_image_to_mesh_modes(rows, mode, check):
    mesh, report = image_to_mesh(encode_png_gray(rows), mode, width_mm=80, height_mm=10)
    assert report["watertight"] and mesh.is_watertight() and mesh.volume() > 0
    assert check(mesh, report)


def test_image_to_mesh_errors():
    with pytest.raises(ValueError):
        image_to_mesh(encode_png_gray(L_SHAPE), "sculpt")
    with pytest.raises(ValueError):
        image_to_mesh(encode_png_gray([[1.0] * 10 for _ in range(10)]))  # nothing to segment


def test_extrude_handles_clockwise_input():
    sq = [(0, 0), (0, 10), (10, 10), (10, 0)]
    assert signed_area(sq) < 0
    m = extrude_polygon(sq, 5)
    assert m.is_watertight() and m.volume() == pytest.approx(500)


def test_brief_from_image():
    png = encode_png_gray(L_SHAPE)
    with pytest.raises(ValueError):
        brief_from_image(png)
    b = brief_from_image(png, user_prompt="desk lamp")
    assert b.source == "user" and b.silhouette.aspect == pytest.approx(1.0, rel=0.05)

    class Vision:
        def describe(self, image, mime, instruction):
            assert "JSON" in instruction
            return {
                "name": "Lamp",
                "prompt": "LED lamp with touch button",
                "features": ["led"],
                "approxSizeMm": {"w": 80, "d": 30, "h": 20},
                "summary": "an L-shaped lamp",
                "components": ["LED strip"],
                "uncertainties": ["power source"],
            }

    b = brief_from_image(png, vision=Vision())
    assert b.source == "vision" and b.name == "Lamp" and b.size_mm["w"] == 80
    assert b.analysis == {"summary": "an L-shaped lamp", "components": ["LED strip"], "uncertainties": ["power source"]}
    b = brief_from_image(b"P2\n1 1\n255\n255\n", user_prompt="blank")
    assert b.silhouette is None


def test_extrude_with_holes_exact_and_from_picture():
    from hwbp.image import bridge_holes, find_holes

    outer = [(0, 0), (10, 0), (10, 10), (0, 10)]
    holes = [[(2, 2), (2, 4), (4, 4), (4, 2)], [(6, 6), (6, 8), (8, 8), (8, 6)]]
    m = extrude_polygon(outer, 2, holes=holes)
    assert m.is_watertight() and m.volume() == pytest.approx((100 - 8) * 2)
    pts, refs = bridge_holes(outer, [h[::-1] for h in holes])
    assert len(pts) == 4 + 2 * (4 + 2) and refs[0] == (0, 0)

    def bracket(x, y):
        body = (20 <= x < 100 and 70 <= y < 100) or (20 <= x < 50 and 20 <= y < 100)
        return body and (x - 35) ** 2 + (y - 40) ** 2 >= 8**2 and (x - 80) ** 2 + (y - 85) ** 2 >= 7**2

    rows = _img(bracket)
    assert len(find_holes([[not bool(v) for v in r] for r in rows])) == 2
    mesh, report = image_to_mesh(encode_png_gray(rows), "extrude", width_mm=80, height_mm=5)
    expected = ((80 * 30 + 30 * 50) - math.pi * (64 + 49)) * 5
    assert report["holes"] == 2 and mesh.is_watertight()
    assert report["volumeMm3"] == pytest.approx(expected, rel=0.05)
