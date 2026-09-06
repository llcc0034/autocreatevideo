import assert from 'node:assert/strict'
import test from 'node:test'
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const frontendRoot = fileURLToPath(new URL('..', import.meta.url))
const server = () => createServer({ root: frontendRoot, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
const fictionalConditioningModes = [{
  key: 'cinema_text', label: '虚构影片文本模式', description: '只使用文本', input_kind: 'text_only',
  requires_entity_reference_bible: false, supports_native_audio: false,
  inputs: Object.fromEntries([
    'total_images', 'first_frame', 'last_frame', 'entity_images', 'reference_videos', 'paired_video_audios', 'reference_audios',
  ].map((key) => [key, { minimum_items: 0, maximum_items: 0 }])),
}]

test('storyboard no longer trusts or displays retired optimizer claims', () => {
  const view = readFileSync(join(frontendRoot, 'src/views/StoryboardView.vue'), 'utf8')
  assert.equal(view.includes('quality.optimizer'), false)
  assert.equal(view.includes('promptQuality.optimizer'), false)
  assert.equal(view.includes('source_fields_optimized'), false)
  assert.ok(view.includes('gate.would_block_enqueue'))
})

test('runtime source has no mock data module or environment fallback switch', () => {
  const sourceRoot = fileURLToPath(new URL('../src', import.meta.url))
  const collectFiles = (directory: string): string[] => readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name)
    return entry.isDirectory() ? collectFiles(path) : [path]
  })
  const runtimeSource = collectFiles(sourceRoot)
    .filter((path) => /\.(?:ts|vue)$/.test(path))
    .map((path) => readFileSync(path, 'utf8'))
    .join('\n')

  assert.equal(existsSync(join(sourceRoot, 'data', 'mock.ts')), false)
  for (const forbidden of [
    '@/data/mock', 'VITE_USE_MOCK', 'VITE_MOCK', 'fromConfiguredSource',
    'mockAssets', 'mockProjects', 'mockWorkflows',
  ]) assert.equal(runtimeSource.includes(forbidden), false, `runtime source still contains ${forbidden}`)
})

test('visual-rules color adapter keeps shape and uniqueness hard without a hidden count range', async () => {
  const vite = await server()
  try {
    const visualRules = await vite.ssrLoadModule('/src/services/visualRulesDraft.ts')
    const base = {
      base_colors: [],
      accent_color: 'none',
      palette_relation: 'cinematic',
      lighting: 'soft_diffuse',
      contrast: 'restrained',
      saturation: 'muted',
      exposure: 'balanced',
      progression: 'stable',
      preserve_natural_skin_exposure: true,
    }
    assert.deepEqual(visualRules.visualRulesColorDesignErrors(base), [])
    assert.deepEqual(visualRules.visualRulesColorDesignErrors({
      ...base,
      base_colors: ['black', 'charcoal', 'gray', 'slate'],
    }), [])
    assert.match(visualRules.visualRulesColorDesignErrors({
      ...base,
      base_colors: ['black', 'black'],
    }).join(' '), /不能重复/)
    assert.match(visualRules.visualRulesColorDesignErrors({
      ...base,
      base_colors: 'black',
    }).join(' '), /必须是数组/)
  } finally {
    await vite.close()
  }
})

test('API errors never expose an HTML debug page and keep structured messages', async () => {
  const vite = await server()
  try {
    const api = await vite.ssrLoadModule('/src/services/api.ts')
    assert.equal(
      api.safeApiErrorMessage('<!DOCTYPE html><html><body>secret traceback</body></html>', 500, 'text/html; charset=utf-8'),
      '服务端发生内部错误，请重试；若持续出现请查看服务日志。',
    )
    assert.equal(api.safeApiErrorMessage({ detail: '您没有执行该操作的权限。' }, 403, 'application/json'), '您没有执行该操作的权限。')
    assert.ok(api.safeApiErrorMessage('x'.repeat(5000), 400, 'text/plain').length <= 1000)
  } finally { await vite.close() }
})

test('production capability accepts the canonical configured timeout contract', async (context) => {
  const vite = await server()
  context.after(async () => { await vite.close() })
  const { repository } = await vite.ssrLoadModule('/src/services/repository.ts')
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  const originalGet = api.get
  context.after(() => { api.get = originalGet })
  let effectiveTaskTimeout = 21600
  let queueAdvisories: Array<Record<string, unknown>> = []
  api.get = async (url: string, options: { params?: Record<string, unknown> } = {}) => {
    assert.equal(url, '/capabilities/production/')
    assert.deepEqual(options.params, { project_id: '11111111-1111-4111-8111-111111111111' })
    return { data: {
      recommended_max_pending: 20,
      recommended_max_active_per_user: 5,
      max_concurrent: 1,
      configured_concurrency: 1,
      deployment_max_concurrent: 1,
      task_timeout_seconds: effectiveTaskTimeout,
      configured_task_timeout_seconds: 21600,
      task_idle_timeout_seconds: 1800,
      video_shot_min_duration_ms: 5000,
      video_shot_max_duration_ms: 15000,
      script_breakdown_default_shot_duration_ms: 5000,
      script_breakdown_default_first_frame_continuity: false,
      script_breakdown_default_last_frame_continuity: false,
      script_breakdown_auto_split_trigger_chars: 320,
      script_breakdown_auto_split_target_chars: 220,
      active_task_count: 0,
      user_active_task_count: 0,
      queue_guidance_enforcement: 'advisory',
      queue_advisories: queueAdvisories,
      business_volume_limited: false,
      effective_limits: {
        max_variants: 7, max_routing_routes: 15, max_episodes: 100, max_shots_per_episode: 100, max_total_shots: 1000, max_generation_tasks: 100,
        max_reference_bindings: 64, max_bible_candidates_per_entity: 10, max_bible_reference_assets_per_entity: 10, max_bible_regeneration_entities_per_request: 10,
        dialogue_max_entries: 64, dialogue_speaker_id_max_characters: 160, dialogue_text_max_characters: 2000, dialogue_delivery_max_characters: 500,
        execution_monitoring_max_node_type_overrides: 128, execution_monitoring_max_node_overrides: 256, execution_monitoring_max_idle_timeout_seconds: 86400,
        prompt_review_max_candidates: 100, prompt_review_max_advisories: 200, prompt_review_max_input_bytes: 524288, prompt_review_max_field_characters: 30000, prompt_review_max_usage_count: 1000000000,
        audit_note_min_characters: 8, audit_note_max_characters: 4000,
      },
      creation_defaults: {
        default_language: 'Chinese', default_aspect_ratio: '9:16',
        aspect_ratio_options: [{ value: '9:16', label: '竖屏', width: 768, height: 1344 }, { value: '16:9', label: '横屏', width: 1344, height: 768 }],
        default_shot_media_type: 'video', default_include_audio: true,
        episode_count: 1, shots_per_episode: 8, episode_duration_seconds: 90, max_generation_tasks: 20, fps: 24, width: 768, height: 1344,
      },
      configuration_public_sha256: 'a'.repeat(64),
      configuration_sources: { 'production.task_timeout_seconds': 'system' },
      conditioning_modes: fictionalConditioningModes,
      conditioning_contract_sha256: 'b'.repeat(64),
      default_conditioning_mode: 'cinema_text',
    } }
  }

  const capability = await repository.getProductionCapability('11111111-1111-4111-8111-111111111111')
  assert.equal(capability.taskTimeoutSeconds, 21600)
  assert.equal(capability.configuredTaskTimeoutSeconds, 21600)
  assert.equal(capability.scriptBreakdownDefaultShotDurationMs, 5000)
  assert.equal(capability.creationDefaults.shotsPerEpisode, 8)
  assert.equal(capability.creationDefaults.defaultLanguage, 'Chinese')
  assert.equal(capability.creationDefaults.defaultAspectRatio, '9:16')
  assert.deepEqual(capability.creationDefaults.aspectRatioOptions[1], { value: '16:9', label: '横屏', width: 1344, height: 768 })
  assert.equal(capability.creationDefaults.defaultShotMediaType, 'video')
  assert.equal(capability.creationDefaults.defaultIncludeAudio, true)
  assert.equal(capability.effectiveLimits.maxVariants, 7)
  assert.equal(capability.effectiveLimits.auditNoteMaxCharacters, 4000)
  assert.equal(capability.queueGuidanceEnforcement, 'advisory')
  assert.deepEqual(capability.queueAdvisories, [])
  assert.equal('confirmationRequired' in capability, false)
  assert.equal('deploymentTaskTimeoutSeconds' in capability, false)
  queueAdvisories = [{
    schema_version: 1,
    code: 'recommended_max_active_per_user_exceeded',
    severity: 'warning',
    scope: 'user',
    measured: 6,
    recommended_maximum: 5,
  }]
  const warned = await repository.getProductionCapability('11111111-1111-4111-8111-111111111111')
  assert.deepEqual(warned.queueAdvisories[0], {
    schemaVersion: 1,
    code: 'recommended_max_active_per_user_exceeded',
    severity: 'warning',
    scope: 'user',
    measured: 6,
    recommendedMaximum: 5,
  })
  effectiveTaskTimeout = 60
  await assert.rejects(
    repository.getProductionCapability('11111111-1111-4111-8111-111111111111'),
    /有效任务超时与配置值不一致/,
  )
})

test('shared API error presentation keeps codes and fields but hides raw provider diagnostics', async () => {
  const vite = await server()
  try {
    const errors = await vite.ssrLoadModule('/src/services/apiErrorPresentation.ts')
    const shown = errors.apiErrorPresentation({ response: { status: 400, headers: { 'content-type': 'application/json' }, data: {
      error: { code: 'config_invalid', message: '配置未通过校验', details: {
        value: ['必须是整数'], provider_response: 'raw upstream body', traceback: 'secret stack',
      } },
    } } })
    assert.deepEqual(shown, { code: 'config_invalid', message: '配置未通过校验', fields: { value: ['必须是整数'] } })
    assert.deepEqual(errors.apiErrorPresentation({ response: { status: 409, headers: { 'content-type': 'application/json' }, data: {
      error: { status_code: 409, code: 'resource_in_use', details: { detail: '当前记录仍被发布版本引用。' } },
    } } }), { code: 'resource_in_use', message: '当前记录仍被发布版本引用。', fields: {} })
    const guided = errors.apiErrorPresentation({ response: { status: 400, headers: { 'content-type': 'application/json' }, data: {
      error: { code: 'host_snapshot_stale', details: {
        detail: '当前验收部署没有可用的新鲜主机证明。',
        reason_code: 'host_snapshot_stale',
        recovery: {
          schema_version: 1,
          state: 'host_probe_required',
          title: '需要刷新验收主机证明',
          summary: '当前验收部署没有可用的新鲜主机证明。',
          steps: ['前往运行主机执行探测。', '探测成功后返回重新校验。'],
          actions: [
            { kind: 'open_settings', section: 'runtime', label: '前往运行主机' },
            { kind: 'retry_live_validate', label: '重新校验' },
            { kind: 'open_settings', section: 'security', label: '不允许的跳转' },
          ],
        },
      } },
    } } })
    assert.deepEqual(guided.recovery, {
      schemaVersion: 1,
      state: 'host_probe_required',
      title: '需要刷新验收主机证明',
      summary: '当前验收部署没有可用的新鲜主机证明。',
      steps: ['前往运行主机执行探测。', '探测成功后返回重新校验。'],
      actions: [
        { kind: 'open_settings', section: 'runtime', label: '前往运行主机' },
        { kind: 'retry_live_validate', label: '重新校验' },
      ],
    })
    const invalid = { response: { status: 400, headers: { 'content-type': 'application/json' }, data: {
      error: { code: 'invalid', details: [
        '生产运行配置未通过校验。',
        '请选择已发布且可用的提示词策略。',
        '<html>secret provider response</html>',
        'Traceback (most recent call last): secret stack',
        { provider_response: 'raw upstream body', token: 'secret-token' },
      ] },
    } } }
    assert.deepEqual(errors.apiErrorPresentation(invalid, '服务端请求失败'), {
      code: 'invalid', message: '生产运行配置未通过校验。；请选择已发布且可用的提示词策略。', fields: {},
    })
    assert.equal(errors.apiErrorSummary(invalid, '服务端请求失败'), '[invalid] 生产运行配置未通过校验。；请选择已发布且可用的提示词策略。')
    const fieldOnly = { response: { status: 400, headers: { 'content-type': 'application/json' }, data: {
      error: { code: 'invalid', details: {
        'production.prompt_normalization_deployment_id': ['所选模型部署尚不可用于 prompt.normalize'],
      } },
    } } }
    assert.equal(
      errors.apiErrorSummary(fieldOnly, '请求失败，服务端没有返回可读错误'),
      '[invalid] production.prompt_normalization_deployment_id: 所选模型部署尚不可用于 prompt.normalize',
    )
    assert.equal(JSON.stringify(errors.apiErrorPresentation(invalid)).includes('secret'), false)
    assert.equal(JSON.stringify(shown).includes('upstream'), false)
    assert.equal(errors.apiErrorPresentation({ response: { status: 500, headers: { 'content-type': 'text/html' }, data: '<html>traceback secret</html>' } }).message.includes('traceback'), false)
  } finally { await vite.close() }
})

test('platform adapters preserve unknown backend strings and artifact metadata', async () => {
  const vite = await server()
  try {
    const adapters = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
    const run = adapters.adaptProductionRun({ id: 'run-1', status: 'future_server_state', current_stage_key: 'custom_stage', created_at: '', updated_at: '' })
    assert.equal(run.status, 'future_server_state')
    assert.equal(run.currentStageKey, 'custom_stage')
    assert.deepEqual(adapters.presentPlatformStatus('future_server_state'), { label: '未知状态', tone: 'unknown' })
    const definitionContract = { has_values: false, mutable_fields: ['description'] }
    const standardDefinition = adapters.adaptConfigDefinition({ id: 'config-standard', configuration_class: 'standard', ...definitionContract })
    const securityDefinition = adapters.adaptConfigDefinition({ id: 'config-security', configuration_class: 'security_boundary', ...definitionContract })
    const missingClassDefinition = adapters.adaptConfigDefinition({ id: 'config-missing', ...definitionContract })
    const futureClassDefinition = adapters.adaptConfigDefinition({ id: 'config-future', configuration_class: 'future_security_v2', ...definitionContract })
    assert.equal(standardDefinition.configurationClass, 'standard')
    assert.equal(securityDefinition.configurationClass, 'security_boundary')
    assert.deepEqual([missingClassDefinition.configurationClass, missingClassDefinition.configurationClassRaw], ['unknown', ''])
    assert.deepEqual([futureClassDefinition.configurationClass, futureClassDefinition.configurationClassRaw], ['unknown', 'future_security_v2'])
    assert.throws(() => adapters.adaptPromptProfileOptions({ operations: [] }))
    assert.throws(() => adapters.adaptConfigDefinitionOptions({ configuration_classes: [] }))
    assert.throws(() => adapters.adaptProductionRecipeEditorOptions({ definition_schema_version: 1 }))
    const securityRevision = adapters.adaptConfigRevision({ id: 'revision-security', changes_security_boundaries: true })
    assert.equal(securityRevision.changesSecurityBoundaries, true)
    assert.equal(adapters.adaptConfigRevision({ id: 'revision-unclassified' }).changesSecurityBoundaries, null)
    const artifact = adapters.adaptGeneratedArtifact({ id: 'a-1', kind: 'future_media', status: 'server_selected_v2', manifest_metadata: { task_id: 't-1', output_index: 3 }, comparison_parameters: { model: 'model-v2', generation: { seed: 29 } }, created_at: '', updated_at: '' })
    assert.equal(artifact.kind, 'future_media')
    assert.equal(artifact.status, 'server_selected_v2')
    assert.deepEqual(artifact.manifestMetadata, { task_id: 't-1', output_index: 3 })
    assert.deepEqual(artifact.comparisonParameters, { model: 'model-v2', generation: { seed: 29 } })
    const mediaRoutes = adapters.adaptProductionRecipeMediaRouteOptions({
      schema_version: 2,
      recipe_version_id: 'recipe-1',
      recipe_definition_sha256: 'c'.repeat(64),
      routes: [{
        route_index: 2,
        workflow_version_id: 'workflow-1',
        workflow_label: 'H3 参考生视频 · v3',
        model_deployment_id: 'deployment-1',
        deployment_label: 'Local ComfyUI',
        model_identifier: 'minimax-h3',
        driver_type: 'comfyui',
        output_kinds: ['video'],
        conditioning_modes: fictionalConditioningModes,
        conditioning_contract_sha256: 'e'.repeat(64),
        media_operations: [],
        is_selectable: false,
        reason_codes: ['workflow_smoke_missing'],
        reasons: ['工作流未通过 smoke'],
        prompt_profile_requirements: [{
          operation: 'video.generate', mode: 'Ref2VA', choices: [{
            binding_id: 'binding-1', binding_key: 'h3-ref-cinematic', binding_name: '电影感参考生视频',
            prompt_profile_version_id: 'prompt-profile-1', profile_key: 'h3.ref.cinematic', profile_version: 3,
            profile_name: 'H3 电影感参考策略', contract_sha256: 'd'.repeat(64), priority: 20,
            binding_scope: 'exact_model',
          }],
        }],
      }],
      bible_image_generation: null,
    })
    assert.equal(mediaRoutes.schemaVersion, 2)
    assert.equal(mediaRoutes.routes[0].routeIndex, 2)
    assert.deepEqual(mediaRoutes.routes[0].conditioningModes.map((item: { key: string }) => item.key), ['cinema_text'])
    assert.equal(mediaRoutes.routes[0].conditioningContractSha256, 'e'.repeat(64))
    assert.deepEqual(mediaRoutes.routes[0].reasonCodes, ['workflow_smoke_missing'])
    assert.deepEqual(mediaRoutes.routes[0].conditioningModes[0].inputs.referenceVideos, {
      minimumItems: 0, maximumItems: 0,
    })
    assert.equal(mediaRoutes.routes[0].promptProfileRequirements[0].choices[0].bindingId, 'binding-1')
    const snapshotted = adapters.adaptProductionRun({
      id: 'run-snapshots', created_at: '', updated_at: '',
      stages: [{
        id: 'stage-1', stage_key: 'script_generate', created_at: '', updated_at: '',
        decision_policy: 'approve_single_frozen_script_v1', decision_policy_sha256: 'c'.repeat(64),
        input_snapshot: { prompt: 'frozen' }, input_snapshot_sha256: 'a'.repeat(64),
        output_snapshot: { review_payload: { kind: 'generated_script_preview' } }, output_snapshot_sha256: 'b'.repeat(64),
        approval_decision_facts: [{
          id: 'decision-1', revision: 1, decision: 'approve', comment: 'policy approved',
          output_snapshot_sha256: 'b'.repeat(64), actor: 'recipe_policy',
          decision_policy: 'approve_single_frozen_script_v1', decision_policy_sha256: 'c'.repeat(64),
          decided_by: null, created_at: '2026-08-25T00:00:00Z',
        }],
      }],
    })
    assert.deepEqual(snapshotted.stages[0].inputSnapshot, { prompt: 'frozen' })
    assert.equal(snapshotted.stages[0].inputSnapshotSha256, 'a'.repeat(64))
    assert.deepEqual(snapshotted.stages[0].outputSnapshot, { review_payload: { kind: 'generated_script_preview' } })
    assert.equal(snapshotted.stages[0].outputSnapshotSha256, 'b'.repeat(64))
    assert.equal(snapshotted.stages[0].decisionPolicy, 'approve_single_frozen_script_v1')
    assert.equal(snapshotted.stages[0].approvalDecisionFacts[0].actor, 'recipe_policy')
    assert.equal(snapshotted.stages[0].approvalDecisionFacts[0].decidedBy, '')
  } finally { await vite.close() }
})

test('settings registry adapters preserve immutable review evidence after refresh', async () => {
  const vite = await server()
  try {
    const adapters = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
    const profile = adapters.adaptModelProfileVersion({
      id: 'profile-1', profile_key: 'text-model', version: 2, provider: 'provider-1',
      specification: { capabilities: ['script.generate'] }, specification_schema_version: 1,
      specification_size_bytes: 128, specification_sha256: 'a'.repeat(64),
      status: 'published', published_by: 'reviewer-1', created_at: '', updated_at: '',
    })
    assert.equal(profile.publishedBy, 'reviewer-1')
    assert.equal(profile.specificationSha256, 'a'.repeat(64))
    const registryOptions = adapters.adaptModelProfileRegistryOptions({
      specification_schema_version: 1,
      provider_drivers: [{ value: 'comfyui', label: 'ComfyUI', modalities: ['image'], operations: ['submit'] }],
      modalities: [{ value: 'image', label: '图片' }],
      artifact_roles: [{ value: 'checkpoint', label: 'Checkpoint' }],
      capabilities: [{ value: 'image.text_to_image', label: '文生图', modalities: ['image'], driver_types: ['comfyui'], legacy: false }],
      model_families: ['sdxl'],
      conditioning_contract: {
        schema_version: 1, adapter_key: 'media_conditioning_v1',
        input_kinds: [{value:'classified_references',label:'多参考素材'}],
        input_slots: [{value:'total_images',label:'图片总数'}],
        maximum_items: 64, maximum_modes: 16, identifier_pattern: '^[A-Za-z][A-Za-z0-9_.-]{0,63}$',
        mode_template: { inputs: { total_images: {minimum_items:0,maximum_items:0} } },
      },
    })
    assert.equal(registryOptions.specificationSchemaVersion, 1)
    assert.deepEqual(registryOptions.providerDrivers[0].operations, ['submit'])
    assert.deepEqual(registryOptions.capabilities[0].driverTypes, ['comfyui'])
    assert.throws(() => adapters.adaptModelProfileRegistryOptions({
      specification_schema_version: 1,
      provider_drivers: [],
      modalities: [],
      artifact_roles: [],
      capabilities: [],
      model_families: [],
    }), /没有已注册 Provider 驱动/)
    assert.throws(() => adapters.adaptModelProfileRegistryOptions({
      specification_schema_version: 1,
      provider_drivers: [{ value: 'comfyui', label: 'ComfyUI' }],
      modalities: [{ value: 'image', label: '图片' }],
      artifact_roles: [{ value: 'checkpoint', label: 'Checkpoint' }],
      capabilities: [{ value: 'image.text_to_image', label: '文生图', modalities: ['image'], driver_types: ['comfyui'], legacy: false }],
      model_families: [],
    }), /provider_drivers\[0\]\.modalities/)
    const deployment = adapters.adaptModelDeployment({
      id: 'deployment-1', profile_version: 'profile-1', provider_profile_id: 'provider-1',
      latest_smoke: { id: 'smoke-1', status: 'passed', operations: ['prompt.normalize'], captured_at: '2026-08-23T00:00:00Z', expires_at: '2026-08-24T00:00:00Z', fresh: true },
      runtime_config: {}, runtime_config_sha256: 'b'.repeat(64), created_at: '', updated_at: '',
    })
    assert.deepEqual(deployment.latestSmoke?.operations, ['prompt.normalize'])
    assert.equal(deployment.latestSmoke?.fresh, true)
    const dynamicReferences = {
      mode: 'NebulaTransfer', maximum_physical_assets: 18,
      pictures: { min_items: 0, max_items: 9, categories: ['character', 'continuity_frame'] },
      videos: { min_items: 0, max_items: 3, purposes: ['orbit_rhythm_v7'], default_purpose_key: 'orbit_rhythm_v7', supports_embedded_audio: true, supports_external_audio: true },
      audios: { min_items: 0, max_items: 3, purposes: ['pulse_track_q'], default_purpose_key: 'pulse_track_q' },
      conditioning_mode: {
        key: 'NebulaTransfer', label: 'Nebula Transfer', description: '', input_kind: 'classified_references',
        requires_entity_reference_bible: false, supports_native_audio: true,
        inputs: {
          total_images: { minimum_items: 0, maximum_items: 9 },
          first_frame: { minimum_items: 0, maximum_items: 0 },
          last_frame: { minimum_items: 0, maximum_items: 0 },
          entity_images: { minimum_items: 0, maximum_items: 9 },
          reference_videos: { minimum_items: 0, maximum_items: 3 },
          paired_video_audios: { minimum_items: 0, maximum_items: 3 },
          reference_audios: { minimum_items: 0, maximum_items: 3 },
        },
        dynamic_reference_topology: {
          schema_version: 1,
          videos: {
            minimum_items: 0, maximum_items: 3, default_purpose_key: 'orbit_rhythm_v7',
            purposes: [{ key: 'orbit_rhythm_v7', label: 'Orbit rhythm', description: '', minimum_items: 0, maximum_items: 3 }],
          },
          audios: {
            minimum_items: 0, maximum_items: 3, default_purpose_key: 'pulse_track_q',
            purposes: [{ key: 'pulse_track_q', label: 'Pulse track', description: '', minimum_items: 0, maximum_items: 3 }],
          },
          preprocessing: {
            videos: { adapter_key: 'cfr_video_v1', minimum_duration_ms: 1, maximum_duration_ms: 15000, target_fps: 24, requires_source_fps: true },
            paired_video_audios: { adapter_key: 'aligned_audio_v1', minimum_duration_ms: 1, maximum_duration_ms: 15000 },
            reference_audios: { adapter_key: 'passthrough_audio_v1', minimum_duration_ms: 1, maximum_duration_ms: 15000 },
          },
          pairing_rules: [{
            key: 'video_audio_pair', label: 'Video audio pair', description: '', source_media_kind: 'video', target_media_kind: 'audio',
            source_purpose_keys: ['orbit_rhythm_v7'], target_purpose_keys: ['pulse_track_q'], default_target_purpose_key: 'pulse_track_q',
            minimum_targets_per_source: 0, maximum_targets_per_source: 1,
            allow_embedded_source: true, allow_external_target: true, source_and_external_mutually_exclusive: true,
          }],
          mutual_exclusion_rules: [],
        },
      },
    }
    const workflow = adapters.adaptWorkflowTemplateVersion({
      id: 'workflow-1', template: 'template-1', version: 3,
      validated_deployment: 'deployment-1', validation_state: 'passed', smoke_test_state: 'passed',
      validation_evidence: { object_info_sha256: 'c'.repeat(64) },
      smoke_test_evidence: { output_sha256: 'd'.repeat(64) },
      validation_evidence_sha256: 'e'.repeat(64), smoke_test_evidence_sha256: 'f'.repeat(64),
      execution_contract_sha256: '1'.repeat(64), published_by: 'reviewer-2', created_at: '', updated_at: '',
      smoke_fixture_schema: {
        schema_version: 2, available: true,
        parameters: [{ semantic_key: 'sampling.seed', type: 'integer', required: true, min: 0, max: 100, multiple_of: 1, alignment_modulus: null, alignment_remainder: null, options: [], selection_group: '', recommended_value: 1 }],
        asset_slots: [{ semantic_key: 'asset.image.source', input_slot: 1, media_kind: 'image', category: 'image', min_items: 1, max_items: 1, required: true }],
        dynamic_reference_modes: { NebulaTransfer: dynamicReferences },
        dynamic_references: dynamicReferences,
      },
    })
    assert.equal(workflow.validatedDeployment, 'deployment-1')
    assert.deepEqual(workflow.validationEvidence, { object_info_sha256: 'c'.repeat(64) })
    assert.deepEqual(workflow.smokeTestEvidence, { output_sha256: 'd'.repeat(64) })
    assert.equal(workflow.smokeFixtureSchema?.parameters[0].recommendedValue, 1)
    assert.equal(workflow.smokeFixtureSchema?.assetSlots[0].minItems, 1)
    assert.equal(workflow.smokeFixtureSchema?.schemaVersion, 2)
    assert.equal(workflow.smokeFixtureSchema?.dynamicReferences?.videos.maxItems, 3)
    assert.equal(workflow.smokeFixtureSchema?.dynamicReferences?.videos.defaultPurposeKey, 'orbit_rhythm_v7')
    assert.equal(workflow.smokeFixtureSchema?.dynamicReferences?.videos.supportsEmbeddedAudio, true)
    assert.equal(workflow.publishedBy, 'reviewer-2')
    const parameterBase = {
      semantic_key: 'video.frames', type: 'integer', required: true,
      min: -12, max: 90, multiple_of: null,
      alignment_modulus: 17, alignment_remainder: 5,
      min_length: null, max_length: null, options: [], selection_group: '',
      recommended_value: -12,
    }
    const adaptParameter = (parameter: Record<string, unknown>) => (
      adapters.adaptWorkflowSmokeFixtureSchema({
        schema_version: 2, available: true, parameters: [parameter],
        asset_slots: [], dynamic_reference_modes: {}, dynamic_references: null,
      })
    )
    assert.equal(adaptParameter(parameterBase).parameters[0].alignmentModulus, 17)
    assert.throws(() => adaptParameter({
      semantic_key: 'video.frames', type: 'integer', required: true,
      min: 5, max: 90, multiple_of: null, options: [], selection_group: '',
      recommended_value: 5,
    }), /缺少整数对齐合同字段/)
    for (const invalid of [
      { alignment_modulus: 17, alignment_remainder: null },
      { alignment_modulus: 0, alignment_remainder: 0 },
      { alignment_modulus: 17.5, alignment_remainder: 5 },
      { alignment_modulus: 17, alignment_remainder: 17 },
    ]) {
      assert.throws(() => adaptParameter({ ...parameterBase, ...invalid }), /整数对齐合同无效/)
    }
    const historicalWorkflow = {
      id: 'workflow-historical', template: 'template-1', version: 1, created_at: '', updated_at: '',
      input_contract: { schema_version: 1, parameters: { 'prompt.positive': { type: 'text', required: true } }, outputs: { primary: { node_id: '13', kind: 'video', required: true } } },
      smoke_fixture_schema: { schema_version: 2, available: true, parameters: [], asset_slots: [] },
    }
    assert.throws(() => adapters.adaptWorkflowTemplateVersion(historicalWorkflow), /缺少 dynamic_reference_modes/)
    const historicalAudit = adapters.adaptWorkflowTemplateVersionForAudit(historicalWorkflow)
    assert.match(historicalAudit.smokeFixtureSchemaError, /缺少 dynamic_reference_modes/)
    assert.equal(historicalAudit.inputContract.outputs.primary.node_id, '13')
    const artifact = adapters.adaptModelArtifact({
      id: 'artifact-1', profile_version: 'profile-1', verified_snapshot: 'snapshot-1',
      verified_at: '2026-08-23T00:00:00Z', verified_by: 'reviewer-3', created_at: '', updated_at: '',
    })
    assert.equal(artifact.verifiedSnapshot, 'snapshot-1')
    assert.equal(artifact.verifiedBy, 'reviewer-3')
  } finally { await vite.close() }
})

