import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const frontendRoot = fileURLToPath(new URL('..', import.meta.url))
const server = () => createServer({
  root: frontendRoot,
  logLevel: 'silent',
  appType: 'custom',
  server: { middlewareMode: true },
})

test('production and prompt editors do not mirror business maxima in the browser', async () => {
  const production = await readFile(new URL('../src/views/ProductionRunView.vue', import.meta.url), 'utf8')
  const profiles = await readFile(new URL('../src/components/PromptProfileSettings.vue', import.meta.url), 'utf8')
  for (const retired of [
    'maxlength="6000"', 'maxlength="2000"', 'maxlength="30000"',
    'max="100"', 'max="3600"', 'maxLength: 8000', 'maxLength: 2000', 'maxLength: 3000',
  ]) assert.equal(production.includes(retired), false, retired)
  for (const retired of ['max="30000"', '> 30000', '{0,11}', 'max="1000"']) {
    assert.equal(profiles.includes(retired), false, retired)
  }
  const review = await readFile(new URL('../src/components/ProductionTextCandidateReview.vue', import.meta.url), 'utf8')
  assert.match(production, /ProductionTextCandidateReview/)
  assert.match(review, /qualityAdvisories/)
  assert.match(review, /仅提醒，最终范围由服务端校验/)
})

