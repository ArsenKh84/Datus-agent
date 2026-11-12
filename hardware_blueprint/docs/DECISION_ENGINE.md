# Decision engine — algorithms

```
prompt ─┐                          ┌─ VisionModel (ClaudeVision when a key is set) ─┐
picture ┴─ image.py: decode → Otsu → components → outline + panel features ───────┴─ form factor + parts + prompt
            │                                     (no prompt, no vision → ask "what should it do?")
            │
requirements.py   capabilities, counts, power source, runtime, budget, size class, priorities, materials, keywords
            │
selection.py      needs → set-cover peripherals → MCU feasibility + ranking → re-score vs MCU logic → drivers → budget
            │
wiring.py         pin allocation → data netlist → level-shifter insertion
            │
power.py          rail voltages → source/battery → regulators (efficiency-ranked feeds) → current propagation → runtime
            │
wiring.validate   ERC (supply ranges, rail budgets, logic, I2C addresses, pin conflicts, drivers, reserved pins, orphans)
            │
mech.py           skyline packing → enclosure/lid/chassis/arm links → delta connections → reference-compatible solver
            │
cad.py / mesh.py  feature specs → watertight meshes (STL/OBJ/GLB) → print cost; CAD jobs + credits
            │
pricing.py        offers → quotes (breaks, MOQ, pack, stock, trust) → vendor consolidation → BOM + roll-up
            │
instructions.py, firmware.py, enrichment.py, export.py → project document + viewer + ZIP
```

Everything is deterministic and offline. LLMs plug in at well-defined seams (§9) and their output is always
validated back into the same models.

## 1. Requirement extraction (`requirements.py`)
Regex rule tables map phrases to capabilities (`sense.*`, `actuate.*`, `ui.display`, `comm.*`, `output.*`,
`input.*`), power source (solar > wall cues > battery cues; drive robots imply battery), size class, priorities
(cost, battery_life, precision, performance, robustness) and materials (word-bounded). Counts: "4-axis",
"12 servos", quadruped → 12, hexapod → 18, 4WD → 4 motors. Runtime ("2 weeks" → 336 h) and budget ("under $60").
`keywords` (salient prompt words) later act as technology preferences. `design_hash` = sha256 of the normalised
capability/power/size/priority tuple (16 hex), like the reference `designHash`.

## 2. Component selection (`selection.py`)
**Scoring.** For a candidate set, each criterion is normalised to 0..1 within the set:
cost = min_price / price, power = min_current / current (sleep-weighted when battery life matters),
size = min_volume / volume, popularity, fit = matched preferred capabilities (+0.5 per prompt keyword hit),
compat = logic-level compatibility with the MCU. Default weights cost .30, popularity .20, power .15, size .10,
fit .15, compat .10 — priorities rescale them (cost ×2; battery life/solar → power ×2.5; precision → fit ×2;
tiny/small → size ×2.5) and they are renormalised.

**Hard filters.** `require` capabilities (touch, e-paper), user `force`/`exclude` overrides, and *named technology*:
when the prompt names a technology that some candidates match and others don't ("radar", "ultrasonic", "oled"),
only the matching ones stay.

**Peripherals = weighted greedy set cover.** Value = score × (1 + 0.6 (covered − 1)), so one BME280 beats separate
temperature + humidity + pressure sensors. Per-unit capabilities (servos, motors, buttons, distance sensors) become
one node per unit because each needs its own pin.

