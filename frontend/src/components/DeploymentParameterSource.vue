<script setup lang="ts">
import { computed } from 'vue'
import type { JsonObject } from '@/services/platformTypes'
import type { RegistryEditorDependencies, RegistryFieldSchema } from '@/services/registryResourceSchema'
import { deploymentParameterSource } from '@/services/deploymentParameterSource'
const props = defineProps<{ field: RegistryFieldSchema; payload: JsonObject; dependencies: RegistryEditorDependencies }>()
const source = computed(() => deploymentParameterSource(props.field, props.payload, props.dependencies))
</script>

<template>
  <details v-if="source" class="deployment-parameter-source">
    <summary>{{ source.state }}<template v-if="source.own !== '未声明'">：{{ source.own }}</template> · 查看来源</summary>
    <dl><dt>连接声明</dt><dd>{{ source.declared }}</dd><dt>来源连接</dt><dd>{{ source.connection }}</dd></dl>
    <p>仅对照连接原值和本部署草稿，不是最终请求的生效值。模型能力由引用版本维护；不会复制到本部署。</p>
    <p v-if="source.grouped">嵌套参数按原合同整组覆盖；本组已填写时，缺少的子项不表示逐项继承。</p>
  </details>
</template>

<style scoped>
.deployment-parameter-source{font-size:var(--config-help-size);color:var(--text-secondary,#96a5b3);overflow-wrap:anywhere}
summary{cursor:pointer;line-height:1.6;color:var(--text-primary,#d7dde5)}dl{display:grid;grid-template-columns:auto minmax(0,1fr);gap:6px 12px;margin:8px 0}dd{margin:0}p{margin:6px 0;line-height:1.6}
</style>