test('workflow version listing preserves pagination with and without an exact model filter', async (context) => {
  const vite = await server()
  context.after(async () => { await vite.close() })
  const { platformApi } = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  const originalGet = api.get
  context.after(() => { api.get = originalGet })

  const endpoint = '/workflow-template-versions/'
  const modelProfileVersion = 'aaaaaaaa-1111-4111-8111-111111111111'
  const requestedModelProfileVersion = modelProfileVersion.toUpperCase()
  const nextPage = '/workflow-template-versions/?page=2'
  const filteredNextPage = `/workflow-template-versions/?model_profile_version=${modelProfileVersion}&page=2`
  const calls: Array<{ url: string; params?: Record<string, unknown> }> = []
  api.get = async (url: string, options: { params?: Record<string, unknown> } = {}) => {
    calls.push({ url, params: options.params })
    if (url === endpoint) {
      const exactModel = String(options.params?.model_profile_version || '')
      return { data: { results: [{ id: 'workflow-page-1', model_profile_version: exactModel }], next: exactModel ? filteredNextPage : nextPage } }
    }
    if (url === filteredNextPage) {
      return { data: { results: [{ id: 'workflow-page-2', model_profile_version: modelProfileVersion }], next: null } }
    }
    if (url === nextPage) {
      return { data: { results: [{ id: 'workflow-page-2', model_profile_version: '22222222-2222-4222-8222-222222222222' }], next: null } }
    }
    throw new Error(`unexpected workflow list URL: ${url}`)
  }

  const filtered = await platformApi.listWorkflowVersions({ modelProfileVersion: requestedModelProfileVersion })
  assert.deepEqual(filtered.map((item: { id: string }) => item.id), ['workflow-page-1', 'workflow-page-2'])
  assert.deepEqual(calls, [
    { url: endpoint, params: { model_profile_version: modelProfileVersion, page_size: 50 } },
    { url: filteredNextPage, params: undefined },
  ])

  calls.length = 0
  const unfiltered = await platformApi.listWorkflowVersions()
  assert.deepEqual(unfiltered.map((item: { id: string }) => item.id), ['workflow-page-1', 'workflow-page-2'])
  assert.deepEqual(calls, [
    { url: endpoint, params: { page_size: 50 } },
    { url: nextPage, params: undefined },
  ])

  api.get = async () => ({ data: {
    results: [{ id: 'workflow-wrong-model', model_profile_version: '22222222-2222-4222-8222-222222222222' }],
    next: null,
  } })
  await assert.rejects(
    () => platformApi.listWorkflowVersions({ modelProfileVersion: requestedModelProfileVersion }),
    /不属于精确模型版本/,
  )
})

test('latest request commit rejects stale unfiltered success and stale failure', async (context) => {
  const vite = await server()
  context.after(async () => { await vite.close() })
  const { commitLatestRequest } = await vite.ssrLoadModule('/src/services/latestRequestCommit.ts')
  const deferred = <T>() => {
    let resolve!: (value: T) => void
    let reject!: (cause: unknown) => void
    const promise = new Promise<T>((resolvePromise, rejectPromise) => {
      resolve = resolvePromise
      reject = rejectPromise
    })
    return { promise, resolve, reject }
  }
  let generation = 0
  let rows: string[] = []
  let failure = ''
  const begin = (request: Promise<string[]>) => {
    const current = ++generation
    return commitLatestRequest(
      request,
      () => current === generation,
      (value: string[]) => { rows = value; failure = '' },
      (cause: unknown) => { failure = String(cause) },
    )
  }

  const oldUnfiltered = deferred<string[]>()
  const oldUnfilteredTask = begin(oldUnfiltered.promise)
  const newFiltered = deferred<string[]>()
  const newFilteredTask = begin(newFiltered.promise)
  newFiltered.resolve(['new-filtered-model'])
  await newFilteredTask
  oldUnfiltered.resolve(['old-unfiltered-model'])
  await oldUnfilteredTask
  assert.deepEqual(rows, ['new-filtered-model'])

  const oldFailure = deferred<string[]>()
  const oldFailureTask = begin(oldFailure.promise)
  const currentSuccess = deferred<string[]>()
  const currentSuccessTask = begin(currentSuccess.promise)
  currentSuccess.resolve(['current-success'])
  await currentSuccessTask
  oldFailure.reject(new Error('old request failed'))
  await oldFailureTask
  assert.deepEqual(rows, ['current-success'])
  assert.equal(failure, '')
})

test('model workflow context uses one exact version link and fails closed without automatic operations', () => {
  const settings = readFileSync(join(frontendRoot, 'src', 'views', 'SettingsView.vue'), 'utf8')
  const sourceBlock = (startMarker: string, endMarker: string, label: string) => {
    const startIndex = settings.indexOf(startMarker)
    const endIndex = settings.indexOf(endMarker, Math.max(startIndex, 0))
    assert.ok(startIndex >= 0, `${label} misses ${startMarker}`)
    assert.ok(endIndex > startIndex, `${label} misses ${endMarker} after its start marker`)
    return settings.slice(startIndex, endIndex)
  }
  for (const marker of [
    "model_profile_version: profile.id",
    "profile.status === 'draft' ? 'attest' : profile.status === 'retired' ? 'history' : 'associate'",
    "if (profile.status === 'draft') return '从工作流提取并证明'",
    "if (profile.status === 'retired') return '查看历史工作流'",
    "return '管理工作流关联'",
    "workflowModelContextProvider.value.driverType !== 'comfyui'",
    "platformApi.listWorkflowVersions(workflowVersionListOptions())",
    "profile.id !== workflowModelContextId.value",
    "draft.modelProfileVersionId === workflowModelContextId.value",
    '已恢复的导入会话绑定其他模型版本；本页未自动覆盖',
    '当前精确模型版本没有符合筛选条件的关联工作流版本',
    '<WorkflowConfigurationLinks :workflow="item" compact/>',
    '@click="clearWorkflowModelContext"',
    'let settingsLoadGeneration = 0',
    'let savedWorkflowScanGeneration = 0',
    'workflowContextSnapshot',
    'workflowLoadStillCurrent',
    'captureCurrent',
    'workflowModelContextReadOnly',
    "return 'provider_inactive'",
    "return provider.isActive ? 'available' as const : 'historical' as const",
    '<template v-if="!workflowModelContextReadOnly">',
    '只读历史',
    'savedWorkflowPreservedSessionItems',
    ".toLocaleLowerCase('en-US')",
    'classifySavedWorkflowModelContext',
    'visibleSavedWorkflowItems',
    '当前模型扫描视图',
    '{{ savedWorkflowManagedCount }}/{{ currentModelSavedWorkflowItems.length }} 个当前候选已纳管',
    '{{ savedWorkflowContractCount }} 个已生成',
    '{{ promotableSavedWorkflowItems.length }} 个已就绪',
    '成功读取的受控目录中没有工作流文件',
    '显示其他扫描结果',
    'savedWorkflowIsContextDiagnostic(item)',
    'savedWorkflowIsContextReadOnly(item)',
    'currentModelSavedWorkflowContractBlockedCount',
    '当前模型候选 · 结构合同受阻',
    '其他扫描结果 · 只读诊断',
    'savedWorkflowPreservedSessionItems.set(savedWorkflowPreservedSessionKey(item), previous)',
  ]) assert.ok(settings.includes(marker), `model workflow context misses ${marker}`)

  assert.ok(settings.includes('if (!contextQueryError)'))
  assert.ok(settings.includes("captureWorkflow('workflows', platformApi.listWorkflowVersions(contextId ? { modelProfileVersion: contextId } : {})"))
  assert.ok(settings.includes('v-for="item in visibleSavedWorkflowItems"'))
  assert.equal(settings.includes('v-for="item in savedWorkflowItems"'), false)
  const savedWorkflowPromotableBlock = sourceBlock(
    'const savedWorkflowPromotable =',
    'const savedWorkflowReady =',
    'saved workflow promotability',
  )
  const readOnlyGateIndex = savedWorkflowPromotableBlock.indexOf('savedWorkflowIsContextReadOnly(item)')
  const nonTemplateReadyIndex = savedWorkflowPromotableBlock.indexOf("draft.importKind !== 'template_version'")
  assert.ok(readOnlyGateIndex >= 0, 'saved workflow promotability must contain the model-context read-only gate')
  assert.ok(nonTemplateReadyIndex >= 0, 'saved workflow promotability must contain the non-template branch')
  assert.ok(readOnlyGateIndex < nonTemplateReadyIndex, 'the read-only gate must run before any early-promotable branch')
  const savedWorkflowSelectionBlock = sourceBlock(
    'const savedWorkflowReady =',
    'const savedWorkflowBatchPayload =',
    'saved workflow promotion selection',
  )
  assert.ok(savedWorkflowSelectionBlock.includes('?.selected && savedWorkflowPromotable(item)'), 'batch selection must be separate from promotability')
  assert.ok(settings.includes("item.suggestedImportKind==='template_version'&&!savedWorkflowPromotable(item)"))
  assert.ok(settings.includes('步骤 1 · 纳管候选'))
  assert.ok(settings.includes('步骤 2 · 生成合同草稿'))
  assert.ok(settings.includes('步骤 3 · 提升工作流草稿'))
  assert.ok(settings.includes('定位第一个阻断项'))
  assert.ok(settings.includes('查看合同技术证据'))
  assert.ok(settings.includes('class="saved-workflow-technical"'))
  assert.ok(settings.includes('aria-describedby="saved-workflow-promote-help"'))
  const savedWorkflowProgressIndex = settings.indexOf('class="saved-workflow-process"')
  const savedWorkflowPromoteBarIndex = settings.indexOf('<footer v-if="currentModelSavedWorkflowItems.length" class="saved-workflow-footer">')
  const savedWorkflowListIndex = settings.indexOf('<div v-if="savedWorkflowItems.length" id="saved-workflow-scan-results" class="saved-workflow-list">')
  assert.ok(savedWorkflowProgressIndex >= 0, 'saved workflow progress summary must exist')
  assert.ok(savedWorkflowPromoteBarIndex > savedWorkflowProgressIndex, 'step 3 action bar must follow the progress summary')
  assert.ok(savedWorkflowPromoteBarIndex < savedWorkflowListIndex, 'step 3 action bar must remain reachable before the long candidate list')
  assert.ok(settings.includes('.saved-workflow-importer{margin-bottom:16px;overflow:visible}'), 'the importer container must not disable its sticky step 3 action bar')
  assert.ok(settings.includes('.saved-workflow-footer{align-items:center;border-top:1px solid #2a313a;background:#0a0e12;position:sticky;top:0;z-index:3}'), 'desktop step 3 action bar must remain visible while reviewing the long candidate list')
  assert.equal(settings.includes('||!savedWorkflowDrafts[savedWorkflowKey(item)].modelProfileVersionId" :title="item.candidateManaged'), false, 'contract draft generation must not require a selected model version')
  assert.ok(settings.includes('const selectedSavedWorkflowCount = computed(() => currentModelSavedWorkflowItems.value.filter'))
  const savedWorkflowBatchBlock = sourceBlock(
    'const batchImportSavedWorkflows =',
    'const closeAction =',
    'saved workflow batch import',
  )
  assert.ok(savedWorkflowBatchBlock.includes('readySavedWorkflowItems.value.map'), 'batch payloads must be derived only from ready context rows')
  assert.equal(savedWorkflowBatchBlock.includes('savedWorkflowItems.value.map'), false, 'batch import must not map the raw global scan result')
  const restoreBlock = sourceBlock(
    'const restoreSavedWorkflowAttestationsAfterScan =',
    'const resetSavedWorkflowDrafts =',
    'saved workflow attestation restoration',
  )
  const restoreEntryCurrentGateIndex = restoreBlock.indexOf('if (!isCurrent()) return')
  const restoreReadOnlyGateIndex = restoreBlock.indexOf('savedWorkflowIsContextReadOnly(item)')
  const restoredPreviewIndex = restoreBlock.indexOf('const preview = draft.preview')
  const restoredPreviewGateIndex = restoreBlock.indexOf('if (!preview) continue', restoredPreviewIndex)
  const reconcileIndex = restoreBlock.indexOf('reconcileSavedWorkflowReviewHashes', restoredPreviewGateIndex)
  const contractDraftGateIndex = restoreBlock.indexOf('draft.reviewRequiresConfirmation = !preview.contractDraftId', reconcileIndex)
  const restoreAttemptMatchIndex = restoreBlock.indexOf('attestationMatchesSavedWorkflowReview')
  const restoreTimerIndex = restoreBlock.indexOf('window.setTimeout')
  for (const [label, index] of [
    ['restore entry currentness gate', restoreEntryCurrentGateIndex],
    ['read-only restore gate', restoreReadOnlyGateIndex],
    ['server-discovery preview', restoredPreviewIndex],
    ['server-discovery preview gate', restoredPreviewGateIndex],
    ['restored hash reconciliation', reconcileIndex],
    ['persisted ContractDraft gate', contractDraftGateIndex],
    ['attestation match', restoreAttemptMatchIndex],
    ['attestation polling timer', restoreTimerIndex],
  ] as const) assert.ok(index >= 0, `saved workflow restoration misses ${label}`)
  assert.ok(restoreEntryCurrentGateIndex < restoreReadOnlyGateIndex)
  assert.ok(restoreReadOnlyGateIndex < restoredPreviewIndex)
  assert.ok(restoredPreviewIndex < restoredPreviewGateIndex)
  assert.ok(restoredPreviewGateIndex < reconcileIndex)
  assert.ok(reconcileIndex < contractDraftGateIndex)
  assert.equal(restoreBlock.includes('platformApi.previewComfySavedWorkflow'), false, 'page restoration must never create a ContractDraft implicitly')
  assert.equal(restoreBlock.includes('applySavedWorkflowPreview'), false, 'page restoration only consumes server discovery facts')
  assert.ok(restoreReadOnlyGateIndex < restoreAttemptMatchIndex)
  assert.ok(restoreReadOnlyGateIndex < restoreTimerIndex)
  for (const [startMarker, endMarker, actionMarker] of [
    ['const previewSavedWorkflow =', 'const refreshSavedWorkflowContract =', 'platformApi.previewComfySavedWorkflow'],
    ['const refreshSavedWorkflowContract =', 'const openSavedWorkflowModelDraft =', 'draft.outputKind ='],
    ['const openSavedWorkflowModelDraft =', 'const refreshSavedWorkflowAttestation =', "openEditor('model')"],
    ['const refreshSavedWorkflowAttestation =', 'const attestSavedWorkflowModels =', 'platformApi.getRuntimeHostProbeAttempt'],
    ['const attestSavedWorkflowModels =', 'const hardSavedWorkflowBlockers =', 'platformApi.attestComfySavedWorkflowModelArtifacts'],
  ]) {
    const block = sourceBlock(startMarker, endMarker, startMarker)
    const gateIndex = block.indexOf('savedWorkflowIsContextReadOnly(item)')
    const actionIndex = block.indexOf(actionMarker)
    assert.ok(gateIndex >= 0, `${startMarker} misses the model-context read-only gate`)
    assert.ok(actionIndex >= 0, `${startMarker} misses ${actionMarker}`)
    assert.ok(gateIndex < actionIndex, `${startMarker} must reject read-only rows before ${actionMarker}`)
  }
  const operationGuardBlock = sourceBlock(
    'const savedWorkflowOperationGuard =',
    'const previewSavedWorkflow =',
    'saved workflow operation currentness guard',
  )
  for (const marker of [
    'const scanGenerationSnapshot = savedWorkflowScanGeneration',
    'const loadGenerationSnapshot = settingsLoadGeneration',
    'const contextSnapshot = JSON.stringify(route.query.model_profile_version ?? null)',
    'scanGenerationSnapshot === savedWorkflowScanGeneration',
    'loadGenerationSnapshot === settingsLoadGeneration',
    "tab.value === 'workflows'",
    'JSON.stringify(route.query.model_profile_version ?? null) === contextSnapshot',
    'savedWorkflowScanActionable.value',
    'workflowModelContextCanImport.value',
    'savedWorkflowDrafts[key] === draft',
    'row.sourceKey === item.sourceKey && row.sourceSha256 === item.sourceSha256',
    '!savedWorkflowIsContextReadOnly(item)',
  ]) assert.ok(operationGuardBlock.includes(marker), `saved workflow operation guard misses ${marker}`)
  const previewBlock = sourceBlock(
    'const previewSavedWorkflow =',
    'const refreshSavedWorkflowContract =',
    'saved workflow preview',
  )
  const previewGuardIndex = previewBlock.indexOf('const operationStillCurrent = savedWorkflowOperationGuard(item, draft)')
  const previewRequestIndex = previewBlock.indexOf('await platformApi.previewComfySavedWorkflow')
  const previewResponseGateIndex = previewBlock.indexOf('if (!operationStillCurrent()) return', previewRequestIndex)
  const previewApplyIndex = previewBlock.indexOf('applySavedWorkflowPreview', previewResponseGateIndex)
  const previewCatchIndex = previewBlock.indexOf('} catch (cause)')
  const previewCatchGateIndex = previewBlock.indexOf('if (operationStillCurrent()) draft.previewError =', previewCatchIndex)
  const previewBusyOwnerIndex = previewBlock.indexOf("if (savedWorkflowBusy.value === busyKey) savedWorkflowBusy.value = ''")
  for (const [label, index] of [
    ['preview operation guard', previewGuardIndex],
    ['preview request', previewRequestIndex],
    ['preview response currentness gate', previewResponseGateIndex],
    ['preview apply', previewApplyIndex],
    ['preview catch', previewCatchIndex],
    ['preview catch currentness gate', previewCatchGateIndex],
    ['preview busy ownership gate', previewBusyOwnerIndex],
  ] as const) assert.ok(index >= 0, `saved workflow preview misses ${label}`)
  assert.ok(previewGuardIndex < previewRequestIndex)
  assert.ok(previewRequestIndex < previewResponseGateIndex)
  assert.ok(previewResponseGateIndex < previewApplyIndex)
  assert.ok(previewCatchIndex < previewCatchGateIndex)
  assert.ok(previewCatchGateIndex < previewBusyOwnerIndex)

  const refreshAttestationBlock = sourceBlock(
    'const refreshSavedWorkflowAttestation =',
    'const attestSavedWorkflowModels =',
    'saved workflow attestation refresh',
  )
  const refreshGuardIndex = refreshAttestationBlock.indexOf('const operationStillCurrent = savedWorkflowOperationGuard(item, draft)')
  const refreshRequestIndex = refreshAttestationBlock.indexOf('await platformApi.getRuntimeHostProbeAttempt')
  const refreshResponseGateIndex = refreshAttestationBlock.indexOf('if (!refreshStillCurrent()) return', refreshRequestIndex)
  const refreshFirstApplyIndex = refreshAttestationBlock.indexOf('draft.attestation = refreshedAttempt', refreshResponseGateIndex)
  const refreshProfilesRequestIndex = refreshAttestationBlock.indexOf('await platformApi.listModelProfiles()')
  const refreshProfilesGateIndex = refreshAttestationBlock.indexOf('if (!refreshStillCurrent()) return', refreshProfilesRequestIndex)
  const refreshProfilesApplyIndex = refreshAttestationBlock.indexOf('modelProfiles.value = refreshedProfiles', refreshProfilesGateIndex)
  const refreshCatchIndex = refreshAttestationBlock.indexOf('} catch (cause)')
  const refreshCatchGateIndex = refreshAttestationBlock.indexOf('if (!refreshStillCurrent()) return', refreshCatchIndex)
  const refreshCatchWriteIndex = refreshAttestationBlock.indexOf('draft.previewError =', refreshCatchGateIndex)
  for (const [label, index] of [
    ['attestation refresh operation guard', refreshGuardIndex],
    ['attestation refresh request', refreshRequestIndex],
    ['attestation refresh response gate', refreshResponseGateIndex],
    ['attestation refresh first apply', refreshFirstApplyIndex],
    ['profile refresh request', refreshProfilesRequestIndex],
    ['profile refresh response gate', refreshProfilesGateIndex],
    ['profile refresh apply', refreshProfilesApplyIndex],
    ['attestation refresh catch', refreshCatchIndex],
    ['attestation refresh catch gate', refreshCatchGateIndex],
    ['attestation refresh catch write', refreshCatchWriteIndex],
  ] as const) assert.ok(index >= 0, `saved workflow attestation refresh misses ${label}`)
  assert.ok(refreshGuardIndex < refreshRequestIndex)
  assert.ok(refreshRequestIndex < refreshResponseGateIndex)
  assert.ok(refreshResponseGateIndex < refreshFirstApplyIndex)
  assert.ok(refreshProfilesRequestIndex < refreshProfilesGateIndex)
  assert.ok(refreshProfilesGateIndex < refreshProfilesApplyIndex)
  assert.ok(refreshCatchIndex < refreshCatchGateIndex)
  assert.ok(refreshCatchGateIndex < refreshCatchWriteIndex)

  const attestBlock = sourceBlock(
    'const attestSavedWorkflowModels =',
    'const hardSavedWorkflowBlockers =',
    'saved workflow attestation submission',
  )
  const attestGuardIndex = attestBlock.indexOf('const operationStillCurrent = savedWorkflowOperationGuard(item, draft)')
  const attestRequestIndex = attestBlock.indexOf('await platformApi.attestComfySavedWorkflowModelArtifacts')
  const attestResponseGateIndex = attestBlock.indexOf('if (!operationStillCurrent() || draft.modelProfileVersionId !== profile.id) return', attestRequestIndex)
  const attestApplyIndex = attestBlock.indexOf('draft.attestation = attempt', attestResponseGateIndex)
  const attestCatchIndex = attestBlock.indexOf('} catch (cause)')
  const attestCatchGateIndex = attestBlock.indexOf('if (operationStillCurrent()) draft.previewError =', attestCatchIndex)
  const attestBusyOwnerIndex = attestBlock.indexOf("if (savedWorkflowBusy.value === busyKey) savedWorkflowBusy.value = ''")
  for (const [label, index] of [
    ['attestation submission operation guard', attestGuardIndex],
    ['attestation submission request', attestRequestIndex],
    ['attestation submission response gate', attestResponseGateIndex],
    ['attestation submission apply', attestApplyIndex],
    ['attestation submission catch', attestCatchIndex],
    ['attestation submission catch gate', attestCatchGateIndex],
    ['attestation submission busy ownership gate', attestBusyOwnerIndex],
  ] as const) assert.ok(index >= 0, `saved workflow attestation submission misses ${label}`)
  assert.ok(attestGuardIndex < attestRequestIndex)
  assert.ok(attestRequestIndex < attestResponseGateIndex)
  assert.ok(attestResponseGateIndex < attestApplyIndex)
  assert.ok(attestCatchIndex < attestCatchGateIndex)
  assert.ok(attestCatchGateIndex < attestBusyOwnerIndex)
  const persistBlock = sourceBlock(
    'const persistSavedWorkflowDraftSession =',
    'const outputCandidatesFor =',
    'saved workflow session persistence',
  )
  const preservedSessionIndex = persistBlock.indexOf('if (preserved)')
  const persistReadOnlyGateIndex = persistBlock.indexOf('savedWorkflowIsContextReadOnly(item)')
  const persistAssignmentIndex = persistBlock.indexOf('items[item.sourcePath] = {')
  assert.ok(preservedSessionIndex >= 0)
  assert.ok(persistReadOnlyGateIndex >= 0)
  assert.ok(persistAssignmentIndex >= 0)
  assert.ok(preservedSessionIndex < persistReadOnlyGateIndex, 'an existing global draft must be preserved before read-only rows are skipped')
  assert.ok(persistReadOnlyGateIndex < persistAssignmentIndex, 'a read-only row must not create or rewrite a browser draft')
  const scanBlock = sourceBlock(
    'const scanSavedWorkflows =',
    'const previewSavedWorkflow =',
    'saved workflow scan',
  )
  for (const marker of [
    'if (loading.value || !workflowModelContextCanImport.value || savedWorkflowBusy.value) return',
    'const scanGeneration = ++savedWorkflowScanGeneration',
    'const loadGenerationSnapshot = settingsLoadGeneration',
    'const contextSnapshot = JSON.stringify(route.query.model_profile_version ?? null)',
    'const scanStillCurrent = () =>',
    '&& !loading.value',
    '&& workflowModelContextCanImport.value',
    'await restoreSavedWorkflowAttestationsAfterScan(scanStillCurrent)',
    'const ownsBusyState = scanGeneration === savedWorkflowScanGeneration',
    'hydrationCompleted && currentAtCompletion',
  ]) assert.ok(scanBlock.includes(marker), `saved workflow scan misses stale-response guard ${marker}`)
  const scanGenerationIndex = scanBlock.indexOf('const scanGeneration = ++savedWorkflowScanGeneration')
  const scanCurrentnessIndex = scanBlock.indexOf('const scanStillCurrent = () =>')
  const scanResponseIndex = scanBlock.indexOf('const scan = await platformApi.scanComfySavedWorkflows()')
  const scanResponseGateIndex = scanBlock.indexOf('if (!scanStillCurrent()) return', scanResponseIndex)
  const scanFirstCommitIndex = scanBlock.indexOf('savedWorkflowItems.value = scan.items', scanResponseIndex)
  assert.ok(scanGenerationIndex >= 0)
  assert.ok(scanCurrentnessIndex > scanGenerationIndex, 'the scan token must be captured before the currentness predicate')
  assert.ok(scanResponseIndex > scanCurrentnessIndex, 'the currentness predicate must be frozen before the scan request starts')
  assert.ok(scanResponseIndex >= 0)
  assert.ok(scanResponseGateIndex > scanResponseIndex, 'scan response must be checked after the request resolves')
  assert.ok(scanFirstCommitIndex > scanResponseGateIndex, 'stale scan response must be rejected before any result commit')
  const scanRestoreIndex = scanBlock.indexOf('await restoreSavedWorkflowAttestationsAfterScan(scanStillCurrent)')
  const scanPostRestoreGateIndex = scanBlock.indexOf('if (!scanStillCurrent()) return', scanRestoreIndex)
  const scanNoticeIndex = scanBlock.indexOf('notice.value =', scanPostRestoreGateIndex)
  assert.ok(scanRestoreIndex >= 0)
  assert.ok(scanPostRestoreGateIndex > scanRestoreIndex)
  assert.ok(scanNoticeIndex > scanPostRestoreGateIndex, 'stale restoration must be rejected before success state is shown')
  const scanCatchIndex = scanBlock.indexOf('} catch (cause)')
  const scanCatchGateIndex = scanBlock.indexOf('if (!scanStillCurrent()) return', scanCatchIndex)
  const scanFailureWriteIndex = scanBlock.indexOf("savedWorkflowScanState.value = 'failed'", scanCatchIndex)
  assert.ok(scanCatchIndex >= 0)
  assert.ok(scanCatchGateIndex > scanCatchIndex)
  assert.ok(scanFailureWriteIndex > scanCatchGateIndex, 'a stale failure must not overwrite the new context')
  const scanFinallyIndex = scanBlock.indexOf('finally {')
  const scanCompletionCurrentIndex = scanBlock.indexOf('const currentAtCompletion = scanStillCurrent()', scanFinallyIndex)
  const scanBusyOwnerIndex = scanBlock.indexOf('const ownsBusyState = scanGeneration === savedWorkflowScanGeneration', scanCompletionCurrentIndex)
  const scanStaleResetIndex = scanBlock.indexOf('if (ownsBusyState && !currentAtCompletion) resetSavedWorkflowScanView()', scanBusyOwnerIndex)
  const scanBusyClearIndex = scanBlock.indexOf("if (ownsBusyState) savedWorkflowBusy.value = ''", scanStaleResetIndex)
  const scanPersistIndex = scanBlock.indexOf('if (hydrationCompleted && currentAtCompletion)', scanBusyClearIndex)
  for (const [label, index] of [
    ['scan finally', scanFinallyIndex],
    ['scan completion currentness check', scanCompletionCurrentIndex],
    ['scan busy ownership check', scanBusyOwnerIndex],
    ['stale scan reset', scanStaleResetIndex],
    ['owned scan busy clear', scanBusyClearIndex],
    ['current hydrated session persistence', scanPersistIndex],
  ] as const) assert.ok(index >= 0, `saved workflow scan misses ${label}`)
  assert.ok(scanFinallyIndex < scanCompletionCurrentIndex)
  assert.ok(scanCompletionCurrentIndex < scanBusyOwnerIndex)
  assert.ok(scanBusyOwnerIndex < scanStaleResetIndex)
  assert.ok(scanStaleResetIndex < scanBusyClearIndex)
  assert.ok(scanBusyClearIndex < scanPersistIndex)
  assert.ok(settings.includes(':disabled="loading||Boolean(savedWorkflowBusy)||!savedWorkflowDirectory"'))
  assert.ok(settings.includes('v-if="savedWorkflowItems.length" id="saved-workflow-scan-results"'))
  const routeWatcher = sourceBlock(
    "watch([() => route.query.section, () => route.query.model_profile_version]",
    'watch(tab,',
    'workflow route watcher',
  )
  assert.ok(routeWatcher.includes('void load()'))
  assert.equal(routeWatcher.includes('scanSavedWorkflows'), false)
  assert.equal(routeWatcher.includes('previewSavedWorkflow'), false)
  assert.equal(routeWatcher.includes('batchImportSavedWorkflows'), false)
})

