# AutoPrompt Studio Backend

Django REST Framework backend for a locally runnable and deployable episodic short-video production system. It manages eleven style families, local material metadata, character/scene/prop bibles, versioned continuity locks, unlimited episodes and shots, native MiniMax H3 prompts, dynamically injected ComfyUI workflows, and a durable generation queue. The project source license is not yet determined; third-party licenses do not grant rights to the project source code.

## Start locally (PowerShell)

```powershell
# Run from the repository root.
Set-Location .\backend
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py seed_data
.\.venv\Scripts\python manage.py import_asset_library --manifest ..\assets\library\manifest.json
.\.venv\Scripts\python manage.py createsuperuser
.\.venv\Scripts\python manage.py runserver 127.0.0.1:8000
```

The default seed creates no account. Only for an isolated local demonstration, run `manage.py seed_data --with-demo-project`. A newly created `demo` receives a high-entropy password from Python's system-backed `secrets` generator; it is displayed once on that command's standard output and is not written to a file by the application. Do not capture it in shared logs or tickets. Re-running the seed never resets or redisplays an existing demo password. Production administrators must be created interactively with `manage.py createsuperuser` or through the operator's approved identity-management process.

Run the ComfyUI worker in a second terminal:

```powershell
Set-Location .\backend
.\.venv\Scripts\python manage.py run_generation_worker
```

Run the FFmpeg assembly worker in a third terminal. Assembly API requests only
persist a source snapshot and return a `queued` job; this process claims and
executes those jobs independently of the HTTP request lifecycle:

```powershell
Set-Location .\backend
.\.venv\Scripts\python manage.py run_assembly_worker
```

Swagger is at `http://127.0.0.1:8000/api/docs/`. Obtain JWT tokens from `POST /api/v1/auth/token/`.

## Main flow

1. Create a project, its character/scene/prop bibles, episodes, and shots. Editors should save authoritative snapshots through `POST /api/v1/projects/{id}/sync-bibles/` and `POST /api/v1/episodes/{id}/sync-shots/`; both are transactional, delete omitted records, and safely handle unique-key/order swaps. Shot snapshots retain structured dialogue, references, and `metadata.framing`; the first Bible reference becomes `anchor_asset`.
2. `POST /api/v1/projects/{id}/sync-locks/` freezes current identities as versioned locks.
3. `POST /api/v1/prompts/generate/` creates one prompt per shot or one H3 multi-shot prompt.
4. `POST /api/v1/workflows/generate/` injects the prompt and production settings into a ComfyUI API workflow.
5. `POST /api/v1/tasks/` enqueues it; poll `GET /api/v1/tasks/{id}/`.
6. For a whole series or selected episodes, `POST /api/v1/projects/{id}/build-production/` accepts optional `episode_ids` (omitted/empty means all), per-mode `workflow_selections`, the complete official ResolutionSelector megapixel profiles (`project`, `0.1`–`0.9`, `0.98`, `1.0`, `1.2`, `1.5`, `1.8`, `2.0`), and `resolution_multiple` (default 32; 8–128 in steps of 4). Each selection binds `output_kind + h3_mode` to a compatible template and its declared typed parameters. Images have independent `image_resolution_profile` (default `0.98`) and `image_resolution_multiple` (defaults to `resolution_multiple`), so a mixed episode can keep video at a `0.2` smoke profile without degrading SDXL stills. Numeric profiles reproduce ComfyUI's official calculation using the project's `9:16`, `16:9`, or `1:1` aspect ratio; portrait image `0.98` is 768×1344 at multiple 32. The endpoint groups contiguous shots by their own `media_type + h3_mode + include_audio`, partitions video into safe 5–15 second jobs, auto-selects compatible active templates when no explicit selection is supplied, and performs prompt/workflow/queue creation atomically. An image shot without a configured active image template fails the whole request explicitly; it is never skipped or treated as video.
7. Dry-run or enqueue ordered FFmpeg assembly through `POST /api/v1/assemblies/episode/` or `/api/v1/assemblies/series/`. Non-dry-run requests return a durable `queued` job immediately; poll `GET /api/v1/assemblies/{id}/`. The independent assembly worker writes a claim-specific temporary MP4 and atomically publishes it only after FFmpeg succeeds, so stale claims can be safely recovered without exposing a partial output.

