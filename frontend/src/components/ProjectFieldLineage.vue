<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { ProductionRun } from '@/services/platformTypes'
import type { SeriesProject } from '@/types'
import type { VisualRuleReviewContract } from '@/services/visualRuleReviewContract'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { fieldConsumers, lineageTaskTarget, projectVisualFields, visualLineageContract, visualReviewEvidence } from '@/services/projectFieldLineage'

const props = defineProps<{ run?: ProductionRun; project: SeriesProject; savedProject?: SeriesProject }>()
const open = ref(false), selected = ref<string>('negative_prompt'), loading = ref(false), error = ref('')
const contract = ref<VisualRuleReviewContract | null>(null)
const evidence = computed(() => {
  try { return { value: props.run ? visualReviewEvidence(props.run, props.project.id) : null, error: '' } }
  catch (e) { return { value: null, error: e instanceof Error ? e.message : '无法读取来源' } }
})
const fields = computed(() => [...projectVisualFields, ...(contract.value?.fields || [])
  .filter(field => !projectVisualFields.some(row => row.key === field.key))
  .map(field => ({ key: field.key, label: field.label, type: '文本', path: '本轮影视规则采用记录（不写入项目基础字段）' }))])
const field = computed(() => fields.value.find(row => row.key === selected.value) || fields.value[0]!)
const definition = computed(() => contract.value?.fields.find(row => row.key === field.value.key))
const consumers = computed(() => props.run && props.run.project === props.project.id && definition.value
  ? fieldConsumers(props.run, definition.value.source_selector) : null)
const ruleValue = computed(() => evidence.value.value?.rules[field.value.key])
function projectValue(project: SeriesProject | undefined): unknown {
  if (!project) return undefined
  if (field.value.key === 'negative_prompt') return project.bible.negativePrompt
  if (field.value.key === 'color_script') return project.bible.colorScript
  if (field.value.key === 'continuity_summary') return project.continuitySummary
  return undefined
}
const comparison = computed(() => {
  const saved = projectValue(props.savedProject), draft = projectValue(props.project)
  if (saved !== undefined && draft !== saved && typeof draft === 'string') return '此字段有未保存编辑；保存不会改写本轮采用记录。'
  if (saved === undefined || typeof ruleValue.value !== 'string') return '没有足够的已保存值与采用凭据进行比较。'
  return saved === ruleValue.value ? '项目保存值与本轮采用值一致；相同值不代表最后写入来源相同。' : '项目保存值与本轮采用值不同；下游若绑定本轮已审核来源，不会自动读取这里的新值。'
})
let request = 0
async function load() {
  const ticket = ++request
  contract.value = null; error.value = ''; loading.value = false
  if (!open.value || !props.run || evidence.value.error) return
  const original = props.run, review = evidence.value.value?.review
  if (!review) return
  if (typeof review.artifact_id !== 'string' || !review.artifact_id) { error.value = '本轮采用记录未保存原产物标识，无法追溯冻结字段定义。'; return }
  loading.value = true
  try {
    const artifact = await platformApi.getArtifact(review.artifact_id)
    if (ticket !== request) return
    contract.value = visualLineageContract(original, props.project.id, artifact)
  } catch (e) { if (ticket === request) error.value = apiErrorSummary(e, '字段来源读取失败') }
  finally { if (ticket === request) loading.value = false }
}
watch(() => JSON.stringify([open.value, props.project.id, props.run?.id, props.run?.executionPlan?.sha256,
  props.run?.stages?.map(row => [row.id, row.status, row.outputSnapshotSha256])]), load)
watch(fields, () => { if (!fields.value.some(row => row.key === selected.value)) selected.value = 'negative_prompt' })
onBeforeUnmount(() => { request++ })
</script>