**MCU.** Feasibility: required radios ⊆ board radios; UART/ADC counts; camera/CSI; Linux SBCs have no ADC; and a
full pin-allocation dry run (drivers that don't exist yet are modelled: > 2 servos → one PCA9685, every 2 DC motors
→ a 4-pin H-bridge, each pump → a MOSFET). Ranking = score − 0.08 × (#peripherals needing level shifting)
+ 0.1 × min(1, spare_pins / 30 %).

**Drivers.** DC motors → dual H-bridge with VM range ∋ motor voltage and I_ch ≥ 0.8 × stall (one per 2 motors,
plus a caster for 2-wheel robots); > 2 servos → PCA9685; pumps → logic-level MOSFET.

**Budget.** While over budget: swap the slot with the best saving / score-loss ratio among compatible alternatives;
if still over, raise `budget_exceeded`.

**Component offering.** Every slot keeps its top-3 alternatives (score, price delta, reasons); additions are proposed
from the design state (power switch for batteries, current monitor, I2C mux when addresses collide).

## 3. Pin allocation & netlist (`wiring.py`)
I2C and SPI buses bind to the MCU's default bus pins (shared by all devices; SPI gets one CS per device); each UART
device gets its own hardware port with TX↔RX crossover. Remaining signals need a capability (gpio | pwm | adc) and are
assigned **most-constrained-first**, each taking the candidate pin with the fewest capabilities (keeps scarce ADC/PWM
pins free). Reserved/strapping pins are never used; ESP32-S3 ADC is restricted to ADC1 (ADC2 is unusable with Wi-Fi).
Direction follows information flow (sensors/inputs → MCU, MCU → displays/actuators). Signals of logic-incompatible
parts are routed through 4-channel BSS138 shifters (LVn on the MCU side, HVn on the part side; LV/HV supplied from
the logic and 5 V rails). Diagram edges are aggregated exactly like the reference (power per unordered pair with
the voltage label, data per directed pair with `proto ×n`).

## 4. Power architecture (`power.py`)
1. **Rail choice per load**: MCU-logic voltage > 5 V > VBAT > 12 V, inside the load's input range; MCUs prefer 5 V
   (on-board LDO); actuators never use the logic rail; a raw battery rail is allowed only if the *whole* battery range
   (empty…charged) fits — for motors only the upper bound matters.
2. **Source**: wall → USB-C 5 V (or 12 V + buck when a load needs > 5.6 V). Battery/solar →
   P_avg = Σ V·I_avg / 0.85 (duty-cycled: pump 2 %, servo 15 %, DC motor 50 %; MCUs/radios 15 % active when low-power),
   E = P_avg × runtime / 0.85; packs that meet E are kept (runtime is a requirement), then ranked; 2S when a load needs
   > 5.5 V or servo peak > 3 A; max discharge ≥ 0.8 × peak. 1S Li-ion/LiPo adds a USB-C charger + protection (or
   MPPT + panel + DW01 for solar).
3. **Regulators**: topology by input range vs output (buck/LDO, boost, buck-boost); every existing higher rail and the
   primary source are tried as feeds and the chain with the best end-to-end efficiency wins (e.g. VBAT → buck-boost
   3.3 V at 90 % beats VBAT → boost 5 V → LDO 3.3 V at 58 %). The MCU board's 3V3 output is reused up to 300 mA.
4. **Current propagation** bottom-up: switcher I_in = V_out·I_out/(η·V_in) + I_q; LDO I_in = I_out + I_q; the MCU's
   on-board LDO adds to the MCU's rail. Peaks use diversity: one DC motor at stall (firmware soft-start), > 4 servos at
   50 % of stall.
5. **Estimates**: runtime = Wh × 0.85 / P_avg; solar harvest = V·I × 4 sun-hours × 0.75 vs 24 h consumption.

## 5. Electrical rule check (`wiring.validate`)
supply voltage within the pin's rating · rail peak ≤ source rating (error) and typical ≤ 80 % (warning) · I2C address
conflicts resolved with alternate addresses, else error + TCA9548A suggestion · non-bus pin used twice · reserved pin
used · orphan / unpowered parts (passives exempt) · motors/pumps without a driver · battery peak discharge.

## 6. Mechanical synthesis & solver (`mech.py`)
**Synthesis**: internal boards are packed on the floor with a skyline bottom-left heuristic (largest area first,
strip width = max(widest part, √(1.6 × Σ area))); panel parts (displays, controls) are packed under the lid;
enclosure = inner footprint + 2 × (wall 2 + clearance 4), height = floor 2 + standoffs 5 + tallest board + clearance 4
+ tallest panel part, rounded up to 5 mm; picture form factors keep the drawn aspect ratio. Drive robots get a chassis
plate with motors at the corners; servo arms a chain of printed links; external parts (strips, pumps, probes, panels)
are placed in a row beside the enclosure. Fasteners: 4 screws per board ≥ 300 mm² + 4 lid screws, 4 standoffs per board.
Connections are emitted as `delta = centre(target) − centre(source)`.

**Solver** (reproduces the reference MECH tab): dimension parsing (X = A, Z = B, Y = C), participation rule,
mountsFor implicit edges, undirected BFS tree from the largest enclosure/structural part, explicit deltas, implicit
hold/top/beside rules, component packing (30 mm), bounding box, and an AABB interference check (contained parts —
boards inside a shell — are allowed).

**Sibling grid**: several implicit "top" children of one parent are laid out in a centred grid (largest nearest the
centre, rows along X, wrapping along Z, 5 mm gaps). **Explode**: offset = (|half-extents|·1.5 + 0.05·diagonal)·0.9
along unit(part − bbox centre) (golden-angle fallback), then ≤ 30 de-overlap sweeps along the least-penetration axis.
**Joints** (assembly v2): one rigid joint per tree edge (offset = child − parent); a slider translates the child
subtree along its axis by `pose` mm, a revolute rotates the subtree by `pose`° about the axis through the child
(Rodrigues), with rotated AABB extents |R|·half-extents. **Heal/enrich** (foreign docs): orphan layout parts are
attached to their mount target or the root with implicit placement, then every missing `delta` is back-filled from
the solved positions.

## 7. Meshes & CAD (`mesh.py`, `cad.py`, `image.py`)
Closed, consistently wound primitives (box, open tray, frame with window, cylinder, tube, standoff plate) — verified
by an exact edge-pairing manifold check and divergence-theorem volume. Writers: binary/ASCII STL, OBJ, GLB (glTF 2.0,
flat normals, per-part materials). Feature specs per part: enclosure → open tray, lid → frame with the display
window (module size − 6 mm), mount → standoff plate, wheels → cylinder, else box. Print cost = (shell = area ×
1.2 mm, capped at volume) + infill × core → grams (filament density), hours (8 mm³/s), $/kg + $0.35/h.
Metal structural parts: volume × $/cm³ × 3 (stock waste) + $25 machining.

**Picture → mesh**: Otsu threshold (plateau midpoint for two-level images; alpha channel when present) with
border-majority polarity → largest 4-connected component → Moore-neighbour boundary → Ramer–Douglas–Peucker →
holes = enclosed background components → bridged into the outer contour (earcut technique) → ear clipping →
extrude (prism with hole walls), or **relief** (height map, ≤ 120 cells/side) or **revolve** (half-width profile
lathed around Y, uniform scale). All outputs are watertight. Outline drawings (holes covering > 50 % of the outline
area) are auto-filled to solids; small holes (mounting holes) are kept. STEP export writes an AP214 faceted B-rep
(one FACETED_BREP per mesh) — verified by re-parsing with the independent `steputils` ISO-10303-21 reader.

**Picture → blueprint** (`image.analyze_picture`, `brief_from_image`): the foreground mask is split into
4-connected components; the largest is the device outline. Candidate *panel regions* are (a) holes of a solid
silhouette and (b) other components drawn inside the outline's bounding box, each filled with the area it encloses
(a drawn rectangle becomes a solid rectangle). An outline drawing's interior (> 50 % of the area) is not a region.
Each region is classified by area fraction of the front f, rectangularity r = area / bbox area and aspect a:
rectangle (r ≥ 0.86) with f ≥ 6 % and 1/3.5 ≤ a ≤ 3.5 → **display window** (largest only); f < 3 % round or
squarish → **push button**; long thin rectangle (a > 3) → slot (USB / vent / card, asked); round 3–20 % → knob,
speaker or lens (asked); round ≥ 20 % → round display or speaker (asked). Pictures with < 85 % near-black/near-white
pixels are treated as **photos**: their regions come from lighting, so only the outline is used and a vision model is
recommended. The detected parts are appended to the prompt ("… with a display and 3 buttons") unless the user already
named them, and the display window's share of the front steers selection: f ≥ 20 % rules out `display.small` (no
0.96" OLED behind a large window), f ≤ 8 % rules out `display.large` (`Need.avoid`: excluded while any other candidate
remains). With no prompt and no vision model the run stops in `waiting_for_input` with a `device_function` question
whose options fit the picture (screen + buttons → handheld console / weather station / thermostat / logger; screen
only → clock / dashboard / monitor; buttons only → remote / keypad / doorbell; round → speaker / lamp / timer; long →
light bar / door sensor …), plus free text. `vision.ClaudeVision` sends the picture to Claude with `VISION_PROMPT`
and a strict JSON schema (structured outputs) and server-side refusal fallbacks; any vision error falls back to the
offline path and is reported in `imageAnalysis.uncertainties`.

