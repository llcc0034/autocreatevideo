import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createSSRApp, createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'

const root = fileURLToPath(new URL('..', import.meta.url))
const outcome = { message: '隔离状态说明', artifact_count: 0, selected_count: 0 }
const mainPresentation = { kind: 'primary', label: '第1集 · 镜头4', operation: 'video.generate', parent: null, task: null }
const preparation = { id: 'prep', stage_run: null, asset_job: 'job', deployment_smoke: null, workflow_smoke: null,
  execution_instance_version: 'version', purpose: 'production', unit_key: 'unit', request_key: 'candidate', revision: 3,
  status: 'preparing', intent_sha256: 'intent-sha', request_sha256: null, request_frozen_at: null, next_check_at: null,
  reason_code: '', reason: '', evaluation_revision: null, evaluated_at: null, evaluation: null, cancellation: null, presentation: null, outcome,
  created_by: 1, created_at: 'created', updated_at: 'updated', intent: {}, resolved_inputs: {}, review_contract: { schema_version: 1, mode: 'manual' },
  review_summary: { mode: 'manual', label: '人工确认字段', description: '必须明确确认与采用' }, prepared_request: null, capacity_sample: null, auxiliary_batches: [], response_diagnostic: null, resource_review: null, text_response_review: null,
  field_slots: [{ unit_key: 'unit', field_key: 'direction', next_revision: 2, source_language: '', target_language: 'en',
    field_label: '导演描述', latest_value_id: 'field-1', effective_language: 'en',
    field_contract: { field_key: 'direction', max_chars: 7000, generation_required: true,
      generation_instruction: 'Describe only supplied facts.', generation_mode: 'model_generated' } }],
  field_selections: [{ id: 'choice', preparation: 'prep', unit_key: 'unit', field_key: 'direction', field_value: 'field-1',
    revision: 1, selected_by: 1, selected_at: 'selected' }] }
const field = { id: 'field-1', preparation: 'prep', unit_key: 'unit', field_key: 'direction', revision: 1, warnings: [],
  source_sha256: 'source-sha', field_contract_sha256: 'rule-sha', text_sha256: 'text-sha', origin_kind: 'manual',
  origin_attempt: null, origin_asset_job: null, status: 'confirmed', approval_kind: 'human', approval_label: '人工审核', reviewed_by: 1,
  reviewed_at: 'reviewed', approval_policy_sha256: null, created_by: 1, created_at: 'created', updated_at: 'updated', text: '  Exact text.\n' }

const recoveryBatchKey = 'a'.repeat(64)
test('uncertain media retry requires explicit scoped acknowledgement and prevents duplicate clicks', async () => {
  const vite = await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  let ctx:any
  try {
    ctx = await recoveryWorkbench(vite)
    const {state,api,data,props}=ctx
    const recovery={taskId:'original-task',attemptId:'original-attempt',taskUpdatedAt:'exact-time',requestSha256:'a'.repeat(64),canRetry:true,reason:''}
    api.get=async()=>({...structuredClone(data),mediaUncertainRetry:recovery})
    await state.load(); await nextTick()
    let writes=0,resolve:any
    api.retryUncertainMedia=async(id,revision,row)=>{writes++;assert.equal(id,'prep');assert.equal(revision,3);assert.equal(row.attemptId,'original-attempt');return new Promise(done=>resolve=done)}
    api.continue=async()=>{throw Error('not an automatic continue')}
    await state.retryUncertainMedia();assert.equal(writes,0)
    state.uncertainMediaAcknowledged.value=true
    const pending=state.retryUncertainMedia();await state.retryUncertainMedia();assert.equal(writes,1)
    api.get=async()=>({...structuredClone(data),revision:4,mediaUncertainRetry:null})
    resolve(data);await pending;await nextTick()
    assert.equal(state.uncertainMediaAcknowledged.value,false)
    assert.match(state.notice.value,/旧调用仍保留未决结果与费用/)
    props.preparationId='other';await nextTick()
    assert.equal(state.uncertainMediaAcknowledged.value,false)
  } finally {ctx?.app.unmount();await vite.close()}
})

test('media archive recovery is explicit, bounded to the observed task, and never calls continue or model retry', async () => {
  const vite = await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  let app:any
  try {
    const {adaptExecutionPreparationDetail:adapt}=await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const {executionPreparationApi:api}=await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const Panel=(await vite.ssrLoadModule('/src/components/ExecutionPreparationWorkbench.vue')).default
    const recovery={task_id:'original-task',attempt_id:'original-attempt',task_updated_at:'exact-time',receipt_sha256:'a'.repeat(64),can_recover:true,reason:''}
    const detail=adapt({...preparation,media_archive_recovery:recovery})
    let writes=0,resolve:any,state:any
    api.get=async()=>detail
    api.continue=api.retryFieldBatch=async()=>{throw Error('must not generate')}
    api.recoverMediaArchive=async(id,revision,row)=>{writes++;assert.equal(id,'prep');assert.equal(revision,3);assert.equal(row.taskId,'original-task');return new Promise(done=>resolve=done)}
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    app=renderer.createApp({setup(){state=Panel.setup({preparationId:'prep',versionId:'version',purpose:'production'},{emit(){},expose(){}});return()=>h('div')}})
    app.provide(ssrContextKey,{});app.mount({});await state.load()
    assert.equal(writes,0)
    const pending=state.recoverMediaArchive();await state.recoverMediaArchive();assert.equal(writes,1)
    resolve(detail);await pending;assert.match(state.notice.value,/不.*重新生成/)
    api.recoverMediaArchive=async()=>{throw Error('原回执失效')}
    await state.recoverMediaArchive();assert.match(state.error.value,/原回执失效/);assert.equal(state.notice.value,'')
  }finally{app?.unmount();await vite.close()}
})
const waitingFields = (actions: string[] = ['select']) => ({ ...preparation, status: 'awaiting_review', reason_code: 'execution_fields_unselected',
  reason: '服务端原原因', evaluation_revision: 3, evaluated_at: '2026-10-06T07:00:00Z', field_selections: [],
  field_slots: actions.map((action, index) => ({ ...preparation.field_slots[0], field_key: `direction-${index}`,
    field_contract: { ...preparation.field_slots[0].field_contract, field_key: `direction-${index}` } })),
  evaluation: { schema_version: 1, phase: 'fields', status: 'awaiting_review', reason_code: 'execution_fields_unselected', preparation_revision: 3,
    outstanding_fields: actions.map((action, index) => ({ unit_key: 'unit', field_key: `direction-${index}`, required: true, action })) } })

