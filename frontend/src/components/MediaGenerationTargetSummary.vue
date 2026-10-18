<script setup lang="ts">
import { computed } from 'vue'
import type { JsonObject } from '@/services/platformTypes'
import { readMediaGenerationTarget } from '@/services/mediaGenerationTarget'

const props = defineProps<{ resolvedInputs: JsonObject; versionId: string | null }>()
const state = computed(() => readMediaGenerationTarget(props.resolvedInputs, props.versionId))
const target = computed(() => state.value.kind === 'recorded' ? state.value.target : null)
const mediaLabels = { image: '图片', video: '视频', audio: '音频' }
</script>

<template>
  <section v-if="state.kind !== 'missing'" class="media-target-summary" aria-label="本次媒体生成目标">
    <h4>本次媒体生成目标</h4>
    <p v-if="state.kind === 'invalid'" class="platform-alert danger" role="alert">{{ state.message }}</p>
    <template v-else-if="target">
      <dl>
        <div><dt>目标类型</dt><dd>{{ mediaLabels[target.mediaType] }} · {{ target.appearance?.role === 'primary' ? '主图' : target.appearance?.role === 'variant' ? '变体' : '未声明主图/变体' }}</dd></div>
        <div><dt>候选规则</dt><dd>计划 {{ target.candidates.count }} 个候选 · {{ target.candidates.evaluator === 'first_succeeded' ? '首个成功候选（自动规则）' : '未配置自动选择' }}</dd></div>
        <div><dt>关联设定</dt><dd>{{ target.setting ? `确切设定修订 ${target.setting.revision}` : '本次目标未关联设定' }}；不随当前设定变化。</dd></div>
        <div v-if="target.appearance?.primary"><dt>变体基准</dt><dd>已记录确切主图版本，不使用最新主图替换。
          <p>逻辑输入：{{ target.appearance.primaryInput?.inputKey }} · 输入项：{{ target.appearance.primaryInput?.itemKey }}</p>
        </dd></div>
        <div v-if="target.appearance?.difference"><dt>差异来源</dt><dd>{{ target.appearance.difference.kind === 'manual_channel' ? '原请求人工提示词通道' : target.appearance.difference.kind === 'reference_field' ? '原结构化参考字段（正文贡献来源）' : target.appearance.difference.kind === 'structured_field' ? '原结构化直通字段' : '原解析文字输入（策略正文映射）' }}：{{ target.appearance.difference.key }}。
          <p v-if="target.appearance.difference.kind === 'reference_field'">单元：{{ target.appearance.difference.unitKey }} · 字段：{{ target.appearance.difference.fieldKey }}。此处记录生成前来源，不是已选辅助结果。</p>
          <p v-else-if="target.appearance.difference.kind === 'structured_field'">字段：{{ target.appearance.difference.fieldKey }}。路径由本次确切策略指定，不是另填的差异正文。</p>
          正文保留在原来源，不另存副本。</dd></div>
      </dl>
      <p>来自本次已保存的准备，不代表调用成功、产物已生成或已采用。候选规则不是人工审核记录。</p>
      <p v-if="target.appearance?.role === 'variant'">已核对准备中的主图端口映射与非空差异来源，不代表模型已收到请求；动态配方履约与结果登记尚未完成。</p>
      <details><summary>查看确切版本与摘要（技术信息）</summary>
        <dl>
          <div><dt>执行实例版本</dt><dd>{{ target.versionId }}</dd></div>
          <div><dt>定义摘要</dt><dd>{{ target.definitionSha256 }}</dd></div>
          <div><dt>输入摘要</dt><dd>{{ target.inputsSha256 }}</dd></div>
          <template v-if="target.setting">
            <div><dt>设定身份</dt><dd>{{ target.setting.appearanceId }}</dd></div>
            <div><dt>设定修订标识</dt><dd>{{ target.setting.revisionId }}</dd></div>
            <div><dt>设定内容摘要</dt><dd>{{ target.setting.sha256 }}</dd></div>
          </template>
          <div v-if="target.appearance?.difference"><dt>差异文字摘要</dt><dd>{{ target.appearance.difference.sha256 }}</dd></div>
          <template v-if="target.appearance?.primary">
            <div><dt>主图系列</dt><dd>{{ target.appearance.primary.seriesId }}</dd></div>
            <div><dt>主图版本</dt><dd>{{ target.appearance.primary.versionId }}</dd></div>
            <div><dt>主图素材</dt><dd>{{ target.appearance.primary.assetId }}</dd></div>
            <div><dt>主图文件摘要</dt><dd>{{ target.appearance.primary.sha256 }}</dd></div>
          </template>
        </dl>
      </details>
    </template>
  </section>
</template>

<style scoped>
.media-target-summary{min-width:0;border:1px solid var(--border);border-radius:8px;padding:14px;display:grid;gap:10px;font-size:13px;line-height:1.7}.media-target-summary h4,.media-target-summary p,.media-target-summary dl{margin:0}.media-target-summary dl>div{display:grid;grid-template-columns:minmax(90px,140px) minmax(0,1fr);gap:12px;padding:5px 0}.media-target-summary dt{color:var(--text-secondary)}.media-target-summary dd{margin:0;min-width:0;overflow-wrap:anywhere}.media-target-summary summary{cursor:pointer;overflow-wrap:anywhere}.media-target-summary summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}@media(max-width:640px){.media-target-summary dl>div{grid-template-columns:minmax(0,1fr);gap:2px}}
</style>
