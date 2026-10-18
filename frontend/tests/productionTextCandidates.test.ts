import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createRenderer, createSSRApp, h, reactive, nextTick, ssrContextKey } from 'vue'
import { createRouter, createMemoryHistory } from 'vue-router'
import { renderToString } from 'vue/server-renderer'
import { visualRuleReviewFixture } from './visualRulesOutputFixture.ts'

const server = () => createServer({ root: fileURLToPath(new URL('..', import.meta.url)), logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
const hash = 'a'.repeat(64)
const source = { id: 'source', ordinal: 1, status: 'succeeded', stageKey: 'script_generate', outputSnapshotSha256: hash }
const stage = { id: 'review', stageKey: 'script_review', ordinal: 2, inputSnapshotSha256: hash,
  inputSnapshot: { candidate_source: { stage_id: 'source', stage_key: 'script_generate', output_snapshot_sha256: hash } } }
const artifact = (id: string) => ({ id, productionRun: 'run', stageRun: 'source', variantIndex: id === 'one' ? 1 : 2,
  status: 'available', sha256: hash, manifestMetadataSha256: hash, createdAt:'2026-09-23T01:00:00Z', generationAttempt:'attempt',
  textCandidate: { operation: 'script.generate', candidate: { title: id, logline: 'candidate story' }, text: `script ${id}`, warnings: [] } })

async function renderReview(Component: any, props: any) {
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}}]})
  await router.push('/'); await router.isReady()
  return renderToString(createSSRApp(Component,props).use(router))
}

async function withReadonlyReview(fn: (context: any) => Promise<void>, visual = false) {
  const vite = await server()
  const { platformApi: api } = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const original = {...api}, writes: any[] = [], reads: any[] = []
  const producer = {...source,stageKey:visual?'visual_rules_generate':'script_generate'}
  const review = {...stage,stageKey:visual?'visual_rules_review':'script_review'}
  const options = {runId:'run',runPlanSha256:hash,currentScript:null,currentScriptIdentity:null,
    actions:[{stageKey:review.stageKey,action:'approve',sourceStageKeys:[producer.stageKey]}]}
  let row:any = visual ? {...artifact('one'),textCandidate:{operation:'visual_rules.generate',candidate:{negative_prompt:'',
    color_script:'Original color.',continuity_summary:'Original continuity.'},warnings:[],review_contract:visualRuleReviewFixture()}} : artifact('one')
  api.listArtifactPage=async query=>{reads.push(query);return {results:[row],count:1,hasNext:false,hasPrevious:false}}
  api.getArtifact=async()=>row;api.getManualProductionActions=async()=>options
  api.approveProductionRun=api.performManualProductionAction=async(...args)=>{writes.push(args);throw Error('read-only cannot write')}
  const {default:C}=await vite.ssrLoadModule('/src/components/ProductionTextCandidateReview.vue')
  const props=reactive({run:{id:'run',project:'project',planSource:'manual',status:'succeeded',executionPlan:{sha256:hash},stages:[producer,review]},
    stage:review,currentRules:{},manualOptions:options,readOnly:false})
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  let state:any
  const app=renderer.createApp({setup(){state=C.setup(props,{emit(){},expose(){}});return()=>h('div')}})
  app.provide(ssrContextKey,{modules:new Set()});app.mount({})
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}},{path:'/workflows',name:'workflows',component:{render:()=>null}}]})
  await router.push('/');await router.isReady()
  const render=()=>renderToString(createSSRApp({...C,setup:()=>state},props).use(router))
  try { await fn({state,props,api,writes,reads,render,row,setRow:(value:any)=>{row=value}}) }
  finally {app.unmount();Object.assign(api,original);await vite.close()}
}

test('ended manual rounds browse and refresh available or selected scripts without any review controls or writes',()=>withReadonlyReview(async({state,props,writes,reads,render,row,setRow})=>{
  assert.equal(reads.length,0);assert.equal(state.manualSourceId.value,'')
  for(const status of ['succeeded','failed','cancelled']) for(const candidateStatus of ['available','selected']) {
    props.run.status=status;state.pendingRequest.value=null;setRow({...row,status:candidateStatus});await nextTick()
    state.manualSourceId.value='source';await state.load();await state.choose({...row,status:candidateStatus})
    assert.equal(state.detail.value.text,'script one');assert.equal(state.ready.value,false)
    const html=await render()
    assert.match(html,/本轮已结束，仅查看原候选/);assert.match(html,/script one/)
    assert.match(html,/source_id=attempt/);assert.doesNotMatch(html,/<footer|<textarea|<input|确认采用此剧本/)
    state.reviewed.value=true;state.decision.value='reject'
    await state.submit();state.pendingRequest.value={stage_key:'script_review'};await state.submit()
    assert.equal(writes.length,0)
    await state.refresh();assert.equal(state.pendingRequest.value,null)
    assert.equal(state.rows.value.length,1)
  }
  for(const query of reads) assert.equal(query.stage_run,'source')
}))

