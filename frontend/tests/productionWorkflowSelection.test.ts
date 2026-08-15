import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { normalizeWorkflowParameterValue } from '../src/services/workflowParameters.ts'

const source = (relativePath: string) => readFileSync(new URL(relativePath, import.meta.url), 'utf8')
const occurrences = (value: string, fragment: string) => value.split(fragment).length - 1

const workflowView = source('../src/views/WorkflowView.vue')
const storyboardView = source('../src/views/StoryboardView.vue')
const seriesStudioView = source('../src/views/SeriesStudioView.vue')
const repository = source('../src/services/repository.ts')
const backendAdapters = source('../src/services/backendAdapters.ts')
const studioStore = source('../src/stores/studio.ts')
const picker = source('../src/components/ProductionWorkflowPicker.vue')

test('all four active video production entries expose Ref2VA workflow selection', () => {
  assert.ok(workflowView.includes('v-if="selectedProductionHasRef2VA"'))
  assert.ok(storyboardView.includes('v-if="selectedShot.mediaType===\'video\'&&selectedShot.mode===\'Ref2VA\'"'))
  assert.ok(storyboardView.includes('v-if="episodeHasRef2VA"'))
  assert.ok(seriesStudioView.includes('v-if="wholeEpisodeHasRef2VA"'))

  assert.match(workflowView, /studio\.createWorkflow\([^)]*productionWorkflowSelections\.value,extendedProductionConfirmed\.value,idempotencyKey\)/s)
  assert.match(storyboardView, /studio\.regenerateShot\([^)]*shotWorkflowSelections\.value, extendedGenerationConfirmed\)/s)
  assert.match(storyboardView, /studio\.generateWholeEpisode\([^)]*wholeEpisodeWorkflowSelections\.value,wholeEpisodeExtendedConfirmed\.value\)/s)
  assert.match(seriesStudioView, /studio\.generateWholeEpisode\([^)]*wholeEpisodeWorkflowSelections\.value, wholeEpisodeExtendedConfirmed\.value\)/s)
})

test('the three production repository methods conditionally send workflow selections', () => {
  assert.equal(occurrences(repository, 'workflow_selections: workflowSelectionsPayload(workflowSelections)'), 3)
  assert.equal(occurrences(repository, 'api.post(`/projects/${project.id}/build-production/`, payload, { timeout: 0 })'), 3)
  assert.equal(occurrences(studioStore, 'workflowSelections: ProductionWorkflowSelection[] = []'), 3)
  assert.equal(occurrences(studioStore, ', workflowSelections, extendedGenerationConfirmed)'), 2)
  assert.equal(occurrences(studioStore, ', workflowSelections, extendedGenerationConfirmed, idempotencyKey)'), 1)
  assert.equal(occurrences(studioStore, ', workflowSelections)'), 0)
})

test('whole-episode queue confirmation is explicit and never hard-coded', () => {
  assert.equal(occurrences(repository, 'extended_generation_confirmed: extendedGenerationConfirmed'), 4)
  assert.ok(!repository.includes('extended_generation_confirmed: true'))
  assert.ok(storyboardView.includes("productionErrorCode(error)==='production_queue_confirmation_required'"))
  assert.ok(seriesStudioView.includes("productionErrorCode(error) === 'production_queue_confirmation_required'"))
  assert.ok(storyboardView.includes('wholeEpisodeExtendedConfirmed.value=true'))
  assert.ok(seriesStudioView.includes('wholeEpisodeExtendedConfirmed.value = true'))
  assert.ok(storyboardView.includes('不会截断镜数'))
  assert.ok(seriesStudioView.includes('不会截断镜数'))
  assert.ok(storyboardView.includes("productionErrorCode(error)==='production_idempotency_conflict'"))
  assert.ok(seriesStudioView.includes("productionErrorCode(error) === 'production_idempotency_conflict'"))
  assert.ok(storyboardView.includes('wholeEpisodeIdempotencyConflict.value='))
  assert.ok(seriesStudioView.includes('wholeEpisodeIdempotencyConflict.value ='))
  assert.ok(storyboardView.includes('核对任务后放弃旧尝试'))
  assert.ok(seriesStudioView.includes('核对任务后放弃旧尝试'))
  assert.ok(!storyboardView.includes("clearWholeEpisodeRetryState(storageKey)\n    }"))
})

