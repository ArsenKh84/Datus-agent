# hwbp — hardware blueprint decision engine

A clean-room, offline, deterministic engine that turns a **prompt or a picture** into a buildable hardware
project. It produces component selection, wiring (with exact pins), a power architecture, a 3D mechanical layout,
printable meshes, a priced BOM, an assembly guide and firmware. The project document format is
**wire-compatible with blueprint.io** (see the teardown in [`docs/ANALYSIS.md`](docs/ANALYSIS.md)), and it adds the
electrical engineering that the reference leaves to an LLM.

- [`docs/ANALYSIS.md`](docs/ANALYSIS.md): how blueprint.io works (architecture, protocol, algorithms, constants, weaknesses)
- [`docs/DECISION_ENGINE.md`](docs/DECISION_ENGINE.md): every algorithm in this engine, with formulas
- [`docs/PROTOCOL.md`](docs/PROTOCOL.md): HTTP API (generation runs, chat/edits, parts, CAD, picture→mesh, export)
- [`schema/project.schema.json`](schema/project.schema.json): JSON Schema of the project document
- [`examples/`](examples): generated projects (open `viewer.html`) and picture→mesh outputs

## Quick start

```bash
cd hardware_blueprint
uv sync --group dev
```

```bash
uv run hwbp generate "A portable tactical radar with a touchscreen display, carbon fiber enclosure" -o out/radar
```

```bash
uv run hwbp generate "desk lamp with LED strip and knob" --image sketch.png -o out/lamp
```

```bash
uv run hwbp image2mesh bracket.png --mode extrude --width 60 --height 6 -o bracket.stl
```

```bash
uv run hwbp serve --port 8000
```

`generate` writes `project.json`, `viewer.html` (Info / Parts / Wiring / Mech-3D / Instructions / Code), `wiring.svg`,
`guide.md` and `export.zip` (adds the BOM CSV, firmware/, cad/assembly.glb|obj and cad/parts/*.stl).
`hwbp layout project.json` solves the 3D layout of any document, including blueprint.io exports, and
`hwbp bom project.json --qty 100` prices a production run.

## What it does

| Stage | Module | Highlights |
|---|---|---|
| Requirements | `requirements.py` | capabilities, counts, power source, runtime, budget, size, priorities, materials, keywords |
| Classification | `classify.py` | chat intent (edit/ask + structured intent), part type/category from free text |
| Component selection | `selection.py` | weighted set cover, MCU feasibility (pin-allocation dry run), multi-criteria scoring, named-technology filter, drivers, budget downgrades, ranked alternatives (**component offering**) |
| Wiring | `wiring.py`, `layout.py` | bus/pin allocation, UART crossover, level-shifter insertion, ground net, ERC, reference-style edge labels, layered (Sugiyama) layout, SVG |
| Power | `power.py` | rail planning, battery sizing from an energy budget, charger/protection, efficiency-ranked regulator chains, current propagation, runtime and solar balance |
| Mechanics | `mech.py` | skyline packing, enclosure/lid/chassis/arm synthesis, reference-compatible solver (delta semantics, BFS tree), interference check |
| Meshes / CAD | `mesh.py`, `cad.py` | watertight primitives, STL/OBJ/GLB, feature specs per part, CAD jobs, credit wallet, print-cost estimate |
| Picture → mesh / CAD | `image.py` | PNG/PGM/PPM decoding (Pillow optional), Otsu, contour tracing, RDP, **holes**, ear clipping; extrude / relief / revolve |
| Picture → blueprint | `image.py`, `api.py` | silhouette → enclosure form factor; pluggable `VisionModel` → requirements prompt |
| Pricing / BOM | `pricing.py` | price parsing, price breaks, MOQ/pack, stock, vendor trust, shipping-aware consolidation, roll-ups, CSV |
| Enrichment | `enrichment.py` | canonicalisation (id/MPN/name/search/transient), spec sheets, sourcing providers (Nexar GraphQL adapter), research queries |
| Guide / firmware | `instructions.py`, `firmware.py` | fabricate → wire → bringup → assemble with real pins; PlatformIO project from the pin map |
| Edits | `edits.py` | reference-format edit plans with auto-repair, NL edits → requirement overrides, optimistic revisions |
| Protocol | `api.py` | FastAPI server mirroring the reference API (long-poll runs + SSE, 409 conflicts, CAD jobs, publish) |

## Verified

- 104 tests (`uv run pytest`), 97 % line coverage, ruff clean.
- The mech solver reproduces blueprint.io's own MECH output for a published project: bounding box
  155 × 32.5 × 107.5 mm, same assembly tree, same CAD-eligible count. It also flagged two interferences in that
  LLM-generated layout.
- All generated meshes pass an exact manifold check (every directed edge paired) and have positive volume. Picture
  extrusion with holes matches the analytic area within 5 %.
- The viewer renders without JS errors in light and dark themes, with no horizontal overflow at 375 px.

## Honest limits

- Catalog prices are illustrative list prices (`catalog_data.py`). Plug in a `SourcingProvider` (e.g.
  `NexarProvider` with your API token) for live quotes.
- The default requirement extractor is rule-based. Inferring a device's *function* from a photo requires a vision
  LLM (`VisionModel`); offline, a picture only sets the form factor (or becomes a mesh).
- Meshes are parametric/functional (trays, lids with windows, mounts, plates, extrusions), not free-form industrial
  design. STEP export is not implemented.
