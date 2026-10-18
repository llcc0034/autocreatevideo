import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { readFileSync } from 'node:fs'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'

const root = fileURLToPath(new URL('..', import.meta.url))
const uid = (n: number) => `00000000-0000-0000-0000-${String(n).padStart(12, '0')}`
const raw = { run_id: uid(1), run_plan_sha256: 'a'.repeat(64), checked_at: '2026-09-13T10:00:00+08:00',
  source: { stage_run_id: uid(2), stage_key: 'script_input', decision_id: uid(3), revision: 1, script_revision_id: uid(4) },
  assembly_locked: false, run_read_only: false, stages: [{ id: uid(5), stage_key: 'script_generate', status: 'running', preparation_count: 1 }],
  replacement: {allowed:false, code:'active_preparations', reason:'请先停止后续主准备，再查看原调用是否结束'},
  count: 1, offset: 0, limit: 20, next_offset: null,
  results: [{ id: uid(6), stage_run_id: uid(5), stage_key: 'script_generate', unit_key: `project:${uid(7)}`, status: 'preparing', revision: 1,
    reference_state: 'recorded_reference', references: [{ input_path: 'script_adoption_baseline', kind: 'adoption_baseline' }],
    attempts: [{ role: 'primary', status: 'failed', count: 1 }, { role: 'auxiliary', status: 'succeeded', count: 2 }] }] }
async function modules(fn: (m: any, vite: any) => Promise<void> | void) {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  try { await fn(await vite.ssrLoadModule('/src/services/productionManualChangeImpact.ts'), vite) }
  finally { await vite.close() }
}
test('original references and call counts remain distinct from current execution validity', () => modules(m => {
  const value = structuredClone(raw), data = m.adaptManualChangeImpact(value)
  assert.equal(data.results[0].referenceState, 'recorded_reference')
  assert.equal(data.results[0].attempts[0].count, 1)
  assert.match(m.changeReferenceKindLabels.adoption_baseline, /不是提示词输入/)
  value.results[0].references[0].input_path = 'changed'
  assert.equal(data.results[0].references[0].inputPath, 'script_adoption_baseline')
}))
test('unknown, missing, mismatched and silently truncated data are rejected', () => modules(m => {
  for (const patch of [{count: 2}, {results: []}, {next_offset: 20}, {limit: 51}, {checked_at: 'yesterday'},
    {assembly_locked: null}, {source: {}}, {stages: []}, {replacement:null},
    {replacement:{allowed:true,code:'unresolved_calls',reason:'远端未结束'}}]) assert.throws(() => m.adaptManualChangeImpact({...raw,...patch}), /不能据此认定没有影响/)
  for (const patch of [{reference_state:'unknown'}, {references:[]}, {stage_key:'storyboard'}, {revision:0}]) {
    assert.throws(() => m.adaptManualChangeImpact({...raw, results:[{...raw.results[0],...patch}]}))
  }
}))
test('no recorded reference and unverifiable evidence are explicit, never green success', () => modules(m => {
  for (const state of ['manual_review_required','unverifiable']) {
    const data = m.adaptManualChangeImpact({...raw, results:[{...raw.results[0],reference_state:state,references:[]}]})
    assert.equal(data.results[0].referenceState,state)
  }
  const empty = m.adaptManualChangeImpact({...raw,stages:[],count:0,results:[]})
  assert.equal(empty.count,0)
}))

test('settled media permits explicit content change without claiming automatic reuse', () => modules(m => {
  const replacement={allowed:true,code:'media_review_required',reason:'旧生成记录不变，需重新审核复用或明确创建新候选'}
  const data=m.adaptManualChangeImpact({...raw,replacement})
  assert.equal(data.replacement.code,'media_review_required')
  assert.equal(data.replacement.allowed,true)
  assert.match(data.replacement.reason,/重新审核/)
  assert.throws(()=>m.adaptManualChangeImpact({...raw,replacement:{...replacement,allowed:false}}))
}))
test('formal endpoint is a read-only paginated GET and needs the selected content step', () => modules(async (m, vite) => {
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  const calls: unknown[] = []
  api.get = async (...args: unknown[]) => { calls.push(args); return {data:raw} }
  api.post = async () => { throw new Error('Impact viewing cannot write') }
  const { platformApi } = await vite.ssrLoadModule('/src/services/platformApi.ts')
  assert.equal((await platformApi.getManualChangeImpact(uid(1),'script_input',0)).count,1)
  assert.deepEqual(calls,[[`/production-runs/${uid(1)}/manual-change-impact/`,{params:{stage_key:'script_input',offset:0}}]])
}))

test('closed text results require source checks and explicit new work, not rebound candidates', () => modules(m => {
  const replacement={allowed:true,code:'text_review_required',reason:'旧文本保留原依据；依据变化须主动创建新准备，不会自动重跑'}
  const data=m.adaptManualChangeImpact({...raw,replacement})
  assert.equal(data.replacement.allowed,true)
  assert.match(data.replacement.reason,/主动创建新准备/)
  for(const key of ['assembly_locked','run_read_only'])assert.throws(()=>m.adaptManualChangeImpact({...raw,replacement,[key]:true}))
  assert.throws(()=>m.adaptManualChangeImpact({...raw,replacement:{...replacement,allowed:false}}))
}))

