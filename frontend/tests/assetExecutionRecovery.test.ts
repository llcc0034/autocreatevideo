import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { readFileSync } from 'node:fs'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'
import { unusedLanguageForm } from './fixtures/executionLanguage.ts'
import { generationOptions, generationContract, auxiliaryIds, profileId } from './fixtures/assetGenerationScenarios.ts'

const root = fileURLToPath(new URL('..', import.meta.url))

test('asset recovery and source-boundary controls remain explicit and shrink on narrow screens', () => {
  const source = readFileSync(new URL('../src/components/AssetExecutionCreateForm.vue', import.meta.url), 'utf8')
  assert.match(source, /重试读取所选实例输入合同/)
  assert.match(source, /此独立素材入口不提供生产审核上下文/)
  assert.match(source, /href="\/projects"/)
  assert.match(source, /asset-execution-form\{display:grid;grid-template-columns:minmax\(0,1fr\)/)
  assert.match(source, /asset-execution-form select\{min-width:0;max-width:100%\}/)
})
const option = { versionId: 'version', name: '图片实例', version: 1, operation: 'image.text_to_image', promptMode: 'manual', definitionSha256: 'a'.repeat(64) }
const rawContract = () => ({
  appearance_inputs: { primary_inputs: [], difference_sources: [{ kind: 'manual_channel', key: 'positive_prompt' }] },
  version_id: 'version', revision: 1, frozen: true, sha256: option.definitionSha256, context_fields: [], input_forms: {}, subject_categories: [{value:'character',label:'人物'},{value:'scene',label:'场景'},{value:'prop',label:'道具'}], language_form: unusedLanguageForm(),
  definition: {
    version_id: 'version',
    configuration: { deployment_id: 'deployment', operation: option.operation, mode: '', workflow_version_id: 'workflow', prompt_mode: 'manual', prompt_profile_version_id: null, inputs: [], parameters: [], auxiliaries: [] },
    interface: { output: { contract: { primary: { kind: 'image' } } }, input_purposes: {}, parameters: {}, prompt: { mode: 'manual', channels: { positive_prompt: { rule: { type: 'string', required: true } } } } },
  },
})

async function setup(context: any, scenario?: any) {
  const previousStorage = globalThis.localStorage
  const previousWindow = globalThis.window
  globalThis.localStorage = { getItem: () => null } as any
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  let app: any
  context.after(async () => { app?.unmount(); await vite.close(); globalThis.localStorage = previousStorage; globalThis.window = previousWindow })
  const { default: Form } = await vite.ssrLoadModule('/src/components/AssetExecutionCreateForm.vue')
  const { repository } = await vite.ssrLoadModule('/src/services/repository.ts')
  const { executionRegistryApi: registry } = await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  repository.getAssetGenerationOptions = async () => ({ count: 1, results: [scenario || option], previous: null, next: null })
  repository.getAssetGenerationJobs = async () => []
  registry.contract = async () => scenario ? generationContract(scenario) : rawContract()
  let state: any
  const events: any[][] = []
  const props = reactive({ mediaType: scenario?.kind || 'image', promptCandidate: '保留候选原文 中文 🌿', metadata: { name: '原名称', description: '原基础描述', styleId: '', imageCategory: 'reference' } })
  const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
  app = renderer.createApp({ setup() { state = Form.setup(props, { emit: (...args: any[]) => events.push(args), expose() {} }); return () => h('div') } })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); await nextTick(); await nextTick()
  return { state, registry, repository, props, events, unmount: () => app.unmount() }
}

async function ready(state: any) {
  await state.choose('version')
  state.draft.value.channels.positive_prompt = '待确认请求的原始正文'
  await nextTick(); state.reviewed.value = true
}

test('missing appearance metadata fails the image contract and a retry loads exact metadata without changing draft', async context => {
  const { state, registry } = await setup(context)
  registry.contract = async () => { const value: any = rawContract(); delete value.appearance_inputs; return value }
  await state.choose('version')
  assert.equal(state.contract.value, null); assert.equal(state.draft.value, null); assert.equal(state.appearanceInputs.value, null)
  assert.match(state.error.value, /外观输入目录不完整/)
  registry.contract = async () => rawContract()
  await state.retryContract()
  assert.deepEqual(state.appearanceInputs.value, rawContract().appearance_inputs)
  assert.equal(state.dirty.value, false); assert.equal(state.appearanceTarget.value, null)
  await state.choose('')
  assert.equal(state.appearanceInputs.value, null)
})