test('live end signal overrides a retained opening snapshot including a pending review retry',()=>withReadonlyReview(async({state,props,writes,row})=>{
  props.run.status='awaiting_input';await nextTick()
  state.manualSourceId.value='source';await state.load();await state.choose(row);state.reviewed.value=true
  assert.equal(state.ready.value,true)
  state.pendingRequest.value={original:'unsettled-request'}
  props.readOnly=true;await nextTick()
  assert.equal(props.run.status,'awaiting_input');assert.equal(state.ready.value,false)
  await state.submit();assert.equal(writes.length,0)
  assert.deepEqual(state.pendingRequest.value,{original:'unsettled-request'})
  assert.equal(state.detail.value.text,'script one')
}))

test('read-only visual results show original model fields, not editable or falsely adopted values',()=>withReadonlyReview(async({state,render,row,writes})=>{
  state.manualSourceId.value='source';await state.load();await state.choose(row)
  state.edits.value.color_script='Unsaved edit, not adopted'
  const html=await render()
  assert.match(html,/Original color/);assert.match(html,/不代表最终人工采用值/)
  assert.match(html,/输入是否允许本次编辑由精确策略决定/)
  assert.match(html,/项目事实需预览确认后冻结/)
  assert.match(html,/实际输入及来源可在原调用记录中查看/)
  assert.match(html,/aria-label="刷新影视规则候选"/)
  assert.match(html,/Frozen output/);assert.match(html,/响应 → 审核 → 下游来源/)
  assert.match(html,/reviewed_visual_rules.continuity_summary/)
  assert.doesNotMatch(html,/引用是可编辑的内容副本|重新读取当前剧本和候选/)
  assert.doesNotMatch(html,/<textarea|<input|<footer|Unsaved edit|确认采用这些规则/)
  await state.submit();assert.equal(writes.length,0)
},true))

test('candidate source and detail require precise scope, explicit manifest and complete text', async t => {
  const vite = await server(); t.after(() => vite.close())
  const m = await vite.ssrLoadModule('/src/services/productionTextCandidates.ts')
  assert.equal(m.textCandidateSource(stage, [source]).id, 'source')
  assert.throws(() => m.textCandidateSource(stage, [{ ...source, id: 'latest' }]), /来源/)
  assert.throws(() => m.textCandidateSource({ ...stage, inputSnapshot: {} }, [source]))
  assert.equal(m.readTextCandidateDetail(artifact('one'), 'run', 'source').text, 'script one')
  assert.equal(m.readTextCandidateDetail({...artifact('one'),status:'selected'},'run','source').text,'script one')
  const customFields = { story_name: '候选名称', synopsis: '候选简介', emotion: '紧张' }
  const custom = m.readTextCandidateDetail({ ...artifact('one'), textCandidate: { ...artifact('one').textCandidate, candidate: customFields } }, 'run', 'source')
  assert.deepEqual(custom.candidate, customFields)
  assert.equal(custom.text, 'script one')
  assert.equal(m.readTextCandidateDetail({ ...artifact('one'), textCandidate: { ...artifact('one').textCandidate, candidate: {} } }, 'run', 'source').text, 'script one')
  for (const value of [[{ '自定义.字段': { score: 0, flag: false, empty: null } }], [], 0, false, null, '']) {
    const parsed = m.readTextCandidateDetail({ ...artifact('one'), textCandidate: { ...artifact('one').textCandidate, candidate: value } }, 'run', 'source')
    assert.deepEqual(parsed.candidate, value)
  }
  const missing = { ...artifact('one'), textCandidate: { ...artifact('one').textCandidate } }
  delete missing.textCandidate.candidate
  assert.throws(() => m.readTextCandidateDetail(missing, 'run', 'source'), /缺少字段值/)
  for (const value of [[], null, false, 0]) {
    assert.throws(() => m.readTextCandidateDetail({ ...artifact('one'), textCandidate: {
      operation: 'visual_rules.generate', candidate: value, warnings: [], review_contract: visualRuleReviewFixture(),
    } }, 'run', 'source'), /完整对象/)
  }
  for (const value of [{ ...artifact('one'), status: 'rejected' }, { ...artifact('one'), productionRun: 'foreign' },
    { ...artifact('one'), manifestMetadataSha256: '' }, { ...artifact('one'), manifestMetadataSha256: 'not-a-hash' },
    { ...artifact('one'), textCandidate: null }, { ...artifact('one'), textCandidate: { ...artifact('one').textCandidate, text: '' } }]) {
    assert.throws(() => m.readTextCandidateDetail(value, 'run', 'source'))
  }
})

