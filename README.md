<p align="center">
  <img src="docs/images/flowkit_banner.svg" width="720" alt="FlowKit" />
</p>

<h1 align="center">FlowKit — Self-hosted API for Google Flow</h1>

<p align="center">
  Open-source REST API and automation layer for <b>Google Flow</b>:<br/>
  <b>Nano Banana image generation</b>, <b>Omni Flash / Veo video generation</b>, image-to-video, references, project automation, exports and polling.
</p>

<p align="center">
  <a href="https://github.com/Bl0ck154/flowkit/stargazers"><img src="https://img.shields.io/github/stars/Bl0ck154/flowkit?style=flat&logo=github" alt="GitHub stars"/></a>
  <a href="https://github.com/Bl0ck154/flowkit/commits/main"><img src="https://img.shields.io/github/last-commit/Bl0ck154/flowkit?logo=github" alt="Last commit"/></a>
  <a href="#license"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="MIT License"/></a>
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white" alt="Python 3.10+"/>
  <img src="https://img.shields.io/badge/FastAPI-self--hosted-009688?logo=fastapi&logoColor=white" alt="FastAPI"/>
  <img src="https://img.shields.io/badge/Google%20Flow-live%20tested-4285F4?logo=googlechrome&logoColor=white" alt="Google Flow live tested"/>
</p>

