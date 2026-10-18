<script setup lang="ts">
import { computed } from 'vue'
import type { WorkflowSmokeAttempt } from '@/services/platformTypes'

const props = defineProps<{ attempt: WorkflowSmokeAttempt }>()
// This legacy fixed-slot list is not the dynamic media input contract. The
// authoritative per-use input stays in the linked Preparation workbench.
const stagedCount = computed(() => {
  const evidence = props.attempt.result.evidence
  if (!evidence || typeof evidence !== 'object' || Array.isArray(evidence)) return null
  const count = 'staged_input_count' in evidence ? evidence.staged_input_count : undefined
  return Number.isSafeInteger(count) && Number(count) >= 0 ? Number(count) : null
})
const inputLink = computed(() => props.attempt.preparationId
  ? { name: 'workflows', query: { category: 'smoke', kind: 'preparation', id: props.attempt.preparationId } }
  : null)
</script>

<template>
  <section class="smoke-media-facts" aria-label="本次烟测素材来源">
    <strong>固定槽位绑定记录</strong>
    <ul>
      <li v-for="binding in attempt.assetBindings" :key="binding.semanticKey"><code>{{ binding.semanticKey }}</code> · slot {{ binding.inputSlot }} · {{ binding.mimeType }} · {{ binding.checksum.slice(0, 12) }}</li>
      <li v-if="!attempt.assetBindings.length">未记录固定槽位绑定；不代表没有动态参考输入。</li>
    </ul>
    <p v-if="stagedCount !== null">执行暂存回执：{{ stagedCount }} 项。该数量不是素材身份、用途或槽位明细。</p>
    <p v-else>尚无可核对的媒体暂存数量；不按零项处理。</p>
    <RouterLink v-if="inputLink" :to="inputLink" class="button ghost tiny">查看本次完整输入、动态参考与调用记录</RouterLink>
    <p v-else role="status">本次任务缺少准备记录关联，无法核对完整输入。</p>
  </section>
</template>

<style scoped>
.smoke-media-facts{min-width:0;overflow-wrap:anywhere}.smoke-media-facts p{font-size:12px;line-height:1.7;color:var(--text-secondary)}.smoke-media-facts ul{padding-left:20px}.smoke-media-facts .button{white-space:normal;text-align:left}
</style>
