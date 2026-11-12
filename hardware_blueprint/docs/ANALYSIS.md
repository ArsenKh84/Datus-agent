# blueprint.io — technical teardown

*Snapshot: 2026-11-12. Method and limits are in §0. Everything here is a description of observed behaviour written
for a clean-room re-implementation (`hwbp`); no proprietary source code is reproduced.*

## 0. Method, sources and limits

| Source | Access | Used for |
|---|---|---|
| `https://www.blueprint.io/` HTML + public JS/CSS bundle (`/assets/index-*.js`, 4.57 MB, Vite build) | public | routes, components, client algorithms, API surface |
| Published community project via share link `/s/<token>` → `GET server.blueprint.io/api/published?token=` | public | real project document (38 KB JSON), rendered tabs, screenshots |
| `GET /api/community`, `/api/trending`, `/api/stats`, `/api/features` | public | catalog/feeds/feature flags |
| The project linked in the task (`/project/804df87a-…`) | **private** — redirects anonymous visitors to `/`; the `/join#token=` invite can only be redeemed by a signed-in account (`POST /api/share-links/redeem` with a bearer token) | not accessed |

Server-side code (LLM prompts, CAD script generation, the parts database) is not visible from the client; items
inferred rather than observed are marked *(inferred)*. The private project can be analysed the same way once
shared as a public link or exported (`/api/export/zip`).

Public counters at capture time: 361,934 projects created, 286,138 accounts, 16,948 published community projects.

## 1. Architecture

```
 Browser (Vite + React SPA on Vercel)
   ├─ React Flow (@xyflow/react) + elkjs ............ wiring diagram
   ├─ react-three-fiber + drei (three.js) ........... MECH 3D assembly, GLTFLoader + Draco/Meshopt
   ├─ jszip ......................................... client-side CAD bundle re-packing
   ├─ sketch canvas (lazy chunk; Excalidraw-like)  .. "DRAW" input (inferred)
   └─ PostHog / Sentry / GA / pixels ................ analytics
        │ REST (+ long-poll), WebSocket /api/projects/:id/events, /api/collab/readme
        ▼
 server.blueprint.io (REST, /api/*)  ── LLMs: Gemini lite|max|ultra, OpenAI tiers; Brave search grounding
        │                               ── Nexar/Octopart sourcing, affiliate link services, ForgeCAD (CAD)
        ▼
 Supabase: auth (JWT bearer), Postgres (projects, project_members, subscriptions, parts_sourcing; RPCs
           get_chat_messages, get_my_project_role, get_my_paid_access), Storage buckets
           project-images (public) and part-asset-files (signed URLs, 1 h)
```

Client routes: `/`, `/blueprint` (new generation), `/community`, `/projects`, `/project/:id`, `/s/:token` (public
share), `/about`, `/pricing`, `/faq`, `/account`, `/onboarding`, `/email/preferences`, `/email/respond`,
`/invitations`, `/join`. Project tabs: Chat, **Info**, **Parts**, **Wiring**, **Mech**, **Instructions** (there is no
code/firmware tab; README is a collaborative "Notes" field in Info, edited over a WebSocket).

## 2. Project document (wire format)

```jsonc
{
  "projectName", "originalPrompt", "projectDescription", "plan" /* markdown bullets */, "notes" /* tags */,
  "projectImageUrl", "projectImageHash",
  "imagePromptSnapshot": { "tags", "designHash" /* 16 hex */, "description", "technicalDescription" },
  "nodes": [{
     "id" /* semantic slug */, "name", "type", "category" /* electrical|mechanical */, "partId", "productName",
     "quantity", "estimatedCost" /* USD per unit, LLM estimate */, "dimensions" /* "A x B x C mm" */,
     "pins" /* string[] (electrical) */, "description", "material", "printSettings", "mountsFor",
     "purchaseUrl", "ebayUrl", "aliexpressUrl", "imageUrl",
     "research": { "specs": [{label,value}], "sourcing": [{url,price,vendor}], "datasheetUrl", "documentationUrls": [{url,title}] }
  }],
  "electricalConnections": [ {type:"power",source,target,voltage,current} | {type:"data",source,target,protocol,sourcePin,targetPin} ],
  "mechanicalConnections": [ {source,target,label,delta:{x,y,z}} ],
  "instructionPreamble": { "tools": [], "assumptions": [] },
  "instructionSteps": [ { "id": "fabricate|wire|bringup|assemble", "subSteps": [{id,title,partIds}] } ]
}
```