**Images without AI**: the hero image and per-step illustrations are isometric painter's-algorithm renders of the
solved assembly (shells drawn last and translucent = x-ray), so pictures always match the design; rerolls rotate
the view deterministically.

## 8. Pricing & BOM (`pricing.py`)
Price strings in US/EU formats and currencies → USD (static FX table, replaceable). Quote: order_qty = ceil(max(qty,
MOQ) / pack) × pack; unit = largest break ≤ order_qty; in-stock offers first; rank by total × vendor trust
(DigiKey/Mouser 1.0 … eBay 1.15). Consolidation: repeatedly move all lines of a vendor onto already-used vendors when
the extra cost is below that vendor's shipping fee. `build_bom(nodes, build_qty)`; roll-up category → type → lines.
`use_offers=False` reproduces the reference (estimatedCost only).

## 9. Extension points (where an LLM or live data plugs in)
| Seam | Protocol | Default |
|---|---|---|
| Requirements | `RequirementExtractor.extract(prompt) -> Requirements` | rule-based |
| Picture understanding | `VisionModel.describe(image, mime, VISION_PROMPT) -> {name, prompt, summary, components, uncertainties, approxSizeMm}` | offline drawing analysis + "what should it do?" question; `vision.ClaudeVision` when `ANTHROPIC_API_KEY` is set (`--vision`, `HWBP_VISION`, `HWBP_VISION_MODEL`) |
| CAD | `CadGenerator.generate(node, instruction, parameters) -> Mesh` | parametric |
| Sourcing | `SourcingProvider.offers(part) -> [Offer]` (`NexarProvider` with injected transport) | catalog + search links |
| LLM context | `edits.project_summary(content)` | — |
| Chat answers | `api._answer` | from the document |

## 10. Known limits
Catalog prices are illustrative (replace with live sourcing). The rule-based extractor covers common phrasing, not
arbitrary language — plug in an LLM extractor for open-ended prompts. Meshes are functional placeholders/printable
basics, not organic industrial design; STEP export is faceted (triangles), not NURBS. Offline, a picture gives the
outline and the parts drawn on its front (screen, buttons); what the device *does* comes from the user's answer or a
vision model, and a photo's parts are only recognised by a vision model.