test('current server field evaluation distinguishes review, adoption and mixed waits without a new state', async () => {
  const vite = await createServer({ root, envFile: false, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    for (const [actions, label] of [[['select'], '待采用字段'], [['review'], '待审核字段'], [['review', 'select'], '待审核 / 采用字段']] as const) {
      const data = m.adaptExecutionPreparationDetail(waitingFields([...actions])), original = JSON.stringify(data)
      assert.equal(m.preparationFieldWait(data).label, label)
      assert.equal(data.status, 'awaiting_review'); assert.equal(data.reason, '服务端原原因')
      assert.equal(JSON.stringify(data), original)
    }
    assert.equal(m.preparationFieldWait(null), null)
  } finally { await vite.close() }
})

test('stale, invalid, auxiliary and non-field waits never masquerade as adopted or reviewed', async () => {
  const vite = await createServer({ root, envFile: false, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionPreparation.ts'), data = m.adaptExecutionPreparationDetail(waitingFields())
    const alternatives = [
      { ...data, revision: 4 }, { ...data, evaluationRevision: 2 }, { ...data, evaluation: null },
      { ...data, status: 'blocked' }, { ...data, reasonCode: 'execution_resource_confirmation_required' },
      { ...data, requestFrozenAt: 'frozen' }, { ...data, presentation: { ...mainPresentation, kind: 'auxiliary' } },
      { ...data, fieldSelections: [{ ...m.adaptExecutionFieldSelection(preparation.field_selections[0]), fieldKey: 'direction-0' }] },
      { ...data, fieldSlots: [] }, { ...data, fieldSlots: data.fieldSlots.map((row: any) => ({ ...row, required: false })) },
      ...[{ schema_version: 9 }, { phase: 'admission' }, { status: 'preparing' }, { reason_code: 'other' },
        { preparation_revision: 2 }, { outstanding_fields: null }, { outstanding_fields: [] },
        { outstanding_fields: [null] }, { outstanding_fields: [{ ...data.evaluation.outstanding_fields[0], action: 'input' }] },
        { outstanding_fields: [{ ...data.evaluation.outstanding_fields[0], action: 'ready' }] },
        { outstanding_fields: [{ ...data.evaluation.outstanding_fields[0], unit_key: 'foreign' }] },
        { outstanding_fields: [{ ...data.evaluation.outstanding_fields[0], required: false }] },
        { outstanding_fields: [data.evaluation.outstanding_fields[0], data.evaluation.outstanding_fields[0]] }]
        .map(evaluation => ({ ...data, evaluation: { ...data.evaluation, ...evaluation } })),
    ]
    for (const alternative of alternatives) assert.equal(m.preparationFieldWait(alternative), null)
  } finally { await vite.close() }
})

test('preparation contextual label keeps actual status and reason and unknown statuses stay unknown', async () => {
  const vite = await createServer({ root, envFile: false, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try {
    const Pill = (await vite.ssrLoadModule('/src/components/PlatformStatusPill.vue')).default
    const render = (props: any) => renderToString(createSSRApp({ render: () => h(Pill, props) }))
    const contextual = await render({ status: 'awaiting_review', contextLabel: '待采用字段' })
    assert.match(contextual, /待采用字段/); assert.match(contextual, /awaiting_review/); assert.match(contextual, /warning/)
    assert.match(await render({ status: 'awaiting_review' }), /待人工确认/)
    const unknown = await render({ status: 'unknown_future_state', contextLabel: '已通过' })
    assert.doesNotMatch(unknown, /已通过/); assert.match(unknown, /unknown_future_state/); assert.match(unknown, /unknown/)
  } finally { await vite.close() }
})

test('actual workbench renders adoption wait from its dated evaluation without confirming or running', async () => {
  const vite = await createServer({ root, envFile: false, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const { executionPreparationApi: api } = await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const Panel = (await vite.ssrLoadModule('/src/components/ExecutionPreparationWorkbench.vue')).default
    let writes = 0
    api.get = async () => m.adaptExecutionPreparationDetail(waitingFields())
    for (const action of ['confirmField', 'bulkConfirm', 'continue', 'retryFieldBatch', 'createField']) api[action] = async () => { writes++; throw Error('Display cannot mutate') }
    const Loaded = { ...Panel, async setup(props: any, context: any) { const state = Panel.setup(props, context); await state.load(); return state } }
    const html = await renderToString(createSSRApp({ render: () => h(Loaded, { preparationId: 'prep', versionId: 'version', purpose: 'production' }) }))
    assert.match(html, /待采用字段/); assert.match(html, /awaiting_review/); assert.match(html, /服务端原原因/)
    assert.match(html, /尚未采用，不代表已提交执行/); assert.equal(writes, 0)
  } finally { await vite.close() }
})

test('request document size failure keeps server byte counts and configuration guidance visible without writes', async () => {
  const vite = await createServer({ root, envFile: false, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const { executionPreparationApi: api } = await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const Panel = (await vite.ssrLoadModule('/src/components/ExecutionPreparationWorkbench.vue')).default
    const reason = '原生文本请求计划为93529字节，超过当前65536字节上限（security.snapshot.stage_max_bytes）。尚未提交主调用；请核对系统配置中的请求文档边界，不会截断正文。'
    const diagnostic = { code: 'execution_request_too_large', part: 'native_plan', actual_bytes: 93529, max_bytes: 65536,
      configuration_key: 'security.snapshot.stage_max_bytes', message: reason }
    const data = m.adaptExecutionPreparationDetail({ ...preparation, status: 'blocked', reason_code: 'execution_request_invalid', reason,
      field_slots: [], field_selections: [], evaluation_revision: 3, evaluated_at: '2026-10-13T12:00:00Z',
      evaluation: { schema_version: 1, phase: 'compilation', status: 'blocked', reason_code: 'execution_request_invalid',
        reason, preparation_revision: 3, diagnostic } })
    let writes = 0
    api.get = async () => data
    for (const action of ['confirmField', 'bulkConfirm', 'continue', 'retryFieldBatch', 'createField']) api[action] = async () => { writes++; throw Error('Display cannot mutate') }
    const Loaded = { ...Panel, async setup(props: any, context: any) { const state = Panel.setup(props, context); await state.load(); return state } }
    const html = await renderToString(createSSRApp({ render: () => h(Loaded, { preparationId: 'prep', versionId: 'version', purpose: 'production' }) }))
    assert.ok(html.includes(reason)); assert.match(html, /检查并继续此任务/)
    assert.deepEqual(data.evaluation.diagnostic, diagnostic)
    assert.equal(data.requestFrozenAt, null); assert.equal(writes, 0)
  } finally { await vite.close() }
})

test('explicit adoption calls only the original field action and does not promise worker advancement needs another click', async () => {
  const vite = await createServer({ root, envFile: false, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  let app: any
  try {
    const m = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const { executionPreparationApi: api } = await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const Panel = (await vite.ssrLoadModule('/src/components/ExecutionPreparationWorkbench.vue')).default
    const originalField = m.adaptExecutionFieldDetail(field)
    let data = m.adaptExecutionPreparationDetail({ ...preparation, field_selections: [] }), adoptions = 0, continues = 0, state: any
    api.get = async () => data; api.field = async () => originalField
    api.continue = async () => { continues++; throw Error('Adoption UI cannot call continue') }
    api.confirmField = async (id: string, input: any) => {
      assert.equal(id, originalField.id); assert.equal(input.selectForExecution, true); adoptions++
      data = m.adaptExecutionPreparationDetail({ ...preparation, revision: 4 })
      return { field: originalField, preparation: data, selection: data.fieldSelections[0] }
    }
    const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
    app = renderer.createApp({ setup() { state = Panel.setup({ preparationId: 'prep', versionId: 'version', purpose: 'production' }, { emit() {}, expose() {} }); return () => h('div') } })
    app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); await state.load(); await state.openSlot(data.fieldSlots[0])
    await state.review('select')
    assert.equal(adoptions, 1); assert.equal(continues, 0)
    assert.equal(state.preparation.value.fieldSelections[0].fieldValue, originalField.id)
    assert.match(state.notice.value, /系统会按原任务规则检查后续执行条件/)
    assert.doesNotMatch(state.notice.value, /点击.*才推进任务/)
  } finally { app?.unmount(); await vite.close() }
})

test('read-only refresh switches review to adoption only on the returned current evaluation', async () => {
  const vite = await createServer({ root, envFile: false, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  let app: any
  try {
    const m = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const { executionPreparationApi: api } = await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const Panel = (await vite.ssrLoadModule('/src/components/ExecutionPreparationWorkbench.vue')).default
    let next: any = waitingFields(['review']), writes = 0, state: any
    api.get = async () => m.adaptExecutionPreparationDetail(next)
    for (const action of ['confirmField', 'bulkConfirm', 'continue', 'retryFieldBatch', 'createField']) api[action] = async () => { writes++; throw Error('Refresh cannot mutate') }
    const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
    app = renderer.createApp({ setup() { state = Panel.setup({ preparationId: 'prep', versionId: 'version', purpose: 'production' }, { emit() {}, expose() {} }); return () => h('div') } })
    app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); await state.load()
    assert.equal(state.fieldWait.value.label, '待审核字段')
    next = waitingFields(); await state.load(); assert.equal(state.fieldWait.value.label, '待采用字段')
    next = { ...waitingFields(), revision: 4 }; await state.load(); assert.equal(state.fieldWait.value, null)
    assert.equal(writes, 0)
  } finally { app?.unmount(); await vite.close() }
})
test('unit groups validate exact stage, kind and unit while history remains paginated', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const { executionPreparationApi: client } = await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const { api } = await vite.ssrLoadModule('/src/services/api.ts')
    const row = { ...preparation, stage_run:'stage', asset_job:null, presentation:mainPresentation }
    const group = {unit_key:'unit', label:'第1集 · 镜头4', version_count:2, latest:row}
    const raw = {count:1, next:null, previous:null, results:[group]}
    assert.equal(m.adaptPreparationGroupPage(raw,'stage','primary').results[0].versionCount,2)
    for (const bad of [{...group, unit_key:'foreign'}, {...group, version_count:0}, {...group, latest:{...row, stage_run:'foreign'}}]) {
      assert.throws(()=>m.adaptPreparationGroupPage({...raw,results:[bad]},'stage','primary'))
    }
    assert.throws(()=>m.adaptPreparationGroupPage(raw,'stage','auxiliary'))
    assert.throws(()=>m.adaptPreparationGroupPage({...raw,results:[group,group]},'stage','primary'))
    const calls:any[]=[]
    api.get=async (url:any, options:any)=> { calls.push({url,options}); return {data:raw} }
    await client.groups('stage',2,'primary')
    assert.equal(calls[0].url,'/execution-preparations/groups/')
    assert.deepEqual(calls[0].options.params,{stage_run_id:'stage',page:2,kind:'primary'})
    api.get=async (url:any, options:any)=> { calls.push({url,options}); return {data:{count:2,next:null,previous:'prev',results:[row]}} }
    const history=await client.forStage('stage',2,'primary','unit')
    assert.equal(history.count,2)
    assert.equal(calls[1].options.params.unit_key,'unit')
    await assert.rejects(client.forStage('stage',2,'primary','foreign'),/不属于/)
  } finally { await vite.close() }
})

test('stage renders one summary per unit with latest result and loads no collapsed history', async () => {
  const vite = await createServer({ root, logLevel:'silent', appType:'custom', server:{middlewareMode:true,hmr:false} })
  try {
    const { executionPreparationApi:api } = await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const Panel=(await vite.ssrLoadModule('/src/components/StageExecutionPreparations.vue')).default
    let historyReads=0, writes=0
    api.forStage=async()=>{historyReads++;throw Error('collapsed')}
    api.continue=api.retryFieldBatch=async()=>{writes++;throw Error('no writes')}
    api.groups=async()=>({count:1,next:null,previous:null,results:[{unitKey:'unit',label:'第1集 · 镜头6',versionCount:2,
      latest:{id:'latest',unitKey:'unit',requestKey:'attempt:2',purpose:'production',executionInstanceVersion:'version',status:'dispatched',
        outcome:{message:'主调用成功，已记录 1 项产物。',artifactCount:1,selectedCount:0},
        presentation:{...mainPresentation,task:{id:'task',status:'succeeded',attemptNumber:2,errorCode:''}}}}]})
    const Loaded={...Panel,async setup(props:any,ctx:any){const state=Panel.setup(props,ctx);await Promise.resolve();await nextTick();return state}}
    const html=await renderToString(createSSRApp({render:()=>h(Loaded,{stageId:'stage'})}))
    assert.equal((html.match(/<article/g)||[]).length,1)
    assert.match(html,/最近一次调用/);assert.match(html,/执行版本 \/ 历史（2）/)
    assert.match(html,/succeeded/);assert.match(html,/第 2 次/)
    assert.equal(historyReads,0);assert.equal(writes,0)
  } finally {await vite.close()}
})

test('unit history ignores old unit responses, reports errors and never submits calls',async()=>{
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  let app:any
  try {
    const {executionPreparationApi:api}=await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const Panel=(await vite.ssrLoadModule('/src/components/PreparationUnitHistory.vue')).default
    const requests:any[]=[], calls:any[]=[];let state:any
    api.forStage=(...args:any[])=>{calls.push(args);return new Promise((resolve,reject)=>requests.push({resolve,reject}))}
    const props=reactive({stageId:'stage',kind:'auxiliary',unitKey:'shot:6',latestId:'latest',busy:false})
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    app=renderer.createApp({setup(){state=Panel.setup(props,{emit(){},expose(){}});return()=>h('div')}})
    app.provide(ssrContextKey,{});app.mount({});await nextTick()
    assert.deepEqual(calls[0],['stage',1,'auxiliary','shot:6'])
    props.unitKey='shot:7';await nextTick()
    requests[1].resolve({count:1,results:[],next:null,previous:null});await Promise.resolve()
    requests[0].resolve({count:99,results:[],next:null,previous:null});await Promise.resolve()
    assert.equal(state.result.value.count,1)
    const pending=state.load(2);requests[2].reject(Error('history unavailable'));await pending
    assert.match(state.error.value,/history unavailable/)
    const retry=state.load(2);requests[3].resolve({count:3,results:[],next:null,previous:'prev'});await retry
    assert.equal(state.page.value,2);assert.equal(state.error.value,'')
  } finally {app?.unmount();await vite.close()}
})

test('retained output renders actual field text or raw malformed body, escapes HTML and never adopts', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try {
    const View = (await vite.ssrLoadModule('/src/components/ExecutionReturnedText.vue')).default
    const raw = '<script>do-not-run()</script> unparsed model output'
    const response = { originalText: raw, fields: [], notices: [], diagnostics: { summary: '格式未通过，原文已保留' } }
    const render = (value:any) => renderToString(createSSRApp({ render: () => h(View, { response: value }) }))
    const html = await render(response)
    assert.match(html, /无法可靠拆分/); assert.match(html, /&lt;script&gt;/)
    assert.doesNotMatch(html, /<script>|<button|<textarea/)
    const parsed = await render({ ...response, fields: [{ unitKey: 'unit', fieldKey: 'direction', label: '导演描述', text: 'Actual response body.' }], notices: [{code: 'generated_field_label', message: '建议不阻止确认'}] })
    assert.match(parsed, /Actual response body/); assert.match(parsed, /建议不阻止确认/)
    assert.match(parsed, /查看完整原始响应/); assert.doesNotMatch(parsed, /无法可靠拆分|<details[^>]*open/)
  } finally { await vite.close() }
})

test('main preparation reads its own auxiliary raw response on demand, rejecting foreign and late results', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  let app:any
  try {
    const { adaptExecutionPreparationDetail: adapt } = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const { executionPreparationApi: api } = await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const Panel = (await vite.ssrLoadModule('/src/components/ExecutionPreparationWorkbench.vue')).default
    const cancellation = { allowed: false, reason: '只读测试', cancelled_at: null, cancelled_by: null }
    const main = adapt({ ...preparation, stage_run: 'stage', asset_job: null, cancellation, field_selections: [],
      field_slots: [{...preparation.field_slots[0], latest_value_id: null}],
      auxiliary_batches: [{field_batch_key: recoveryBatchKey, purpose: 'generate_fields', field_keys: ['direction'], attempt_number: 1, task_id: 'task', preparation_id: 'child', status: 'failed', reason: '', can_retry: true, requires_uncertain_acknowledgement: false}] })
    const child = adapt({ ...preparation, id: 'child', stage_run: 'stage', asset_job: null, cancellation, field_slots: [], field_selections: [],
      presentation: {kind: 'auxiliary', label: '辅助', operation: 'prompt.fields.generate', parent: {id: 'prep', label: '镜头', status:'blocked', unit_key:'unit'}, task: {id: 'task', status: 'awaiting_review', error_code:'execution_output_review_required', attempt_number: 1}},
      text_response_review: {id:'review', attempt_id:'task', asset_job_id:null, corrected_text:null, corrected_sha256:null, confirmed_by:null, original_text:'actual rejected result', original_sha256:'a'.repeat(64), fields:[], notices:[], diagnostics:{summary:'保留原文'}, editable:false, confirmed_at:null, readonly_reason:'人工另存'} })
    const calls:string[] = []; let next:any = async () => child; let state:any; let writes = 0
    api.get = async (id:string) => { calls.push(id); return id === 'prep' ? main : next() }
    api.continue = api.confirmField = api.retryFieldBatch = async () => { writes++; throw Error('No writes') }
    const renderer = createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    app=renderer.createApp({setup(){state=Panel.setup(reactive({preparationId:'prep',versionId:'version',purpose:'production'}),{emit(){},expose(){}});return()=>h('div')}})
    app.provide(ssrContextKey, {}); app.mount({}); await Promise.resolve(); await nextTick()
    assert.deepEqual(calls, ['prep'])
    await state.loadReturnedText(); assert.equal(state.returned.value.textResponseReview.originalText, 'actual rejected result')
    next = async () => ({...child, presentation:{...child.presentation,parent:{id:'foreign'}}})
    await state.loadReturnedText(); assert.equal(state.returned.value,null); assert.match(state.returnedError.value,/不一致/)
    let resolve:any; next = () => new Promise(done=>{resolve=done})
    const pending=state.loadReturnedText(); await state.openSlot(main.fieldSlots[0]); resolve(child); await pending
    assert.equal(state.returned.value,null)
    const before=calls.length; state.text.value='unsaved'; await state.loadReturnedText(); assert.equal(calls.length,before)
    assert.equal(writes,0)
  } finally {app?.unmount();await vite.close()}
})

test('auxiliary detail shows actual outcome and parent review entry, not an empty human-approval card', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try {
    const { adaptExecutionPreparationDetail: adapt } = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const { executionPreparationApi: api } = await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const Panel = (await vite.ssrLoadModule('/src/components/ExecutionPreparationWorkbench.vue')).default
    let writes = 0
    api.continue = api.confirmField = api.retryFieldBatch = async () => { writes++; throw Error('must not write') }
    let status = 'succeeded'
    api.get = async () => adapt({ ...preparation, status: 'dispatched', field_slots: [], field_selections: [],
      response_diagnostic: status === 'failed' ? {state:'unverifiable',task_id:'task',observed_at:null,details:[],summary:'历史原文当时未保存，无法恢复'} : null,
      presentation: { kind: 'auxiliary', label: '第1集 · 镜头4 · 提示词生成', operation: 'prompt.fields.generate',
        parent: { id: 'parent', label: '第1集 · 镜头4', status: 'awaiting_review', unit_key: 'unit' },
        task: { id: 'task', status, error_code: status === 'failed' ? 'execution_response_invalid' : '', attempt_number: 1 } } })
    const Loaded = { ...Panel, async setup(props:any, context:any) { const state = Panel.setup(props, context); await Promise.resolve(); await nextTick(); return state } }
    const render = () => renderToString(createSSRApp({ render: () => h(Loaded, { preparationId: 'prep', versionId: 'version', purpose: 'production', allowPreparationNavigation: true }) }))
    const html = await render()
    assert.match(html, /实际调用结果/); assert.match(html, /succeeded/)
    assert.match(html, /第1集 · 镜头4/); assert.match(html, /查看生成正文 \/ 前往主准备审核/)
    assert.doesNotMatch(html, /<h4>人工确认字段|本次无需额外填写或复核策略字段|确认并采用此修订/)
    assert.ok(html.indexOf('辅助任务结果与正文入口') < html.indexOf('输入与运行参数'))
    assert.doesNotMatch(html, /<details[^>]*open/)
    status = 'failed'; const failure = await render()
    assert.match(failure, /execution_response_invalid/); assert.doesNotMatch(failure, /succeeded/)
    assert.equal((failure.match(/历史原文当时未保存，无法恢复/g)||[]).length, 1)
    assert.doesNotMatch(failure, /本次文本响应诊断|现在文本的新请求输出额度/)
    assert.equal(writes, 0)
  } finally { await vite.close() }
})

test('stage navigation is scoped, read-only and ignores late parent and page responses', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  let app:any
  try {
    const { executionPreparationApi: api } = await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const Panel = (await vite.ssrLoadModule('/src/components/StageExecutionPreparations.vue')).default
    const main = { id: 'parent', stageRun: 'stage', executionInstanceVersion: 'version', purpose: 'production', presentation: mainPresentation }
    const child = { ...main, id: 'child' }
    const calls:any[] = []; const pages:any[] = []
    api.groups = (...args:any[]) => { calls.push(args); return new Promise(resolve => pages.push(resolve)) }
    api.get = async () => main
    let state:any
    const renderer = createRenderer<any,any>({ createElement:()=>({}), createText:()=>({}), createComment:()=>({}), insert(){}, remove(){}, setText(){}, setElementText(){}, parentNode:()=>null, nextSibling:()=>null, patchProp(){} })
    app = renderer.createApp({ setup() { state = Panel.setup(reactive({ stageId: 'stage' }), { emit(){}, expose(){} }); return () => h('div') } })
    app.provide(ssrContextKey, {}); app.mount({}); await nextTick()
    assert.deepEqual(calls[0], ['stage', 1, 'primary'])
    state.switchKind('auxiliary'); assert.deepEqual(calls[1], ['stage', 1, 'auxiliary'])
    pages[1]({ count: 1, results: [child], previous: null, next: null }); await Promise.resolve()
    pages[0]({ count: 9, results: [main], previous: null, next: null }); await Promise.resolve()
    assert.equal(state.result.value.count, 1)
    state.select(child, '辅助任务'); await state.openParent('parent')
    assert.equal(state.selected.value.id, 'parent'); assert.equal(state.selectedLabel.value, mainPresentation.label)
    state.select(child, '辅助任务'); api.get = async () => ({ ...main, stageRun: 'foreign' })
    await state.openParent('parent'); assert.equal(state.selected.value.id, 'child'); assert.match(state.navigationError.value, /不一致/)
    let resolve:any
    api.get = () => new Promise(done => { resolve = done })
    const pending = state.openParent('parent'); state.close(); resolve(main); await pending
    assert.equal(state.selected.value, null)
    state.select(child, '辅助任务'); state.setBusy(true); await state.openParent('parent')
    assert.equal(state.selected.value.id, 'child')
    state.setBusy(false)
  } finally { app?.unmount(); await vite.close() }
})

test('presentation adapter rejects fabricated call success and absent list metadata', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    for (const value of [undefined, { ...mainPresentation, kind: 'guess' }, { ...mainPresentation, task: { id: 'task', status: 'dispatched', attempt_number: 1, error_code: '' } }, { ...mainPresentation, parent: { id: 'foreign', label: 'foreign', status: 'ready' } }]) assert.throws(() => m.adaptPreparationPresentation(value))
    assert.equal(m.adaptPreparationPresentation(mainPresentation).task, null)
    for (const bad of [undefined, {...outcome, artifact_count:-1}, {...outcome, artifact_count:1.5},
      {...outcome, selected_count:1}, {...outcome, message:null}]) {
      assert.throws(()=>m.adaptExecutionPreparationDetail({...preparation,outcome:bad}))
    }
    const { executionPreparationApi: client } = await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const { api } = await vite.ssrLoadModule('/src/services/api.ts')
    const calls:any[] = []
    api.get = async (url:any, options:any) => { calls.push(options); return { data: { count: 1, next: null, previous: null, results: [{ ...preparation, stage_run: 'stage', asset_job: null, presentation: mainPresentation }] } } }
    await client.forStage('stage', 2, 'primary')
    assert.deepEqual(calls[0].params, { stage_run_id: 'stage', page: 2, kind: 'primary' })
    await assert.rejects(client.forStage('stage', 1, 'auxiliary'), /分类/)
  } finally { await vite.close() }
})
// Mount the real setup/watch lifecycle; requests are isolated, never sent to a provider.
test('primary outcome keeps failure, cancellation and recorded outputs distinct on reads and refreshes', async()=>{
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try {
    const {adaptExecutionPreparationDetail:adapt}=await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const {executionPreparationApi:api}=await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const Panel=(await vite.ssrLoadModule('/src/components/ExecutionPreparationWorkbench.vue')).default
    let writes=0,reads=0
    api.continue=api.confirmField=api.retryFieldBatch=async()=>{writes++;throw Error('read only')}
    for (const [status,taskStatus,message] of [
      ['dispatched','failed','主调用失败，辅助字段成功不代表媒体生成成功。'],
      ['cancelled','submitted','此准备已停止；远端调用尚未确认终态。'],
      ['blocked',null,'主调用尚未创建；输入准备受阻。'],
      ['dispatched','succeeded','主调用成功，已记录 1 项产物，其中 0 项标记为已选择。'],
      ['cancelled',null,'独立素材准备已停止，远端终态尚未确认。'],
    ]) {
      api.get=async()=>{reads++;return adapt({...preparation,status,field_slots:[],field_selections:[],
        outcome:{message,artifact_count:taskStatus==='succeeded'?1:0,selected_count:0},
        request_frozen_at:status==='blocked'?null:'2026-09-29T00:00:00Z',presentation:status==='cancelled'&&!taskStatus?null:{...mainPresentation,
        task:taskStatus?{id:'task',status:taskStatus,attempt_number:1,error_code:taskStatus==='failed'?'fixture_error':''}:null}})}
      const Loaded={...Panel,async setup(props:any,ctx:any){const state=Panel.setup(props,ctx);await Promise.resolve();await nextTick();await state.load();return state}}
      const html=await renderToString(createSSRApp({render:()=>h(Loaded,{preparationId:'prep',versionId:'version',purpose:'production'})}))
      assert.ok(html.includes(message!));assert.match(html,/主调用与产物状态/)
      if(status==='cancelled')assert.doesNotMatch(html,/检查并继续此任务/)
    }
    assert.equal(writes,0);assert.equal(reads,10)
  }finally{await vite.close()}
})

