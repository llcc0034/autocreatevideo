import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { createServer } from 'vite'
import { createRenderer, createSSRApp, h, nextTick, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { version } from './fixtures/execution-authoring-data.ts'

const flush = async () => { for (let i = 0; i < 20; i++) { await Promise.resolve(); await nextTick() } }

test('runtime resource entry points share readonly explorer; file narrow reading is not trapped in fixed model height',async()=>{
  const source=await readFile(new URL('../src/views/SettingsView.vue',import.meta.url),'utf8')
  for(const kind of ['provider','artifact','host'])assert.match(source,new RegExp(`inspect\\('${kind}',`))
  assert.doesNotMatch(source,/showDetail\('(模型文件|运行主机|服务连接)'/)
  assert.match(source,/class="platform-card runtime-resource-card"/)
  assert.match(source,/model-registry-workbench:has\(\.model-artifact-panel :deep\(\.resource-inspector\)\)\{grid-template-rows:auto auto\}/)
  assert.match(source,/watch\(\(\)=>route.query.host,\(\)=>\{if\(tab.value==='runtime'\)void load\(\)\}\)/)
})

test('runtime resource projections show explicit links, safe fields and exact maintenance targets only', async context => {
  const {platform,readResourceDetails}=await setup(context)
  platform.listProviders=async()=>[{id:'p',name:'连接',isActive:false,credentialConfigured:false,driverType:'comfyui',driverConfig:{secret:'sensitive-marker'}}]
  platform.listModelProfiles=async()=>[{id:'old',provider:'p',profileKey:'同名',version:1},{id:'other',provider:'q',profileKey:'同名',version:2}]
  platform.listRuntimeHosts=async()=>[{id:'h',name:'主机',status:'disabled',fingerprintSha256:'sensitive-marker'}]
  platform.listDeployments=async()=>[{id:'d',runtimeHost:'h',name:'部署'},{id:'other',runtimeHost:null}]
  platform.listModelArtifacts=async()=>[{id:'f',profileVersion:'old',logicalName:'文件',status:'unverified',sizeBytes:0,storageUri:'sensitive-marker'}]
  const provider=await readResourceDetails('provider','p'),host=await readResourceDetails('host','h'),file=await readResourceDetails('artifact','f')
  assert.deepEqual(provider.links.map(r=>r.id),['old']);assert.deepEqual(host.links.map(r=>r.id),['d']);assert.deepEqual(file.links.map(r=>r.id),['old'])
  assert.equal(provider.status,'inactive');assert.equal(provider.version,undefined)
  assert.match(provider.maintenanceUrl,/provider=p/);assert.match(host.maintenanceUrl,/section=runtime&host=h/);assert.match(file.maintenanceUrl,/model_profile_version=old&artifact=f/)
  assert.equal(file.sections[0].facts[2].value,0);assert.doesNotMatch(JSON.stringify([provider,host,file]),/sensitive-marker/)
  assert.match(host.relationScope,/不是探测结果/);assert.match(provider.relationScope,/不代表所有/)
})

test('runtime resource catalog missing, duplicate identity and read failures never imply an empty usable resource', async context => {
  const {platform,readResourceDetails}=await setup(context)
  platform.listProviders=async()=>[];await assert.rejects(readResourceDetails('provider','p'),/不存在/)
  platform.listProviders=async()=>[{id:'p'},{id:'p'}];await assert.rejects(readResourceDetails('provider','p'),/重复/)
  platform.listRuntimeHosts=async()=>{throw Error('权限不足')};await assert.rejects(readResourceDetails('host','h'),/权限不足/)
  platform.listRuntimeHosts=async()=>[{id:'h'}];platform.listDeployments=async()=>{throw Error('使用方读取失败')};await assert.rejects(readResourceDetails('host','h'),/使用方读取失败/)
})

test('runtime resources reuse the same trail without unsupported relation requests and restore original focus',async context=>{
  const {Component,platform,api}=await setup(context)
  platform.listProviders=async()=>[{id:'p',name:'连接',isActive:true}]
  platform.listModelProfiles=async()=>[{id:'m',provider:'p',profileKey:'旧模型',version:1}]
  platform.getModelProfile=async id=>({id,profileKey:'旧模型',version:1,status:'published',specification:{},provider:'p'})
  let relationReads=0;api.get=async(_url,options)=>{relationReads++;assert.equal(options.params.kind,'model');return{data:page([row('model','m')])}}
  const {state}=mount(Component,context);let focused=0
  state.inspect('provider','p',{currentTarget:{isConnected:true,focus(){focused++},scrollIntoView(){}}});await flush()
  assert.equal(relationReads,0);assert.equal(state.details.value.id,'p')
  state.inspect('model','m');await flush();assert.equal(relationReads,1);assert.equal(state.details.value.id,'m')
  state.inspect('provider','p');await flush();assert.equal(state.trail.value.length,1)
  await state.returnToOwner();assert.equal(focused,1);assert.equal(state.current.value,undefined)
})

test('late runtime readers cannot replace a new resource or reopen a returned owner',async context=>{
  const {Component,platform}=await setup(context)
  const pending:any[]=[];platform.listProviders=()=>new Promise(resolve=>pending.push(resolve));platform.listModelProfiles=async()=>[]
  platform.listRuntimeHosts=async()=>[{id:'h',name:'主机'}];platform.listDeployments=async()=>[]
  const {state}=mount(Component,context)
  state.inspect('provider','p');state.inspect('host','h');await flush()
  pending[0]([{id:'p'}]);await flush();assert.equal(state.details.value.id,'h')
  state.inspect('provider','p');await state.returnToOwner();pending[1]([{id:'p'}]);await flush();assert.equal(state.details.value,null)
})
const row = (kind = 'execution', id = 'v1') => ({ kind, id, label: '同名对象', status: 'published', config_url: `/settings?section=models&execution_version=${id}`, overview_url: `/settings/acceptance-smoke?view=relations&kind=${kind}&ids=${id}`, acceptance_url: '', groups: [], capabilities: [], notices: [] })
const page = (rows: any[] = [row()], extra = {}) => ({ count: rows.length, results: rows, next: null, previous: null, generated_at: '2026-10-09T02:15:00Z', context_label: '', ...extra })
async function setup(context: any) {
  const vite = await createServer({ logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true } }); context.after(() => vite.close())
  const { default: Component } = await vite.ssrLoadModule('/src/components/RegistryResourceExplorer.vue')
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  const { executionRegistryApi: executions } = await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  const { platformApi: platform } = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const { readResourceDetails } = await vite.ssrLoadModule('/src/services/registryResourceDetails.ts')
  for (const method of ['get', 'post', 'patch', 'put', 'delete', 'request']) api[method] = () => assert.fail(`unexpected network ${method}`)
  return { vite, Component, api, executions, platform, readResourceDetails }
}
function mount(Component: any, context: any) {
  let state: any
  const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
  const app = renderer.createApp({ setup() { state = Component.setup({ label: '原表单' }, { expose() {} }); return () => null } }); app.provide(ssrContextKey, {}); app.mount({}); context.after(() => app.unmount())
  return { state, close: () => app.unmount() }
}

test('resource actions use common author typography even when the legacy small-button class is present', async () => {
  const source=await readFile(new URL('../src/components/RegistryResourceExplorer.vue',import.meta.url),'utf8')
  assert.match(source,/\.resource-inspector \.button\{font:500 var\(--config-body-size\)\/1\.5 var\(--font\);[^}]*min-height:var\(--config-control-height\)/)
  assert.match(source,/\.resource-inspector summary\{[^}]*min-height:var\(--config-control-height\)/)
  assert.match(source,/@media\(max-width:640px\)\{\.resource-inspector\{--config-control-height:44px\}\}/)
})

test('resource reads remain lazy, exact and readonly; returning restores the original trigger without changing references', async context => {
  const { Component, api, executions } = await setup(context)
  const reads: any[] = []; api.get = async (_url: string, options: any) => { reads.push(options.params); return { data: page([row('execution', options.params.ids)]) } }
  executions.version = async (id: string) => ({ ...structuredClone(version), id })
  const { state } = mount(Component, context); assert.equal(reads.length, 0)
  const focus: any[] = [], opener = { isConnected: true, focus: (options: any) => focus.push(options), scrollIntoView: (options: any) => focus.push(options) }
  state.inspect('execution', 'exact-old', { currentTarget: opener }); await flush()
  assert.equal(state.details.value.id, 'exact-old'); assert.equal(state.exactRow.value.id, 'exact-old')
  assert.equal(reads[0].ids, 'exact-old'); assert.equal(reads[0].page_size, 25)
  state.inspect('execution', 'other-old'); await flush(); assert.equal(state.trail.value.length, 2)
  state.ancestor(0); await flush(); assert.equal(state.details.value.id, 'exact-old')
  await state.returnToOwner(); assert.equal(state.current.value, undefined)
  assert.deepEqual(focus, [{ preventScroll: true }, { block: 'nearest' }])
})

test('owner slot stays mounted while hidden; single detail region has no nested modal or writer and maintenance preserves context', async context => {
  const { Component } = await setup(context)
  const Wrapper = { ...Component, setup(props: any, ctx: any) {
    const state = Component.setup(props, ctx)
    state.trail.value = [{ url: row().overview_url, label: '原精确对象' }]
    state.page.value = page(); state.details.value = { id: 'v1', name: '同名对象', version: 1, status: 'published', sections: [{ title: '输入约束', facts: [{ label: '可选', value: false }, { label: '数量', value: 0 }] }] }
    return state
  } }
  const html = await renderToString(createSSRApp({ render: () => h(Wrapper, { label: '原表单' }, { default: () => h('input', { value: '未保存 原值 🙂' }) }) }))
  assert.match(html, /class="resource-owner" style="display:none;"/); assert.match(html, /value="未保存 原值 🙂"/)
  assert.equal((html.match(/aria-label="公共资源只读详情"/g) || []).length, 1)
  assert.doesNotMatch(html, /role="dialog"|type="submit"/)
  assert.match(html, /target="_blank" rel="noopener"/)
  assert.match(html, /section=executions&amp;execution_version=v1&amp;relation_context=execution%3Av1/)
  assert.match(html, /否（false）/); assert.match(html, />0<\/pre>/)
})

test('stale detail/relations, wrong identity, failures and unmount never replace current selection', async context => {
  const { Component, api, executions } = await setup(context)
  const pending: any[] = [], reads: any[] = []
  executions.version = (id: string) => new Promise((resolve, reject) => pending.push({ id, resolve, reject }))
  api.get = (_url: string, options: any) => new Promise(resolve => reads.push({ options, resolve }))
  const { state, close } = mount(Component, context)
  state.inspect('execution', 'a'); state.inspect('execution', 'b'); assert.equal(reads[0].options.signal.aborted, true)
  pending[1].resolve({ ...version, id: 'b' }); reads[1].resolve({ data: page([row('execution', 'b')]) }); await flush()
  pending[0].resolve({ ...version, id: 'a' }); reads[0].resolve({ data: page([row('execution', 'a')]) }); await flush()
  assert.equal(state.details.value.id, 'b'); assert.equal(state.page.value.results[0].id, 'b')
  const wrong = state.load(); pending[2].resolve({ ...version, id: 'same-name-new-version' }); reads[2].resolve({ data: page([row('model', 'b')]) }); await wrong
  assert.match(state.detailError.value, /身份不匹配/); assert.match(state.error.value, /其他对象/); assert.equal(state.details.value, null)
  const fail = state.load(); pending[3].reject({ response: { status: 403, data: { message: '无权查看' } } }); reads[3].resolve({ data: page([]) }); await fail
  assert.match(state.detailError.value, /无权查看/); assert.equal(state.exactRow.value, null)
  const last = state.load(); close(); pending[4].resolve({ ...version, id: 'b' }); reads[4].resolve({ data: page([row('execution', 'b')]) }); await last
  assert.equal(state.details.value, null)
})

test('relation drilldown and bounded pagination retain owner context; cycles do not duplicate and unsafe urls are refused', async context => {
  const { Component, api, executions } = await setup(context)
  const reads: any[] = []
  api.get = async (_url: string, options: any) => { reads.push(options.params); return { data: page([row()], { count: 20, next: 'next' }) } }
  executions.version = async id => ({ ...version, id })
  const { state } = mount(Component, context)
  state.inspect('execution', 'v1'); await flush()
  state.navigate('/settings/acceptance-smoke?view=relations&kind=execution&source_kind=prompt&source_id=old&relation=executions', '使用方'); await flush()
  assert.equal(state.details.value, null); state.changePage(1); await flush()
  assert.equal(reads.at(-1).page, '2'); assert.equal(reads.at(-1).source_id, 'old'); assert.equal(state.trail.value.length, 2)
  state.inspect('execution', 'v1'); await flush(); assert.equal(state.trail.value.length, 1)
  const count = reads.length; state.navigate('https://external.invalid/', 'wrong'); assert.equal(reads.length, count); assert.match(state.error.value, /地址无效/)
})

test('semantic projections preserve values, do not expose deployment runtime secrets and do not promote published to runnable', async context => {
  const { platform, readResourceDetails } = await setup(context)
  platform.getDeployment = async id => ({ id, name: '部署', status: 'inactive', priority: 0, isDefault: false, credentialConfigured: false, profileVersion: 'model-old', runtimeConfig: { secret: 'must-not-render' } })
  const detail = await readResourceDetails('deployment', 'deployment-old')
  assert.equal(detail.status, 'inactive'); assert.equal(detail.version, undefined)
  assert.match(JSON.stringify(detail), /model-old/); assert.doesNotMatch(JSON.stringify(detail), /must-not-render|runtimeConfig/)
  assert.equal(detail.sections[0].facts[1].value, 0); assert.equal(detail.sections[0].facts[2].value, false)
  platform.getWorkflowVersion = async id => ({ id, templateName: '工作流', version: 3, status: 'published', inputContract: { slots: [{ key: '精确中文🙂', min: 0 }] }, validationState: 'failed', smokeTestState: 'unknown' })
  const workflow = await readResourceDetails('workflow', 'workflow-old')
  assert.match(JSON.stringify(workflow), /精确中文🙂/); assert.match(JSON.stringify(workflow), /failed/); assert.match(JSON.stringify(workflow), /unknown/)
  platform.getRecipe = async id => ({ id, name: '独立配方', version: 4, status: 'draft', definition: { schema_version: 3, interaction_mode: 'assisted', stages: [] } })
  const recipe = await readResourceDetails('recipe', 'recipe-old')
  assert.equal(recipe.id, 'recipe-old'); assert.equal(recipe.version, 4); assert.equal(recipe.sections.length, 1)
})

test('recipe semantics reuse saved step order and declared consumers without resolving models or replacing versions', async context => {
  const { platform, readResourceDetails } = await setup(context)
  const definition = { schema_version: 3, interaction_mode: 'assisted', stages: [
    { key: 'create', kind: 'script_generate', mode: 'ai', approval: 'auto', routing: { routes: [{ execution_instance_version_id: 'exact-old', input_sources: { prompt: { kind: 'literal', value: '' } }, parameter_policies: { seed: 0, enabled: false } }] } },
    { key: 'review', kind: 'script_review', mode: 'review', approval: 'required' },
    { key: 'next', kind: 'storyboard', mode: 'ai', approval: 'auto', routing: { routes: [{ input_sources: { script: { kind: 'selected_output', stage_key: 'create', output_key: 'script' } } }] } },
  ] }
  const original = JSON.stringify(definition)
  platform.getRecipe = async id => ({ id, name: '同名配方', version: 2, status: 'published', definition })
  const recipe = await readResourceDetails('recipe', 'old')
  assert.equal(recipe.sections.length, 4)
  assert.equal(recipe.sections[1].facts[2].value, definition.stages[0])
  assert.equal(recipe.sections[1].facts[3].value[0]['目标位置'], 'script')
  assert.equal(recipe.sections[1].facts[3].value[0]['引用顺序正确'], true)
  assert.match(JSON.stringify(recipe), /exact-old/); assert.equal(JSON.stringify(definition), original)
  for (const stages of [undefined, null, [null], ['invalid']]) {
    platform.getRecipe = async id => ({ id, definition: { stages } })
    await assert.rejects(() => readResourceDetails('recipe', 'old'), /步骤声明缺失或格式异常/)
  }
  platform.getRecipe = async () => ({ id: 'new-same-name', definition })
  await assert.rejects(() => readResourceDetails('recipe', 'old'), /身份不匹配/)
})

test('prompt details follow exact source/field/channel rules and reject mismatched or incomplete rule sets', async context => {
  const { platform, readResourceDetails } = await setup(context)
  platform.getPromptProfileVersion = async id => ({ id, name: '策略', version: 2, status: 'published', fieldRuleCount: 1, inputStructureVersion: 'structure-old' })
  const rule = { id: 'rule', profileVersion: 'prompt-old', fieldDefinitionVersion: 'field-old', fieldKey: 'custom', fieldLabel: '名称🙂', position: 0, channel: 'positive_prompt', generationMode: 'passthrough', hasDefault: true, defaultValue: '', required: false,
    sourceSelector: 'fixed.value', fixedValue: false, generationSourceSelector: 'source.text', generationContextSelectors: ['project.title'], generationInstruction: '保留原名🙂', generationMin: 0, generationMax: null, separator: '\n', omitIfEmpty: false }
  platform.listPromptProfileFieldRules = async id => { assert.equal(id, 'prompt-old'); return [{ ...rule }] }
  const prompt = await readResourceDetails('prompt', 'prompt-old')
  assert.match(JSON.stringify(prompt), /field-old/); assert.match(JSON.stringify(prompt), /positive_prompt/)
  assert.equal(prompt.sections[1].facts[0].value['默认值'], ''); assert.equal(prompt.sections[1].facts[0].value['必需'], false)
  const ruleValue = prompt.sections[1].facts[0].value
  assert.equal(ruleValue['固定值'], false); assert.equal(ruleValue['取值来源'], 'fixed.value')
  assert.equal(ruleValue['通道组装']['分隔符'], '\n'); assert.equal(ruleValue['通道组装']['空内容省略'], false)
  assert.deepEqual(ruleValue['生成／翻译配置（按处理方式生效）']['上下文来源'], ['project.title'])
  assert.equal(ruleValue['生成／翻译配置（按处理方式生效）']['生成来源'], 'source.text')
  assert.equal(ruleValue['生成／翻译配置（按处理方式生效）']['生成指令'], '保留原名🙂')
  assert.equal(ruleValue['生成／翻译配置（按处理方式生效）']['最小长度'], 0)
  assert.equal(ruleValue['生成／翻译配置（按处理方式生效）']['最大长度'], null)
  for (const fixedValue of ['', 0, null]) {
    platform.listPromptProfileFieldRules = async () => [{ ...rule, fixedValue }]
    assert.equal((await readResourceDetails('prompt', 'prompt-old')).sections[1].facts[0].value['固定值'], fixedValue)
  }
  platform.listPromptProfileFieldRules = async () => [{ ...rule, profileVersion: 'other' }]
  await assert.rejects(readResourceDetails('prompt', 'prompt-old'), /其他策略/)
  platform.listPromptProfileFieldRules = async () => []
  await assert.rejects(readResourceDetails('prompt', 'prompt-old'), /数量或身份/)
})

test('definitions, structure, output and projector use existing exact readers without deriving task values', async context => {
  const { vite, platform, readResourceDetails } = await setup(context)
  const { dataStructures } = await vite.ssrLoadModule('/src/services/dataStructures.ts')
  const { fieldDefinitionVersions } = await vite.ssrLoadModule('/src/services/fieldDefinitionVersions.ts')
  const { businessOutputs } = await vite.ssrLoadModule('/src/services/businessOutputs.ts')
  platform.getModelProfile = async id => ({ id, profileKey: '公共模型', version: 1, status: 'published', specification: { parameters: { custom: false } } })
  platform.getWorkflowProjectorVersion = async id => ({ id, projectorKey: '公共投影', version: 3, status: 'draft', engineVersion: 0, definition: { custom: '' } })
  businessOutputs.detail = async id => ({ id, name: '输出', version: 2, status: 'published', parser_key:'structured_json.v1', data_structure_version: 'old-structure', parser_contract: { key: 'structured_json.v1',format:'json' },quality_advisories:{} })
  fieldDefinitionVersions.get = async id => ({ id, label: '字段🙂', version: 4, status: 'retired', unit: '', value_options: [false, 0, null] })
  dataStructures.get = async id => ({ id, name: '结构', version: 1, status: 'draft', nodes: [{ node_key: 'k1', property_name: '中文🙂', parent_key: null, field_definition_version: 'old-field', required: false, allow_null: true, allow_blank: false, allow_empty: true, has_default: true, default_value: 0, constraints: {} }] })
  for (const kind of ['model', 'projector', 'output', 'field_version', 'structure']) {
    const value = await readResourceDetails(kind, `${kind}-old`)
    assert.equal(value.id, `${kind}-old`); assert.ok(value.sections.length)
  }
  const structure = await readResourceDetails('structure', 's1')
  assert.equal(structure.sections[1].facts[0].value['默认值'], 0)
  assert.equal(structure.sections[1].facts[0].value['字段版本'], 'old-field')
  assert.equal(structure.sections[1].facts[0].value['允许空值'], true)
  assert.equal(structure.sections[1].facts[0].value['允许空文本'], false)
  assert.equal(structure.sections[1].facts[0].value['允许空集合'], true)
  dataStructures.get = async () => ({ id: 's1' }); await assert.rejects(readResourceDetails('structure', 's1'), /节点未返回/)
  fieldDefinitionVersions.get = async () => ({ id: 'latest' }); await assert.rejects(readResourceDetails('field_version', 'old'), /身份不匹配/)
})
