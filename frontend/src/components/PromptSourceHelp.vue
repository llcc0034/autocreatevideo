<script setup lang="ts">
import { computed } from 'vue'
import type { PromptSourceOption } from '@/services/platformTypes'
const props = defineProps<{ selector: string; sources?: PromptSourceOption[]; fieldLabel?: string; fieldKey?: string; channel?: string; generationMode?: string; generationOutputSemantics?: string }>()
const source = computed(() => props.sources?.find(item => item.value === props.selector))
const usesAuxiliaryResult = computed(() => ['model_generated', 'translate_only'].includes(props.generationMode || ''))
</script>

<template>
  <div class="source-help" aria-label="内容来源说明">
    <p v-if="fieldKey"><strong>来源与字段的连接：</strong>{{ usesAuxiliaryResult ? '已采用的辅助生成／翻译结果' : source?.label || selector }}<template v-if="usesAuxiliaryResult && generationOutputSemantics === 'contribution'"> → 原语合成</template> → {{ fieldLabel || fieldKey }}（{{ fieldKey }}）<template v-if="channel"> → {{ channel }}</template></p>
    <template v-if="source">
      <p><strong>{{ source.groupLabel }} · {{ source.label }}</strong> <code>{{ selector }}</code></p>
      <p>{{ source.description }}</p>
      <dl><dt>在哪里维护或填写</dt><dd>{{ source.recordLocation }}</dd><dt>如何进入本次请求</dt><dd>{{ source.executionUsage }}</dd></dl>
    </template>
    <p v-else role="status">来源 <code>{{ selector || '尚未选择' }}</code> 的说明未读取或不在当前目录中；不据名称推断实际取值。</p>
    <p v-if="usesAuxiliaryResult" class="source-warning">此规则先生成或翻译字段；<template v-if="generationOutputSemantics === 'contribution'">已采用的结果先作为贡献交给所选原语合成，不直接当作最终字段。</template><template v-else-if="generationOutputSemantics === 'final'">已采用的结果直接作为最终字段值排版。</template><template v-else>生成结果用途尚未明确，不能推断是最终字段还是投影贡献。</template>主要来源与额外上下文见本规则的生成配置；审核与采用按实际流程记录。</p>
  </div>
</template>

<style scoped>
.source-help{min-width:0;font-size:12px;line-height:1.65;border-left:2px solid var(--border);padding:8px 12px}.source-help p{margin:0 0 6px;overflow-wrap:anywhere;color:var(--text-secondary)}.source-help strong{color:var(--text-primary)}.source-help code{overflow-wrap:anywhere}.source-help dl{display:grid;grid-template-columns:140px minmax(0,1fr);gap:6px 12px;margin:8px 0}.source-help dt{color:var(--text-secondary)}.source-help dd{margin:0;overflow-wrap:anywhere}.source-warning{color:var(--warning)!important}@media(max-width:640px){.source-help dl{grid-template-columns:1fr;gap:3px}.source-help dd{margin-bottom:8px}}
</style>
