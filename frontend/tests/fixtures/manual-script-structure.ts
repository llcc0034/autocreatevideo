import { createApp, h, ref } from 'vue'
import ManualScriptStructureCheck from '../../src/components/ManualScriptStructureCheck.vue'
import type { ManualActionOptions, ManualScriptStructureCheck as Result } from '../../src/services/productionManual'
import { platformApi } from '../../src/services/platformApi'
import { api } from '../../src/services/api'
import '../../src/styles.css'

// This page has only in-memory fixtures; never send a business API request.
api.interceptors.request.use(() => { throw Error('隔离样例禁止真实API请求') })
const hash = 'a'.repeat(64)
const options = ref({ runId: 'fixture-run', currentScript: { id: 'fixture-revision', sha256: hash },
  currentScriptDecisionId: 'fixture-adoption' } as unknown as ManualActionOptions)
const mode = ref('structured'), calls = ref(0)
platformApi.getManualScriptStructure = async () => {
  calls.value++
  if (mode.value === 'error') throw Error('隔离样例：已保存结构摘要损坏，请核对来源')
  const unstructured = mode.value === 'unstructured'
  return { runId: options.value.runId, currentScript: options.value.currentScript,
    currentScriptDecisionId: options.value.currentScriptDecisionId, status: unstructured ? 'unstructured' : 'structured',
    reasonCode: unstructured ? 'script_structure_required' : '',
    reason: unstructured ? '本次采用的修订尚未保存结构化结果；这不代表原文有误，也不要求一定调用模型。' :
      '本次采用的修订已有按冻结字段映射保存的结构化结果，无需重复解析。',
    episodeCount: unstructured ? null : mode.value === 'empty' ? 0 : 2,
    shotCount: unstructured ? null : mode.value === 'empty' ? 0 : 4 } as Result
}
createApp({ setup: () => () => h('main', { style: 'max-width:960px;margin:16px auto;padding:16px;min-width:0' }, [
  h('h1', '已采用剧本结构检查 · 隔离样例'),
  h('p', '真实组件、内存回执；不调用业务API或模型，不创建分镜，不证明现用后端已部署。'),
  h('label', ['样例状态 ', h('select', { value: mode.value, onChange: (event: Event) => {
    mode.value = (event.target as HTMLSelectElement).value
  } }, [['structured', '已保存结构'], ['unstructured', '尚无结构'], ['empty', '空声明'], ['error', '来源损坏']]
    .map(([value, label]) => h('option', { value }, label)))]),
  h('button', { type: 'button', onClick: () => { options.value = { ...options.value,
    currentScriptDecisionId: options.value.currentScriptDecisionId + '-changed' } } }, '模拟改用另一采用决定'),
  h('p', { role: 'status' }, `隔离读取次数：${calls.value}`),
  h(ManualScriptStructureCheck, { runId: options.value.runId, options: options.value }),
]) }).mount('#app')
