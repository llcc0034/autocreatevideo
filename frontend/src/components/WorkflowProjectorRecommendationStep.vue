<script setup lang="ts">
import { computed } from 'vue'
import { ArrowRight, Settings2 } from 'lucide-vue-next'

const props = defineProps<{
  scanState: 'idle' | 'loading' | 'fresh' | 'partial' | 'failed'
  scannedCount: number
  candidates: { key: string; label: string }[]
  selectedKey: string
  busy: boolean
  recommending: boolean
  unavailableReason: string
}>()
const emit = defineEmits<{ select: [key: string]; recommend: []; scan: [] }>()
const actionable = computed(() => !props.unavailableReason && ['fresh', 'partial'].includes(props.scanState))
const hasCandidates = computed(() => actionable.value && props.candidates.length > 0)
const selectedAvailable = computed(() => props.candidates.some(item => item.key === props.selectedKey))
const canRecommend = computed(() => !props.busy && hasCandidates.value && selectedAvailable.value)
const canGoToScan = computed(() => !props.busy && props.scanState !== 'loading' && !props.unavailableReason)
const status = computed(() => {
  if (props.unavailableReason) return { title: '暂不能从工作流推荐', detail: props.unavailableReason, warning: true }
  if (props.scanState === 'loading') return { title: '正在扫描工作流…', detail: '扫描完成后才能选择候选，请稍候。', warning: false }
  if (props.scanState === 'failed') return { title: '扫描未成功，暂不能生成推荐', detail: '请前往扫描区查看失败原因并重试；旧扫描结果不会用于推荐。', warning: true }
  if (props.scanState === 'idle') return { title: '尚未扫描工作流', detail: '先前往扫描区读取工作流目录，再纳管候选并确认主输出。', warning: false }
  if (props.scanState === 'partial') return { title: '部分来源扫描失败', detail: hasCandidates.value ? '可选择成功来源中已就绪的候选；其余来源请前往扫描区查看原因。' : '成功来源中暂无就绪候选；请前往扫描区检查失败来源、纳管状态和主输出。', warning: true }
  if (!props.scannedCount) return { title: '扫描完成，未发现工作流', detail: '请前往扫描区检查目录配置和来源，再重新扫描。', warning: false }
  if (!hasCandidates.value) return { title: '候选尚未就绪', detail: '已扫描到工作流，但尚无满足推荐条件的候选；请检查纳管状态、主输出及候选错误。', warning: true }
  return { title: `已有 ${props.candidates.length} 个候选可生成推荐`, detail: '选择候选后点击“生成推荐配置”，再复核节点、参数和素材规则。', warning: false }
})
</script>

<template>
  <div class="projector-recommendation-step">
    <div class="recommendation-status" :class="{warning:status.warning}" role="status" aria-live="polite">
      <strong>{{ status.title }}</strong><p>{{ status.detail }}</p>
    </div>
    <label v-if="hasCandidates">已纳管且已确认主输出的工作流
      <select :value="selectedKey" :disabled="busy" @change="!busy && emit('select', ($event.target as HTMLSelectElement).value)">
        <option value="">请选择工作流候选</option><option v-for="item in candidates" :key="item.key" :value="item.key">{{ item.label }}</option>
      </select>
    </label>
    <div class="recommendation-actions">
      <button v-if="hasCandidates" type="button" class="button primary small" :disabled="!canRecommend" @click="canRecommend && emit('recommend')"><Settings2 :size="14"/>{{ recommending ? '正在生成推荐…' : '生成推荐配置' }}</button>
      <button type="button" class="button small" :class="hasCandidates ? 'ghost' : 'primary'" :disabled="!canGoToScan" @click="canGoToScan && emit('scan')"><ArrowRight :size="14"/>{{ hasCandidates ? '查看扫描候选' : '前往扫描工作流' }}</button>
      <span v-if="hasCandidates && !selectedAvailable" class="hint">{{ selectedKey ? '原候选已不可推荐，请重新选择。' : '请先选择一个工作流候选。' }}</span>
    </div>
    <p class="hint">切换初始化方式不会扫描或运行工作流。前往扫描区会保留本页填写内容；关闭或刷新页面不保留。</p>
  </div>
</template>

<style scoped>
.projector-recommendation-step{display:grid;gap:12px;min-width:0;font-size:12px;line-height:1.6}.recommendation-status{border-left:3px solid var(--border-strong);padding:8px 12px;background:var(--surface);min-width:0}.recommendation-status.warning{border-color:var(--accent)}.recommendation-status p,.hint{margin:4px 0 0;color:var(--text-secondary);overflow-wrap:anywhere}.recommendation-status strong{color:var(--text)}label{display:grid;gap:6px;color:var(--text-secondary);min-width:0}select{min-width:0;width:100%;min-height:38px;padding:8px;border:1px solid var(--border-strong);border-radius:6px;background:var(--surface);color:var(--text)}.recommendation-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.recommendation-actions .hint{font-size:11px}button:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}@media(max-width:640px){.recommendation-actions{align-items:stretch;flex-direction:column}.recommendation-actions button{justify-content:center;white-space:normal}}
</style>