test('script review accepts a list root without dropping values or enabling read-only adoption', () => withReadonlyReview(async ({ state, writes, render, row, setRow }) => {
  const candidate = [{ '自定义.字段': { score: 0, flag: false, empty: null } }]
  const value = { ...row, textCandidate: { ...row.textCandidate, candidate, text: JSON.stringify(candidate), warnings: ['[0].额外字段：已保留'] } }
  setRow(value); state.manualSourceId.value = 'source'; await state.load(); await state.choose(value)
  assert.deepEqual(state.detail.value.candidate, candidate)
  assert.equal(state.error.value, '')
  const html = await render()
  assert.match(html, /列表 · 1 项/)
  assert.match(html, /自定义\.字段/)
  assert.match(html, /score.*0/)
  assert.match(html, /flag.*false/)
  assert.match(html, /empty.*null/)
  assert.match(html, /额外字段：已保留/)
  await state.submit(); assert.equal(writes.length, 0)
}))

test('review component loads one page, rejects stale detail, and submits an explicit candidate only after review', async t => {
  const vite = await server(); t.after(() => vite.close())
  const { platformApi } = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const originals = { listArtifactPage: platformApi.listArtifactPage, getArtifact: platformApi.getArtifact, approveProductionRun: platformApi.approveProductionRun }
  t.after(() => Object.assign(platformApi, originals))
  const queries: any[] = [], pending: any[] = [], submitted: any[] = []
  platformApi.listArtifactPage = async params => { queries.push(params); return { results: [artifact('one'), artifact('two')], count: 2, hasNext: false, hasPrevious: false } }
  platformApi.getArtifact = id => new Promise(resolve => pending.push({ id, resolve }))
  platformApi.approveProductionRun = async (id, input) => { submitted.push({ id, input }); return {} }
  const { default: Component } = await vite.ssrLoadModule('/src/components/ProductionTextCandidateReview.vue')
  const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
  const props = reactive({ run: { id: 'run', stages: [source, stage] }, stage, currentRules: {} })
  let state: any
  const events: any[] = []
  const app = renderer.createApp({ setup() { state = Component.setup(props, { expose() {}, emit: (...args) => events.push(args) }); return () => h('div') } })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); t.after(() => app.unmount())
  await Promise.resolve(); await nextTick()
  assert.deepEqual(queries, [{ production_run: 'run', stage_run: 'source', page: 1 }])
  assert.equal(pending.length, 0); assert.equal(state.selected.value, null); assert.equal(state.ready.value, false)
  const first = state.choose(artifact('one')), second = state.choose(artifact('two'))
  pending[1].resolve(artifact('two')); await second
  pending[0].resolve(artifact('one')); await first
  assert.equal(state.selected.value.id, 'two')
  const html = await renderToString(createSSRApp({ ...Component, setup: () => state }, props).use(createRouter({ history: createMemoryHistory(), routes: [{ path: '/workflows', name: 'workflows', component: { render: () => null } }] })))
  assert.match(html, /保留项目原标题与梗概/)
  assert.doesNotMatch(html, /同时采用候选标题|adoptMetadata/)
  await state.submit(); assert.equal(submitted.length, 0)
  state.reviewed.value = true
  await state.submit()
  assert.deepEqual(submitted[0].input.textCandidate, { artifactId: 'two', expectedSha256: hash })
  assert.equal(submitted[0].input.expectedSourceSnapshotSha256, hash)
  assert.equal('adoptGeneratedMetadata' in submitted[0].input, false)
  assert.equal(state.selected.value, null)
  assert.equal(events.filter(row => row[0] === 'changed').length, 1)
  const selected = {...artifact('two'),status:'selected'}
  const selectedRead = state.choose(selected)
  pending[2].resolve(selected); await selectedRead
  assert.equal(state.selected.value.id,'two')
  assert.equal(state.detail.value.text,'script two')
  state.reviewed.value = true
  assert.equal(state.ready.value,false)
  await state.submit(); assert.equal(submitted.length,1)
  state.decision.value='reject';await state.submit();assert.equal(submitted.length,1)
})

