<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { stageLabel } from '@/services/presentationLabels'
import { changeReferenceLabels, changeReferenceKindLabels, type ManualChangeImpact } from '@/services/productionManualChangeImpact'
import PlatformStatusPill from './PlatformStatusPill.vue'
const props = defineProps<{ runId: string; runPlanSha256: string; stageKeys: string[] }>()
const stageKey = ref(''), data = ref<ManualChangeImpact | null>(null), busy = ref(false), error = ref('')
let serial = 0
async function load(offset = 0) {
  if (busy.value || !props.stageKeys.includes(stageKey.value)) return
  const token = ++serial, runId = props.runId, selected = stageKey.value
  busy.value = true; error.value = ''
  try {
    const result = await platformApi.getManualChangeImpact(runId, selected, offset)
    if (token !== serial) return
    if (result.runId !== runId || result.source.stageKey !== selected || result.runPlanSha256 !== props.runPlanSha256) throw new Error('影响结果不属于当前运行、内容步骤或冻结计划，请刷新运行详情')
    data.value = result
  } catch (e) { if (token === serial) error.value = apiErrorSummary(e) }
  finally { if (token === serial) busy.value = false }
}
watch(() => [props.runId, props.runPlanSha256, stageKey.value], () => { serial++; busy.value = false; data.value = null; error.value = '' })
onBeforeUnmount(() => { serial++ })
</script>

<template>
  <section class="manual-change-impact">
    <p>先选择准备改选的内容。这里只查看本轮后续步骤和已记录引用，不更换内容、不取消任务，也不发起模型调用。</p>
    <label class="platform-field"><span>准备改选的内容步骤</span><select v-model="stageKey" :disabled="busy">
      <option value="">请选择内容步骤</option><option v-for="key in stageKeys" :key="key" :value="key">{{ stageLabel(key) }}</option>
    </select></label>
    <button class="button ghost small" :disabled="busy || !stageKey" @click="load()">{{ busy ? '正在核对记录…' : '查看改选影响' }}</button>
    <p v-if="error" class="platform-alert danger" role="alert">{{ error }}。下方若有记录为上次查询结果。</p>
    <p v-if="busy" role="status">正在读取阶段、冻结输入与调用记录…</p>
    <template v-if="data">
      <p>核对时间：{{ new Date(data.checkedAt).toLocaleString() }} · 当前采用修订 {{ data.source.revision }}</p>
      <p v-if="data.runReadOnly || data.assemblyLocked" class="platform-alert warning">{{ data.runReadOnly ? '本轮已结束，只能查看。' : '本轮已提交装配，选片已冻结。' }}需要更换内容时，请新建人工制作轮次。</p>
      <p v-else class="platform-alert" :class="data.replacement.allowed ? 'info' : 'warning'" role="status">{{ data.replacement.reason }}</p>
      <p v-if="data.stages.length">后续 {{ data.stages.length }} 个步骤，共 {{ data.count }} 份执行准备。</p>
      <p v-if="data.replacement.allowed">这是核对时的状态；请返回对应内容的采用/审核入口提交，服务端会再次核对。此页不会直接更换。</p>
      <p>“已记录引用”表示冻结输入或写回基线引用了所选内容，不等于已经失效。“需人工核对”表示没有查到直接引用，仍可能间接依赖或使用人工提示词，不能据此认定没有影响。</p>
      <details v-if="data.stages.length"><summary>查看后续步骤（{{ data.stages.length }}）</summary>
        <ul><li v-for="stage in data.stages" :key="stage.id">{{ stageLabel(stage.stageKey) }} · <PlatformStatusPill :status="stage.status"/> · {{ stage.preparationCount }} 份准备</li></ul>
      </details>
      <article v-for="item in data.results" :key="item.id" class="platform-card">
        <header><strong>{{ stageLabel(item.stageKey) }}</strong> · <PlatformStatusPill :status="item.status"/> · {{ changeReferenceLabels[item.referenceState] }}</header>
        <p>制作单元：{{ item.unitKey }}<br>执行准备：{{ item.id }}</p>
        <ul v-if="item.references.length"><li v-for="ref in item.references" :key="`${ref.inputPath}:${ref.kind}`">{{ ref.inputPath }} · {{ changeReferenceKindLabels[ref.kind] }}</li></ul>
        <p v-if="item.referenceState === 'unverifiable'" class="platform-alert danger">来源详情缺失、损坏或类型未登记。请查看原记录，当前不能判断引用关系。</p>
        <p v-if="!item.attempts.length">尚未创建模型调用。</p>
        <ul v-else><li v-for="attempt in item.attempts" :key="`${attempt.role}:${attempt.status}`">{{ attempt.role === 'primary' ? '主调用' : '辅助调用' }} · <PlatformStatusPill :status="attempt.status"/> · {{ attempt.count }} 次</li></ul>
      </article>
      <div v-if="data.count" class="manual-controls">
        <span>执行准备 {{ Math.min(data.offset + 1, data.count) }}–{{ Math.min(data.offset + data.results.length, data.count) }} / {{ data.count }}</span>
        <button class="button ghost small" :disabled="busy || data.offset === 0" @click="load(Math.max(0, data.offset - data.limit))">上一页</button>
        <button class="button ghost small" :disabled="busy || data.nextOffset === null" @click="load(data.nextOffset!)">下一页</button>
      </div>
    </template>
  </section>
</template>

<style scoped>
.manual-change-impact { display: grid; gap: 1rem; }
.manual-change-impact .platform-card { padding: 1rem; overflow-wrap: anywhere; }
.manual-change-impact header { display: flex; align-items: center; flex-wrap: wrap; gap: .4rem; }
.manual-change-impact ul { padding-left: 1.4rem; }
</style>