test('saved Comfy workflow adapters keep review hashes and never require raw canvas values', async () => {
  const vite = await server()
  try {
    const adapters = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
    const scan = adapters.adaptComfySavedWorkflowScan({
      schema_version: 1,
      status: 'complete',
      source_count: 1,
      succeeded_source_count: 1,
      failed_source_count: 0,
      matched_count: 1,
      review_count: 0,
      effective_limits: { batch_max_items: 20 },
      sources: [{
        source_scope_schema_version: 1,
        source_scope_sha256: 'b'.repeat(64),
        source_deployment_id: 'deployment-1',
        provider_id: 'provider-1',
        runtime_host_id: 'host-1',
        source_label: 'Local ComfyUI',
        deployment_count: 2,
        status: 'succeeded',
        item_count: 1,
        error_code: '',
        error_message: '',
      }],
      items: [{
        source_scope_schema_version: 1, source_scope_sha256: 'b'.repeat(64),
        source_key: `scope-1:image/example.json:${'a'.repeat(64)}`,
        source_deployment_id: 'deployment-1', source_provider_id: 'provider-1', source_runtime_host_id: 'host-1',
        source_path: 'image/example.json', source_sha256: 'a'.repeat(64), size_bytes: 1200,
        source_workflow_id: 'canvas-id', suggested_import_kind: 'template_version',
        candidate_managed: true, candidate_source_id: 'candidate-1', imported: true,
        source_id: 'source-1', workflow_version_id: 'version-1', template_id: 'template-1',
        contract_upgrade_required: true, error_code: '', error_message: '',
        analysis_status: 'complete', analysis_code: '', analysis_message: '',
        admission_tier: 'generic_fixed', actionability: 'ready_for_executable_import', recovery_actions: [],
        match_status: 'matched', match_reason_codes: ['exact_source_revision'], match_actions: [],
        output_candidates: [
          { canvas_node_id: '123', node_type: 'SaveImage', suggested_output_kind: 'image' },
          { canvas_node_id: '124', node_type: 'CustomSave', suggested_output_kind: '' },
        ],
        model_profile_candidates: [{
          model_profile_version_id: 'profile-1', profile_key: 'image-profile', version: 1,
          status: 'published', modality: 'image', target_deployment_id: 'deployment-1',
          eligible: true, live_validate_ready: true, eligible_output_node_ids: ['123'],
          rejection_codes: [], live_validate_rejection_codes: [],
        }],
        matched_model_profile_version_id: 'profile-1', matched_target_deployment_id: 'deployment-1',
        matched_primary_output_node_id: '123', matched_output_kind: 'image',
        suggested_template: { use_existing: true, template_id: 'template-1', name: 'Image', slug: 'image', output_kind: 'image' },
        preview: null,
      }],
    })
    assert.equal(scan.items[0].templateId, 'template-1')
    assert.equal(scan.items[0].contractUpgradeRequired, true)
    assert.equal(scan.items[0].sourceDeploymentId, 'deployment-1')
    assert.equal(scan.items[0].matchStatus, 'matched')
    assert.equal(scan.items[0].outputCandidates[1].suggestedOutputKind, '', 'an unresolved Projector keeps the selectable output candidate')
    assert.deepEqual(scan.items[0].modelProfileCandidates[0].eligibleOutputNodeIds, ['123'])
    assert.equal(scan.sources[0].deploymentCount, 2)
    const preview = adapters.adaptComfySavedWorkflowPreview({
      schema_version: 1,
      source_path: 'image/example.json',
      source_sha256: 'a'.repeat(64),
      size_bytes: 1200,
      source_workflow_id: 'canvas-id',
      primary_output_node_ids: ['123'],
      primary_api_node_ids: ['11'],
      output_candidates: [{ canvas_node_id: '123', node_type: 'SaveImage', suggested_output_kind: 'image' }],
      model_dependencies: [{
        node_id: '1', node_type: 'UNETLoader', input_name: 'unet_name',
        filename: 'qwen/model.safetensors', role: 'diffusion_model', suggested_alias: 'diffusion_model_1',
        inventory_folder: 'diffusion_models', inventory_node_type: '', inventory_input_name: '',
        available_in_object_info: true,
        artifact_matches: [{ id: 'artifact-1', status: 'available', sha256: 'b'.repeat(64), size_bytes: 42, verified_snapshot: 'snapshot-1' }],
      }],
      asset_dependencies: [{ node_id: '2', node_type: 'LoadImage', input_name: 'image', media_kind: 'image', value_type: 'asset_image', suggested_semantic_key: 'asset.image.source', input_slot: 1, asset_category: 'image', is_required: false, min_items: 0, max_items: 1 }],
      conversion_report: { schema_version: 1 },
      object_info_schema_sha256: 'c'.repeat(64),
      converted_graph_sha256: 'd'.repeat(64),
      configurable_parameters: [{
        semantic_key: 'image.megapixels', source_value: 0.4, recommended_safe_value: 0.4,
        default: 1.0, min: 0.1, max: 0.4, schema_min: 0.1, schema_max: 4.0,
        multiple_of: null, alignment_modulus: null, alignment_remainder: null,
        options: [0.1, 0.2, 0.4], type: 'number', selection_group: null,
        target: { node_id: '3', node_type: 'ResolutionSelector', input_name: 'megapixels' },
      }, {
        semantic_key: 'video.frames', source_value: 56, recommended_safe_value: 56,
        default: 56, min: 22, max: 3600, schema_min: 5, schema_max: 3600,
        multiple_of: null, alignment_modulus: 17, alignment_remainder: 5,
        options: [], type: 'integer', selection_group: null,
        target: { node_id: '4', node_type: 'MiniMaxH3AudioConditioningT8', input_name: 'length' },
      }],
      parameter_schema_sha256: 'e'.repeat(64),
      structural_contract_sha256: 'f'.repeat(64),
      suggested_input_contract: { compiler: { id: 'generic.comfy', version: 1 } },
      suggested_semantic_bindings: [{ semantic_key: 'asset.image.source', node_id: '2', input_name: 'image', value_type: 'asset_image', is_required: true }],
      suggested_model_requirements: [{ alias: 'diffusion_model_1', artifact_id: 'artifact-1', expected_sha256: 'b'.repeat(64) }],
      blockers: [],
      live_validate_requirements: [], admission_tier: 'generic_fixed',
      actionability: 'ready_for_executable_import', recovery_actions: [],
      contract_draft_id: 'contract-draft-1', contract_draft_revision: 1,
    })
    assert.deepEqual(preview.primaryOutputNodeIds, ['123'])
    assert.deepEqual(preview.primaryApiNodeIds, ['11'])
    assert.equal(preview.objectInfoSchemaSha256, 'c'.repeat(64))
    assert.equal(preview.convertedGraphSha256, 'd'.repeat(64))
    assert.equal(preview.parameterSchemaSha256, 'e'.repeat(64))
    assert.equal(preview.structuralContractSha256, 'f'.repeat(64))
    assert.deepEqual(preview.configurableParameters[0], {
      semanticKey: 'image.megapixels', sourceValue: 0.4, recommendedSafeValue: 0.4,
      defaultValue: 1, min: 0.1, max: 0.4, schemaMin: 0.1, schemaMax: 4,
      multipleOf: null, alignmentModulus: null, alignmentRemainder: null,
      options: [0.1, 0.2, 0.4], type: 'number', selectionGroup: '',
      target: { nodeId: '3', nodeType: 'ResolutionSelector', inputName: 'megapixels' },
    })
    assert.equal(preview.configurableParameters[1].alignmentModulus, 17)
    assert.equal(preview.configurableParameters[1].alignmentRemainder, 5)
    assert.equal(preview.modelDependencies[0].artifactMatches[0].verifiedSnapshot, 'snapshot-1')
    assert.equal(preview.assetDependencies[0].suggestedSemanticKey, 'asset.image.source')
    assert.equal(preview.assetDependencies[0].inputSlot, 1)
    assert.equal(preview.assetDependencies[0].assetCategory, 'image')
    assert.equal(preview.assetDependencies[0].minItems, 0)
    assert.equal(preview.assetDependencies[0].maxItems, 1)
    assert.equal(JSON.stringify(preview).includes('prompt literal'), false)

    const attempt = adapters.adaptRuntimeHostProbeAttempt({
      id: 'attempt-1', host: 'host-1', probe_kind: 'draft_model_artifact_attestation',
      request_snapshot: { source_sha256: 'e'.repeat(64) }, request_snapshot_schema_version: 1,
      request_snapshot_size_bytes: 128, request_snapshot_sha256: 'f'.repeat(64),
      status: 'queued', created_at: '', updated_at: '',
    })
    assert.equal(attempt.probeKind, 'draft_model_artifact_attestation')
    assert.equal(attempt.requestSnapshotSha256, 'f'.repeat(64))

    const settings = readFileSync(join(frontendRoot, 'src', 'views', 'SettingsView.vue'), 'utf8')
    for (const marker of [
      "probe_kind: 'draft_model_artifact_attestation'",
      'restoreSavedWorkflowAttestationsAfterScan',
      'refreshSavedWorkflowAttestation(item)',
      'attestationMatchesSavedWorkflowReview(row.requestSnapshot, review)',
      'savedWorkflowHydrationGuard.run(async () =>',
      'SAVED_WORKFLOW_STORAGE_ERROR',
      'item.contractUpgradeRequired',
      'expectedStructuralContractSha256',
      'savedWorkflowKey(item)',
      '扫描已配置的 ComfyUI 工作流目录',
      '模板目标（自动）',
      'groupSavedWorkflowBatches',
      'sourceDeploymentId: item.sourceDeploymentId',
      '具体参数值与 Asset 不属于版本合同',
    ]) assert.ok(settings.includes(marker), `saved workflow attestation recovery misses ${marker}`)
    assert.equal(settings.includes('来源部署（Deployment）'), false)
  } finally { await vite.close() }
})

test('workflow smoke candidate and attempt adapters preserve server-issued fixture facts', async () => {
  const vite = await server()
  try {
    const adapters = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
    const asset = adapters.adaptWorkflowSmokeAssetCandidate({
      id: 'asset-1', name: 'Managed image',
      mime_type: 'image/png', checksum: 'a'.repeat(64),
      smoke_eligible: false, smoke_ineligible_reason: 'checksum_mismatch', metadata: {},
    })
    assert.equal(asset.smokeEligible, false)
    assert.equal(asset.smokeIneligibleReason, 'checksum_mismatch')
    assert.equal(JSON.stringify(asset).includes('hidden'), false)
    const attempt = adapters.adaptWorkflowSmokeAttempt({
      id: 'attempt-1', workflow_version: 'version-1', deployment: 'deployment-1', status: 'uncertain',
      fixture_values_schema_version: 2,
      fixture_values_size_bytes: 19, fixture_values_sha256: 'b'.repeat(64), fixture_sha256: 'c'.repeat(64),
      asset_bindings: [{ semantic_key: 'asset.image.source', input_slot: 1, asset: 'asset-1', media_kind: 'image', category: 'image', checksum: 'a'.repeat(64), mime_type: 'image/png', size_bytes: 42, created_at: '' }],
      resolution_required: true,
      resolutions: [{
        id: 'resolution-1', workflow_smoke_attempt_id: 'attempt-1', revision: 1,
        outcome: 'confirmed_remote_failed', provider_request_id_sha256: 'd'.repeat(64),
        evidence: {}, evidence_schema_version: 1, evidence_size_bytes: 80,
        evidence_sha256: 'e'.repeat(64), note: '已核对远端控制台终态', decided_by: 'staff-1',
        created_at: '', updated_at: '',
      }],
      result: {}, created_at: '', updated_at: '',
    })
    assert.equal(JSON.stringify(attempt).includes('sampling.seed'), false)
    assert.equal('fixtureValues' in attempt, false)
    assert.equal(attempt.fixtureSha256, 'c'.repeat(64))
    assert.equal(attempt.assetBindings[0].asset, 'asset-1')
    assert.equal(attempt.resolutionRequired, true)
    assert.equal(attempt.resolutions[0].outcome, 'confirmed_remote_failed')
    assert.equal(attempt.resolutions[0].providerRequestIdSha256, 'd'.repeat(64))
    assert.equal('providerRequestId' in attempt.resolutions[0], false)

    const editor = readFileSync(join(frontendRoot, 'src', 'views', 'WorkflowVersionEditorView.vue'), 'utf8')
    for (const marker of [
      'attempts.find((item) => item.resolutionRequired)',
      'smokeResolutionRequired.value',
      'platformApi.resolveWorkflowSmokeAttempt',
      'confirmed_not_submitted',
      'confirmed_remote_failed',
      'confirmed_remote_succeeded',
      'type="password"',
      '确认远端成功也不等于 smoke passed',
      'await restoreSmokeAttempt()',
      'auth.user?.isStaff',
    ]) assert.ok(editor.includes(marker), `workflow smoke uncertainty UI misses ${marker}`)
  } finally { await vite.close() }
})

test('system config change set submits only dirty keys and explicit inheritance restores', async () => {
  const vite = await server()
  try {
    const registry = await vite.ssrLoadModule('/src/services/settingsRegistry.ts')
    const definition = (key: string, overrides = {}) => ({
      key,
      valueType: 'integer',
      valueSchema: {},
      isActive: true,
      isSecret: false,
      ...overrides,
    })
    const result = registry.buildSystemConfigChangeSet({
      definitions: [
        definition('production.changed'),
        definition('production.inherited'),
        definition('security.secret', { isSecret: true }),
        definition('production.untouched'),
      ],
      draftText: {
        'production.changed': '42',
        'production.inherited': '99',
        'production.untouched': '777',
      },
      dirtyKeys: new Set(['production.changed', 'security.secret']),
      unsetKeys: new Set(['production.inherited']),
      secretSelections: { 'security.secret': 'binding-1' },
      isSuperuser: true,
    })
    assert.deepEqual(result, {
      values: {
        'production.changed': 42,
        'security.secret': { credential_id: 'binding-1' },
      },
      unsetKeys: ['production.inherited'],
    })
    assert.equal('production.untouched' in result.values, false)
    assert.equal('production.inherited' in result.values, false)
  } finally { await vite.close() }
})

test('config schema validation matches typed controls and blocks invalid dirty values', async () => {
  const vite = await server()
  try {
    const schema = await vite.ssrLoadModule('/src/services/configValueSchema.ts')
    const registry = await vite.ssrLoadModule('/src/services/settingsRegistry.ts')
    const definition = (key: string, valueType: string, valueSchema = {}) => ({ key, valueType, valueSchema, isActive: true, isSecret: false })
    assert.equal(schema.configValueEditorKind(definition('enabled', 'boolean')), 'boolean')
    assert.equal(schema.configValueEditorKind(definition('mode', 'string', { enum: ['safe', 'fast'] })), 'enum')
    assert.equal(schema.configValueEditorKind(definition('normalizer', 'string', {
      resource_ref: { resource: 'model_deployment', capability: 'prompt.normalize', allow_empty: true, require_ready: true },
    })), 'resource')
    assert.match(schema.configValueConstraintSummary(definition('normalizer', 'string', {
      resource_ref: { resource: 'model_deployment', capability: 'prompt.normalize' },
    })), /依赖 model_deployment · prompt\.normalize/)
    assert.equal(schema.parseConfigValue(definition('count', 'integer', { minimum: 1, maximum: 3 }), '4').error, 'count 不能大于 3')
    assert.match(schema.parseConfigValue(definition('name', 'string', { pattern: '[a-z]+' }), '"中文"').error, /格式约束/)
    assert.match(schema.parseConfigValue(definition('workflow.path', 'string', {
      format: 'comfyui-saved-workflow-directory',
    }), '"../outside"').error, /只能是 workflows/)
    assert.equal(schema.parseConfigValue(definition('workflow.path', 'string', {
      format: 'comfyui-saved-workflow-directory',
    }), '"workflows/team-a"').error, '')
    assert.match(schema.parseConfigValue(definition('items', 'array', { maxItems: 1 }), '[1,2]').error, /最多允许 1 项/)
    assert.throws(() => registry.buildSystemConfigChangeSet({
      definitions: [definition('count', 'integer', { minimum: 1 })], draftText: { count: '0' },
      dirtyKeys: ['count'], unsetKeys: [], secretSelections: {}, isSuperuser: true,
    }), /不能小于 1/)
  } finally { await vite.close() }
})