test('manual review explicitly chooses a source and retries the same command without recipe approval or automatic continuation', async t => {
  const vite = await server(); t.after(() => vite.close())
  const { platformApi } = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const { adaptManualActionOptions } = await vite.ssrLoadModule('/src/services/productionManual.ts')
  const options = adaptManualActionOptions({ run_id: 'run', run_plan_sha256: hash,
    execution_activity:[],content_change_stage_keys:[],script_adoption: { revision: 0, decision_id: null, content: null }, script_review: { revision: 0, decision_id: null, content: null }, visual_rules_review:{revision:0,decision_id:null,status:'not_reviewed',reasons:[]},current_script:null,current_script_identity:null,current_script_decision_id:null,
    actions: [{ stage_key: 'script_review', action: 'approve', source_stage_keys: ['script_generate', 'script_structure'], operation: null, output_kinds: [], scope_kinds: ['project'] }] })
  const originals = { listArtifactPage: platformApi.listArtifactPage, getArtifact: platformApi.getArtifact,
    approveProductionRun: platformApi.approveProductionRun, performManualProductionAction: platformApi.performManualProductionAction,
    getManualProductionActions: platformApi.getManualProductionActions }
  t.after(() => Object.assign(platformApi, originals))
  const queries: any[] = [], submitted: any[] = [], events: any[] = []
  platformApi.listArtifactPage = async value => { queries.push(value); return { results: [artifact('one')], count: 1, hasNext: false, hasPrevious: false } }
  platformApi.getArtifact = async id => artifact(id)
  platformApi.approveProductionRun = async () => { throw new Error('Manual review must not call recipe control') }
  platformApi.performManualProductionAction = async (id, input) => {
    submitted.push({ id, input: JSON.parse(JSON.stringify(input)) })
    if (submitted.length === 1) throw new Error('uncertain connection result')
    return { action: 'approve', result_kind: 'approval_decision', target_id: 'decision' }
  }
  platformApi.getManualProductionActions = async () => options
  const { default: Component } = await vite.ssrLoadModule('/src/components/ProductionTextCandidateReview.vue')
  const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
  const props = reactive({ run: { id: 'run', project: 'project', planSource: 'manual', executionPlan: { sha256: hash }, stages: [{ ...source, status: 'running' }, stage] }, stage, currentRules: {}, manualOptions: options })
  let state: any
  const app = renderer.createApp({ setup() { state = Component.setup(props, { expose() {}, emit: (...args) => events.push(args) }); return () => h('div') } })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); t.after(() => app.unmount())
  await Promise.resolve(); await nextTick()
  assert.equal(queries.length, 0); assert.equal(state.manualSourceId.value, '')
  state.manualSourceId.value = source.id; await state.load(1)
  assert.deepEqual(queries, [{ production_run: 'run', stage_run: source.id, page: 1 }])
  await state.choose(artifact('one')); await state.submit(); assert.equal(submitted.length, 0)
  state.reviewed.value = true; await state.submit()
  assert.match(state.error.value, /uncertain/); assert.equal(state.locked.value, true)
  const originalPending = JSON.parse(JSON.stringify(state.pendingRequest.value))
  await state.refresh(); await state.load(1)
  assert.deepEqual(state.pendingRequest.value, originalPending)
  assert.equal(queries.length, 1)
  await state.choose(artifact('two')); assert.equal(state.selected.value.id, 'one')
  await state.submit()
  assert.equal(submitted.length, 2); assert.deepEqual(submitted[1], submitted[0])
  assert.equal(submitted[0].input.stage_key, 'script_review')
  assert.equal(submitted[0].input.expected_selection_revision, 0)
  assert.equal(submitted[0].input.payload.expected_current_script, null)
  assert.equal('adopt_generated_metadata' in submitted[0].input.payload, false)
  assert.deepEqual(submitted[0].input.payload.text_candidate, { artifact_id: 'one', expected_sha256: hash, expected_manifest_sha256: hash })
  assert.equal('expected_source_snapshot_sha256' in submitted[0].input.payload, false)
  assert.equal(state.pendingRequest.value, null); assert.equal(events.filter(row => row[0] === 'changed').length, 1)
  const html = await renderReview(Component, props)
  assert.match(html, /查看哪个生成步骤/); assert.match(html, /确认采用此剧本/)
  assert.doesNotMatch(html, /确认采用并继续|拒绝本阶段并终止/)
  assert.equal(queries.length, 1) // rendering never chooses a source or reads candidates
})

