import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { readFile } from 'node:fs/promises'
import { createServer } from 'vite'
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'

const root = fileURLToPath(new URL('..', import.meta.url))
const id = (n: number) => `00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`
const profileId = id(1), definitionId = id(2), exactVersionId = id(3), structureId = id(4)
const stamp = '2026-10-04T00:00:00Z', source = 'request.structured_input'
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value))
const page = (results: unknown[]) => ({ count: results.length, next: null, previous: null, results })
const flush = async () => { await nextTick(); await new Promise(resolve => setImmediate(resolve)); await nextTick() }
const choices = (...values: string[]) => values.map(value => ({ value, label: `夹具 · ${value}` }))
function catalog() {
  return {
    operations: ['video.generate'], model_families: ['isolated-model'], modes: ['fixture-mode'], custom_entry_fields: ['language_policy'],
    source_selectors: [{ value: source, label: '本次结构化原文', group_label: '输入', description: '确切版本中的原文', record_location: '准备输入', execution_usage: '按已保存路径取值' }],
    channels: choices('positive_prompt'), label_styles: choices('label_colon'), separators: choices('newline'),
    generation_modes: choices('model_generated', 'translate_only', 'passthrough'),
    generation_leaf_operations: ['prompt.fields.generate', 'prompt.fields.translate'],
    generation_source_selectors: [{ value: source, label: '本次结构化原文', scope_type: 'unit', group: 'input', group_label: '输入',
      description: '只处理配置路径', availability: '已提供输入时', scope: '当前单元', status: 'active', generation_modes: ['model_generated', 'translate_only'] }],
    generation_context_selectors: [{ value: 'project.style', label: '项目风格', description: '已保存的项目风格' }],
    generation_output_semantics: choices('final'), source_projection_keys: choices('direct_v1'), source_projection_primitives: [],
    reference_rendering_fields: [], reference_rendering_text_slots: [], language_policies: choices('inherit', 'preserve_source', 'en', 'bilingual:en+zh-CN'),
    length_units: choices('none', 'characters'), generation_validators: choices('non_empty'), model_profile_versions: [],
    rule_system_defaults: { label_style: 'label_colon', separator: 'newline', required: false, omit_if_empty: true,
      max_chars: 2048, generation_output_semantics: 'final', generation_required: false, language_policy: 'inherit', length_unit: 'none' },
  }
}

// Actual setup/watch/lifecycle code and API adapters run; only the HTTP boundary
// is replaced by declared in-memory responses. Unknown reads and all undeclared
// writes fail the test, even when the product catches the request error.
async function harness(context: any) {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom',
    server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true } })
  const apps: any[] = [], calls: { method: string; url: string; data: any }[] = [], unexpected: string[] = []
  const handlers = new Map<string, (data: any, options: any) => unknown>()
  const block = (label: string): never => { unexpected.push(label); throw Error(`No live HTTP: ${label}`) }
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  api.interceptors.request.use((config: any) => block(`${config.method} ${config.url}`))
  api.defaults.adapter = async () => block('Axios transport')
  context.mock.method(globalThis, 'fetch', async () => block('fetch transport'))
  for (const method of ['get', 'post', 'patch', 'put', 'delete', 'options', 'head']) {
    api[method] = async (url: string, data: any, options: any) => {
      const handler = handlers.get(`${method} ${url}`)
      if (!handler) return block(`${method} ${url}`)
      calls.push({ method, url, data: data === undefined ? undefined : clone(data) })
      return { data: clone(await handler(data, options)) }
    }
  }
  context.after(async () => {
    for (const app of apps.reverse()) app.unmount()
    await vite.close()
    assert.deepEqual(unexpected, [], 'No swallowed undeclared business requests')
  })
  async function component(path: string, props: any = {}) {
    const { default: Component } = await vite.ssrLoadModule(path)
    let state: any
    const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
      insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
    const app = renderer.createApp({ setup() { state = Component.setup(props, { expose() {}, emit() {} }); return () => h('div') } })
    app.provide(ssrContextKey, { modules: new Set() }); apps.push(app); app.mount({}); await flush()
    return { state, render: () => {
      const view = createSSRApp({ render: () => h({ ...Component, setup: () => state }, props) })
      view.config.warnHandler = message => assert.fail(message)
      return renderToString(view)
    } }
  }
  return { vite, handlers, calls, component }
}

