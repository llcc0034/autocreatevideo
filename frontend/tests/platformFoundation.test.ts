import assert from 'node:assert/strict'
import test from 'node:test'
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const frontendRoot = fileURLToPath(new URL('..', import.meta.url))
const server = () => createServer({ root: frontendRoot, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })

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
    assert.deepEqual(adapters.presentPlatformStatus('future_server_state'), { label: 'future_server_state', tone: 'unknown' })
    const artifact = adapters.adaptGeneratedArtifact({ id: 'a-1', kind: 'future_media', status: 'server_selected_v2', manifest_metadata: { task_id: 't-1', output_index: 3 }, comparison_parameters: { model: 'model-v2', generation: { seed: 29 } }, created_at: '', updated_at: '' })
    assert.equal(artifact.kind, 'future_media')
    assert.equal(artifact.status, 'server_selected_v2')
    assert.deepEqual(artifact.manifestMetadata, { task_id: 't-1', output_index: 3 })
    assert.deepEqual(artifact.comparisonParameters, { model: 'model-v2', generation: { seed: 29 } })
    const mediaRoutes = adapters.adaptProductionRecipeMediaRouteOptions({
      schema_version: 1,
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
        h3_modes: ['Ref2VA'],
        media_operations: [],
        is_selectable: false,
        reason_codes: ['workflow_smoke_missing'],
        reasons: ['工作流未通过 smoke'],
      }],
    })
    assert.equal(mediaRoutes.routes[0].routeIndex, 2)
    assert.deepEqual(mediaRoutes.routes[0].h3Modes, ['Ref2VA'])
    assert.deepEqual(mediaRoutes.routes[0].reasonCodes, ['workflow_smoke_missing'])
    const snapshotted = adapters.adaptProductionRun({
      id: 'run-snapshots', created_at: '', updated_at: '',
      stages: [{
        id: 'stage-1', stage_key: 'script_generate', created_at: '', updated_at: '',
        input_snapshot: { prompt: 'frozen' }, input_snapshot_sha256: 'a'.repeat(64),
        output_snapshot: { review_payload: { kind: 'generated_script_preview' } }, output_snapshot_sha256: 'b'.repeat(64),
      }],
    })
    assert.deepEqual(snapshotted.stages[0].inputSnapshot, { prompt: 'frozen' })
    assert.equal(snapshotted.stages[0].inputSnapshotSha256, 'a'.repeat(64))
    assert.deepEqual(snapshotted.stages[0].outputSnapshot, { review_payload: { kind: 'generated_script_preview' } })
    assert.equal(snapshotted.stages[0].outputSnapshotSha256, 'b'.repeat(64))
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
    const deployment = adapters.adaptModelDeployment({
      id: 'deployment-1', profile_version: 'profile-1', provider_profile_id: 'provider-1',
      latest_smoke: { id: 'smoke-1', status: 'passed', operations: ['prompt.normalize'], captured_at: '2026-08-23T00:00:00Z', expires_at: '2026-08-24T00:00:00Z', fresh: true },
      runtime_config: {}, runtime_config_sha256: 'b'.repeat(64), created_at: '', updated_at: '',
    })
    assert.deepEqual(deployment.latestSmoke?.operations, ['prompt.normalize'])
    assert.equal(deployment.latestSmoke?.fresh, true)
    const workflow = adapters.adaptWorkflowTemplateVersion({
      id: 'workflow-1', template: 'template-1', version: 3,
      validated_deployment: 'deployment-1', validation_state: 'passed', smoke_test_state: 'passed',
      validation_evidence: { object_info_sha256: 'c'.repeat(64) },
      smoke_test_evidence: { output_sha256: 'd'.repeat(64) },
      validation_evidence_sha256: 'e'.repeat(64), smoke_test_evidence_sha256: 'f'.repeat(64),
      execution_contract_sha256: '1'.repeat(64), published_by: 'reviewer-2', created_at: '', updated_at: '',
    })
    assert.equal(workflow.validatedDeployment, 'deployment-1')
    assert.deepEqual(workflow.validationEvidence, { object_info_sha256: 'c'.repeat(64) })
    assert.deepEqual(workflow.smokeTestEvidence, { output_sha256: 'd'.repeat(64) })
    assert.equal(workflow.publishedBy, 'reviewer-2')
    const artifact = adapters.adaptModelArtifact({
      id: 'artifact-1', profile_version: 'profile-1', verified_snapshot: 'snapshot-1',
      verified_at: '2026-08-23T00:00:00Z', verified_by: 'reviewer-3', created_at: '', updated_at: '',
    })
    assert.equal(artifact.verifiedSnapshot, 'snapshot-1')
    assert.equal(artifact.verifiedBy, 'reviewer-3')
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