test('manual film rules allow empty negative constraints, need no script and require all three explicit checks', async t => {
  const vite = await server(); t.after(() => vite.close())
  const { platformApi } = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const { adaptManualActionOptions } = await vite.ssrLoadModule('/src/services/productionManual.ts')
  const content = {kind:'script_revision',id:'script',sha256:hash,revision:1}
  const options = adaptManualActionOptions({run_id:'run',run_plan_sha256:hash,
    execution_activity:[],content_change_stage_keys:[],script_adoption:{revision:0,decision_id:null,content:null},script_review:{revision:0,decision_id:null,content:null},
    visual_rules_review:{revision:2,decision_id:'previous-rules',status:'current',reasons:[]},current_script:null,current_script_identity:null,current_script_decision_id:null,
    actions:[{stage_key:'visual_rules_review',action:'approve',operation:null,output_kinds:[],scope_kinds:['project'],source_stage_keys:['visual_rules_generate']}]})
  const producer = {...source,stageKey:'visual_rules_generate',ordinal:7,status:'running'}
  const review = {...stage,stageKey:'visual_rules_review',ordinal:8,inputSnapshot:{}}
  const row = {...artifact('rules'),textCandidate:{operation:'visual_rules.generate',warnings:[],review_contract:visualRuleReviewFixture(),candidate:{
    negative_prompt:'',color_script:'Muted blue.',continuity_summary:'Keep the traveler consistent.'}}}
  const originals = {listArtifactPage:platformApi.listArtifactPage,getArtifact:platformApi.getArtifact,performManualProductionAction:platformApi.performManualProductionAction}
  t.after(()=>Object.assign(platformApi,originals))
  const calls: any[] = []
  platformApi.listArtifactPage = async () => ({results:[row],count:1,hasNext:false,hasPrevious:false})
  platformApi.getArtifact = async () => row
  platformApi.performManualProductionAction = async (id,input) => {calls.push(JSON.parse(JSON.stringify(input)));return {action:'approve',result_kind:'approval_decision',target_id:'new-decision'}}
  const {default:Component} = await vite.ssrLoadModule('/src/components/ProductionTextCandidateReview.vue')
  const props = reactive({run:{id:'run',project:'project',planSource:'manual',executionPlan:{sha256:hash},stages:[producer,review]},
    stage:review,currentRules:{negative_prompt:'Old rule.'},manualOptions:options})
  const renderer = createRenderer<any, any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  let state: any
  const app = renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit(){}});return ()=>h('div')}})
  app.provide(ssrContextKey,{modules:new Set()});app.mount({});t.after(()=>app.unmount())
  state.manualSourceId.value=producer.id;await state.load(1);await state.choose(row)
  state.checked.value.negative_prompt=true;state.checked.value.color_script=true
  await state.submit();assert.equal(calls.length,0)
  state.edits.value.continuity_summary='Manually reviewed continuity.';state.checked.value.continuity_summary=true
  await state.submit();assert.equal(calls.length,1)
  assert.equal(calls[0].stage_key,'visual_rules_review');assert.equal(calls[0].expected_selection_revision,2)
  assert.equal('expected_current_script' in calls[0].payload,false)
  assert.equal(calls[0].payload.candidate_overrides.negative_prompt,'')
  assert.equal(calls[0].payload.candidate_overrides.continuity_summary,'Manually reviewed continuity.')
  assert.equal('adopt_generated_metadata' in calls[0].payload,false)
  const html=await renderReview(Component,props)
  assert.match(html,/确认采用这些规则/);assert.doesNotMatch(html,/确认采用并继续/)
  assert.match(html,/本次明确配置的输入/)
})