async function author(context: any, fieldKey = 'custom_camera_orbit', props: any = {}) {
  const env = await harness(context)
  const exactVersion = { id: exactVersionId, field_definition: definitionId, field_key: fieldKey, version: 2, status: 'published',
    label: '自定义环绕 / 隔离旧版本', value_type: 'long_text', value_options: [], category: 'custom', unit: '', description: '确切 v2 定义', impact: '', preview_example: '' }
  const profile = { id: profileId, profile_key: 'fixture-profile', version: 3, name: '隔离作者策略', description: '', status: 'draft',
    model_family: 'isolated-model', operation: 'video.generate', mode: 'fixture-mode', source_projection_key: 'direct_v1',
    source_projection_config: {}, channel_limits: {}, input_structure_version: structureId, review_state: 'needs_review',
    contract_schema_version: 1, contract_sha256: 'a'.repeat(64), contract_size_bytes: 800, field_rule_count: 0 }
  const rawRules: any[] = [], options = catalog()
  env.handlers.set('get /field-definitions/', () => page([{ id: definitionId, key: fieldKey, label: '当前字典名称不可代替确切版本', is_active: true }]))
  env.handlers.set('get /prompt-profile-versions/', () => page([profile]))
  env.handlers.set('get /prompt-profile-field-rules/', () => page(rawRules))
  env.handlers.set('get /prompt-profile-versions/options/', () => options)
  env.handlers.set('get /registry-relations/', () => ({ ...page([]), generated_at: stamp, context_label: 'isolated' }))
  env.handlers.set(`get /field-definition-versions/${exactVersionId}/`, () => exactVersion)
  env.handlers.set(`get /data-structure-versions/${structureId}/schema-preview/`, () => ({ valid: true, errors: {},
    json_schema: { type: 'object', properties: { 'notes.custom': { type: 'string' } }, required: ['notes.custom'] } }))
  env.handlers.set('post /prompt-profile-field-rules/', payload => {
    assert.equal(payload.field_definition_version, exactVersionId)
    const raw = { ...payload, id: id(5), field_key: fieldKey, field_label: exactVersion.label, field_value_type: exactVersion.value_type,
      created_at: stamp, updated_at: stamp }
    rawRules.push(raw); profile.field_rule_count = rawRules.length
    return raw
  })
  env.handlers.set(`patch /prompt-profile-field-rules/${id(5)}/`, payload => {
    Object.assign(rawRules[0], payload); return rawRules[0]
  })
  env.handlers.set(`patch /prompt-profile-versions/${profileId}/`, payload => {
    assert.deepEqual(payload, { review_state: 'ready' }, 'Profile readiness changes only its own saved field')
    Object.assign(profile, payload); return profile
  })
  const view = await env.component('/src/components/PromptProfileSettings.vue',props)
  const state = view.state
  assert.equal(state.error.value, ''); assert.equal(state.promptOptionsReady.value, true)
  async function open(existing?: any) {
    state.openRule(existing)
    if (!existing) Object.assign(state.ruleForm, { fieldDefinition: definitionId, fieldDefinitionVersion: exactVersionId,
      sourceSelector: source, generationSourceSelector: source, sourcePath: ['notes.custom'], channel: 'positive_prompt',
      generationMode: 'model_generated', generationInstruction: '仅依据配置的原文描写动作，保留多语言名称。',
      generationRequired: true, languagePolicy: 'inherit', lengthUnit: 'characters', generationMin: '', generationMax: 1800,
      generationValidatorKeys: ['non_empty'], generationContextSelectors: ['project.style'] })
    const { fieldDefinitionVersions } = await env.vite.ssrLoadModule('/src/services/fieldDefinitionVersions.ts')
    state.selectRuleVersion(await fieldDefinitionVersions.get(exactVersionId))
    await flush()
    assert.deepEqual(state.editorValidation.value, [])
  }
  async function save() {
    assert.deepEqual(state.editorValidation.value, [])
    const expected = clone(state.buildRulePayload())
    await state.submitEditor(); await flush()
    assert.equal(state.editorError.value, ''); assert.equal(state.error.value, '')
    assert.equal(state.rules.value.length, 1)
    return expected
  }
  return { ...env, state, exactVersion, profile, rawRules, options, open, save, fieldKey }
}

