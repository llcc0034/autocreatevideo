<script setup lang="ts">
import ProductionRecipeDefinitionView from './ProductionRecipeDefinitionView.vue'
import PlatformStatusPill from './PlatformStatusPill.vue'
import { mediaKindLabel, reasonCodeLabel } from '@/services/presentationLabels'
import type { ProductionRecipe, ProductionRecipeEditorOptions, ProductionReadinessRecipe } from '@/services/platformTypes'
defineProps<{ recipe: ProductionRecipe; recipeEditorOptions?: ProductionRecipeEditorOptions | null;
  readiness?: ProductionReadinessRecipe | null; readinessReportState?: string; showRaw?: boolean; compact?: boolean }>()
</script>

<template>
  <section class="recipe-configuration-view">
    <p>查看此配方版本保存的配置。资源下钻按保存的版本 ID 读取，不自动换成最新版；配方中的预期摘要保留在绑定详情，下钻本身不核验摘要或执行准入。配置展示不代表生产验收通过。</p>
    <p v-if="readinessReportState==='error'">配置与证据核对读取失败，不能按已就绪处理。</p>
    <section v-else-if="readiness" aria-label="配方配置与证据核对">
      <strong>配置与证据核对</strong>
      <p>这里只核对所选实例与组合证据，不代表本次执行已获准。素材、参数、容量和预算在创建执行准备后核对；辅助实例仅在实际生成字段时需要调用。</p>
      <p v-for="code in readiness.reasonCodes" :key="code">{{ reasonCodeLabel(code) }}</p>
      <details v-if="readiness.validationErrors.length" class="recipe-validation-errors">
        <summary>查看此配方版本的合同错误（{{ readiness.validationErrors.length }}）</summary>
        <article v-for="(error, index) in readiness.validationErrors" :key="index">
          <strong>{{ error.stageIndex === null ? '完整配方' : `步骤 ${error.stageIndex + 1} · ${error.stageKey ?? '身份不可核验'}` }}</strong>
          <span v-if="error.routingKey">{{ error.routingKey === 'variant_routing' ? '变体执行实例' : '执行实例' }}<template v-if="error.routeIndex !== null"> {{ error.routeIndex + 1 }}</template></span>
          <code>{{ error.fieldPath }}</code>
          <p>{{ error.message }}</p>
          <small v-if="error.code">{{ error.code }}</small>
        </article>
      </details>
      <article v-for="(row, kind) in readiness.capabilities" :key="kind" class="recipe-readiness-row">
        <template v-if="row.required">
          <strong>{{ kind==='assembly' ? '装配' : kind==='script' ? '剧本（script）' : mediaKindLabel(kind) }}</strong>
          <PlatformStatusPill :status="row.state"/>
          <span>{{ row.instanceCount === null ? '数量不可核验' : `关联实例 ${row.instanceCount} 项` }}</span>
          <p v-for="code in row.reasonCodes" :key="code">{{ reasonCodeLabel(code) }}</p>
          <p v-for="code in row.warningCodes" :key="code">提示：{{ reasonCodeLabel(code) }}</p>
        </template>
      </article>
    </section>
    <ProductionRecipeDefinitionView :definition="recipe.definition" :options="recipeEditorOptions"/>
    <details v-if="showRaw"><summary>完整配方合同 · {{ recipe.definitionSha256 }}</summary><pre>{{ JSON.stringify(recipe.definition,null,2) }}</pre></details>
  </section>
</template>

<style scoped>
.recipe-configuration-view{display:grid;gap:var(--config-space-md);min-width:0;font:var(--config-body-size)/1.65 var(--font)}.recipe-configuration-view>p{color:var(--text-secondary);margin:0}.recipe-configuration-view pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto}.recipe-configuration-view summary{cursor:pointer;overflow-wrap:anywhere;min-height:var(--config-control-height);align-content:center}
.recipe-readiness-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.recipe-readiness-row:empty{display:none}.recipe-readiness-row p{flex-basis:100%;margin:0;color:var(--text-secondary,#94a3b8)}
.recipe-validation-errors article{display:grid;gap:6px;min-width:0;margin-top:10px}.recipe-validation-errors p{margin:0}.recipe-validation-errors code,.recipe-validation-errors p,.recipe-validation-errors strong{white-space:pre-wrap;overflow-wrap:anywhere}
</style>