async function recoveryWorkbench(vite: any) {
  const { adaptExecutionPreparationDetail: adapt, adaptExecutionFieldDetail: adaptField } = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
  const { executionPreparationApi: api } = await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
  const Panel = (await vite.ssrLoadModule('/src/components/ExecutionPreparationWorkbench.vue')).default
  const data = adapt({ ...preparation, auxiliary_batches: [{ field_batch_key: recoveryBatchKey, purpose: 'generate_fields',
    field_keys: ['direction'], attempt_number: 2, task_id: 'failed-original', preparation_id: 'aux-original', status: 'failed', reason: 'invalid output', can_retry: true, requires_uncertain_acknowledgement: false }] })
  const props = reactive({ preparationId: 'prep', versionId: 'version', purpose: 'production' })
  const reads: string[] = [], events: string[] = []
  api.get = async (id: string) => { reads.push(id); return { ...structuredClone(data), id } }
  api.field = async () => adaptField(field)
  let state: any
  const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
    insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
  const app = renderer.createApp({ setup() { state = Panel.setup(props, { emit(name: string) { events.push(name) }, expose() {} }); return () => h('div') } })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); await nextTick()
  return { app, state, props, api, data, reads, events, Panel, viewed: adaptField(field) }
}

test('all recovery drafts block continue and auxiliary retry without losing input; confirmation is scoped and read-only', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  let ctx: any
  try {
    ctx = await recoveryWorkbench(vite)
    const { state, api, Panel } = ctx
    let writes = 0
    api.continue = api.retryFieldBatch = async () => { writes++; throw new Error('must not call') }
    for (const [key, value] of [['text', 'field draft'], ['smokeDirty', true], ['recoveryText', 'unfinished JSON']] as const) {
      state[key].value = value; await nextTick(); state.continueAcknowledged.value = true
      assert.equal(state.canAdvance.value, false)
      await state.advance(); state.requestBatchRetry(state.preparation.value.auxiliaryBatches[0]); await state.retryBatch()
      assert.equal(writes, 0); assert.equal(state.pendingBatchRetry.value, null); assert.equal(state[key].value, value)
      state[key].value = key === 'smokeDirty' ? false : ''; await nextTick()
    }
    state.requestBatchRetry(state.preparation.value.auxiliaryBatches[0])
    assert.equal(state.canAdvance.value, false); assert.equal(writes, 0)
    const Confirming = { ...Panel, async setup(props: any, context: any) {
      const current = Panel.setup(props, context); await Promise.resolve(); await nextTick()
      current.requestBatchRetry(current.preparation.value.auxiliaryBatches[0]); return current
    } }
    const html = await renderToString(createSSRApp({ render: () => h(Confirming, { preparationId: 'prep', versionId: 'version', purpose: 'production' }) }))
    for (const part of ['确认重试辅助批次', '主准备 prep · 修订 3', '第 3 次尝试', recoveryBatchKey, 'failed-original', 'aux-original', '可能产生费用', '返回，不重试', '确认重试此批次']) assert.ok(html.includes(part), part)
    state.pendingBatchRetry.value = null; await state.retryBatch(); assert.equal(writes, 0)
    state.preparation.value.requestFrozenAt = 'frozen'; state.preparation.value.status = 'dispatched'
    state.continueAcknowledged.value = true; await state.advance(); state.requestBatchRetry(state.preparation.value.auxiliaryBatches[0])
    assert.equal(state.pendingBatchRetry.value, null); assert.equal(writes, 0)
    state.preparation.value.status = 'cancelled'; state.preparation.value.reasonCode = 'execution_resource_confirmation_required'
    assert.equal(state.canAdvance.value, false); await state.advance(); assert.equal(writes, 0)
  } finally { ctx?.app.unmount(); await vite.close() }
})