test('strategy processing summary tracks author modes and language without changing writer ownership', async context => {
  const env = await author(context); await env.open()
  const { state } = env
  env.options.generation_modes.push({ value: 'server_locked', label: '服务端锁定' })
  state.options.value.generationModes.push({ value: 'server_locked', label: '服务端锁定' })
  const sourceBefore = clone(env.profile)
  state.ruleForm.languagePolicy = 'bilingual:en+zh-Hant'
  const first = await env.save()
  for (const generationMode of ['translate_only', 'passthrough', 'server_locked', 'model_generated']) {
    await env.open(state.rules.value[0])
    state.ruleForm.generationMode = generationMode; await flush()
    const summary = state.draftRulePath.value
    assert.equal(summary.generationMode, generationMode)
    assert.equal(summary.languagePolicy, 'bilingual:en+zh-Hant')
    assert.deepEqual(summary.sourcePath, ['notes.custom'])
    assert.equal(summary.generationInstruction, first.generation_instruction)
    assert.deepEqual(summary.generationContextSelectors, ['project.style'])
    await env.save()
    assert.deepEqual(env.calls.filter(call => call.method === 'patch').at(-1)?.data, { generation_mode: generationMode })
    assert.equal(env.rawRules[0].field_definition_version, exactVersionId)
  }
  await env.open(state.rules.value[0])
  state.ruleForm.languagePolicy = 'preserve_source'; await flush()
  state.ruleForm.generationMode = 'translate_only'; await flush()
  assert.equal(state.draftRulePath.value.languagePolicy, '', 'explicit translation requires a target, never guesses English')
  assert.ok(state.editorValidation.value.some((message: string) => /语言/.test(message)))
  assert.equal(state.ruleForm.generationInstruction, first.generation_instruction)
  assert.deepEqual(state.ruleForm.generationContextSelectors, ['project.style'])
  assert.deepEqual(env.profile, { ...sourceBefore, field_rule_count: 1 })
})

test('strategy author hydration keeps another rule source and updates only changed fields', async context => {
  const env=await author(context); await env.open(); await env.save()
  const {state}=env
  await env.open(state.rules.value[0])
  const other={...clone(state.rules.value[0]),id:id(6),sourceSelector:'request.theme',sourcePath:null,generationSourceSelector:'request.action'}
  state.openRule(other); await flush()
  assert.equal(state.ruleForm.generationSourceSelector,'request.action','opening must not act as a user changing source')
  assert.equal(state.editorDirty.value,false)
  await env.open(state.rules.value[0]); state.ruleForm.position=7
  env.rawRules[0].generation_instruction='另一窗口已保存说明'
  await state.submitEditor()
  const patch=env.calls.filter(call=>call.method==='patch').at(-1)
  assert.deepEqual(patch?.data,{position:7})
  assert.equal(env.rawRules[0].generation_instruction,'另一窗口已保存说明')
})

test('strategy author double click, uncertain write and readonly recovery never resend', async context => {
  const env=await author(context); await env.open()
  let reject!: (cause:unknown)=>void
  env.handlers.set('post /prompt-profile-field-rules/',()=>new Promise((_,no)=>{reject=no}))
  const pending=env.state.submitEditor(); await flush(); await env.state.submitEditor()
  assert.equal(env.calls.filter(call=>call.method==='post').length,1)
  reject({response:{status:504}}); await pending
  assert.equal(env.state.writeUnknown.value.kind,'rule'); assert.equal(env.state.editorOpen.value,true)
  await env.state.submitEditor(); await env.state.readWriteResult()
  assert.equal(env.calls.filter(call=>call.method==='post').length,1)
  assert.equal(env.state.recoveryChecked.value,true)
  assert.deepEqual(env.state.recoveryRules.value,[])
  assert.ok(env.state.ruleForm.generationInstruction.includes('多语言'))
  env.state.closeEditor(); assert.equal(env.state.discardOpen.value,true)
  env.state.keepEditing(); assert.equal(env.state.editorOpen.value,true)
  env.state.discardEditor(); assert.equal(env.state.editorOpen.value,false)
})

test('strategy rejected fields locate sections, preserve input and allow deliberate correction', async context => {
  const env=await author(context); await env.open()
  env.handlers.set('post /prompt-profile-field-rules/',()=>{throw {response:{status:400,data:{generation_instruction:['需要调整原文']}}}})
  const before=env.state.editorSnapshot(); await env.state.submitEditor()
  assert.equal(env.state.editorSnapshot(),before)
  assert.equal(env.state.writeUnknown.value,null)
  assert.deepEqual(env.state.editorIssues.value,[{field:'generation_instruction',message:'需要调整原文',section:'process'}])
  assert.deepEqual(env.state.authorSections.value.map((x:any)=>x.key),['identity','inputs','process','output'])
  env.state.closeEditor(); assert.equal(env.state.discardOpen.value,true)
  env.state.keepEditing(); assert.equal(env.state.editorSnapshot(),before)
})

test('strategy known save remains successful when the following catalog read fails', async context => {
  const env=await author(context); await env.open()
  env.handlers.set('get /prompt-profile-versions/',()=>{throw Error('目录暂不可读')})
  await env.state.submitEditor()
  assert.equal(env.rawRules.length,1); assert.equal(env.state.editorOpen.value,false)
  assert.equal(env.state.writeUnknown.value,null)
  assert.match(env.state.notice.value,/保存已成功.*重读失败/)
})