for (const mode of ['manual', 'recipe']) {
  test(`${mode} candidate refresh preserves reviewed content on failure and restores only matching identities`, () => withReadonlyReview(async ({state,props,api,writes,row,render}) => {
    props.run.status = 'awaiting_approval'; props.run.planSource = mode; await nextTick()
    if (mode === 'manual') state.manualSourceId.value = 'source'
    await state.load(); await state.choose(row)
    state.reviewed.value = true; state.comment.value = 'Unsubmitted review note'
    assert.equal(state.ready.value, true)
    api.listArtifactPage = async () => { throw Error('candidate list offline') }
    await state.refresh()
    assert.equal(state.selected.value.id, 'one'); assert.equal(state.detail.value.text, 'script one')
    assert.equal(state.rows.value.length, 1); assert.equal(state.count.value, 1)
    assert.equal(state.reviewed.value, true)
    assert.equal(state.comment.value, 'Unsubmitted review note'); assert.equal(state.ready.value, false)
    assert.match(await render(), /候选清单未能完整读取.*保留的是上次内容与未提交审核/)
    await state.choose({...row,id:'two'}); assert.equal(state.selected.value.id, 'one')
    await state.submit(); state.decision.value = 'reject'; await state.submit(); assert.equal(writes.length, 0)
    state.decision.value = 'approve'
    api.listArtifactPage = async () => ({results:[row],count:1,hasNext:false,hasPrevious:false})
    await state.refresh()
    assert.equal(state.listReadFailed.value, false); assert.equal(state.error.value, '')
    assert.equal(state.ready.value, true); assert.equal(state.comment.value, 'Unsubmitted review note')
    api.listArtifactPage = async () => ({results:[{...row,manifestMetadataSha256:'b'.repeat(64)}],count:1,hasNext:false,hasPrevious:false})
    await state.refresh()
    assert.equal(state.selected.value, null); assert.equal(state.detail.value, null); assert.equal(state.ready.value, false)
    assert.equal(state.comment.value, 'Unsubmitted review note'); assert.match(state.readNotice.value, /已取消旧选择/)
    assert.equal(writes.length, 0)
  }))
}

test('visual candidate refresh retains edits and field checks without mistaking stale content for a valid review', () => withReadonlyReview(async ({state,props,api,row,writes}) => {
  props.run.status='awaiting_approval'; await nextTick(); state.manualSourceId.value='source'
  await state.load(); await state.choose(row)
  state.edits.value.color_script='Human draft'; state.checked.value={negative_prompt:true,color_script:true,continuity_summary:true}
  api.getManualProductionActions=async()=>{throw Error('manual state offline')}
  await state.refresh(); assert.equal(state.listReadFailed.value,true); assert.equal(state.ready.value,false)
  assert.equal(state.edits.value.color_script,'Human draft'); assert.equal(state.checked.value.color_script,true)
  await state.submit(); assert.equal(writes.length,0)
},true))

test('candidate list rejects foreign scope and duplicate IDs without replacing prior facts', () => withReadonlyReview(async ({state,props,api,row,writes}) => {
  props.run.status='awaiting_approval'; await nextTick(); state.manualSourceId.value='source'
  await state.load(); await state.choose(row)
  for (const results of [[{...row,productionRun:'foreign'}],[{...row,stageRun:'foreign'}],[row,row]]) {
    api.listArtifactPage=async()=>({results,count:results.length,hasNext:false,hasPrevious:false})
    await state.load(); assert.equal(state.rows.value.length,1); assert.equal(state.selected.value.id,'one')
    assert.equal(state.listReadFailed.value,true); assert.match(state.error.value,/清单归属或身份不一致/)
    await state.submit(); assert.equal(writes.length,0)
  }
}))

test('same-hash wrong candidate detail and detail failure cannot become selected or submit', () => withReadonlyReview(async ({state,props,api,row,writes}) => {
  props.run.status='awaiting_approval'; await nextTick(); state.manualSourceId.value='source'; await state.load()
  api.getArtifact=async()=>({...row,id:'different-candidate'})
  await state.choose(row); assert.equal(state.selected.value,null); assert.equal(state.detail.value,null)
  assert.match(state.error.value,/候选身份或内容/)
  api.getArtifact=async()=>{throw Error('detail offline')}
  await state.choose(row); assert.match(state.error.value,/detail offline/); assert.equal(state.ready.value,false)
  await state.submit(); assert.equal(writes.length,0)
  api.getArtifact=async()=>row; await state.choose(row)
  assert.equal(state.error.value,''); assert.equal(state.detail.value.text,'script one'); assert.equal(state.reviewed.value,false)
}))