test('registry resources use typed schemas, explicit defaults and prerequisite blockers', async () => {
  const vite = await server()
  try {
    const schema = await vite.ssrLoadModule('/src/services/registryResourceSchema.ts')
    const emptyDependencies = { providers: [], modelProfiles: [], deployments: [], runtimeHosts: [], workflowVersions: [] }
    const config = schema.applyResourceDefaults('config-definition', {
      key: 'production.test', domain: 'production', value_type: 'integer', description: 'test', impact_modules: ['worker'],
    }, emptyDependencies)
    assert.equal(config.allowed_scopes, 'system,project,run')
    assert.equal(config.restart_required, false)
    assert.equal(config.is_secret, false)
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
    const asyncProvider = schema.applyResourceDefaults('provider', {
      key: 'media-api', name: 'Media API', driver_type: 'async_rest', modalities: 'image', driver_config: {}, is_active: true,
    }, emptyDependencies)
    assert.deepEqual(asyncProvider.driver_config.request_field_map, { model: 'model', prompt: 'prompt' })
    const comfyProvider = { id: 'provider-comfy', name: 'Comfy', driverType: 'comfyui', driverConfig: {}, isActive: true }
    const publishedModel = { id: 'model-comfy', profileKey: 'sdxl', version: 1, provider: 'provider-comfy', modelIdentifier: 'sdxl', status: 'published', specification: { capabilities: ['image.text_to_image'] } }
    const deployment = schema.applyResourceDefaults('deployment', {
      key: 'comfy-deployment', name: 'Comfy deployment', profile_version: 'model-comfy', runtime_config: {}, status: 'active',
    }, { ...emptyDependencies, providers: [comfyProvider], modelProfiles: [publishedModel] })
    assert.deepEqual(deployment.runtime_config, {})
    assert.ok(schema.validateRegistryResource('deployment', deployment, {
      ...emptyDependencies, providers: [comfyProvider], modelProfiles: [publishedModel],
    }).errors.some((item: string) => item.includes('服务根地址')))
    const recipeStages = schema.standardRecipeStages('ai')
    assert.equal(recipeStages[0].key, 'script_generate')
    assert.ok(recipeStages.some((stage: { key: string }) => stage.key === 'media_generation'))
    assert.ok(recipeStages.some((stage: { key: string }) => stage.key === 'delivery'))
    assert.equal(recipeStages.find((stage: { key: string }) => stage.key === 'artifact_selection').mode, 'review')
    assert.ok(schema.recipeStageOptions.some((item: { value: string }) => item.value === 'script_structure'))
    assert.ok(schema.routedRecipeStageKeys.has('script_structure'))
  } finally { await vite.close() }

  const settings = readFileSync(join(frontendRoot, 'src', 'views', 'SettingsView.vue'), 'utf8')
  const editor = readFileSync(join(frontendRoot, 'src', 'components', 'RegistryResourceEditor.vue'), 'utf8')
  const registrySchema = readFileSync(join(frontendRoot, 'src', 'services', 'registryResourceSchema.ts'), 'utf8')
  for (const marker of ['RegistryResourceEditor', 'editorDependencies', 'editorValidity', 'SCHEMA-DRIVEN · SERVER VALIDATED']) {
    assert.ok(settings.includes(marker), `SettingsView misses ${marker}`)
  }
  assert.equal(settings.includes('structuredClone(editorPayload.value)'), false)
  assert.equal(editor.includes('structuredClone(recipeStages.value)'), false)
  assert.ok(editor.indexOf('const recipeErrors = computed') < editor.indexOf('const validation = computed'), 'recipe validation must initialize recipeErrors before the immediate watcher reads it')
  for (const marker of ['影响：', '套用 AI 起稿链路', 'active Deployment', '高级 JSON · 非唯一配置入口']) {
    assert.ok(editor.includes(marker), `RegistryResourceEditor misses ${marker}`)
  }
  for (const marker of ['请先发布模型版本', '前置依赖', 'driver_config.base_url', 'driver_config.progress_field', 'driver_config.pricing.currency', 'standardRecipeStages']) {
    assert.ok(registrySchema.includes(marker), `registryResourceSchema misses ${marker}`)
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
  for (const marker of ['compatibleDeployments', 'validatedDeployment', 'validationEvidence', 'smokeTestEvidence', 'publishOpen']) {
    assert.ok(workflow.includes(marker), `WorkflowVersionEditor misses ${marker}`)
  }
  assert.ok(settings.indexOf('关联并核对模型文件') < settings.indexOf('核对发布'))
})

test('workflow editor submits the complete immutable contract with artifact_id only', () => {
  const workflow = readFileSync(join(frontendRoot, 'src', 'views', 'WorkflowVersionEditorView.vue'), 'utf8')
  const platformApi = readFileSync(join(frontendRoot, 'src', 'services', 'platformApi.ts'), 'utf8')
  for (const marker of ['inputContractText', 'inputContractField', '合同 Schema', '生产能力', '编译器 ID', 'semanticBindings', 'modelRequirements', 'v-model="item.artifactId"', 'metadataComplete', 'getAllNodeMetadata']) {
    assert.ok(workflow.includes(marker), `WorkflowVersionEditor misses ${marker}`)
  }
  for (const marker of ['input_contract:', 'semantic_bindings:', 'model_requirements:', 'artifact_id: item.artifactId']) {
    assert.ok(platformApi.includes(marker), `platformApi misses ${marker}`)
  }
  assert.equal(platformApi.includes('artifact: item.artifactId'), false)
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
  for (const marker of ['downloadSavedComfyCanvas', "openMode !== 'download_import'", '在 ComfyUI 中打开（下载导入）', '不会跨页面注入工作流，也不会下发生产任务', 'version.validatedDeployment || deploymentId.value']) {
    assert.ok(workflowEditor.includes(marker), `WorkflowVersionEditor misses safe workflow export marker: ${marker}`)
  }
  assert.ok(api.includes("{ params: deploymentId ? { deployment_id: deploymentId } : {} }"))
})

test('PromptProfile settings expose typed CRUD, deterministic preview and published binding dependencies', async () => {
  const vite = await server()
  try {
    const adapters = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
    const field = adapters.adaptPromptFieldDefinition({
      id: 'field-1', key: 'lighting', label: '灯光', category: 'visual', description: '场景照明', impact: '影响画面层次',
      value_type: 'enum', default_value: 'cinematic', value_options: ['cinematic', 'natural'],
      is_required: true, is_active: true, display_order: 10, created_at: '', updated_at: '',
    })
    assert.equal(field.valueType, 'enum')
    assert.deepEqual(field.valueOptions, ['cinematic', 'natural'])
    assert.equal(field.isRequired, true)
    const options = adapters.adaptPromptProfileOptions({
      operations: ['video.generate'], model_families: ['minimax-h3'], modes: ['text_to_video'],
      source_selectors: [{ value: 'request.lighting', label: '请求灯光' }],
      channels: [{ value: 'positive', label: '正向提示词' }], label_styles: [], separators: [],
      model_profile_versions: [{ id: 'model-1', profile_key: 'h3', version: 2, model_identifier: 'h3-v2', modality: 'video' }],
    })
    assert.deepEqual(options.sourceSelectors[0], { value: 'request.lighting', label: '请求灯光' })
    assert.equal(options.modelProfileVersions[0].modelIdentifier, 'h3-v2')
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
  const component = readFileSync(join(frontendRoot, 'src', 'components', 'PromptProfileSettings.vue'), 'utf8')
  const api = readFileSync(join(frontendRoot, 'src', 'services', 'platformApi.ts'), 'utf8')
  for (const marker of ["id: 'prompts'", "tab==='prompts'", 'PromptProfileSettings']) assert.ok(settings.includes(marker), `SettingsView misses ${marker}`)
  for (const marker of ['通用字段字典', '类型化预览', '运行时绑定', '官方来源', 'system_instruction', 'definition.default', 'Published PromptProfile', '不调用文本模型']) {
    assert.ok(component.includes(marker), `PromptProfileSettings misses ${marker}`)
  }
  for (const marker of ['/prompt-field-definitions/', '/prompt-profile-versions/', '/prompt-profile-field-rules/', '/prompt-profile-bindings/', 'previewPromptProfileVersion', 'publishPromptProfileVersion', 'retirePromptProfileVersion']) {
    assert.ok(api.includes(marker), `platformApi misses ${marker}`)
  }
  assert.equal(component.includes('window.prompt'), false)
  assert.equal(component.includes('v-model="rawJson"'), false)
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
  for (const capability of ['image.text_to_image', 'video.text_to_video', 'prompt.normalize', 'script.generate', 'script.structure']) {
    assert.ok(settings.includes(`'${capability}'`), `SettingsView deployment smoke operations miss ${capability}`)
  }
  assert.ok(settings.includes('profileFor(item.profileVersion)?.specification.capabilities'))
  assert.ok(settings.includes('actionSmokeOperations'))
  const routingLoad = settings.slice(
    settings.indexOf("else if (tab.value === 'routing')"),
    settings.indexOf("else if (tab.value === 'defaults')"),
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
    const { buildProductionRunRequestPayload } = await vite.ssrLoadModule('/src/services/productionRunPayload.ts')
    const common = {
      projectId: 'project-1', recipeVersionId: 'recipe-1', idempotencyKey: 'request-1',
      allowExternalCalls: true, budgetLimit: '20.0000', maxGenerationTasks: 42, configOverrides: { seed: 9 },
      scriptRevisionId: 'revision-should-be-omitted', idea: '岛上的谜案', styleHint: '黑色电影',
      episodeCount: 6, shotsPerEpisode: 8, episodeDurationSeconds: 90,
      episodeIds: ['episode-b', 'episode-a', 'episode-b'],
    }
    const ai = buildProductionRunRequestPayload({ ...common, scriptMode: 'ai' })
    assert.deepEqual(ai, {
      project_id: 'project-1', recipe_version_id: 'recipe-1', idempotency_key: 'request-1',
      allow_external_calls: true, budget_limit: '20.0000', max_generation_tasks: 42,
      config_overrides: { seed: 9 }, idea: '岛上的谜案', style_hint: '黑色电影',
      episode_count: 6, shots_per_episode: 8, episode_duration_seconds: 90,
    })
    assert.equal('script_revision_id' in ai, false)
    assert.equal('episode_ids' in ai, false)

    const confirmed = buildProductionRunRequestPayload({ ...common, scriptMode: 'confirmed_revision', scriptRevisionId: 'revision-1' })
    assert.equal(confirmed.script_revision_id, 'revision-1')
    for (const key of ['idea', 'style_hint', 'episode_count', 'shots_per_episode', 'episode_duration_seconds', 'episode_ids']) assert.equal(key in confirmed, false)

    const downstream = buildProductionRunRequestPayload({ ...common, scriptMode: 'downstream' })
    assert.deepEqual(downstream.episode_ids, ['episode-a', 'episode-b'])
    for (const key of ['script_revision_id', 'idea', 'style_hint', 'episode_count', 'shots_per_episode', 'episode_duration_seconds']) assert.equal(key in downstream, false)

    const uniform = buildProductionRunRequestPayload({
      ...common,
      scriptMode: 'downstream',
      workflowSelection: { schemaVersion: 1, mode: 'uniform', routeIndex: 1 },
    })
    assert.deepEqual(uniform.workflow_selection, {
      schema_version: 1, mode: 'uniform', route_index: 1, shot_routes: [],
    })
    const perShot = buildProductionRunRequestPayload({
      ...common,
      scriptMode: 'downstream',
      workflowSelection: {
        schemaVersion: 1,
        mode: 'per_shot',
        shotRoutes: [
          { shotId: 'shot-a', routeIndex: 0 },
          { shotId: 'shot-b', routeIndex: 1 },
        ],
      },
    })
    assert.deepEqual(perShot.workflow_selection, {
      schema_version: 1,
      mode: 'per_shot',
      route_index: null,
      shot_routes: [
        { shot_id: 'shot-a', route_index: 0 },
        { shot_id: 'shot-b', route_index: 1 },
      ],
    })
    assert.throws(() => buildProductionRunRequestPayload({
      ...common,
      scriptMode: 'downstream',
      workflowSelection: {
        schemaVersion: 1,
        mode: 'per_shot',
        shotRoutes: [
          { shotId: 'shot-a', routeIndex: 0 },
          { shotId: 'shot-a', routeIndex: 1 },
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

    const storyboardRun = adapters.adaptProductionRun({ id: 'run-storyboard', stages: [{
      id: 'stage-storyboard', stage_key: 'storyboard', ordinal: 3, status: 'succeeded', created_at: '', updated_at: '',
      output_snapshot_sha256: '7'.repeat(64), output_snapshot: {
        stage_key: 'storyboard', formal_object_type: 'storyboard', canonical_sha256: '8'.repeat(64),
        canonical: { episodes: [{ id: revisionId, number: 1, title: '第一集', shots: [
          { id: artifactId, number: 1, duration_ms: 2500, media_type: 'image', include_audio: false },
          { id: attemptId, number: 2, duration_ms: 5000, media_type: 'video', include_audio: true },
        ] }] },
      },
    }] })
    const storyboard = review.parseStoryboardReviewPayload(storyboardRun.stages)
    assert.equal(storyboard.error, '')
    assert.deepEqual({ shots: storyboard.value.shotCount, images: storyboard.value.imageCount, videos: storyboard.value.videoCount, audio: storyboard.value.audioShotCount, duration: storyboard.value.durationMs }, { shots: 2, images: 1, videos: 1, audio: 1, duration: 7500 })
    storyboardRun.stages[0].outputSnapshot.canonical_sha256 = ''
    assert.match(review.parseStoryboardReviewPayload(storyboardRun.stages).error, /hash/)

    const normalizationRun = adapters.adaptProductionRun({ id: 'run-normalization', stages: [{
      id: 'stage-normalization', stage_key: 'script_normalize', ordinal: 4, status: 'succeeded', created_at: '', updated_at: '',
      output_snapshot_sha256: '9'.repeat(64), output_snapshot: {
        stage_key: 'script_normalize', review_payload: {
          kind: 'prompt_normalization_candidates', attempt_id: attemptId, result_sha256: '6'.repeat(64),
          provider: 'deepseek', model: 'deepseek-chat', submission_semantics: 'durable_single_submit_manual_resolution',
          candidates: [{
            request_id: 'candidate-character-name', scope_type: 'character', scope_id: revisionId,
            field_key: 'character.name', label: '角色名', source_text: '林雾',
            source_hash: '4'.repeat(64), context_hash: '5'.repeat(64), translated_text: 'S1',
          }],
        },
      },
    }] })
    const normalization = review.parsePromptNormalizationReviewPayload(normalizationRun.stages)
    assert.equal(normalization.error, '')
    assert.equal(normalization.value.candidates[0].requestId, 'candidate-character-name')
    assert.equal(normalization.value.candidates[0].translatedText, 'S1')
    assert.equal(normalization.value.snapshotSha256, '9'.repeat(64))

    const noOpRun = adapters.adaptProductionRun({ id: 'run-normalization-no-op', stages: [{
      id: 'stage-normalization-no-op', stage_key: 'script_normalize', ordinal: 4, status: 'succeeded', created_at: '', updated_at: '',
      output_snapshot_sha256: '3'.repeat(64), output_snapshot: {
        stage_key: 'script_normalize', review_payload: {
          kind: 'prompt_normalization_candidates', attempt_id: null, result_sha256: '2'.repeat(64),
          provider: '', model: '', submission_semantics: 'no_submission', candidates: [],
        },
      },
    }] })
    const noOpNormalization = review.parsePromptNormalizationReviewPayload(noOpRun.stages)
    assert.equal(noOpNormalization.error, '')
    assert.equal(noOpNormalization.value.attemptId, null)
    assert.deepEqual(noOpNormalization.value.candidates, [])

    const productionView = readFileSync(join(frontendRoot, 'src', 'views', 'ProductionRunView.vue'), 'utf8')
    const platformApiSource = readFileSync(join(frontendRoot, 'src', 'services', 'platformApi.ts'), 'utf8')
    assert.match(productionView, /英文执行字段人工审核/)
    assert.match(productionView, /candidateOverrides/)
    assert.match(productionView, /expectedSourceSnapshotSha256/)
    assert.match(productionView, /adoptGeneratedMetadata/)
    assert.match(platformApiSource, /candidate_overrides:\s*input\.candidateOverrides/)
    assert.match(platformApiSource, /expected_source_snapshot_sha256:\s*input\.expectedSourceSnapshotSha256/)
    assert.match(platformApiSource, /adopt_generated_metadata:\s*input\.adoptGeneratedMetadata/)
  } finally { await vite.close() }
})

test('production navigation restores only one actionable run and validates deep-link ownership', async () => {
  const vite = await server()
  try {
    const navigation = await vite.ssrLoadModule('/src/services/productionRunPresentation.ts')
    const runs = [{ id: 'one', project: 'project-a', status: 'blocked' }, { id: 'done', project: 'project-a', status: 'succeeded' }]
    assert.equal(navigation.restorableProductionRunId(runs), 'one')
    assert.equal(navigation.restorableProductionRunId([...runs, { id: 'two', project: 'project-a', status: 'failed' }]), '')
    assert.equal(navigation.productionRunBelongsToProject(runs[0], 'project-a'), true)
    assert.equal(navigation.productionRunBelongsToProject(runs[0], 'project-b'), false)
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

test('Comfy API graph keeps complex controls but blocks publish compatibility', async () => {
  const vite = await server()
  try {
    const graphTools = await vite.ssrLoadModule('/src/services/comfyWorkflowGraph.ts')
    const graph = {
      '1': { class_type: 'KnownNode', inputs: { prompt: 'hello', custom_widget: { value: 1 } } },
      '2': { class_type: 'KnownOutput', inputs: { image: ['1', 0] } },
    }
    const analysis = graphTools.analyzeComfyWorkflow(graph, [
      { classType: 'KnownNode', displayName: 'Known', category: 'core', requiredInputs: ['prompt'], optionalInputs: [], outputCount: 1 },
      { classType: 'KnownOutput', displayName: 'Output', category: 'core', requiredInputs: ['image'], optionalInputs: [], outputCount: 1 },
    ])
    assert.equal(analysis.format, 'api')
    assert.equal(analysis.edges.length, 1)
    assert.ok(analysis.blockers.some((item: string) => item.includes('custom_widget')))
    const rebuilt = graphTools.buildApiGraph(analysis.nodes, analysis.edges)
    assert.deepEqual(rebuilt['1'].inputs.custom_widget, { value: 1 })
    assert.deepEqual(rebuilt['2'].inputs.image, ['1', 0])
  } finally { await vite.close() }
})

test('workflow inspector literal edits serialize immediately without blur', async () => {
  const vite = await server()
  try {
    const graphTools = await vite.ssrLoadModule('/src/services/comfyWorkflowGraph.ts')
    const graph = {
      '5': { class_type: 'KSampler', inputs: { steps: 4, cfg: 5, model: ['1', 0] } },
      '9': { class_type: 'OtherSampler', inputs: { steps: 8 } },
    }
    const analysis = graphTools.analyzeComfyWorkflow(graph)
    const stepsEdited = graphTools.updateGraphLiteralInput(analysis.nodes, '5', 'steps', '30')
    const cfgEdited = graphTools.updateGraphLiteralInput(stepsEdited, '5', 'cfg', '6.5')
    const rebuilt = graphTools.buildApiGraph(cfgEdited, analysis.edges)
    assert.equal(rebuilt['5'].inputs.steps, 30)
    assert.equal(rebuilt['5'].inputs.cfg, 6.5)
    assert.equal(typeof rebuilt['5'].inputs.steps, 'number')
    assert.equal(rebuilt['9'].inputs.steps, 8)
    assert.deepEqual(rebuilt['5'].inputs.model, ['1', 0])

    const editor = readFileSync(join(frontendRoot, 'src', 'views', 'WorkflowVersionEditorView.vue'), 'utf8')
    assert.match(editor, /@input="updateInput\(String\(selectedNode\.id\),name,\(\$event\.target as HTMLTextAreaElement\)\.value\)"/)
    assert.equal(editor.includes('@change="updateInput(name,$event)"'), false)
  } finally { await vite.close() }
})

test('Comfy UI positional widgets and subgraphs are explicit blockers', async () => {
  const vite = await server()
  try {
    const graphTools = await vite.ssrLoadModule('/src/services/comfyWorkflowGraph.ts')
    const analysis = graphTools.analyzeComfyWorkflow({
      subgraphs: [{ id: 'nested' }],
      nodes: [{ id: 1, type: 'CheckpointLoaderSimple', pos: [10, 20], inputs: [], widgets_values: ['model.safetensors'] }],
      links: [],
    })
    assert.equal(analysis.format, 'ui')
    assert.equal(analysis.nodes.length, 1)
    assert.ok(analysis.blockers.some((item: string) => item.includes('子图')))
    assert.ok(analysis.blockers.some((item: string) => item.includes('widgets_values')))
  } finally { await vite.close() }
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