test('resource-backed configuration uses server options instead of UUID text entry', () => {
  const settings = readFileSync(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
  const editor = readFileSync(new URL('../src/components/ConfigValueEditor.vue', import.meta.url), 'utf8')
  const api = readFileSync(new URL('../src/services/platformApi.ts', import.meta.url), 'utf8')
  assert.ok(settings.includes(':resource-options="configResourceOptions[item.key]||[]"'))
  assert.ok(settings.includes('platformApi.listConfigResourceOptions(item.id)'))
  assert.ok(editor.includes("kind==='resource'"))
  assert.ok(editor.includes('暂无满足条件的资源'))
  assert.ok(api.includes('/resource-options/'))
})

test('saved workflow page exposes the configured userdata directory and budget navigation', () => {
  const settings = readFileSync(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
  for (const marker of [
    'workflow.comfyui_saved_workflow_directory',
    'security.comfy.import.scan_max_seconds',
    'userdata/{{ savedWorkflowDirectory',
    '配置扫描目录',
    '复核扫描预算',
    "captureWorkflow('resolved', platformApi.resolveConfiguration()",
  ]) assert.ok(settings.includes(marker), marker)
  assert.ok(settings.includes('Boolean(savedWorkflowBusy)||!savedWorkflowDirectory'))
})

test('settings safety category fails closed and uses catalog permissions with searchable active records', () => {
  const settings = readFileSync(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
  const prompts = readFileSync(new URL('../src/components/PromptProfileSettings.vue', import.meta.url), 'utf8')
  const filterComponent = readFileSync(new URL('../src/components/SettingsListFilter.vue', import.meta.url), 'utf8')
  const catalog = readFileSync(new URL('../src/content/settingsSectionCatalog.ts', import.meta.url), 'utf8')
  const adapters = readFileSync(new URL('../src/services/platformAdapters.ts', import.meta.url), 'utf8')
  const styles = readFileSync(new URL('../src/platform.css', import.meta.url), 'utf8')
  assert.ok(catalog.includes("id: 'security'"))
  assert.ok(catalog.includes('writeRequiresSuperuser: true'))
  for (const marker of [
    'currentSectionWriteRequiresSuperuser', 'canWriteCurrentSection', 'unknownConfigurationDefinitions',
    'configurationClassRaw', 'settingsListSearch', 'showRetiredSettingsRecords', 'SettingsListFilter',
    'revisionsForCurrentSection', 'changesSecurityBoundaries', 'unknownConfigRevisionClassifications', 'canWriteConfigRevision',
    "item.configurationClass === currentConfigurationClass.value",
  ]) assert.ok(settings.includes(marker), `SettingsView safety convergence misses ${marker}`)
  for (const marker of ['SettingsListFilter', 'filterSettingsList', 'filteredFields', 'filteredProfiles', 'filteredBindings']) {
    assert.ok(prompts.includes(marker), `PromptProfile settings misses shared filtering marker ${marker}`)
  }
  assert.ok(filterComponent.includes('显示已退役/停用记录（Include retired/inactive）'))
  for (const stale of ['configSearch', 'showInactiveConfigDefinitions', 'fieldSearch', 'profileSearch']) {
    assert.equal(`${settings}\n${prompts}`.includes(stale), false, `stale local filter ${stale} must be removed`)
  }
  for (const unfilteredLoop of [
    'item in providers', 'item in modelProfiles', 'item in deployments', 'item in modelArtifacts',
    'item in workflowVersions', 'item in recipes', 'host in runtimeHosts', 'item in secretBindings',
  ]) assert.equal(settings.includes(unfilteredLoop), false, `settings list still bypasses the shared filter: ${unfilteredLoop}`)
  assert.equal(adapters.includes("string(raw.configuration_class) || 'standard'"), false)
  assert.ok(adapters.includes("configurationClassRaw === 'standard' || configurationClassRaw === 'security_boundary'"))
  assert.ok(styles.includes('grid-template-columns:repeat(9,minmax(0,1fr))'))
  assert.ok(styles.includes('@media(max-width:1280px){.settings-center-nav{grid-template-columns:repeat(3,1fr)}'))
})

test('shared settings list filter hides retired records by default and searches visible facts', async () => {
  const vite = await server()
  try {
    const { filterSettingsList } = await vite.ssrLoadModule('/src/services/settingsListFilter.ts')
    const source = [
      { name: 'Alpha Provider', status: 'active' },
      { name: 'Alpha Legacy', status: 'retired' },
      { name: 'Beta Disabled', status: 'disabled' },
    ]
    const options = {
      search: 'alpha', showRetired: false,
      isRetired: (item: { status: string }) => ['retired', 'disabled'].includes(item.status),
      searchableValues: (item: { name: string; status: string }) => [item.name, item.status],
    }
    assert.deepEqual(filterSettingsList(source, options), {
      items: [source[0]], totalCount: 3, hiddenRetiredCount: 2,
    })
    assert.deepEqual(filterSettingsList(source, { ...options, showRetired: true }).items, [source[0], source[1]])
    assert.deepEqual(filterSettingsList(source, { ...options, search: 'DISABLED', showRetired: true }).items, [source[2]])
  } finally { await vite.close() }
})

test('model registry groups versions, files and runtime instances without merging lifecycle or evidence', async () => {
  const settings = readFileSync(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
  const api = readFileSync(new URL('../src/services/platformApi.ts', import.meta.url), 'utf8')
  const types = readFileSync(new URL('../src/services/platformTypes.ts', import.meta.url), 'utf8')
  for (const marker of [
    "'deployment-status'", "'retire-deployment'", 'retireDeployment', '删除复核', '退役保留历史',
    'buildModelRegistryFamilies', 'modelRegistryFamilies', '管理服务连接', '新建模型',
    '新建下一版本', '新增文件声明', '新增运行实例', '实例状态', ':acceptance-loading="loading"',
    'ModelDeploymentExecutionSummary', '工作流验收', 'deploymentAcceptanceRows',
    'sectionErrors.deploymentAcceptance', 'listAcceptanceSmokeDeploymentRows',
    'latestModelProfileForFamily', 'createNextModelProfile', 'model-registry-workbench',
    'model-family-browser', 'model-family-inspector', 'model-version-tabs',
    'model-section-tabs', 'model-version-detail', '.model-instance-list article>.model-instance-actions',
    'selectedModelFamilyKey', 'selectedModelProfileId', 'selectedModelSection',
    'selectedModelFamily', 'selectedModelVersionGroup', 'selectModelFamily', 'selectModelVersion',
    'modelCapabilitySearchHit', 'modelArtifactSearchHit', 'modelDeploymentSearchHit',
    'role="tablist"', 'role="tab"', ':aria-selected=', 'role="tabpanel"', ':aria-controls=',
    'aria-haspopup="dialog"', '<BaseModal :open="providerManagementOpen"',
    'height:clamp(540px,calc(100vh - 330px),760px)', 'overflow:auto',
    '.model-family-browser-status em.published', '.model-family-browser-status em.error',
    '@media(max-width:1200px)', '@media(max-width:980px)', '@media(max-width:720px)',
    'item.latestSmoke?.unresolvedSubmission?.status', 'modelFamilyNextVersionTitle',
    'providers.value = []', 'modelProfiles.value = []', 'deployments.value = []',
    'modelArtifacts.value = []', 'runtimeHosts.value = []',
    'inventory_folder: existing.inventoryFolder', '声明相对路径', 'artifactLatestAvailabilityLabel',
    "profileFor(item.profileVersion)?.status === 'retired'",
  ]) assert.ok(settings.includes(marker), `SettingsView model registry misses ${marker}`)
  assert.equal(settings.includes('version: source.version + 1'), false)
  for (const retiredMarker of [
    'expandedModelFamilyKeys', 'expandedModelSectionKeys', 'modelFamilyExpanded',
    'modelSectionExpanded', 'trackModelFamilyExpanded', 'trackModelSectionExpanded',
    'class="platform-card model-family-card"', 'class="model-version-section"',
  ]) assert.equal(settings.includes(retiredMarker), false, `retired model disclosure remains: ${retiredMarker}`)
  assert.equal(/model-version-tabs[^]*tabindex="[^"]*\?-?1/.test(settings), false, 'version choices must remain keyboard reachable')
  assert.ok((settings.match(/role="tab"/g) || []).length >= 3)
  assert.ok((settings.match(/role="tabpanel"/g) || []).length >= 3)
  assert.match(settings, /\.model-registry-workbench\{[^}]*height:[^}]*overflow:hidden/)
  assert.match(settings, /\.model-family-browser-list\{[^}]*overflow:auto/)
  assert.match(settings, /\.model-family-browser-identity strong\{[^}]*color:var\(--text\)/)
  assert.match(settings, /\.model-family-browser-identity small\{[^}]*color:var\(--text-secondary\)/)
  const activeFamilyRule = settings.match(/\.model-family-browser-list>button\.active\{[^}]*\}/)?.[0] || ''
  assert.match(activeFamilyRule, /background:/)
  assert.match(activeFamilyRule, /border|box-shadow/)
  assert.equal(settings.includes("@click=\"openEditor('artifact')\"><Plus"), false)
  assert.equal(settings.includes("@click=\"openEditor('deployment')\"><Plus"), false)
  assert.ok(api.includes('/model-deployments/${id}/retire/'))
  assert.ok(api.includes('/model-profile-versions/${sourceId}/draft-next/'))
  assert.ok(api.includes('/model-profile-versions/options/'))
  assert.ok(api.includes("fetchAllPages<unknown>('/acceptance-smoke/'"))
  assert.ok(api.includes("row.subject !== 'deployment'"))
  assert.ok(api.includes('模型实例验收响应包含重复 Deployment'))
  assert.ok(types.includes('ModelProfileRegistryOptions'))
  assert.ok(types.includes("'active' | 'disabled' | 'error' | 'retired'"))

  const vite = await server()
  try {
    const {
      buildModelRegistryFamilies,
      modelArtifactAvailabilityEvidence,
      modelDeploymentWorkflowSmokeUrl,
    } = await vite.ssrLoadModule('/src/services/modelRegistryPresentation.ts')
    const { deploymentWorkflowSignal } = await vite.ssrLoadModule('/src/services/modelDeploymentExecution.ts')
    const provider = { id: 'provider-1', key: 'provider-alpha', name: 'Provider Alpha', driverType: 'async_rest' }
    const publishedProfile = {
      id: 'profile-1', profileKey: 'family-alpha', version: 2, provider: 'provider-1',
      modelIdentifier: 'model-alpha-v2', modality: 'image', status: 'published', specification: { capabilities: ['image.text_to_image'] },
    }
    const retiredProfile = { ...publishedProfile, id: 'profile-retired', version: 1, status: 'retired' }
    const groupedArtifact = {
      id: 'artifact-grouped', profileVersion: 'profile-1', logicalName: 'weights', filename: 'alpha.safetensors',
      storageUri: '', inventoryFolder: 'checkpoints', role: 'checkpoint', status: 'available',
    }
    const groupedDeployment = {
      id: 'deployment-grouped', profileVersion: 'profile-1', key: 'alpha-runtime', name: 'Runtime Alpha',
      status: 'active', providerDriver: 'async_rest', runtimeHost: '', latestSmoke: { status: 'passed', fresh: true },
    }
    const retiredDeployment = { ...groupedDeployment, id: 'deployment-retired', profileVersion: 'profile-retired', status: 'retired' }
    const disabledDeployment = { ...groupedDeployment, id: 'deployment-disabled', status: 'disabled' }
    const artifactSearch = buildModelRegistryFamilies(
      [publishedProfile, retiredProfile], [groupedArtifact], [groupedDeployment, retiredDeployment], [provider],
      { search: 'alpha.safetensors', showRetired: false },
    )
    assert.equal(artifactSearch.length, 1)
    assert.equal(artifactSearch[0].versions[0].profile.id, 'profile-1')
    assert.equal(artifactSearch[0].versions[0].artifacts[0].id, 'artifact-grouped')
    const deploymentSearch = buildModelRegistryFamilies(
      [publishedProfile, retiredProfile], [groupedArtifact], [groupedDeployment, retiredDeployment], [provider],
      { search: 'runtime alpha', showRetired: false },
    )
    assert.equal(deploymentSearch[0].versions[0].deployments[0].id, 'deployment-grouped')
    const withoutDisabledHistory = buildModelRegistryFamilies(
      [publishedProfile], [groupedArtifact], [disabledDeployment], [provider],
      { showRetired: false },
    )
    assert.equal(withoutDisabledHistory[0].versions[0].deployments.length, 0)
    const withHistory = buildModelRegistryFamilies(
      [publishedProfile, retiredProfile], [groupedArtifact], [groupedDeployment, retiredDeployment], [provider],
      { showRetired: true },
    )
    assert.deepEqual(withHistory[0].versions.map((item: { profile: { version: number } }) => item.profile.version), [2, 1])
    assert.equal(withHistory[0].versions[1].deployments[0].id, 'deployment-retired')

    const deploymentAcceptance = {
      subject: 'deployment',
      deployment: { id: 'deployment-grouped', targetState: 'current', profileContractState: 'current' },
      workflowAssociationSummary: {
        profileMatchCount: 1,
        compatibleOnlyCount: 0,
        validatedCount: 1,
        smokeHistoryCount: 2,
        freshValidationCount: 1,
        driftedValidationCount: 0,
        freshSmokeCount: 1,
        unresolvedSmokeCount: 0,
      },
      actions: [{
        kind: 'view_workflow_associations',
        uiUrl: '/settings/acceptance-smoke?subject=workflow&deployment_id=deployment-grouped',
      }],
    }
    assert.equal(
      deploymentWorkflowSignal(deploymentAcceptance).label,
      '1 个工作流通过',
    )
    assert.equal(
      deploymentWorkflowSignal(null, '网络错误').label,
      '读取失败',
    )
    assert.equal(
      deploymentWorkflowSignal(null).label,
      '待读取',
    )
    assert.equal(
      modelDeploymentWorkflowSmokeUrl(deploymentAcceptance, 'deployment-grouped'),
      '/settings/acceptance-smoke?subject=workflow&deployment_id=deployment-grouped',
    )

    const sha256 = 'a'.repeat(64)
    const artifact = { id: 'artifact-1', profileVersion: 'profile-1', sizeBytes: 100, sha256 }
    const loopback = {
      id: 'deployment-1', profileVersion: 'profile-1', runtimeHost: 'host-1',
      runtimeConfig: { base_url: 'http://127.0.0.1:8188' },
    }
    const snapshot = {
      id: 'snapshot-1', host: 'host-1', capturedAt: '2026-08-30T00:00:00Z',
      expiresAt: '2026-08-31T00:00:00Z', probeStatus: 'succeeded',
      modelAvailability: [{
        artifact: 'artifact-1', deployment: 'deployment-1', status: 'available',
        observedSizeBytes: 100, observedSha256: sha256, observationSource: 'local_filesystem',
      }],
    }
    const eligible = modelArtifactAvailabilityEvidence(
      artifact, [snapshot], [loopback], Date.parse('2026-08-30T12:00:00Z'),
    )
    assert.equal(eligible.verificationCandidate?.snapshot.id, 'snapshot-1')
    assert.equal(eligible.latest?.exactContract, true)
    assert.equal(modelArtifactAvailabilityEvidence(
      artifact, [{ ...snapshot, expiresAt: '2026-08-30T11:59:59Z' }], [loopback], Date.parse('2026-08-30T12:00:00Z'),
    ).verificationCandidate, null)
    assert.equal(modelArtifactAvailabilityEvidence(
      artifact, [{ ...snapshot, modelAvailability: [{ ...snapshot.modelAvailability[0], observedSha256: 'b'.repeat(64) }] }], [loopback], Date.parse('2026-08-30T12:00:00Z'),
    ).verificationCandidate, null)
    assert.equal(modelArtifactAvailabilityEvidence(
      artifact, [{ ...snapshot, modelAvailability: [{ ...snapshot.modelAvailability[0], status: 'missing' }] }], [loopback], Date.parse('2026-08-30T12:00:00Z'),
    ).verificationCandidate, null)
    const olderAvailable = { ...snapshot, id: 'snapshot-old', capturedAt: '2026-08-29T23:00:00Z' }
    const newestMissing = {
      ...snapshot, id: 'snapshot-new', capturedAt: '2026-08-30T01:00:00Z',
      modelAvailability: [{ ...snapshot.modelAvailability[0], status: 'missing' }],
    }
    const noHistoricalFallback = modelArtifactAvailabilityEvidence(
      artifact, [olderAvailable, newestMissing], [loopback], Date.parse('2026-08-30T12:00:00Z'),
    )
    assert.equal(noHistoricalFallback.latest?.snapshot.id, 'snapshot-new')
    assert.equal(noHistoricalFallback.verificationCandidate, null)
    const secondHostDeployment = { ...loopback, id: 'deployment-2', runtimeHost: 'host-2' }
    const newerMissingOnSecondHost = {
      ...snapshot, id: 'snapshot-host-2', host: 'host-2', capturedAt: '2026-08-30T02:00:00Z',
      modelAvailability: [{ ...snapshot.modelAvailability[0], deployment: 'deployment-2', status: 'missing' }],
    }
    const perHostCandidate = modelArtifactAvailabilityEvidence(
      artifact, [snapshot, newerMissingOnSecondHost], [loopback, secondHostDeployment], Date.parse('2026-08-30T12:00:00Z'),
    )
    assert.equal(perHostCandidate.latest?.snapshot.id, 'snapshot-host-2')
    assert.equal(perHostCandidate.verificationCandidate?.snapshot.id, 'snapshot-1')
  } finally { await vite.close() }
})

test('registry resources use typed schemas, explicit defaults and prerequisite blockers', async () => {
  const vite = await server()
  try {
    const schema = await vite.ssrLoadModule('/src/services/registryResourceSchema.ts')
    const modelProfileOptions = {
      specificationSchemaVersion: 1,
      providerDrivers: [
        { value: 'comfyui', label: 'ComfyUI', modalities: ['text', 'image', 'video', 'audio'], operations: ['health', 'submit', 'poll', 'collect'] },
        { value: 'async_rest', label: '受限异步 REST', modalities: ['image', 'video', 'audio'], operations: ['health', 'submit', 'poll', 'cancel', 'collect'] },
        { value: 'deepseek_api', label: 'DeepSeek API', modalities: ['text'], operations: ['prompt.normalize', 'chat_completion'] },
      ],
      modalities: [
        { value: 'text', label: '文本（text）' }, { value: 'image', label: '图片（image）' },
        { value: 'video', label: '视频（video）' }, { value: 'audio', label: '音频（audio）' },
        { value: 'multimodal', label: '多模态（multimodal）' },
      ],
      artifactRoles: [{ value: 'model', label: '模型' }, { value: 'checkpoint', label: 'Checkpoint' }],
      capabilities: [
        { value: 'script.generate', label: '剧本生成', modalities: ['text', 'multimodal'], driverTypes: ['deepseek_api'], legacy: false },
        { value: 'script.structure', label: '剧本结构化', modalities: ['text', 'multimodal'], driverTypes: ['deepseek_api'], legacy: false },
        { value: 'prompt.normalize', label: '提示词规范化', modalities: ['text', 'multimodal'], driverTypes: ['deepseek_api'], legacy: false },
        { value: 'visual_rules.generate', label: '全剧影视规则生成', modalities: ['text', 'multimodal'], driverTypes: ['deepseek_api'], legacy: false },
        { value: 'image.text_to_image', label: '文生图', modalities: ['image', 'multimodal'], driverTypes: ['comfyui', 'async_rest'], legacy: false },
      ],
      modelFamilies: ['deepseek'],
    }
    const configDefinitionOptions = {
      configurationClasses: [{ value: 'standard', label: '普通配置' }, { value: 'security_boundary', label: '安全边界' }],
      valueTypes: [{ value: 'integer', label: '整数' }],
      scopes: [{ value: 'system', label: '系统' }, { value: 'project', label: '项目' }, { value: 'run', label: '单次运行' }],
      defaultSources: [{ value: 'model', label: '模型' }, { value: 'workflow', label: '工作流' }],
      domains: [{ value: 'production', label: 'production' }, { value: 'security', label: 'security' }],
      units: [], impactModules: [{ value: 'worker', label: 'worker' }, { value: 'security', label: 'security' }],
      customEntryFields: ['domain', 'unit', 'impact_modules'],
      systemDefaults: {
        configuration_class: 'standard', allowed_scopes: 'system,project,run', allowed_default_sources: '',
        restart_required: false, is_required: false, is_secret: false, is_active: true, needs_review: false,
      },
    }
    const recipeEditorOptions = {
      definitionSchemaVersion: 1,
      stageOptions: [
        { value: 'script_generate', modes: ['ai'], requiresRouting: true },
        { value: 'script_structure', modes: ['ai'], requiresRouting: true },
        { value: 'script_normalize', modes: ['ai', 'manual'], requiresRouting: true },
        { value: 'script_normalize_review', modes: ['review'], requiresRouting: false },
        { value: 'bible_materialize', modes: ['deterministic'], requiresRouting: false },
        { value: 'bible_review', modes: ['review', 'manual'], requiresRouting: false },
        { value: 'storyboard_review', modes: ['review'], requiresRouting: false },
        { value: 'media_generation', modes: ['existing_pipeline'], requiresRouting: true },
        { value: 'artifact_selection', modes: ['review'], requiresRouting: false },
        { value: 'delivery', modes: ['existing_pipeline'], requiresRouting: false },
      ],
      approvalOptions: [{ value: 'auto', label: '自动' }, { value: 'required', label: '必须审核' }],
      routingModeOptions: [{ value: 'fixed', label: '固定路由' }],
      presets: [{
        value: 'ai_draft', label: 'AI 起稿', stages: [
          { key: 'script_generate', mode: 'ai', approval: 'auto' },
          { key: 'bible_materialize', mode: 'deterministic', approval: 'auto' },
          { key: 'bible_review', mode: 'review', approval: 'required' },
          { key: 'media_generation', mode: 'existing_pipeline', approval: 'auto' },
          { key: 'artifact_selection', mode: 'review', approval: 'required' },
          { key: 'delivery', mode: 'existing_pipeline', approval: 'auto' },
        ],
      }],
    }
    const emptyDependencies = { providers: [], modelProfiles: [], deployments: [], runtimeHosts: [], workflowVersions: [], modelProfileOptions, configDefinitionOptions, recipeEditorOptions }
    const recipeFields = schema.resourceEditorFields('recipe', {
      recipe_key: 'series-ai-production', version: 99, name: 'AI production',
      definition: { schema_version: 1, stages: [] },
    }, emptyDependencies)
    assert.equal(recipeFields.some((field: { path: string }) => field.path === 'definition'), false)
    assert.equal(recipeFields.find((field: { path: string }) => field.path === 'version')?.kind, 'readonly')
    const recipePayload = schema.buildRegistryResourcePayload('recipe', {
      recipe_key: 'series-ai-production', version: 99, name: 'AI production',
      definition: { schema_version: 1, stages: [] },
    }, emptyDependencies)
    assert.equal('version' in recipePayload, false)
    assert.equal((recipePayload.definition as { schema_version: number }).schema_version, 1)
    const config = schema.applyResourceDefaults('config-definition', {
      key: 'production.test', domain: 'production', value_type: 'integer', description: 'test', impact_modules: ['worker'],
    }, emptyDependencies)
    assert.equal(config.allowed_scopes, 'system,project,run')
    assert.equal(config.restart_required, false)
    assert.equal(config.is_secret, false)
    const validSecurityDefinition = {
      key: 'security.test.max_items', domain: 'security', configuration_class: 'security_boundary',
      value_type: 'integer', description: 'test security boundary', impact_modules: ['security'],
      allowed_scopes: 'system', allowed_default_sources: '', restart_required: false,
      is_required: true, is_secret: false, is_active: true, needs_review: false,
      value_schema: { minimum: 1 }, initial_value: 10,
    }
    assert.deepEqual(schema.validateRegistryResource('config-definition', validSecurityDefinition, emptyDependencies).errors, [])
    const invalidSecurityDefinition = {
      ...validSecurityDefinition, domain: 'production', allowed_scopes: 'system,project',
      allowed_default_sources: 'model', is_required: false, is_secret: true, is_active: false,
    }
    const securityErrors = schema.validateRegistryResource('config-definition', invalidSecurityDefinition, emptyDependencies).errors.join(' ')
    for (const marker of ['security 配置域', 'system 作用域', '不能接受模型或工作流默认值', '必须标记为必填', '不能承载秘密正文', '保持启用']) {
      assert.match(securityErrors, new RegExp(marker))
    }
    const reactiveModel = new Proxy({
      profile_key: 'deepseek-text', version: 5, provider: '', model_identifier: 'deepseek-v4-flash', modality: 'text',
      specification: { capabilities: ['script.generate', 'prompt.normalize', 'script.structure'] },
    }, {})
    const normalizedReactiveModel = schema.applyResourceDefaults('model', reactiveModel, emptyDependencies)
    assert.equal(normalizedReactiveModel.specification.schema_version, 1)
    assert.deepEqual(normalizedReactiveModel.specification.capabilities, ['script.generate', 'prompt.normalize', 'script.structure'])
    const blockedModel = schema.validateRegistryResource('model', {
      profile_key: 'model-test', version: 1, provider: '', model_identifier: 'model', modality: 'text',
      specification: { schema_version: 1, capabilities: ['script.generate'] },
    }, emptyDependencies)
    assert.ok(blockedModel.errors.some((item: string) => item.includes('先新增并启用 Provider')))
    const providerFields = schema.resourceEditorFields('provider', {
      driver_type: 'async_rest', driver_config: {},
    }, emptyDependencies)
    for (const path of ['driver_config.base_url', 'driver_config.submit_path', 'driver_config.poll_path', 'driver_config.collect_path', 'driver_config.artifact_allowed_hosts', 'driver_config.request_field_map']) {
      assert.ok(providerFields.some((field: { path: string }) => field.path === path), `missing typed provider field ${path}`)
    }
    for (const path of ['driver_config.timeout_seconds', 'driver_config.poll_interval_seconds', 'driver_config.max_poll_seconds']) {
      const field = providerFields.find((item: { path: string }) => item.path === path)
      assert.equal(field?.required, true, `async runtime budget must be explicit: ${path}`)
    }
    const modelFields = schema.resourceEditorFields('model', {
      modality: 'audio', specification: {},
    }, emptyDependencies)
    const modalityField = modelFields.find((field: { path: string }) => field.path === 'modality')
    assert.ok(modalityField?.options.some((option: { value: string; label: string }) => option.value === 'audio' && option.label === '音频（audio）'))
    assert.equal(modelFields.find((field: { path: string; kind: string }) => field.path === 'version')?.kind, 'readonly')
    assert.equal(modelFields.find((field: { path: string; kind: string }) => field.path === 'specification.schema_version')?.kind, 'readonly')
    assert.equal(modelFields.find((field: { path: string; kind: string }) => field.path === 'specification.capabilities')?.kind, 'multiselect')
    assert.equal(modelFields.find((field: { path: string; kind: string }) => field.path === 'specification.model_family')?.kind, 'select')
    const deepseekProvider = { id: 'provider-deepseek', name: 'DeepSeek', driverType: 'deepseek_api', isActive: true }
    const controlledModelPayload = {
      profile_key: 'new-text-family', provider: 'provider-deepseek', model_identifier: 'deepseek-model', modality: 'text',
      specification: { schema_version: 1, capabilities: ['script.generate'], model_family: 'new-text-family' },
    }
    const controlledDependencies = { ...emptyDependencies, providers: [deepseekProvider] }
    assert.deepEqual(schema.validateRegistryResource('model', controlledModelPayload, controlledDependencies).errors, [])
    const controlledFields = schema.resourceEditorFields('model', controlledModelPayload, controlledDependencies)
    const capabilityField = controlledFields.find((field: { path: string }) => field.path === 'specification.capabilities')
    assert.ok(capabilityField.options.some((option: { value: string }) => option.value === 'script.generate'))
    assert.equal(capabilityField.options.some((option: { value: string }) => option.value === 'image.text_to_image'), false)
    const familyField = controlledFields.find((field: { path: string }) => field.path === 'specification.model_family')
    assert.ok(familyField.options.some((option: { value: string; label: string }) => option.value === 'new-text-family' && option.label.includes('新建提示词策略族')))
    const unavailableProvider = schema.validateRegistryResource('model', {
      ...controlledModelPayload, provider: 'inactive-provider',
    }, controlledDependencies)
    assert.ok(unavailableProvider.errors.some((item: string) => item.includes('包含不可用选项 inactive-provider')))
    const legacyFields = schema.resourceEditorFields('model', {
      ...controlledModelPayload,
      specification: { ...controlledModelPayload.specification, capabilities: ['legacy.text.operation'] },
    }, controlledDependencies)
    assert.ok(legacyFields.find((field: { path: string }) => field.path === 'specification.capabilities')?.options.some(
      (option: { value: string; label: string }) => option.value === 'legacy.text.operation' && option.label.includes('历史已登记值'),
    ))
    const promptNormalizationFields = schema.resourceEditorFields('model', {
      modality: 'text', specification: { capabilities: ['prompt.normalize'] },
    }, emptyDependencies)
    for (const path of [
      'specification.prompt_normalization.max_input_bytes',
      'specification.prompt_normalization.input_byte_warning_threshold',
      'specification.prompt_normalization.max_output_tokens',
      'specification.prompt_normalization.output_token_warning_threshold',
      'specification.prompt_normalization.max_items_per_batch',
      'specification.prompt_normalization.timeout_seconds',
      'specification.prompt_normalization.chat_request_options',
    ]) assert.ok(promptNormalizationFields.some((field: { path: string }) => field.path === path), `missing prompt.normalize typed field ${path}`)
    assert.equal(modelFields.some((field: { path: string }) => field.path.startsWith('specification.prompt_normalization.')), false)
    assert.deepEqual(schema.promptNormalizationValidationErrors({
      max_input_bytes: 16000,
      input_byte_warning_threshold: 8000,
      max_output_tokens: 8192,
      output_token_warning_threshold: 4096,
      max_items_per_batch: 1,
      timeout_seconds: 180,
      chat_request_options: {
        temperature: 0,
        response_format: { type: 'json_object' },
        thinking: { type: 'disabled' },
      },
    }), [])
    assert.match(schema.promptNormalizationValidationErrors({
      max_input_bytes: 16000,
      input_byte_warning_threshold: 16001,
      max_output_tokens: 8192,
      max_items_per_batch: 1,
      timeout_seconds: 180,
      chat_request_options: {},
    }).join(' '), /输入字节建议阈值不能大于/)
    assert.match(schema.promptNormalizationValidationErrors({
      max_input_bytes: 16000,
      max_output_tokens: 8192,
      output_token_warning_threshold: 8193,
      max_items_per_batch: 1,
      timeout_seconds: 180,
      chat_request_options: {},
    }).join(' '), /建议阈值不能大于/)
    assert.deepEqual(schema.promptNormalizationValidationErrors({
      max_input_bytes: 12000,
      max_output_tokens: 8192,
      max_items_per_batch: 1,
      timeout_seconds: 180,
      chat_request_options: {},
    }), [], 'generic editor range must not hard-code the current H3 capacity instance')
    assert.match(schema.promptNormalizationValidationErrors({
      max_input_bytes: 16000,
      max_output_tokens: 8192,
      max_items_per_batch: 1,
      timeout_seconds: 180,
      chat_request_options: { nested: { messages: [] }, clientSecret: 'must-not-save' },
      legacy_batch_size: 4,
    }).join(' '), /未知字段.*不得覆盖.*不得包含.*凭据/)
    const visualRulesFields = schema.resourceEditorFields('model', {
      modality: 'text', specification: { capabilities: ['visual_rules.generate'] },
    }, emptyDependencies)
    for (const path of [
      'specification.visual_rules.max_input_bytes',
      'specification.visual_rules.input_byte_warning_threshold',
      'specification.visual_rules.max_output_tokens',
      'specification.visual_rules.output_token_warning_threshold',
      'specification.visual_rules.timeout_seconds',
      'specification.visual_rules.quality_advisories',
      'specification.visual_rules.chat_request_options',
    ]) assert.ok(visualRulesFields.some((field: { path: string }) => field.path === path), `missing visual_rules.generate typed field ${path}`)
    assert.deepEqual(schema.visualRulesValidationErrors({
      max_input_bytes: 65536,
      max_output_tokens: 131072,
      timeout_seconds: 600,
      chat_request_options: { temperature: 0, response_format: { type: 'json_object' } },
      quality_advisories: {
        schema_version: 1,
        base_colors_min_items: 1,
        base_colors_max_items: 3,
        negative_constraints_min_items: 0,
        negative_constraints_max_items: 32,
        negative_constraint_max_chars: 120,
        recommended_negative_clauses: ['identity drift', 'readable text'],
        continuity_summary_max_chars: 3000,
        negative_prompt_max_chars: 8000,
        color_script_max_chars: 2000,
      },
    }), [])
    assert.deepEqual(schema.visualRulesValidationErrors({
      max_input_bytes: 65536,
      max_output_tokens: 131072,
      timeout_seconds: 600,
      chat_request_options: {},
    }), [], 'omitted visual-rules quality policy must not enable hidden recommendations')
    assert.deepEqual(schema.visualRulesValidationErrors({
      max_input_bytes: 65536,
      max_output_tokens: 131072,
      timeout_seconds: 600,
      chat_request_options: {},
      quality_advisories: {
        schema_version: 1,
        continuity_summary_max_chars: 70 * 1024,
      },
    }), [], 'quality recommendations above 64 KiB remain valid below the absolute Provider JSON envelope')
    assert.match(schema.visualRulesValidationErrors({
      max_input_bytes: 65536,
      max_output_tokens: 131072,
      timeout_seconds: 600,
      chat_request_options: {},
      quality_advisories: {
        schema_version: 1,
        base_colors_min_items: 4,
        base_colors_max_items: 3,
      },
    }).join(' '), /base_colors_min_items 不能大于/)
    assert.match(schema.visualRulesValidationErrors({
      max_input_bytes: 65536,
      max_output_tokens: 131072,
      timeout_seconds: 600,
      chat_request_options: {},
      quality_advisories: {
        schema_version: 1,
        recommended_negative_clauses: ['identity drift', 'Identity Drift'],
      },
    }).join(' '), /recommended_negative_clauses 不能包含重复项/)
    const artifactKindField = providerFields.find((field: { path: string }) => field.path === 'driver_config.artifact_kind_field')
    assert.match(artifactKindField?.description || '', /图片（image）.*视频（video）.*音频（audio）/)
    const artifactFields = schema.resourceEditorFields('artifact', {}, emptyDependencies)
    for (const path of ['inventory_folder', 'inventory_node_type', 'inventory_input_name']) {
      assert.ok(artifactFields.some((field: { path: string }) => field.path === path), `missing typed artifact inventory field ${path}`)
    }
    assert.match(artifactFields.find((field: { path: string }) => field.path === 'inventory_folder')?.description || '', /标准文件角色可留空/)
    const asyncProvider = schema.applyResourceDefaults('provider', {
      key: 'media-api', name: 'Media API', driver_type: 'async_rest', modalities: 'image', driver_config: {}, is_active: true,
    }, emptyDependencies)
    assert.equal(asyncProvider.driver_config.request_field_map, undefined)
    const comfyProvider = { id: 'provider-comfy', name: 'Comfy', driverType: 'comfyui', driverConfig: {}, isActive: true }
    const publishedModel = { id: 'model-comfy', profileKey: 'sdxl', version: 1, provider: 'provider-comfy', modelIdentifier: 'sdxl', status: 'published', specification: { capabilities: ['image.text_to_image'] } }
    const deployment = schema.applyResourceDefaults('deployment', {
      key: 'comfy-deployment', name: 'Comfy deployment', profile_version: 'model-comfy', runtime_config: {}, status: 'active',
    }, { ...emptyDependencies, providers: [comfyProvider], modelProfiles: [publishedModel] })
    assert.deepEqual(deployment.runtime_config, {})
    const comfyDeploymentFields = schema.resourceEditorFields('deployment', deployment, {
      ...emptyDependencies, providers: [comfyProvider], modelProfiles: [publishedModel],
    })
    for (const path of [
      'runtime_config.pre_submit_resource_release.schema_version',
      'runtime_config.pre_submit_resource_release.enabled',
      'runtime_config.pre_submit_resource_release.unload_models',
      'runtime_config.pre_submit_resource_release.free_memory',
    ]) assert.ok(comfyDeploymentFields.some((field: { path: string }) => field.path === path), `missing Comfy pre-submit release field ${path}`)
    assert.match(schema.validateRegistryResource('deployment', {
      ...deployment,
      runtime_config: {
        base_url: 'http://127.0.0.1:8188',
        pre_submit_resource_release: { schema_version: 1, enabled: true },
      },
    }, { ...emptyDependencies, providers: [comfyProvider], modelProfiles: [publishedModel] }).errors.join(' '), /缺少字段/)
    assert.deepEqual(schema.validateRegistryResource('deployment', {
      ...deployment,
      runtime_config: {
        base_url: 'http://127.0.0.1:8188',
        timeout_seconds: 30, poll_interval_seconds: 2,
        max_poll_seconds: 3600, capability_ttl_seconds: 3600,
        pre_submit_resource_release: {
          schema_version: 1, enabled: true, unload_models: true, free_memory: true,
        },
      },
    }, { ...emptyDependencies, providers: [comfyProvider], modelProfiles: [publishedModel] }).errors, [])
    assert.ok(schema.validateRegistryResource('deployment', deployment, {
      ...emptyDependencies, providers: [comfyProvider], modelProfiles: [publishedModel],
    }).errors.some((item: string) => item.includes('服务根地址')))
    const recipeStages = schema.standardRecipeStages('ai', recipeEditorOptions)
    assert.equal(recipeStages[0].key, 'script_generate')
    assert.ok(recipeStages.some((stage: { key: string }) => stage.key === 'media_generation'))
    assert.ok(recipeStages.some((stage: { key: string }) => stage.key === 'delivery'))
    assert.equal(recipeStages.find((stage: { key: string }) => stage.key === 'artifact_selection').mode, 'review')
    const recipeStageOptions = schema.recipeStageOptions(recipeEditorOptions)
    assert.ok(recipeStageOptions.some((item: { value: string }) => item.value === 'script_structure'))
    const recipeStageOrder = recipeStageOptions.map((item: { value: string }) => item.value)
    assert.ok(recipeStageOrder.indexOf('script_normalize_review') < recipeStageOrder.indexOf('storyboard_review'))
    assert.deepEqual(schema.recipeStageModes(recipeEditorOptions).script_normalize, ['ai', 'manual'])
    assert.equal(schema.recipeStageRequiresRouting({ key: 'script_normalize', mode: 'ai' }, recipeEditorOptions), true)
    assert.equal(schema.recipeStageRequiresRouting({ key: 'script_normalize', mode: 'manual' }, recipeEditorOptions), false)
    assert.equal(schema.recipeStageRequiresRouting({ key: 'media_generation', mode: 'existing_pipeline' }, recipeEditorOptions), true)

    const bibleImages = [
      { key: 'bible_image_generation', mode: 'existing_pipeline', approval: 'auto' },
      { key: 'bible_image_review', mode: 'review', approval: 'required' },
    ]
    assert.deepEqual(schema.recipeBibleSourceValidationErrors({
      schema_version: 1,
      stages: [
        { key: 'bible_materialize', mode: 'deterministic', approval: 'auto' },
        { key: 'bible_review', mode: 'review', approval: 'required' },
        ...bibleImages,
      ],
    }), [])
    assert.deepEqual(schema.recipeBibleSourceValidationErrors({
      schema_version: 1,
      stages: [
        { key: 'script_input', mode: 'manual', approval: 'auto' },
        { key: 'bible_review', mode: 'manual', approval: 'required' },
        ...bibleImages,
      ],
    }), [])
    assert.match(schema.recipeBibleSourceValidationErrors({
      schema_version: 1,
      stages: [
        { key: 'bible_materialize', mode: 'deterministic', approval: 'auto' },
        { key: 'bible_review', mode: 'manual', approval: 'required' },
        ...bibleImages,
      ],
    })[0], /必须且只能选择一种文本 Bible 来源/)
    assert.deepEqual(schema.recipeBibleSourceValidationErrors({
      schema_version: 1,
      stages: [
        { key: 'script_generate', mode: 'ai', approval: 'auto' },
        { key: 'bible_review', mode: 'manual', approval: 'required' },
        ...bibleImages,
      ],
    }), [])
    assert.equal(
      schema.standardRecipeStages('ai', recipeEditorOptions).find((stage: { key: string }) => stage.key === 'bible_materialize')?.mode,
      'deterministic',
    )
    assert.equal(
      schema.standardRecipeStages('ai', recipeEditorOptions).find((stage: { key: string }) => stage.key === 'bible_review')?.mode,
      'review',
    )
    assert.deepEqual(schema.recipeBibleSourceValidationErrors({
      schema_version: 2,
      interaction_mode: 'unattended',
      stages: [
        { key: 'bible_materialize', mode: 'deterministic', approval: 'auto' },
        { key: 'bible_review', mode: 'review', approval: 'policy' },
        ...bibleImages,
      ],
    }), [])
  } finally { await vite.close() }

  const settings = readFileSync(join(frontendRoot, 'src', 'views', 'SettingsView.vue'), 'utf8')
  const editor = readFileSync(join(frontendRoot, 'src', 'components', 'RegistryResourceEditor.vue'), 'utf8')
  const registrySchema = readFileSync(join(frontendRoot, 'src', 'services', 'registryResourceSchema.ts'), 'utf8')
  for (const marker of ['RegistryResourceEditor', 'editorDependencies', 'editorValidity', '结构化字段（Schema-driven）· 服务端校验']) {
    assert.ok(settings.includes(marker), `SettingsView misses ${marker}`)
  }
  for (const marker of [
    'editorConfigHasValues', 'editorConfigMutableFields', 'editorOriginalPayload',
    'delete parsed.is_active', 'delete parsed.needs_review', 'delete parsed.review_reason',
    'delete parsed.recipe_key', "['config-definition', 'recipe'].includes(editorResource.value)",
    'Object.fromEntries(Object.entries(parsed).filter', '未检测到需要提交的配置定义变更',
    'recipes.value = []', 'sectionErrors.value.recipes', '配方列表或服务端编辑目录尚未完整读取',
  ]) assert.ok(settings.includes(marker), `SettingsView misses minimized ConfigDefinition payload marker ${marker}`)
  assert.equal(settings.includes('structuredClone(editorPayload.value)'), false)
  assert.equal(editor.includes('structuredClone(recipeStages.value)'), false)
  assert.ok(editor.indexOf('const recipeErrors = computed') < editor.indexOf('const validation = computed'), 'recipe validation must initialize recipeErrors before the immediate watcher reads it')
  for (const marker of ['影响：', '套用 AI 起稿链路', 'active Deployment', '高级 JSON · 同一对象完整视图']) {
    assert.ok(editor.includes(marker), `RegistryResourceEditor misses ${marker}`)
  }
  for (const marker of ['recipeStageRequiresRouting(stages[index], recipeEditorOptions.value)', 'v-if="recipeStageRequiresRouting(stage,recipeEditorOptions)"']) {
    assert.ok(editor.includes(marker), `RegistryResourceEditor misses mode-aware routing marker ${marker}`)
  }
  for (const marker of ['请先发布模型版本', '前置依赖', 'driver_config.base_url', 'driver_config.progress_field', 'driver_config.pricing.currency', 'standardRecipeStages']) {
    assert.ok(registrySchema.includes(marker), `registryResourceSchema misses ${marker}`)
  }
  for (const marker of ['promptNormalizationCapacitySummary', 'maxInputBytes} B', '重跑真实烟测（Smoke）']) {
    assert.ok(settings.includes(marker), `SettingsView misses prompt normalization summary marker ${marker}`)
  }
  assert.equal(settings.includes('v-model="editorText"'), false)
})

test('settings lifecycle uses explicit details, evidence and confirmation modals', () => {
  const settings = readFileSync(join(frontendRoot, 'src', 'views', 'SettingsView.vue'), 'utf8')
  const workflow = readFileSync(join(frontendRoot, 'src', 'views', 'WorkflowVersionEditorView.vue'), 'utf8')
  assert.equal(settings.includes('window.prompt'), false)
  assert.equal(settings.includes('.slice(0,30)'), false)
  assert.equal(settings.includes('.slice(0,50)'), false)
  for (const marker of ['detailOpen', 'actionOpen', 'decisionOpen', 'buildSystemConfigChangeSet', 'restoreConfigInheritance', 'getLatestRuntimeSnapshot']) {
    assert.ok(settings.includes(marker), `SettingsView misses ${marker}`)
  }
  for (const marker of ['compatibleDeployments', 'validatedDeployment', 'validationEvidence', 'smokeTestEvidence', 'publishOpen', '处理建议', '技术信息（供排查）', 'handleRecoveryAction', 'validationGateMessage']) {
    assert.ok(workflow.includes(marker), `WorkflowVersionEditor misses ${marker}`)
  }
  assert.ok(settings.indexOf('关联并核对模型文件') < settings.indexOf('核对发布'))
})

test('workflow acceptance unlocks Smoke and publish in the validated lifecycle stage', async () => {
  const vite = await server()
  try {
    const acceptance = await vite.ssrLoadModule('/src/services/workflowAcceptance.ts')
    assert.deepEqual(acceptance.workflowAcceptancePhase({
      status: 'draft', validationState: 'not_run', smokeTestState: 'not_run',
    }), {
      canLiveValidate: true, canConfigureSmoke: false, canStartSmoke: false, canPublish: false,
    })
    assert.deepEqual(acceptance.workflowAcceptancePhase({
      status: 'validated', validationState: 'passed', smokeTestState: 'not_run',
    }), {
      canLiveValidate: true, canConfigureSmoke: true, canStartSmoke: true, canPublish: false,
    })
    assert.deepEqual(acceptance.workflowAcceptancePhase({
      status: 'validated', validationState: 'passed', smokeTestState: 'passed',
    }), {
      canLiveValidate: true, canConfigureSmoke: true, canStartSmoke: true, canPublish: true,
    })
    for (const status of ['published', 'retired']) assert.deepEqual(
      acceptance.workflowAcceptancePhase({ status, validationState: 'passed', smokeTestState: 'passed' }),
      { canLiveValidate: false, canConfigureSmoke: false, canStartSmoke: false, canPublish: false },
    )

    const editor = readFileSync(join(frontendRoot, 'src', 'views', 'WorkflowVersionEditorView.vue'), 'utf8')
    for (const marker of [
      'acceptancePhase.canConfigureSmoke', 'smokeStartBlockReason', 'smokeInputDisabled',
      'smokeDraftRequiresAssetCandidates', 'void loadSmokeAssetCandidates()', '成功后即可编辑本次 Smoke 输入',
      'deploymentId.value !== version.validatedDeployment', 'syncDeploymentEvidence()',
    ]) assert.ok(editor.includes(marker), `workflow acceptance UI misses ${marker}`)
    assert.equal(editor.includes("savedVersion.status!=='draft'||savedVersion.validationState!=='passed'"), false)
  } finally { await vite.close() }
})

test('workflow version review is read-only and saved-workflow scan is the only creation path', () => {
  const workflow = readFileSync(join(frontendRoot, 'src', 'views', 'WorkflowVersionEditorView.vue'), 'utf8')
  const platformApi = readFileSync(join(frontendRoot, 'src', 'services', 'platformApi.ts'), 'utf8')
  const settings = readFileSync(join(frontendRoot, 'src', 'views', 'SettingsView.vue'), 'utf8')
  for (const marker of ['不修改节点图或创建新版本', '模板固定', '执行任务冻结', 'validateWorkflow', 'createWorkflowSmokeAttempt', 'publishWorkflow']) {
    assert.ok(workflow.includes(marker), `WorkflowVersionEditor misses ${marker}`)
  }
  for (const retired of ['VueFlow', '导入 JSON', '保存新版本', '添加动态节点', 'importWorkflowVersion', 'inputContractText']) {
    assert.equal(workflow.includes(retired), false, retired)
  }
  assert.equal(platformApi.includes('importWorkflowVersion'), false)
  assert.equal(settings.includes('/settings/workflows/new'), false)
  assert.ok(settings.includes('scanComfySavedWorkflows'))
  assert.ok(settings.includes('batchImportComfySavedWorkflows'))
})

test('workflow template canvas export is a download/import flow and never fakes direct open', async () => {
  const vite = await server()
  try {
    const adapters = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
    const exported = adapters.adaptWorkflowTemplateCanvasExport({
      workflow_version_id: 'version-1', format: 'comfyui_ui', filename: 'workflow.json',
      graph_sha256: 'a'.repeat(64), template_slug: 'image', template_version: 2,
      deployment_id: 'deployment-1', deployment_name: 'Local ComfyUI',
      deployment_source: 'explicit_draft',
      workflow: { version: 0.4, nodes: [], links: [] }, import_ready: true,
      open_reason: '下载后导入', direct_open_supported: true,
      direct_open_url: 'https://unsafe.example.test/inject', open_mode: 'download_import', download_url: '/download',
    })
    assert.equal(exported.directOpenSupported, false)
    assert.equal(exported.directOpenUrl, null)
    assert.equal(exported.openMode, 'download_import')
    assert.equal(exported.deploymentId, 'deployment-1')
    assert.equal(exported.deploymentName, 'Local ComfyUI')
    assert.equal(exported.deploymentSource, 'explicit_draft')
    assert.equal(exported.workflow.version, 0.4)
  } finally { await vite.close() }

  const api = readFileSync(join(frontendRoot, 'src', 'services', 'platformApi.ts'), 'utf8')
  const settings = readFileSync(join(frontendRoot, 'src', 'views', 'SettingsView.vue'), 'utf8')
  const workflowEditor = readFileSync(join(frontendRoot, 'src', 'views', 'WorkflowVersionEditorView.vue'), 'utf8')
  assert.ok(api.includes('/comfyui-workflow/'))
  for (const marker of ['在 ComfyUI 中打开…', 'direct_open_supported=false', 'downloadWorkflowCanvas', '不会提交生产任务', 'workflowExportDeploymentOptions', '未校验草稿没有持久化节点目录证据', '不会回退默认 ComfyUI']) {
    assert.ok(settings.includes(marker), `SettingsView misses safe workflow export marker: ${marker}`)
  }
  for (const marker of ['downloadSavedComfyCanvas', "openMode !== 'download_import'", '下载 ComfyUI 画布', '不会注入 ComfyUI 或提交生产任务', 'version.validatedDeployment || deploymentId.value']) {
    assert.ok(workflowEditor.includes(marker), `WorkflowVersionEditor misses safe workflow export marker: ${marker}`)
  }
  assert.ok(api.includes("{ params: deploymentId ? { deployment_id: deploymentId } : {} }"))
})

test('PromptProfile settings expose typed CRUD, deterministic preview and published binding dependencies', async () => {
  const vite = await server()
  try {
    const adapters = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
    const previewExamples = await vite.ssrLoadModule('/src/services/promptProfilePreviewExamples.ts')
    const field = adapters.adaptPromptFieldDefinition({
      id: 'field-1', key: 'lighting', label: '灯光', category: 'visual', description: '场景照明', impact: '影响画面层次',
      value_type: 'enum', default_value: 'cinematic', value_options: ['cinematic', 'natural'],
      preview_example: 'natural',
      is_required: true, is_active: true, display_order: 10, created_at: '', updated_at: '',
    })
    assert.equal(field.valueType, 'enum')
    assert.deepEqual(field.valueOptions, ['cinematic', 'natural'])
    assert.equal(field.previewExample, 'natural')
    assert.equal(field.isRequired, true)
    assert.deepEqual(
      previewExamples.mergePromptProfilePreviewExamples({}, [
        { inputKey: 'list', previewExample: ['first', 'second'] },
        { inputKey: 'flag', previewExample: false },
        { inputKey: 'count', previewExample: 0 },
      ]),
      { list: 'first\nsecond', flag: false, count: 0 },
    )
    assert.deepEqual(
      previewExamples.mergePromptProfilePreviewExamples(
        { text: '', flag: false, count: 0 },
        [
          { inputKey: 'text', previewExample: 'must not replace explicit empty' },
          { inputKey: 'flag', previewExample: true },
          { inputKey: 'count', previewExample: 1 },
          { inputKey: 'new', previewExample: 'server example' },
        ],
      ),
      { text: '', flag: false, count: 0, new: 'server example' },
    )
    const rawOptions = {
      operations: ['video.generate'], model_families: ['minimax-h3'], modes: ['text_to_video'],
      field_value_types: [{ value: 'text', label: '短文本' }], field_categories: [{ value: 'visual', label: 'visual' }],
      custom_entry_fields: ['field_category', 'operation', 'mode', 'language_policy'],
      field_system_defaults: { default_value: null, value_options: [], is_required: false, is_active: true, display_order: 'append' },
      rule_system_defaults: { label_style: 'label_colon', separator: 'newline', required: false, omit_if_empty: true, max_chars: 2000, generation_output_semantics: 'final', generation_required: false, language_policy: 'inherit', length_unit: 'none', generation_validator_keys: [], generation_context_selectors: [] },
      source_selectors: [{ value: 'request.lighting', label: '请求灯光' }],
      channels: [{ value: 'positive', label: '正向提示词' }], label_styles: [{ value: 'label_colon', label: '字段名加冒号' }], separators: [{ value: 'newline', label: '换行' }],
      generation_modes: [{ value: 'model_generated', label: '模型生成 / Model generated' }],
      generation_source_selectors: [{
        value: 'shot.narrative', label: '当前镜头叙事', scope_type: 'shot', group: 'shot', group_label: '当前镜头 / Target shot',
        description: '读取当前镜头已确认的叙事。', availability: '适用于模型生成和仅翻译。', scope: '仅当前镜头；不自动读取对白或其他镜头。',
        dialogue_exposure: 'not_explicit', cross_shot_exposure: 'target_shot_only', cardinality: 'single', status: 'active',
        generation_modes: ['model_generated', 'translate_only'],
      }],
      generation_context_selectors: [{ value: 'target_shot.linked_bible', label: '目标镜头关联 Bible / Target shot linked Bible', description: '当前目标镜头已关联的人物、场景和道具一致性事实。' }],
      generation_output_semantics: [{ value: 'final', label: '最终字段值 / Final field value' }],
      source_projection_keys: [{ value: 'direct_v1', label: '直接字段装配' }], source_projection_primitives: [{value:'test.summary',label:'测试摘要',engine_key:'test',contribution_source_selectors:[],rendering_template_keys:['summary']}],
      reference_rendering_fields: [{ key: 'summary', label: '任务摘要', placeholders: ['mode', 'duration_seconds'],
        description:'任务概要', assembly:'任务摘要原语', requirement:'必填',
        placeholder_help:{mode:{label:'生成模式',description:'本次模式',example:'Ref2VA'},duration_seconds:{label:'秒数',description:'本次总时长',example:'4.0'}},
        example:{template:'{mode} {duration_seconds}',result:'Ref2VA 4.0'},
      }],
      language_policies: [{ value: 'en', label: '英文 / English' }],
      length_units: [{ value: 'english_words', label: '英文词数 / English words' }],
      generation_validators: [{ value: 'non_empty', label: '非空 / Non-empty' }],
      model_profile_versions: [{ id: 'model-1', profile_key: 'h3', version: 2, model_identifier: 'h3-v2', modality: 'video' }],
    }
    const options = adapters.adaptPromptProfileOptions(rawOptions)
    assert.deepEqual(options.sourceProjectionPrimitives[0].renderingTemplateKeys,['summary'])
    assert.throws(()=>adapters.adaptPromptProfileOptions({...rawOptions,source_projection_primitives:[{value:'test.summary'}]}),/rendering_template_keys/)
    const {placeholder_help, ...renderingField} = rawOptions.reference_rendering_fields[0]
    assert.deepEqual(options.referenceRenderingFields, [{...renderingField, placeholderHelp:placeholder_help}])
    for (const invalid of [
      [{ key: 'summary', label: '任务摘要' }],
      [{ key: 'summary', label: '', placeholders: [] }],
      [{ key: 'summary', label: '任务摘要', placeholders: ['mode', 'mode'] }],
      [{ key: 'summary', label: '任务摘要', placeholders: ['mode.__class__'] }],
      [{...rawOptions.reference_rendering_fields[0],placeholder_help:{}}],
      [{...rawOptions.reference_rendering_fields[0],example:{template:'{mode}'}}],
    ]) assert.throws(() => adapters.adaptPromptProfileOptions({ ...rawOptions, reference_rendering_fields: invalid }))
    assert.deepEqual(options.sourceSelectors[0], { value: 'request.lighting', label: '请求灯光' })
    assert.equal(options.generationModes[0].value, 'model_generated')
    assert.deepEqual(options.generationSourceSelectors[0], {
      value: 'shot.narrative', label: '当前镜头叙事', scopeType: 'shot', group: 'shot', groupLabel: '当前镜头 / Target shot',
      description: '读取当前镜头已确认的叙事。', availability: '适用于模型生成和仅翻译。', scope: '仅当前镜头；不自动读取对白或其他镜头。',
      dialogueExposure: 'not_explicit', crossShotExposure: 'target_shot_only', cardinality: 'single', status: 'active',
      generationModes: ['model_generated', 'translate_only'],
    })
    assert.equal(options.generationContextSelectors[0].value, 'target_shot.linked_bible')
    assert.equal(options.generationContextSelectors[0].description, '当前目标镜头已关联的人物、场景和道具一致性事实。')
    assert.equal(options.lengthUnits[0].value, 'english_words')
    assert.equal(options.generationValidators[0].value, 'non_empty')
    assert.equal(options.modelProfileVersions[0].modelIdentifier, 'h3-v2')
    const rule = adapters.adaptPromptProfileFieldRule({
      id: 'rule-1', profile_version: 'profile-1', field_definition: 'field-1', field_key: 'lighting', field_label: '灯光',
      field_value_type: 'long_text', source_selector: 'request.lighting', channel: 'positive_prompt', position: 1,
      label_style: 'label_colon', separator: 'newline', required: true, omit_if_empty: false, max_chars: 2000,
      generation_mode: 'model_generated', generation_source_selector: 'shot.narrative', generation_required: true,
      generation_context_selectors: ['project.style', 'target_shot.linked_bible'],
      language_policy: 'en', length_unit: 'english_words', generation_min: 120, generation_max: 240,
      generation_validator_keys: ['non_empty'], created_at: '', updated_at: '',
    })
    assert.equal(rule.generationMode, 'model_generated')
    assert.equal(rule.generationSourceSelector, 'shot.narrative')
    assert.deepEqual(rule.generationContextSelectors, ['project.style', 'target_shot.linked_bible'])
    assert.deepEqual(rule.generationValidatorKeys, ['non_empty'])
    const preview = adapters.adaptPromptProfilePreview({
      schema_version: 1, prompt_profile_version_id: 'profile-1', profile_key: 'h3-t2v', version: 1,
      contract_sha256: 'a'.repeat(64), channels: { positive: 'Lighting: cinematic' },
      structured_fields: { lighting: 'cinematic' },
      trace: [{ field_key: 'lighting', source_selector: 'request.lighting', channel: 'positive', defaulted: false }],
    })
    assert.equal(preview.channels.positive, 'Lighting: cinematic')
    assert.deepEqual(preview.structuredFields, { lighting: 'cinematic' })
    assert.equal(preview.trace[0].source_selector, 'request.lighting')
  } finally { await vite.close() }

  const settings = readFileSync(join(frontendRoot, 'src', 'views', 'SettingsView.vue'), 'utf8')
  const sectionCatalog = readFileSync(join(frontendRoot, 'src', 'content', 'settingsSectionCatalog.ts'), 'utf8')
  const component = readFileSync(join(frontendRoot, 'src', 'components', 'PromptProfileSettings.vue'), 'utf8')
  const api = readFileSync(join(frontendRoot, 'src', 'services', 'platformApi.ts'), 'utf8')
  assert.ok(sectionCatalog.includes("id: 'prompts'"), 'settings section catalog misses prompts')
  for (const marker of ["tab==='prompts'", 'PromptProfileSettings']) assert.ok(settings.includes(marker), `SettingsView misses ${marker}`)
  for (const marker of ['通用字段字典', '最终提示词预览', '运行时绑定', '官方来源', 'system_instruction', 'definition.default', 'Published PromptProfile', '不会调用模型', '字段内容如何获得', '最终提示词如何排版', '内容生成方式', '主要内容来源', '这不是文件地址或数据库路径', '读取什么', '什么时候可用', '作用范围', '生成时可参考的额外信息', '不含对白正文', '必须显式选择', 'options API', 'Language policy', 'Controlled validators', '不支持表达式、脚本、函数或自定义正则', 'length_advisory', '实时最终提示词', '当前未保存设置', '400ms 后自动组合', '服务端已自动填入通用示例', '恢复通用示例', 'mergePromptProfilePreviewExamples']) {
    assert.ok(component.includes(marker), `PromptProfileSettings misses ${marker}`)
  }
  assert.equal(component.includes('第二层'), false, 'PromptProfileSettings must not require Layer 2 numbering')
  assert.equal(component.includes('第三层'), false, 'PromptProfileSettings must not require Layer 3 numbering')
  assert.equal(component.includes('内容读取位置'), false, 'PromptProfileSettings must use a business-facing source label')
  assert.equal(component.includes('placeholder="shot.narrative"'), false, 'generation source must not be a free path input')
  assert.equal(/<input[^>]+v-model(?:\.trim)?="ruleForm\.generationSourceSelector"/.test(component), false, 'generation source must not use a text input')
  assert.ok(component.includes('<select v-model="ruleForm.generationSourceSelector"'))
  assert.ok(component.includes('options.value.generationSourceSelectors'))
  assert.ok(component.includes('generationSourceGroups'))
  assert.ok(component.includes('generationSourceUnknown'))
  assert.ok(component.includes('generationSourceIncompatible'))
  assert.ok(component.includes('当前草稿必须从下拉目录重新选择后才能保存或预览'))
  assert.ok(component.includes('contributionSourceSelectionBlocked'))
  assert.ok(component.includes('请先在下方选择声明式投影输出与确定性原语'))
  assert.ok(component.includes('rememberGenerationModeBeforeUserChange'))
  assert.ok(component.includes('onRuleGenerationModeChange'))
  assert.ok(component.includes('lockedGenerationModes.includes(generationModeBeforeUserChange.value)'))
  assert.ok(component.includes('跟随最终提示词取值'))
  assert.ok(component.includes('当前候选主要内容来源'))
  assert.ok(component.includes('ruleGenerationModeOptions'))
  assert.ok(component.includes('selectedProfileSupportsModelGeneration'))
  assert.ok(component.includes('字段级模型生成目前由图像/视频 ProductionRun 执行'))
  assert.equal(component.includes('候选规则已按'), false, 'preview must not claim validation before a server response')
  for (const marker of ['void runPreview(requestSerial)', '}, 400)', 'requestSerial !== previewRequestSerial', 'expectedBaseContractSha256', 'previewResult.channels', 'previewResult.executedStages', '示例不会保存', '上次有效预览；等待当前设置重新组合', 'previewBusy.value = false', ':open="!previewResult"']) {
    assert.ok(component.includes(marker), `PromptProfile live preview misses ${marker}`)
  }
  for (const marker of ['CatalogChoiceField v-model="profileForm.operation"', ':options="operationCatalog"', '登记新 Operation（高级）', '日常配置从服务端目录选择', 'Provider driver 证据']) {
    assert.ok(component.includes(marker), `PromptProfile controlled operation catalog misses ${marker}`)
  }
  assert.equal(component.includes('list="prompt-profile-operations"'), false, 'PromptProfile operation must not fall back to a datalist')
  assert.equal(/<input[^>]+v-model(?:\.trim)?="profileForm\.operation"/.test(component), false, 'PromptProfile operation must use the shared controlled catalog')
  assert.ok(component.includes('pattern="^[a-z][a-z0-9_.-]{1,99}$"'), 'PromptProfile advanced operation registration misses the controlled key format')
  assert.equal(/<input[^>]+v-model\.number="profileForm\.version"/.test(component), false, 'PromptProfile version is server-assigned and must not be an input')
  for (const marker of ['/prompt-field-definitions/', '/prompt-profile-versions/', '/prompt-profile-field-rules/', '/prompt-profile-bindings/', 'previewPromptProfileVersion', 'publishPromptProfileVersion', 'retirePromptProfileVersion']) {
    assert.ok(api.includes(marker), `platformApi misses ${marker}`)
  }
  assert.equal(component.includes('window.prompt'), false)
  assert.equal(component.includes('v-model="rawJson"'), false)
  assert.ok(component.includes('Binding Key 创建后不可修改；如需新 Key，请新建绑定并停用旧绑定。'))
  assert.ok(component.includes('v-model="bindingForm.key" :disabled="Boolean(bindingForm.id)"'))
  assert.ok(component.includes('generation_context_selectors: [...ruleForm.generationContextSelectors]'))
  assert.ok(component.includes('v-model="ruleForm.generationContextSelectors"'))
  assert.ok(component.includes('promptOptionsReady'))
  assert.ok(component.includes('contextOptions.some((item) => !item.value.trim() || !item.label.trim() || !item.description.trim())'))
  assert.ok(component.includes('generation_context_selectors 受控目录，字段规则编辑已停止'))
  assert.ok(component.includes('allowedContextSelectors.has(item)'))
  assert.ok(component.includes(':disabled="!activeFields.length||!promptOptionsReady"'))
  for (const marker of [
    'primitiveEngineKeysForSelectedProfile', 'compatibleProjectionPrimitiveOptions',
    'primitiveEngineKeysForSelectedProfile.value.has(item.engineKey)',
    'profileProjectionLocked', "profileForm.sourceProjectionKey !== 'declarative_v1') return {}",
    'changedTopLevelPayload', 'delete payload.profile_key', 'delete payload.key',
    'direct_v1 只接受 final',
    ':disabled="selectedProfile?.sourceProjectionKey===\'direct_v1\'&&item.value===\'contribution\'"',
    ':open="!ruleForm.id||!ruleForm.languagePolicy||!ruleForm.lengthUnit||',
  ]) assert.ok(component.includes(marker), `PromptProfile projection editor misses ${marker}`)
  assert.equal(component.includes('sourceProjectionConfigText'), false, 'PromptProfile must not expose a free projection contract JSON editor')
  for (const marker of ['options.referenceRenderingFields', 'PromptRenderingTemplateEditor', 'profileForm.renderingTemplates[item.key]',
    'schema_version: 4', 'rendering: {', 'renderingTemplateKey', '服务端参考渲染字段目录为空或重复']) {
    assert.ok(component.includes(marker), `PromptProfile configured rendering misses ${marker}`)
  }
  assert.equal(component.includes('reference_context_policy:'), false, 'new policies must not emit the retired built-in rendering configuration')
  assert.equal(component.includes('request.h3_ref2va.'), false, 'source options must come from the server catalog')
  assert.equal(component.includes('v-for="item in options.sourceProjectionPrimitives"'), false, 'primitive options must be filtered by the Profile configured engine')
  for (const selector of ['project.core', 'project.style', 'target_shot.core', 'target_shot.dialogue_metadata', 'target_shot.dialogue_authored_text', 'target_shot.linked_bible', 'adjacent_shots.core', 'all_shots.core']) {
    assert.equal(component.includes(`'${selector}'`), false, `PromptProfileSettings must read ${selector} from options API instead of hard-coding it`)
  }
})

test('uncertain resolution UIs use safe projections, staff actions and modal-local errors', () => {
  const production = readFileSync(join(frontendRoot, 'src', 'views', 'ProductionRunView.vue'), 'utf8')
  const settings = readFileSync(join(frontendRoot, 'src', 'views', 'SettingsView.vue'), 'utf8')
  for (const marker of ['listGenerationAttempts', 'auth.user?.isStaff', 'uncertainResolutionError', 'resolveProductionRunUncertain', 'requestSnapshotSha256']) {
    assert.ok(production.includes(marker), `ProductionRun resolver misses ${marker}`)
  }
  for (const marker of ['unresolvedSubmission', 'auth.user?.isStaff', 'smokeResolutionError', 'resolveDeploymentSmokeUncertain', '不代表烟测通过']) {
    assert.ok(settings.includes(marker), `deployment smoke resolver misses ${marker}`)
  }
  assert.equal(/uncertainResolutionAttempt\.requestSnapshot(?!Sha256)/.test(production), false)
  assert.equal(settings.includes('unresolvedSubmission.requestPayload'), false)
})

test('settings deployment smoke operations include every supported production capability', () => {
  const settings = readFileSync(join(frontendRoot, 'src', 'views', 'SettingsView.vue'), 'utf8')
  for (const capability of ['image.text_to_image', 'video.text_to_video', 'audio.text_to_audio', 'prompt.normalize', 'script.generate', 'script.structure', 'visual_rules.generate']) {
    assert.ok(settings.includes(`'${capability}'`), `SettingsView deployment smoke operations miss ${capability}`)
  }
  assert.ok(settings.includes('profileFor(item.profileVersion)?.specification.capabilities'))
  assert.ok(settings.includes('actionSmokeOperations'))
  const routingLoad = settings.slice(
    settings.indexOf("else if (requestedTab === 'routing')"),
    settings.indexOf("else if (requestedTab === 'defaults')"),
  )
  assert.ok(routingLoad.includes('platformApi.listModelProfiles()'), 'routing must load model profiles before capability filtering')
  assert.ok(routingLoad.includes('modelProfiles.value = value'), 'routing must retain model profiles for typed route options')
})

test('async deployment smoke and lifecycle audit preserve durable server facts', async () => {
  const vite = await server()
  try {
    const adapters = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
    const attempt = adapters.adaptModelDeploymentSmokeAttempt({
      id: 'attempt-1', deployment: 'deployment-1', operation: 'image.text_to_image',
      status: 'submitted', request_sha256: 'a'.repeat(64), target_sha256: 'b'.repeat(64),
      provider_request_id_sha256: 'c'.repeat(64), poll_count: 3,
      remote_status: 'processing', error_code: '', error_message: '',
      created_at: '2026-08-23T00:00:00Z', updated_at: '2026-08-23T00:00:03Z',
    })
    assert.equal(attempt.status, 'submitted')
    assert.equal(attempt.pollCount, 3)
    assert.equal(attempt.providerRequestIdSha256, 'c'.repeat(64))
    const audit = adapters.adaptPlatformLifecycleAuditEvent({
      id: 'audit-1', actor: 'staff-1', actor_username: 'reviewer', action: 'retire',
      object_type: 'model_profile_version', object_id: 'model-1', reason_code: 'planned_change',
      before_sha256: 'd'.repeat(64), after_sha256: 'e'.repeat(64),
      before_summary: { status: 'published' }, after_summary: { status: 'retired' },
      created_at: '2026-08-23T00:00:00Z',
    })
    assert.equal(audit.actorUsername, 'reviewer')
    assert.equal(audit.reasonCode, 'planned_change')
    assert.deepEqual(audit.afterSummary, { status: 'retired' })
  } finally { await vite.close() }

  const settings = readFileSync(join(frontendRoot, 'src', 'views', 'SettingsView.vue'), 'utf8')
  const api = readFileSync(join(frontendRoot, 'src', 'services', 'platformApi.ts'), 'utf8')
  for (const marker of [
    'const attempt = await platformApi.smokeTestDeployment', 'deploymentSmokeTerminalStatuses',
    'restoreDeploymentSmokeAttempts', 'refreshDeploymentSmokeAttempts',
    'cancelDeploymentSmoke', 'listPlatformLifecycleAuditEvents',
    'filteredLifecycleAudit', 'actionReasonRequired',
  ]) assert.ok(settings.includes(marker), `SettingsView misses ${marker}`)
  for (const marker of [
    'response.status !== 202', 'getDeploymentSmokeAttempt',
    'listDeploymentSmokeAttempts', 'cancelDeploymentSmokeAttempt',
    'X-Audit-Reason-Code', 'listPlatformLifecycleAuditEvents',
  ]) assert.ok(api.includes(marker), `platformApi misses ${marker}`)
  assert.ok(settings.includes('真实烟测任务已创建'))
  assert.ok(settings.includes('页面将持续读取持久状态'))
})

test('runtime host probes are durable and expose trusted artifact facts without paths', async () => {
  const vite = await server()
  try {
    const adapters = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
    const attempt = adapters.adaptRuntimeHostProbeAttempt({
      id: 'probe-attempt-1', host: 'host-1', created_by: 'admin-1', trigger: 'manual',
      status: 'running', worker_id: 'maintenance-1', heartbeat_at: '2026-08-23T12:00:00Z',
      progress_marker: 'phase:probing', final_snapshot: null,
      created_at: '2026-08-23T11:59:00Z', updated_at: '2026-08-23T12:00:00Z',
    })
    assert.equal(attempt.host, 'host-1')
    assert.equal(attempt.status, 'running')
    assert.equal(attempt.progressMarker, 'phase:probing')
    const snapshot = adapters.adaptHostSnapshot({
      id: 'snapshot-1', host: 'host-1', model_availability: [{
        id: 'fact-1', deployment: 'deployment-1', artifact: 'artifact-1',
        availability_key: 'artifact-1', status: 'available', reason: 'verified',
        observed_size_bytes: 75000000000, observed_sha256: 'a'.repeat(64),
        observation_source: 'local_filesystem',
      }], created_at: '', updated_at: '',
    })
    assert.equal(snapshot.modelAvailability[0].observedSizeBytes, 75000000000)
    assert.equal(snapshot.modelAvailability[0].observedSha256, 'a'.repeat(64))
    assert.equal(snapshot.modelAvailability[0].observationSource, 'local_filesystem')
    assert.equal('path' in snapshot.modelAvailability[0], false)
  } finally { await vite.close() }

  const settings = readFileSync(join(frontendRoot, 'src', 'views', 'SettingsView.vue'), 'utf8')
  const api = readFileSync(join(frontendRoot, 'src', 'services', 'platformApi.ts'), 'utf8')
  for (const marker of [
    'restoreRuntimeProbeAttempts', 'refreshRuntimeProbeAttempts', 'scheduleRuntimeProbePoll',
    'cancelRuntimeProbe', 'runtimeProbeActiveStatuses', 'observedSizeBytes',
    'observedSha256', '复制完整 SHA',
  ]) assert.ok(settings.includes(marker), `SettingsView misses ${marker}`)
  for (const marker of [
    'createRuntimeHostProbe', 'listRuntimeHostProbeAttempts',
    'getRuntimeHostProbeAttempt', 'cancelRuntimeHostProbeAttempt',
  ]) assert.ok(api.includes(marker), `platformApi misses ${marker}`)
  assert.equal(api.includes('async probeRuntime('), false)
  assert.equal(settings.includes('fact.path'), false)
})

test('settings presents global recipe readiness in routing and host facts from overview to drill-down', () => {
  const settings = readFileSync(join(frontendRoot, 'src', 'views', 'SettingsView.vue'), 'utf8')
  const routingLoad = settings.slice(
    settings.indexOf("else if (requestedTab === 'routing')"),
    settings.indexOf("else if (requestedTab === 'defaults')"),
  )
  const runtimeLoad = settings.slice(
    settings.indexOf('const loadRuntime = async'),
    settings.indexOf('const load = async'),
  )
  const routingTemplate = settings.slice(
    settings.indexOf(`<template v-else-if="tab==='routing'">`),
    settings.indexOf(`<template v-else-if="tab==='defaults'||tab==='security'">`),
  )
  const runtimeTemplate = settings.slice(
    settings.indexOf(`<template v-else-if="tab==='runtime'">`),
    settings.indexOf('<template v-else>', settings.indexOf(`<template v-else-if="tab==='runtime'">`)),
  )

  assert.ok(routingLoad.includes('platformApi.getProductionReadiness()'))
  assert.equal(runtimeLoad.includes('platformApi.getProductionReadiness()'), false)
  assert.ok(routingTemplate.includes('配方生产就绪概览'))
  assert.ok(routingTemplate.includes('全部仍处于 published'))
  assert.equal(runtimeTemplate.includes('production-readiness'), false)
  assert.ok(routingTemplate.indexOf('配方生产就绪概览') < routingTemplate.indexOf('生产配方（Production Recipe）'))
  assert.ok(routingTemplate.indexOf('配方生产就绪概览') < routingTemplate.indexOf('<SettingsListFilter'))

  const recipeCardTag = routingTemplate.match(/<details[^>]*v-for="item in recipeListState\.items"[^>]*>/)?.[0] || ''
  assert.ok(recipeCardTag)
  assert.equal(/\sopen(?:\s|>|=)/.test(recipeCardTag), false)

  const overview = runtimeTemplate.indexOf('运行主机总体情况')
  const probe = runtimeTemplate.indexOf('主机探测与推荐')
  const hosts = runtimeTemplate.indexOf('事实 · 运行主机')
  const snapshot = runtimeTemplate.indexOf('事实 · 所选主机最近探测')
  assert.ok(overview >= 0 && overview < probe && probe < hosts && hosts < snapshot)
  assert.ok(runtimeTemplate.includes('runtimeProbeDurationLabel(selectedProbeAttempt)'))

  const evidenceTag = runtimeTemplate.match(/<details[^>]*class="runtime-model-facts"[^>]*>/)?.[0] || ''
  assert.ok(evidenceTag)
  assert.equal(/\sopen(?:\s|>|=)/.test(evidenceTag), false)
})

test('production recipe script mode switches AI and confirmed revision inputs', async () => {
  const vite = await server()
  try {
    const adapters = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
    assert.equal(adapters.productionRecipeScriptMode({ stages: [
      { key: 'script_generate', mode: 'ai' },
      { key: 'script_review', mode: 'review' },
      { key: 'script_normalize', mode: 'ai' },
      { key: 'script_normalize_review', mode: 'review' },
    ] }), 'ai')
    assert.equal(adapters.productionRecipeScriptMode({ stages: [
      { key: 'script_input', mode: 'confirmed_revision' },
      { key: 'script_structure', mode: 'ai' },
      { key: 'script_review', mode: 'review' },
    ] }), 'confirmed_revision')
    assert.equal(adapters.productionRecipeScriptMode({ stages: [
      { key: 'script_input', mode: 'manual' },
      { key: 'media_generation', mode: 'existing_pipeline' },
    ] }), 'downstream')
    assert.equal(adapters.productionRecipeScriptMode({ stages: [
      { key: 'script_input', mode: 'manual' },
      { key: 'script_normalize', mode: 'manual' },
      { key: 'script_normalize_review', mode: 'review' },
      { key: 'media_generation', mode: 'existing_pipeline' },
    ] }), 'downstream')
    for (const definition of [
      { stages: [] },
      { stages: [{ key: 'media_generation', mode: 'existing_pipeline' }] },
      { stages: [{ key: 'script_input', mode: 'unknown' }] },
      { stages: [{ key: 'script_generate', mode: 'ai' }] },
      { stages: [
        { key: 'script_generate', mode: 'ai' },
        { key: 'script_structure', mode: 'ai' },
        { key: 'script_review', mode: 'review' },
      ] },
      { stages: [
        { key: 'script_input', mode: 'confirmed_revision' },
        { key: 'script_structure', mode: 'manual' },
        { key: 'script_review', mode: 'review' },
      ] },
      { stages: [
        { key: 'script_input', mode: 'confirmed_revision' },
        { key: 'script_structure', mode: 'ai' },
      ] },
      { stages: [
        { key: 'script_generate', mode: 'ai' },
        { key: 'script_review', mode: 'review' },
        { key: 'script_normalize', mode: 'ai' },
      ] },
      { stages: [
        { key: 'script_input', mode: 'confirmed_revision' },
        { key: 'script_normalize', mode: 'wrong' },
        { key: 'script_normalize_review', mode: 'review' },
      ] },
      { stages: [
        { key: 'script_generate', mode: 'ai' },
        { key: 'script_review', mode: 'review' },
        { key: 'script_input', mode: 'confirmed_revision' },
      ] },
    ]) assert.equal(adapters.productionRecipeScriptMode(definition), 'invalid')
    assert.equal(adapters.productionRecipeInteractionMode({ schema_version: 1, stages: [] }), 'assisted')
    assert.equal(adapters.productionRecipeInteractionMode({ schema_version: 2, interaction_mode: 'unattended', stages: [] }), 'unattended')
    assert.equal(adapters.productionRecipeInteractionMode({ schema_version: 2, interaction_mode: 'assisted', stages: [] }), 'invalid')
    assert.equal(adapters.productionRecipeInteractionMode({ schema_version: 1, interaction_mode: 'unattended', stages: [] }), 'invalid')
  } finally { await vite.close() }
})

test('artifact comparison enforces 2-4 candidates, real differences, and synchronized video state', async () => {
  const vite = await server()
  try {
    const comparison = await vite.ssrLoadModule('/src/services/artifactComparison.ts')
    const artifacts = Array.from({ length: 6 }, (_, index) => ({
      id: `artifact-${index + 1}`,
      status: index === 5 ? 'selected' : 'available',
    }))
    const reconciled = comparison.reconcileArtifactSelection(
      artifacts,
      ['artifact-1', 'artifact-2', 'artifact-3', 'artifact-4'],
    )
    assert.deepEqual(reconciled, ['artifact-6', 'artifact-1', 'artifact-2', 'artifact-3'])
    assert.deepEqual(comparison.toggleArtifactSelection(reconciled, 'artifact-4', true, 6), reconciled)
    const three = comparison.toggleArtifactSelection(reconciled, 'artifact-1', false, 6)
    assert.equal(three.length, 3)
    const two = comparison.toggleArtifactSelection(three, 'artifact-2', false, 6)
    assert.equal(two.length, 2)
    assert.deepEqual(comparison.toggleArtifactSelection(two, 'artifact-3', false, 6), two)

    assert.deepEqual(comparison.comparisonParameterRows([
      { schema_version: 1, model: 'model-a', generation: { seed: 9, steps: 24 } },
      { schema_version: 1, model: 'model-b', generation: { seed: 9, steps: 30 } },
    ]), [
      { key: 'model', values: ['"model-a"', '"model-b"'] },
      { key: 'generation.steps', values: ['24', '30'] },
    ])
    const manyDifferences = Object.fromEntries(Array.from({ length: 35 }, (_, index) => [`key_${index}`, index]))
    assert.equal(comparison.comparisonParameterRows([manyDifferences, {}]).length, 35)

    let playCount = 0
    let pauseCount = 0
    const source = { currentTime: 4.25, paused: false, play: async () => undefined, pause: () => undefined }
    const peer = {
      currentTime: 0,
      paused: true,
      play: async () => { playCount += 1; peer.paused = false },
      pause: () => { pauseCount += 1; peer.paused = true },
    }
    const videos = new Map([['source', source], ['peer', peer]])
    await comparison.synchronizeVideos('source', 'play', videos)
    assert.equal(peer.currentTime, 4.25)
    assert.equal(playCount, 1)
    source.currentTime = 8
    await comparison.synchronizeVideos('source', 'seek', videos)
    assert.equal(peer.currentTime, 8)
    await comparison.synchronizeVideos('source', 'pause', videos)
    assert.equal(pauseCount, 1)
  } finally { await vite.close() }
})

test('runtime evidence is host-bound and stale recommendations stay disabled', async () => {
  const vite = await server()
  try {
    const { runtimeEvidenceState } = await vite.ssrLoadModule('/src/services/runtimeHostSelection.ts')
    const now = Date.parse('2026-08-23T12:00:00Z')
    const snapshot = { id: 'snapshot-a', host: 'host-a', expiresAt: '2026-08-23T12:05:00Z' }
    const readiness = { snapshotId: 'snapshot-a' }
    const recommendation = { hostSnapshot: 'snapshot-a' }
    assert.deepEqual(runtimeEvidenceState('host-a', snapshot, readiness, recommendation, now), {
      snapshotCurrent: true, readinessCurrent: true, recommendationCurrent: true,
    })
    assert.deepEqual(runtimeEvidenceState('host-b', snapshot, readiness, recommendation, now), {
      snapshotCurrent: false, readinessCurrent: false, recommendationCurrent: false,
    })
    assert.equal(runtimeEvidenceState('host-a', snapshot, readiness, recommendation, Date.parse('2026-08-23T12:06:00Z')).recommendationCurrent, false)
    assert.equal(runtimeEvidenceState('host-a', snapshot, readiness, { hostSnapshot: 'snapshot-old' }, now).recommendationCurrent, false)
  } finally { await vite.close() }
})

test('stage disclosure opens only current or explicit user selection and permits full collapse', async () => {
  const vite = await server()
  try {
    const disclosure = await vite.ssrLoadModule('/src/services/productionStageDisclosure.ts')
    const stages = [
      { id: 'historical', stageKey: 'script_input' },
      { id: 'current', stageKey: 'media_generation' },
    ]
    assert.equal(disclosure.defaultExpandedStageId(stages, 'media_generation'), 'current')
    assert.equal(disclosure.reconcileExpandedStageId(stages, 'media_generation', 'historical', true), 'historical')
    assert.equal(disclosure.reconcileExpandedStageId(stages, 'media_generation', '', true), '')
    assert.equal(disclosure.reconcileExpandedStageId(stages, 'media_generation', 'missing', true), 'current')
  } finally { await vite.close() }
})

test('ProductionRun request payload has one authoritative shape for each script mode', async () => {
  const vite = await server()
  try {
    const { buildProductionRunRequestPayload, plannedAverageShotDurationMs } = await vite.ssrLoadModule('/src/services/productionRunPayload.ts')
    const {
      createUnattendedReferenceInputDrafts,
      selectedUnattendedReferenceInputs,
      unattendedReferenceInputDraftError,
    } = await vite.ssrLoadModule('/src/services/unattendedReferenceInputs.ts')
    const common = {
      projectId: 'project-1', recipeVersionId: 'recipe-1', idempotencyKey: 'request-1',
      allowExternalCalls: true, budgetLimit: '20.0000', maxGenerationTasks: 42, configOverrides: { seed: 9 },
      scriptRevisionId: 'revision-should-be-omitted', idea: '岛上的谜案', styleHint: '黑色电影',
      episodeCount: 6, shotsPerEpisode: 8, episodeDurationSeconds: 90,
      episodeIds: ['episode-b', 'episode-a', 'episode-b'],
      effectiveLimits: { maxRoutingRoutes: 15, maxReferenceBindings: 64 },
    }
    const ai = buildProductionRunRequestPayload({ ...common, scriptMode: 'ai' })
    assert.equal(plannedAverageShotDurationMs(1, 1), 1000)
    assert.equal(plannedAverageShotDurationMs(3, 10), 3333)
    assert.throws(() => plannedAverageShotDurationMs(0, 1), /每集镜数必须是正整数/)
    assert.deepEqual(ai, {
      project_id: 'project-1', recipe_version_id: 'recipe-1', idempotency_key: 'request-1',
      allow_external_calls: true, budget_limit: '20.0000', max_generation_tasks: 42,
      config_overrides: { seed: 9 }, idea: '岛上的谜案', style_hint: '黑色电影',
      episode_count: 6, shots_per_episode: 8, episode_duration_seconds: 90,
    })
    assert.equal('script_revision_id' in ai, false)
    assert.equal('episode_ids' in ai, false)

    const unattended = buildProductionRunRequestPayload({
      ...common,
      scriptMode: 'ai',
      unattendedConfirmed: true,
      referenceInputs: [{
        assetId: 'reference-video-1',
        purpose: 'orbit_rhythm_v7',
        useEmbeddedAudio: false,
      }],
    })
    assert.equal(unattended.unattended_confirmed, true)
    assert.deepEqual(unattended.reference_inputs, [{
      asset_id: 'reference-video-1',
      purpose: 'orbit_rhythm_v7',
      use_embedded_audio: false,
    }])
    const multipleReferences = buildProductionRunRequestPayload({
      ...common,
      scriptMode: 'ai',
      unattendedConfirmed: true,
      referenceInputs: [
        {
          assetId: 'reference-video-1',
          purpose: 'motion_camera_reference',
          useEmbeddedAudio: false,
        },
        {
          assetId: 'reference-video-2',
          purpose: 'audio_reference',
          useEmbeddedAudio: true,
        },
      ],
    })
    assert.deepEqual(multipleReferences.reference_inputs, [
      {
        asset_id: 'reference-video-1',
        purpose: 'motion_camera_reference',
        use_embedded_audio: false,
      },
      {
        asset_id: 'reference-video-2',
        purpose: 'audio_reference',
        use_embedded_audio: true,
      },
    ])
    assert.throws(() => buildProductionRunRequestPayload({
      ...common,
      scriptMode: 'ai',
      unattendedConfirmed: true,
      referenceInputs: Array.from({ length: 65 }, (_, index) => ({
        assetId: `reference-video-${index}`,
        purpose: 'motion_camera_reference',
        useEmbeddedAudio: false,
      })),
    }), /服务端有效绑定上限 64 条/)
    assert.throws(() => buildProductionRunRequestPayload({
      ...common,
      scriptMode: 'ai',
      unattendedConfirmed: false,
      referenceInputs: [],
    }), /显式确认/)
    const optionalReference = buildProductionRunRequestPayload({
      ...common,
      scriptMode: 'ai',
      unattendedConfirmed: true,
      referenceInputs: [],
    })
    assert.deepEqual(optionalReference.reference_inputs, [])

    const drafts = createUnattendedReferenceInputDrafts(
      { minimumItems: 2, maximumItems: 3 },
      'motion_camera_reference',
    )
    drafts[0].assetId = 'reference-video-1'
    drafts[1].assetId = 'reference-video-2'
    drafts[1].purpose = 'audio_reference'
    drafts[1].useEmbeddedAudio = true
    const topology = {
      schemaVersion: 1,
      videos: {
        minimumItems: 0,
        maximumItems: 3,
        defaultPurposeKey: 'motion_camera_reference',
        purposes: [
          { key: 'motion_camera_reference', label: '运镜参考', description: '', minimumItems: 0, maximumItems: 3 },
          { key: 'audio_reference', label: '音频参考', description: '', minimumItems: 0, maximumItems: 1 },
        ],
      },
      audios: { minimumItems: 0, maximumItems: 0, defaultPurposeKey: null, purposes: [] },
      preprocessing: { videos: null, pairedVideoAudios: null, referenceAudios: null },
      pairingRules: [{
        key: 'embedded-audio', label: '内嵌音轨', description: '',
        sourceMediaKind: 'video', targetMediaKind: 'audio',
        sourcePurposeKeys: ['audio_reference'], targetPurposeKeys: ['reference_audio'],
        defaultTargetPurposeKey: 'reference_audio', minimumTargetsPerSource: 1,
        maximumTargetsPerSource: 1, allowEmbeddedSource: true,
        allowExternalTarget: false, sourceAndExternalMutuallyExclusive: true,
      }],
      mutualExclusionRules: [],
    }
    assert.equal(unattendedReferenceInputDraftError({
      drafts,
      cardinality: { minimumItems: 2, maximumItems: 3 },
      availableAssetIds: new Set(['reference-video-1', 'reference-video-2']),
      topology,
    }), '')
    assert.deepEqual(selectedUnattendedReferenceInputs(drafts), [
      { assetId: 'reference-video-1', purpose: 'motion_camera_reference', useEmbeddedAudio: false },
      { assetId: 'reference-video-2', purpose: 'audio_reference', useEmbeddedAudio: true },
    ])
    drafts[1].assetId = 'reference-video-1'
    assert.match(unattendedReferenceInputDraftError({
      drafts,
      cardinality: { minimumItems: 2, maximumItems: 3 },
      availableAssetIds: new Set(['reference-video-1', 'reference-video-2']),
      topology,
    }), /不能重复选择/)

    const confirmed = buildProductionRunRequestPayload({ ...common, scriptMode: 'confirmed_revision', scriptRevisionId: 'revision-1' })
    assert.equal(confirmed.script_revision_id, 'revision-1')
    for (const key of ['idea', 'style_hint', 'episode_count', 'shots_per_episode', 'episode_duration_seconds', 'episode_ids']) assert.equal(key in confirmed, false)

    const structuredImport = buildProductionRunRequestPayload({
      ...common,
      scriptMode: 'confirmed_revision',
      scriptRevisionId: 'revision-1',
      structuredImportPlan: true,
      episodeCount: 1,
      shotsPerEpisode: 1,
      episodeDurationSeconds: 1,
    })
    assert.deepEqual(structuredImport, {
      project_id: 'project-1', recipe_version_id: 'recipe-1', idempotency_key: 'request-1',
      allow_external_calls: true, budget_limit: '20.0000', max_generation_tasks: 42,
      config_overrides: { seed: 9 }, script_revision_id: 'revision-1',
      episode_count: 1, shots_per_episode: 1, episode_duration_seconds: 1,
    })
    assert.throws(() => buildProductionRunRequestPayload({
      ...common,
      scriptMode: 'confirmed_revision',
      scriptRevisionId: 'revision-1',
      structuredImportPlan: true,
      shotsPerEpisode: 0,
    }), /每集镜数必须是正整数/)

    const seriesStudioView = readFileSync(join(frontendRoot, 'src', 'views', 'SeriesStudioView.vue'), 'utf8')
    const creationModeIndex = seriesStudioView.indexOf('<div class="form-section"><div class="section-number">01</div>')
    const structuredPlanIndex = seriesStudioView.indexOf('class="form-section onboarding-structure-plan-section"')
    const workflowIndex = seriesStudioView.indexOf('class="form-section onboarding-workflow-section"')
    assert.ok(creationModeIndex >= 0 && structuredPlanIndex > creationModeIndex)
    assert.ok(workflowIndex > structuredPlanIndex)
    assert.match(seriesStudioView, /structuredImportPlan:\s*scriptMode === 'confirmed_revision' && selectedCreationRecipeSummary\.value\.hasStructure/)
    assert.match(seriesStudioView, /stage\.key === 'script_structure' && stage\.mode === 'ai'/)

    const downstream = buildProductionRunRequestPayload({ ...common, scriptMode: 'downstream' })
    assert.deepEqual(downstream.episode_ids, ['episode-a', 'episode-b'])
    for (const key of ['script_revision_id', 'idea', 'style_hint', 'episode_count', 'shots_per_episode', 'episode_duration_seconds']) assert.equal(key in downstream, false)

    const uniform = buildProductionRunRequestPayload({
      ...common,
      scriptMode: 'downstream',
      workflowSelection: {
        schemaVersion: 2,
        mode: 'uniform',
        routeIndex: 1,
        promptProfileSelections: [{
          operation: 'video.generate', mode: 'T2VA', promptProfileBindingId: 'binding-video',
        }],
      },
      bibleImagePromptProfileBindingId: 'binding-bible',
    })
    assert.deepEqual(uniform.workflow_selection, {
      schema_version: 2, mode: 'uniform', route_index: 1, shot_routes: [],
      prompt_profile_selections: [{
        operation: 'video.generate', mode: 'T2VA', prompt_profile_binding_id: 'binding-video',
      }],
    })
    assert.equal(uniform.bible_image_prompt_profile_binding_id, 'binding-bible')
    const perShot = buildProductionRunRequestPayload({
      ...common,
      scriptMode: 'downstream',
      workflowSelection: {
        schemaVersion: 2,
        mode: 'per_shot',
        shotRoutes: [
          { shotId: 'shot-a', routeIndex: 0, promptProfileBindingId: 'binding-a' },
          { shotId: 'shot-b', routeIndex: 1, promptProfileBindingId: 'binding-b' },
        ],
      },
    })
    assert.deepEqual(perShot.workflow_selection, {
      schema_version: 2,
      mode: 'per_shot',
      route_index: null,
      prompt_profile_selections: [],
      shot_routes: [
        { shot_id: 'shot-a', route_index: 0, prompt_profile_binding_id: 'binding-a' },
        { shot_id: 'shot-b', route_index: 1, prompt_profile_binding_id: 'binding-b' },
      ],
    })
    assert.throws(() => buildProductionRunRequestPayload({
      ...common,
      scriptMode: 'downstream',
      workflowSelection: {
        schemaVersion: 2,
        mode: 'per_shot',
        shotRoutes: [
          { shotId: 'shot-a', routeIndex: 0, promptProfileBindingId: 'binding-a' },
          { shotId: 'shot-a', routeIndex: 1, promptProfileBindingId: 'binding-b' },
        ],
      },
    }), /不能重复/)
  } finally { await vite.close() }
})

test('review payload parsers require immutable source snapshots and hashes', async () => {
  const vite = await server()
  try {
    const adapters = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
    const review = await vite.ssrLoadModule('/src/services/productionRunReview.ts')
    const revisionId = '11111111-1111-4111-8111-111111111111'
    const artifactId = '22222222-2222-4222-8222-222222222222'
    const attemptId = '33333333-3333-4333-8333-333333333333'
    const contentHash = 'a'.repeat(64)
    const stageHash = 'b'.repeat(64)
    const scriptRun = adapters.adaptProductionRun({
      id: 'run-1', created_at: '', updated_at: '', stages: [{
        id: 'stage-script', stage_key: 'script_generate', ordinal: 1, status: 'succeeded',
        created_at: '', updated_at: '', output_snapshot_sha256: stageHash,
        output_snapshot: {
          stage_key: 'script_generate', canonical: { id: revisionId, content_sha256: contentHash },
          review_payload: {
            kind: 'generated_script_preview', script_revision_id: revisionId, artifact_id: artifactId,
            attempt_id: attemptId, content_sha256: contentHash, result_sha256: 'c'.repeat(64),
            title: '潮汐', logline: '岛上失踪案。', warnings: ['结尾需复核'],
          },
        },
      }],
    })
    const parsedScript = review.parseScriptReviewPayload(scriptRun.stages)
    assert.equal(parsedScript.error, '')
    assert.equal(parsedScript.value.scriptRevisionId, revisionId)
    assert.equal(parsedScript.value.snapshotSha256, stageHash)
    assert.equal(parsedScript.value.sourceStageKey, 'script_generate')

    scriptRun.stages[0].outputSnapshot.review_payload.kind = 'other_preview'
    assert.match(review.parseScriptReviewPayload(scriptRun.stages).error, /kind/)

    const structuredRun = adapters.adaptProductionRun({
      id: 'run-structured', created_at: '', updated_at: '', stages: [{
        id: 'stage-structured', stage_key: 'script_structure', ordinal: 2, status: 'succeeded',
        created_at: '', updated_at: '', output_snapshot_sha256: '1'.repeat(64),
        output_snapshot: {
          stage_key: 'script_structure', canonical: { id: revisionId, content_sha256: contentHash },
          review_payload: {
            kind: 'structured_script_preview', submission_semantics: 'no_submission',
            script_revision_id: revisionId, artifact_id: artifactId, attempt_id: null,
            content_sha256: contentHash, result_sha256: '', title: '导入剧本', logline: '原稿已符合正式结构。',
          },
        },
      }],
    })
    const parsedStructured = review.parseScriptReviewPayload(structuredRun.stages)
    assert.equal(parsedStructured.error, '')
    assert.equal(parsedStructured.value.sourceStageKey, 'script_structure')
    assert.equal(parsedStructured.value.attemptId, null)
    assert.equal(parsedStructured.value.submissionSemantics, 'no_submission')

    const candidate = (entityType, suffix) => ({
      candidate_id: `candidate-${suffix}`, entity_type: entityType, name: `名称-${suffix}`,
      description: '描述', visual_identity_en: 'visual identity', occurrence: 1, recommended: true,
      reviewed_content_sha256: suffix.repeat(64),
    })
    const bibleRun = adapters.adaptProductionRun({
      id: 'run-2', created_at: '', updated_at: '', stages: [{
        id: 'stage-bible', stage_key: 'bible_materialize', ordinal: 2, status: 'succeeded',
        created_at: '', updated_at: '', output_snapshot_sha256: 'd'.repeat(64),
        output_snapshot: {
          stage_key: 'bible_materialize', review_payload: {
            kind: 'script_bible_candidates', script_revision_id: revisionId,
            script_content_sha256: contentHash, source_fingerprint: 'e'.repeat(64), candidate_sha256: 'f'.repeat(64),
            collections: {
              characters: [candidate('character', '1')], scenes: [candidate('scene', '2')], props: [candidate('prop', '3')],
            },
            diagnostics: [{ code: 'parsed', severity: 'info' }],
          },
        },
      }],
    })
    const parsedBible = review.parseBibleReviewPayload(bibleRun.stages)
    assert.equal(parsedBible.error, '')
    assert.equal(parsedBible.value.collections.characters[0].name, '名称-1')
    assert.deepEqual(parsedBible.value.diagnostics, [{ code: 'parsed', severity: 'info' }])
    bibleRun.stages[0].outputSnapshotSha256 = ''
    assert.match(review.parseBibleReviewPayload(bibleRun.stages).error, /hash/)

    const reviewLimits = { maxReferenceBindings: 64, promptReviewMaxCandidates: 100, promptReviewMaxAdvisories: 200, promptReviewMaxInputBytes: 512 * 1024, promptReviewMaxFieldCharacters: 30_000, promptReviewMaxUsageCount: 1_000_000_000 }
    const storyboardRun = adapters.adaptProductionRun({ id: 'run-storyboard', stages: [{
      id: 'stage-storyboard', stage_key: 'storyboard', ordinal: 3, status: 'succeeded', created_at: '', updated_at: '',
      output_snapshot_sha256: '7'.repeat(64), output_snapshot: {
        stage_key: 'storyboard', formal_object_type: 'storyboard', canonical_sha256: '8'.repeat(64),
        canonical: { episodes: [{ id: revisionId, number: 1, title: '第一集', shots: [
          { id: artifactId, number: 1, title: '定场', duration_ms: 2500, media_type: 'image', conditioning_mode: '', include_audio: false, character_ids: [], visual_reference_character_ids: [], scene_id: null, prop_ids: [], reference_bindings: {}, requires_first_frame_continuity: false, requires_last_frame_continuity: false },
          { id: attemptId, number: 2, title: '推进', duration_ms: 5000, media_type: 'video', conditioning_mode: 'vendor.frame', conditioning_mode_capability: { key: 'vendor.frame', label: '首帧模式', description: '', input_kind: 'frame_slots', requires_entity_reference_bible: false, supports_native_audio: true, inputs: { total_images: { minimum_items: 0, maximum_items: 2 }, first_frame: { minimum_items: 0, maximum_items: 1 }, last_frame: { minimum_items: 0, maximum_items: 1 }, entity_images: { minimum_items: 0, maximum_items: 0 }, reference_videos: { minimum_items: 0, maximum_items: 0 }, paired_video_audios: { minimum_items: 0, maximum_items: 0 }, reference_audios: { minimum_items: 0, maximum_items: 0 } } }, include_audio: true, character_ids: [], visual_reference_character_ids: [], scene_id: null, prop_ids: [], reference_bindings: {}, requires_first_frame_continuity: false, requires_last_frame_continuity: false },
        ] }] },
      },
    }] })
    const storyboard = review.parseStoryboardReviewPayload(storyboardRun.stages, reviewLimits)
    assert.equal(storyboard.error, '')
    assert.deepEqual({ shots: storyboard.value.shotCount, images: storyboard.value.imageCount, videos: storyboard.value.videoCount, audio: storyboard.value.audioShotCount, duration: storyboard.value.durationMs }, { shots: 2, images: 1, videos: 1, audio: 1, duration: 7500 })
    assert.deepEqual(storyboard.value.episodes[0].shots[1].conditioningModeCapability.inputs.referenceVideos, { minimumItems: 0, maximumItems: 0 })
    storyboardRun.stages[0].outputSnapshot.canonical_sha256 = ''
    assert.match(review.parseStoryboardReviewPayload(storyboardRun.stages, reviewLimits).error, /hash/)

    const normalizationRun = adapters.adaptProductionRun({ id: 'run-normalization', stages: [{
      id: 'stage-normalization', stage_key: 'script_normalize', ordinal: 4, status: 'succeeded', created_at: '', updated_at: '',
      output_snapshot_sha256: '9'.repeat(64), output_snapshot: {
        stage_key: 'script_normalize', review_payload: {
          kind: 'prompt_normalization_candidates', attempt_id: attemptId, result_sha256: '6'.repeat(64),
          provider: 'deepseek', model: 'deepseek-chat', submission_semantics: 'durable_single_submit_manual_resolution',
          advisories: [
            { schema_version: 1, code: 'recommended_input_bytes_exceeded', severity: 'warning', measured: 25093, recommended_maximum: 16000, hard_maximum: 65536, batch_index: 0 },
            { schema_version: 1, code: 'recommended_output_tokens_exceeded', severity: 'warning', usage_field: 'completion_tokens', measured: 40001, recommended_maximum: 32768, hard_maximum: 131072, batch_index: 0 },
            { schema_version: 1, code: 'recommended_length_out_of_range', severity: 'warning', request_id: 'candidate-character-name', field_key: 'detailed_description', measured: 229, minimum: 350, maximum: 500, length_unit: 'english_words', request_index: 0, batch_index: 0 },
          ],
          candidates: [{
            request_id: 'candidate-character-name', scope_type: 'character', scope_id: revisionId,
            field_key: 'character.name', label: '角色名', source_text: '林雾',
            source_hash: '4'.repeat(64), context_hash: '5'.repeat(64), translated_text: 'S1',
          }],
        },
      },
    }] })
    const normalization = review.parsePromptNormalizationReviewPayload(normalizationRun.stages, reviewLimits)
    assert.equal(normalization.error, '')
    assert.equal(normalization.value.candidates[0].requestId, 'candidate-character-name')
    assert.equal(normalization.value.candidates[0].translatedText, 'S1')
    assert.equal(normalization.value.snapshotSha256, '9'.repeat(64))
    assert.equal(normalization.value.reviewOrigin, 'model_candidate')
    assert.equal(normalization.value.retryPolicy, null)
    assert.equal(normalization.value.advisories.length, 3)
    assert.equal(normalization.value.advisories[0].code, 'recommended_input_bytes_exceeded')

    const humanFallbackRun = adapters.adaptProductionRun({ id: 'run-normalization-human', stages: [{
      id: 'stage-normalization-human', stage_key: 'script_normalize', ordinal: 4, status: 'succeeded', created_at: '', updated_at: '',
      output_snapshot_sha256: '1'.repeat(64), output_snapshot: {
        stage_key: 'script_normalize', review_payload: {
          kind: 'prompt_normalization_candidates', review_origin: 'human_after_validation_retry_exhausted',
          stop_reason: 'retry_limit_reached',
          attempt_id: attemptId, result_sha256: '2'.repeat(64), provider: 'deepseek', model: 'deepseek-chat',
          submission_semantics: 'durable_single_submit_manual_resolution',
          retry_policy: { config_key: 'production.prompt_field_retry_limit', retry_limit: 2, max_attempts: 3, attempts_used: 3 },
          validation_failures: [1, 2, 3].map((attemptNumber) => ({
            attempt_id: attemptNumber === 3 ? attemptId : `${attemptNumber}`.repeat(8) + '-1111-4111-8111-' + `${attemptNumber}`.repeat(12),
            attempt_number: attemptNumber,
            route_index: 0,
            unit_key: 'prompt-normalization:001:model_generated',
            batch_index: 0,
            error_code: 'provider_response_invalid',
            error_message: 'Provider 返回的响应未通过严格合同校验。',
            resolution_id: `${attemptNumber}`.repeat(8) + '-2222-4222-8222-' + `${attemptNumber}`.repeat(12),
            resolution_evidence_sha256: `${attemptNumber}`.repeat(64),
            contract_error_code: 'prompt_field_generation_contract_invalid',
            reason_code: 'length_out_of_range',
            validation_context: {
              schema_version: 1,
              field_key: 'shot.detailed_description',
              measured: 100,
              minimum: 350,
              maximum: 500,
              length_unit: 'english_words',
              request_index: 0,
            },
          })),
          candidates: [{
            request_id: 'candidate-shot-narrative', scope_type: 'shot', scope_id: revisionId,
            field_key: 'shot.narrative', label: '镜头叙事', source_text: '林雾进入旧宅。',
            source_hash: '4'.repeat(64), context_hash: '5'.repeat(64), translated_text: '',
          }],
        },
      },
    }] })
    const humanFallback = review.parsePromptNormalizationReviewPayload(humanFallbackRun.stages, reviewLimits)
    assert.equal(humanFallback.error, '')
    assert.equal(humanFallback.value.reviewOrigin, 'human_after_validation_retry_exhausted')
    assert.equal(humanFallback.value.retryPolicy?.maxAttempts, 3)
    assert.equal(humanFallback.value.stopReason, 'retry_limit_reached')
    assert.equal(humanFallback.value.validationFailures[0].errorCode, 'provider_response_invalid')
    assert.equal(humanFallback.value.validationFailures[0].reasonCode, 'length_out_of_range')
    assert.equal(humanFallback.value.validationFailures[0].validationContext?.requestIndex, 0)
    assert.equal(humanFallback.value.candidates[0].translatedText, '')
    const failureAttemptIds = humanFallback.value.validationFailures.map((failure: { attemptId: string }) => failure.attemptId)
    const retryAttempts = [
      adapters.adaptGenerationAttempt({
        id: failureAttemptIds[0], operation: 'prompt.normalize', attempt_number: 1,
        repair_of_attempt_id: '', retry_feedback_sha256: '', created_at: '', updated_at: '',
      }),
      adapters.adaptGenerationAttempt({
        id: failureAttemptIds[1], operation: 'prompt.normalize', attempt_number: 2,
        repair_of_attempt_id: failureAttemptIds[0], retry_feedback_sha256: 'a'.repeat(64), created_at: '', updated_at: '',
      }),
      adapters.adaptGenerationAttempt({
        id: failureAttemptIds[2], operation: 'prompt.normalize', attempt_number: 3,
        repair_of_attempt_id: failureAttemptIds[1], retry_feedback_sha256: 'b'.repeat(64), created_at: '', updated_at: '',
      }),
    ]
    const repairEvidence = review.parsePromptNormalizationRetryRepairs(retryAttempts, humanFallback.value.validationFailures)
    assert.equal(repairEvidence.error, '')
    assert.deepEqual(repairEvidence.value.map((item: { failureAttemptId: string; retryFeedbackSha256: string }) => [item.failureAttemptId, item.retryFeedbackSha256]), [
      [failureAttemptIds[0], 'a'.repeat(64)],
      [failureAttemptIds[1], 'b'.repeat(64)],
    ])
    retryAttempts[1].retryFeedbackSha256 = 'provider-authored unknown text'
    assert.match(review.parsePromptNormalizationRetryRepairs(retryAttempts, humanFallback.value.validationFailures).error, /反馈 hash 无效/)
    retryAttempts[1].retryFeedbackSha256 = 'a'.repeat(64)
    humanFallbackRun.stages[0].outputSnapshot.review_payload.validation_failures[0].provider_text = 'must never render'
    assert.match(review.parsePromptNormalizationReviewPayload(humanFallbackRun.stages, reviewLimits).error, /未知字段/)
    delete humanFallbackRun.stages[0].outputSnapshot.review_payload.validation_failures[0].provider_text

    humanFallbackRun.stages[0].outputSnapshot.review_payload.stop_reason = 'safe_feedback_unavailable'
    humanFallbackRun.stages[0].outputSnapshot.review_payload.retry_policy.attempts_used = 1
    humanFallbackRun.stages[0].outputSnapshot.review_payload.validation_failures = [
      humanFallbackRun.stages[0].outputSnapshot.review_payload.validation_failures[0],
    ]
    humanFallbackRun.stages[0].outputSnapshot.review_payload.attempt_id = failureAttemptIds[0]
    const safeFeedbackFallback = review.parsePromptNormalizationReviewPayload(humanFallbackRun.stages, reviewLimits)
    assert.equal(safeFeedbackFallback.error, '')
    assert.equal(safeFeedbackFallback.value.stopReason, 'safe_feedback_unavailable')

    const humanOnlyRun = adapters.adaptProductionRun({ id: 'run-normalization-human-only', stages: [{
      id: 'stage-normalization-human-only', stage_key: 'script_normalize', ordinal: 4, status: 'succeeded', created_at: '', updated_at: '',
      output_snapshot_sha256: '7'.repeat(64), output_snapshot: {
        stage_key: 'script_normalize', review_payload: {
          kind: 'prompt_normalization_candidates', review_origin: 'human_only_recipe',
          attempt_id: null, result_sha256: '8'.repeat(64), provider: '', model: '',
          submission_semantics: 'human_only_no_submission',
          candidates: [{
            request_id: 'candidate-shot-human-only', scope_type: 'shot', scope_id: revisionId,
            field_key: 'shot.narrative', label: '镜头叙事', source_text: '林雾进入旧宅。',
            source_hash: '4'.repeat(64), context_hash: '5'.repeat(64), translated_text: '',
          }],
        },
      },
    }] })
    const humanOnly = review.parsePromptNormalizationReviewPayload(humanOnlyRun.stages, reviewLimits)
    assert.equal(humanOnly.error, '')
    assert.equal(humanOnly.value.reviewOrigin, 'human_only_recipe')
    assert.equal(humanOnly.value.submissionSemantics, 'human_only_no_submission')
    assert.equal(humanOnly.value.attemptId, null)
    assert.equal(humanOnly.value.candidates[0].translatedText, '')
    humanOnlyRun.stages[0].outputSnapshot.review_payload.review_origin = 'human_only_external_disabled'
    const legacyHumanOnly = review.parsePromptNormalizationReviewPayload(humanOnlyRun.stages, reviewLimits)
    assert.equal(legacyHumanOnly.error, '')
    assert.equal(legacyHumanOnly.value.reviewOrigin, 'human_only_external_disabled')
    humanOnlyRun.stages[0].outputSnapshot.review_payload.submission_semantics = 'no_submission'
    assert.match(review.parsePromptNormalizationReviewPayload(humanOnlyRun.stages, reviewLimits).error, /来源与提交语义不匹配/)

    const noOpRun = adapters.adaptProductionRun({ id: 'run-normalization-no-op', stages: [{
      id: 'stage-normalization-no-op', stage_key: 'script_normalize', ordinal: 4, status: 'succeeded', created_at: '', updated_at: '',
      output_snapshot_sha256: '3'.repeat(64), output_snapshot: {
        stage_key: 'script_normalize', review_payload: {
          kind: 'prompt_normalization_candidates', attempt_id: null, result_sha256: '2'.repeat(64),
          provider: '', model: '', submission_semantics: 'no_submission', candidates: [],
        },
      },
    }] })
    const noOpNormalization = review.parsePromptNormalizationReviewPayload(noOpRun.stages, reviewLimits)
    assert.equal(noOpNormalization.error, '')
    assert.equal(noOpNormalization.value.attemptId, null)
    assert.equal(noOpNormalization.value.reviewOrigin, 'no_submission')
    assert.deepEqual(noOpNormalization.value.candidates, [])

    const productionView = readFileSync(join(frontendRoot, 'src', 'views', 'ProductionRunView.vue'), 'utf8')
    const platformApiSource = readFileSync(join(frontendRoot, 'src', 'services', 'platformApi.ts'), 'utf8')
    assert.match(productionView, /英文执行字段人工审核/)
    assert.match(productionView, /normalizationHumanOnly/)
    assert.match(productionView, /script_normalize\/manual 纯人工方式执行/)
    assert.match(productionView, /历史纯人工来源/)
    assert.match(productionView, /纯人工输入证据摘要/)
    assert.match(productionView, /candidateOverrides/)
    assert.match(productionView, /expectedSourceSnapshotSha256/)
    assert.match(productionView, /adoptGeneratedMetadata/)
    assert.match(platformApiSource, /candidate_overrides:\s*input\.candidateOverrides/)
    assert.match(platformApiSource, /expected_source_snapshot_sha256:\s*input\.expectedSourceSnapshotSha256/)
    assert.match(platformApiSource, /adopt_generated_metadata:\s*input\.adoptGeneratedMetadata/)
    assert.match(productionView, /shotReferenceBindings/)
    assert.match(productionView, /逐镜精确参考素材/)
    for (const marker of [
      'parsePromptNormalizationRetryRepairs', '受控原因码', 'validationContext.requestIndex',
      'retry_feedback SHA-256', '反馈正文不可查看或编辑',
    ]) assert.ok(productionView.includes(marker), `ProductionRunView misses safe retry audit marker ${marker}`)
    assert.equal(productionView.includes('failure.errorMessage'), false)
    assert.match(platformApiSource, /listGenerationAttempts/)
    assert.match(platformApiSource, /shot_reference_bindings:\s*input\.shotReferenceBindings/)
    assert.match(platformApiSource, /shotReferenceBindingsPayload/)
  } finally { await vite.close() }
})

test('production navigation restores only one actionable run and validates deep-link ownership', async () => {
  const vite = await server()
  try {
    const navigation = await vite.ssrLoadModule('/src/services/productionRunPresentation.ts')
    const waiting = navigation.productionIssuePresentation(
      'waiting_external_task',
      '已创建持久化任务，等待 worker 完成。',
      'running',
    )
    assert.deepEqual(waiting, {
      code: 'waiting_external_task',
      summary: '正在等待外部任务/执行器完成，运行会自动继续。',
      tone: 'info',
      isWaiting: true,
    })
    assert.doesNotMatch(waiting.summary, /阻止|失败/)
    const blocked = navigation.productionIssuePresentation('route_contract_invalid', '路由合同无效', 'blocked')
    assert.equal(blocked.tone, 'danger')
    assert.match(blocked.summary, /服务端阻止/)
    const running = navigation.productionIssuePresentation('worker_heartbeat_pending', '等待 worker', 'running')
    assert.equal(running.tone, 'info')
    assert.doesNotMatch(running.summary, /服务端阻止/)
    const runs = [{ id: 'one', project: 'project-a', status: 'blocked' }, { id: 'done', project: 'project-a', status: 'succeeded' }]
    assert.equal(navigation.restorableProductionRunId(runs), 'one')
    assert.equal(navigation.productionRunRestoreTarget(runs, '', undefined), 'one')
    assert.equal(navigation.productionRunRestoreTarget(runs, '', 'list'), '')
    assert.equal(navigation.productionRunRestoreTarget(runs, '', ['list']), '')
    assert.equal(navigation.productionRunRestoreTarget(runs, 'one', undefined), '')
    assert.equal(navigation.restorableProductionRunId([...runs, { id: 'two', project: 'project-a', status: 'failed' }]), '')
    assert.equal(navigation.productionRunBelongsToProject(runs[0], 'project-a'), true)
    assert.equal(navigation.productionRunBelongsToProject(runs[0], 'project-b'), false)
    const productionView = readFileSync(join(frontendRoot, 'src', 'views', 'ProductionRunView.vue'), 'utf8')
    assert.match(productionView, /:class="blockedPresentation\.tone"/)
    assert.match(productionView, /v-if="blockedPresentation\.isWaiting"/)
    assert.match(productionView, /productionRunRestoreTarget\(runRows, expectedRunId, route\.query\.view\)/)
    assert.match(productionView, /query:\s*\{\s*view:\s*'list'\s*\}/)
    const createViewStart = productionView.indexOf('<template v-else-if="!currentRun">')
    const createView = productionView.slice(
      createViewStart,
      productionView.indexOf('\n    <template v-else>\n      <section class="run-command-bar"', createViewStart),
    )
    assert.match(createView, /创建生产运行（ProductionRun）/)
    assert.match(createView, /历史生产运行（ProductionRun）/)
  } finally { await vite.close() }
})

test('FL2VA storyboard review preserves zero, first, last, and dual frame contracts', async () => {
  const vite = await server()
  try {
    const reference = await vite.ssrLoadModule('/src/services/storyboardReferenceBindings.ts')
    const shot = {
      id: '11111111-1111-4111-8111-111111111111', number: 3, title: '转场',
      mediaType: 'video', conditioningMode: 'vendor.frames', conditioningModeCapability: { key: 'vendor.frames', label: '首尾帧', description: '', inputKind: 'frame_slots', requiresEntityReferenceBible: false, supportsNativeAudio: true, inputs: { totalImages: { minimumItems: 0, maximumItems: 2 }, firstFrame: { minimumItems: 0, maximumItems: 1 }, lastFrame: { minimumItems: 0, maximumItems: 1 }, entityImages: { minimumItems: 0, maximumItems: 0 }, referenceVideos: { minimumItems: 0, maximumItems: 0 }, pairedVideoAudios: { minimumItems: 0, maximumItems: 0 }, referenceAudios: { minimumItems: 0, maximumItems: 0 } } }, includeAudio: false, durationMs: 4000,
      characterIds: [], visualReferenceCharacterIds: [], sceneId: null, propIds: [],
    }
    const first = '22222222-2222-4222-8222-222222222222'
    const last = '33333333-3333-4333-8333-333333333333'
    const cases = [
      [{}, {}],
      [{ firstFrameAssetId: first }, { firstFrameAssetId: first }],
      [{ lastFrameAssetId: last }, { lastFrameAssetId: last }],
      [{ firstFrameAssetId: first, lastFrameAssetId: last }, { firstFrameAssetId: first, lastFrameAssetId: last }],
    ]
    for (const [values, expected] of cases) {
      const draft = { ...reference.emptyStoryboardReferenceDraft(), ...values }
      assert.deepEqual(reference.buildStoryboardShotReferenceBindings(shot, draft), expected)
      assert.equal(reference.validateStoryboardFrameDraft(shot, draft, (assetId) => [first, last].includes(assetId)), '')
    }
    const duplicate = { ...reference.emptyStoryboardReferenceDraft(), firstFrameAssetId: first, lastFrameAssetId: first }
    assert.match(reference.validateStoryboardFrameDraft(shot, duplicate, () => true), /不能复用/)
  } finally { await vite.close() }
})

test('storyboard visual bindings omit voice-only speakers while preserving story membership', async () => {
  const vite = await server()
  try {
    const reference = await vite.ssrLoadModule('/src/services/storyboardReferenceBindings.ts')
    const visibleCharacterId = '11111111-1111-4111-8111-111111111111'
    const voiceOnlyCharacterId = '22222222-2222-4222-8222-222222222222'
    const visibleAssetId = '33333333-3333-4333-8333-333333333333'
    const voiceOnlyAssetId = '44444444-4444-4444-8444-444444444444'
    const shot = {
      id: '55555555-5555-4555-8555-555555555555', number: 1, title: '画外对白',
      mediaType: 'video', conditioningMode: 'vendor.references',
      conditioningModeCapability: {
        key: 'vendor.references', label: '分类参考', description: '',
        inputKind: 'classified_references', requiresEntityReferenceBible: true,
        supportsNativeAudio: true,
      },
      includeAudio: true, durationMs: 4000,
      characterIds: [visibleCharacterId, voiceOnlyCharacterId],
      visualReferenceCharacterIds: [visibleCharacterId],
      sceneId: null, propIds: [],
    }
    const draft = {
      ...reference.emptyStoryboardReferenceDraft(),
      characterAssetIds: {
        [visibleCharacterId]: visibleAssetId,
        [voiceOnlyCharacterId]: voiceOnlyAssetId,
      },
    }

    const bindings = reference.buildStoryboardShotReferenceBindings(shot, draft)
    assert.deepEqual(bindings.characters, [{
      entityId: visibleCharacterId,
      assetId: visibleAssetId,
    }])
    assert.deepEqual(shot.characterIds, [visibleCharacterId, voiceOnlyCharacterId])
  } finally { await vite.close() }
})

test('narrative duration includes image display and video duration with one shared total', async () => {
  const vite = await server()
  try {
    const duration = await vite.ssrLoadModule('/src/services/shotDuration.ts')
    const episode = { shots: [{ duration: 2.5, mediaType: 'image' }, { duration: 5, mediaType: 'video' }] }
    assert.equal(duration.episodeNarrativeDurationSeconds(episode), 7.5)
    assert.equal(duration.seriesNarrativeDurationSeconds([episode, { shots: [{ duration: 1, mediaType: 'image' }] }]), 8.5)
    assert.equal(duration.formatNarrativeDuration(7.5), '7.5')
  } finally { await vite.close() }
})

test('status tones separate lifecycle facts from verified success', async () => {
  const vite = await server()
  try {
    const adapters = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
    for (const status of ['active', 'published', 'configured']) assert.notEqual(adapters.presentPlatformStatus(status).tone, 'success')
    for (const status of ['ready', 'passed', 'succeeded']) assert.equal(adapters.presentPlatformStatus(status).tone, 'success')
    assert.deepEqual(adapters.presentPlatformStatus('not_applicable'), { label: '不适用', tone: 'muted' })
  } finally { await vite.close() }
})

test('account copy, published selectors and Unicode text remain explicit', () => {
  const auth = readFileSync(join(frontendRoot, 'src', 'views', 'AuthView.vue'), 'utf8')
  const settings = readFileSync(join(frontendRoot, 'src', 'views', 'SettingsView.vue'), 'utf8')
  const production = readFileSync(join(frontendRoot, 'src', 'views', 'ProductionRunView.vue'), 'utf8')
  const permissions = readFileSync(join(frontendRoot, 'src', 'views', 'PermissionManagementView.vue'), 'utf8')
  for (const source of [auth, settings, production, permissions]) assert.equal(source.includes('\uFFFD'), false)
  assert.ok(auth.includes('<span>账号</span>'))
  assert.ok(auth.includes('<span>联系邮箱</span>'))
  assert.equal(auth.includes('账号或邮箱'), false)
  assert.ok(production.includes("listRecipes('published')"))
  assert.ok(settings.includes("item.status==='published'"))
  assert.ok(permissions.includes('item.actorId'))
})

test('node metadata loader consumes every v2 page before exposing a catalog', async () => {
  const vite = await server()
  try {
    const { loadCompleteNodeMetadata } = await vite.ssrLoadModule('/src/services/nodeMetadata.ts')
    const all = Array.from({ length: 451 }, (_, index) => ({
      classType: `Node${String(index).padStart(3, '0')}`,
      displayName: `Node ${index}`,
      category: 'test',
      requiredInputs: [],
      optionalInputs: [],
      outputCount: 1,
    }))
    const offsets: number[] = []
    const catalog = await loadCompleteNodeMetadata(async (offset: number, limit: number) => {
      offsets.push(offset)
      const next = offset + limit < all.length ? offset + limit : null
      return {
        schemaVersion: 2,
        deploymentId: 'deployment-1',
        search: '',
        totalCount: all.length,
        offset,
        limit,
        nextOffset: next,
        catalogSha256: 'a'.repeat(64),
        items: all.slice(offset, offset + limit),
      }
    })
    assert.deepEqual(offsets, [0, 200, 400])
    assert.equal(catalog.items.length, 451)
    assert.equal(catalog.catalogSha256, 'a'.repeat(64))
    assert.equal(new Set(catalog.items.map((item) => item.classType)).size, 451)
  } finally { await vite.close() }
})

test('node metadata loader fails closed on drift, duplicate nodes and no progress', async () => {
  const vite = await server()
  try {
    const { loadCompleteNodeMetadata } = await vite.ssrLoadModule('/src/services/nodeMetadata.ts')
    const node = (classType: string) => ({ classType, displayName: classType, category: '', requiredInputs: [], optionalInputs: [], outputCount: 1 })
    await assert.rejects(() => loadCompleteNodeMetadata(async (offset: number) => ({
      schemaVersion: 2, deploymentId: 'd', search: '', totalCount: 2, offset, limit: 1,
      nextOffset: offset === 0 ? 1 : null, catalogSha256: offset === 0 ? 'a'.repeat(64) : 'b'.repeat(64), items: [node(`Node${offset}`)],
    }), 1), /漂移/)
    await assert.rejects(() => loadCompleteNodeMetadata(async (offset: number) => ({
      schemaVersion: 2, deploymentId: 'd', search: '', totalCount: 2, offset, limit: 1,
      nextOffset: offset === 0 ? 1 : null, catalogSha256: 'a'.repeat(64), items: [node('DuplicateNode')],
    }), 1), /重复/)
    await assert.rejects(() => loadCompleteNodeMetadata(async () => ({
      schemaVersion: 2, deploymentId: 'd', search: '', totalCount: 1, offset: 0, limit: 1,
      nextOffset: 0, catalogSha256: 'a'.repeat(64), items: [],
    }), 1), /空页|前进/)
  } finally { await vite.close() }
})

test('safe uncertainty adapters expose hashes but never raw provider request data', async () => {
  const vite = await server()
  try {
    const adapters = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
    const deployment = adapters.adaptModelDeployment({
      id: 'deployment-1', created_at: '', updated_at: '', runtime_config: {},
      latest_smoke: {
        id: null, status: 'unresolved', operations: ['prompt.normalize'], fresh: false,
        unresolved_submission: {
          id: 'smoke-1', status: 'uncertain', operation: 'prompt.normalize',
          request_sha256: 'a'.repeat(64), target_sha256: 'b'.repeat(64),
          provider_request_id_sha256: 'c'.repeat(64), submission_started_at: '2026-08-23T00:00:00Z',
          provider_request_id: 'must-not-leak', request_payload: { secret: true },
        },
      },
    })
    assert.equal(deployment.latestSmoke?.unresolvedSubmission?.providerRequestIdSha256, 'c'.repeat(64))
    assert.equal('providerRequestId' in deployment.latestSmoke!.unresolvedSubmission!, false)
    assert.equal('requestPayload' in deployment.latestSmoke!.unresolvedSubmission!, false)
    const attempt = adapters.adaptGenerationAttempt({
      id: 'attempt-1', created_at: '', updated_at: '', provider_request_id: 'must-not-leak',
      request_snapshot: { secret: true }, provider_request_id_sha256: 'd'.repeat(64), provider_request_id_suffix: 'masked1',
    })
    assert.equal(attempt.providerRequestIdSha256, 'd'.repeat(64))
    assert.equal('providerRequestId' in attempt, false)
    assert.equal('requestSnapshot' in attempt, false)
  } finally { await vite.close() }
})
