// Real component, simulated transport; no business API or model invocation.
import { createApp, h, reactive, ref } from 'vue'
import ManualPrimaryRetry from '../../src/components/ManualPrimaryRetry.vue'
import { platformApi } from '../../src/services/platformApi'
import { api } from '../../src/services/api'
import type { ExecutionPreparationDetail } from '../../src/services/executionPreparation'
import type { JsonObject, ProductionRun } from '../../src/services/platformTypes'
import baseStyles from '../../src/styles.css?inline'
import '../../src/platform.css'

api.defaults.adapter = async () => { throw Error('隔离夹具禁止业务网络请求') }
const sha = 'a'.repeat(64)
const mode = ref('receipt'), reads = ref(0), calls = ref<JsonObject[]>([])
const changed = ref(0), busy = ref(false), dirty = ref(false), recovering = ref(false)
const run = { id: 'fixture-run', planSource: 'manual', status: 'running', executionPlan: { sha256: sha },
  stages: [{ id: 'fixture-stage', stageKey: 'media_generation', status: 'running' }] } as ProductionRun
const preparation = reactive({ id: 'fixture-preparation', stageRun: 'fixture-stage', unitKey: 'opaque-unit', purpose: 'production', revision: 4,
  status: 'dispatched', requestFrozenAt: '2026-09-29', intent: { request: { requested: {
    stage_key: 'media_generation', run_plan_sha256: sha, scope: { kind: 'shot', id: 'fixture-shot' },
  } } }, presentation: { kind: 'primary', task: { id: 'fixture-failed-call', status: 'failed', errorCode: 'execution_workflow_failed' } },
} as ExecutionPreparationDetail)
platformApi.getManualProductionActions = async () => {
  reads.value++
  if (mode.value === 'read-error' && !recovering.value) throw Error('隔离示例：动作读取失败')
  return { runId: run.id, runPlanSha256: sha, actions: mode.value === 'no-action' ? [] : [
    { action: 'retry', stageKey: 'media_generation', scopeKinds: ['shot'] },
  ] } as Awaited<ReturnType<typeof platformApi.getManualProductionActions>>
}
platformApi.performManualProductionAction = async (_runId, request) => {
  calls.value.push(JSON.parse(JSON.stringify(request)))
  if (mode.value === 'unknown' && calls.value.length === 1) throw Error('隔离示例：网络回执丢失')
  return { id: 'fixture-receipt', action: 'retry', result_kind: 'preparation', target_id: preparation.id }
}
const switchMode = (value: string) => {
  mode.value = value; reads.value = 0; calls.value = []; changed.value = 0
  busy.value = false; dirty.value = false; recovering.value = false
  preparation.presentation!.task!.errorCode = value === 'remote-unknown' ? 'uncertain_remote_response'
    : value === 'cost-unknown' ? 'actual_cost_reconciliation_required' : 'execution_workflow_failed'
  preparation.presentation!.task!.status = value === 'unknown-status' ? 'unknown' : 'failed'
}
createApp({ render: () => h('main', { style: { maxWidth: '920px', margin: '20px auto', padding: '16px' } }, [
  h('style', baseStyles.replace(/@import\s*(?:url\([^)]*\)|"[^"]*"|'[^']*')\s*;/g, '')),
  h('h1', '人工失败主调用重试 · 隔离验收'),
  h('p', '实际组件，模拟回执；业务网络已拦截，不调用模型、不重试真实任务。'),
  h('nav', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap' } }, Object.entries({
    receipt: '受理回执（不代表成功）', unknown: '提交回执未知', 'read-error': '动作读取失败', 'no-action': '旧计划无重试动作',
    'remote-unknown': '失败但远端结果未知', 'cost-unknown': '失败但费用待核对', 'unknown-status': '原调用未知状态',
  }).map(([value, label]) => h('button', { class: 'button ghost', disabled: busy.value, onClick: () => switchMode(value) }, label))),
  mode.value === 'read-error' ? h('button', { class: 'button ghost', onClick: () => { recovering.value = true } }, '恢复模拟读取服务') : null,
  h(ManualPrimaryRetry, { key: mode.value, run, preparation,
    onChanged: () => { changed.value++ }, onBusy: (value: boolean) => { busy.value = value }, onDirty: (value: boolean) => { dirty.value = value },
  }),
  h('p', { role: 'status' }, `模拟读取 ${reads.value} 次；模拟提交 ${calls.value.length} 次；不同请求 ${new Set(calls.value.map(row => row.idempotency_key)).size} 个；刷新通知 ${changed.value} 次；未核实请求 ${dirty.value ? '有' : '无'}`),
  h('details', [h('summary', '本地模拟请求（不发送）'), h('pre', { style: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' } }, JSON.stringify(calls.value, null, 2))]),
]) }).mount('#app')