**Type enum (12)** — electrical: `mcu, sensor, actuator, power, module, display`; mechanical: `structural, enclosure,
mechanism, misc, 3d_printed, other` (unknown → `other`; legacy aliases `fastener→misc`, `bearing|wheel|gear→mechanism`;
any node with pins is forced to `electrical`). Colours (dark theme): mcu `#06abab`, sensor `#418dbf`, actuator
`#df663b`, power `#f5c94e`, module `#7253ad`, display `#db87a1`, structural `#6fb2fd`, enclosure `#a58be0`,
mechanism `#e8a25b`, misc `#b7a26b`, 3d_printed `#bfd36a`, other `#9d9f9e`. Sort keys 0–5 electrical, 10–14 mechanical, 20 other.

## 3. Generation pipeline (prompt / picture → project)

1. **Input** — text prompt, uploaded/pasted image (PNG/JPEG/WebP ≤ 40 MB; re-encoded if > 5 MB or > 2048 px) or a
   drawing. Images are uploaded first: `POST /api/generation-input-images {image:dataURL, draftKey}` → `{id,url,hash,mimeType}`.
   Image input is a paid feature. The run snapshot can carry `imageAnalysis {summary, components[], uncertainties[]}`.
2. **Admission** — `POST /api/generation-runs {idempotencyKey, prompt, modelProvider, modelTier, inputImages?,
   targetProjectId?, baseVersion?, historyEpoch?}` → RunSnapshot. 402 = no credits.
3. **Progress** — HTTP long-poll `GET /api/generation-runs/:id?waitMs=10000&sinceRevision=N` (client loop every
   250 ms, recovery via `?recoverable=true` on focus/online). No SSE/websocket for runs.
   - `status`: queued · running · waiting_for_input · completed · failed · cancelled
   - `phase`: planning → questions → electrical (∥ mechanical) → mechanical → revealing → guide → done
   - `sections{parts,wiring,mech,info,guide}` unlock tabs; `flags{guide,image,info}` drive background spinners;
     `subStatus` per phase; `questionRounds` (multiple choice + "Other") answered via
     `POST …/questions {revision, qa:[{question,answer}], moreQuestions}`; `…/retry`, `…/cancel {revision}`.
   - `projectData` grows in place; the server persists the project and returns `savedVersion`/`historyEpoch`.
4. The LLM prompts live server-side. The client only contains a project-summary context builder (nodes / connections /
   mounts / disconnected nodes as text) and per-type research prompt tables (specs, sourcing ≥ 3 vendors, official
   docs only, CAD search URLs) with Brave search query templates.

## 4. Chat / edit loop

- `POST /api/projects/:id/chat/classify {message}` → `{mode:"edit"|"ask", nextAction, decisionReceipt}`.
- Shared chat: `GET/POST /api/projects/:id/chat` (idempotent, `responseMode:"ack"`, polled every 4 s), turns move
  through `queued → running → awaiting_approval → applying → saving → completed` (or `save_pending`, `outdated`,
  `expired`, `cancelled`, `failed` with a provider error code); `POST …/chat/:turnId/{approve|dismiss|retry}`.