test('auxiliary retry uses only current server permission and exact confirmation; uncertain writes require rereading the same preparation', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  let ctx: any
  try {
    ctx = await recoveryWorkbench(vite)
    const { state, api, data, reads } = ctx
    const calls: any[] = []; let resolve: any
    api.retryFieldBatch = (...args: any[]) => { calls.push(args); return new Promise(done => { resolve = done }) }
    const batch = state.preparation.value.auxiliaryBatches[0]
    for (const status of ['succeeded', 'running', 'queued', 'failed']) {
      batch.status = status; batch.canRetry = false
      state.requestBatchRetry({ ...batch, canRetry: true })
      assert.equal(state.pendingBatchRetry.value, null) // caller cannot manufacture permission, including unknown remote outcome
    }
    batch.status = 'failed'; batch.canRetry = true
    state.requestBatchRetry(batch); state.preparation.value.revision++
    await state.retryBatch(); assert.equal(calls.length, 0); assert.equal(state.needsRefresh.value, true)
    assert.match(state.error.value, /已变化/); await state.load()
    state.requestBatchRetry(state.preparation.value.auxiliaryBatches[0])
    state.recoveryText.value = 'do not lose'; await state.retryBatch(); assert.equal(calls.length, 0)
    state.recoveryText.value = ''; await nextTick()
    const pending = state.retryBatch(); await state.retryBatch()
    assert.deepEqual(calls, [['prep', 3, recoveryBatchKey, 3]])
    resolve({ ...structuredClone(data), revision: 4, auxiliaryBatches: [{ ...data.auxiliaryBatches[0], attemptNumber: 3, canRetry: false, status: 'queued' }] })
    await pending; assert.equal(state.pendingBatchRetry.value, null); assert.equal(state.busy.value, false)
    assert.equal(state.preparation.value.auxiliaryBatches[0].status, 'queued')
    await state.load(); state.requestBatchRetry(state.preparation.value.auxiliaryBatches[0])
    api.retryFieldBatch = async (...args: any[]) => { calls.push(args); throw new Error('response unavailable') }
    await state.retryBatch(); assert.equal(state.needsRefresh.value, true); assert.match(state.error.value, /response unavailable/)
    await state.retryBatch(); state.continueAcknowledged.value = true; await state.advance(); assert.equal(calls.length, 2)
    api.get = async (id: string) => { reads.push(id); return { ...structuredClone(data), revision: 5, auxiliaryBatches: [{ ...data.auxiliaryBatches[0], attemptNumber: 3, canRetry: false, status: 'queued' }] } }
    await state.load()
    assert.equal(state.pendingBatchRetry.value, null); assert.equal(state.needsRefresh.value, false)
    assert.equal(state.preparation.value.revision, 5); assert.ok(reads.every((id: string) => id === 'prep')); assert.equal(calls.length, 2)
    api.continue = async (...args: any[]) => { calls.push(args); throw new Error('continue receipt unavailable') }
    state.continueAcknowledged.value = true; await state.advance(); await state.advance()
    assert.deepEqual(calls[2], ['prep', 5]); assert.equal(calls.length, 3); assert.equal(state.needsRefresh.value, true)
    await state.load(); assert.equal(calls.length, 3); assert.equal(state.continueAcknowledged.value, false)
  } finally { ctx?.app.unmount(); await vite.close() }
})

test('uncertain auxiliary retry requires exact explicit acknowledgement and retains unknown-cost warning', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  let ctx: any
  try {
    ctx = await recoveryWorkbench(vite)
    const { state, api, data, Panel } = ctx
    data.auxiliaryBatches[0].status = 'uncertain'
    data.auxiliaryBatches[0].requiresUncertainAcknowledgement = true
    await state.load()
    const calls: any[] = []
    api.retryFieldBatch = async (...args: any[]) => { calls.push(args); return { ...structuredClone(data), revision: 4,
      auxiliaryBatches: [{ ...data.auxiliaryBatches[0], taskId: 'new-automatic', status: 'queued', attemptNumber: 3, canRetry: false }] } }
    state.requestBatchRetry(state.preparation.value.auxiliaryBatches[0])
    assert.equal(calls.length, 0)
    const Confirming = { ...Panel, async setup(props: any, context: any) {
      const current = Panel.setup(props, context); await Promise.resolve(); await nextTick()
      current.requestBatchRetry(current.preparation.value.auxiliaryBatches[0]); return current
    } }
    const html = await renderToString(createSSRApp({ render: () => h(Confirming, { preparationId: 'prep', versionId: 'version', purpose: data.purpose }) }))
    for (const part of ['确认风险并新增尝试', '可能已经计费', '不人工补字段', '不会覆盖新尝试的字段', 'failed-original']) assert.ok(html.includes(part), part)
    await state.retryBatch()
    assert.deepEqual(calls, [['prep', 3, recoveryBatchKey, 3, 'failed-original']])
    assert.equal(state.preparation.value.auxiliaryBatches[0].taskId, 'new-automatic')
    await state.retryBatch(); assert.equal(calls.length, 1)
  } finally { ctx?.app.unmount(); await vite.close() }
})

test('failed production auxiliary uses server risk flag and rejects a changed acknowledgement scope', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  let ctx: any
  try {
    ctx = await recoveryWorkbench(vite)
    const { state, api, data } = ctx
    data.auxiliaryBatches[0].requiresUncertainAcknowledgement = true
    await state.load()
    const calls: any[] = []
    api.retryFieldBatch = async (...args: any[]) => { calls.push(args); return structuredClone(data) }
    state.requestBatchRetry(state.preparation.value.auxiliaryBatches[0])
    state.preparation.value.auxiliaryBatches[0].requiresUncertainAcknowledgement = false
    await state.retryBatch()
    assert.equal(calls.length, 0); assert.equal(state.needsRefresh.value, true)
    await state.load()
    state.requestBatchRetry(state.preparation.value.auxiliaryBatches[0])
    await state.retryBatch()
    assert.deepEqual(calls, [['prep', 3, recoveryBatchKey, 3, 'failed-original']])
  } finally { ctx?.app.unmount(); await vite.close() }
})

test('late continue, retry and field mutation receipts cannot change a different preparation or an unmounted workbench', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try {
    for (const action of ['advance', 'retry', 'create', 'confirm', 'select', 'reject']) {
      for (const outcome of ['success', 'error', 'unmount']) {
        const ctx = await recoveryWorkbench(vite)
        try {
          const { state, props, api, data, viewed, reads, events } = ctx
          let resolve: any, reject: any, writes = 0
          const deferred = () => { writes++; return new Promise((done, fail) => { resolve = done; reject = fail }) }
          api.continue = api.retryFieldBatch = api.createField = api.confirmField = api.rejectField = deferred
          state.continueAcknowledged.value = true; state.slotKey.value = JSON.stringify(['unit', 'direction'])
          state.field.value = viewed
          if (action === 'create') state.text.value = 'original input'
          if (action === 'retry') state.requestBatchRetry(state.preparation.value.auxiliaryBatches[0])
          const pending = action === 'advance' ? state.advance() : action === 'retry' ? state.retryBatch()
            : action === 'create' ? state.createField() : state.review(action)
          assert.equal(writes, 1, action)
          if (outcome === 'unmount') ctx.app.unmount()
          else { props.preparationId = 'other'; await nextTick(); await nextTick() }
          state.notice.value = 'new context notice'; state.error.value = 'new context error'
          state.text.value = 'new draft'; state.busy.value = true; state.field.value = null
          const count = reads.length, changes = events.filter((name: string) => name === 'changed').length
          if (outcome === 'error') reject(new Error('obsolete failure'))
          else resolve(action === 'advance' || action === 'retry' ? data : { preparation: data, field: viewed, selection: null })
          await pending
          assert.equal(state.notice.value, 'new context notice'); assert.equal(state.error.value, 'new context error')
          assert.equal(state.text.value, 'new draft'); assert.equal(state.busy.value, true); assert.equal(state.field.value, null)
          assert.equal(state.needsRefresh.value, false); assert.equal(reads.length, count)
          assert.equal(events.filter((name: string) => name === 'changed').length, changes)
          if (outcome !== 'unmount') assert.equal(state.preparation.value.id, 'other')
        } finally { if (outcome !== 'unmount') ctx.app.unmount() }
      }
    }
  } finally { await vite.close() }
})

