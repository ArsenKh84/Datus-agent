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
Picture input: the picture always supplies the form factor (silhouette width/depth/aspect → enclosure footprint);
a drawing also supplies front-panel parts (`imageAnalysis.detected`: display window, buttons, slots, knobs; the window's
share of the front → `form_factor.displayFraction`). With a text prompt, or a configured vision model
(`hwbp serve --vision auto|claude|none`, env `HWBP_VISION`; Claude when `ANTHROPIC_API_KEY` is set), the run proceeds.
With neither, the run returns `status: "waiting_for_input"`, `phase: "questions"` and one question
`{id: "device_function", options: [4 functions that fit the picture], freeText: true}`; answer it with
`POST /api/generation-runs/{id}/questions {revision, qa: [{id: "device_function", answer}]}` (422 `answer_required` when
empty; `/retry` is 422 until answered). A failing vision model falls back to this offline path. 

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

## Extended protocol (parity additions)

Identity: every request may carry `X-User-Id` (plug real auth here); default `local-user` (Pro plan). Roles per
project: owner > editor > viewer — saves, chat edits and edit plans need editor; publish/collaboration need owner.
Errors: `{"detail": {"error": <code>, "message": …}}` with the reference codes where known.

| Area | Method & path | Notes |
|---|---|---|
| Runs | `POST /api/generation-runs/{id}/questions {revision, qa:[{id or question, answer}]}` | re-runs into the same project; 409 `stale_revision` |
| Runs | `POST /api/generation-runs/{id}/retry {revision?}` · `GET /api/generation-runs?recoverable=true` | |
| Images | `POST /api/generation-input-images {image: dataURL or base64, mime?, draftKey?}` → `{id, url, hash, mimeType}` · `GET /api/input-images/{id}` | PNG/JPEG/WebP/PNM ≤ 40 MB; runs accept `inputImages: [id]`; snapshot carries `imageAnalysis` |
| Projects | `GET /api/projects` · `POST /api/projects/copy {token}` | list mine (with role) · fork a published project |
| Chat | `GET /api/projects/{id}/chat` · `POST /api/projects/{id}/chat {message, autoApprove:false}` · `POST /api/projects/{id}/chat/{turn}/{approve,dismiss,retry}` · `POST /api/projects/{id}/chat/clear` | turns: `awaiting_approval` (plan preview in reference edit-plan format, 15 min deadline) → `completed` / `outdated` / `expired` / `cancelled` |
| Live | `WS /api/projects/{id}/events` | send `{"type":"authenticate","userId"}`; receive `ready`, `presence`, `project_changed`, `chat_changed`, `readme_changed`; `ping`→`pong` |
| Notes | `GET/PUT /api/projects/{id}/readme {text, baseVersion}` | 409 `readme_conflict` |
| Collaboration | `GET/PATCH/DELETE /api/projects/{id}/members[/{user}]` · `POST/GET/DELETE /api/projects/{id}/invitations[/{inv}]` · `POST /api/project-invitations/accept {token}` · `POST/GET/DELETE /api/projects/{id}/share-links[/{link}]` · `POST /api/share-links/redeem {token}` | codes: `invitation_invalid/expired/cooldown`, `share_link_invalid/expired`, `collaborator_limit_reached` (10), `cannot_change_owner` |
| Community | `POST /api/unpublish` · `GET /api/community?page&limit&sort=trending|new|stars&q` · `GET /api/community/suggest?q` · `POST /api/star {token}` · `GET /api/community/starred` · `GET /api/trending?limit` · `GET /api/stats` | trending = (stars+1)/(age_h+2)^1.5 |
| Parts | `POST /api/parts/nexar-sourcing {partIds}` · `POST /api/parts/{amazon,affiliate,adafruit}-links {partIds}` · `GET /api/parts/image?name&type` · `GET /api/parts/icon.svg` · `GET /api/parts/{id}/photo` | plain (non-affiliate) links; typed SVG icons |
| AI-action equivalents | `POST /api/ideas` · `GET /api/projects/{id}/suggested-prompts?mode` · `POST /api/projects/{id}/part-descriptions/refresh` · `GET /api/projects/{id}/instructions/{step}/{sub}/detail` · `GET /api/projects/{id}/image.svg?variant` · `POST /api/projects/{id}/image-rerolls` · `POST …/image-rerolls/{id}/apply` · `GET /api/projects/{id}/steps/{step}/{sub}/image.svg` | deterministic, no LLM |
| Mechanics | `POST /api/projects/{id}/mech/heal` · `POST …/mech/enrich` · `GET …/mech/explode?factor` · `GET/POST …/mech/joints {joints, save?}` | joints: rigid / slider (mm) / revolute (°) |
| Wiring/BOM | `GET /api/projects/{id}/wiring/layout?mode=simple|enriched` · `GET …/wiring-enriched.svg` · `GET …/bom/merged` | |
| Export | `POST /api/export/cad {projectId, formats:[stl,step,glb,obj], nodeIds?}` (headers `X-Downloaded-Count`, `X-Omitted-Count`) · `POST /api/export/file {projectId, kind}` · `POST /api/export/zip` | kinds: project.json, bom.csv, guide.md, wiring.svg, wiring_enriched.svg, viewer.html, hero.svg, firmware.zip |
| CAD | artifacts now include `model.step` · `POST /api/cad-wallet/purchase {bundle: small|medium|large}` | purchase is simulated (no payment provider) |
| Account | `GET /api/profile/me` · `GET /api/account` · `POST /api/set-username|auto-username|set-bio|set-profile-color` · `POST /api/onboarding/complete` · `GET/POST /api/email-preferences` · `GET|POST /api/get-credits` · `POST /api/consume-credit` · `GET /api/check-subscription` · `POST /api/billing/plan` (simulated) · `POST /api/contact-leads` | free plan: 10 design credits / week (402 `no_credits`) |
| Picture | `POST /api/input-images/{id}/analysis` → `{detected, functionOptions, visionModel}` | offline analysis shown before generating |
| Project page | `GET /api/projects/{id}/meta` → `{role, version, author{username, profileColor}, published{token, url, starCount} or null}` · `GET /api/projects/{id}/cad-jobs` → `{eligible[], done, jobs{nodeId: job}, wallet}` | role-aware controls; Mech sidebar "CAD n/N" |
| Community cards | items of `/api/community`, `/community/starred`, `/trending` carry `ownerId` and `author{username, profileColor}` | |
| Pages | `GET /` (studio) · `GET /p/{id}` (project page + chat) · `GET /s/{token}` (share page + Star/Copy) · `GET /community` · `/projects` · `/pricing` · `/faq` · `/about` · `/account` · `/invitations#token=` · `/join#token=` | live updates need the `websockets` package (in the `api` extra) |