test('strategy clone freezes exact source and opens only its returned independent draft', async context => {
  const env=await author(context)
  env.profile.status='published'; env.profile.review_state='ready'; await env.state.refresh()
  const sourceBefore=clone(env.profile)
  env.state.openCloneSuccessor(); env.state.cloneName.value='后继 · 中文 🎬'
  const next={...clone(env.profile),id:id(9),version:4,status:'draft',name:env.state.cloneName.value}
  env.handlers.set(`post /prompt-profile-versions/${profileId}/clone-successor/`,payload=>{
    assert.deepEqual(payload,{expected_source_contract_sha256:env.profile.contract_sha256,name:'后继 · 中文 🎬'}); return next
  })
  env.handlers.set('get /prompt-profile-versions/',()=>page([env.profile,next]))
  await env.state.cloneSuccessor(); await flush()
  assert.deepEqual(env.profile,sourceBefore)
  assert.equal(env.state.selectedProfileId.value,next.id); assert.equal(env.state.profileForm.id,next.id)
  assert.equal(env.state.editorOpen.value,true); assert.equal(env.state.profileForm.name,next.name)
  assert.deepEqual(env.state.authorSections.value.map((x:any)=>x.key),['identity','inputs','process','output'])
})

test('strategy rule save selects its own profile even when entering through another exact version',async context=>{
  const env=await author(context,'custom_motion',{promptProfileVersionId:profileId})
  const next={...clone(env.profile),id:id(17),name:'另一个精确策略'}
  env.handlers.set('get /prompt-profile-versions/',()=>page([env.profile,next]))
  await env.state.refresh(); env.state.selectedProfileId.value=next.id
  await env.open(); await env.state.submitEditor()
  assert.equal(env.state.selectedProfileId.value,next.id)
  assert.equal(env.rawRules[0].profile_version,next.id)
})

test('strategy resource drilldown keeps close confirmation outside the hidden owner slot',async()=>{
  const code=await readFile(new URL('../src/components/PromptProfileSettings.vue',import.meta.url),'utf8')
  const modal=code.slice(code.indexOf('<BaseModal :open="editorOpen'),code.indexOf('<BaseModal :open="Boolean(configurationSnapshot)'))
  assert.ok(modal.indexOf('<ConfigurationDiscardNotice')<modal.indexOf('<RegistryResourceExplorer'))
  assert.match(modal,/inspect\('structure',profileForm.inputStructureVersion,\$event\)/)
  assert.match(modal,/inspect\('field_version',ruleForm.fieldDefinitionVersion,\$event\)/)
  assert.match(modal,/<option value="">请选择取值来源<\/option>/)
  assert.match(modal,/<option value="">请选择输出通道<\/option>/)
  assert.doesNotMatch(code.slice(code.indexOf('<style scoped>')),/font-size:(8|9|10|11)px/)
})

test('strategy uncertain clone loads only an explicitly chosen freshly read draft and never resends',async context=>{
  const env=await author(context);env.profile.status='published';await env.state.refresh()
  env.state.openCloneSuccessor()
  const next={...clone(env.profile),id:id(21),version:4,status:'draft',name:'回读后继'}
  env.handlers.set(`post /prompt-profile-versions/${profileId}/clone-successor/`,()=>{throw {response:{status:504}}})
  env.handlers.set('get /prompt-profile-versions/',()=>page([env.profile,next]))
  env.handlers.set(`get /prompt-profile-versions/${next.id}/`,()=>({...next,id:'wrong'}))
  await env.state.cloneSuccessor();await env.state.readWriteResult()
  assert.equal(env.state.writeUnknown.value.kind,'clone')
  assert.deepEqual(env.state.recoveryProfiles.value.map((x:any)=>x.id),[next.id])
  await env.state.loadRecoveredProfile(next.id)
  assert.match(env.state.recoveryError.value,/目标不是/);assert.ok(env.state.writeUnknown.value)
  env.handlers.set(`get /prompt-profile-versions/${next.id}/`,()=>next)
  await env.state.loadRecoveredProfile(next.id)
  assert.equal(env.state.profileForm.id,next.id);assert.equal(env.state.writeUnknown.value,null)
  assert.equal(env.state.recoveryBusy.value,false)
  assert.equal(env.calls.filter(call=>call.method==='post').length,1)
  assert.match(env.state.notice.value,/不据此认定/)
})