test('single-shot regeneration confirms queue guidance only after the server asks', () => {
  const methodStart = repository.indexOf('async regenerateShot(')
  const methodEnd = repository.indexOf('\n  async ', methodStart + 1)
  const method = repository.slice(methodStart, methodEnd)
  assert.ok(method.includes('extendedGenerationConfirmed = false'))
  assert.ok(method.includes('extended_generation_confirmed: extendedGenerationConfirmed'))
  assert.ok(studioStore.includes('workflowSelections, extendedGenerationConfirmed)'))
  assert.ok(storyboardView.includes('@click="saveAndRegenerateShot(false)"'))
  assert.ok(storyboardView.includes("!extendedGenerationConfirmed && productionErrorCode(e) === 'production_queue_confirmation_required'"))
  assert.ok(storyboardView.includes('pendingShotRegenerationConfirmation.value'))
  assert.ok(storyboardView.includes('@click="confirmShotRegeneration"'))
  assert.ok(storyboardView.includes('await saveAndRegenerateShot(true)'))
  assert.ok(storyboardView.includes('取消，不提交'))
  assert.ok(storyboardView.includes('选择取消不会创建新的生产任务'))
})

test('current-shot prompt and production persist only the selected shot', () => {
  const syncShotStart = backendAdapters.indexOf('export const syncShot = async')
  const syncShotEnd = backendAdapters.indexOf('\nexport const syncEpisode = async', syncShotStart)
  const syncShotMethod = backendAdapters.slice(syncShotStart, syncShotEnd)
  assert.ok(syncShotStart >= 0 && syncShotEnd > syncShotStart)
  assert.ok(syncShotMethod.includes('shotPersistencePayload(project, shot)'))
  assert.ok(syncShotMethod.includes('api.patch(`/shots/${shot.id}/`'))
  assert.ok(syncShotMethod.includes("api.post('/shots/'"))
  assert.ok(!syncShotMethod.includes('sync-shots'))

  const syncEpisodeStart = backendAdapters.indexOf('export const syncEpisode = async')
  const syncEpisodeEnd = backendAdapters.indexOf('\nconst taskNode', syncEpisodeStart)
  const syncEpisodeMethod = backendAdapters.slice(syncEpisodeStart, syncEpisodeEnd)
  assert.ok(syncEpisodeMethod.includes('episode.shots.map((shot) => shotPersistencePayload(project, shot))'))
  assert.ok(syncEpisodeMethod.includes('`/episodes/${episode.id}/sync-shots/`'))

  assert.ok(repository.includes('async saveShot(project: SeriesProject, episode: Episode, shot: Shot)'))
  assert.ok(repository.includes('syncShot(project, episode, shot)'))
  assert.ok(studioStore.includes('const previousId = shot.id'))
  assert.ok(studioStore.includes('item.id === previousId || item.id === saved.id'))
  assert.ok(studioStore.includes('saveEpisode, saveShot, breakdownScript'))
  assert.ok(storyboardView.includes('const selectionStillTargetsSavedShot = selectedShotId.value === previousId || selectedShotId.value === saved.id'))
  assert.ok(storyboardView.includes('delete prompts[previousId]'))
  assert.ok(storyboardView.includes('if (selectionStillTargetsSavedShot) selectedShotId.value = saved.id'))

  const currentPromptStart = storyboardView.indexOf('const generate=async')
  const currentPromptEnd = storyboardView.indexOf('\nconst generateAll=async', currentPromptStart)
  const currentPromptMethod = storyboardView.slice(currentPromptStart, currentPromptEnd)
  assert.ok(currentPromptMethod.includes('persistCurrentShotKeepingSelection()'))
  assert.ok(!currentPromptMethod.includes('persistEpisodeKeepingSelection()'))

  const allPromptStart = currentPromptEnd
  const allPromptEnd = storyboardView.indexOf('\nconst restoreWorkflowSelection', allPromptStart)
  assert.ok(storyboardView.slice(allPromptStart, allPromptEnd).includes('persistEpisodeKeepingSelection()'))
  const wholeEpisodeStart = storyboardView.indexOf('const submitWholeEpisode=async')
  const wholeEpisodeEnd = storyboardView.indexOf('\nconst saveAndRegenerateShot', wholeEpisodeStart)
  assert.ok(storyboardView.slice(wholeEpisodeStart, wholeEpisodeEnd).includes('persistEpisodeKeepingSelection()'))
  const currentProductionStart = wholeEpisodeEnd
  const currentProductionEnd = storyboardView.indexOf('\nconst closeShotRegenerationConfirmation', currentProductionStart)
  assert.ok(storyboardView.slice(currentProductionStart, currentProductionEnd).includes('persistCurrentShotKeepingSelection()'))

  assert.ok(storyboardView.includes("if (selectedShot.value?.legacyNegativePromptRepairEligible) return '当前镜头的来源匹配负面约束待服务端审计恢复。'"))
  assert.ok(storyboardView.includes(':disabled="generating||referenceValidationErrors.length>0||currentShotPersistenceBlocked"'))
  assert.ok(storyboardView.includes(':disabled="generating||currentShotPersistenceBlocked"'))
  assert.ok(storyboardView.includes(':disabled="regeneratingShot||saving||generating||currentShotPersistenceBlocked||referenceValidationErrors.length>0||selectedPromptProductionBlocked"'))
})

