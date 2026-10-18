<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import BibleContentReview from './BibleContentReview.vue'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { buildBibleConfirmation, readRecipeBibleReview, type RecipeBibleReview } from '@/services/productionManualBible'
import type { JsonObject, ProductionRun } from '@/services/platformTypes'

const props = defineProps<{ run: ProductionRun; disabled?: boolean }>()
const emit = defineEmits<{ confirmation: [value: JsonObject | null] }>()
const detail = ref<RecipeBibleReview | null>(null), loading = ref(false), error = ref('')
const candidates = ref<string[]>([]), settings = ref<string[]>([]), project = ref(false)
let sequence = 0
const confirmation = computed(() => {
  if (!detail.value || loading.value || error.value) return null
  try { return buildBibleConfirmation(detail.value, detail.value.source_snapshot_sha256, candidates.value, settings.value, project.value) }
  catch { return null }
})
watch(confirmation, value => emit('confirmation', value), { immediate: true })
async function load() {
  const token = ++sequence
  detail.value = null; candidates.value = []; settings.value = []; project.value = false; error.value = ''; loading.value = true
  emit('confirmation', null)
  try {
    const result = await platformApi.getBibleReview(props.run.id)
    if (token === sequence) detail.value = readRecipeBibleReview(result, props.run)
  } catch (e) { if (token === sequence) error.value = apiErrorSummary(e) }
  finally { if (token === sequence) loading.value = false }
}
watch(() => [props.run.id, props.run.executionPlan?.sha256, props.run.scriptRevision,
  (props.run.stages || []).filter(s => ['bible_materialize','bible_review'].includes(s.stageKey))
    .map(s => `${s.id}:${s.status}:${s.outputSnapshotSha256}`).join('|')].join(':'), () => void load(), { immediate: true })
onBeforeUnmount(() => { sequence++; emit('confirmation', null) })
</script>

<template>
  <section class="platform-card recipe-bible-review">
    <header><strong>设定集完整内容审核</strong><button class="button ghost small" :disabled="disabled || loading" @click="load">重新读取完整内容</button></header>
    <p>读取不会调用模型或写入设定。逐项核对后，通过本页原有的“提交审批”一次性保存；内容变化时需要重新读取并确认。</p>
    <p v-if="loading" role="status">正在读取原始声明与当前设定…</p><p v-if="error" class="platform-alert danger" role="alert">{{ error }}</p>
    <BibleContentReview v-if="detail" :content="detail" :disabled="disabled || loading" v-model:candidates="candidates" v-model:settings="settings" v-model:project="project" />
  </section>
</template>

<style scoped>
.recipe-bible-review{display:grid;gap:12px;min-width:0}.recipe-bible-review header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
</style>
