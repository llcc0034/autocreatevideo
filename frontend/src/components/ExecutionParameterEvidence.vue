<script setup lang="ts">
import { computed, ref } from 'vue'
import { executionRuleDescription, executionValueRule } from '@/services/executionRequestForm'

const props = defineProps<{ entry: unknown; resolved?: boolean; actualSource?: unknown }>()
const expanded = ref(false)
const record = (value: unknown): Record<string, unknown> | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
const entry = computed(() => record(props.entry))
// Presentation only: the backend intersects rules and resolves values. Never
// reconstruct missing layers or a historical value from current catalog data.
const describe = (value: unknown) => {
  try { return executionRuleDescription(executionValueRule(value, '参数约束')) }
  catch { return null }
}
const effective = computed(() => describe(entry.value?.rule))
const layers = computed(() => {
  const rows = entry.value?.layers
  if (!Array.isArray(rows) || !rows.length) return null
  const sources = new Set<string>()
  const result: { source: string; summary: string }[] = []
  for (const value of rows) {
    const row = record(value), summary = describe(row?.rule)
    if (!row || typeof row.source !== 'string' || !row.source.trim() || sources.has(row.source) || !summary) return null
    sources.add(row.source); result.push({ source: row.source, summary })
  }
  return result
})
function sourceLabel(source: unknown): string {
  if (source === 'request') return '本次请求填写'
  if (source === 'preparation.generated') return '首次准备时生成并保存；继续与重试不重新生成'
  if (source === 'workflow.static_semantics') return '工作流冻结静态事实，不是可调参数'
  if (typeof source !== 'string' || !source.trim()) return '来源记录缺失，不能推断'
  if (/^system\.execution\.resources\.review_policy(?:@[^@]+)?$/.test(source)) return '系统设置 → 默认值与限制 → 预算、调用次数与输出额度'
  if (/^model\.[^.]+\.media_constraints$/.test(source)) return '精确模型版本 → 媒体能力约束'
  if (/^model\.[^.]+\.async_media\./.test(source)) return '精确模型版本 → 受限 REST 请求合同'
  if (/^model\.[^.]+\./.test(source)) return '精确模型版本 → 执行能力合同'
  if (/^workflow\.[^.]+\./.test(source)) return '精确工作流版本 → 参数／Projector 合同'
  if (/^instance\.[^.]+\.parameters$/.test(source)) return '当前精确执行实例版本 → 参数范围与固定值'
  if (/^step\.parameter_policies\./.test(source)) return '生产步骤 → 参数策略（如固定来源，参见完整来源记录）'
  return '合同记录的来源（未登记页面定位）'
}
const presetSource = computed(() => entry.value?.value_source)
const noPreset = computed(() => presetSource.value === null)
const generated = computed(() => record(entry.value?.rule)?.seed_policy === 'generate_once')
</script>

<template>
  <div class="parameter-evidence">
    <p v-if="effective"><strong>生效约束：</strong>{{ effective }}</p>
    <p v-else class="evidence-warning" role="status">生效约束记录缺失或不完整；不能以当前配置补算。</p>
    <p v-if="resolved"><strong>本次值来源：</strong>{{ sourceLabel(actualSource) }}<code v-if="typeof actualSource==='string'">{{ actualSource }}</code></p>
    <p v-else-if="noPreset">{{ generated ? '尚未生成种子；创建准备时由服务端生成并保存。' : presetSource===null && layers?.some(row=>row.source==='system.execution.resources.review_policy') ? '新请求额度在创建准备时读取系统配置并冻结；不是实例默认值。' : '合同未提供默认值或固定值来源；必填项需要在请求中提供。' }}</p>
    <p v-else><strong>合同默认／固定值来源：</strong>{{ sourceLabel(presetSource) }}<code v-if="typeof presetSource==='string'">{{ presetSource }}</code></p>
    <details @toggle="expanded=($event.target as HTMLDetailsElement).open">
      <summary>约束来源与优先关系<template v-if="layers"> · {{ layers.length }} 层</template></summary>
      <div v-if="expanded" class="evidence-body">
        <p>范围、枚举、对齐规则取交集，固定值不能被覆盖；只有默认值按下列顺序由后层提供。不能靠人工确认突破模型或工作流的物理约束。</p>
        <p v-if="!layers" class="evidence-warning" role="status">来源层记录缺失或格式不完整，无法说明交集来源；不会用生效规则伪造来源层。</p>
        <ol v-else><li v-for="layer in layers" :key="layer.source"><strong>{{ sourceLabel(layer.source) }}</strong><code>{{ layer.source }}</code><p>{{ layer.summary }}</p></li></ol>
        <p>这里的来源标识用于追溯约束，不是工作流节点/API 字段的物理路径。物理映射以冻结工作流／部署合同及实际请求为准，不按参数名猜测。</p>
      </div>
    </details>
  </div>
</template>

<style scoped>
.parameter-evidence{min-width:0;display:grid;gap:6px;font-size:12px;line-height:1.7}.parameter-evidence p{margin:0;color:var(--text-secondary);overflow-wrap:anywhere}.parameter-evidence code{display:block;overflow-wrap:anywhere;color:var(--text-secondary);font-size:11px}.parameter-evidence summary{cursor:pointer;min-height:36px;align-content:center;overflow-wrap:anywhere}.parameter-evidence summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.evidence-body{display:grid;gap:8px}.evidence-body ol{margin:0;padding-left:20px}.evidence-body li{padding:6px 0;overflow-wrap:anywhere}.parameter-evidence .evidence-warning{color:var(--warning,var(--accent))}
</style>
