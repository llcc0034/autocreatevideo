<script setup lang="ts">
import { computed } from 'vue'
import type { ProductionRecipe } from '@/services/platformTypes'
import { modeLabel, stageLabel } from '@/services/presentationLabels'
import { recipeExecutionOverview } from '@/services/recipeExecutionOverview'

const props = defineProps<{ recipe: ProductionRecipe }>()
const overview = computed(() => recipeExecutionOverview(props.recipe.definition))
</script>

<template>
  <section class="recipe-execution-summary" aria-label="配方冻结的执行配置">
    <header><strong>配方冻结的执行配置</strong><small>精确版本 · 只读</small></header>
    <p>模型、工作流和提示词策略由执行实例版本固定，这里不再分别选择。</p>
    <p>预算、调用次数和文本输出额度统一在系统设置管理。超额需人工确认，配方不再另设额度；外部调用仍须明确授权。</p>
    <p v-if="overview.error" class="summary-error" role="alert">{{ overview.error }}</p>
    <template v-else>
      <article v-for="stage in overview.stages" :key="stage.key">
        <strong>{{ stage.name || stageLabel(stage.key) }}</strong>
        <small v-if="stage.name">步骤身份 {{ stage.key }}</small>
        <small>执行方式 {{ stage.mode }} · 审核方式 {{ stage.approval }}</small>
        <template v-for="group in [{label:stage.key==='bible_image_generation'?'主图':'执行',routingMode:stage.routingMode,routes:stage.routes},...(stage.variantRouting?[{label:'变体',...stage.variantRouting}]:[])]" :key="group.label">
          <small v-if="group.routingMode">{{ group.label }}实例选择方式：{{ modeLabel(group.routingMode) }}</small>
          <div v-for="(route, index) in group.routes" :key="`${stage.key}:${group.label}:${index}`" class="frozen-route">
            <span>执行实例 {{ index + 1 }} · 精确版本</span><code>{{ route.versionId }}</code>
            <details><summary>查看冻结摘要与输入配置</summary>
              <code>{{ route.definitionSha256 }}</code>
              <p>输入来源：{{ route.inputKeys.length ? route.inputKeys.join('、') : '未配置来源映射' }}</p>
              <p>参数策略：{{ route.parameterCount }} 项</p>
            </details>
          </div>
        </template>
        <small v-if="!stage.routingMode">此阶段未配置执行实例。</small>
      </article>
      <p>输入按配方步骤解析，具体来源在执行准备中复核；这里展示配置，不代表执行准入或烟测已通过。</p>
    </template>
  </section>
</template>

<style scoped>
.recipe-execution-summary{display:grid;gap:10px;padding:14px;border:1px solid var(--border);border-radius:10px;min-width:0}
header{display:flex;gap:12px;justify-content:space-between;flex-wrap:wrap}p{margin:0}small,p{color:var(--text-muted)}
article,.frozen-route{display:grid;gap:6px;min-width:0}article{border-top:1px solid var(--border);padding-top:10px}
.frozen-route{padding:8px;background:var(--surface-2);border-radius:6px}code{overflow-wrap:anywhere;white-space:normal}
summary{cursor:pointer}.summary-error{color:var(--red)}
</style>