test('accepted field saves refresh the original preparation and release busy, while late refreshes cannot release a new action', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  let ctx: any
  try {
    ctx = await recoveryWorkbench(vite)
    const { state, api, data, viewed, props, events } = ctx
    api.createField = async () => ({ preparation: data, field: viewed, selection: null })
    api.confirmField = api.createField
    api.get = async () => ({ ...structuredClone(data), revision: 4 })
    state.slotKey.value = JSON.stringify(['unit', 'direction']); state.text.value = 'saved input'
    await state.createField()
    assert.equal(state.text.value, ''); assert.equal(state.busy.value, false); assert.equal(state.preparation.value.revision, 4)
    assert.equal(events.filter((name: string) => name === 'changed').length, 1)
    let resolve: any
    api.get = () => new Promise(done => { resolve = done })
    const pending = state.review('select'); await nextTick()
    assert.equal(state.loading.value, true); assert.equal(state.busy.value, true)
    api.get = async () => ({ ...structuredClone(data), id: 'other' })
    props.preparationId = 'other'; await nextTick(); await nextTick(); state.busy.value = true
    resolve({ ...structuredClone(data), revision: 5 }); await pending
    assert.equal(state.preparation.value.id, 'other'); assert.equal(state.busy.value, true)
    assert.equal(events.filter((name: string) => name === 'changed').length, 1)
  } finally { ctx?.app.unmount(); await vite.close() }
})

test('read-only values preserve scalars and lazily reveal complete structured evidence without editing controls', async () => {
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try {
    const View=(await vite.ssrLoadModule('/src/components/ReadonlyDataValue.vue')).default
    const render=(value:unknown)=>renderToString(createSSRApp({render:()=>h(View,{value,label:'只读内容'})}))
    for(const [value,expected] of [[0,'0'],[false,'否（false）'],[null,'空值（null）'],[undefined,'字段缺失'],['','空文本'],['  Exact text.\n第二行','  Exact text.\n第二行']] as const){
      const html=await render(value);assert.ok(html.includes(expected));assert.doesNotMatch(html,/<input|<select|<textarea|<button|删除|内容格式/)
    }
    const value={zero:0,enabled:false,nothing:null,empty:[],nested:{text:'<script>not markup</script>',last:'complete evidence'}}
    const before=JSON.stringify(value)
    const collapsed=await render(value);assert.match(collapsed,/字段集合 · 5 项 · 只读/)
    assert.doesNotMatch(collapsed,/<pre|complete evidence/)
    const Open={...View,setup(props:any,context:any){const state=View.setup(props,context);state.expanded.value=true;return state}}
    const html=await renderToString(createSSRApp({render:()=>h(Open,{value,label:'完整记录'})}))
    assert.match(html,/complete evidence/);assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>|<input|<select|<textarea|<button/)
    assert.equal(JSON.stringify(value),before)
    assert.match(await render([]),/列表 · 0 项/);assert.match(await render({}),/字段集合 · 0 项/)
  }finally{await vite.close()}
})

test('preparation input summary reads saved values only, distinguishes missing data and leaves diagnostics collapsed',async()=>{
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try{
    const Facts=(await vite.ssrLoadModule('/src/components/ExecutionPreparationFacts.vue')).default
    const value={schema_version:1,inputs:{schema_version:1,logical_values:{creative:'English creative',count:0,enabled:false,nothing:null,empty:''},input_provenance:[{input_key:'creative',proof:'frozen evidence'}]},parameters:{values:{seed:0}},future_field:{kept:true}}
    const before=JSON.stringify(value)
    const closed=await renderToString(createSSRApp({render:()=>h(Facts,{resolvedInputs:value})}))
    assert.match(closed,/5 项输入 · 1 项参数/);assert.doesNotMatch(closed,/English creative|schema_version|<pre/)
    const Open={...Facts,setup(props:any,context:any){const state=Facts.setup(props,context);state.expanded.value=true;return state}}
    const render=(resolvedInputs:any)=>renderToString(createSSRApp({render:()=>h(Open,{resolvedInputs})}))
    const html=await render(value)
    for(const part of ['English creative','count','否（false）','空值（null）','空文本','seed','实际运行参数','查看来源、映射与完整记录'])assert.ok(html.includes(part),part)
    assert.doesNotMatch(html,/<input|<select|<textarea|删除|内容格式|frozen evidence|future_field/)
    assert.match(await render({inputs:{logical_values:{}},parameters:{values:{}}}),/本次没有运行参数/)
    for(const broken of [{},{inputs:{logical_values:[]},parameters:{values:null}}]){
      const invalid=await render(broken);assert.match(invalid,/摘要数据不完整/);assert.match(invalid,/数据缺失或格式不完整/);assert.doesNotMatch(invalid,/0 项输入|本次没有运行参数/)
    }
    assert.equal(JSON.stringify(value),before)
  }finally{await vite.close()}
})

test('workbench shows server review explanation and read-only facts before continuation without authorizing execution',async()=>{
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try{
    const {adaptExecutionPreparationDetail:adapt}=await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const {executionPreparationApi:api}=await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const {executionStorageApi:storage}=await vite.ssrLoadModule('/src/services/executionStorageMetrics.ts')
    let byteReads=0;storage.get=async()=>{byteReads++;throw new Error('collapsed metadata must not load')}
    const Panel=(await vite.ssrLoadModule('/src/components/ExecutionPreparationWorkbench.vue')).default
    let writes=0
    let data:any={...preparation,field_slots:[],field_selections:[],resolved_inputs:{inputs:{logical_values:{idea:'preserved'}},parameters:{values:{seed:0}}}}
    api.get=async()=>adapt(data);api.continue=async()=>{writes++;throw new Error('read must not execute')}
    const Loaded={...Panel,async setup(props:any,context:any){const state=Panel.setup(props,context);await Promise.resolve();await nextTick();return state}}
    const render=()=>renderToString(createSSRApp({render:()=>h(Loaded,{preparationId:'prep',versionId:'version',purpose:'production'})}))
    let html=await render();assert.match(html,/人工确认字段/);assert.match(html,/必须明确确认与采用/)
    assert.match(html,/查看准备字节明细/);assert.equal(byteReads,0)
    assert.match(html,/查看完整审核规则（技术信息）/);assert.match(html,/本次无需额外填写或复核策略字段/)
    assert.ok(html.indexOf('输入与运行参数')<html.indexOf('检查并继续此任务'))
    assert.match(html,/<button[^>]*disabled[^>]*>检查并继续此任务/)
    assert.doesNotMatch(html,/内容格式|schema_version|添加属性|<select|<textarea|readonly-data[^>]*open/)
    data={...data,review_contract:{schema_version:1,mode:'policy',policy:{key:'current-exact'}},review_summary:{mode:'policy',label:'服务端审核名称',description:'服务端审核说明'}}
    html=await render();assert.match(html,/服务端审核名称/);assert.match(html,/服务端审核说明/);assert.doesNotMatch(html,/人工确认字段/)
    assert.equal(writes,0)
  }finally{await vite.close()}
})

test('evidence revision refresh updates preparation without clobbering unsaved fields or authorizing calls', async () => {
  const vite = await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  let app: any
  try {
    const { adaptExecutionPreparationDetail: adapt } = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const { executionPreparationApi: api } = await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const { default: Panel } = await vite.ssrLoadModule('/src/components/ExecutionPreparationWorkbench.vue')
    let reads=0, state: any, revision=3
    api.get=async()=>{reads++;return adapt({...preparation,revision})}
    api.continue=()=>{throw new Error('Read must not advance')}
    const props=reactive({preparationId:'prep',versionId:'version',purpose:'production',refreshToken:'initial'})
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    app=renderer.createApp({setup(){state=Panel.setup(props,{emit(){},expose(){}});return()=>h('div')}})
    app.provide(ssrContextKey,{modules:new Set()});app.mount({});await nextTick()
    assert.equal(reads,1);state.text.value='unsaved draft';revision=4;props.refreshToken='changed';await nextTick()
    assert.equal(reads,1);assert.equal(state.text.value,'unsaved draft')
    state.text.value='';await nextTick();await nextTick()
    assert.equal(reads,2);assert.equal(state.preparation.value.revision,4)
    assert.equal(state.continueAcknowledged.value,false)
    state.smokeDirty.value=true;props.refreshToken='next';revision=5;await nextTick();assert.equal(reads,2)
    state.smokeDirty.value=false;await nextTick();await nextTick();assert.equal(state.preparation.value.revision,5)
  } finally {app?.unmount();await vite.close()}
})

test('primary response panel distinguishes saved success, rejected output and explicit human recovery without writes', async () => {
  const vite = await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try {
    const {adaptExecutionPreparationDetail:adapt} = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const {executionPreparationApi:api} = await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const Panel = (await vite.ssrLoadModule('/src/components/ExecutionPreparationWorkbench.vue')).default
    const data = adapt({...preparation,field_slots:[],field_selections:[],presentation:mainPresentation})
    data.textResponseReview = {id:'review',attemptId:'attempt',originalText:'<script>original model body</script>',
      originalSha256:'b'.repeat(64),editable:false,confirmedAt:null,fields:[],notices:[],
      readonlyReason:'模型原始返回只读保留；候选采用在对应步骤处理。',
      diagnostics:{contract_error_code:'response_candidate_recorded',summary:'已保存模型原始返回与解析后的候选'}}
    api.get = async()=>structuredClone(data)
    let writes=0
    api.completeTextResponse=api.continue=async()=>{writes++;throw Error('view must not mutate')}
    const Loaded = {...Panel,async setup(props:any,context:any){
      const state=Panel.setup(props,context);await Promise.resolve();await nextTick();return state
    }}
    const render=()=>renderToString(createSSRApp({render:()=>h(Loaded,{preparationId:'prep',versionId:'version',purpose:'production'})}))
    const success=await render()
    assert.match(success,/模型返回与处理记录/);assert.match(success,/已保存模型原始返回与解析后的候选/)
    assert.match(success,/&lt;script&gt;original model body&lt;\/script&gt;/)
    assert.doesNotMatch(success,/文本输出待人工处理|可补全本次结构化输出|校验并保存候选|<script>/)
    data.textResponseReview.editable=true;data.textResponseReview.readonlyReason=''
    data.textResponseReview.diagnostics={contract_error_code:'response_contract_invalid',summary:'格式未通过，原文保留；请核对并人工修正。',validation_code:'response_json_invalid'}
    const rejected=await render()
    assert.match(rejected,/格式未通过/);assert.match(rejected,/校验并保存候选/)
    data.textResponseReview.editable=false;data.textResponseReview.confirmedAt='2026-09-28T05:10:00Z'
    const recovered=await render()
    assert.match(recovered,/保存人工处理结果，原响应仍保留/);assert.match(recovered,/原始模型响应的历史校验记录/)
    assert.doesNotMatch(recovered,/校验并保存候选|role="alert"/)
    assert.equal(writes,0)
  } finally {await vite.close()}
})

