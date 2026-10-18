<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Image, LoaderCircle } from 'lucide-vue-next'
import type { Asset, BibleImageLatestCandidate } from '@/types'
import type { BibleImageFulfilledMaterial, BibleImageReviewEntity } from '@/services/productionRunReview'
import { eligibleBibleLibraryAssets, fulfilledBiblePrimary, verifiedBibleMaterialAsset, verifiedBibleCandidatePreview, type BibleImageReviewSelection } from '@/services/bibleImageSelections'
const props = defineProps<{ entities: BibleImageReviewEntity[]; assets: Asset[]; history: Record<string, BibleImageLatestCandidate>;
  modelValue: Record<string, BibleImageReviewSelection>; disabled?: boolean; loading?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: Record<string, BibleImageReviewSelection>] }>()
const bibleImageSelections = computed({ get: () => props.modelValue, set: value => emit('update:modelValue', value) })
const bibleImageHistoryLoading = computed(() => props.loading)
const bibleLibraryExpanded = ref<Record<string, boolean>>({})
const bibleLibrarySearches = ref<Record<string, string>>({})
const bibleLibraryLimits = ref<Record<string, number>>({})
const materialLimits = ref<Record<string, number>>({})
const failedPreviews = ref(new Set<string>())
watch(() => props.assets, () => { failedPreviews.value = new Set() })
const materialSources = { existing: '已有素材复用', upload: '上传素材', generated: '模型生成并登记' }
const materialPreview = (material: BibleImageFulfilledMaterial) => {
  const asset = verifiedBibleMaterialAsset(props.assets, material)
  return failedPreviews.value.has(material.referenceVersionId) ? '' : asset?.thumbnail || asset?.mediaUrl || ''
}
const visibleMaterials = (entity: BibleImageReviewEntity) => (entity.fulfilledMaterials || []).slice(0, materialLimits.value[entity.entityId] || 12)
const entityFor = (kind: string, id: string) => props.entities.find(entity => entity.entityType === kind && entity.entityId === id)
const primaryFor = (kind: string, id: string) => {
  const entity = entityFor(kind, id)
  return entity ? fulfilledBiblePrimary(entity) : ''
}
const selectableCandidates = (entity: BibleImageReviewEntity) => entity.candidates.filter(candidate => !entity.fulfilledMaterials?.some(item => item.reference.artifact_id === candidate.artifactId))
const bibleCandidatePreview = (id: string, hash: string) => verifiedBibleCandidatePreview(props.history, id, hash)
const bibleEntityKey = (entityType: string, entityId: string) => `${entityType}:${entityId}`
const bibleSelectionFor = (entityType: string, entityId: string) => (
  bibleImageSelections.value[bibleEntityKey(entityType, entityId)]
  || { selectedArtifactIds: [], selectedAssetIds: [], anchorArtifactId: '', anchorAssetId: '' }
)
const eventChecked = (event: Event) => (event.target as HTMLInputElement).checked
const eventValue = (event: Event) => (event.target as HTMLInputElement | HTMLSelectElement).value
const toggleBibleArtifact = (entityType: string, entityId: string, artifactId: string, checked: boolean) => {
  const key = bibleEntityKey(entityType, entityId)
  const current = bibleSelectionFor(entityType, entityId)
  const selected = new Set(current.selectedArtifactIds)
  if (checked) selected.add(artifactId)
  else selected.delete(artifactId)
  bibleImageSelections.value = {
    ...bibleImageSelections.value,
    [key]: {
      selectedArtifactIds: [...selected],
      selectedAssetIds: current.selectedAssetIds,
      anchorArtifactId: selected.has(current.anchorArtifactId) ? current.anchorArtifactId : '',
      anchorAssetId: current.anchorAssetId,
    },
  }
}
const setBibleAnchor = (entityType: string, entityId: string, artifactId: string) => {
  if (primaryFor(entityType, entityId)) return
  const key = bibleEntityKey(entityType, entityId)
  const current = bibleSelectionFor(entityType, entityId)
  if (!current.selectedArtifactIds.includes(artifactId)) return
  bibleImageSelections.value = {
    ...bibleImageSelections.value,
    [key]: { ...current, anchorArtifactId: artifactId, anchorAssetId: '' },
  }
}
const bibleLibraryAssetsFor = (kind: string) => eligibleBibleLibraryAssets(props.assets, kind)
const BIBLE_LIBRARY_PAGE_SIZE = 12
const bibleLibrarySearchFor = (entityType: string, entityId: string) => (
  bibleLibrarySearches.value[bibleEntityKey(entityType, entityId)] || ''
)
const filteredBibleLibraryAssetsFor = (
  entityType: 'character' | 'scene' | 'prop',
  entityId: string,
) => {
  const query = bibleLibrarySearchFor(entityType, entityId).trim().toLocaleLowerCase('zh-CN')
  const selected = new Set(bibleSelectionFor(entityType, entityId).selectedAssetIds)
  return bibleLibraryAssetsFor(entityType)
    .filter(asset => !entityFor(entityType, entityId)?.fulfilledMaterials?.some(item => item.assetId === asset.id))
    .filter((asset) => !query || `${asset.name} ${asset.backendKind || ''} ${asset.id}`.toLocaleLowerCase('zh-CN').includes(query))
    .sort((left, right) => {
      const selectedOrder = Number(selected.has(right.id)) - Number(selected.has(left.id))
      return selectedOrder || left.name.localeCompare(right.name, 'zh-CN')
    })
}
const visibleBibleLibraryAssetsFor = (
  entityType: 'character' | 'scene' | 'prop',
  entityId: string,
) => {
  const key = bibleEntityKey(entityType, entityId)
  return filteredBibleLibraryAssetsFor(entityType, entityId).slice(
    0,
    bibleLibraryLimits.value[key] || BIBLE_LIBRARY_PAGE_SIZE,
  )
}
const bibleLibraryIsExpanded = (entityType: string, entityId: string) => Boolean(
  bibleLibraryExpanded.value[bibleEntityKey(entityType, entityId)],
)
const setBibleLibraryExpanded = (entityType: string, entityId: string, expanded: boolean) => {
  const key = bibleEntityKey(entityType, entityId)
  bibleLibraryExpanded.value = { ...bibleLibraryExpanded.value, [key]: expanded }
  if (expanded && !bibleLibraryLimits.value[key]) {
    bibleLibraryLimits.value = { ...bibleLibraryLimits.value, [key]: BIBLE_LIBRARY_PAGE_SIZE }
  }
}
const setBibleLibrarySearch = (entityType: string, entityId: string, value: string) => {
  const key = bibleEntityKey(entityType, entityId)
  bibleLibrarySearches.value = { ...bibleLibrarySearches.value, [key]: value }
  bibleLibraryLimits.value = { ...bibleLibraryLimits.value, [key]: BIBLE_LIBRARY_PAGE_SIZE }
}
const showMoreBibleLibraryAssets = (entityType: string, entityId: string) => {
  const key = bibleEntityKey(entityType, entityId)
  bibleLibraryLimits.value = {
    ...bibleLibraryLimits.value,
    [key]: (bibleLibraryLimits.value[key] || BIBLE_LIBRARY_PAGE_SIZE) + BIBLE_LIBRARY_PAGE_SIZE,
  }
}
const toggleBibleLibraryAsset = (entityType: 'character' | 'scene' | 'prop', entityId: string, assetId: string, checked: boolean) => {
  if (entityFor(entityType, entityId)?.fulfilledMaterials?.some(item => item.assetId === assetId)) return
  const key = bibleEntityKey(entityType, entityId)
  const current = bibleSelectionFor(entityType, entityId)
  const selected = new Set(current.selectedAssetIds)
  if (checked) selected.add(assetId)
  else selected.delete(assetId)
  bibleImageSelections.value = {
    ...bibleImageSelections.value,
    [key]: {
      selectedArtifactIds: current.selectedArtifactIds,
      selectedAssetIds: [...selected],
      anchorArtifactId: current.anchorArtifactId,
      anchorAssetId: selected.has(current.anchorAssetId) ? current.anchorAssetId : '',
    },
  }
}
const setBibleLibraryAnchor = (entityType: 'character' | 'scene' | 'prop', entityId: string, assetId: string) => {
  if (primaryFor(entityType, entityId)) return
  const key = bibleEntityKey(entityType, entityId)
  const current = bibleSelectionFor(entityType, entityId)
  if (!current.selectedAssetIds.includes(assetId)) return
  bibleImageSelections.value = {
    ...bibleImageSelections.value,
    [key]: { ...current, anchorArtifactId: '', anchorAssetId: assetId },
  }
}
</script>
<template>
  <fieldset class="bible-selection-editor" :disabled="disabled">
          <section v-for="entity in entities" :key="`${entity.entityType}:${entity.entityId}`" class="bible-image-review-entity">
            <header><div><strong>{{ entity.entityName }}</strong><small>{{ entity.entityType }} · {{ entity.entityId }}</small></div><div class="bible-image-review-actions"><code>{{ entity.targetContentSha256 }}</code><slot name="entity-actions" :entity="entity"/></div></header>
            <template v-if="entity.fulfilledMaterials?.length">
              <div class="bible-image-source-heading"><strong>本轮已履约素材 · {{ entity.fulfilledMaterials.length }} 项</strong></div>
              <p class="material-review-note">以下是已有选材与登记事实，随本次审核保留，不会重新生成或记作新的人工选材。主图不能在审核时替换；改选须沿依赖变更流程处理。</p>
              <div class="bible-image-review-candidates fulfilled-materials">
                <article v-for="material in visibleMaterials(entity)" :key="String(material.reference.unit_key)" class="selected">
                  <img v-if="materialPreview(material)" :src="materialPreview(material)" :alt="`${entity.entityName} 已履约${material.appearanceRole === 'primary' ? '主图' : '变体'} ${material.assetName}`" loading="lazy" decoding="async" @error="failedPreviews.add(material.referenceVersionId)"/>
                  <div v-else class="bible-image-review-placeholder" role="status"><Image :size="28"/><span>{{ loading ? '正在读取确切素材…' : '素材预览或摘要不可用，请重新读取' }}</span></div>
                  <strong>{{ material.appearanceRole === 'primary' ? '主图 · 已固定' : '变体 · 保留' }}</strong>
                  <span>{{ material.assetName }}</span><small>{{ materialSources[material.source] }} · 已选，不能在此移除</small>
                  <details><summary>确切版本与来源</summary><dl><dt>素材版本</dt><dd>{{ material.referenceVersionId }}</dd><dt>原选择</dt><dd>{{ material.reference.fulfillment_id || material.reference.decision_id }}</dd><dt>素材需求</dt><dd>{{ material.reference.requirement_key }}</dd><dt>摘要</dt><dd>{{ material.checksum }}</dd></dl></details>
                </article>
              </div>
              <div class="bible-library-pagination"><small>已显示 {{ visibleMaterials(entity).length }} / {{ entity.fulfilledMaterials.length }} 项（同一图片可履约多个确切设定版本）</small><button v-if="visibleMaterials(entity).length < entity.fulfilledMaterials.length" type="button" class="button ghost tiny" @click="materialLimits[entity.entityId] = visibleMaterials(entity).length + 12">再显示 12 项履约素材</button></div>
            </template>
            <div class="bible-image-source-heading"><strong>本次及历史模型候选</strong><small>核对实际实例与执行准备；也支持没有策略的人工提示词候选</small></div>
            <div class="bible-image-review-candidates">
              <article v-for="candidate in selectableCandidates(entity)" :key="candidate.artifactId" :class="{selected:bibleSelectionFor(entity.entityType,entity.entityId).selectedArtifactIds.includes(candidate.artifactId)}">
                <img v-if="bibleCandidatePreview(candidate.artifactId,candidate.sha256)" :src="bibleCandidatePreview(candidate.artifactId,candidate.sha256)" :alt="`${entity.entityName} 参考图候选 ${candidate.variantIndex}`" loading="lazy" decoding="async"/>
                <div v-else-if="bibleImageHistoryLoading" class="bible-image-review-placeholder" role="status"><LoaderCircle class="spin" :size="28"/><span>正在校验预览…</span></div>
                <div v-else class="bible-image-review-placeholder"><Image :size="28"/><span>预览不可用，禁止盲选</span></div>
                <label><input type="checkbox" :checked="bibleSelectionFor(entity.entityType,entity.entityId).selectedArtifactIds.includes(candidate.artifactId)" :disabled="!bibleCandidatePreview(candidate.artifactId,candidate.sha256)" @change="toggleBibleArtifact(entity.entityType,entity.entityId,candidate.artifactId,eventChecked($event))"/><span>绑定此版本</span></label>
                <label><input type="radio" :name="`bible-anchor-${entity.entityId}`" :checked="bibleSelectionFor(entity.entityType,entity.entityId).anchorArtifactId===candidate.artifactId" :disabled="!!fulfilledBiblePrimary(entity)||!bibleSelectionFor(entity.entityType,entity.entityId).selectedArtifactIds.includes(candidate.artifactId)" @change="setBibleAnchor(entity.entityType,entity.entityId,candidate.artifactId)"/><span>设为主锚点（anchor）</span></label>
                <small>{{ candidate.historical ? '历史运行（Run）复审' : '本次运行（Run）新候选' }} · 版本 {{ candidate.variantIndex }} · 可审核 · 准备 {{ candidate.preparationId.slice(0,8) }}</small><small>执行实例版本 {{ candidate.executionInstanceVersionId.slice(0,8) }}</small><code>{{ candidate.sha256 }}</code>
              </article>
            </div>
            <div class="bible-image-source-heading"><strong>素材库图片</strong><small>可与模型候选混合选择；服务端会复验所有者、分类、MIME、文件大小与内容摘要（checksum）</small></div>
            <template v-if="bibleLibraryAssetsFor(entity.entityType).length">
              <div class="bible-library-controls">
                <label class="platform-field"><span>按名称或类型筛选素材库</span><input :value="bibleLibrarySearchFor(entity.entityType,entity.entityId)" type="search" placeholder="例如：黑色门禁卡" @input="setBibleLibrarySearch(entity.entityType,entity.entityId,eventValue($event))"/></label>
                <button class="button ghost tiny" type="button" @click="setBibleLibraryExpanded(entity.entityType,entity.entityId,!bibleLibraryIsExpanded(entity.entityType,entity.entityId))">{{ bibleLibraryIsExpanded(entity.entityType,entity.entityId)?'收起素材库':`展开素材库（${bibleLibraryAssetsFor(entity.entityType).length}）` }}</button>
              </div>
              <div v-if="bibleLibraryIsExpanded(entity.entityType,entity.entityId)&&filteredBibleLibraryAssetsFor(entity.entityType,entity.entityId).length" class="bible-image-review-candidates bible-library-candidates">
                <article v-for="asset in visibleBibleLibraryAssetsFor(entity.entityType,entity.entityId)" :key="asset.id" :class="{selected:bibleSelectionFor(entity.entityType,entity.entityId).selectedAssetIds.includes(asset.id)}">
                  <img :src="asset.thumbnail||asset.mediaUrl" :alt="`${entity.entityName} 素材库参考图 ${asset.name}`" loading="lazy" decoding="async"/>
                  <label><input type="checkbox" :checked="bibleSelectionFor(entity.entityType,entity.entityId).selectedAssetIds.includes(asset.id)" @change="toggleBibleLibraryAsset(entity.entityType,entity.entityId,asset.id,eventChecked($event))"/><span>绑定素材库图片</span></label>
                  <label><input type="radio" :name="`bible-anchor-${entity.entityId}`" :checked="bibleSelectionFor(entity.entityType,entity.entityId).anchorAssetId===asset.id" :disabled="!!fulfilledBiblePrimary(entity)||!bibleSelectionFor(entity.entityType,entity.entityId).selectedAssetIds.includes(asset.id)" @change="setBibleLibraryAnchor(entity.entityType,entity.entityId,asset.id)"/><span>设为主锚点（anchor）</span></label>
                  <small>素材库 · {{ asset.name }} · {{ asset.backendKind || asset.type }}</small><code>{{ asset.checksum }}</code>
                </article>
              </div>
              <div v-else-if="bibleLibraryIsExpanded(entity.entityType,entity.entityId)" class="platform-empty bible-library-empty">没有匹配当前筛选词的图片。</div>
              <div v-if="bibleLibraryIsExpanded(entity.entityType,entity.entityId)" class="bible-library-pagination">
                <small>已显示 {{ visibleBibleLibraryAssetsFor(entity.entityType,entity.entityId).length }} / {{ filteredBibleLibraryAssetsFor(entity.entityType,entity.entityId).length }} 张</small>
                <button v-if="visibleBibleLibraryAssetsFor(entity.entityType,entity.entityId).length<filteredBibleLibraryAssetsFor(entity.entityType,entity.entityId).length" class="button ghost tiny" type="button" @click="showMoreBibleLibraryAssets(entity.entityType,entity.entityId)">再显示 {{ BIBLE_LIBRARY_PAGE_SIZE }} 张</button>
              </div>
            </template>
            <div v-else class="platform-empty bible-library-empty">当前素材库没有分类、图片完整性与预览均可用的{{ entity.entityType }}图片。<RouterLink to="/assets" target="_blank">打开素材库</RouterLink></div>
          </section>
  </fieldset>