test('primary declaration is part of review and a detached unknown request, including exact setting identity', async context => {
  const { state, repository } = await setup(context)
  await ready(state)
  const target = { role: 'primary', setting_revision_id: '10000000-0000-0000-0000-000000000001', primary_version_id: null, primary_input: null }
  state.appearanceTarget.value = target
  await nextTick(); assert.equal(state.reviewed.value, false); assert.equal(state.dirty.value, true)
  state.reviewed.value = true
  const calls: any[] = []
  repository.createAssetGenerationJob = async payload => { calls.push(JSON.parse(JSON.stringify(payload))); throw Error('响应丢失') }
  await state.submit()
  assert.deepEqual(calls[0].appearanceTarget, target)
  assert.notEqual(state.pending.value.appearanceTarget, state.appearanceTarget.value)
  state.appearanceTarget.value.setting_revision_id = null
  await state.submit()
  assert.equal(calls.length, 1, 'unknown requests are read, not posted again')
  assert.deepEqual(JSON.parse(JSON.stringify(state.pending.value)), calls[0])
})

test('variant nested identity is detached in unknown submission and original key is preserved on retry', async context => {
  const { state, repository } = await setup(context)
  await ready(state)
  state.appearanceInputs.value.primary_inputs = [{input_key:'image',target_key:'slot'}]
  state.contract.value.inputs = [{key:'image',kind:'image',required:true,min_items:1,max_items:1,
    semantic_contract:{schema_version:1,allowed_purposes:[],allowed_subject_kinds:['external'],allowed_scope_kinds:['request'],paired_with_input_key:null,allow_repeated_asset:true},targets:[]}]
  state.primaryAssetId.value = 'asset'
  state.draft.value.media.image = [{itemKey:'one',assetId:'asset',purpose:null,pairedTo:null,subjectId:null}]
  state.appearanceTarget.value = {role:'variant',setting_revision_id:null,primary_version_id:'10000000-0000-0000-0000-000000000008',
    primary_input:{input_key:'image',item_key:'one'},difference_input:{kind:'manual_channel',key:'positive_prompt'}}
  await nextTick(); state.reviewed.value = true
  const calls: any[] = []
  repository.createAssetGenerationJob = async payload => { calls.push(JSON.parse(JSON.stringify(payload))); throw Error('响应丢失') }
  await state.submit(); assert.equal(calls.length,1,state.error.value)
  state.appearanceTarget.value.primary_input.item_key = 'changed'
  state.appearanceTarget.value.difference_input.key = 'changed'
  await state.submit(); assert.equal(calls.length, 1); assert.deepEqual(JSON.parse(JSON.stringify(state.pending.value)), calls[0])
})

test('switching instance or job purpose clears the appearance declaration only after confirmation', async context => {
  const { state } = await setup(context)
  await ready(state)
  state.appearanceTarget.value = { role: 'primary', setting_revision_id: null, primary_version_id: null, primary_input: null }
  await state.choose(''); assert.ok(state.appearanceTarget.value)
  assert.equal(state.inputChange.value.kind, 'instance'); state.inputChange.value = null
  await state.changeJobType('prompt'); assert.equal(state.jobType.value, 'asset'); assert.ok(state.appearanceTarget.value)
  await state.confirmChange(); assert.equal(state.jobType.value, 'prompt'); assert.equal(state.appearanceTarget.value, null)
  await state.choose('version')
  state.appearanceTarget.value = { role: 'primary', setting_revision_id: null, primary_version_id: null, primary_input: null }
  await state.choose(''); await state.confirmChange(); assert.equal(state.appearanceTarget.value, null)
})