test('resource confirmation is explicit and never dispatches; incomplete response stays editable after rejected completion', async () => {
  const vite = await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  let app: any
  try {
    const {adaptExecutionPreparationDetail:adapt} = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const {executionPreparationApi:api} = await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const Panel = (await vite.ssrLoadModule('/src/components/ExecutionPreparationWorkbench.vue')).default
    const data = adapt({...preparation,field_slots:[],field_selections:[]})
    data.resourceReview = {fingerprint:'a'.repeat(64),policyCurrent:true,issues:[{code:'budget_exceeded',message:'超出提醒值'}],
      projectedCalls:2,projectedTotal:'2',currency:'USD',confirmed:false,confirmedAt:null}
    data.textResponseReview = {id:'review',attemptId:'attempt',originalText:'incomplete',originalSha256:'b'.repeat(64),editable:true,confirmedAt:null,diagnostics:{}}
    let state:any, confirmations=0, completions=0, calls=0
    api.get = async()=>structuredClone(data)
    api.confirmResources = async(id:string,revision:number,fingerprint:string)=>{
      confirmations++;assert.equal(id,'prep');assert.equal(revision,3);assert.equal(fingerprint,'a'.repeat(64))
      return {...structuredClone(data),revision:4,resourceReview:{...data.resourceReview,confirmed:true}}
    }
    api.completeTextResponse = async()=>{completions++;throw new Error('完整正文仍未通过校验')}
    api.continue = async()=>{calls++;throw new Error('must not dispatch')}
    const props=reactive({preparationId:'prep',versionId:'version',purpose:'production'})
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    app=renderer.createApp({setup(){state=Panel.setup(props,{emit(){},expose(){}});return()=>h('div')}})
    app.provide(ssrContextKey,{modules:new Set()});app.mount({});await nextTick()
    await state.confirmResources();assert.equal(confirmations,0)
    state.resourceAcknowledged.value=true;await state.confirmResources()
    assert.equal(confirmations,1);assert.equal(calls,0);assert.equal(state.continueAcknowledged.value,false)
    assert.match(state.notice.value,/已记录本次资源确认/)
    await state.primaryRetried()
    assert.equal(state.notice.value,'', 'a handled retry must retire the earlier resource-confirmation instruction')
    state.recoveryText.value='my corrected draft';await state.completeResponse();assert.equal(completions,0)
    state.recoveryAcknowledged.value=true;await state.completeResponse()
    assert.equal(completions,1);assert.equal(state.recoveryText.value,'my corrected draft')
    assert.equal(state.busy.value,false);assert.equal(state.needsRefresh.value,false)
    assert.match(state.error.value,/未通过校验/);assert.equal(calls,0)
    let resolve:any
    api.completeTextResponse=()=>new Promise(done=>{resolve=done})
    const pending=state.completeResponse()
    props.preparationId='other';api.get=async()=>({...structuredClone(data),id:'other'})
    await nextTick();resolve({...structuredClone(data),revision:5});await pending
    assert.equal(state.preparation.value.id,'other');assert.equal(state.recoveryText.value,'')
  } finally {app?.unmount();await vite.close()}
})

test('user foreign keys follow integer API identity, not fixture-only strings', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    assert.equal(m.adaptExecutionPreparation(preparation).createdBy, 1)
    assert.equal(m.adaptExecutionFieldSelection(preparation.field_selections[0]).selectedBy, 1)
    assert.equal(m.adaptExecutionField(field).reviewedBy, 1)
    assert.equal(m.adaptExecutionField({ ...field, reviewed_by: null }).reviewedBy, null)
    for (const value of ['1', 'owner', true, 0, -1, 1.5, null, undefined]) {
      assert.throws(() => m.adaptExecutionPreparation({ ...preparation, created_by: value }), /created_by/)
      assert.throws(() => m.adaptExecutionFieldSelection({ ...preparation.field_selections[0], selected_by: value }), /selected_by/)
    }
  } finally { await vite.close() }
})

test('production cancellation preserves explicit permission, revision and recorded stop facts', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const raw = { ...preparation, stage_run: 'stage', asset_job: null,
      cancellation: { allowed: true, reason: '停止此主准备及辅助', cancelled_at: null, cancelled_by: null } }
    assert.equal(m.adaptExecutionPreparationDetail(raw).cancellation.allowed, true)
    for (const cancellation of [undefined, {}, { ...raw.cancellation, allowed: 'true' }]) {
      assert.throws(() => m.adaptExecutionPreparationDetail({ ...raw, cancellation }))
    }
    assert.throws(() => m.adaptExecutionPreparationDetail({ ...raw, status: 'cancelled' }))
    const stopped = { ...raw, status: 'cancelled', revision: 4,
      cancellation: { allowed: false, reason: '准备已停止；远端状态仍待确认', cancelled_at: '2026-09-13T10:00:00Z', cancelled_by: 'owner' } }
    const { api } = await vite.ssrLoadModule('/src/services/api.ts')
    const { executionPreparationApi } = await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const calls: unknown[] = []
    api.post = async (...args: unknown[]) => { calls.push(args); return { data: stopped } }
    const data = await executionPreparationApi.cancel('prep', 3)
    assert.deepEqual(calls, [['/execution-preparations/prep/cancel/', { expected_preparation_revision: 3 }]])
    assert.equal(data.cancellation.cancelledBy, 'owner')
    assert.equal(data.cancellation.cancelledAt, stopped.cancellation.cancelled_at)
  } finally { await vite.close() }
})

test('mounted cancellation requires confirmation and clean edits; errors and old responses cannot report success', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  const previousWindow = globalThis.window
  let app: any
  try {
    const m = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const { executionPreparationApi: api } = await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const { default: Panel } = await vite.ssrLoadModule('/src/components/ExecutionPreparationWorkbench.vue')
    const data = m.adaptExecutionPreparationDetail({ ...preparation, stage_run: 'stage', asset_job: null,
      cancellation: { allowed: true, reason: '停止此准备', cancelled_at: null, cancelled_by: null } })
    let writes = 0, state: any, resolve: any
    globalThis.window = { confirm() { throw new Error('Cancellation must not open a browser-blocking dialog') } } as any
    api.get = async (id: string) => ({ ...data, id })
    api.cancel = async () => { writes++; throw new Error('准备修订冲突') }
    const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
    const props = reactive({ preparationId: 'prep', versionId: 'version', purpose: 'production' })
    app = renderer.createApp({ setup() { state = Panel.setup(props, { emit() {}, expose() {} }); return () => h('div') } })
    app.provide(ssrContextKey, { modules: new Set() }); app.mount({})
    await nextTick(); await state.load()
    assert.equal(writes, 0)
    state.text.value = 'unsaved'; state.requestCancellation(); assert.equal(state.pendingCancellation.value, null)
    state.text.value = ''; await state.cancelPreparation(); assert.equal(writes, 0)
    state.requestCancellation(); assert.deepEqual(state.pendingCancellation.value, { id: 'prep', revision: 3 }); assert.equal(writes, 0)
    state.pendingCancellation.value = null; await state.cancelPreparation(); assert.equal(writes, 0)
    state.requestCancellation(); state.preparation.value.revision = 4; await state.cancelPreparation()
    assert.equal(writes, 0); assert.match(state.error.value, /准备已变化/)
    await state.load(); state.requestCancellation(); await state.cancelPreparation()
    assert.equal(writes, 1); assert.match(state.error.value, /准备修订冲突/)
    assert.equal(state.needsRefresh.value, true); assert.equal(state.notice.value, '')
    await state.cancelPreparation(); assert.equal(writes, 1)
    await state.load()
    api.cancel = () => { writes++; return new Promise(r => { resolve = r }) }
    state.requestCancellation()
    const pending = state.cancelPreparation()
    props.preparationId = 'another'; await nextTick(); await state.load()
    resolve({ ...data, status: 'cancelled', cancellation: { ...data.cancellation, allowed: false } }); await pending
    assert.equal(state.preparation.value.id, 'another')
    assert.equal(state.preparation.value.status, 'preparing')
    assert.equal(state.notice.value, ''); assert.equal(state.busy.value, false)
  } finally { app?.unmount(); globalThis.window = previousWindow; await vite.close() }
})

test('response diagnostics preserve backend explanations and reject missing or misleading facts', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    assert.equal(m.adaptExecutionPreparationDetail(preparation).responseDiagnostic, null)
    const raw = { state: 'recorded', task_id: 'original-task', observed_at: '2026-09-12T10:00:00Z',
      summary: '来自服务端的具体诊断', details: [{ label: '输出 token', value: '0' }] }
    const diagnostic = m.adaptExecutionPreparationDetail({ ...preparation, response_diagnostic: raw }).responseDiagnostic
    assert.equal(diagnostic.taskId, 'original-task')
    assert.equal(diagnostic.summary, raw.summary)
    assert.equal(diagnostic.details[0].value, '0')
    assert.equal(m.adaptExecutionResponseDiagnostic({ ...raw, state: 'unverifiable', observed_at: null, details: [] }).state, 'unverifiable')
    for (const broken of [undefined, { ...raw, state: 'passed' }, { ...raw, task_id: null },
      { ...raw, observed_at: null }, { ...raw, details: null }, { ...raw, state: 'unverifiable' },
      { ...raw, details: [{ label: 'token', value: 0 }] }]) {
      assert.throws(() => m.adaptExecutionPreparationDetail({ ...preparation, response_diagnostic: broken }), /格式错误/)
    }
  } finally { await vite.close() }
})

test('preparation workbench renders original response diagnosis without retrying the task', async () => {
  const vite = await createServer({ root, envFile: false, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false, ws: false, watch: null } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const { executionPreparationApi: api } = await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const { default: Panel } = await vite.ssrLoadModule('/src/components/ExecutionPreparationWorkbench.vue')
    const raw = { state: 'recorded', task_id: 'own-original-task', observed_at: '2026-09-12T10:00:00Z',
      summary: '服务端确认输出额度用尽，未取得正文。', details: [{ label: '输出 token', value: '0' }] }
    let gets = 0, writes = 0
    let stageRun: string | null = 'stage', assetJob: string | null = null
    api.get = async () => { gets++; return m.adaptExecutionPreparationDetail({ ...preparation, stage_run: stageRun, asset_job: assetJob,
      cancellation: stageRun ? { allowed: false, reason: '隔离已提交请求', cancelled_at: null, cancelled_by: null } : null, response_diagnostic: raw }) }
    api.continue = async () => { writes++; throw new Error('read must not execute') }
    // SSR awaits the actual component's read-only load. No alternate display
    // implementation; this checks its template, state and API call together.
    const LoadedPanel = { ...Panel, async setup(props: any, context: any) {
      const state = Panel.setup(props, context)
      await Promise.resolve(); await nextTick()
      return state
    } }
    const router = createRouter({ history: createMemoryHistory(), routes: [{ name: 'workflows', path: '/workflows', component: { render: () => null } }] })
    await router.push('/workflows'); await router.isReady()
    const render = () => renderToString(createSSRApp({ render: () => h(LoadedPanel,
      { preparationId: 'prep', versionId: 'version', purpose: 'production', allowSmoke: false }) }).use(router))
    const html = await render()
    assert.match(html, /服务端确认输出额度用尽/)
    assert.match(html, /own-original-task/)
    assert.match(html, /输出 token/)
    assert.match(html, /href="\/workflows\?kind=preparation&amp;id=prep&amp;attempt_id=own-original-task"/)
    assert.doesNotMatch(html, /自动重跑已启动/)
    raw.state = 'unverifiable'; raw.observed_at = null as any; raw.details = []
    raw.summary = '诊断证据无法核验，不自动重跑。'
    const invalid = await render()
    assert.match(invalid, /诊断不可核验/)
    assert.match(invalid, /诊断证据无法核验/)
    assert.doesNotMatch(invalid, /输出 token/)
    assert.match(invalid, /attempt_id=own-original-task/)
    stageRun = null; assetJob = 'different-task'
    const mismatched = await render()
    assert.match(mismatched, /诊断任务关联未核验/)
    assert.doesNotMatch(mismatched, /查看这次调用的请求/)
    assetJob = raw.task_id
    const direct = await render()
    assert.match(direct, /href="\/workflows\?kind=preparation&amp;id=prep"/)
    assert.doesNotMatch(direct, /attempt_id=/)
    assert.equal(gets, 4)
    assert.equal(writes, 0)
  } finally { await vite.close() }
})