<template>
  <section class="field-lineage" aria-label="字段定义与流向">
    <header><div><strong>字段定义与流向</strong><p>字段可以先是生成结果，再成为下游输入。这里区分当前编辑值与本轮采用的历史值。</p></div>
      <button type="button" class="button ghost small" :aria-expanded="open" @click="open = !open">{{ open ? '收起字段流向' : '查看字段定义与流向' }}</button></header>
    <div v-if="open" class="lineage-body">
      <label class="field lineage-select">查看字段<select v-model="selected"><option v-for="item in fields" :key="item.key" :value="item.key">{{ item.label }}</option></select></label>
      <p v-if="loading" role="status">正在读取原产物的字段定义…</p>
      <p v-if="evidence.error || error" class="platform-alert danger" role="alert">{{ evidence.error || error }} <button v-if="!evidence.error" type="button" class="button ghost small" @click="load">重新读取来源</button></p>
      <ol class="lineage-grid">
        <li><span class="eyebrow">01 / 字段定义</span><h4>{{ field.label }}</h4><p>{{ field.type }}</p>
          <template v-if="definition && contract"><p>原业务输出：{{ contract.output.name }} · v{{ contract.output.version }}</p><p>输出中的字段名称：{{ definition.label }}</p>
            <details><summary>查看精确字段与转换规则</summary><dl><dt>输出版本</dt><dd>{{ contract.output.id }}</dd><dt>模型返回路径</dt><dd>{{ definition.response_path }}</dd><dt>保存前转换</dt><dd>{{ definition.transformation }}</dd><dt>审核后来源</dt><dd>{{ definition.source_selector }}</dd><dt>值要求</dt><dd>{{ definition.allow_blank ? '允许空文本' : '需要非空文本' }}</dd><dt>版本摘要</dt><dd>{{ contract.output.sha256 }}</dd></dl></details>
          </template>
          <p v-else>项目字段类型已知；{{ loading ? '正在核对' : '尚未获得' }}本轮原输出的字段定义，不按名称套用当前目录。</p>
        </li>
        <li><span class="eyebrow">02 / 上游来源</span><h4>生成 → 采用</h4>
          <template v-if="evidence.value"><p>本轮影视规则已保存采用记录；原生成候选和采用后的值分别保留。</p>
            <RouterLink v-if="run && contract" :to="lineageTaskTarget(run, String(evidence.value.review.generation_stage_id), String(evidence.value.review.preparation_id))">查看原生成任务与实际输入</RouterLink>
            <RouterLink v-else-if="run" :to="lineageTaskTarget(run, evidence.value.stage.id)">查看采用阶段记录</RouterLink>
            <details v-if="typeof ruleValue === 'string'"><summary>查看本轮采用值</summary><p class="saved-value">{{ ruleValue || '（空文本）' }}</p></details>
          </template>
          <p v-else>{{ evidence.error ? '来源异常，不能归因。' : '本轮尚无成功的影视规则采用凭据。项目可能有人工输入或其他版本结果，不能据此宣称由本轮生成。' }}</p>
        </li>
        <li><span class="eyebrow">03 / 保存位置</span><h4>{{ projectVisualFields.some(row => row.key === field.key) ? '项目当前输入' : '本轮采用记录' }}</h4><code>{{ field.path }}</code>
          <p>{{ comparison }}</p>
          <p>此面板只读，不保存、不重新生成、不自动采用。</p>
        </li>
        <li><span class="eyebrow">04 / 下游使用</span><h4>本轮输入绑定</h4>
          <template v-if="consumers"><p>{{ consumers.note }}</p><ul class="consumer-list"><li v-for="(row, index) in consumers.rows" :key="index"><strong>{{ row.stageName }} → {{ row.inputKey }}</strong><small>{{ row.route }}</small><code>{{ row.target }}</code><details><summary>精确执行实例版本</summary><code>{{ row.versionId }}</code></details></li></ul></template>
          <p v-else>缺少本轮原输出来源定义，无法确认下游绑定；不依据字段名称推测接线。</p>
          <RouterLink v-if="run && run.project === project.id" :to="lineageTaskTarget(run)">查看本轮任务的实际输入与提示词</RouterLink>
        </li>
      </ol>
    </div>
  </section>
</template>

<style scoped>
.field-lineage{border:1px solid var(--border);border-radius:12px;padding:16px;margin:16px 0;min-width:0;background:var(--surface-2)}
header{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}header p{margin:6px 0 0}p,small,dt{color:var(--text-muted)}
.lineage-body{margin-top:16px}.lineage-select{max-width:360px}.lineage-grid{list-style:none;padding:0;margin:16px 0 0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.lineage-grid>li{border:1px solid var(--border);border-radius:10px;padding:16px;min-width:0}.lineage-grid h4{font-size:16px;margin:10px 0}.lineage-grid p{font-size:14px;line-height:1.65}
summary,a{font-size:14px}summary{cursor:pointer}details{margin-top:10px}code,dd,.consumer-list strong{overflow-wrap:anywhere;white-space:normal}dd{margin:4px 0 12px}code{font-size:12px}dt,dd{font-size:13px}.saved-value{white-space:pre-wrap;max-height:220px;overflow:auto}
.consumer-list{list-style:none;padding:0;display:grid;gap:12px}.consumer-list li{display:grid;gap:5px;border-top:1px solid var(--border);padding-top:10px}.consumer-list small{font-size:12px}
@media(max-width:720px){.lineage-grid{grid-template-columns:minmax(0,1fr)}.field-lineage{padding:12px}.lineage-select{max-width:100%}}
</style>
