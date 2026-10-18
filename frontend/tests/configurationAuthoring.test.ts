import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { createServer } from 'vite'
import { createSSRApp, createRenderer, h, nextTick, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { configuration, instance, version, deployments, workflows, profiles, choices, authoring } from './fixtures/execution-authoring-data.ts'
import { modeScenarios, derivedInput, scenarioChoices, scenarioAuthoring } from './fixtures/execution-version-scenarios.ts'

const server = () => createServer({ logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true } })
const flush = async () => { for (let i = 0; i < 15; i++) { await Promise.resolve(); await nextTick() } }

test('configuration footer actions use the shared typography and responsive touch target', async () => {
  const modal = await readFile(new URL('../src/components/BaseModal.vue', import.meta.url), 'utf8')
  assert.match(modal, /\.configuration-modal>\.modal-footer :deep\(\.button\)\{min-height:var\(--config-control-height\);font-size:var\(--config-body-size\)\}/)
  assert.match(modal, /\.configuration-modal>\.modal-body :deep\(\.button\)\{min-height:var\(--config-control-height\);font-size:var\(--config-body-size\)/)
  assert.match(modal, /\.configuration-modal>\.modal-body :deep\(summary\)\{min-height:var\(--config-control-height\);font-size:var\(--config-body-size\)/)
  assert.match(modal, /@media\(max-width:640px\)\{\.configuration-modal\{--config-control-height:44px\}\}/)
})
test('shared disclosure, readonly values and discard actions use the same typography and touch targets', async () => {
  const component = (name: string) => readFile(new URL(`../src/components/${name}.vue`, import.meta.url), 'utf8')
  const [shell, value, discard] = await Promise.all(['ConfigurationAuthoringShell', 'ReadonlyDataValue', 'ConfigurationDiscardNotice'].map(component))
  assert.match(shell, /:deep\(summary\)\{min-height:var\(--config-control-height\);align-content:center;font-size:var\(--config-body-size\)/)
  assert.match(value, /\.readonly-data summary\{[^}]*min-height:var\(--config-control-height\)[^}]*font-size:var\(--config-body-size\)/)
  assert.match(value, /\.readonly-data small\{[^}]*font-size:var\(--config-help-size\)/)
  assert.match(value, /\.readonly-scalar\{[^}]*font-size:var\(--config-body-size\)/)
  assert.match(value, /\.readonly-data pre\{[^}]*font-size:var\(--config-help-size\)/)
  assert.match(value, /@media\(max-width:640px\)\{\.readonly-data\{--config-control-height:44px\}/)
  assert.match(discard, /\.configuration-discard-notice button\{font:500 var\(--config-body-size\)\/1\.5 var\(--font\);min-height:var\(--config-control-height\)/)
  assert.match(discard, /@media\(max-width:640px\)\{\.configuration-discard-notice\{--config-control-height:44px\}/)
  assert.doesNotMatch(value, /font-size:13px/)
})
function mountState(Component: any, props: any, emit = (_event: string, _value: any) => {}) {
  let state: any
  const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
  const app = renderer.createApp({ setup() { state = Component.setup(props, { expose() {}, emit }); return () => null } })
  app.provide(ssrContextKey, {}); app.mount({})
  return { state, close: () => app.unmount() }
}
async function setup(context: any) {
  const vite = await server(); context.after(() => vite.close())
  const { default: Editor } = await vite.ssrLoadModule('/src/components/ExecutionVersionEditor.vue')
  const { executionRegistryApi: client } = await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  const { platformApi } = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  for (const method of ['get', 'post', 'patch', 'put', 'delete', 'request']) api[method] = () => assert.fail(`unexpected network ${method}`)
  client.choices = async () => structuredClone(choices)
  client.authoring = async () => structuredClone(authoring)
  platformApi.listPromptProfileVersions = async () => profiles
  platformApi.listWorkflowVersions = async () => workflows
  platformApi.getPromptProfileOptions = async () => ({ sourceSelectors: [] })
  return { vite, Editor, client }
}

for (const scenario of modeScenarios) test(`version mode matrix ${scenario.mode}: original create, saved catalog and differential patch preserve source`, async context => {
  const { Editor, client } = await setup(context)
  client.choices = async (deployment?: string) => scenarioChoices(deployment)
  let saved: any, creates = 0, patches = 0
  client.publish = client.clone = () => assert.fail('new author must not publish or clone')
  const fresh = mountState(Editor, { instance: scenario.family, version: null, deployments }, (event, value) => { if (event === 'saved') saved = value }); context.after(fresh.close)
  const state = fresh.state; await flush()
  assert.equal(state.draft.value.deployment_id, '')
  const config = scenario.source.configuration
  for (const key of ['deployment_id', 'workflow_version_id', 'operation', 'mode', 'prompt_mode', 'prompt_profile_version_id']) if (config[key]) await state.changedSelection(key, config[key])
  assert.equal(state.canSave.value, true); assert.equal(state.options.value, null)
  client.createVersion = async (id: string, revision: number, payload: any) => {
    creates++; assert.equal(id, scenario.family.id); assert.equal(revision, scenario.family.revision)
    assert.equal(payload.prompt_mode, scenario.mode); assert.equal(payload.workflow_version_id, config.workflow_version_id)
    assert.equal(payload.prompt_profile_version_id, config.prompt_profile_version_id)
    assert.deepEqual(payload.inputs, []); assert.deepEqual(payload.auxiliaries, [])
    return { ...scenario.source, id: 'created', version: 2, revision: 1, status: 'draft', configuration: { ...structuredClone(payload), inputs: scenario.mode === 'profile' ? [structuredClone(derivedInput)] : [] } }
  }
  await state.save(); assert.equal(creates, 1)
  client.authoring = async (id: string) => { assert.equal(id, saved.id); return scenarioAuthoring(saved) }
  client.versions = async () => ({ count: 0, next: null, results: [] })
  const edit = mountState(Editor, { instance: scenario.family, version: saved, deployments }); context.after(edit.close); await flush()
  assert.ok(edit.state.options.value); assert.equal(edit.state.configurationChanged.value, false)
  assert.deepEqual(edit.state.options.value.derived_input_keys, scenario.mode === 'profile' ? ['creative_brief'] : [])
  edit.state.draft.value.parameters = [{ ...configuration.parameters[0], default_value: 0 }]
  client.updateVersion = async (id: string, patch: any) => {
    patches++; assert.equal(id, saved.id); assert.equal(patch.expected_revision, 1)
    assert.deepEqual(Object.keys(patch).sort(), ['expected_revision', 'parameters']); assert.equal(patch.parameters[0].default_value, 0)
    return { ...saved, revision: 2, configuration: structuredClone(edit.state.draft.value) }
  }
  await edit.state.save(); assert.equal(patches, 1); assert.equal(creates, 1)
  assert.equal(scenario.source.status, 'published'); assert.equal(scenario.source.configuration.parameters[0].default_value, 0)
})

test('successor comparison groups only explicit changes and never invents persistent lineage or a writer', async context => {
  const { Editor } = await setup(context)
  const source = structuredClone(modeScenarios[0].source), successor = { ...structuredClone(source), id: 'new-successor', status: 'draft', revision: 1, version: 2 }
  const props = { instance: modeScenarios[0].family, version: successor, sourceVersion: source, deployments }
  const { state, close } = mountState(Editor, props); context.after(close); await flush()
  assert.deepEqual(state.successorChanges.value, [])
  state.draft.value.parameters[0].default_value = 1.25
  state.draft.value.workflow_version_id = 'new-explicit-workflow'
  state.draft.value.output_version_id = 'output-new'
  assert.deepEqual(state.successorChanges.value, [
    { title: '能力与实现', fields: ['工作流版本'] }, { title: '输入准备', fields: ['参数覆盖'] }, { title: '输出约定', fields: ['输出版本'] },
  ])
  assert.equal(source.configuration.parameters[0].default_value, 0)
  state.draft.value = structuredClone(source.configuration); assert.deepEqual(state.successorChanges.value, [])
  const html = await renderToString(createSSRApp(Editor, props)); assert.match(html, /后继配置变更范围/); assert.match(html, /包含当前未保存修改/)
  for (const invalid of [null, { ...source, id: successor.id }, { ...source, instance_id: 'another-family' }]) {
    const mounted = mountState(Editor, { ...props, sourceVersion: invalid }); context.after(mounted.close)
    assert.equal(mounted.state.successorChanges.value, null)
  }
})

test('shared authoring sections remain mounted, labelled and visible; navigation never submits', async context => {
  const vite = await server(); context.after(() => vite.close())
  const { default: Shell } = await vite.ssrLoadModule('/src/components/ConfigurationAuthoringShell.vue')
  const html = await renderToString(createSSRApp({ render: () => h(Shell, { label: '示例配置', status: '尚未保存', sections: [
    { key: 'input', title: '输入', description: '来源定义' }, { key: 'output', title: '输出', description: '输出约定' },
  ] }, { input: () => h('input', { required: true, value: '保留未提交值' }), output: () => h('input', { type: 'number', value: 0 }) }) }))
  assert.match(html, /尚未保存/)
  assert.equal((html.match(/type="button"/g) || []).length, 2)
  assert.equal((html.match(/data-authoring-section=/g) || []).length, 2)
  assert.match(html, /value="保留未提交值"/); assert.match(html, /value="0"/)
  assert.doesNotMatch(html.replace(/<!--[\s\S]*?-->/g, ''), /display:\s*none|\shidden[=> ]|role="tab"/)
  const { state, close } = mountState(Shell, { label: '', status: '', sections: [] }); context.after(close)
  const actions: any[] = []
  state.root.value = { querySelectorAll: () => [{ dataset: { authoringSection: 'input' }, querySelectorAll: () => [], scrollIntoView: (value: any) => actions.push(value), focus: (value: any) => actions.push(value) }] }
  state.locate('input'); assert.deepEqual(actions, [{ block: 'start' }, { preventScroll: true }])
  state.locate('missing'); assert.equal(actions.length, 2)
})

test('new version uses the original create writer once; navigation and selection do not save or publish', async context => {
  const { Editor, client } = await setup(context)
  let creates = 0, saved: any
  client.publish = client.smoke = () => assert.fail('authoring must not publish or execute')
  const { state, close } = mountState(Editor, { instance, version: null, deployments }, (event, result) => { if (event === 'saved') saved = result }); context.after(close)
  await flush(); assert.match(state.authoringStatus.value, /尚未保存/)
  await state.changedSelection('deployment_id', configuration.deployment_id)
  await state.changedSelection('workflow_version_id', configuration.workflow_version_id)
  await state.changedSelection('operation', configuration.operation)
  await state.changedSelection('mode', configuration.mode)
  await state.changedSelection('prompt_mode', 'manual')
  assert.equal(creates, 0); assert.equal(state.options.value, null)
  assert.equal(state.saveLabel.value, '创建草稿并配置输入')
  let finish: (value: any) => void = () => {}
  client.createVersion = (id: string, revision: number, payload: any) => {
    creates++; assert.equal(id, instance.id); assert.equal(revision, instance.revision)
    assert.equal(payload.prompt_mode, 'manual'); assert.deepEqual(payload.inputs, [])
    return new Promise(resolve => { finish = resolve })
  }
  const request = state.save(); assert.match(state.authoringStatus.value, /正在保存/)
  await state.save(); assert.equal(creates, 1)
  finish(version); await request; assert.equal(saved.id, version.id)
})

test('draft failure retains values and original revision; output selection is locked during combination confirmation', async context => {
  const { Editor, client } = await setup(context)
  const original = structuredClone(version)
  const { state, close } = mountState(Editor, { instance, version: original, deployments }); context.after(close)
  await flush(); state.draft.value.parameters[0].default_value = 0.75
  const edits = JSON.stringify(state.draft.value)
  client.updateVersion = async (id: string, patch: any) => {
    assert.equal(id, version.id); assert.equal(patch.expected_revision, version.revision)
    assert.deepEqual(Object.keys(patch).sort(), ['expected_revision', 'parameters'])
    throw { response: { status: 400, data: { message: '隔离保存失败' } } }
  }
  await state.save(); assert.match(state.authoringStatus.value, /保存失败/)
  assert.equal(JSON.stringify(state.draft.value), edits); assert.deepEqual(original, version)
  await state.changedSelection('prompt_mode', 'profile')
  state.choices.value.target.operations[0].output_parser_keys = ['fixture.parser']
  state.chooseOutput({ id: 'forbidden-output', status: 'published', parser_key: 'fixture.parser' })
  assert.equal(state.draft.value.output_version_id, undefined)
  state.cancelSelection(); assert.equal(JSON.stringify(state.draft.value), edits)
})

test('authoring modes expose text preparation under inputs without a fabricated AI branch', async context => {
  const { Editor } = await setup(context)
  for (const mode of ['manual', 'profile', 'none']) {
    const source: any = structuredClone(version); source.configuration.prompt_mode = mode
    source.configuration.prompt_profile_version_id = mode === 'profile' ? 'fixture-profile' : null
    const Wrapper = { ...Editor, setup(props: any, ctx: any) {
      const state = Editor.setup(props, ctx)
      state.choices.value = structuredClone(choices); state.options.value = structuredClone(authoring)
      state.profiles.value = profiles; state.workflows.value = workflows
      return state
    } }
    const html = await renderToString(createSSRApp(Wrapper, { instance, version: source, deployments }))
    const capability = html.indexOf('data-authoring-section="capability"'), inputs = html.indexOf('data-authoring-section="inputs"'), output = html.indexOf('data-authoring-section="output"')
    assert.ok(capability < inputs && inputs < output)
    assert.ok(html.indexOf('>提示词方式<') > inputs && html.indexOf('>提示词方式<') < output)
    assert.equal((html.match(/class="execution-config"/g) || []).length, 1)
    assert.match(html, /不会自动开始/)
    if (mode === 'profile') assert.match(html, /按需辅助文字生成与翻译/)
    else assert.doesNotMatch(html, /<h4>按需辅助文字生成与翻译/)
  }
})

test('switching implementations clears the old model identity until the exact choices return', async context => {
  const { Editor, client } = await setup(context)
  const { state, close } = mountState(Editor, { instance, version: structuredClone(version), deployments }); context.after(close)
  await flush(); assert.ok(state.target.value.model_profile_version_id)
  let resolve: (value: any) => void = () => {}, reject: (value: any) => void = () => {}
  client.choices = () => new Promise((done, fail) => { resolve = done; reject = fail })
  await state.changedSelection('deployment_id', 'new-deployment')
  const updating = state.confirmSelection()
  assert.equal(state.draft.value.deployment_id, 'new-deployment')
  assert.equal(state.loading.value, true); assert.equal(state.choices.value, null); assert.equal(state.target.value, undefined)
  resolve({ ...structuredClone(choices), target: { ...structuredClone(choices.target), model_profile_version_id: 'new-exact-model' } })
  await updating; assert.equal(state.target.value.model_profile_version_id, 'new-exact-model')
  const failed = state.changedSelection('workflow_version_id', 'new-workflow')
  assert.equal(state.target.value, undefined)
  reject(Error('目录无法读取')); await failed
  assert.equal(state.target.value, undefined); assert.equal(state.choices.value, null)
  assert.match(state.catalogError.value, /目录无法读取/)
})

test('readonly and successor share authoring layout without mutating the source version', async context => {
  const { Editor } = await setup(context)
  const source: any = { ...structuredClone(version), status: 'published' }
  let state: any
  const Wrapper = { ...Editor, setup(props: any, ctx: any) { state = Editor.setup(props, ctx); return state } }
  const html = await renderToString(createSSRApp(Wrapper, { instance, version: source, deployments }))
  assert.match(html, /只读版本/); assert.doesNotMatch(html, /type="submit"/)
  await state.changedSelection('prompt_mode', 'none'); assert.equal(state.draft.value.prompt_mode, 'manual')
  const successor = { ...structuredClone(source), id: 'successor', version: 3, revision: 1, status: 'draft' }
  const mounted = mountState(Editor, { instance, version: successor, deployments }); context.after(mounted.close)
  await flush(); mounted.state.draft.value.parameters[0].default_value = 1
  assert.match(mounted.state.authoringStatus.value, /草稿 v3 · 有未保存修改/)
  assert.equal(source.configuration.parameters[0].default_value, 0)
})

test('output details retain exact identity and ignore stale responses', async context => {
  const { Editor, vite } = await setup(context)
  const { businessOutputs } = await vite.ssrLoadModule('/src/services/businessOutputs.ts')
  const pending = new Map<string, (value: any) => void>()
  businessOutputs.detail = (id: string) => new Promise(resolve => pending.set(id, resolve))
  const { state, close } = mountState(Editor, { instance, version: structuredClone(version), deployments }); context.after(close)
  await flush()
  state.draft.value.output_version_id = 'output-a'; await flush()
  state.draft.value.output_version_id = 'output-b'; await flush()
  pending.get('output-a')!({ id: 'output-a', name: 'stale' }); await flush()
  assert.equal(state.selectedOutput.value, null)
  pending.get('output-b')!({ id: 'wrong-output', name: 'wrong' }); await flush()
  assert.equal(state.draft.value.output_version_id, 'output-b')
  assert.equal(state.selectedOutput.value, null)
  assert.match(state.outputError.value, /身份不匹配/)
  state.draft.value.output_version_id = 'output-c'; await flush()
  pending.get('output-c')!({ id: 'output-c', name: 'exact', version: 4 }); await flush()
  assert.equal(state.selectedOutput.value.id, 'output-c')
  assert.equal(state.outputError.value, '')
})

test('authoring issues use only explicit paths; nested field location expands details and focuses the field', async context => {
  const { vite } = await setup(context)
  const { authoringIssues, isExplicitAuthoringRejection } = await vite.ssrLoadModule('/src/services/configurationAuthoring.ts')
  const result = authoringIssues({ response: { status: 400, data: { fields: { 'parameters[0].default_value': ['超过范围'], unfamiliar: ['inputs 尚未读取'] } } } }, { parameters: 'inputs' })
  assert.deepEqual(result, [{ field: 'parameters[0].default_value', message: '超过范围', section: 'inputs' }, { field: 'unfamiliar', message: 'inputs 尚未读取', section: undefined }])
  for (const status of [400, 401, 403, 404, 409, 422, 429]) assert.equal(isExplicitAuthoringRejection({ response: { status } }), true)
  for (const status of [0, 408, 500, 502, 504]) assert.equal(isExplicitAuthoringRejection({ response: { status } }), false)
  const { default: Shell } = await vite.ssrLoadModule('/src/components/ConfigurationAuthoringShell.vue')
  const { state, close } = mountState(Shell, { sections: [] }); context.after(close)
  const actions: string[] = [], details = { tagName: 'DETAILS', open: false, parentElement: null }
  const field = { dataset: { authoringField: 'parameters.0.default_value' }, parentElement: details,
    scrollIntoView: () => actions.push('scroll'), querySelector: () => ({ focus: () => actions.push('field-focus') }) }
  state.root.value = { querySelectorAll: () => [{ dataset: { authoringSection: 'inputs' }, querySelectorAll: () => [field] }] }
  state.locate('inputs', 'parameters[0].default_value')
  assert.equal(details.open, true); assert.deepEqual(actions, ['scroll', 'field-focus'])
})

test('unknown draft save blocks writes; readback and explicit load never assert this request succeeded', async context => {
  const { Editor, client } = await setup(context)
  let writes = 0, reads = 0, recovered: any, saved = 0
  const { state, close } = mountState(Editor, { instance, version: structuredClone(version), deployments }, (event, value) => {
    if (event === 'saved') saved++; if (event === 'recovered') recovered = value
  }); context.after(close); await flush()
  state.draft.value.parameters[0].default_value = 0.75
  const submitted = JSON.stringify(state.draft.value)
  client.updateVersion = async () => { writes++; throw Error('连接中断') }
  await state.save(); assert.match(state.authoringStatus.value, /结果待核对/)
  assert.equal(state.editingLocked.value, true); assert.equal(state.canSave.value, false)
  await state.save(); await state.retrySameAttempt(); assert.equal(writes, 1)
  await state.changedSelection('prompt_mode', 'none'); assert.equal(state.draft.value.prompt_mode, 'manual')
  client.version = async (id: string) => { reads++; assert.equal(id, version.id); return { ...version, revision: 4, configuration: JSON.parse(submitted) } }
  await state.readSaveOutcome(); assert.equal(reads, 1); assert.equal(writes, 1)
  assert.equal(state.retryAllowed.value, false); assert.equal(saved, 0)
  assert.match(state.recoveryNote.value, /不会猜测/)
  state.useRecoveryVersion(); assert.equal(recovered.revision, 4); assert.equal(saved, 0)
  assert.equal(JSON.stringify(state.draft.value), submitted)
})

test('readback of unchanged revision permits only the exact submitted payload and original revision', async context => {
  const { Editor, client } = await setup(context)
  const { state, close } = mountState(Editor, { instance, version: structuredClone(version), deployments }); context.after(close); await flush()
  state.draft.value.parameters[0].default_value = 0.75
  const patches: any[] = []
  client.updateVersion = async (_id: string, patch: any) => { patches.push(patch); throw { response: { status: 504 } } }
  await state.save(); state.draft.value.parameters[0].default_value = 99 // programmatic edit cannot mutate the frozen attempt
  client.version = async () => structuredClone(version)
  await state.readSaveOutcome(); assert.equal(state.retryAllowed.value, true)
  await state.retrySameAttempt(); assert.deepEqual(patches[0], patches[1]); assert.equal(patches[1].expected_revision, version.revision)
  assert.equal(patches[1].parameters[0].default_value, 0.75); assert.equal(state.retryAllowed.value, false)
})

test('unknown create reads exact family with pagination; no name matching or latest version adoption', async context => {
  const { Editor, client } = await setup(context)
  const events: string[] = []
  const { state, close } = mountState(Editor, { instance, version: null, deployments }, event => events.push(event)); context.after(close); await flush()
  state.draft.value = structuredClone(configuration)
  let writes = 0
  client.createVersion = async () => { writes++; throw Error('timeout') }
  await state.save()
  client.instance = async (id: string) => { assert.equal(id, instance.id); return { ...instance, revision: instance.revision + 1 } }
  client.versions = async (page: number, filter: any) => { assert.deepEqual(filter, { instance: instance.id }); return { results: [{ ...version, id: `v${page}`, version: page }], next: page === 1 ? 'next' : null } }
  await state.readSaveOutcome(); assert.equal(state.retryAllowed.value, false); assert.equal(state.recoveryVersion.value, null)
  assert.equal(state.recoveryHasMore.value, true)
  await state.moreRecoveryVersions(); assert.deepEqual(state.recoveryVersions.value.map((v: any) => v.id), ['v1', 'v2'])
  client.version = async (id: string) => ({ ...version, id, revision: 8 })
  await state.inspectRecoveryVersion(state.recoveryVersions.value[1]); assert.equal(state.recoveryVersion.value.id, 'v2')
  assert.ok(!events.includes('recovered')); assert.ok(!events.includes('saved')); assert.equal(writes, 1)
  state.useRecoveryVersion(); assert.ok(events.includes('recovered')); assert.ok(!events.includes('saved'))
})

test('recovery failure, identity mismatch and stale unmounted response retain unknown outcome without a write', async context => {
  const { Editor, client } = await setup(context)
  const { state, close } = mountState(Editor, { instance, version: structuredClone(version), deployments }); context.after(close); await flush()
  state.draft.value.parameters[0].default_value = 0.75
  client.updateVersion = async () => { throw Error('timeout') }; await state.save()
  client.version = async () => ({ ...version, id: 'unrelated' })
  await state.readSaveOutcome(); assert.match(state.recoveryError.value, /身份不匹配/); assert.equal(state.retryAllowed.value, false)
  client.version = async () => { throw { response: { status: 403, data: { message: '无权限' } } } }
  await state.readSaveOutcome(); assert.match(state.recoveryError.value, /无权限/); assert.equal(state.recoveryVersion.value, null)
  let finish: any; client.version = () => new Promise(resolve => { finish = resolve })
  const reading = state.readSaveOutcome(); close(); finish({ ...version, revision: 50 }); await reading
  assert.equal(state.recoveryVersion.value, null); assert.ok(state.unresolved.value)
})

test('409 uses readback while explicit field rejection stays editable with section locations', async context => {
  const { Editor, client } = await setup(context)
  const { state, close } = mountState(Editor, { instance, version: structuredClone(version), deployments }); context.after(close); await flush()
  state.draft.value.parameters[0].default_value = 0.75
  client.updateVersion = async () => { throw { response: { status: 400, data: { fields: { 'parameters.0.default_value': ['不在范围内'] } } } } }
  await state.save(); assert.equal(state.unresolved.value, null); assert.equal(state.editingLocked.value, false)
  assert.equal(state.issues.value[0].section, 'inputs')
  client.updateVersion = async () => { throw { response: { status: 409, data: { message: '修订已变化' } } } }
  await state.save(); assert.match(state.authoringStatus.value, /修订冲突/); assert.equal(state.canSave.value, false)
})

test('unknown create can retry only after reading the same family revision, never with a refreshed revision', async context => {
  const { Editor, client } = await setup(context)
  const { state, close } = mountState(Editor, { instance, version: null, deployments }); context.after(close); await flush()
  state.draft.value = structuredClone(configuration)
  const attempts: any[] = []
  client.createVersion = async (id: string, revision: number, config: any) => { attempts.push({ id, revision, config }); throw Error('timeout') }
  await state.save()
  client.instance = async () => structuredClone(instance)
  client.versions = async () => ({ count: 0, results: [], next: null, previous: null })
  await state.readSaveOutcome(); assert.equal(state.retryAllowed.value, true)
  await state.retrySameAttempt(); assert.equal(attempts.length, 2); assert.deepEqual(attempts[0], attempts[1])
  client.instance = async () => ({ ...instance, revision: instance.revision + 1 })
  await state.readSaveOutcome(); await state.retrySameAttempt()
  assert.equal(attempts.length, 2); assert.equal(state.retryAllowed.value, false)
})