test('stage list preserves pagination, identities and owner scope without interpreting a detail contract', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const row = { ...preparation, stage_run: 'stage', asset_job: null, presentation: mainPresentation }
    const page = { count: 2, next: '/api/v1/execution-preparations/?page=2', previous: null, results: [row] }
    const result = m.adaptStagePreparationPage(page, 'stage')
    assert.equal(result.count, 2)
    assert.equal(result.results[0].id, 'prep')
    assert.equal(result.results[0].stageRun, 'stage')
    assert.equal(Object.hasOwn(result.results[0], 'intent'), false)
    assert.throws(() => m.adaptStagePreparationPage(page, 'other-stage'))
    assert.throws(() => m.adaptStagePreparationPage({ ...page, results: [preparation] }, 'stage'))
    assert.throws(() => m.adaptStagePreparationPage({ ...page, results: [row, row] }, 'stage'))
    assert.throws(() => m.adaptStagePreparationPage({ ...page, count: null }, 'stage'))
    const apiSource = await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    assert.equal(typeof apiSource.executionPreparationApi.forStage, 'function')
  } finally { await vite.close() }
})

test('auxiliary batches preserve real status, separate identities and explicit retry permission', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const batch = { field_batch_key: 'a'.repeat(64), purpose: 'translate_fields', field_keys: ['direction'],
      attempt_number: 1, task_id: 'attempt', preparation_id: 'leaf-prep', status: 'failed', reason: '明确失败', can_retry: true, requires_uncertain_acknowledgement: false }
    const result = m.adaptExecutionPreparationDetail({ ...preparation, auxiliary_batches: [batch] })
    assert.equal(result.auxiliaryBatches[0].preparationId, 'leaf-prep')
    assert.equal(result.auxiliaryBatches[0].canRetry, true)
    assert.equal(result.id, 'prep')
    for (const broken of [{ auxiliary_batches: undefined }, { auxiliary_batches: [{ ...batch, can_retry: 'yes' }] },
      { auxiliary_batches: [{ ...batch, purpose: 'implicit_model' }] }]) {
      assert.throws(() => m.adaptExecutionPreparationDetail({ ...preparation, ...broken }))
    }
  } finally { await vite.close() }
})

test('Smoke reuse exports exact choice and never inherits earlier authorization or chooses newer fields', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const raw = { execution_instance_version_id: 'version', expected_definition_sha256: 'a'.repeat(64),
      reuse_preparation_id: 'prep', expected_reuse_revision: 3,
      fixture: { inputs: { brief: '  Original.\n' }, parameters: { seed: 0 }, manual_channels: {}, subject_facts: [], source_language: '', target_language: 'en' },
      field_selections: [{ source_preparation_id: 'prep', field_value_id: 'field-1', unit_key: 'unit', field_key: 'direction' }] }
    const source = m.adaptExecutionSmokeReuseFixture(raw)
    const request = m.executionReuseSmokeRequest(source, false)
    assert.deepEqual(request.fixture, raw.fixture)
    assert.deepEqual(request.reuse.fields, raw.field_selections)
    assert.deepEqual(request.fieldValues, [])
    assert.equal(request.allowExternalCalls, false)
    assert.equal('budgetLimit' in request, false)
    assert.equal('budget_limit' in m.executionSmokePayload(request), false)
    for (const changes of [{ expected_reuse_revision: '3' }, { expected_definition_sha256: 'guessed' },
      { field_selections: [raw.field_selections[0], raw.field_selections[0]] },
      { fixture: { ...raw.fixture, allow_external_calls: true } },
      { fixture: { ...raw.fixture, manual_channels: { prompt: false } } }]) {
      assert.throws(() => m.adaptExecutionSmokeReuseFixture({ ...raw, ...changes }), /格式错误/)
    }
  } finally { await vite.close() }
})

test('Smoke reuse preserves the exact optional appearance declaration through the submitted fixture', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const fixture = { inputs: { references: { kind: 'media', items: [{ item_key: 'primary', asset_id: 'asset' }] },
      'request.subject': { kind: 'literal', value: '  Preserve identity. Change outfit.\n' } },
      parameters: { seed: 0 }, manual_channels: {}, subject_facts: [], source_language: '', target_language: 'en' }
    const raw = { execution_instance_version_id: 'version', expected_definition_sha256: 'a'.repeat(64),
      reuse_preparation_id: 'prep', expected_reuse_revision: 3, fixture, field_selections: [] }
    const primary = { role: 'primary', setting_revision_id: null, primary_version_id: null, primary_input: null }
    const variant = { role: 'variant', setting_revision_id: null,
      primary_version_id: '10000000-0000-0000-0000-000000000001',
      primary_input: { input_key: 'references', item_key: 'primary' },
      difference_input: { kind: 'logical_input', key: 'request.subject' } }
    for (const declaration of [primary, variant, { ...variant, difference_input: {
      kind: 'reference_field', key: 'units', field_key: 'direction', unit_key: 'original-unit' } }, null]) {
      const input = { ...raw, fixture: { ...fixture, appearance_target: declaration } }
      const original = JSON.stringify(input)
      const source = m.adaptExecutionSmokeReuseFixture(input)
      const submitted = m.executionSmokePayload(m.executionReuseSmokeRequest(source, false))
      assert.deepEqual(submitted.fixture, input.fixture)
      assert.equal(JSON.stringify(input), original)
      assert.equal(submitted.allow_external_calls, false)
      assert.deepEqual(submitted.field_values, [])
    }
    const withoutAppearance = m.executionSmokePayload(m.executionReuseSmokeRequest(m.adaptExecutionSmokeReuseFixture(raw), false))
    assert.equal('appearance_target' in withoutAppearance.fixture, false)
    for (const appearance_target of [undefined, false, 'variant', []]) {
      assert.throws(() => m.adaptExecutionSmokeReuseFixture({ ...raw, fixture: { ...fixture, appearance_target } }), /格式错误/)
    }
    assert.throws(() => m.adaptExecutionSmokeReuseFixture({ ...raw,
      fixture: { ...fixture, appearance_target: variant, appearance: variant } }), /格式错误/)
  } finally { await vite.close() }
})

test('preparation and field responses preserve explicit choice and distinguish missing detail', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    assert.deepEqual(m.executionPreparationContinuePayload(3), { expected_preparation_revision: 3 })
    const smokePreparation = { ...preparation, purpose: 'instance_smoke', deployment_smoke: 'smoke-attempt', asset_job: null }
    assert.equal(m.adaptExecutionSmokeCreated({ attempt_kind: 'deployment_smoke', attempt_id: 'smoke-attempt', status: 'preparing', preparation: smokePreparation }).status, 'preparing')
    assert.throws(() => m.adaptExecutionSmokeCreated({ attempt_kind: 'deployment_smoke', attempt_id: 'other', status: 'succeeded', preparation: smokePreparation }), /关联不一致/)
    for (const value of [0, -1, true, '3', NaN, 1.5]) {
      assert.throws(() => m.executionPreparationContinuePayload(value), /revision/)
    }
    assert.equal(m.adaptExecutionPreparationDetail(preparation).fieldSelections[0].fieldValue, 'field-1')
    assert.equal(m.adaptExecutionPreparationDetail(preparation).preparedRequest, null)
    assert.deepEqual(m.adaptExecutionPreparationDetail(preparation).reviewSummary,
      { mode: 'manual', label: '人工确认字段', description: '必须明确确认与采用' })
    const automatic = { ...preparation, review_contract: { schema_version: 1, mode: 'policy', policy: { key: 'frozen-policy' } },
      review_summary: { mode: 'policy', label: '服务端批准策略名称', description: '服务端策略说明' } }
    assert.equal(m.adaptExecutionPreparationDetail(automatic).reviewSummary.label, '服务端批准策略名称')
    for (const changes of [{ review_summary: undefined }, { review_summary: { ...preparation.review_summary, mode: 'policy' } },
      { review_summary: { mode: 'manual', label: 'missing description' } }]) {
      assert.throws(() => m.adaptExecutionPreparationDetail({ ...preparation, ...changes }), /格式错误/)
    }
    assert.equal(m.adaptExecutionField(field).approvalLabel, '人工审核')
    const sample = { id: 'sample', observed_at: 'observed', elapsed_ms: 123, dimensions_complete: false,
      signature_sha256: null, known_dimensions: { reference_count: 2 }, missing_dimensions: ['text.input_tokens_and_tokenizer'] }
    const observed = m.adaptExecutionPreparationDetail({ ...preparation, capacity_sample: sample }).capacitySample
    assert.equal(observed.dimensionsComplete, false)
    assert.equal(observed.elapsedMs, 123)
    assert.equal(observed.signatureSha256, null)
    for (const changes of [{ elapsed_ms: -1 }, { elapsed_ms: NaN }, { dimensions_complete: true },
      { missing_dimensions: [] }, { missing_dimensions: [''] }, { signature_sha256: 'guessed' }]) {
      assert.throws(() => m.adaptExecutionCapacitySample({ ...sample, ...changes }), /格式错误/)
    }
    assert.equal(m.adaptExecutionFieldDetail(field).text, field.text)
    assert.equal('text' in m.adaptExecutionField(field), false)
    const result = m.adaptExecutionFieldActionResult({ field, preparation, selection: null })
    assert.equal(result.field.status, 'confirmed')
    assert.equal(result.selection, null)
    assert.equal(m.adaptExecutionPreparation({ ...preparation, status: 'new_server_state' }).status, 'new_server_state')
    for (const key of ['revision', 'status', 'field_selections', 'prepared_request', 'capacity_sample', 'field_slots']) {
      const broken = { ...preparation }
      delete broken[key]
      assert.throws(() => m.adaptExecutionPreparationDetail(broken), /格式错误/)
    }
    for (const value of [0, true, '1', NaN]) {
      assert.throws(() => m.adaptExecutionField({ ...field, revision: value }), /revision/)
    }
    const withoutText = { ...field }
    delete withoutText.text
    assert.throws(() => m.adaptExecutionFieldDetail(withoutText), /text/)
  } finally { await vite.close() }
})

test('field workbench uses server revision and explicit replacement, never the latest row', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const prep = m.adaptExecutionPreparationDetail(preparation), current = m.adaptExecutionFieldDetail(field)
    assert.equal(prep.fieldSlots[0].nextRevision, 2)
    assert.equal(prep.fieldSlots[0].targetLanguage, 'en')
    const newer = { ...current, id: 'field-2', revision: 2 }
    assert.throws(() => m.preparationFieldConfirmInput(prep, newer, true, false), /替换/)
    assert.deepEqual(m.preparationFieldConfirmInput(prep, newer, true, true), {
      expectedTextSha256: 'text-sha', expectedSourceSha256: 'source-sha', expectedPreparationRevision: 3,
      selectForExecution: true, replace: true, expectedSelectionRevision: 1,
    })
    assert.equal(m.preparationFieldConfirmInput(prep, newer, false, false).expectedSelectionRevision, null)
    assert.equal(m.preparationFieldConfirmInput(prep, current, true, false).replace, false)
    for (const changes of [{ requestFrozenAt: 'frozen' }, { status: 'cancelled' }, { id: 'another-prep' }]) {
      assert.throws(() => m.preparationFieldConfirmInput({ ...prep, ...changes }, current, true, false), /当前准备/)
    }
    assert.throws(() => m.adaptExecutionPreparationFieldSlot({ ...preparation.field_slots[0], next_revision: 0 }), /格式错误/)
    assert.throws(() => m.adaptExecutionPreparationFieldSlot({ ...preparation.field_slots[0], field_key: 'other' }), /格式错误/)
  } finally { await vite.close() }
})

