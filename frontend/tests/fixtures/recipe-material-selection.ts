// Original comparison and modal; transport is isolated, never business evidence.
import { createApp, h, ref } from 'vue'
import GeneratedArtifactCompare from '../../src/components/GeneratedArtifactCompare.vue'
import { platformApi } from '../../src/services/platformApi'
import { api } from '../../src/services/api'
import { adaptRecipeMaterialSelectionContext } from '../../src/services/recipeMaterialSelection'
import '../../src/styles.css'
import '../../src/platform.css'
api.interceptors.request.use(() => { throw new Error('隔离夹具禁止真实业务API') })
const picture = 'data:image/svg+xml,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512"><rect width="512" height="512" fill="#315d69"/><text x="90" y="265" fill="white" font-size="32">ISOLATED FIXTURE</text></svg>')
const artifacts = ref<any[]>([1, 2].map(number => ({ id: `candidate-${number}`, stageRun: 'stage', productionRun: 'isolated',
  kind: 'image', role: 'primary_generation_output', status: 'available', variantIndex: number, variantGroup: 'same-appearance',
  objectKey: `isolated-candidate-${number}.svg`, mimeType: 'image/svg+xml', sha256: 'a'.repeat(64) })))
const mode = ref('ready'), reads = ref(0), writes = ref(0), requests = ref('尚未提交'), epoch = ref(0)
let pending: (() => void) | null = null
platformApi.getArtifactPreview = async () => ({ previewUrl: picture, downloadUrl: '', backendStatus: 'available', parameters: {} } as any)
platformApi.getMaterialSelectionContext = async (_run, artifact) => {
  reads.value++
  const selectedMode = mode.value
  if (selectedMode === 'delayed') await new Promise<void>(resolve => { pending = resolve })
  if (selectedMode === 'error') throw new Error('隔离影响读取失败；请重试')
  return adaptRecipeMaterialSelectionContext({ artifact_id: artifact, stage_run_id: 'stage', unit_key: 'appearance:traveler',
    appearance_role: 'primary', reference_name: '旅人 · 城市便装主图', candidate_number: artifact.endsWith('1') ? 1 : 2,
    expected_decision_id: 'observed-decision', selected_artifact_id: selectedMode === 'reselect' ? 'candidate-1' : 'previous-candidate',
    can_select: selectedMode !== 'blocked', reason: selectedMode === 'blocked' ? '变体调用在途或结果未知，不能取消、替换或重发。' : '',
    dependencies_sha256: 'b'.repeat(64),
    dependent_preparations: selectedMode === 'dependency' || selectedMode === 'blocked' ? [
      { id: 'prep', unit_key: 'appearance:raincoat', reference_name: '旅人雨衣', status: 'awaiting_input', revision: 2,
        action: selectedMode === 'blocked' ? 'blocked' : 'reprepare', reason: selectedMode === 'blocked' ? '辅助调用仍在途或结果未知；待原调用明确结束后重新读取影响，不取消或重发。' : '主调用尚未开始：停止旧准备及未提交辅助任务，按新主图重新准备；已结束的辅助请求、结果及费用保留在旧准备，不移植到新请求。' },
      { id: 'completed', unit_key: 'appearance:suit', reference_name: '旅人正装', status: 'dispatched', revision: 5,
        action: 'retain', reason: '已生成：保留原主图依据和结果，不重新调用。' }] : [] }, artifact)
}
let currentId = ''
platformApi.selectArtifact = async (_run, body, key) => {
  writes.value++; requests.value = JSON.stringify({ body, key })
  if (mode.value === 'conflict') throw { response: { status: 409, data: { error: { status_code: 409, code: 'material_selection_changed', details: { detail: '当前选择已改变，请重新查看候选和影响后确认。' } } } } }
  currentId = body.artifact_id
  return {id:crypto.randomUUID(),action:'select',result_kind:'artifact_selection',target_id:crypto.randomUUID(),created_at:new Date().toISOString()} as any
}
createApp({setup() { return () => h('main', { style: { maxWidth: '1060px', margin: '16px auto', padding: '16px', background: '#101820', color: '#e6edf3' } }, [
  h('h1', '阶段内素材选择 · 隔离验收'), h('p', '正式比较组件；所有预览、选择响应都是隔离夹具，没有业务API、真实生成或审核采用。'),
  h('label', ['场景 ', h('select', { value: mode.value, onChange: (event: Event) => { mode.value = (event.target as HTMLSelectElement).value;
    artifacts.value = artifacts.value.map(item => ({ ...item, status: mode.value === 'reselect' && item.id === 'candidate-1' ? 'selected' : 'available' })); epoch.value++ } },
    ['ready', 'reselect', 'dependency', 'blocked', 'conflict', 'error', 'delayed'].map(value => h('option', { value }, value)))]),
  h('button', { onClick: () => { pending?.(); pending = null } }, '释放延迟响应'),
  h('button', { onClick: () => { epoch.value++; artifacts.value = artifacts.value.map(item => ({ ...item, status: 'available' })) } }, '切换运行视图'),
  h('output', { id: 'counts' }, `影响读取 ${reads.value} 次；提交 ${writes.value} 次`),
  h(GeneratedArtifactCompare, { key: epoch.value, runId: `isolated-${epoch.value}`, artifacts: artifacts.value, materialSelection: true,
    onSelected: () => { artifacts.value = artifacts.value.map(item => ({ ...item, status: item.id === currentId ? 'selected' : 'available' })) } }),
  h('pre', { style: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }, id: 'requests' }, requests.value),
]) }}).mount('#app')
