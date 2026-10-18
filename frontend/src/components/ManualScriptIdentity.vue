<script setup lang="ts">
import type { ManualActionOptions } from '@/services/productionManual'
import { taskCenterTarget, taskTime } from '@/services/taskCenter'
import { stageLabel } from '@/services/presentationLabels'
defineProps<{ options: ManualActionOptions }>()
</script>

<template>
  <section class="script-identity" aria-label="本轮采用剧本">
    <template v-if="options.currentScript && options.currentScriptIdentity">
      <strong>{{ options.currentScriptIdentity.sourceName || '未命名原稿' }} · 修订 {{ options.currentScript.revision }}</strong>
      <p>{{ options.currentScriptIdentity.sourceTypeLabel }} · {{ options.currentScriptIdentity.sourceFormatLabel }} · 已确认于 {{ taskTime(String(options.currentScript.confirmed_at)) }}</p>
      <p v-if="options.currentScriptIdentity.candidate">本轮已审核并采用 {{ stageLabel(options.currentScriptIdentity.candidate.stageKey).split('（')[0].trim() }} 的候选 {{ options.currentScriptIdentity.candidate.variantIndex }}{{ options.currentScriptIdentity.candidate.editedFromRevisionId ? ' 的人工编辑修订（不是模型原文）' : '' }}，无需再次采用同一修订。</p>
      <p v-else>本轮已明确采用这个已确认修订；不是重新生成或重新审核候选。</p>
      <RouterLink v-if="options.currentScriptIdentity.candidate" class="button ghost small" :to="taskCenterTarget({kind:'preparation',id:options.currentScriptIdentity.candidate.preparationId,runId:options.runId,stageId:options.currentScriptIdentity.candidate.stageId,category:'production'})">查看该剧本的原调用</RouterLink>
      <details><summary>候选、原稿、修订与采用决定的对应关系</summary>
        <dl>
          <template v-if="options.currentScriptIdentity.candidate"><dt>候选产物 ID（模型返回的候选）</dt><dd>{{ options.currentScriptIdentity.candidate.artifactId }}</dd></template>
          <dt>原稿来源 ID（来源容器）</dt><dd>{{ options.currentScript.source_id }}</dd>
          <dt>修订 ID（本轮使用的内容版本）</dt><dd>{{ options.currentScript.id }}</dd>
          <template v-if="options.currentScriptIdentity.candidate?.editedFromRevisionId"><dt>编辑依据修订 ID（原模型候选）</dt><dd>{{ options.currentScriptIdentity.candidate.editedFromRevisionId }}</dd></template>
          <dt>采用决定 ID（本轮明确选择）</dt><dd>{{ options.currentScriptDecisionId }}</dd>
          <dt>正文 SHA-256（内容指纹，不是候选 ID）</dt><dd>{{ options.currentScript.sha256 }}</dd>
        </dl><p>这些编号属于不同对象，不应相同。仅内容指纹相同也不代表同一个候选或同一次采用。</p>
      </details>
    </template>
    <p v-else-if="!options.currentScript">本轮尚未采用剧本。请审核生成候选，或选择一个已确认的剧本修订。</p>
    <p v-else role="alert">采用身份摘要缺失，请重新读取制作状态。</p>
  </section>
</template>

<style scoped>
.script-identity{display:grid;gap:10px;min-width:0}.script-identity p{margin:0}.script-identity dl{display:grid;gap:6px;min-width:0}.script-identity dd{margin:0 0 8px;overflow-wrap:anywhere}.script-identity .button{justify-self:start}.script-identity strong{overflow-wrap:anywhere}
</style>