test('field request payloads cannot send origin, approval or implicit selection', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  try {
    const m = await vite.ssrLoadModule('/src/services/executionPreparation.ts')
    const request = { preparationId: 'prep', unitKey: 'unit', fieldKey: 'direction', text: field.text, revision: 1,
      expectedPreparationRevision: 3, originKind: 'model', status: 'confirmed' }
    assert.deepEqual(m.executionFieldCreatePayload(request), { preparation_id: 'prep', unit_key: 'unit', field_key: 'direction',
      text: field.text, revision: 1, expected_preparation_revision: 3 })
    const review = { expectedTextSha256: 'text-sha', expectedSourceSha256: 'source-sha', expectedPreparationRevision: 3,
      selectForExecution: false, replace: false, expectedSelectionRevision: null, approvalKind: 'policy' }
    assert.deepEqual(m.executionFieldConfirmPayload(review), { expected_text_sha256: 'text-sha', expected_source_sha256: 'source-sha',
      expected_preparation_revision: 3, select_for_execution: false, replace: false, expected_selection_revision: null })
    assert.equal('select_for_execution' in m.executionFieldReviewPayload(review), false)
  } finally { await vite.close() }
})

test('review opens saved text read-only, respects adopted revision, and makes editing an explicit new draft', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  let ctx: any
  try {
    ctx = await recoveryWorkbench(vite)
    const { state, api, viewed } = ctx
    await nextTick()
    assert.equal(state.field.value.text, field.text)
    assert.equal(state.editing.value, false)
    assert.equal(state.text.value, '')
    assert.equal(state.historyOpen.value, false)
    assert.equal(state.languageLabel('en'), '英语')
    assert.equal(state.languageLabel('any'), '不限语言')
    assert.equal(state.languageLabel('bilingual:zh-CN+en'), `${new Intl.DisplayNames(['zh-CN'], { type: 'language' }).of('zh-CN')} + 英语`)
    const reads: string[] = []; let writes = 0
    api.field = async (id: string) => { reads.push(id); return { ...viewed, id, status: 'pending' } }
    api.confirmField = api.continue = async () => { writes++; throw new Error('must not submit') }
    const slot = state.preparation.value.fieldSlots[0]
    slot.latestValueId = 'newer-field'
    await state.openSlot(slot)
    assert.deepEqual(reads, ['field-1']) // adopted value wins over newer revision, display is not adoption
    state.preparation.value.fieldSelections = []
    await state.openSlot(slot)
    assert.equal(state.field.value.id, 'newer-field')
    state.editField()
    assert.equal(state.text.value, field.text)
    assert.equal(state.editing.value, true)
    await state.review('select'); await state.openSlot(slot)
    state.continueAcknowledged.value = true; await state.advance()
    assert.equal(writes, 0); assert.equal(reads.length, 2); assert.equal(state.text.value, field.text)
    state.text.value = ''; state.editing.value = false
    slot.latestValueId = null
    await state.openSlot(slot)
    assert.equal(state.field.value, null); assert.equal(reads.length, 2)
  } finally { ctx?.app.unmount(); await vite.close() }
})

test('field navigation ignores late results and foreign field bodies; failures never look like empty successful output', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  let ctx: any
  try {
    ctx = await recoveryWorkbench(vite); await nextTick()
    const { state, api, viewed } = ctx
    let finish: any
    api.field = () => new Promise(resolve => { finish = resolve })
    const older = state.openField('old')
    api.field = async id => ({ ...viewed, id })
    await state.openField('new')
    finish({ ...viewed, id: 'old' }); await older
    assert.equal(state.field.value.id, 'new')
    api.field = async id => ({ ...viewed, id, preparation: 'foreign' })
    await state.openField('foreign-field')
    assert.equal(state.field.value, null); assert.match(state.fieldError.value, /不属于/)
    api.field = async () => { throw new Error('无法读取正文') }
    await state.openField('new')
    assert.match(state.fieldError.value, /无法读取正文/); assert.equal(state.field.value, null)
    assert.equal(state.fieldLoading.value, false)
  } finally { ctx?.app.unmount(); await vite.close() }
})

test('review layout shows body before optional history and editing, and collapses generation instructions', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  let ctx: any
  try {
    ctx = await recoveryWorkbench(vite)
    const { api, data, viewed, Panel } = ctx
    api.get = async () => ({ ...structuredClone(data), fieldSelections: [], auxiliaryBatches: [],
      fieldSlots: [{ ...data.fieldSlots[0], targetLanguage: 'Chinese', effectiveLanguage: 'en',
        fieldContract: { ...data.fieldSlots[0].fieldContract, language_policy: 'en', generation_output_semantics: 'contribution' } }] })
    api.field = async () => ({ ...viewed, status: 'pending' })
    const Loaded = { ...Panel, async setup(props: any, context: any) {
      const state = Panel.setup(props, context); await Promise.resolve(); await nextTick(); await nextTick(); return state
    } }
    const html = await renderToString(createSSRApp({ render: () => h(Loaded, { preparationId: 'prep', versionId: 'version', purpose: 'production' }) }))
    assert.match(html, /审核提示词组成字段/); assert.match(html, /导演描述/)
    assert.match(html, /策略目标语言（生成指导）：英语/); assert.match(html, /不是新的剧本/)
    assert.match(html, /Exact text/); assert.match(html, /确认并采用此修订/)
    assert.doesNotMatch(html, /<textarea|填写哪个字段/)
    assert.ok(html.indexOf('Exact text') < html.indexOf('查看全部字段修订'))
    const rules = html.match(/<details([^>]*)><summary[^>]*>生成规则与来源标识/)
    assert.ok(rules); assert.doesNotMatch(rules[1], /\bopen\b/)
  } finally { ctx?.app.unmount(); await vite.close() }
})

test('preparation API uses the formal endpoints and server pagination', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  try {
    const { api } = await vite.ssrLoadModule('/src/services/api.ts')
    const { executionPreparationApi: client } = await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const calls = []
    api.get = async (url, options) => {
      calls.push({ url, options })
      return { data: url === '/execution-field-values/'
        ? { count: 1, next: null, previous: null, results: [field] } : preparation }
    }
    const detail = await client.get('prep')
    assert.equal(detail.id, 'prep')
    const page = await client.fields('prep', 2)
    assert.equal(page.results[0].id, 'field-1')
    assert.equal(page.next, null)
    assert.deepEqual(calls, [{ url: '/execution-preparations/prep/', options: undefined },
      { url: '/execution-field-values/', options: { params: { preparation_id: 'prep', page: 2 } } }])
    api.get = async (url, options) => {
      calls.push({ url, options })
      return { data: { count: 1, next: null, previous: null, results: [{ ...preparation, stage_run: 'stage', asset_job: null, presentation: mainPresentation }] } }
    }
    assert.equal((await client.forStage('stage', 1)).results[0].id, 'prep')
    assert.deepEqual(calls.at(-1), { url: '/execution-preparations/', options: { params: { stage_run_id: 'stage', page: 1 } } })
    api.post = async (url, payload) => {
      calls.push({ url, payload })
      return { data: { field, preparation, selection: null } }
    }
    const review = { expectedTextSha256: 'text-sha', expectedSourceSha256: 'source-sha', expectedPreparationRevision: 3,
      selectForExecution: false, replace: false, expectedSelectionRevision: null }
    assert.equal((await client.confirmField('field-1', review)).selection, null)
    assert.equal(calls.at(-1).url, '/execution-field-values/field-1/confirm/')
    api.post = async (url, payload) => {
      calls.push({ url, payload })
      return { data: preparation }
    }
    assert.equal((await client.continue('prep/one', 3)).id, 'prep')
    assert.deepEqual(calls.at(-1), { url: '/execution-preparations/prep%2Fone/continue/', payload: { expected_preparation_revision: 3 } })
    await client.recoverMediaArchive('prep/one', 3, {taskId:'task',attemptId:'attempt',taskUpdatedAt:'original-time',receiptSha256:'a'.repeat(64)})
    assert.deepEqual(calls.at(-1), {url:'/execution-preparations/prep%2Fone/recover-media-archive/',payload:{expected_preparation_revision:3,task_id:'task',attempt_id:'attempt',task_updated_at:'original-time',receipt_sha256:'a'.repeat(64)}})
    await client.retryUncertainMedia('prep/one',3,{taskId:'task',attemptId:'attempt',taskUpdatedAt:'original-time',requestSha256:'b'.repeat(64)})
    assert.deepEqual(calls.at(-1),{url:'/execution-preparations/prep%2Fone/retry-uncertain-media/',payload:{expected_preparation_revision:3,task_id:'task',attempt_id:'attempt',task_updated_at:'original-time',request_sha256:'b'.repeat(64),acknowledged:true}})
    assert.equal((await client.retryFieldBatch('prep/one', 3, 'a'.repeat(64), 2)).id, 'prep')
    assert.deepEqual(calls.at(-1), { url: '/execution-preparations/prep%2Fone/retry-field-batch/',
      payload: { expected_preparation_revision: 3, field_batch_key: 'a'.repeat(64), attempt_number: 2 } })
    await client.retryFieldBatch('prep/one', 3, 'a'.repeat(64), 2, 'original-uncertain')
    assert.deepEqual(calls.at(-1), { url: '/execution-preparations/prep%2Fone/retry-field-batch/',
      payload: { expected_preparation_revision: 3, field_batch_key: 'a'.repeat(64), attempt_number: 2,
        acknowledged_uncertain_task_id: 'original-uncertain' } })
    const { executionRegistryApi: registry } = await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
    api.post = async (url, payload, options) => {
      calls.push({ url, payload, options })
      return { data: { attempt_kind: 'deployment_smoke', attempt_id: 'smoke-attempt', status: 'preparing',
        preparation: { ...preparation, purpose: 'instance_smoke', deployment_smoke: 'smoke-attempt', asset_job: null } } }
    }
    const smokeInput = { expectedDefinitionSha256: 'exact-sha', fixture: { inputs: {}, parameters: { max_tokens: 128 }, manual_channels: { positive_prompt: 'Describe a blue parcel.' }, subject_facts: [], source_language: '', target_language: '' },
      fieldValues: [], allowExternalCalls: true, budgetLimit: '1', scope: 'resource', skip_checks: true }
    assert.equal((await registry.smoke('version/one', smokeInput, 'intent-key')).status, 'preparing')
    assert.equal(calls.at(-1).url, '/model-execution-instance-versions/version%2Fone/smoke-test/')
    assert.deepEqual(calls.at(-1).options, { headers: { 'Idempotency-Key': 'intent-key' } })
    assert.equal('scope' in calls.at(-1).payload, false)
    assert.equal('skip_checks' in calls.at(-1).payload, false)
    const fields = [{ source_preparation_id: 'source-prep', field_value_id: 'confirmed-field', unit_key: 'asset', field_key: 'direction' }]
    await registry.smoke('version', { ...smokeInput, reuse: { preparationId: 'source-prep', expectedRevision: 7, fields } }, 'reuse-key')
    assert.equal(calls.at(-1).payload.reuse_preparation_id, 'source-prep')
    assert.equal(calls.at(-1).payload.expected_reuse_revision, 7)
    assert.deepEqual(calls.at(-1).payload.field_selections, fields)
    api.get = async () => ({ data: { results: [] } })
    await assert.rejects(() => client.fields('prep', 1), /分页响应不完整/)
  } finally { await vite.close() }
})