The seed provides three H3 input modes through five active video templates: official-default T2VA, optional first/last-frame video, and Ref2VA graphs, plus the opt-in experimental Ref2VA `video_minimax_h3_r2v_t8_dual_clock_4step` and `video_minimax_h3_r2v_turbo_lora` variants. The variants expose bounded `STEPS` and `LORA_STRENGTH` parameters and require their declared LoRA and custom-node capabilities. The backend accepts only 5–15 second H3 workflow requests, aligns them to `17k+5` (124–362 frames, approximately 5.167–15.083 seconds), and dynamically adds only the selected `LoadImage`, `LoadVideo -> GetVideoComponents`, and `LoadAudio` inputs. Authorized image/video/audio files are normalized and staged into a task-specific ComfyUI input folder; silent output removes only unneeded audio decode/mux nodes. Immediately before queueing, every executable graph is checked once against the connected server's live `/object_info`: node/input presence, required and autogrow sockets, output slots/types, numeric ranges, widget/model enums, and cycles must all match. The passed contract hash is persisted in `capability_snapshot.comfyui_contract_preflight`. `GET /api/v1/workflow-templates/production-options/` returns path-free selector metadata, while `GET /api/v1/workflow-templates/{id}/validate-comfyui/` is a template-level availability check; neither endpoint submits a generation.

Ref2VA video staging never edits the uploaded `Asset`. Every reference video is decoded into a task-local H.264 MP4 with an exact 24/1 constant frame rate and verified frame count before upload to ComfyUI. Selected embedded audio is padded/trimmed to that exact frame duration; an external video-paired track is staged as a duration-matched WAV. The task records source/effective FPS, source/effective duration, frame count, and synchronization proof in `staged_inputs`, and exposes the safe timing summary in task-detail `input_parameters.reference_assets`. Missing FPS, failed conversion, or failed post-conversion verification blocks the task before `/prompt` is called.

The seeded `minimax-reference-image` record is an active, fixed-model local SDXL Base 1.0 text-to-image graph. It uses only `CheckpointLoaderSimple`, two `CLIPTextEncode` nodes, `EmptyLatentImage`, `KSampler` (30 steps, CFG 6.5, `dpmpp_2m`, `karras`), `VAEDecode`, and `SaveImage`; the checkpoint is pinned to `sd_xl_base_1.0.safetensors` and checked against the live checkpoint enum before `/prompt`. Its source and exact file size/SHA256 are stored in `input_schema`. Selected character/scene/prop images serve only as continuity provenance: their Bible descriptions influence text, but v1 has no `LoadImage`, IP-Adapter, ControlNet, or other pixel-conditioning connection. The recommended production profile is `0.98` MP (minimum recommendation `0.8` MP); `0.1`–`0.3` are smoke-test profiles.

Workflow templates are executable configuration: only staff may create, edit or delete them, and ordinary users may execute only active global system templates. Every graph is checked against a small server-side node allowlist; Partner/API/network, downloader/uploader, checkpoint/model saving and text-saving nodes are always rejected. Operators may extend the benign local-node list with the server-only comma-separated `COMFYUI_EXTRA_ALLOWED_NODE_TYPES`, but dangerous markers cannot be overridden.

### H3 contract

