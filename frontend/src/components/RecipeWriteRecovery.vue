<script setup lang="ts">
import { onMounted, ref } from 'vue'
import ProductionRecipeDefinitionView from './ProductionRecipeDefinitionView.vue'
import type { ProductionRecipe, ProductionRecipeEditorOptions } from '@/services/platformTypes'
defineProps<{ busy: boolean; error: string; records: ProductionRecipe[]; selected: ProductionRecipe | null; checked: boolean; options?: ProductionRecipeEditorOptions | null }>()
defineEmits<{ read: []; inspect: [ProductionRecipe]; load: [] }>()
const root=ref<HTMLElement|null>(null)
onMounted(()=>{root.value?.focus();root.value?.scrollIntoView({block:'nearest'})})
</script>
<template>
  <section ref="root" tabindex="-1" class="recipe-write-recovery" role="status" aria-label="配方写入结果核对">
    <strong>{{ busy ? '正在回读配方记录…' : '写入结果待核对，已阻止重复提交' }}</strong>
    <p>不按名称或最新版本认领回执。先回读同配方族的记录，查看确切版本，再决定载入。载入不是确认原请求成功，且会替换当前未保存内容。</p>
    <button type="button" class="button ghost" :disabled="busy" @click="$emit('read')">重新读取结果（不写入）</button>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="checked&&!records.length">尚未查到可核对记录；这不证明原请求没有执行，不会自动重发。</p>
    <ul><li v-for="row in records" :key="row.id"><strong>{{ row.name }} · v{{ row.version }} · {{ row.status }}</strong><code>{{ row.id }}</code><button type="button" class="button ghost" :disabled="busy" @click="$emit('inspect',row)">查看 v{{ row.version }} 确切记录</button></li></ul>
    <div v-if="selected">
      <strong>正在核对：{{ selected.name }} · v{{ selected.version }} · {{ selected.status }}</strong><code>{{ selected.id }}</code>
      <ProductionRecipeDefinitionView :definition="selected.definition" :options="options" />
      <button type="button" class="button primary" :disabled="busy || selected.status!=='draft'" @click="$emit('load')">载入此草稿，替换本地内容</button>
      <p v-if="selected.status!=='draft'">此记录不是草稿，不能覆盖编辑；未解锁原写入。</p>
    </div>
  </section>
</template>
<style scoped>
.recipe-write-recovery{display:grid;gap:var(--config-space-md);font-size:var(--config-body-size);padding:var(--config-space-lg);border:1px solid var(--accent);border-radius:var(--config-radius);min-width:0}.recipe-write-recovery p{margin:0;overflow-wrap:anywhere}.recipe-write-recovery code{display:block;overflow-wrap:anywhere;font-size:var(--config-help-size)}.recipe-write-recovery ul{margin:0;padding-left:20px}.recipe-write-recovery li{margin-block:var(--config-space-sm)}.recipe-write-recovery button{white-space:normal;min-height:36px}@media(max-width:640px){.recipe-write-recovery button{min-height:44px}}
</style>