test('real Profile author saves and reopens arbitrary fields with exact versions and generation/source/language contracts', async context => {
  // Neither key has a product-specific branch. The same author, API adapter and
  // saved configuration reader handle both, including punctuation in the key.
  for (const fieldKey of ['custom_camera_orbit', 'custom.sound_texture']) {
    const env = await author(context, fieldKey)
    await env.open()
    const created = await env.save()
    assert.equal(created.field_definition_version, exactVersionId)
    assert.deepEqual(created.source_path, ['notes.custom'])
    assert.equal(created.generation_mode, 'model_generated')
    assert.equal(created.language_policy, 'inherit')
    assert.equal(created.generation_min, null)
    await env.open(env.state.rules.value[0])
    assert.deepEqual(env.state.buildRulePayload(), created)
    assert.equal(env.state.selectedRuleField.value.label, env.exactVersion.label)
    for (const [generationMode, languagePolicy, required] of [
      ['translate_only', 'bilingual:en+zh-CN', true], ['model_generated', 'zh-Hant', false], ['passthrough', 'preserve_source', false],
    ] as const) {
      Object.assign(env.state.ruleForm, { generationMode, languagePolicy, generationRequired: required,
        generationValidatorKeys: required ? ['non_empty'] : [], generationContextSelectors: [], omitIfEmpty: false })
      await flush()
      const saved = await env.save()
      assert.equal(saved.language_policy, languagePolicy); assert.equal(saved.generation_required, required)
      assert.equal(saved.generation_source_selector, source); assert.equal(saved.omit_if_empty, false)
      assert.deepEqual(saved.generation_context_selectors, [])
      await env.open(env.state.rules.value[0])
      assert.deepEqual(env.state.buildRulePayload(), saved)
    }
    // Author readiness is not runtime approval/adoption. The latter belongs to
    // the original preparation's frozen review_contract, tested below.
    env.state.openProfile(env.state.selectedProfile.value); await flush()
    assert.equal(env.state.profileForm.reviewState, 'needs_review')
    env.state.profileForm.reviewState = 'ready'
    assert.deepEqual(env.state.editorValidation.value, [])
    await env.state.submitEditor(); await flush()
    assert.equal(env.state.editorError.value, '')
    env.state.openProfile(env.state.selectedProfile.value); await flush()
    assert.equal(env.state.profileForm.reviewState, 'ready')
    assert.equal(env.state.buildProfilePayload().review_state, 'ready')
    env.state.editorOpen.value = false; await flush()
    const { default: Viewer } = await env.vite.ssrLoadModule('/src/components/PromptProfileConfiguration.vue')
    const html = await renderToString(createSSRApp({ render: () => h(Viewer, {
      profile: env.state.selectedProfile.value, rules: env.state.rules.value, options: env.state.options.value,
    }) }))
    for (const value of [fieldKey, exactVersionId, 'preserve_source', 'notes.custom', env.exactVersion.label]) assert.ok(html.includes(value), value)
    assert.doesNotMatch(html, /当前字典名称不可代替确切版本/)
    assert.equal(env.calls.filter(call => call.method === 'post').length, 1)
    assert.equal(env.calls.filter(call => call.method === 'patch').length, 4)
  }
})

test('language and creative requirements guide generation without becoming execution gates', async () => {
  const template = await readFile(new URL('../src/components/PromptProfileSettings.vue', import.meta.url), 'utf8')
  assert.ok(template.includes('作为生成指导发给模型，不按文字语种阻断执行'))
  assert.ok(template.includes('对白原文、名称与导演描述可按策略使用不同语言'))
  assert.ok(template.includes('执行只检查必填、类型、JSON 结构与安全字符'))
  assert.ok(template.includes('长度上下限始终只作提醒，不截断内容'))
})

test('author rejects missing, foreign or unavailable definition versions and invalid translation contracts before saving', async context => {
  const env = await author(context)
  await env.open()
  for (const candidate of [null, { ...env.exactVersion, id: id(99) }, { ...env.exactVersion, field_definition: id(99) }, { ...env.exactVersion, status: 'retired' }]) {
    env.state.selectRuleVersion(candidate)
    assert.ok(env.state.editorValidation.value.some((error: string) => error.includes('字段定义版本')))
    await env.state.submitEditor()
  }
  env.state.selectRuleVersion(env.exactVersion)
  env.state.ruleForm.generationMode = 'translate_only'; await flush()
  for (const language of ['preserve_source', 'any', 'bilingual:en+en', 'not a language']) {
    env.state.ruleForm.languagePolicy = language
    assert.ok(env.state.editorValidation.value.some((error: string) => /语言|翻译/.test(error)))
    await env.state.submitEditor()
  }
  env.state.ruleForm.languagePolicy = 'en'; env.state.ruleForm.generationInstruction = ''
  assert.ok(env.state.editorValidation.value.some((error: string) => error.includes('生成说明')))
  await env.state.submitEditor()
  assert.equal(env.calls.filter(call => call.method !== 'get').length, 0)
})