- Base T2VA/I2VA/FL2VA/L2VA fields are ordered as `integrated_multimodal_description`, `overall_soundscape`, `non_diegetic_music`.
- Shot 1 has no timestamp; Shot 2+ uses `[Shot 2] At 00:03.500, ...` with strictly increasing times.
- Dialogue uses a character descriptor, stable `(S1)` IDs, and `(S1) says: <d>[Chinese]原文</d>`.
- I2VA, FL2VA and L2VA begin with their required picture-alignment line.
- Audio-disabled prompts use `N/A` for both sound fields.
- Ref2VA fields are ordered as `subject_definitions`, `summary`, `retention_analysis`, `detailed_description`, `overall_soundscape`, `non_diegetic_music`.
- Ref2VA supports 0–9 classified pictures, 0–3 videos, optional same-index embedded/external video soundtracks, and 0–3 standalone audios. `<Picture N>`, `<Video N>`, and `<Audio N>` labels come from the exact normalized graph-input manifest; unbound Bible entities are explicit text-only definitions.
- Ref2VA persists explicit entity-to-picture bindings. Stable project slots determine order only after a binding exists; loading, changing entities, and changing scenes never silently substitute an anchor or fallback image.
- A video soundtrack consumes the next `<Audio N>` before that video's `<Video N>` presentation; standalone audio labels follow all videos.
- Artifact negative prompts merge `production_settings.negative_prompt`, style/Bible exclusions, and each shot's `continuity_notes`.
- Director prose should be English; authored dialogue, lyrics and visible scene text may use their authored language. The optional `H3_PROMPT_OPTIMIZER_*` OpenAI-compatible rewrite stage is disabled by default and never sends a request while disabled. Non-loopback endpoints require HTTPS, an API key and explicit `H3_PROMPT_OPTIMIZER_ALLOW_NON_LOOPBACK=true`; rewritten output is rejected unless sections, lines, shots/timestamps, reference labels, numbers, protected authored blocks and the 7000-character limit are valid.

### Optional English director-prose optimizer

The review-gated offline Qwen field translator is documented separately in
[Prompt translation/expansion providers and human review](../docs/local-prompt-translation.md),
including its pinned official ModelScope artifact, one-process batch runtime,
installation command, fixed director terminology, and confirmation gate. That
workflow also supports an explicitly selected `deepseek_api` provider for
translation, full creative drafts, and single-shot detail expansion; local Qwen
is the default, and the guide documents remote data egress, backend-only
secret configuration, key rotation, and the no-silent-fallback rule.

`build_h3_video_prompt` can pass the locally compiled video prompt to an explicitly configured OpenAI-compatible chat-completions endpoint. It is disabled by default, performs no request while disabled, and does not rewrite image prompts. Image prompt quality reports therefore state `optimizer.status=not_configured` when director/Bible prose still contains CJK; with the default block gate those prompts remain previewable but cannot be enqueued until the listed fields are manually rewritten in English. Configure the optional video optimizer with `H3_PROMPT_OPTIMIZER_ENABLED`, `H3_PROMPT_OPTIMIZER_BASE_URL`, `H3_PROMPT_OPTIMIZER_API_KEY`, `H3_PROMPT_OPTIMIZER_MODEL`, `H3_PROMPT_OPTIMIZER_ALLOW_NON_LOOPBACK`, `H3_PROMPT_OPTIMIZER_TIMEOUT_SECONDS`, and `H3_PROMPT_OPTIMIZER_FAILURE_MODE`.

Loopback HTTP endpoints may omit an API key. Non-loopback endpoints require HTTPS, a key, and explicit non-loopback consent. Before transmission the service replaces `<d>`, `<lyrics>`, and `<screen_text>` blocks with opaque placeholders; director prose and continuity identities outside those blocks are visible to a remote provider. Responses must be a JSON object containing only the rewritten `prompt` value. The validator rejects changed section order, line/list/shot structure, timestamps, numeric tokens, reference-label order, protected placeholders, prompts over 7,000 characters, or CJK director prose. Keep optimizer failure mode at `warn` for inspectable preview fallback and use `H3_PROMPT_LANGUAGE_GATE=block` to prevent mixed-language video prompts from entering the production queue; `H3_PROMPT_LANGUAGE_GATE=warn` provides non-blocking diagnostics. Turning `H3_PROMPT_OPTIMIZER_ENABLED=false` and restarting is the immediate no-egress fallback. Prompt artifacts are immutable and must be regenerated deliberately.

## Production notes

