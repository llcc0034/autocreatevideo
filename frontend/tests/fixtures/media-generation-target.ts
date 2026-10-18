// Actual workbench, synthetic read responses; never reach a business API.
import { createApp, h, ref } from 'vue'
import ExecutionPreparationWorkbench from '../../src/components/ExecutionPreparationWorkbench.vue'
import { adaptExecutionPreparationDetail } from '../../src/services/executionPreparation'
import { executionPreparationApi } from '../../src/services/executionPreparationApi'
import { api } from '../../src/services/api'
import { targetFixture, versionId } from './media-generation-target-data'
import '../../src/styles.css'
import '../../src/platform.css'

api.defaults.adapter = async () => { throw Error('隔离夹具禁止业务网络请求。') }
const mode = ref('variant'), failed = ref(false)
executionPreparationApi.get = async id => {
  if (failed.value) throw Error('隔离模拟：读取失败，不显示为新鲜合同。')
  const target = ['variant', 'structured', 'passthrough'].includes(mode.value) ? targetFixture('variant', true) : mode.value === 'primary' ? targetFixture('primary')
    : mode.value === 'invalid' ? null : targetFixture()
  if (mode.value === 'structured' && target?.appearance) Object.assign(target.appearance, {
    difference: { kind: 'reference_field', key: 'units', unit_key: 'second', field_key: 'unit_description', value_sha256: 'e'.repeat(64) } })
  if (mode.value === 'passthrough' && target?.appearance) Object.assign(target.appearance, {
    difference: { kind: 'structured_field', key: 'change', field_key: 'exact_change', value_sha256: 'f'.repeat(64) } })
  return adaptExecutionPreparationDetail({ id, execution_instance_version: versionId, purpose: 'production',
    stage_run: null, asset_job: 'fixture-job', deployment_smoke: null, workflow_smoke: null, unit_key: 'fixture', request_key: 'fixture',
    revision: 1, status: 'cancelled', intent_sha256: 'a'.repeat(64), request_sha256: null, request_frozen_at: null,
    next_check_at: null, reason_code: '', reason: '隔离展示，不运行任务。', evaluation_revision: null, evaluated_at: null,
    created_by: 1, created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z',
    presentation: null, outcome: { message: '无真实调用或产物。', artifact_count: 0, selected_count: 0 },
    intent: {}, resolved_inputs: mode.value === 'missing' ? {} : { media_generation_target: target },
    review_contract: { schema_version: 1, mode: 'policy' }, review_summary: { mode: 'policy', label: '隔离规则', description: '不是人审' },
    prepared_request: null, evaluation: null, field_slots: [], field_selections: [], capacity_sample: null, auxiliary_batches: [],
    response_diagnostic: null, cancellation: null, resource_review: null, text_response_review: null })
}
createApp({ setup: () => () => h('main', { style: { maxWidth: '1000px', margin: '20px auto', padding: '12px' } }, [
  h('h1', '生成目标 · 隔离夹具'), h('p', '仅验证原工作台展示。数据均为模拟，无业务写入和真实模型请求。'),
  h('label', ['示例 ', h('select', { value: mode.value, onChange: (event: Event) => { mode.value = (event.target as HTMLSelectElement).value; failed.value = false } },
    [['variant', '变体及确切设定'], ['structured', '结构化参考字段差异'], ['passthrough', '结构化直通字段差异'], ['primary', '主图但无设定'], ['undeclared', '未声明角色'], ['missing', '未记录目标'], ['invalid', '合同异常']].map(([value, label]) => h('option', { value }, label)))]),
  h('button', { type: 'button', onClick: () => { failed.value = !failed.value } }, failed.value ? '恢复隔离读取' : '模拟下次读取失败'),
  h(ExecutionPreparationWorkbench, { key: mode.value, preparationId: `fixture-${mode.value}`, versionId, purpose: 'production' }),
]) }).mount('#app')