test('failed readback cannot clear an unknown request or post it again; exact receipt resolves it', async context => {
  const { state, repository, events } = await setup(context)
  await ready(state)
  const calls: any[] = []
  repository.createAssetGenerationJob = async payload => {
    calls.push(JSON.parse(JSON.stringify(payload)))
    throw Error('响应丢失')
  }
  await state.submit()
  const originalCommand = state.pending.value
  const original = JSON.parse(JSON.stringify(state.pending.value))
  for (const status of [400, 401, 403, 404, 409, 422, 408, 429, 500]) {
    repository.getAssetGenerationJobs = async () => { throw { isAxiosError: true, response: { status, data: { detail: `拒绝 ${status}` } } } }
    await state.submit()
    assert.equal(state.pending.value, originalCommand, `retry ${status} must retain the original command`)
    assert.deepEqual(JSON.parse(JSON.stringify(state.pending.value)), original)
    assert.equal(state.locked.value, true)
    assert.equal(events.some(event => event[0] === 'created'), false)
  }
  repository.getAssetGenerationJobs = async () => [{ id: 'job', preparation: { requestKey: original.idempotencyKey } }]
  repository.getAssetGenerationJob = async () => ({ id: 'job', executionInstanceVersionId: 'version', jobType: 'asset', mediaType: 'image', preparation: { requestKey: original.idempotencyKey } })
  await state.submit()
  assert.equal(state.pending.value, null)
  assert.equal(events.filter(event => event[0] === 'created').length, 1)
  assert.equal(calls.length, 1); assert.deepEqual(calls[0], original)
})

test('first definitive rejection permits correction but conflict, timeout and throttling retain the request', async context => {
  const { state, repository } = await setup(context)
  await ready(state)
  const statuses = [400, 401, 403, 404, 422, 409, 408, 429, 500]
  for (const status of statuses) {
    state.pending.value = null; state.reviewed.value = true
    repository.createAssetGenerationJob = async () => { throw { isAxiosError: true, response: { status } } }
    await state.submit()
    const definitive = [400, 401, 403, 404, 422].includes(status)
    assert.equal(!!state.pending.value, !definitive, `first ${status}`)
    assert.equal(state.reviewed.value, !definitive)
  }
})

for (const outcome of ['success', 'failure']) {
  test(`unmounted asset creation ignores late ${outcome} and never submits twice concurrently`, async context => {
    const { state, repository, events, unmount } = await setup(context)
    await ready(state)
    let finish: (value: any) => void, fail: (reason: any) => void, calls = 0
    repository.createAssetGenerationJob = () => { calls++; return new Promise((resolve, reject) => { finish = resolve; fail = reject }) }
    const running = state.submit()
    await nextTick(); await state.submit()
    assert.equal(calls, 1)
    unmount()
    const previous = JSON.stringify({ pending: state.pending.value, error: state.error.value, submitting: state.submitting.value, events })
    if (outcome === 'success') finish!({ executionInstanceVersionId: 'version', jobType: 'asset' })
    else fail!({ isAxiosError: true, response: { status: 400 } })
    await running; await nextTick()
    assert.equal(JSON.stringify({ pending: state.pending.value, error: state.error.value, submitting: state.submitting.value, events }), previous)
  })
}

test('inline instance-change confirmation restores the displayed selection and preserves original inputs', async context => {
  const { state, registry } = await setup(context)
  await ready(state)
  let reads = 0
  registry.contract = async () => { reads++; return rawContract() }
  const select = { value: '' }
  await state.choose(select.value, { target: select } as any)
  assert.equal(state.inputChange.value.kind, 'instance'); assert.equal(state.locked.value, true)
  state.inputChange.value = null
  assert.equal(select.value, 'version')
  assert.equal(state.selectedId.value, 'version')
  assert.equal(state.draft.value.channels.positive_prompt, '待确认请求的原始正文')
  assert.equal(state.reviewed.value, true)
  assert.equal(reads, 0)
})

test('candidate replacement is explicit, keeps the exact text and affects only the chosen channel', async context => {
  const { state, props, repository } = await setup(context)
  await ready(state)
  state.candidateChannel.value = 'positive_prompt'; state.allowExternal.value = true
  const original = JSON.parse(JSON.stringify(state.draft.value))
  let writes = 0; repository.createAssetGenerationJob = async () => { writes++; throw Error('unexpected') }
  state.applyCandidate()
  assert.deepEqual(JSON.parse(JSON.stringify(state.draft.value)), original)
  assert.equal(state.inputChange.value.kind, 'candidate')
  await state.submit(); assert.equal(writes, 0)
  state.inputChange.value = null; assert.deepEqual(JSON.parse(JSON.stringify(state.draft.value)), original)
  state.applyCandidate(); await state.confirmChange(); await nextTick()
  assert.equal(state.draft.value.channels.positive_prompt, props.promptCandidate)
  assert.equal(state.allowExternal.value, true); assert.equal(state.reviewed.value, false)
  assert.equal(state.pending.value, null); assert.equal(writes, 0)
})