- Local `.env` uses `AUTOPROMPT_DEPLOYMENT_PROFILE=development`. The Docker environment must explicitly use `production`; settings then fail before Django starts unless DEBUG is false, the Django key and PostgreSQL password are strong/non-placeholder values, PostgreSQL is selected, the real host and exact HTTPS CORS/CSRF origins are present, secure cookies/redirect are enabled, and HSTS is at least one hour.
- Public registration is off by default (`ALLOW_PUBLIC_REGISTRATION=false`). Anonymous/user API throttles, a stricter registration scope, and an upload scope are enabled; tune them only from trusted server environment variables.
- The production Compose profile is loopback-published behind a separate TLS terminator. That proxy must overwrite `Host`, `X-Forwarded-For` and `X-Forwarded-Proto=https`; never expose the loopback upstream directly. HSTS uses a one-year default, while include-subdomains/preload are deliberate opt-ins only after every subdomain is HTTPS-only.
- Docker/Linux installs `gunicorn`; Windows installs `waitress`. The API timeout is bounded at 120 seconds because requests enqueue durable GPU/FFmpeg work. Docker bootstrap runs the production settings gate and Django deployment checks before migration, then collects static files into the Nginx-mounted volume. Run exactly one `run_generation_worker` process per GPU and at least one `run_assembly_worker`; validate model, resolution, duration, memory and retry limits on the target GPU before production use.
- SQLite is for a single development worker. PostgreSQL is recommended for concurrent workers. A singleton `WorkerLease` row serializes count-and-claim across processes when `TASK_MAX_CONCURRENT=1`; global and per-user active queue limits are checked under database locks.
- Before creating a UUID submission token or calling `/prompt`, the worker must obtain live `/object_info` and pass the complete API-graph contract preflight. A proven mismatch fails the current attempt immediately instead of automatically consuming the remaining attempts; an unreachable contract safely requeues up to the task attempt limit. Both paths have no token and cannot duplicate GPU work. Only after a passed preflight does the worker persist the token/state, pass that exact `prompt_id` to ComfyUI, and recover by querying the same ID. An ambiguous prepared submission is failed for operator review instead of being posted a second time.
- `POST /api/v1/tasks/{id}/retry/` never resets a terminal source task. It creates one auditable queued clone referencing the same immutable generated workflow and records `capability_snapshot.retry_of`; a persisted submission may be retried only after ComfyUI has returned a definitive terminal failure. Successful outputs, active/uncertain submissions, and duplicate retry clicks are rejected.
- Ordinary users cannot declare `local_path`. Image uploads are decoded and dimension-limited; reference videos/audio are MIME/extension/size checked and ffprobe-verified for stream type and 2–15 second duration. All uploads are quota-limited and stored under `media/assets/{owner_id}/...`; task staging revalidates the live Asset owner and resolved tenant path. Global imported material is restricted to the read-only `ASSET_LIBRARY_ROOT`.
- Uploaded assets, global curated assets, generated outputs and completed assemblies use short-lived signed `/api/v1/media/{token}/` URLs so browser `<img>`/`<video>` elements work without a Bearer header. Do not expose or alias the raw `/media/` directory, and do not distribute repository `/assets/library/` paths as publication URLs.
- In development, Django can expose the repository's read-only `assets/` tree. Production Nginx keeps `/assets/library/` internal: browsers receive curated assets only through signed media URLs, so a known repository path cannot bypass access checks. Vite's hashed JavaScript/CSS uses the separate `/assets/` document-root location. Only set `SERVE_ASSETS_WITH_DJANGO=true` for a controlled development deployment.
- ComfyUI defaults to `http://127.0.0.1:8188` and is configurable in `.env`.
- Five seeded H3 video templates and the fixed local SDXL Base 1.0 text-to-image template are active. Treat checkpoint presence and `/object_info` validation as runtime readiness checks; a database `is_active` flag alone never proves the local model is installed. The two optional Ref2VA variants remain experimental until a target environment passes their GPU end-to-end smoke tests.
- FFmpeg assembly can add a visible `AI-generated · MiniMax H3` overlay and matching container metadata. Windows automatically selects an existing system font when `ASSEMBLY_AI_LABEL_FONTFILE` is empty; minimal Linux/custom images should set it to an existing font file. Operators may configure the label through `ASSEMBLY_AI_LABEL_*`.
- MiniMax H3, SDXL, ComfyUI, FFmpeg and other bundled or connected components use their own third-party licenses. Preserve the repository's `licenses/` and `NOTICE` files when distributing the corresponding materials.
- Run `python manage.py spectacular --file openapi.yaml --validate` to export and validate the OpenAPI contract.
- JWT access tokens last 30 minutes; refresh/verify endpoints are `/api/v1/auth/token/refresh/` and `/api/v1/auth/token/verify/`. Refresh tokens rotate and the prior token is blacklisted.

## Tests

```powershell
.\.venv\Scripts\python manage.py test
```