> [!IMPORTANT]
> This is an **actively maintained fork of [crisng95/flowkit](https://github.com/crisng95/flowkit)** focused on keeping the Google Flow integration working against live frontend/API changes and making Flow practical as a self-hosted server API. Upstream remains the original project and receives compatible fixes through pull requests when possible.

> [!NOTE]
> **FlowKit itself is free and open source. Google Flow generation is not necessarily free.** Your Google account, subscription tier and Flow credit limits still apply. FlowKit is unofficial and is not affiliated with or endorsed by Google.

## Why this fork exists

Google Flow does not expose a stable public automation API for these generation workflows. Its web app changes over time: RPC payloads, model identifiers, project lifecycle calls and browser-side state can move without notice.

This fork is maintained around that reality:

- **live compatibility fixes** based on current `flow.google.com` behavior;
- **self-hosted FastAPI endpoints** for server-side apps, bots and agents;
- automatic Flow project creation and short-lived session projects — no permanent `FLOW_PROJECT_ID` pin required;
- Nano Banana image generation, editing and export;
- Omni Flash text-to-video, first-frame, first+last-frame and reference-image video modes;
- Veo image-to-video support;
- real Flow credit balance / generation-cost metadata;
- persistent signed-in Chrome profile with automatic Flow tab recovery;
- generation throttling and circuit-breaker behavior for Google risk / unusual-activity responses;
- caller attribution and diagnostics for production integrations;
- regression tests built from live Flow request captures.

The goal is simple: **when Flow changes, fix the integration quickly and upstream the reusable parts.**

## AI Agent Support

| Agent | Skills | Video review provider |
|-------|--------|----------------------|
| **Muse** | Native — reads `skills/fk-*.md` directly, vision on files and contact sheets (`muse.read`) | `muse` (= the agent itself) — official opt-in self-review: no CLI, no model, no API key; score sheets by hand via `review-sheets` → `review-submit` |
| **Claude Code** | Auto-loaded via `CLAUDE.md`, native `/fk-*` slash commands | `claude` — default `video_review` role |
| **Codex CLI** | Reads `skills/fk-<name>.md` via `AGENTS.md`; vision on contact sheets for self-review | `codex` — OpenAI Codex CLI, or `muse` (= the agent itself) for hand scoring |
| **agy** (Google Antigravity) | Reads skill files manually; vision on contact sheets for self-review | `agy` — Antigravity CLI, or `muse` (= the agent itself) for hand scoring |

## Current status

**Last live validation: 2026-09-23.**

| Capability | Status |
|---|---|
| Nano Banana Pro image generation | ✅ Live tested |
| Nano Banana 2 image generation | ✅ Live tested |
| Nano Banana 2 Lite wire support | ✅ Supported |
| Image references / base-image editing | ✅ Supported |
| Image export | ✅ 2K; 4K where the account/plan allows it |
| Omni Flash text-to-video | ✅ 4 / 6 / 8 / 10 s, 360p / 720p |
| Omni Flash first-frame video | ✅ Live tested |
| Omni Flash first + last frame | ✅ Supported |
| Omni Flash reference / ingredients video | ✅ Supported |
| Veo image-to-video | ✅ Supported |
| Video export / upscale | ✅ 1080p; higher modes depend on Flow/account support |
| Flow project creation | ✅ Automatic |
| Session-scoped projects | ✅ Automatic rotation after idle period |
| Account / credit inspection | ✅ Supported |
| Server REST API | ✅ FastAPI |

Flow changes frequently. If a mode breaks, check the latest commits/issues before assuming your account is blocked.

## What FlowKit actually does

FlowKit runs a local/server FastAPI service next to a persistent Chrome session signed into Google Flow.

```text
┌───────────────────────┐       REST        ┌────────────────────────┐
│ Your app / bot / agent│ ────────────────► │ FlowKit FastAPI        │
└───────────────────────┘                   │ 127.0.0.1:8100         │
                                            └───────────┬────────────┘
                                                        │ WebSocket / CDP
                                                        ▼
                                            ┌────────────────────────┐
                                            │ Chrome + Flow bridge   │
                                            │ signed-in Flow session │
                                            └───────────┬────────────┘
                                                        │
                                                        ▼
                                            ┌────────────────────────┐
                                            │ flow.google.com        │
                                            │ batchexecute / media   │
                                            └────────────────────────┘
```

The browser keeps the authenticated Flow session and page state. FlowKit turns that into a usable local/server API for other software.

## Quick start

### 1. Clone this maintained fork

```bash
git clone https://github.com/Bl0ck154/flowkit.git
cd flowkit
```

### 2. Install

```bash
./setup.sh
```

Or install the Python dependencies manually:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

You also need Chrome/Chromium and `ffmpeg` for the full media pipeline.

### 3. Load the Chrome extension

Open:

```text
chrome://extensions
```

Enable **Developer mode** → **Load unpacked** → select `extension/`.

Then open `https://flow.google.com/` and sign in to the Google account you want FlowKit to use.

### 4. Start FlowKit

| Env var | Default | What it does |
|---------|---------|--------------|
| `FLOW_PROJECT_ID` | — | The Flow project every RPC is scoped to. Required. |
| `FLOW_ALLOW_DEGRADED` | `0` | `1` lets scene chaining and r2v fall back to plain i2v instead of failing. |
| `DEFAULT_PAYGATE_TIER` | `PAYGATE_TIER_TWO` | Carried for the DB and dashboard; no longer selects a model. |
| `MEDIA_PROVIDER` | `flow` | `flow` (Google Flow via extension) or `assistant` (route all generation to the AI assistant). |
| `ASSISTANT_PROVIDER_TIMEOUT_S` | `1800` | How long a request waits for a worker to complete its provider job before failing. |
| `ASSISTANT_PROVIDER_POLL_S` | `15` | How often to poll the provider-job row while waiting for completion. |

### Assistant Media Provider (`MEDIA_PROVIDER=assistant`)

Routes every generation call — reference images, scene images, edits, i2v, r2v —
to the AI assistant instead of Google Flow. No Chrome extension or Flow sign-in
needed; the worker, skills, dashboard, and concat pipeline work unchanged.

Protocol (persistent provider-job queue + HTTP API):

1. Each request is inserted as a `QUEUED` row in the `provider_job` table with the
   full prompt, orientation, input URLs (`source_url` for edits, `start_url`/`end_url`
   for video, `extra.reference_urls` for reference images).
2. An external worker (the assistant) long-polls
   `GET /api/provider-jobs/wait-next?provider=assistant&worker_id=<id>`, claims the
   job (lease granted), heartbeats while working, and finishes with
   `POST /api/provider-jobs/{id}/complete` carrying
   `{"worker_id": "<id>", "result": {"output_url": "<file:// or https:// URL>", "media_id": "<uuid>"}}`
   (or `/fail` with `{"worker_id": "<id>", "error": "..."}`).
   Only the lease holder may heartbeat/progress/complete/fail (409 otherwise);
   complete/fail are idempotent for the holder.
3. The provider mints a UUID `media_id` when the worker omits one and records the
   output URL — the DB rows look identical to Flow-generated ones, so concat and
   upload keep working. Expired leases are reclaimable, so crashed workers never
   strand jobs.

See `agent/worker/assistant_worker.py` for a reference worker implementing the
worker side of the protocol. Video upscale is not supported on this provider and
fails loudly.

### Post-production (provider-neutral)

Concat, finalize, and review work on clips from any provider — each clip is
normalized to H.264/yuv420p + AAC before merging, so Flow and assistant clips
can be mixed in one timeline.

```bash
# Merge scene clips in display_order; trim to the video's target_duration_s
curl -X POST http://127.0.0.1:8100/api/videos/<VID>/concat \
  -H "Content-Type: application/json" \
  -d '{"orientation": "VERTICAL", "target_duration_s": 60}'

# Concat + optional music/narration mix, marks the video COMPLETED
curl -X POST http://127.0.0.1:8100/api/videos/<VID>/finalize \
  -H "Content-Type: application/json" \
  -d '{"orientation": "VERTICAL", "target_duration_s": 60,
       "music_path": "/path/to/music.mp3", "narrate": true}'

# Review scene videos, then auto-enqueue REGENERATE_VIDEO for bad scenes
# (bounded: at most max_regenerations regen requests per scene, ever)
curl -X POST http://127.0.0.1:8100/api/videos/<VID>/review-regenerate \
  -H "Content-Type: application/json" \
  -d '{"project_id": "<PID>", "mode": "light", "max_regenerations": 1}'
```

`target_duration_s` can also be stored on the video itself (`POST /api/videos`
/ `PATCH /api/videos/<VID>`) — concat/finalize fall back to it when the
request omits a target. Review accepts `file://` clip URLs directly, so
assistant-provider output needs no download step; the Flow `get_media`
fallback only runs when the extension is connected.


```bash
source venv/bin/activate
python -m agent.main
```

Default API:

```text
http://127.0.0.1:8100
```

Check it:

```bash
curl http://127.0.0.1:8100/health
curl http://127.0.0.1:8100/api/flow/status
```

You **do not need to manually pin one permanent Flow project** for normal direct API usage. FlowKit can create and rotate session projects itself, while callers can still explicitly reuse an existing `project_id` when they need long-lived project context.

## API examples

### Generate an image — Nano Banana

```bash
curl -X POST http://127.0.0.1:8100/api/flow/generate-image \
  -H 'Content-Type: application/json' \
  -d '{
    "prompt": "cinematic product photo of a red ceramic mug on a white table",
    "image_model": "NANO_BANANA_2",
    "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
    "count": 1
  }'
```

Available configured image families include:

```text
NANO_BANANA_PRO
NANO_BANANA_2
NANO_BANANA_2_LITE
```

See [`docs/IMAGE_API.md`](docs/IMAGE_API.md) for image references, editing, aspect ratios and export.

### Omni Flash text-to-video

```bash
curl -X POST http://127.0.0.1:8100/api/flow/generate-video-omni-text \
  -H 'Content-Type: application/json' \
  -d '{
    "prompt": "slow cinematic camera push toward a coffee cup by a rainy window",
    "duration_s": 4,
    "resolution": "360p",
    "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE"
  }'
```

### Upload an image

For **external/API callers**, send the image bytes directly. Do not pass a path from the caller's filesystem: `flowkit-agent.service` runs as its own `flowkit` user and uses systemd isolation such as `PrivateTmp=yes`, so caller-local `/tmp/...`, `/root/...` and other protected paths may not exist or be readable inside the service.

#### Recommended: multipart file upload

```bash
curl -X POST http://127.0.0.1:8100/api/flow/upload-image-file \
  -F 'file=@./source.jpg;type=image/jpeg'
```

You can optionally pass an existing Flow project:

```bash
curl -X POST http://127.0.0.1:8100/api/flow/upload-image-file \
  -F 'file=@./source.jpg;type=image/jpeg' \
  -F 'project_id=YOUR_FLOW_PROJECT_ID'
```

If `project_id` is omitted, FlowKit automatically uses/creates the current session project.

#### JSON/base64 upload

Useful when your client already transports JSON:

```bash
IMAGE_B64="$(base64 -w0 ./source.jpg)"
curl -X POST http://127.0.0.1:8100/api/flow/upload-image \
  -H 'Content-Type: application/json' \
  -d "{\"image_base64\":\"$IMAGE_B64\",\"mime_type\":\"image/jpeg\",\"file_name\":\"source.jpg\"}"
```

Base64 is supported for compatibility and JSON-only clients, but expands the request by roughly one third compared with multipart.

#### Server-local `file_path` mode

`file_path` remains available as a convenience **only when the image already exists on the FlowKit server** and is readable by the `flowkit` service user:

```bash
curl -X POST http://127.0.0.1:8100/api/flow/upload-image \
  -H 'Content-Type: application/json' \
  -d '{
    "file_path": "/var/lib/flowkit/imports/source.jpg",
    "file_name": "source.jpg"
  }'
```

Do not use caller-local `/tmp/...` paths for this mode. With systemd `PrivateTmp=yes`, the FlowKit service sees a different `/tmp` namespace. Unreadable paths return a clear 403, and paths not visible in the service namespace return a descriptive 404 instead of an internal traceback.

The response contains a Flow `media_id` and the resolved `project_id`, both of which can be reused for later generation.

### Image-to-video with Omni Flash

```bash
curl -X POST http://127.0.0.1:8100/api/flow/generate-video \
  -H 'Content-Type: application/json' \
  -d '{
    "start_image_media_id": "YOUR_MEDIA_ID",
    "scene_id": "demo-scene",
    "prompt": "slow camera pan, natural subtle motion",
    "model_family": "omni_flash",
    "duration_s": 4,
    "resolution": "360p",
    "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE"
  }'
```

For first+last frame and multi-reference / Ingredients modes, see [`docs/OMNI_FLASH.md`](docs/OMNI_FLASH.md).

---

## Core Concepts

### Reference Image System

Every visual element that should stay consistent gets a **reference image** — characters, locations, props. Each reference has a UUID `media_id` used in all scene generations via `imageInputs`.

| Entity Type | Aspect Ratio | Composition |
|-------------|-------------|-------------|
| `character` | Portrait | Full body head-to-toe, front-facing, centered |
| `location` | Landscape | Establishing shot, level horizon, atmospheric |
| `creature` | Portrait | Full body, natural stance, distinctive features |
| `visual_asset` | Portrait | Detailed view, textures, scale reference |

### Scene Prompts = Action Only

Scene prompts describe **what happens**, not character appearance. The reference images maintain visual consistency.

```
DO:   "Pippip juggling fish at Fish Stall, crowd watching in Open Market"
DON'T: "Pippip the chubby orange tabby cat wearing a blue apron juggling..."
```

### Media ID = UUID

All `media_id` values are UUID format (`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`). Never the base64 `CAMS...` mediaGenerationId.

### Two Prompts per Scene

Each scene has **two separate prompts**:
- `prompt` — describes the **still image** (frame 0): `"Luna steps out of rocket onto candy planet. Wide shot, sunrise."`
- `video_prompt` — describes the **8s video motion** with sub-clip timing and camera directions:

```
0-3s: Wide crane down, Luna steps out of rocket onto Candy Planet Surface. Luna gasps "It's beautiful!"
3-6s: Low angle tracking shot, Luna walks across candy ground, shallow DOF. Luna says "Everything is made of candy."
6-8s: Close-up Luna's face, eyes wide with wonder, golden hour backlight. Silence, ambient wind.
```

### Character Voice

Characters can have a `voice_description` (max ~30 words) for voice consistency:
```json
{"name": "Luna", "entity_type": "character", "description": "Small white cat...", "voice_description": "Soft curious childlike voice with wonder and slight purring"}
```

Voice descriptions are auto-appended to video prompts before generation.

### No Background Music

The worker auto-appends `"No background music. Keep only natural sound effects and ambient sounds."` to all video prompts. Sound effects from the scene (footsteps, splashing, wind) are preserved.

## Pipeline Overview

```
1. Create project      POST /api/projects (with entities + story)
2. Create video        POST /api/videos
3. Create scenes       POST /api/scenes (chain_type: ROOT → CONTINUATION)
4. Gen ref images      POST /api/requests {type: GENERATE_CHARACTER_IMAGE} per entity
   → Wait ALL complete, verify all have UUID media_id
5. Gen scene images    POST /api/requests {type: GENERATE_IMAGE} per scene
   → Wait ALL complete
6. Gen videos          POST /api/requests {type: GENERATE_VIDEO} per scene
   → Wait ALL complete (2-5 min each)
7. (Optional) Upscale  POST /api/requests {type: UPSCALE_VIDEO} (TIER_TWO only)
8. Download + concat   ffmpeg normalize + concat
```

## Skills (AI Agent Workflows)

Ready-to-use workflow recipes in `skills/` (also available as `/slash-commands` in Claude Code):

### Basic Pipeline

| Skill | Description |
|-------|-------------|
| `/fk-create-project` | Create project + entities + video + scenes interactively |
| `/fk-research` | Fact-check story details before scripting |
| `/fk-gen-refs` | Generate reference images for all entities |
| `/fk-gen-images` | Generate scene images with character refs |
| `/fk-gen-videos` | Generate videos from scene images (4K upscale via `UPSCALE_VIDEO` request, `PAYGATE_TIER_TWO`) |
| `/fk-concat` | Download + merge all scene videos |
| `/fk-pipeline` | Smart full-pipeline orchestrator — runs the whole chain end to end |
| `/fk-monitor` | Live monitor for a running pipeline |

### Advanced Video

| Skill | Description |
|-------|-------------|
| `/fk-gen-chain-videos` | Auto start+end frame chaining for smooth transitions (i2v_fl) |
| `/fk-insert-scene` | Multi-angle shots, cutaways, close-ups within a chain |
| `/fk-creative-mix` | Analyze story + suggest all techniques (chain, insert, r2v, parallel) |

### Review & Quality

| Skill | Description |
|-------|-------------|
| `/fk-review-video` | AI vision scoring of generated scene videos (quality, consistency, usability) — see [AI Vision Providers](#ai-vision-providers-video-review) below |
| `/fk-review-board` | Visual scene-by-scene review board for feedback before locking a cut |
| `/fk-change-provider` | View/switch the reviewer (`muse` = the assistant itself, or an AI CLI), model and effort behind `/fk-review-video` |

### Reference

| Skill | Description |
|-------|-------------|
| `/fk-camera-guide` | Camera angles, movements, lighting, DOF for cinematic video prompts |
| `/fk-thumbnail-guide` | Hook-worthy thumbnail design rules |
| `/fk-provider` | Media providers: choose the generation backend (`flow`/`assistant`), run the assistant worker, troubleshoot provider jobs |

### TTS & Narration

| Skill | Description |
|-------|-------------|
| `/fk-gen-tts-template` | Create a voice template for consistent narration |
| `/fk-import-voice` | Import an existing voice recording as a template |
| `/fk-gen-narrator` | Generate narrator text + TTS for all scenes |
| `/fk-gen-text-overlays` | Generate text overlays from narrator text (dates, locations, stats) |
| `/fk-concat-fit-narrator` | Trim scene videos to fit narrator duration, then concat |
| `/fk-gen-music` | Generate background music via Suno |

### YouTube

| Skill | Description |
|-------|-------------|
| `/fk-youtube-seo` | Generate SEO-optimized title, description, tags |
| `/fk-brand-logo` | Apply channel icon watermark to video/thumbnails |
| `/fk-youtube-upload` | Upload to YouTube with rule validation + scheduling |
| `/fk-thumbnail` | Generate YouTube-optimized thumbnails |

### Utilities

| Skill | Description |
|-------|-------------|
| `/fk-status` | Full project dashboard + recommended next action |
| `/fk-switch-project` | Switch the active project |
| `/fk-fix-uuids` | Repair any CAMS... media_ids to UUID format |
| `/fk-refresh-urls` | Refresh expired GCS signed URLs for images/videos |
| `/fk-upload-image` | Upload a local image to get a `media_id` |
| `/fk-add-material` | Image material system |
| `/fk-change-model` | View/switch video, image, and upscale model keys |
| `/fk-dashboard` | Live status in the Claude Code statusline |
| `/fk-doctor` | Diagnose any error (Flow API, extension, worker, YouTube) and prescribe a fix |

### Muse Support

Muse is a first-class FlowKit agent, not just another skill reader:

- **Skills run natively** — no shelling out, no slash-command wiring.
  Muse reads `skills/fk-*.md` and calls its own tools directly, including
  vision on files (`muse.read` for contact sheets and images).
- **`muse` is an official video-review provider** — no CLI, no model, no API
  key. The key means "the agent itself": Muse, Codex, or agy scores review
  contact sheets by hand with its own vision through
  `POST .../review-sheets` → vision → `POST .../review-submit`
  (see `/fk-review-video --by muse`). Opt in per role with
  `/fk-change-provider set muse`; the default role stays on `claude`.

### AI CLI Compatibility (Skill Consumption)

Skills are `.md` recipes any AI coding-assistant CLI can read and follow — this is about **which agent reads the skill files**, not which model does the work:

| CLI | Instructions | How skills work |
|-----|-------------|-----------------|
| Muse | Skills auto-loaded | Native tool calls (`muse.read` for files/images) |
| Claude Code | `CLAUDE.md` (auto-loaded) | Native `/fk-*` slash commands |
| Codex CLI | `AGENTS.md` → reads `CLAUDE.md` | User says `/fk-<name>`, agent reads `skills/fk-<name>.md` |

The Gemini CLI target was dropped in v1.3.1 — the CLI is retired, and its
replacement `agy` reads none of what that target generated (see the changelog).
`agy` is still supported, as one of the three CLIs that can run video review —
that is configured in `agent/providers.json`, not by `setup.py`.

### AI Vision Providers (Video Review)

Separate from the table above — this is about **which backend does the vision analysis** for `/fk-review-video`. Four reviewers are supported and swappable at runtime, no restart required:

| Provider | Binary | Reasoning efforts | Model catalog | Setup |
|----------|--------|-------------------|---------------|-------|
| `muse` | — (the assistant itself) | — | — | Official — Muse reads the contact sheets with its own vision via `POST .../review-sheets` → `POST .../review-submit`; no CLI, no API key. Opt in per role (`/fk-change-provider set muse`) |
| `claude` | Claude Code CLI | `low` `medium` `high` `xhigh` `max` | aliases (`sonnet`, `opus`, `haiku`, `fable`) or any full model name | Install the CLI, sign in once |
| `agy` | Google Antigravity CLI | `low` `medium` `high` | closed — `agy models` is the whole list and agy rejects anything else | Install separately, sign in once |
| `codex` | OpenAI Codex CLI | `low` `medium` `high` `xhigh` `max` (varies per model) | codex's own on-disk cache, plus slugs newer than it | `npm install -g @openai/codex`, then `codex login` once |

Provider, model and effort are set **per role** — a role being a job an AI
does for Flow Kit. There is one today, `video_review`; the config is a map so
the next one is an entry rather than a schema change. Model and effort may both
be `null`, meaning "whatever that backend defaults to". The `muse` reviewer
takes no model or effort — it is Muse scoring contact sheets by hand, so
pointing `video_review` at `muse` disables the CLI review endpoint (it
answers 500 with directions) and the review flow becomes
`review-sheets` → hand scoring → `review-submit`.

**For `agy`, model and effort are mutually exclusive.** Its slugs name their own
effort — `gemini-3.8-flash-low`, `gemini-3.1-pro-high` — so setting both is
rejected (`--model gpt-oss-120b-medium conflicts with --effort=low`), and a slug
with no effort in its name refuses `--effort` outright
(`--effort is not supported for model "claude-sonnet-4-6"`). Pick a model, or
pick an effort and let agy choose the model. The API answers 400 for the pair.

### Read the active Google account

```bash
curl http://127.0.0.1:8100/api/flow/account
```

### Read Flow credits

```bash
curl http://127.0.0.1:8100/api/flow/credits
```

Force a visible Flow balance refresh:

```bash
curl 'http://127.0.0.1:8100/api/flow/credits?refresh=true'
```

Generation responses can also include the current balance, estimated generation cost and estimated remaining balance.

## Project lifecycle

Older FlowKit versions commonly relied on one permanently pinned `FLOW_PROJECT_ID`. That becomes increasingly awkward for server workloads because unrelated jobs accumulate in the same Flow project.

This fork supports both patterns:

- **no `project_id` supplied** → FlowKit uses a session project and rotates it after idle time;
- **explicit `project_id` supplied** → FlowKit reuses that exact Flow project;
- higher-level applications can persist an order/job → Flow project mapping and reopen it days later for revisions.

This makes the API suitable for queues, ecommerce workflows, bots and other multi-job systems without losing the ability to return to old Flow projects.

## Production behavior

FlowKit includes several safeguards for unattended server use:

- generation submissions are serialized by default;
- minimum spacing between generation launches;
- `PUBLIC_ERROR_UNUSUAL_ACTIVITY` is treated as a terminal/risk response instead of being blindly retried;
- a short local circuit breaker prevents a failing caller from hammering Flow;
- optional `X-FlowKit-Caller` identifies the integration that triggered a request without logging prompts or media contents;
- `/api/flow/status` exposes generation-guard and session state;
- the Flow tab can close after idle time and be reopened using the persistent browser profile when needed.

## Live API compatibility

Flow's internal request shapes are positional and can change. This fork keeps captured request builders and regression tests for the modes it automates.

Recent examples of live fixes include:

- current Flow project creation RPC;
- Omni Flash 360p model / option slots;
- updated first-frame full-frame crop structure;
- trusted browser interaction for media selection and generation recovery;
- image upload by direct bytes for external callers;
- credit/account visibility and generation-cost estimates.

The capture methodology is documented in [`docs/CAPTURE.md`](docs/CAPTURE.md).

## Dashboard and full video pipeline

FlowKit is more than the low-level API. The repository also contains the original project/dashboard pipeline for multi-scene generation, references, review and post-processing.

<p align="center">
  <img src="docs/images/dashboard_overview.png" width="760" alt="FlowKit dashboard" />
</p>

The low-level REST API can be used independently by your own application, while the higher-level pipeline remains available for larger video workflows.

## Maintained fork vs upstream

This repository started as a fork of **[crisng95/flowkit](https://github.com/crisng95/flowkit)** and keeps the MIT license and upstream attribution.

The projects currently have different maintenance focuses:

| This fork | Upstream |
|---|---|
| Fast live fixes for `flow.google.com` request changes | Original project and broader pipeline direction |
| Server/API integrations and production diagnostics | Video-review/provider tooling and general project features |
| Project/session lifecycle automation | Upstream baseline architecture |
| Credit/account inspection | Original documentation and ecosystem |
| Compatibility patches are submitted upstream when portable | Reviews/merges compatible contributions |

This is **not** intended to erase or replace upstream credit. If you need the original project, use upstream. If you want the compatibility-focused version actively run and updated against live Flow, use this fork.

Current upstream compatibility work is also submitted back through pull requests where practical.

## Automatic compatibility monitoring

Every normal trusted-UI generation now doubles as a **zero-extra-credit compatibility check**. FlowKit captures the actual `f.req` sent by the current Flow UI, strips prompts, media/project IDs, UUIDs, reCAPTCHA/opaque values and random seeds, then compares the remaining structure with the request builder that initiated the job.

`GET /api/flow/status` exposes the latest sanitized `payload_drift` state. Structural changes such as a moved crop slot, changed client descriptor or new resolution flag are logged as `PAYLOAD_DRIFT` without storing the sensitive request payload.

Near-term roadmap:

- automated regression-test / patch branches for simple, whitelisted wire-format changes;
- optional GitHub PR creation when a safe structural patch is inferred and the full suite passes;
- safer periodic upstream syncs without overwriting fork-specific server features.

The goal is to turn a future Flow frontend change from “the API is broken” into “FlowKit detected payload drift and has a tested compatibility patch ready.”

## Search keywords / use cases

FlowKit is relevant if you are looking for a:

- Google Flow API / unofficial Google Flow API;
- self-hosted Google Flow server;
- Nano Banana API or Nano Banana self-hosted automation layer;
- Omni Flash API / Omni image-to-video API;
- Veo image-to-video automation;
- AI image generation REST API;
- AI video generation REST API;
- local FastAPI bridge for Google Flow;
- server-side Flow automation for bots, agents or ecommerce pipelines.

## Important limitations

- This uses **unofficial web interfaces**, not a stable public Google API.
- Google may change Flow without notice.
- A signed-in Google Flow browser session is required.
- Your Google subscription, credit balance, model availability and regional limits still apply.
- Do not treat local cost estimates as billing guarantees; Flow remains authoritative.
- Respect Google's terms and any applicable content/usage policies.

## Contributing

Compatibility reports are most useful when they include:

- FlowKit commit SHA;
- endpoint/mode that failed;
- HTTP/error code;
- whether the same action works manually in the Flow UI;
- sanitized structural differences only — **never post cookies, auth tokens or reCAPTCHA tokens**.

If you capture a changed Flow request shape, add a regression test with the fix so the same drift does not return silently.

## Upstream

Original project: **[crisng95/flowkit](https://github.com/crisng95/flowkit)**

Thanks to the original author and contributors for the architecture this fork builds on.

## License

MIT — see [`LICENSE`](LICENSE).
