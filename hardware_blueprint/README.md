# hwbp — hardware blueprint decision engine

A clean-room, offline, deterministic engine that turns a **prompt or a picture** into a buildable hardware
project. It produces component selection, wiring (with exact pins), a power architecture, a 3D mechanical layout,
printable meshes, a priced BOM, an assembly guide and firmware. The project document format is
**wire-compatible with blueprint.io** (see the teardown in [`docs/ANALYSIS.md`](docs/ANALYSIS.md)), and it adds the
electrical engineering that the reference leaves to an LLM.

- [`docs/ANALYSIS.md`](docs/ANALYSIS.md): how blueprint.io works (architecture, protocol, algorithms, constants, weaknesses)
- [`docs/DECISION_ENGINE.md`](docs/DECISION_ENGINE.md): every algorithm in this engine, with formulas
- [`docs/PROTOCOL.md`](docs/PROTOCOL.md): HTTP API (generation runs, chat/edits, parts, CAD, picture→mesh, export, collaboration, community, accounts)
- [`docs/PARITY.md`](docs/PARITY.md): feature-by-feature parity checklist vs blueprint.io, including every page control (109 done, 0 missing, 8 not applicable with reasons), each verified by an executable check, plus the browser end-to-end results
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
uv run hwbp generate --image sketch.png
```

```bash
uv run hwbp image2mesh bracket.png --mode extrude --width 60 --height 6 -o bracket.stl
```

```bash
uv run hwbp serve --port 8000
```

With only a picture, `generate` prints what the picture shows and suggests functions (the web studio asks the same
question). Photos are understood by a vision model: install the `vision` extra and set `ANTHROPIC_API_KEY`
(`--vision auto|claude|none`, default `auto`).

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
| Mechanics | `mech.py` | skyline packing, enclosure/lid/chassis/arm synthesis, reference-compatible solver (delta semantics, BFS tree, sibling grid), interference check, explode view, joints (rigid/slider/revolute), heal/enrich for foreign docs |
| Meshes / CAD | `mesh.py`, `cad.py` | watertight primitives, STL/OBJ/GLB/**STEP** (faceted B-rep, validated with an independent parser), feature specs per part, CAD jobs, credit wallet, print-cost estimate |
| Picture → mesh / CAD | `image.py` | PNG/PGM/PPM decoding (Pillow optional), Otsu, contour tracing, RDP, **holes** (bridged ear clipping), outline-drawing auto-fill; extrude / relief / revolve |
| Picture → blueprint | `image.py`, `vision.py`, `api.py` | silhouette → enclosure form factor; drawn screen window / buttons / slots → parts (window size picks the display); no prompt → "what should it do?" question with fitting suggestions; `ClaudeVision` (structured JSON, offline fallback) for photos |
| Pricing / BOM | `pricing.py` | price parsing, price breaks, MOQ/pack, stock, vendor trust, shipping-aware consolidation, roll-ups, CSV |
| Enrichment | `enrichment.py` | canonicalisation (id/MPN/name/search/transient), spec sheets, sourcing providers (Nexar GraphQL adapter), research queries |
| Guide / firmware | `instructions.py`, `firmware.py` | fabricate → wire → bringup → assemble with real pins; PlatformIO project from the pin map |
| Edits | `edits.py` | reference-format edit plans with auto-repair, NL edits → requirement overrides, optimistic revisions |
| Protocol | `api.py`, `api_ext.py` | FastAPI server mirroring the reference API: long-poll runs + SSE, run questions/retry, image upload, chat turns with approval, WebSocket events, 409 conflicts, CAD jobs + STEP, exports |
| Services | `services.py` | accounts/plans/credits, collaboration (roles, invitations, share links), community (feed, stars, trending, stats, copy), notes with conflicts, input images |
| Renders / ideas | `render.py`, `ideas.py` | isometric hero/step images (x-ray shells), part icons; idea wizard, suggested prompts, sub-step details, part descriptions |
| Web UI | `viewer/studio.html`, `viewer/index.html`, `viewer/site.html` | studio (prompt with idea/upload/draw tools, picture analysis preview, picture-only question, live progress, resume); project page (author, hero + new image, collapsible cost tree, notes, parts with print settings and build-quantity pricing, wiring with parts sidebar, 3D with per-part eye toggles, per-part CAD, axes, reset/full screen, explode, instructions progress and part cards, chat with prompt chips, Share, History, Export menu, live updates); Community, My projects, Pricing, FAQ, About, Account, invitation and share-link pages |

## Verified

- 147 tests (`uv run pytest`), 97 % line coverage, ruff clean.
- Parity: `uv run python scripts/verify_parity.py` → 109 done / 0 failing / 0 missing / 8 not applicable.
- Browser end-to-end against a live server (`uv run hwbp serve --port 8765`, then
  `node scripts/e2e_ui.js http://127.0.0.1:8765 docs/e2e`): 59 / 59 checks pass. It draws a device with a screen and
  three buttons, generates from the picture alone (answering "what should it do?"), then clicks every control on the
  project page, the share dialog (a second user accepts an invitation and gets viewer rights), the share page and the
  site pages. No JS errors; no horizontal overflow at 375 px.
- The mech solver reproduces blueprint.io's own MECH output for a published project: bounding box
  155 × 32.5 × 107.5 mm, same assembly tree, same CAD-eligible count. It also flagged two interferences in that
  LLM-generated layout.
- All generated meshes pass an exact manifold check (every directed edge paired) and have positive volume. Picture
  extrusion with holes matches the analytic area within 5 %.
- The viewer renders without JS errors in light and dark themes, with no horizontal overflow at 375 px.

## Honest limits

- Catalog prices are illustrative list prices (`catalog_data.py`). Plug in a `SourcingProvider` (e.g.
  `NexarProvider` with your API token) for live quotes.
- The default requirement extractor is rule-based. Offline, a picture gives the outline and the parts drawn on its
  front; what a device *does* comes from the user's answer, and the parts in a *photo* are only recognised by a vision
  model (`ClaudeVision` needs an API key; it is tested against the real SDK with a mocked transport, not live).
- Hero and step images are isometric renders of the solved assembly, not photoreal AI images.
- Meshes are parametric/functional (trays, lids with windows, mounts, plates, extrusions), not free-form industrial
  design. STEP is a faceted (triangulated) B-rep, not NURBS surfaces.
- Payments (Stripe), analytics/ad pixels and affiliate monetisation are deliberately not replicated; plan/credit
  purchases are simulated ledger entries. Identity is a header (`X-User-Id`) — plug in real auth for production.
- On an exFAT/FAT drive (e.g. an external SSD on macOS), keep the virtual environment on an APFS disk:
  `UV_PROJECT_ENVIRONMENT="$HOME/.venvs/hwbp" uv sync --group dev` (macOS writes `._*` files on exFAT, which breaks
  wheel installs).