test('Bible replacement refreshes authoritative ids and script rebuild is explicitly confirmed', () => {
  const updateStart = repository.indexOf('async updateProject(project: SeriesProject)')
  const updateEnd = repository.indexOf('\n  async ', updateStart + 1)
  const updateMethod = repository.slice(updateStart, updateEnd)
  assert.ok(updateMethod.includes('await syncBible(project)'))
  assert.ok(updateMethod.includes('api.get(`/projects/${project.id}/`)'))
  assert.ok(updateMethod.includes('adaptProject('))
  assert.ok(backendAdapters.includes('仍引用已删除或已重建的 Bible 对象'))
  assert.ok(backendAdapters.includes('对白说话人 ${line.speaker_id} · ${character.name} 尚未绑定到本镜头'))
  assert.ok(backendAdapters.includes('参考素材绑定仍指向已删除的 Bible 对象'))

  assert.ok(repository.includes('discard_existing_production: options.discardExistingProduction'))
  assert.ok(seriesStudioView.includes(':open="rebuildConfirmOpen"'))
  assert.ok(seriesStudioView.includes('确认清理并重建'))
  assert.ok(seriesStudioView.includes('await runBreakdown(true)'))
  assert.ok(seriesStudioView.includes('clearReplacedEpisodeRetryState(replacedEpisodeIds)'))
  assert.ok(seriesStudioView.includes('studio.refreshWorkflows()'))
  assert.ok(seriesStudioView.includes('运行中/远端未终结任务'))
  assert.ok(seriesStudioView.includes('封存/发行版本'))
  assert.ok(!seriesStudioView.includes('已有生成或组装审计记录时，后端会拒绝删除'))
})

test('failed-task retry uses the same explicit queue confirmation handshake', () => {
  assert.ok(repository.includes('async retryGenerationTask(taskId: string, extendedGenerationConfirmed = false)'))
  assert.ok(repository.includes('extended_generation_confirmed: extendedGenerationConfirmed'))
  assert.ok(workflowView.includes("productionErrorCode(error)==='production_queue_confirmation_required'"))
  assert.ok(workflowView.includes("repository.retryGenerationTask(taskId,retryExtendedConfirmedTaskId.value===taskId)"))
  assert.ok(workflowView.includes('确认并重试失败任务'))
})

test('production volume and concurrency remain capability-driven', () => {
  assert.ok(!workflowView.includes('LIMIT 1'))
  assert.ok(!workflowView.includes('maxConcurrent || 1'))
  assert.ok(workflowView.includes('productionCapability.maxConcurrent'))
  assert.ok(repository.includes("throw new Error(`生产策略缺少有效字段 ${key}`)"))
  assert.ok(!seriesStudioView.includes('maxlength="1000000"'))
  assert.ok(!seriesStudioView.includes('最大 1,000,000 字符'))
})

test('formal video duration is capability-driven without browser-side fixed clamping', () => {
  assert.ok(repository.includes("positiveInteger('video_shot_min_duration_ms')"))
  assert.ok(repository.includes("positiveInteger('video_shot_max_duration_ms')"))
  assert.ok(storyboardView.includes(':min="videoShotMinDurationSeconds"'))
  assert.ok(storyboardView.includes(':max="videoShotMaxDurationSeconds"'))
  assert.ok(seriesStudioView.includes(':min="videoShotMinDurationSeconds"'))
  assert.ok(seriesStudioView.includes(':max="videoShotMaxDurationSeconds"'))
  assert.ok(!backendAdapters.includes('Math.min(15, Math.max(5'))
  assert.ok(!repository.includes('Math.min(15, Math.max(5'))
})

