"""Command line: ``hwbp generate | image2mesh | layout | bom | serve``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hwbp import mesh as M
from hwbp.engine import Engine
from hwbp.export import export_zip, guide_markdown, render_viewer, wiring_svg
from hwbp.image import brief_from_image, image_to_mesh
from hwbp.mech import solve_layout
from hwbp.models import ProjectContent
from hwbp.pricing import build_bom


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def cmd_generate(a: argparse.Namespace) -> int:
    form_factor = None
    prompt = a.prompt or ""
    if a.image:
        brief = brief_from_image(Path(a.image).read_bytes(), user_prompt=prompt or None)
        prompt = brief.prompt
        if brief.silhouette:
            form_factor = {
                "widthMm": brief.silhouette.width_mm,
                "depthMm": brief.silhouette.depth_mm,
                "aspect": brief.silhouette.aspect,
            }
    content = Engine().generate(prompt, form_factor=form_factor)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "project.json").write_text(json.dumps(content.dump(), indent=2))
    (out / "wiring.svg").write_text(wiring_svg(content))
    (out / "guide.md").write_text(guide_markdown(content))
    (out / "export.zip").write_bytes(export_zip(content))
    (out / "viewer.html").write_text(render_viewer(content), encoding="utf-8")
    _out(f"{content.project_name}: {len(content.nodes)} parts, est. ${content.bom['estimateTotalUsd']:.2f}, "
         f"{len([i for i in content.issues if i.severity == 'error'])} error(s) -> {out}/")  # fmt: skip
    for i in content.issues:
        _out(f"  [{i.severity}] {i.code}: {i.message}")
    return 0


def cmd_image2mesh(a: argparse.Namespace) -> int:
    mesh, report = image_to_mesh(Path(a.image).read_bytes(), a.mode, a.width, a.height)
    out = Path(a.out)
    data = (
        M.to_glb([M.SceneNode("part", mesh)])
        if out.suffix == ".glb"
        else M.to_obj([mesh]).encode()
        if out.suffix == ".obj"
        else M.to_stl_binary(mesh)
    )
    out.write_bytes(data)
    _out(json.dumps(report))
    return 0


def cmd_layout(a: argparse.Namespace) -> int:
    doc = json.loads(Path(a.project).read_text())
    content = ProjectContent.model_validate(doc.get("content", doc))
    _out(json.dumps(solve_layout(content), indent=2))
    return 0


def cmd_bom(a: argparse.Namespace) -> int:
    doc = json.loads(Path(a.project).read_text())
    content = ProjectContent.model_validate(doc.get("content", doc))
    sys.stdout.write(build_bom(content.nodes, a.qty, use_offers=not a.estimates).to_csv())
    return 0


def cmd_serve(a: argparse.Namespace) -> int:  # pragma: no cover - starts a server
    import uvicorn

    uvicorn.run("hwbp.api:app", host=a.host, port=a.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hwbp", description="Hardware blueprint decision engine")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="prompt and/or picture -> full project")
    g.add_argument("prompt", nargs="?", default="")
    g.add_argument("--image", help="PNG/PGM/PPM (JPEG with Pillow) used for the form factor")
    g.add_argument("-o", "--out", default="out")
    g.set_defaults(fn=cmd_generate)
    i = sub.add_parser("image2mesh", help="picture -> STL/OBJ/GLB")
    i.add_argument("image")
    i.add_argument("-o", "--out", default="part.stl")
    i.add_argument("--mode", choices=["extrude", "relief", "revolve"], default="extrude")
    i.add_argument("--width", type=float, default=100.0, help="mm")
    i.add_argument("--height", type=float, default=10.0, help="mm (extrusion / relief height)")
    i.set_defaults(fn=cmd_image2mesh)
    lay = sub.add_parser("layout", help="solve the 3D layout of any project document (incl. blueprint.io exports)")
    lay.add_argument("project")
    lay.set_defaults(fn=cmd_layout)
    b = sub.add_parser("bom", help="BOM CSV for N builds")
    b.add_argument("project")
    b.add_argument("--qty", type=int, default=1)
    b.add_argument("--estimates", action="store_true", help="use estimatedCost only (reference behaviour)")
    b.set_defaults(fn=cmd_bom)
    s = sub.add_parser("serve", help="run the HTTP protocol server")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