</template>
<style scoped>
.bible-selection-editor{display:grid;gap:12px;border:0;padding:0;margin:0;min-width:0}
.material-review-note{margin:0;color:#9baab5;font-size:12px;line-height:1.6}.fulfilled-materials article{min-width:0;overflow-wrap:anywhere}.fulfilled-materials strong,.fulfilled-materials span{font-size:12px;color:#c4cbd0}.fulfilled-materials details{font-size:11px;color:#9baab5}.fulfilled-materials summary{cursor:pointer}.fulfilled-materials dl{margin:8px 0}.fulfilled-materials dd{margin:2px 0 8px;overflow-wrap:anywhere}.bible-image-review-entity{min-width:0}.bible-image-review-entity>header>div{min-width:0;overflow-wrap:anywhere}
.bible-image-review-entity{display:grid;gap:12px;padding:14px;border:1px solid #28333d;border-radius:9px;background:#0a0f13}.bible-image-review-entity>header{display:flex;align-items:flex-start;justify-content:space-between;gap:14px}.bible-image-review-entity>header>div{display:grid;gap:4px}.bible-image-review-entity>header small{color:#7d8992;font-size:10px}.bible-image-review-entity>header code{overflow:hidden;color:#6e7d88;font-size:9px;text-overflow:ellipsis}.bible-image-review-actions{justify-items:end;max-width:55%}.bible-image-review-actions code{max-width:100%}.bible-image-source-heading{display:flex;align-items:baseline;justify-content:space-between;gap:12px;padding-top:3px;border-top:1px solid #202a31}.bible-image-source-heading strong{color:#bdc6cc;font-size:10px}.bible-image-source-heading small{color:#717e87;font-size:9px}.bible-image-review-candidates{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}.bible-image-review-candidates article{display:grid;gap:8px;padding:8px;border:1px solid #28323b;border-radius:8px;background:#090d11}.bible-image-review-candidates article.selected{border-color:#5b4c86;box-shadow:0 0 0 1px rgba(146,114,220,.18)}.bible-library-candidates article.selected{border-color:#a2773e;box-shadow:0 0 0 1px rgba(215,155,75,.18)}.bible-image-review-candidates img,.bible-image-review-placeholder{width:100%;aspect-ratio:1/1;border-radius:6px;background:#11171c;object-fit:cover}.bible-image-review-placeholder{display:flex;align-items:center;justify-content:center;flex-direction:column;gap:7px;color:#7b858d;font-size:10px}.bible-image-review-candidates label{display:flex;align-items:center;gap:7px;color:#c4cbd0;font-size:11px}.bible-image-review-candidates small{color:#7d8992;font-size:9px}.bible-image-review-candidates code{overflow:hidden;color:#687682;font-size:8px;text-overflow:ellipsis}.bible-library-controls,.bible-library-pagination{display:flex;align-items:end;justify-content:space-between;gap:12px}.bible-library-controls .platform-field{flex:1;max-width:520px}.bible-library-pagination{align-items:center}.bible-library-pagination small{color:#7d8992;font-size:9px}.bible-library-empty{padding:14px;border:1px dashed #29343c;border-radius:7px}.bible-library-empty a{margin-left:5px;color:#d09a53}@media(max-width:720px){.bible-image-review-entity>header,.bible-library-controls{display:grid}.bible-image-review-actions{justify-items:start;max-width:100%}.bible-library-controls .platform-field{max-width:none}}
</style>
