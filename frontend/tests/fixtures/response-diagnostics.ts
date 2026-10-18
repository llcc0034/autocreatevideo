import { createApp, h, ref } from 'vue'
import ExecutionReturnedText from '../../src/components/ExecutionReturnedText.vue'
import '../../src/styles.css'

const source = 'source.chapters[0].frames[0].uses[0].variant_key'
const path = 'value.episodes[0].shots[0].references[0].variant_key'
const message = '变体未声明或属于另一实体，不回退主图'
createApp({ setup() {
  const mode = ref('relation')
  return () => h('main', { style: 'max-width:960px;margin:20px auto;padding:16px;min-width:0' }, [
    h('h1', '返回字段诊断 · 隔离样例'), h('p', '内存样例；不访问业务API，不调用模型，不保存数据。'),
    h('label', ['诊断样例 ', h('select', { value: mode.value, onChange: (e: Event) => { mode.value = (e.target as HTMLSelectElement).value } },
      [h('option', { value: 'relation' }, '关系错误'), h('option', { value: 'generic' }, '类型错误'), h('option', { value: 'invalid' }, '损坏诊断')])]),
    h(ExecutionReturnedText, { response: { id: 'fixture', attemptId: 'fixture', assetJobId:null, correctedText:null, correctedSha256:null, confirmedBy:null, originalSha256: 'fixture', editable: false, confirmedAt: null, readonlyReason: '隔离只读', notices: [], fields: [],
      originalText: '{"chapters":[{"frames":[{"uses":[{"variant_key":"未声明变体"}]}]}]}',
      diagnostics: { summary: '返回内容需要修正；原文完整保留。', ...(mode.value === 'relation' ? {
        structure_errors: { [path]: [message], [source]: [message] }, relation_errors: [{ path, source_path: source, origin_kind: 'path', message, code: 'variant_unknown' }],
      } : mode.value === 'generic' ? { structure_errors: { 'response.duration': ['不符合结构约束：integer'] } } : { structure_errors: false }) },
    } }),
  ])
} }).mount('#app')