test('recipe variants, uncertainty notes, and creation plans fail closed on server policy gaps', async () => {
  const [repository, schema, editor, settings, workflow, production, studio, storyboard, adapters] = await Promise.all([
    readFile(new URL('../src/services/repository.ts', import.meta.url), 'utf8'),
    readFile(new URL('../src/services/registryResourceSchema.ts', import.meta.url), 'utf8'),
    readFile(new URL('../src/components/ProductionRecipeEditor.vue', import.meta.url), 'utf8'),
    readFile(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8'),
    readFile(new URL('../src/components/TaskMediaInspector.vue', import.meta.url), 'utf8'),
    readFile(new URL('../src/views/ProductionRunView.vue', import.meta.url), 'utf8'),
    readFile(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8'),
    readFile(new URL('../src/views/StoryboardView.vue', import.meta.url), 'utf8'),
    readFile(new URL('../src/services/backendAdapters.ts', import.meta.url), 'utf8'),
  ])

  assert.match(repository, /maxVariants: effectiveLimit\('max_variants'\)/)
  assert.equal(schema.includes('variant_count:'), false)
  assert.equal(editor.includes('stageVariantMax'), false)
  assert.match(editor, /effective_limits\.max_variants/)
  assert.match(editor, /:max="variantLimit\|\|undefined"/)
  assert.match(editor, /props\.options\?\.effectiveLimits\.maxVariants/)
  assert.equal(settings.includes(':recipe-variant-limit='), false)
  for (const source of [workflow, production]) {
    assert.match(source, /auditNoteMinCharacters/)
    assert.match(source, /auditNoteMaxCharacters/)
    assert.equal(source.includes('maxlength="4000"'), false)
  }
  assert.match(studio, /const creationRunPlan = computed/)
  assert.match(repository, /default_language/)
  assert.match(repository, /aspect_ratio_options/)
  assert.match(repository, /default_shot_media_type/)
  assert.match(repository, /default_include_audio/)
  assert.match(studio, /newProjectAspectRatioOptions/)
  assert.equal(studio.includes("aspectRatio: '9:16'"), false)
  assert.match(storyboard, /defaults\.defaultShotMediaType/)
  assert.match(storyboard, /defaults\.defaultIncludeAudio/)
  assert.equal(storyboard.includes("mediaType:'video'"), false)
  assert.equal(storyboard.includes('?? true'), false)
  for (const retired of ["language: 'Chinese'", "aspect_ratio: project.aspectRatio || '9:16'", 'Number(raw.duration_ms || 3500)', 'raw.include_audio !== false']) {
    assert.equal(adapters.includes(retired), false, retired)
  }
  for (const retired of ['aiMaxGenerationTasks.value || 0', 'aiEpisodeCount.value || 0', 'aiShotsPerEpisode.value || 0', 'aiEpisodeDurationSeconds.value || 0']) {
    assert.equal(studio.includes(retired), false, retired)
  }
})

test('visual-rules candidate detail exposes only frozen advice without invented defaults', async () => {
  const vite = await server()
  try {
    const review = await vite.ssrLoadModule('/src/services/productionTextCandidates.ts')
    const { visualRuleReviewFixture } = await import('./visualRulesOutputFixture.ts')
    const artifact = (quality_advisories?: Record<string, unknown>) => ({
      productionRun: 'run', stageRun: 'stage', status: 'available', sha256: 'a'.repeat(64), manifestMetadataSha256: 'b'.repeat(64),
      textCandidate: { operation: 'visual_rules.generate',
        candidate: { negative_prompt: 'no blur', color_script: 'teal', continuity_summary: 'same coat' },
        warnings: [], text: null, quality_advisories, review_contract: visualRuleReviewFixture() },
    })
    assert.equal(review.readTextCandidateDetail(artifact(), 'run', 'stage').qualityAdvisories, null)
    const advice = { schema_version: 1, negative_prompt_max_chars: 91_000, color_script_max_chars: 92_000, continuity_summary_max_chars: 93_000 }
    assert.deepEqual(review.readTextCandidateDetail(artifact(advice), 'run', 'stage').qualityAdvisories, advice)
    assert.throws(() => review.readTextCandidateDetail(artifact({ schema_version: 1, color_script_max_chars: -1 }), 'run', 'stage'), /建议长度/)
  } finally { await vite.close() }
})

test('conditioning adapter accepts explicit versioned values above retired platform globals', async () => {
  const vite = await server()
  try {
    const capabilities = await vite.ssrLoadModule('/src/services/conditioningCapabilities.ts')
    const input = {
      key: 'dynamic', label: 'Dynamic', description: '', input_kind: 'classified_references',
      requires_entity_reference_bible: false, supports_native_audio: true,
      inputs: {
        total_images: { minimum_items: 0, maximum_items: 0 },
        first_frame: { minimum_items: 0, maximum_items: 0 },
        last_frame: { minimum_items: 0, maximum_items: 0 },
        entity_images: { minimum_items: 0, maximum_items: 0 },
        reference_videos: { minimum_items: 1, maximum_items: 2 },
        paired_video_audios: { minimum_items: 0, maximum_items: 2 },
        reference_audios: { minimum_items: 0, maximum_items: 2 },
      },
      dynamic_reference_topology: {
        schema_version: 1,
        videos: {
          minimum_items: 1, maximum_items: 2, default_purpose_key: 'motion',
          purposes: [{ key: 'motion', label: 'Motion', minimum_items: 0, maximum_items: 2 }],
        },
        audios: {
          minimum_items: 0, maximum_items: 2, default_purpose_key: 'sound',
          purposes: [{ key: 'sound', label: 'Sound', minimum_items: 0, maximum_items: 2 }],
        },
        preprocessing: {
          videos: { adapter_key: 'video', minimum_duration_ms: 1, maximum_duration_ms: 100_000_000, target_fps: 960, requires_source_fps: false },
          paired_video_audios: { adapter_key: 'paired', minimum_duration_ms: 1, maximum_duration_ms: 100_000_000 },
          reference_audios: { adapter_key: 'audio', minimum_duration_ms: 1, maximum_duration_ms: 100_000_000 },
        },
        pairing_rules: [{
          key: 'pair', label: 'Pair', source_media_kind: 'video', target_media_kind: 'audio',
          source_purpose_keys: ['motion'], target_purpose_keys: ['sound'], default_target_purpose_key: 'sound',
          minimum_targets_per_source: 0, maximum_targets_per_source: 2,
          allow_embedded_source: true, allow_external_target: true, source_and_external_mutually_exclusive: false,
        }],
        mutual_exclusion_rules: [],
      },
    }
    const adapted = capabilities.adaptConditioningModeCapability(input)
    assert.equal(adapted.dynamicReferenceTopology.preprocessing.videos.maximumDurationMs, 100_000_000)
    assert.equal(adapted.dynamicReferenceTopology.preprocessing.videos.targetFps, 960)
    assert.equal(adapted.dynamicReferenceTopology.pairingRules[0].maximumTargetsPerSource, 2)
  } finally { await vite.close() }
})

test('saved-workflow scan exposes only explicit server effective directory and budgets', async () => {
  const vite = await server()
  try {
    const adapters = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
    const base = {
      schema_version: 2,
      status: 'complete',
      source_count: 0,
      succeeded_source_count: 0,
      failed_source_count: 0,
      matched_count: 0,
      review_count: 0,
      sources: [],
      items: [],
    }
    const omitted = adapters.adaptComfySavedWorkflowScan(base)
    assert.equal(omitted.effectiveBatchMaxItems, null)
    assert.equal(omitted.effectiveScanMaxSeconds, null)
    assert.equal(omitted.savedWorkflowDirectory, '')
    const retiredTopLevel = adapters.adaptComfySavedWorkflowScan({
      ...base,
      batch_max_items: 37,
    })
    assert.equal(retiredTopLevel.effectiveBatchMaxItems, null)
    const configured = adapters.adaptComfySavedWorkflowScan({
      ...base,
      effective_limits: {
        scan_max_items: 250,
        scan_max_seconds: 30,
        request_max_bytes: 67108864,
        batch_max_items: 37,
        batch_max_seconds: 60,
        saved_workflow_directory: 'workflows/team-a',
      },
    })
    assert.equal(configured.effectiveBatchMaxItems, 37)
    assert.equal(configured.effectiveScanMaxItems, 250)
    assert.equal(configured.effectiveScanMaxSeconds, 30)
    assert.equal(configured.effectiveRequestMaxBytes, 67108864)
    assert.equal(configured.effectiveBatchMaxSeconds, 60)
    assert.equal(configured.savedWorkflowDirectory, 'workflows/team-a')
  } finally { await vite.close() }
})

test('provider editor does not invent transport, polling, ttl, response-field or monitoring limits', async () => {
  const vite = await server()
  try {
    const registry = await vite.ssrLoadModule('/src/services/registryResourceSchema.ts')
    const dependencies = { providers: [], modelProfiles: [], deployments: [], runtimeHosts: [], workflowVersions: [] }
    const payload = { driver_type: 'async_rest', driver_config: { progress_field: 'progress' } }
    const fields = registry.resourceEditorFields('provider', payload, dependencies)
    for (const path of [
      'driver_config.timeout_seconds', 'driver_config.poll_interval_seconds',
      'driver_config.max_poll_seconds', 'driver_config.capability_ttl_seconds',
      'driver_config.health_path', 'driver_config.submit_path', 'driver_config.poll_path',
      'driver_config.request_id_field', 'driver_config.status_field',
      'driver_config.idempotency_header', 'driver_config.progress_max',
    ]) {
      const field = fields.find((item: { path: string }) => item.path === path)
      assert.ok(field, path)
      assert.equal(field.defaultValue, undefined, `${path} default`)
      assert.equal(field.max, undefined, `${path} max`)
      assert.equal(field.min, undefined, `${path} min`)
    }
    const withDefaults = registry.applyResourceDefaults('provider', { driver_type: 'async_rest' }, dependencies)
    assert.equal(withDefaults.driver_config, undefined)

    const modelFields = registry.resourceEditorFields('model', {
      modality: 'video', specification: { capabilities: [] },
    }, dependencies)
    for (const path of [
      'specification.execution_monitoring.default_idle_timeout_seconds',
      'specification.execution_monitoring.node_type_overrides',
    ]) {
      const field = modelFields.find((item: { path: string }) => item.path === path)
      assert.ok(field, path)
      assert.equal(field.max, undefined, `${path} max`)
      assert.equal(field.min, undefined, `${path} min`)
    }
  } finally { await vite.close() }
})