test('official default stays empty and each create button resets stale selection', () => {
  assert.ok(picker.includes(':aria-pressed="!templateId"'))
  assert.ok(picker.includes('@click="selectTemplate(\'\')"'))
  assert.match(workflowView, /const openCreateWizard=async\(\)=>\{\s*restoringSeriesProductionRetryState=true;ref2vaTemplateId\.value='';ref2vaWorkflowParameters\.value=\{\}/u)
  assert.ok(workflowView.includes('if(project&&!project.detailLoaded)await studio.loadProject(project.id)'))
  assert.ok(workflowView.includes('createOpen.value=true'))
  assert.equal(occurrences(workflowView, '@click="openCreateWizard"'), 2)
})

test('whole-series submission has visible server confirmation and retry-safe idempotency', () => {
  assert.ok(workflowView.includes('serverProductionConfirmationRequired.value=true;extendedProductionConfirmed.value=true'))
  assert.ok(workflowView.includes('if (serverProductionConfirmationRequired.value) return true'))
  assert.ok(workflowView.includes('seriesProductionIdempotencyKey.value=idempotencyKey;persistSeriesProductionRetryState'))
  assert.ok(workflowView.includes('上次整剧提交的响应尚未确认'))
  assert.ok(workflowView.includes('已创建的完整批次将直接返回，不会重复创建任务'))
  assert.ok(!workflowView.includes('if(project?.detailLoaded)return'))
  assert.ok(workflowView.includes('restoreSeriesProductionRetryState(id)'))
  assert.ok(workflowView.includes("productionErrorCode(e)==='production_idempotency_conflict'"))
  assert.ok(workflowView.includes('已保留幂等键并停止新建'))
  assert.ok(workflowView.includes('核对任务后放弃旧尝试'))
  assert.ok(workflowView.includes('if(seriesProductionIdempotencyKey.value&&selectedProjectId.value)'))
  assert.ok(workflowView.includes('范围和参数已恢复为原请求'))
  assert.ok(workflowView.includes('restoreSeriesProductionRetryState(selectedProjectId.value)'))
  assert.ok(!workflowView.includes('修改范围或参数会开始一笔新提交'))
  assert.ok(repository.includes('idempotency_key: idempotencyKey'))
  assert.ok(studioStore.includes('extendedGenerationConfirmed = false, idempotencyKey = \'\''))
})

test('workflow parameters come from the selected template schema', () => {
  assert.ok(picker.includes('template.h3Modes.includes(props.h3Mode)'))
  assert.ok(picker.includes('Object.entries(selectedTemplate.value?.userParameters || {})'))
  assert.ok(picker.includes(':min="definition.min"'))
  assert.ok(picker.includes(':max="definition.max"'))
  assert.ok(picker.includes('normalizeWorkflowParameterValue(parameters.value[key], definition)'))
})

test('workflow parameter normalization follows decimal and integer schema steps', () => {
  const lora = {
    label: 'LoRA 模型强度', type: 'number' as const, default: 1,
    min: 0, max: 2.2, step: 0.05,
  }
  assert.equal(normalizeWorkflowParameterValue(0.03, lora), 0.05)
  assert.equal(normalizeWorkflowParameterValue(-0.01, lora), 0)
  assert.equal(normalizeWorkflowParameterValue(2.23, lora), 2.2)
  assert.equal(normalizeWorkflowParameterValue(0.15, lora), 0.15)

  const steps = {
    label: '采样步数', type: 'integer' as const, default: 4,
    min: 1, max: 20, step: 1,
  }
  assert.equal(normalizeWorkflowParameterValue(4.6, steps), 5)
  assert.equal(normalizeWorkflowParameterValue(20.8, steps), 20)
})

test('failed-task retry deliberately reuses the materialized workflow', () => {
  const start = repository.indexOf('async retryGenerationTask(')
  const end = repository.indexOf('\n  async ', start + 1)
  assert.ok(start >= 0 && end > start)
  const retryMethod = repository.slice(start, end)
  assert.ok(retryMethod.includes('api.post(`/tasks/${taskId}/retry/`, {'))
  assert.ok(retryMethod.includes('extended_generation_confirmed: extendedGenerationConfirmed'))
  assert.ok(!retryMethod.includes('workflow_selections'))
  assert.ok(workflowView.includes('重试会复用原任务的实际工作流与参数'))
  assert.ok(workflowView.includes('修改并重新生成此镜头'))
})
