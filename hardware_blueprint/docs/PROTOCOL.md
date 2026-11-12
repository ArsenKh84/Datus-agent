# hwbp protocol

HTTP API served by `hwbp.api` (`uv run hwbp serve` or `uvicorn hwbp.api:app`). Shapes follow the observed
blueprint.io protocol where one exists, so clients written for one can be pointed at the other with small changes.
The project document schema is [`schema/project.schema.json`](../schema/project.schema.json) (camelCase, a
superset of the reference format; unknown fields are preserved on round-trip).

## Generation runs (prompt / picture → project)

| Method & path | Body / query | Response |
|---|---|---|
| `POST /api/generation-runs` | `{prompt?, idempotencyKey?, inputImages?: [{mime, data: base64}], answers?: {power_source, runtime_hours, budget_usd}}` | `RunSnapshot` (same key → same run) |
| `GET /api/generation-runs/{id}` | `waitMs` (≤ 30000), `sinceRevision` | `RunSnapshot`, returns early when `revision > sinceRevision` |
| `GET /api/generation-runs/{id}/events` | — | `text/event-stream`: `progress` (snapshot without projectData), `ping`, `done` |
| `POST /api/generation-runs/{id}/cancel` | — | `RunSnapshot` |

```jsonc
RunSnapshot {
  "id", "status": "queued|running|waiting_for_input|completed|failed|cancelled",
  "phase": "planning|questions|electrical|mechanical|revealing|guide|done",
  "revision": 7,                         // monotonic; long-poll cursor
  "projectId": "…" | null,               // set when the run is persisted (phase done)
  "sections": {"parts","wiring","mech","info","guide": bool},
  "messages": ["…"], "questionRounds": [{"questions": [{id, question, options, default}]}],
  "failure": null | "Error: …", "projectData": ProjectDocument | null
}
```
Picture input: with a text prompt the picture supplies the form factor (silhouette width/depth/aspect →
enclosure footprint); without one a `VisionModel` must be configured (else 400). 

## Projects

| Method & path | Body | Response / errors |
|---|---|---|
| `GET /api/projects/{id}` | — | `{id, version, content, updatedAt}` |
| `GET /api/projects/{id}/head` | — | `{version, updatedAt, activeRun}` |
| `POST /api/projects/save` | `{projectId, content, baseVersion}` | `{version}`; **409** `{error:"version_conflict", currentVersion}` |
| `GET /api/projects/{id}/versions` | — | `{versions: [1,2,…]}` |
| `POST /api/projects/{id}/restore/{version}` | — | `{version}` (restore = new version) |

## Chat & edits

| Method & path | Body | Response |
|---|---|---|
| `POST /api/projects/{id}/chat/classify` | `{message}` | `{mode: edit|ask, intent, targets, confidence, decisionReceipt}` |
| `POST /api/projects/{id}/chat` | `{message, baseVersion?}` | ask → `{mode:"ask", answer}`; edit → `{applied, ops, version}`; 409 on stale base |
| `POST /api/projects/{id}/edit-plan` | `{plan, baseVersion?, dryRun?}` | `{version, repaired[], dropped[]}` or `{plan, …}` when `dryRun` |
| `GET /api/projects/{id}/suggestions` | `nodeId?` | `{suggestions: [{kind, nodeId, catalogId, name, score, priceDeltaUsd, reasons}]}` |

Intents: `swap_part, remove_part, add_part, set_quantity, change_power, reduce_cost, resize, regenerate, modify, question`.
Natural-language edits become requirement overrides (`force`, `exclude`, add/remove capability, power source,
budget, counts, runtime) and the design is regenerated deterministically (stable node ids).

Edit plan (reference format): `{addParts[], removeNodeIds[], addElectricalConnections[], removeElectricalConnections[],
addMechanicalConnections[], removeMechanicalConnections[], newPinsUsedOnExistingNodes[{nodeId, pins}]}`. Repairs:
fuzzy id matching (≥ 0.75), colliding ids renamed `_2…`, undeclared pins declared, duplicates/dangling edges dropped,
added parts canonicalised against the catalog.

## Parts

| Method & path | Body / query | Response |
|---|---|---|
| `GET /api/parts/search` | `q, type?, limit?` | `{results: [{id, name, type, score}]}` |
| `GET /api/parts/{id}` | — | catalog part (ratings, interfaces, MCU pin map, offers, firmware hints) |
| `POST /api/parts/canonicalize` | `{name?|productName?, mpn?, description?, dimensions?}` | `{catalogId, matchedBy: id|mpn|name|search|transient, confidence, part}` |
| `POST /api/parts/enrich-transient` | node-like JSON | `{node (research filled), classification, researchQueries}` |
| `POST /api/wiring/infer-rails` | `{electricalNodes: [{id, pins}], connections}` | `{addElectricalConnections}` (grounds + missing supplies) |

## BOM, wiring, export

`GET /api/projects/{id}/bom?buildQty=N` → `{lines, rollup, partsTotalUsd, shippingUsd, totalUsd, buildQty}`;
`GET …/bom.csv`; `GET …/wiring.svg?dark=`; `GET …/export.zip` (project.json, bom.csv, wiring.svg, guide.md,
viewer.html, firmware/, cad/assembly.{glb,obj}, cad/parts/*.stl).

## CAD & meshes

| Method & path | Body | Response / errors |
|---|---|---|
| `POST /api/cad-jobs/part` | `{projectId, nodeId, mode?, instruction?, parameters?}` | job `{id, status, artifacts[], stats{triangles, volumeMm3, areaMm2, size, sha256, print{filament, grams, hours, cost_usd}}}`; 402 no credits; 422 not CAD-eligible |
| `GET /api/cad-jobs/{id}` | — | job |
| `GET /api/cad-jobs/{id}/artifacts/{model.stl|model.glb|model.obj}` | — | binary |
| `GET /api/cad-wallet` | `projectId?` | `{balance, unlimited, eligible?, billable?}` |
| `POST /api/image-to-mesh` | `{image: base64, mode: extrude|relief|revolve, widthMm, heightMm, format: stl|glb|obj}` | `{report{holes, watertight, volumeMm3, sizeMm, silhouette…}, format, data: base64}`; 422 bad input |

## Publishing

`POST /api/publish {projectId}` → `{token, url:"/s/<token>"}`; `GET /api/published?token=` →
`{publishedProjectId, contentRevision, content, starCount, publishedAt}`; `GET /api/published/head?token=`.

## Meta

`GET /api/features`, `GET /api/capabilities`. Errors use FastAPI's `{"detail": …}` envelope.
