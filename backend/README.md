# Backend

Django REST Framework backend for the media-asset production platform.

## Authoritative contracts

- \`../openapi.yaml\` is the only checked-in machine-readable API contract.
- \`GET /api/schema/\` renders the same contract dynamically.
- \`studio/urls.py\` is the runtime route registry.
- Django migrations are the only schema history.

Do not create or maintain an OpenAPI copy under \`backend/\`.

## Setup

From the repository root:

\`\`\`powershell
.\scripts\setup.ps1
.\scripts\restart.ps1
\`\`\`

For an explicit demo project only:

\`\`\`powershell
Push-Location backend
.venv\Scripts\python.exe manage.py seed_data --with-demo-project
Pop-Location
\`\`\`

The explicit demo command does not own workflow versions, and startup never overwrites the workflow catalog. System workflow versions are initialized exclusively by versioned data migrations.

Before starting a changed checkout:

\`\`\`powershell
Push-Location backend
.venv\Scripts\python.exe manage.py migrate --plan
.venv\Scripts\python.exe manage.py migrate --check
.venv\Scripts\python.exe manage.py migrate
Pop-Location
\`\`\`

## Production contract

\`POST /api/v1/production-runs/\` is the only entry for new production. A run uses either a confirmed script revision or the AI-script path:

\`\`\`text
AI script generation
  -> script review
  -> deterministic Bible materialization
  -> Bible review
  -> storyboard / route / cost gates
  -> production submission
  -> managed artifacts
  -> episode masters
  -> ReleaseBundle
\`\`\`

ProductionRun actions in the root OpenAPI are the supported state transitions. Task, deferred-request, assembly, and release endpoints are fully read-only diagnostic projections. Creation, retry, cancellation, and ordered fallback are available only through the owning ProductionRun.

A recipe can be published only when it contains one valid content entry and the complete ordered \`content -> storyboard -> prompt -> media -> selection -> assembly -> delivery\` closure.

## Workflow execution

A workflow version declares semantic inputs and outputs. The compiler resolves those semantics against the stored graph; production code does not rely on fixed Comfy node IDs. A version is publishable only after validation and a real smoke result.

Bootstrap catalog entries are intentionally inactive drafts with \`needs_review\`. Operators must bind nodes, validate the graph, run a real smoke, and publish explicitly.

Executors currently implemented:

- semantic Comfy workflow execution;
- async REST T2I;
- async REST T2V.

The standard async REST contract is closed and text-only. I2V/reference-image requests are blocked until a provider-specific upload contract exists. There is no cross-node or multi-GPU scheduler.

## Configuration and hosts

Effective configuration is resolved in this order:

1. model-version defaults;
2. workflow-version defaults;
3. active system revision;
4. active project revision;
5. active run override, frozen into the immutable ProductionRun snapshot.

Restart-required changes become pending and are activated explicitly for a new boot. Runtime host probes record observed capability. Recommendations create reviewable draft recipes/configuration. The server revalidates the host snapshot, catalog, configuration, and current candidate compatibility on every decision; stale recommendations are rejected. Accepting one does not download a model, activate configuration, or publish a workflow.

Secret values are stored as immutable encrypted versions and linked through managed bindings. Encryption uses AES-256-GCM with an independent deployment keyring; that keyring must never be stored in the database or bundled with a database backup. APIs accept secret material only through write-only superuser operations and return configured/usable state, never plaintext, ciphertext, nonce, or key material.

## Access control

\`scripts/setup.ps1\` and the container bootstrap create exactly one initial superuser when none exists. The random temporary password is printed once and is never persisted in audit data. Until it is changed, JWT authentication permits only \`GET /api/v1/auth/me/\` and \`POST /api/v1/auth/password/change/\`; changing a password, role, or active state invalidates existing tokens. Only superusers can manage users, roles, temporary passwords, access audit events, and secret bindings. Staff users can use non-secret platform administration but cannot see the permissions menu or bind secrets.

## Artifacts and delivery

Remote executor outputs must be archived into managed storage, validated, and associated with the attempt/provenance record. At archive time, \`comparison_parameters\` is generated from a public whitelist of frozen task/attempt/stage facts. Artifact comparison uses this field and never treats manifest identity, object keys, IDs, or hashes as generation parameters. Assemblies consume managed source items. Delivery produces one master per episode; a \`ReleaseBundle\` groups those masters. The backend does not concatenate a whole season into one long video.

## Workers

\`scripts/restart.ps1\` starts the API plus these worker roles:

- generation worker;
- text-generation worker;
- async-media worker;
- assembly worker;
- platform-maintenance worker.

Worker concurrency is process-local. Do not interpret it as global GPU scheduling. Runtime-host probes are durable platform-maintenance attempts: the HTTP endpoint enqueues and returns 202, while the maintenance worker owns hashing, heartbeats, stale-claim recovery, safe cancellation checkpoints, and terminal snapshot linkage. The synchronous \`probe_runtime_hosts\` command is operations-only.

## Verification

\`\`\`powershell
.\scripts\check.ps1

Push-Location backend
.venv\Scripts\python.exe manage.py test
.venv\Scripts\python.exe manage.py spectacular --file ..\openapi.generated.yaml --validate
Pop-Location
\`\`\`

A passing mocked suite does not prove a real external provider/GPU E2E. Production readiness additionally requires actual provider authorization, workflow smoke evidence, artifact archival, episode-master assembly, and restore testing.

See \`../docs/api.md\`, \`../docs/architecture.md\`, and \`../docs/operations.md\`.