test('duplicate list refresh is suppressed and late list responses cannot replace a new run context', () => withReadonlyReview(async ({state,props,api,row}) => {
  props.run.status='awaiting_approval'; await nextTick(); state.manualSourceId.value='source'; await state.load(); await state.choose(row)
  state.reviewed.value=true
  let finish:any, calls=0
  api.listArtifactPage=()=>{calls++;return new Promise(resolve=>{finish=resolve})}
  const request=state.load(); await state.load(); await state.refresh()
  assert.equal(calls,1); assert.equal(state.ready.value,false); assert.equal(state.detail.value.text,'script one')
  props.run={...props.run,id:'new-run'}; await nextTick()
  finish({results:[row],count:1,hasNext:false,hasPrevious:false}); await request
  assert.equal(state.rows.value.length,0); assert.equal(state.selected.value,null); assert.equal(state.detail.value,null)
  assert.equal(state.loading.value,false); assert.equal(state.ready.value,false)
}))

test('initial list failure is unknown rather than an empty successful list', () => withReadonlyReview(async ({state,props,api,render,writes}) => {
  props.run.status='awaiting_approval'; await nextTick(); state.manualSourceId.value='source'
  api.listArtifactPage=async()=>{throw Error('first read failed')}
  await state.load()
  assert.equal(state.count.value,null); assert.equal(state.listReadFailed.value,true)
  const html=await render(); assert.match(html,/尚无完整候选清单/); assert.doesNotMatch(html,/没有候选产物|共 0 个候选/)
  state.decision.value='reject'; await state.submit(); assert.equal(writes.length,0)
}))

test('late manual refresh cannot attach an old review-basis notice to a new run', () => withReadonlyReview(async ({state,props,api,row}) => {
  props.run.status='awaiting_approval'; await nextTick(); state.manualSourceId.value='source'
  await state.load(); await state.choose(row)
  api.getManualProductionActions=async()=>({...state.manualState.value,currentScript:{kind:'script_revision',id:'changed-script',sha256:hash,revision:2}})
  let finish:any
  api.listArtifactPage=()=>new Promise(resolve=>{finish=resolve})
  const request=state.refresh(); await Promise.resolve()
  props.run={...props.run,id:'new-run'}; await nextTick()
  // Represent the new context's selection before the old read resolves.
  state.selected.value={...row,id:'new-candidate',productionRun:'new-run'}
  finish({results:[row],count:1,hasNext:false,hasPrevious:false}); await request
  assert.equal(state.selected.value.id,'new-candidate'); assert.equal(state.readNotice.value,'')
}))

for (const visual of [false,true]) {
  test(`changed manual ${visual?'visual':'script'} review basis retains drafts but clears previous confirmations`, () => withReadonlyReview(async ({state,props,api,row,writes}) => {
    props.run.status='awaiting_approval'; await nextTick(); state.manualSourceId.value='source'
    await state.load(); await state.choose(row)
    state.reviewed.value=true; state.checked.value={negative_prompt:true,color_script:true,continuity_summary:true}
    state.edits.value.color_script='Unsaved reviewed draft'; state.comment.value='Review note'
    assert.equal(state.ready.value,true)
    const updated={...state.manualState.value,...(visual?{visualRulesReview:{revision:2,decisionId:'other-review',status:'current',reasons:[]}}:{currentScript:{kind:'script_revision',id:'changed-script',sha256:hash,revision:2}})}
    api.getManualProductionActions=async()=>updated
    await state.refresh()
    assert.equal(state.selected.value.id,'one'); assert.equal(state.comment.value,'Review note')
    assert.equal(state.edits.value.color_script,'Unsaved reviewed draft')
    assert.equal(state.reviewed.value,false); assert.deepEqual(state.checked.value,{})
    assert.equal(state.ready.value,false); assert.match(state.readNotice.value,/本轮审核依据已变化/)
    await state.submit(); assert.equal(writes.length,0)
  },visual))
}