test('candidate is never injected into profile, none, undeclared channels or a locked request', async context => {
  const { state } = await setup(context)
  await ready(state); state.candidateChannel.value = 'positive_prompt'
  const original = JSON.stringify(state.draft.value)
  for (const mode of ['profile', 'none']) {
    state.contract.value.promptMode = mode; state.applyCandidate(); assert.equal(state.inputChange.value, null)
  }
  state.contract.value.promptMode = 'manual'; state.candidateChannel.value = 'unknown'; state.applyCandidate()
  state.candidateChannel.value = 'positive_prompt'; state.pending.value = { idempotencyKey: 'original' }; state.applyCandidate()
  assert.equal(state.inputChange.value, null); assert.equal(JSON.stringify(state.draft.value), original)
})

test('unknown catalog identity cannot erase an existing draft or be adopted by confirmation', async context => {
  const { state } = await setup(context)
  await ready(state)
  const original = JSON.stringify(state.draft.value)
  await state.choose('not-in-catalog')
  assert.match(state.error.value, /不在当前目录/); assert.equal(state.inputChange.value, null)
  assert.equal(state.selectedId.value, 'version'); assert.equal(JSON.stringify(state.draft.value), original)
})

test('generation author shows semantic sections, exact resource drilldown and no native confirmation', () => {
  const source = readFileSync(new URL('../src/components/AssetExecutionCreateForm.vue', import.meta.url), 'utf8')
  for (const text of ['选择生成能力', '本次输入与处理', '输出与创建任务', '不是本地模型的暂停开关', '返回素材生成表单']) {
    if (text === '返回素材生成表单') assert.match(source, /RegistryResourceExplorer label="素材生成表单"/)
    else assert.ok(source.includes(text), text)
  }
  assert.match(source, /inspect\('execution',selected.versionId,\$event\)/)
  assert.match(source, /mediaKindLabel\(contract.outputKind\)/)
  assert.doesNotMatch(source, /window.confirm/)
  assert.match(source, /font-size:var\(--config-body-size\)/)
})

test('failed exact contract can be retried without changing instance or asset metadata', async context => {
  const { state, registry, props } = await setup(context)
  let calls = 0
  registry.contract = async id => { calls++; assert.equal(id, option.versionId); if (calls === 1) throw Error('合同读取失败'); return rawContract() }
  await state.choose('version')
  assert.match(state.error.value, /合同读取失败/); assert.equal(state.contract.value, null)
  await state.loadOptions()
  assert.equal(calls, 1, 'catalog refresh is not a contract retry')
  await state.retryContract()
  assert.equal(calls, 2); assert.equal(state.contract.value.versionId, 'version'); assert.equal(state.error.value, '')
  assert.deepEqual(props.metadata, { name: '原名称', description: '原基础描述', styleId: '', imageCategory: 'reference' })
  state.draft.value.channels.positive_prompt = '人工正文保留'
  await state.retryContract()
  assert.equal(calls, 2, 'do not reload an existing editable draft')
  assert.equal(state.draft.value.channels.positive_prompt, '人工正文保留')
})

test('contract retries retain exact identity, serialize reads and reject late unmounted responses', async context => {
  const { state, registry, unmount } = await setup(context)
  let calls = 0, finish: (value: any) => void
  registry.contract = async () => { calls++; return { ...rawContract(), sha256: 'b'.repeat(64) } }
  await state.choose('version')
  assert.match(state.error.value, /合同与选定实例目录不一致/); assert.equal(state.draft.value, null)
  registry.contract = () => { calls++; return new Promise(resolve => { finish = resolve }) }
  const reading = state.retryContract(); await nextTick()
  assert.equal(state.contractLoading.value, true)
  await state.retryContract(); assert.equal(calls, 2)
  unmount(); finish!(rawContract()); await reading
  assert.equal(state.contract.value, null); assert.equal(state.draft.value, null)
})

test('pending or upload-locked requests never reread their contract', async context => {
  const { state, registry } = await setup(context)
  registry.contract = async () => { throw Error('一次失败') }
  await state.choose('version')
  let calls = 0
  registry.contract = async () => { calls++; return rawContract() }
  for (const key of ['uploading', 'submitting', 'pending']) {
    state[key].value = key === 'pending' ? { idempotencyKey: 'original-request' } : true
    await state.retryContract(); assert.equal(calls, 0, key)
    state[key].value = key === 'pending' ? null : false
  }
  await state.retryContract(); assert.equal(calls, 1)
})

