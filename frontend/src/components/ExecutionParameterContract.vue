<script setup lang="ts">
import { computed, ref } from 'vue'
import ExecutionParameterEvidence from './ExecutionParameterEvidence.vue'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
const props = defineProps<{ interface: unknown }>()
const expanded = ref(false)
const record = (value: unknown): Record<string, unknown> | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
const intf = computed(() => record(props.interface))
const parameters = computed(() => record(intf.value?.parameters))
const coupled = computed(() => [
  { key: 'media_constraints', label: '模型媒体整体约束（含宽高、短长边、帧率与时长）' },
  { key: 'media_semantic_keys', label: '媒体语义 → 参数标识' },
  { key: 'resolution_strategy', label: '工作流尺寸推导策略' },
  { key: 'frame_policy', label: '工作流帧数与时长关系' },
  { key: 'static_semantics', label: '工作流冻结静态事实（不是可调参数）' },
].filter(row => intf.value?.[row.key] != null))
</script>

<template>
  <details class="parameter-contract" @toggle="expanded=($event.target as HTMLDetailsElement).open">
    <summary>参数生效约束与来源 · {{ parameters ? `${Object.keys(parameters).length} 项` : '数据不完整' }}</summary>
    <div v-if="expanded" class="contract-body">
      <p>读取本次合同的服务端合并结果，不按当前模型配置重算。单个参数满足范围，不代表宽高、帧数和时长等组合约束已经满足。</p>
      <p v-if="!parameters" role="status">参数合同未记录，不能判定为没有参数。</p>
      <p v-else-if="!Object.keys(parameters).length">此合同明确没有可调标量参数。</p>
      <section v-for="(entry,key) in parameters" :key="key"><h4>{{ key }}</h4><ExecutionParameterEvidence :entry="entry"/></section>
      <details v-if="coupled.length"><summary>整体约束与语义接线（只读）</summary><ReadonlyDataValue v-for="row in coupled" :key="row.key" :value="intf?.[row.key]" :label="row.label"/></details>
    </div>
  </details>
</template>

<style scoped>
.parameter-contract{border-top:1px solid var(--border);min-width:0;font-size:12px;line-height:1.7}.parameter-contract summary{cursor:pointer;min-height:36px;align-content:center;overflow-wrap:anywhere}.parameter-contract summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.contract-body{display:grid;gap:12px;min-width:0}.contract-body p{margin:0;color:var(--text-secondary)}.contract-body section{border-top:1px solid var(--border);padding:10px 0;min-width:0}.contract-body h4{margin:0 0 6px;overflow-wrap:anywhere}
</style>