test('script field draft uses frozen schema, clears confirmation, and manual approval sends explicit edited JSON', () => withReadonlyReview(async ({state,props,api,row,setRow,render}) => {
  props.run.status='awaiting_input'; await nextTick(); state.manualSourceId.value='source'
  const value=[{cost:0,enabled:false,note:null,extra:'original'}]
  const candidate={...row,textCandidate:{...row.textCandidate,candidate:value,text:JSON.stringify(value),json_schema:{type:'array',items:{type:'object',properties:{cost:{type:'number'},enabled:{type:'boolean'},note:{anyOf:[{type:'string'},{type:'null'}]},extra:{type:'string'}},required:['cost']}}}}
  setRow(candidate);await state.load();await state.choose(candidate)
  state.editScript();assert.equal(state.editingScript.value,true);assert.deepEqual(state.scriptDraft.value,value)
  state.reviewed.value=true;state.changeScript([{...value[0],extra:'human edit'}]);assert.equal(state.reviewed.value,false)
  assert.equal(candidate.textCandidate.candidate[0].extra,'original')
  assert.match(await render(),/人工编辑稿|确认另存修订并采用/)
  state.manualState.value={...state.manualState.value,scriptReview:{revision:0},currentScript:null}
  let sent:any,finish:any,calls=0
  api.performManualProductionAction=async(_id:any,request:any)=>{calls++;sent=request;return new Promise(resolve=>{finish=resolve})}
  state.reviewed.value=true;assert.equal(state.ready.value,true,JSON.stringify({errors:state.scriptErrors.value,draft:state.draftErrors.value,error:state.error.value}));const pending=state.submit();await state.submit();assert.equal(calls,1,state.error.value)
  state.changeScript([]);assert.equal(state.scriptDraft.value[0].extra,'human edit')
  assert.deepEqual(JSON.parse(sent.payload.script_content),[{cost:0,enabled:false,note:null,extra:'human edit'}])
  finish({action:'approve',result_kind:'approval_decision',target_id:'decision'});await pending
  assert.equal(state.editingScript.value,false);assert.equal(state.selected.value,null)
}))

test('script edit validation failure retains draft and permits correction, unknown result keeps original request locked', () => withReadonlyReview(async ({state,props,api,row,setRow}) => {
  props.run.status='awaiting_input';await nextTick();state.manualSourceId.value='source'
  const candidate={...row,textCandidate:{...row.textCandidate,candidate:{x:'original'},json_schema:{type:'object',properties:{x:{type:'string'}},required:['x']}}}
  setRow(candidate);await state.load();await state.choose(candidate);state.editScript()
  state.manualState.value={...state.manualState.value,scriptReview:{revision:0},currentScript:null}
  state.changeScript({x:10});state.reviewed.value=true;assert.equal(state.ready.value,false)
  state.changeScript({x:'edited'});state.reviewed.value=true
  api.performManualProductionAction=async()=>{throw {response:{status:400,data:{error:{code:'validation_error',message:'invalid',details:{'source.x':['关系不匹配']}}}}}}
  await state.submit();assert.equal(state.pendingRequest.value,null);assert.equal(state.reviewed.value,false)
  assert.deepEqual(state.scriptDraft.value,{x:'edited'});assert.match(state.error.value,/invalid|关系不匹配/)
  state.changeScript({x:'corrected'});state.reviewed.value=true
  api.performManualProductionAction=async()=>{throw Error('network outcome unknown')}
  await state.submit();const pending=state.pendingRequest.value;assert.ok(pending)
  state.cancelScriptEdit();state.changeScript({x:'wrong retry'});assert.deepEqual(state.scriptDraft.value,{x:'corrected'})
  let replay:any;api.performManualProductionAction=async(_id:any,request:any)=>{replay=request;return {action:'approve',result_kind:'approval_decision',target_id:'decision'}}
  await state.submit();assert.deepEqual(replay,pending)
}))

test('script edit cancel and selected or ended candidates cannot mutate original or send an edit', () => withReadonlyReview(async ({state,props,row,setRow}) => {
  const candidate={...row,textCandidate:{...row.textCandidate,json_schema:{type:'object'},candidate:{x:0}}}
  props.run.status='awaiting_input';await nextTick();state.manualSourceId.value='source';setRow(candidate)
  await state.load();await state.choose(candidate);state.editScript();state.changeScript({x:1});state.cancelScriptEdit()
  assert.equal(state.scriptContent.value,undefined);assert.deepEqual(state.detail.value.candidate,{x:0})
  props.run.status='succeeded';await nextTick();state.editScript();assert.equal(state.editingScript.value,false)
  props.run.status='awaiting_input';state.selected.value.status='selected';await nextTick();state.editScript();assert.equal(state.editingScript.value,false)
}))