function executionRecords(payload: any, exactVersion: any) {
  const fieldKey = exactVersion.field_key
  const fieldContract = { ...clone(payload), field_key: fieldKey, field_definition: {
    field_definition_id: definitionId, field_definition_version_id: exactVersionId, key: fieldKey, label: exactVersion.label, version: 2,
  } }
  const field = { id: id(20), preparation: id(10), unit_key: 'unit:custom', field_key: fieldKey, revision: 2,
    source_sha256: 'b'.repeat(64), field_contract_sha256: 'c'.repeat(64), text_sha256: 'd'.repeat(64),
    origin_kind: 'model', origin_attempt: id(30), origin_asset_job: null, status: 'pending',
    approval_kind: null, approval_label: '尚未审核', reviewed_by: null, reviewed_at: null, approval_policy_sha256: null,
    created_by: 1, created_at: stamp, updated_at: stamp, warnings: [], text: '合成修订正文：环绕 Élodie，保持动作连续。\n<script>not executable</script>' }
  const preparation = { id: id(10), stage_run: null, asset_job: id(11), deployment_smoke: null, workflow_smoke: null,
    execution_instance_version: id(12), purpose: 'production', unit_key: 'unit:custom', request_key: 'fixture', revision: 3,
    status: 'awaiting_review', intent_sha256: 'e'.repeat(64), request_sha256: null, request_frozen_at: null, next_check_at: null,
    reason_code: '', reason: '', evaluation_revision: null, evaluated_at: null, evaluation: null, cancellation: null,
    presentation: { kind: 'primary', label: '隔离主准备', operation: 'video.generate', parent: null, task: null },
    outcome: { message: '辅助已成功，主调用尚未执行。', artifact_count: 0, selected_count: 0 },
    created_by: 1, created_at: stamp, updated_at: stamp, intent: {}, resolved_inputs: {},
    review_contract: { schema_version: 1, mode: 'manual' }, review_summary: { mode: 'manual', label: '人工确认字段', description: '确认不等于采用' },
    prepared_request: null, capacity_sample: null, response_diagnostic: null, resource_review: null, text_response_review: null,
    auxiliary_batches: [{ field_batch_key: 'f'.repeat(64), purpose: 'generate_fields', field_keys: [fieldKey], attempt_number: 1,
      task_id: id(30), preparation_id: id(31), status: 'succeeded', reason: '', can_retry: false }],
    field_slots: [{ unit_key: field.unit_key, field_key: fieldKey, next_revision: 3, field_label: exactVersion.label,
      latest_value_id: field.id, source_language: 'zh-CN', target_language: 'en', effective_language: 'en', field_contract: fieldContract }],
    field_selections: [] as any[],
  }
  return { field, preparation }
}

async function workbench(env: Awaited<ReturnType<typeof author>>, records: ReturnType<typeof executionRecords>) {
  const fields = new Map<string, any>([[records.field.id, records.field]])
  env.handlers.set(`get /execution-preparations/${records.preparation.id}/`, () => records.preparation)
  const register = (field: any) => { fields.set(field.id, field); env.handlers.set(`get /execution-field-values/${field.id}/`, () => field) }
  register(records.field)
  env.handlers.set('get /execution-field-values/', query => { assert.equal(query.params.preparation_id, records.preparation.id); return page([...fields.values()]) })
  const view = await env.component('/src/components/ExecutionPreparationWorkbench.vue', reactive({
    preparationId: records.preparation.id, versionId: records.preparation.execution_instance_version, purpose: 'production',
  }))
  await flush()
  assert.equal(view.state.error.value, ''); assert.equal(view.state.fieldError.value, '')
  return { ...view, register }
}