- **Edit plans are patches**, never full replaces: `{kind:"plan", plan, addParts, removeNodeIds,
  addElectricalConnections, removeElectricalConnections, addMechanicalConnections, removeMechanicalConnections,
  newPinsUsedOnExistingNodes}` or `{kind:"clarification"|"answer", message}`. The client auto-repairs (rename colliding
  ids `_2`, fuzzy-match unknown endpoints, declare new pins, dedupe, drop dangling edges), validates, previews, and on
  approve canonicalises new parts (`POST /api/parts/canonicalize`), re-solves the 3D assembly and saves.
- **Saves**: `POST /api/projects/save {…document, baseVersion, historyEpoch, snapshot:"edit"}`; 409 =
  `version_conflict | history_conflict | readme_conflict` → three-way merge UI. `GET /head` every 4 s →
  `{version, historyEpoch, cadRevision, activeRun, author}`. Versions/restore endpoints exist.
- Live notifications: WebSocket `/api/projects/:id/events` (`ready, ai_activity_changed, chat_changed,
  access_changed, image_reroll_changed, presence`).

## 5. Parts, BOM, sourcing and suggestions

- **BOM maths**: unit price = the LLM's `estimatedCost`; subtotal = qty × unit, always shown with a `~` and `$x.xx`.
  INFO tree = category → type → part (type rows count lines, leaves show quantity). PARTS merges rows with the same
  `productName` using the first row's price (mis-prices duplicates). Sourcing price strings are only parsed to sort
  offers (Amazon first, then AliExpress, verified, cheapest, unpriced); there is no single "best offer" logic and
  `purchaseUrl` is not used by any screen. Affiliate tags on Amazon/eBay; AliExpress links come from the server.
- **Endpoints**: `/api/parts/canonicalize {node}→{node}`; `/api/parts/enrich-transient {productName, category,
  sources}` → marketplace offers; `/api/parts/nexar-sourcing {partIds}` → offers/documents/datasheet/CAD files;
  `/api/parts/{amazon,affiliate,adafruit}-links {partIds}` → `{links}`; `/api/parts/:id`; `/api/parts/:id/photo` (302
  to a signed storage URL); `/api/parts/image?name=` (auth); `/api/part-files/<hh>/<sha256>.pdf` (datasheet proxy).
- **Suggestions**: catalog search and "Search alternatives" call the same AI part search with the project summary as
  context; there is no client-side ranking. Swapping a part happens only through a chat edit plan.

## 6. Wiring diagram

- **Simple mode** (default): one 200×180 card per electrical node, ≤ 8 pin chips (labels > 7 chars → 6 + "…").
  Power edges aggregated per *unordered* pair, label = first voltage (`/\d+\.?\d*V/`); data edges per *directed* pair,
  label = protocol counts in insertion order, `p ×n` when n > 1 (e.g. `uart ×2`, `spi ×2, gpio ×2`). Data = solid
  green `#52ae59` 2 px, power = dashed `#f5c94e` `4 4` 1.5 px, arrowheads; **no ground edges are drawn**.
- **Layout**: elkjs `layered`, direction RIGHT, ORTHOGONAL routing, NETWORK_SIMPLEX node placement, LAYER_SWEEP
  crossing minimisation, spacings nodeNode 100 / between layers 200; ports FIXED_SIDE. ELK only places nodes — React
  Flow re-routes `smoothstep` edges and re-picks handles by geometry. Fallback grid if ELK fails.
- **Enriched mode**: per-pin ports (power-like pins top, the rest split left/right), synthetic rail nodes
  (`rail:5V`, `rail:3.3V`, `rail:GND`) placed above the diagram; `POST /api/wiring/infer-rails
  {electricalNodes:[{id,pins}], connections}` → `{addElectricalConnections}`, optionally followed by a paid LLM job
  that returns add/remove deltas.
- **Validation is topological only**: orphan parts and groups without a power source / ground. No voltage,
  current, logic-level, pin-conflict or protocol checks.

## 7. Mechanical layout (MECH tab) — computed in the browser, no network calls

