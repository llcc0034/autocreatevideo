<script setup lang="ts">
import {computed, onBeforeUnmount, ref, watch} from 'vue'
import {platformApi} from '@/services/platformApi'
import {apiErrorSummary} from '@/services/apiErrorPresentation'
import {recipeStoryboardReferenceReview, type RecipeReferenceReview} from '@/services/recipeStoryboardReferenceReview'
import type {ProductionRun} from '@/services/platformTypes'
import {mediaKindLabel} from '@/services/presentationLabels'
import ExactReferencePreview from './ExactReferencePreview.vue'
import StoryboardShotRelations from './StoryboardShotRelations.vue'
import ShotExecutionEvidence from './ShotExecutionEvidence.vue'

const props = defineProps<{run: ProductionRun; stageId: string; refreshEpoch: number; disabled: boolean}>()
const emit = defineEmits<{ready: [RecipeReferenceReview | null]; error: [string]; busy: [boolean]}>()
const result = ref<RecipeReferenceReview | null>(null), error = ref(''), loading = ref(false)
let sequence = 0
const orderedShots = computed(() => {
  const shots = new Map(result.value?.shots.map(shot => [shot.id, shot]))
  return result.value?.display.shots.map(display => ({display, ...shots.get(display.shot_id)!})) || []
})
const sourceLabel = (value: string) => ({existing: '已有素材', upload: '正式上传', generated: '本轮生成'}[value] || value)
async function read() {
  if (loading.value || props.disabled) return
  const token = ++sequence, runId = props.run.id, projectId = props.run.project
  result.value = null; error.value = ''; loading.value = true
  emit('ready', null); emit('error', '正在核对确切选择及原目标输入合同'); emit('busy', true)
  try {
    const value = await platformApi.getStoryboardReferenceSelection(runId)
    if (token !== sequence) return
    result.value = recipeStoryboardReferenceReview(value, projectId)
    emit('ready', result.value); emit('error', '')
  } catch (cause) {
    if (token === sequence) {error.value = apiErrorSummary(cause, '分镜参考读取失败'); emit('error', error.value)}
  } finally {if (token === sequence) {loading.value = false; emit('busy', false)}}
}
watch([() => props.run.id, () => props.stageId, () => props.refreshEpoch], () => {
  sequence++; result.value = null; error.value = ''; loading.value = false
  emit('ready', null); emit('error', '请读取当前确切选择后再批准'); emit('busy', false)
  void read()
}, {immediate: true})
watch(() => props.disabled, value => {
  if (!value && !result.value && !error.value && !loading.value) void read()
})
onBeforeUnmount(() => {sequence++; emit('ready', null); emit('busy', false)})
</script>

<template>
  <section class="storyboard-reference-review" aria-label="本轮分镜确切素材审核">
    <div class="review-heading"><strong>本轮分镜确切素材审核</strong><button class="button ghost small" :disabled="loading || disabled" @click="read">{{loading ? '核对中…' : '重新读取并核对选择'}}</button></div>
    <p>审核只确认下列已保存选择。需要改选时使用“本轮单镜 / 多镜素材选择”，不会修改 Bible 默认主图。素材预览按用途展开读取，不改变所见审核摘要。</p>
    <p v-if="error" class="platform-alert danger" role="alert">{{error}}；请核对单镜选择或重新读取，尚未批准。</p>
    <p v-else-if="loading" role="status">正在核对本轮来源、确切素材及文件。</p>
    <template v-if="result">
      <p role="status">已核对 {{result.shots.length}} 个镜头；批准时将重新核对所见来源及完整选择摘要。</p>
      <details v-for="shot in orderedShots" :key="shot.id">
        <summary>第{{shot.display.episode_number}}集 · {{shot.display.episode_title}} · 镜头{{shot.display.number}} · {{shot.display.title}} · {{shot.uses.length}} 项独立用途</summary>
        <StoryboardShotRelations :shot="shot.display"/>
        <p v-if="!shot.uses.length">本镜未声明参考用途，不自动填主图。</p>
        <ul v-else><li v-for="use in shot.uses" :key="use.id">
          <strong>{{use.key}} · {{mediaKindLabel(use.mediaKind)}}</strong>
          <span>创作描述：{{use.purpose || '未填写（可选）'}}；{{sourceLabel(use.sourceMode)}}</span>
          <ExactReferencePreview :selection="use"/>
        </li></ul>
        <ShotExecutionEvidence :project-id="run.project" :shot-id="shot.id"/>
      </details>
    </template>
  </section>
</template>

<style scoped>
.storyboard-reference-review{display:grid;gap:12px;min-width:0}.review-heading{display:flex;gap:12px;justify-content:space-between;align-items:center;flex-wrap:wrap}
.storyboard-reference-review p{margin:0;overflow-wrap:anywhere}.storyboard-reference-review summary{cursor:pointer;min-height:36px;overflow-wrap:anywhere}
.storyboard-reference-review ul{padding-left:20px;display:grid;gap:12px}.storyboard-reference-review li{display:grid;gap:4px;overflow-wrap:anywhere}
.review-heading button{min-height:36px;white-space:normal}
</style>