test('saved author contract reaches the real auxiliary reader: generated text, revision and language are not adoption', async context => {
  const env = await author(context)
  await env.open(); const payload = await env.save()
  const records = executionRecords(payload, env.exactVersion)
  const view = await workbench(env, records)
  const writesBefore = env.calls.filter(call => call.method !== 'get').length
  assert.equal(view.state.field.value.originAttempt, records.field.origin_attempt)
  assert.deepEqual(clone(view.state.reviewSlot.value.fieldContract.source_path), payload.source_path)
  let html = await view.render()
  for (const text of ['自定义环绕', '环绕 Élodie', '修订 2', '策略目标语言（生成指导）：英语', '人工确认字段', '尚未采用这份正文', '尚未审核', 'inherit']) assert.ok(html.includes(text), text)
  const contract = await env.component('/src/components/ReadonlyDataValue.vue', { value: view.state.reviewSlot.value.fieldContract })
  contract.state.expanded.value = true
  assert.ok((await contract.render()).includes(exactVersionId), 'Expanding the real contract reader exposes the exact referenced version')
  assert.match(html, /&lt;script&gt;not executable&lt;\/script&gt;/); assert.doesNotMatch(html, /<script>/)
  assert.doesNotMatch(html, /此修订已明确选入本次请求/)
  assert.equal(view.state.preparation.value.fieldSelections.length, 0)
  records.field.status = 'confirmed'; records.field.approval_kind = 'human' as any
  records.field.approval_label = '人工审核'; records.field.reviewed_by = 1 as any; records.field.reviewed_at = stamp as any
  await view.state.openField(records.field.id)
  html = await view.render()
  assert.match(html, /人工审核/); assert.match(html, /尚未采用这份正文/)
  assert.equal(env.calls.filter(call => call.method !== 'get').length, writesBefore, 'Reading a success or confirmation never writes adoption')
})

test('new generated revisions preserve an old selected revision; policy approval is not presented as human review', async context => {
  const env = await author(context, 'custom.sound_texture')
  await env.open(); env.state.ruleForm.languagePolicy = 'bilingual:en+zh-CN'
  const records = executionRecords(await env.save(), env.exactVersion)
  const old = { ...records.field, id: id(21), revision: 1, text: '已明确采用的旧正文', status: 'confirmed', approval_kind: 'human', approval_label: '人工审核', reviewed_by: 1, reviewed_at: stamp }
  records.preparation.field_selections = [{ id: id(22), preparation: records.preparation.id, unit_key: old.unit_key, field_key: old.field_key,
    field_value: old.id, revision: 1, selected_by: 1, selected_at: stamp }]
  env.handlers.set(`get /execution-field-values/${old.id}/`, () => old)
  const view = await workbench(env, records); view.register(old)
  assert.equal(view.state.field.value.id, old.id)
  assert.match(await view.render(), /此修订已明确选入本次请求/)
  await view.state.loadHistory(1); await view.state.openField(records.field.id)
  assert.match(await view.render(), /尚未采用这份正文/)
  assert.equal(view.state.currentSelection.value.fieldValue, old.id)
  assert.equal(view.state.replacing.value, true)
  assert.equal(view.state.replaceAcknowledged.value, false)
  records.preparation.review_contract.mode = 'policy'
  records.preparation.review_summary = { mode: 'policy', label: '配置规则自动批准', description: '不是人工审核记录' }
  Object.assign(records.field, { status: 'confirmed', approval_kind: 'policy', approval_label: '配置规则批准', approval_policy_sha256: '9'.repeat(64) })
  records.preparation.field_slots[0]!.effective_language = 'bilingual:en+zh-CN'
  records.preparation.field_selections[0].field_value = records.field.id
  await view.state.load(); await view.state.openSlot(view.state.preparation.value.fieldSlots[0])
  const html = await view.render()
  const languageNames = new Intl.DisplayNames(['zh-CN'], { type: 'language' })
  const bilingualLabel = ['en', 'zh-CN'].map(tag => languageNames.of(tag)).join(' + ')
  for (const text of ['配置规则自动批准', '配置规则批准', '不是人工审核记录', bilingualLabel, '此修订已明确选入本次请求', '9'.repeat(64)]) assert.ok(html.includes(text), text)
  assert.equal(view.state.reviewSlot.value.effectiveLanguage, 'bilingual:en+zh-CN')
  assert.equal(view.state.field.value.reviewedBy, null)
  assert.equal(view.state.field.value.approvalKind, 'policy')
  assert.equal(env.calls.filter(call => call.method !== 'get').length, 1, 'Only the isolated author save occurred; no review, retry or execution calls')
})