test('standalone asset review-source gate is explicit and ignores disabled optional inputs', async context => {
  const { state, repository } = await setup(context)
  await state.choose('version')
  const contract = state.contract.value
  contract.contextFields = [{ selector_key: 'project.reviewed_continuity', label: '已审核连续性', revision_kind: 'visual_rules_approval' }]
  contract.inputForms = { review: { label: '本轮规则' } }
  state.draft.value.contexts.review = { selectorKey: 'project.reviewed_continuity', projectId: 'p', revisionId: 'approval' }
  state.draft.value.literals.review = { enabled: true, value: '' }
  state.draft.value.channels.positive_prompt = '明确的正文'
  let calls = 0
  repository.createAssetGenerationJob = async () => { calls++; throw Error('should not submit') }
  assert.deepEqual(state.productionSourceInputs.value, ['本轮规则'])
  state.reviewed.value = true
  await state.submit(); assert.equal(calls, 0); assert.equal(state.pending.value, null)
  state.draft.value.literals.review.enabled = false
  assert.deepEqual(state.productionSourceInputs.value, [])
  state.draft.value.literals.review.enabled = true
  contract.contextFields[0].revision_kind = 'script_revision'
  assert.deepEqual(state.productionSourceInputs.value, [], 'do not block supported standalone script context')
})

test('recovering the input contract never changes an unknown submission; absent or mismatched receipts retain it', async context => {
  const { state, registry, repository } = await setup(context)
  registry.contract = async () => { throw Error('暂时不可读') }
  await state.choose('version')
  let contractReads = 0
  registry.contract = async () => { contractReads++; return rawContract() }
  await state.retryContract()
  state.draft.value.channels.positive_prompt = '明确保留的人工提示词'
  await nextTick(); state.reviewed.value = true
  const calls: any[] = []
  repository.createAssetGenerationJob = async payload => {
    calls.push(JSON.parse(JSON.stringify(payload)))
    throw Error('响应丢失')
  }
  await state.submit()
  assert.equal(calls.length, 1); assert.ok(state.pending.value?.idempotencyKey)
  await state.retryContract(); assert.equal(contractReads, 1)
  await state.submit()
  assert.equal(calls.length, 1); assert.match(state.recoveryMessage.value, /尚未查到/)
  repository.getAssetGenerationJobs = async () => [{id:'job',preparation:{requestKey:calls[0].idempotencyKey}}]
  repository.getAssetGenerationJob = async () => ({id:'other-job',executionInstanceVersionId:'version',jobType:'asset',mediaType:'image',preparation:{requestKey:calls[0].idempotencyKey}})
  await state.readUnknown(); assert.match(state.error.value,/身份不一致/)
  assert.deepEqual(JSON.parse(JSON.stringify(state.pending.value)),calls[0]); assert.equal(calls.length,1)
})

test('same-instance success for another request or media kind never clears original pending state', async context => {
  const { state, repository, events } = await setup(context); await ready(state)
  repository.createAssetGenerationJob=async payload=>({id:'job',executionInstanceVersionId:'version',jobType:'asset',mediaType:'image',preparation:{requestKey:'different'}})
  await state.submit(); assert.ok(state.pending.value); assert.equal(events.filter(e=>e[0]==='created').length,0)
  const request=state.pending.value
  repository.getAssetGenerationJobs=async()=>[{id:'job',preparation:{requestKey:request.idempotencyKey}}]
  repository.getAssetGenerationJob=async()=>({id:'job',executionInstanceVersionId:'version',jobType:'asset',mediaType:'video',preparation:{requestKey:request.idempotencyKey}})
  await state.readUnknown(); assert.equal(state.pending.value,request); assert.equal(events.filter(e=>e[0]==='created').length,0)
})

