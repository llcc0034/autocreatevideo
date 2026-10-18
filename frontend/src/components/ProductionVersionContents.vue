<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import ProductionWorkspaceTabs from './ProductionWorkspaceTabs.vue'
import ProductionWorkspacePanel from './ProductionWorkspacePanel.vue'
import { useRoute, useRouter } from 'vue-router'
import type { ProductionRun, ScriptRevision } from '@/services/platformTypes'
import { platformApi } from '@/services/platformApi'
import { executionPreparationApi } from '@/services/executionPreparationApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { productionDate, approvedScriptPreparation, asRecord, asRows, bibleMedia, creationInput, savedStageContent } from '@/services/projectVersionContents'
import ProductionScriptReader from './ProductionScriptReader.vue'
import ProductionReleaseDelivery from './ProductionReleaseDelivery.vue'
import ProductionArtifactPreview from './ProductionArtifactPreview.vue'
import ExecutionPromptDisplay from './ExecutionPromptDisplay.vue'
import { productionWorkspaceSections, workspaceSection, workspaceArtifactSection, type ProductionWorkspaceSection } from '@/services/productionWorkspacePresentation'
import { stageLabel, mediaKindLabel } from '@/services/presentationLabels'
import PlatformStatusPill from './PlatformStatusPill.vue'
import type { ExecutionPreparationDetail } from '@/services/executionPreparation'
const props = defineProps<{ run: ProductionRun }>()
const route = useRoute(), router = useRouter()
const section = computed({get: () => workspaceSection(route.query.section), set: (value: ProductionWorkspaceSection) => { void router.replace({path:route.path,query:{...route.query,section:value}}) }})
const scriptPart = ref<'source'|'script'>('source'), visualPart = ref<'visual'|'terms'>('visual')
const scriptParts = [{key:'source',label:'创作输入与原文'},{key:'script',label:'结构化剧本'}] as const
const visualParts = [{key:'visual',label:'全剧视觉规则'},{key:'terms',label:'统一名称（Canonical）'}] as const
const previewId = ref(''), mediaPage = ref(1)
const artifacts = computed(() => (props.run.artifacts || []).filter(row => ['image','video','audio','assembly'].includes(row.kind) && workspaceArtifactSection(row,props.run.stages)==='media'))
const visibleArtifacts = computed(() => artifacts.value.slice((mediaPage.value-1)*12,mediaPage.value*12))
const sourceBusy = ref(false), sourceError = ref('')
const original = ref<ScriptRevision | null>(null), preparation = ref<ExecutionPreparationDetail | null>(null)
const input = computed(() => creationInput(props.run))
const visual = computed(() => savedStageContent(props.run, 'visual_rules'))
const bible = computed(() => savedStageContent(props.run, 'bible'))
const media = computed(() => bibleMedia(props.run))
const entityImages = (id: unknown) => media.value.bindings.filter(row => row.entityId === id).flatMap(row => row.images)
const storyboard = computed(() => savedStageContent(props.run, 'storyboard', 'storyboard_review'))
const rules = computed(() => asRecord(visual.value.value.rules))
let epoch = 0
async function readSource() {
  if (sourceBusy.value) return
  const token = ++epoch; sourceBusy.value = true; sourceError.value = ''; original.value = null; preparation.value = null
  try {
    if (input.value.importedRevisionId) {
      const row = await platformApi.getScriptRevision(input.value.importedRevisionId)
      if (token !== epoch) return
      if (row.id !== input.value.importedRevisionId) throw new Error('导入原文与本轮来源不一致。')
      original.value = row
    } else {
      const id = approvedScriptPreparation(props.run)
      if (!id) throw new Error('本轮尚无已采用剧本对应的确切请求记录，请到制作台按候选查看；不猜测最后一次调用。')
      const row = await executionPreparationApi.get(id)
      if (token !== epoch) return
      if (row.id !== id || !row.requestFrozenAt) throw new Error('此请求尚未冻结，不能作为实际生成提示词展示。')
      preparation.value = row
    }
  } catch (cause) { if (token === epoch) sourceError.value = apiErrorSummary(cause) }
  finally { if (token === epoch) sourceBusy.value = false }
}
onBeforeUnmount(() => { epoch++ })
watch([section, scriptPart], () => { if (section.value === 'script' && scriptPart.value === 'source' && input.value.importedRevisionId && !original.value) void readSource() }, {immediate:true})
</script>
<template>
  <ProductionWorkspacePanel class="version-contents">
    <ProductionWorkspaceTabs v-model="section" :items="productionWorkspaceSections" label="制作版本内容"/>
    <section class="version-section" :aria-label="productionWorkspaceSections.find(item=>item.key===section)?.label">
      <nav v-if="section==='script' || section==='visual'" class="version-subsections" :aria-label="section==='script'?'剧本内容分类':'视觉内容分类'">
        <template v-if="section==='script'"><button v-for="part in scriptParts" :key="part.key" class="button ghost small" :aria-pressed="scriptPart===part.key" @click="scriptPart=part.key">{{part.label}}</button></template>
        <template v-else><button v-for="part in visualParts" :key="part.key" class="button ghost small" :aria-pressed="visualPart===part.key" @click="visualPart=part.key">{{part.label}}</button></template>
      </nav>
      <template v-if="section==='script' && scriptPart==='source'">
        <h3>{{input.importedRevisionId ? '人工导入的剧本原文' : input.generated ? 'AI 剧本创作输入与提示词' : '本轮创作来源'}}</h3>
        <p class="source-note">只读取本次制作保存的输入，不使用项目现在的描述替换历史内容。</p>
        <template v-if="input.generated && !input.importedRevisionId">
          <h4>本轮填写的创作要求</h4><div class="version-prose">{{input.inputs.idea || '本轮未记录自由创作要求；实际输入以冻结请求为准。'}}</div>
          <p v-if="input.inputs.style_hint"><strong>风格要求：</strong>{{input.inputs.style_hint}}</p>
        </template>
        <button type="button" class="button ghost small" :disabled="sourceBusy" @click="readSource">{{sourceBusy?'正在读取原记录…':input.importedRevisionId?'读取导入原文':'读取已采用剧本的提示词'}}</button>
        <p v-if="sourceError" class="platform-alert danger" role="alert">{{sourceError}}</p>
        <div v-if="original" class="version-prose">{{original.content}}</div>
        <ExecutionPromptDisplay v-if="preparation" :preparation-id="preparation.id" :revision="preparation.revision" :frozen="true" :disabled="false" :field-labels="{}"/>
        <p v-if="!input.generated && !input.importedRevisionId">本轮没有记录导入原稿或 AI 起稿输入。已采用内容可在“结构化剧本”中核对。</p>
      </template>
      <ProductionScriptReader v-else-if="section==='script' || (section==='visual' && visualPart==='terms') || section==='storyboard'" :revision-id="run.scriptRevision" :section="section==='storyboard'?'episodes':section==='visual'?'terms':'script'" :run="run"/>
      <template v-else-if="section==='visual'">
        <h3>本版本已采用的全剧视觉规则</h3>
        <p v-if="visual.error" role="alert">{{visual.error}}</p>
        <p v-else-if="!visual.stage">本版本尚无已保存的视觉规则结果。不会读取项目当前规则来补齐历史。</p>
        <template v-else><p class="source-note">来源：本轮视觉规则阶段保存结果。配置自动采用不等于人工创作质量审核。</p><article v-for="field in [{key:'continuity_summary',label:'全剧连续性约束'},{key:'color_script',label:'色彩与光线'},{key:'negative_prompt',label:'应避免的内容'}]" :key="field.key"><h4>{{field.label}}</h4><div class="version-prose">{{rules[field.key] ?? '此字段未记录'}}</div></article></template>
      </template>
      <template v-else-if="section==='bible'">
        <h3>本版本的设定与素材</h3><p class="source-note">设定来自本轮保存的 Bible，图片按本轮已选文件与摘要读取；不会换成素材库最新版本。</p>
        <p v-if="bible.error || media.error" role="alert">{{bible.error || media.error}}</p>
        <p v-if="!bible.stage">本轮尚无 Bible 设定结果。</p>
        <section v-for="group in [{key:'characters',label:'人物'},{key:'scenes',label:'场景'},{key:'props',label:'道具'}]" :key="group.key" class="bible-group"><h4>{{group.label}}</h4><div class="version-bible-grid"><article v-for="entity in asRows(bible.value[group.key])" :key="String(entity.id)" class="panel"><h4>{{entity.name}}</h4><p class="version-prose">{{entity.description || '未记录基础描述'}}</p>
          <div v-for="setting in asRows(entity.appearance_settings)" :key="String(setting.setting_revision_id)"><strong>{{setting.appearance_role==='primary'?'主图':'变体'}} · {{setting.reference_name}}</strong><p class="source-note">设定修订 {{setting.revision}}</p></div>
          <template v-if="!media.error"><ProductionArtifactPreview v-for="(image,index) in entityImages(entity.id)" :key="image.assetId+index" :asset-id="image.assetId" :run-id="run.id" :sha256="image.sha256" :label="String(entity.name)"/></template>
          <p v-if="!entityImages(entity.id).length">本轮尚无保存完整文件引用的素材预览；待选择或异常项可在制作台核对。</p>
        </article></div><p v-if="!asRows(bible.value[group.key]).length">本轮未记录{{group.label}}设定。</p></section>
      </template>
      <template v-else-if="section==='media'">
        <h3>本轮媒体与选片记录</h3><p class="source-note">按本轮产物记录展示状态；候选不等于已选用。此处只能读取原文件，不能生成或更改选片。</p>
        <p v-if="!artifacts.length">本轮尚无归档媒体，不使用其他版本产物补齐。</p>
        <article v-for="artifact in visibleArtifacts" :key="artifact.id"><h4>{{mediaKindLabel(artifact.kind)}} · {{artifact.id.slice(0,8)}}</h4><PlatformStatusPill :status="artifact.status"/><p>候选组：{{artifact.variantGroup || '未记录'}} · 版本 {{artifact.variantIndex}}</p>
          <button class="button ghost small" @click="previewId=previewId===artifact.id?'':artifact.id">{{previewId===artifact.id?'收起原文件':'查看原文件'}}</button>
          <ProductionArtifactPreview v-if="previewId===artifact.id" :id="artifact.id" :run-id="run.id" :sha256="artifact.sha256" :label="mediaKindLabel(artifact.kind)"/>
        </article>
        <nav v-if="artifacts.length>12" class="version-subsections" aria-label="媒体记录分页"><button class="button ghost small" :disabled="mediaPage<=1" @click="mediaPage--;previewId=''">上一页媒体</button><span>第 {{mediaPage}} 页 · 共 {{artifacts.length}} 项</span><button class="button ghost small" :disabled="mediaPage*12>=artifacts.length" @click="mediaPage++;previewId=''">下一页媒体</button></nav>
      </template>
      <template v-else-if="section==='records'">
        <h3>本轮阶段记录</h3><p class="source-note">仅查看已保存状态和原因；继续、重试、审核等处理请进入生产工作台。</p>
        <p v-if="!run.stages?.length">本轮尚无阶段记录。</p>
        <article v-for="stage in run.stages" :key="stage.id"><h4>{{stage.ordinal}} · {{stageLabel(stage.stageKey)}}</h4><PlatformStatusPill :status="stage.status"/><p v-if="stage.reasonCode || stage.reason">{{stage.reasonCode}} · {{stage.reason}}</p><p v-if="stage.startedAt">开始：{{productionDate(stage.startedAt)}}</p><p v-if="stage.finishedAt">结束：{{productionDate(stage.finishedAt)}}</p></article>
      </template>
      <template v-else-if="section==='delivery'">
        <h3>发行候选与成片</h3><p>发行候选确认、签名校验与成片下载沿用本轮正式交付入口；制作完成不替代内容质量验收。</p>
        <ProductionReleaseDelivery v-if="run.releaseBundleId" :bundle-id="run.releaseBundleId" :run-id="run.id" :project-id="run.project"/>
        <p v-else>本版本尚无封存发行候选。请在制作台完成选片、装配与交付，不展示其他版本成片。</p>
      </template>
      <p v-if="section==='storyboard'" class="source-note">{{storyboard.stage ? '以上正文来自本轮采用剧本，分镜关系来自本轮已保存结果；展开镜头可核对原请求与已选视频。查看不会重做、审核或更改选片。' : '本轮尚无已保存分镜阶段结果，以上为采用剧本中的镜头规划，不代表已生成视频。'}}</p>
    </section>
  </ProductionWorkspacePanel>
</template>
<style scoped>
.version-subsections{display:flex;flex-wrap:wrap;gap:10px}.version-subsections [aria-pressed="true"]{color:var(--accent);border-color:var(--accent)}
.version-section{padding:0;display:grid;gap:18px;min-width:0}.version-section :is(h3,h4,p){margin:0}.version-section h3{font-size:18px}.version-section h4{font-size:15px}.version-section article{display:grid;gap:10px}.version-prose{white-space:pre-wrap;overflow-wrap:anywhere}.source-note{color:var(--text-muted)}.version-bible-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:24px;margin-top:12px}.version-bible-grid article{align-content:start;padding:20px;background:var(--bg-soft)}.bible-group{min-width:0}.version-section .button{justify-self:start;white-space:normal}
.version-section>article{padding:16px;border:1px solid var(--border);border-radius:8px;background:var(--bg-soft)}
</style>