- **Dimensions**: first `A x B x C` (also `×`) in the string; units ignored. **X = A, Z = B, Y(up) = C**, each
  ≥ 1 mm; default 20×10×20. Parts named like wheel/caster/shaft/18650/bearing render as cylinders.
- **Participation**: `type ∉ {misc, other}` and name !~ `screw|bolt|nut|washer|standoff|wire|cable|jumper` (this is
  the "CAD n/N" denominator).
- **Tree**: `mountsFor` adds an implicit edge when the pair has none; edges are undirected; root = largest-volume
  `enclosure`, else `structural`, else any part; BFS in id order.
- **Placement**: `delta = centre(target) − centre(source)` in world mm (not scaled, not face-relative); root bottom
  at y = 0. Without delta: mount pairs stack (hold top/below), a child that fits the parent's footprint goes on top,
  otherwise beside (+X, 5 mm), with centred grids for siblings and a 5 mm push-apart pass. Components are packed 30 mm
  apart, 4 per row. Dimension overlay = AABB of visible parts (rounded).
- **Scene**: 0.01 scene units per mm, translucent boxes (0.15 electrical / 0.25 mechanical), 15° edges, centre-to-centre
  connection lines, explode view, joint editing (rigid / revolute / slider) stored as an assembly v2 joint tree.
- Verification: `hwbp.mech.solve_layout` reproduces the published sample exactly (155 × 32.5 × 107.5 mm, displayed
  155/33/108; same tree; "CAD 0/10").

## 8. CAD ("Powered by ForgeCAD")

`POST /api/cad-jobs/part {projectId, nodeId, mode:create|edit, instruction, parameters, modelTier}` → job; poll
`GET /api/cad-jobs/:id` every 2.5 s through `queued → running → succeeded|failed|canceled`; artifacts `model.glb`,
`model.stl`, `model.step` (GLB is rendered: Z-up mm, rotated −90° about X, centred, yaw-matched to the box).
Billing de-duplicates by `partId > productName > name > id`. Wallet `GET /api/cad-wallet → {balance, unlimited}`;
bundles 20 / 120 / 560 credits for $10 / $50 / $200; Basic 5/month, Pro 40, Ultra unlimited.
Exports: `/api/export/cad` (zip per format), `/api/export/zip`, `/api/export/file` (server-built).

## 9. Plans and gating

Models: gemini lite/max/ultra, OpenAI tiers. Plans: free (10 design credits / week), Pro ($20), Ultra ($200).
`/api/get-credits → {weekly_credits_remaining}`, `/api/check-subscription`, `/api/features → {proTrial, projectCollabSaleEnabled}`,
`/api/capabilities → {clearChatHistory, …}`. Collaboration (share links, invitations, roles owner/editor/viewer) is Pro.

## 10. Observed weaknesses → how `hwbp` addresses them

| blueprint.io | hwbp |
|---|---|
| Electrical correctness left to the LLM; checks are topological only | ERC: supply ranges, rail current budgets with propagation, logic levels (auto level shifters), I2C address conflicts, pin conflicts, missing drivers, reserved pins |
| No ground net in the document | explicit `ground` connections (+ `infer-rails` for foreign docs) |
| Pins chosen by the LLM | constraint-based MCU pin allocation (bus defaults, capability-aware, most-constrained-first) |
| Mechanical deltas generated by the LLM can interpenetrate (2 overlaps found in the published sample) | enclosure synthesis by skyline packing + AABB interference check |
| BOM = LLM estimates; duplicates mispriced; no MOQ/price breaks | offer quotes with price breaks, MOQ/pack rounding, stock, vendor trust, shipping-aware consolidation; build-quantity BOMs |
| No firmware | PlatformIO project from the real pin map |
| CAD needs paid credits per part | deterministic parametric meshes (watertight STL/OBJ/GLB) for every printable part, plus picture→mesh |