for (const [kind, mode] of [['video','profile'], ['script','profile'], ['audio','none']]) {
  test(`generation branch ${kind}/${mode} keeps original inputs and submits no assembled or auxiliary values`, async context => {
    const row = generationOptions.find(row => row.kind === kind && row.promptMode === mode)
    const { state, repository, events } = await setup(context, row)
    await state.choose(row.versionId)
    const text = '林岚与小橙 🟢 原名不翻译；按本轮要求创作。'
    if (mode === 'profile') state.draft.value.literals.creative_brief.value = text
    state.draft.value.parameters.seed = 0
    if (kind === 'video') { state.draft.value.sourceLanguage = 'zh-Hans'; state.draft.value.targetLanguage = 'en' }
    state.candidateChannel.value = 'positive_prompt'; state.applyCandidate()
    assert.deepEqual(state.draft.value.channels, {})
    assert.equal(state.configuration.value.prompt_profile_version_id, mode === 'profile' ? profileId : null)
    assert.deepEqual(state.configuration.value.auxiliaries.map(row=>row.target_version_id), kind === 'video' ? auxiliaryIds : [])
    await nextTick(); state.reviewed.value = true
    const writes: any[] = []
    repository.createAssetGenerationJob = async payload => {
      writes.push(JSON.parse(JSON.stringify(payload)))
      return {id:'job',executionInstanceVersionId:row.versionId,jobType:'asset',mediaType:kind,preparation:{requestKey:payload.idempotencyKey}}
    }
    await state.submit()
    assert.equal(writes.length,1,state.error.value); assert.equal(events.filter(row=>row[0]==='created').length,1)
    assert.deepEqual(writes[0].fixture.inputs, mode === 'profile' ? {creative_brief:{kind:'literal',value:text}} : {})
    assert.deepEqual(writes[0].fixture.manual_channels, {}); assert.deepEqual(writes[0].fixture.parameters, {seed:0})
    assert.equal(writes[0].fixture.target_language,kind==='video'?'en':'')
    assert.equal(writes[0].allowExternalCalls,false)
    for (const key of ['auxiliaries','prompt_profile_version_id','positive_prompt']) assert.equal(Object.hasOwn(writes[0],key),false)
  })
}

test('generation branch missing language and rejected fields retain original author values for correction', async context => {
  const row=generationOptions.find(row=>row.kind==='video'&&row.promptMode==='profile')
  const {state,repository}=await setup(context,row); await state.choose(row.versionId)
  state.draft.value.literals.creative_brief.value='保留原文 中文 🟢'; state.draft.value.parameters.seed=19
  let calls=0
  repository.createAssetGenerationJob=async()=>{calls++;throw{isAxiosError:true,response:{status:400,data:{detail:'隔离字段拒绝'}}}}
  await nextTick();state.reviewed.value=true;await state.submit()
  assert.match(state.error.value,/输入语言/);assert.equal(calls,0)
  state.draft.value.sourceLanguage='zh-Hans';state.draft.value.targetLanguage='en'
  await nextTick();state.reviewed.value=true
  const original=JSON.stringify(state.draft.value)
  await state.submit();assert.equal(calls,1);assert.match(state.error.value,/隔离字段拒绝/)
  assert.equal(state.pending.value,null);assert.equal(state.reviewed.value,false)
  assert.equal(JSON.stringify(state.draft.value),original)
})

test('generation branch switching clears exact strategy links only after confirmation and failed contract cannot retain them', async context => {
  const row=generationOptions.find(row=>row.kind==='video'&&row.promptMode==='profile')
  const {state,registry}=await setup(context,row);await state.choose(row.versionId)
  state.draft.value.literals.creative_brief.value='未保存创作值';await nextTick()
  await state.choose('');assert.equal(state.configuration.value.prompt_profile_version_id,profileId)
  state.inputChange.value=null;assert.equal(state.draft.value.literals.creative_brief.value,'未保存创作值')
  await state.choose('');await state.confirmChange();assert.equal(state.configuration.value,null);assert.equal(state.contract.value,null)
  registry.contract=async()=>({...generationContract(row),sha256:'f'.repeat(64)})
  await state.choose(row.versionId);assert.equal(state.configuration.value,null);assert.equal(state.draft.value,null)
  assert.match(state.error.value,/目录不一致/)
})

test('generation branch strategy drilldown is exact and copy never requires blanket manual review',()=>{
  const source=readFileSync(new URL('../src/components/AssetExecutionCreateForm.vue',import.meta.url),'utf8')
  assert.match(source,/inspect\('prompt',configuration.prompt_profile_version_id,\$event\)/)
  assert.match(source,/inspect\('execution',auxiliary.target_version_id,\$event\)/)
  assert.match(source,/审核与采用按配置处理/)
  assert.doesNotMatch(source,/逐字段填写并复核/)
  const editor=readFileSync(new URL('../src/components/ExecutionRequestInputEditor.vue',import.meta.url),'utf8')
  assert.match(editor,/是否使用工作流或提示词策略由实例配置决定/)
  assert.doesNotMatch(editor,/工作流与提示词策略已包含|当前策略不使用此语言设置/)
})