test('passive reviews beside stopped preparations require rereview, not silent reuse or restart', () => modules(m => {
  const replacement={allowed:true,code:'review_and_stopped',reason:'逐项重新审核并主动创建新准备；旧记录不变，不自动续跑'}
  const data=m.adaptManualChangeImpact({...raw,replacement})
  assert.equal(data.replacement.allowed,true)
  assert.equal(data.replacement.code,'review_and_stopped')
  assert.match(data.replacement.reason,/不自动续跑/)
  for(const key of ['assembly_locked','run_read_only'])assert.throws(()=>m.adaptManualChangeImpact({...raw,replacement,[key]:true}))
  assert.throws(()=>m.adaptManualChangeImpact({...raw,replacement:{...replacement,allowed:false}}))
}))
test('stopped unused scope is distinguished from unresolved calls, ended runs and adopted results', () => modules(m => {
  const replacement = {allowed:true,code:'stopped_unused',reason:'已停止且未采用，需手动创建新准备'}
  const data = m.adaptManualChangeImpact({...raw,replacement})
  assert.equal(data.replacement.allowed,true)
  assert.equal(data.replacement.reason,replacement.reason)
  for (const key of ['assembly_locked','run_read_only']) assert.throws(() => m.adaptManualChangeImpact({...raw,replacement,[key]:true}))
  for (const code of ['unresolved_calls','dependent_results','inconsistent_records']) {
    assert.equal(m.adaptManualChangeImpact({...raw,replacement:{allowed:false,code,reason:'等待原记录核对'}}).replacement.code,code)
  }
}))

test('review-only content replacement is explicit and never overrides assembly or ended-run locks', () => modules(m => {
  const replacement={allowed:true,code:'review_only',reason:'更换内容后必须重新采用和审核，不执行模型'}
  const record={...raw,source:{...raw.source,stage_key:'storyboard'},replacement}
  const data=m.adaptManualChangeImpact(record)
  assert.equal(data.replacement.code,'review_only')
  assert.equal(data.replacement.allowed,true)
  for(const key of ['assembly_locked','run_read_only'])assert.throws(()=>m.adaptManualChangeImpact({...record,[key]:true}))
  assert.throws(()=>m.adaptManualChangeImpact({...record,replacement:{...replacement,allowed:false}}))
  assert.equal(m.adaptManualChangeImpact({...raw,replacement}).replacement.allowed,true)
}))
test('workbench provides lazy accessible entry, stale-response guards and no change/dispatch action', () => {
  const component = readFileSync(new URL('../src/components/ManualContentChangeImpact.vue', import.meta.url),'utf8')
  const workbench = readFileSync(new URL('../src/components/ManualProductionWorkbench.vue', import.meta.url),'utf8')
  assert.match(workbench,/v-if="impactOpen && options"/)
  assert.match(component,/token !== serial/)
  assert.match(component,/result.runId !== runId/)
  assert.match(component,/请选择内容步骤/)
  assert.match(component,/data.replacement.reason/)
  assert.match(component,/服务端会再次核对/)
  assert.match(component,/原记录/)
  assert.match(component,/上一页/); assert.match(component,/下一页/)
  assert.doesNotMatch(component,/performManualProductionAction|retryProductionRun|advance_execution|\.post\(/)
})

test('mounted reader ignores old run responses, preserves dated results on error and never writes', () => modules(async (m, vite) => {
  const { platformApi } = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const calls: any[] = [], pending: any[] = []
  platformApi.getManualChangeImpact = (...args: unknown[]) => { calls.push(args); return new Promise((resolve, reject) => pending.push({resolve,reject})) }
  platformApi.performManualProductionAction = () => { throw new Error('Viewing cannot change upstream contents') }
  const { default: Component } = await vite.ssrLoadModule('/src/components/ManualContentChangeImpact.vue')
  const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
  const props = reactive({runId: uid(1), runPlanSha256: raw.run_plan_sha256, stageKeys:['script_input']})
  let state: any
  const app = renderer.createApp({setup() { state = Component.setup(props,{expose() {}}); return () => h('div') }})
  app.provide(ssrContextKey,{modules:new Set()}); app.mount({})
  try {
    await nextTick(); await state.load(); assert.equal(calls.length,0)
    state.stageKey.value = 'script_input'; await nextTick()
    const old = state.load(); props.runId = uid(9); await nextTick()
    pending[0].resolve(m.adaptManualChangeImpact(raw)); await old
    assert.equal(state.data.value,null)
    const current = state.load()
    pending[1].resolve(m.adaptManualChangeImpact({...raw,run_id:uid(9)})); await current
    assert.equal(state.data.value.runId,uid(9))
    const failed = state.load(); pending[2].reject(new Error('read failed')); await failed
    assert.match(state.error.value,/read failed/)
    assert.equal(state.data.value.checkedAt,raw.checked_at)
    const mismatch = state.load(); pending[3].resolve(m.adaptManualChangeImpact(raw)); await mismatch
    assert.match(state.error.value,/不属于当前运行/)
    assert.equal(state.data.value.runId,uid(9))
  } finally { app.unmount() }
}))