test('editing a generated field saves an exact new pending revision without overwriting, approval or adoption', async context => {
  const env = await author(context)
  await env.open(); const records = executionRecords(await env.save(), env.exactVersion)
  const view = await workbench(env, records), original = clone(records.field)
  const body = '  手工补充 Élodie 的动态描述。\n保留换行与原语言。  '
  let saved: any
  env.handlers.set('post /execution-field-values/', payload => {
    assert.deepEqual(payload, { preparation_id: records.preparation.id, unit_key: records.field.unit_key,
      field_key: env.fieldKey, text: body, revision: 3, expected_preparation_revision: 3 })
    saved = { ...original, id: id(23), revision: 3, text: payload.text, text_sha256: '8'.repeat(64),
      origin_kind: 'manual', origin_attempt: null }
    records.preparation.revision++
    records.preparation.field_slots[0]!.next_revision = 4
    records.preparation.field_slots[0]!.latest_value_id = saved.id
    view.register(saved)
    return { field: saved, preparation: records.preparation, selection: null }
  })
  view.state.editField(); assert.equal(view.state.text.value, original.text)
  view.state.text.value = body; await flush(); await view.state.createField(); await flush()
  assert.equal(view.state.error.value, '')
  assert.equal(view.state.field.value.id, id(23)); assert.equal(view.state.field.value.text, body)
  assert.equal(view.state.field.value.status, 'pending'); assert.equal(view.state.field.value.approvalKind, null)
  assert.equal(view.state.preparation.value.fieldSelections.length, 0)
  assert.deepEqual(records.field, original, 'The generated revision is still intact')
  assert.match(await view.render(), /已保存待确认修订，尚未采用，也没有触发模型调用/)
  await view.state.openField(original.id)
  assert.equal(view.state.field.value.text, original.text)
  assert.deepEqual(env.calls.filter(call => call.method !== 'get').map(call => call.url), ['/prompt-profile-field-rules/', '/execution-field-values/'])
})

test('real Profile draft can explicitly change projection with existing rules, without deleting or transforming them', async context => {
  const env = await author(context)
  await env.open(); await env.save()
  env.options.source_projection_keys.push(...choices('declarative_v1'))
  env.options.source_selectors.push({ value: 'projection.output', label: '声明式输出', group_label: '投影',
    description: '已配置原语输出', record_location: '冻结来源', execution_usage: '按明确规则消费' })
  Object.assign(env.options, {
    source_projection_primitives: [{ value: 'reference_context.subject_definitions', label: '参考主体', description: '确切参考主体',
      engine_key: 'reference_context_ir_v1', rendering_template_keys: ['reference', 'entity_reference'] }],
    reference_rendering_fields: ['reference', 'entity_reference', 'retention', 'reference_usage', 'shot', 'reviewed_shot',
      'summary', 'shot_opening', 'shot_transition'].map(key => ({ key, label: key, description: key,
        assembly: '隔离作者控件目录', requirement: '显式配置', placeholders: ['label'],
        placeholder_help: { label: { label: '素材标签', description: '隔离动态值', example: '<Picture 1>' } },
        example: { template: '{label}', result: '<Picture 1>' } })),
  })
  await env.state.refresh(); await flush()
  assert.equal(env.state.error.value, '')
  const rulesBefore = clone(env.rawRules), savedBefore = clone(env.profile)
  env.state.openProfile(env.state.selectedProfile.value); await flush()
  // The actual setup/watch validation and saved API adapter run with the retained rules.
  env.state.profileForm.sourceProjectionKey = 'declarative_v1'
  Object.assign(env.state.profileForm, {
    renderingTemplates: Object.fromEntries(env.options.reference_rendering_fields.map(row => [row.key, '{label}'])),
    renderingSeparator: '\n', renderingListPrefix: '- ', renderingTemplateKey: 'isolated-author-template',
    renderingTaskSeparator: '\n', renderingUnreferencedTask: 'text-only',
  })
  await flush()
  assert.equal(env.state.profileProjectionChanged.value, true)
  assert.deepEqual(env.state.editorValidation.value, [])
  env.handlers.set(`patch /prompt-profile-versions/${profileId}/`, payload => {
    assert.deepEqual(Object.keys(payload).sort(), ['source_projection_config', 'source_projection_key'])
    assert.equal(payload.source_projection_key, 'declarative_v1')
    assert.deepEqual(payload.source_projection_config, clone(env.state.buildProjectionConfig()))
    Object.assign(env.profile, payload); return env.profile
  })
  await env.state.submitEditor(); await flush()
  assert.equal(env.state.editorError.value, '')
  assert.deepEqual(env.rawRules, rulesBefore)
  assert.deepEqual(clone(env.profile), { ...savedBefore, source_projection_key: 'declarative_v1',
    source_projection_config: clone(env.state.buildProjectionConfig()) })
  env.state.openProfile(env.state.selectedProfile.value); await flush()
  assert.equal(env.state.profileForm.sourceProjectionKey, 'declarative_v1')
  assert.equal(env.state.profileProjectionChanged.value, false)
  assert.deepEqual(env.state.profileForm.renderingTemplates, env.profile.source_projection_config.primitive_options.reference_context_ir_v1.rendering.templates)
  assert.deepEqual(env.calls.filter(call => call.method !== 'get').map(call => call.url), [
    '/prompt-profile-field-rules/', `/prompt-profile-versions/${profileId}/`,
  ])
})
