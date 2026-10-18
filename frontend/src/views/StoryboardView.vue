<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { AlertTriangle, ArrowLeft, AudioLines, Box, Check, ChevronLeft, ChevronRight, Film, GripVertical, Image, Layers3, Link2, Lock, MonitorPlay, Play, Plus, Save, Sparkles, Trash2, UserRound, WandSparkles } from 'lucide-vue-next'
import ToggleSwitch from '@/components/ToggleSwitch.vue'
import ShotDialogueEditor from '@/components/ShotDialogueEditor.vue'
import { visibleCharacterIds, toggleVisibleCharacter } from '@/services/shotCharacters'
import ShotTimingEditor from '@/components/ShotTimingEditor.vue'
import ShotExecutionEvidence from '@/components/ShotExecutionEvidence.vue'
import ShotSourceFields from '@/components/ShotSourceFields.vue'
import ShotFieldRevisions from '@/components/ShotFieldRevisions.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import AssetImage from '@/components/AssetImage.vue'
import PromptImpactBadge from '@/components/PromptImpactBadge.vue'
import { repository, type ProductionCapability } from '@/services/repository'
import { bibleAssetIds } from '@/services/bibleAssets'
import { normalizeShotVersionScopeKey } from '@/services/shotVersionScope'
import { episodeNarrativeDurationSeconds, formatNarrativeDuration, shotNarrativeDurationSeconds } from '@/services/shotDuration'
import { useStudioStore } from '@/stores/studio'
import type { Episode, SeriesProject, Shot } from '@/types'

const route = useRoute(); const router = useRouter(); const studio = useStudioStore()
const project = ref<SeriesProject | null>(null); const episode = ref<Episode | null>(null); const selectedShotId = ref(''); const loading = ref(true); const saving = ref(false); const inspectorTab = ref<'visual'|'continuity'|'audio'>('visual')
const projectInputsTarget = computed(() => ({
  path: `/projects/${project.value?.id}/production${typeof route.query.run==='string' ? '/' + route.query.run : ''}`,
  query: { step: 'storyboard' },
}))
const productionTarget = computed(() => typeof route.query.run==='string'
  ? `/projects/${project.value?.id}/production/${route.query.run}`
  : `/projects/${project.value?.id}/production?view=list`)
const productionCapability = ref<ProductionCapability | null>(null); const productionCapabilityError = ref('')
const saveError = ref('')
const selectedShotVersionKey = ref('')
const episodeShotVersions = computed(() => episode.value?.shotVersions || [])
const activeShotVersionLabel = computed(() => episodeShotVersions.value.find((item) => item.key === selectedShotVersionKey.value)?.label || '')
const selectedShotVersionScopeKey = computed(() => episode.value
  ? normalizeShotVersionScopeKey(selectedShotVersionKey.value, episode.value.id)
  : '')
const selectedShotVersionScopeLabel = computed(() => activeShotVersionLabel.value || '初始分镜')
const selectShotVersion = (key: string) => {
  const current = episode.value
  const version = current?.shotVersions?.find((item) => item.key === key)
  if (!current || !version) return
  current.shots = version.shots.map((shot) => ({ ...shot, metadata: { ...(shot.metadata || {}) } }))
  current.activeShotVersionKey = version.key
  selectedShotVersionKey.value = version.key
  selectedShotId.value = current.shots[0]?.id || ''
}
const selectedShot = computed(() => episode.value?.shots.find((item) => item.id === selectedShotId.value) || null)
const videoShotMinDurationSeconds = computed(() => productionCapability.value ? productionCapability.value.videoShotMinDurationMs / 1000 : undefined)
const videoShotMaxDurationSeconds = computed(() => productionCapability.value ? productionCapability.value.videoShotMaxDurationMs / 1000 : undefined)
const selectedShotDurationOutOfRange = computed(() => {
  const duration = selectedShot.value?.duration
  const minimum = videoShotMinDurationSeconds.value
  const maximum = videoShotMaxDurationSeconds.value
  return selectedShot.value?.mediaType === 'video'
    && duration !== undefined
    && minimum !== undefined
    && maximum !== undefined
    && (!Number.isFinite(duration) || duration < minimum || duration > maximum)
})
const style = computed(() => studio.styles.find((item) => item.id === project.value?.styleId))
const totalDuration = computed(() => episode.value ? episodeNarrativeDurationSeconds(episode.value) : 0)
const totalDurationLabel = computed(() => formatNarrativeDuration(totalDuration.value))
const videoShotCount = computed(() => episode.value?.shots.filter((item) => item.mediaType === 'video').length || 0)
const imageShotCount = computed(() => episode.value?.shots.filter((item) => item.mediaType === 'image').length || 0)
const loadProductionCapability=async()=>{productionCapabilityError.value='';try{productionCapability.value=await repository.getProductionCapability(String(route.params.projectId||''))}catch(error){productionCapability.value=null;productionCapabilityError.value=error instanceof Error?error.message:'服务端未返回正式视频时长配置'}}
const continuityCount = computed(() => selectedShot.value ? visibleCharacterIds(selectedShot.value).length + (selectedShot.value.sceneId ? 1 : 0) + selectedShot.value.propIds.length : 0)
const clone = <T,>(value:T):T => JSON.parse(JSON.stringify(value))

const markShotInputsChanged = (shot:Shot) => { shot.status='draft' }
const episodeAudioEnabled = computed<boolean>({
  get: () => episode.value?.audioEnabled ?? false,
  set: (enabled) => {
    if (!episode.value) return
    episode.value.audioEnabled = enabled
    for (const shot of episode.value.shots) {
      if (shot.mediaType !== 'video' || shot.audioEnabled === enabled) continue
      shot.audioEnabled = enabled
      markShotInputsChanged(shot)
    }
  }
})
const selectedPromptInputs = computed(() => {
  const shot = selectedShot.value
  if (!shot) return null
  return {
    shotId: shot.id,
    fingerprint: JSON.stringify({
      mediaType: shot.mediaType,
      mode: shot.mode,
      duration: shot.duration,
      episodeStartMs: shot.episodeStartMs,
      episodeEndMs: shot.episodeEndMs,
      requiresFirstFrameContinuity: shot.requiresFirstFrameContinuity,
      requiresLastFrameContinuity: shot.requiresLastFrameContinuity,
      characterRelations: shot.characterRelations,
      sceneId: shot.sceneId || '',
      propIds: shot.propIds,
      audioEnabled: shot.audioEnabled,
      audio: shot.audio
    })
  }
})
const toggleEntity=(kind:'character'|'prop',id:string)=>{if(!selectedShot.value)return;const shot=selectedShot.value;if(kind==='character')toggleVisibleCharacter(shot,id);else{const i=shot.propIds.indexOf(id);if(i>=0)shot.propIds.splice(i,1);else shot.propIds.push(id)}markShotInputsChanged(shot)}
const selectScene=(id:string)=>{if(!selectedShot.value)return;selectedShot.value.sceneId=id;markShotInputsChanged(selectedShot.value)}
const newShot = (order:number):Shot => {
  const defaults = productionCapability.value?.creationDefaults
  if (!defaults) throw new Error(productionCapabilityError.value || '服务端项目创建策略缺失，不能新增分镜。')
  const mediaType = defaults.defaultShotMediaType
  return { id:`shot-${Date.now()}-${order}`, order, title:`镜头 ${String(order).padStart(2,'0')}`, mediaType, duration:productionCapability.value!.scriptBreakdownDefaultShotDurationMs / 1000, mode:'', characterRelations:[], sceneId:project.value?.bible.scenes[0]?.id, propIds:[], requiresFirstFrameContinuity:productionCapability.value!.scriptBreakdownDefaultFirstFrameContinuity, requiresLastFrameContinuity:productionCapability.value!.scriptBreakdownDefaultLastFrameContinuity, audioEnabled:mediaType === 'video' && defaults.defaultIncludeAudio, audio:{ dialogue:[] }, status:'draft' }
}
const load = async () => {
  loading.value=true; await studio.initialize(); const found=await studio.loadProject(String(route.params.projectId)); if(!found){router.replace('/projects');return}
  project.value=clone(found); const foundEpisode=project.value.episodes.find(item=>item.id===String(route.params.episodeId)); if(!foundEpisode){router.replace(`/projects/${project.value.id}`);return}
  episode.value=foundEpisode
  if (!episode.value.shots.length) {
    try { episode.value.shots.push(newShot(1)) }
    catch (error) {
      loading.value = false
      studio.notify(error instanceof Error ? error.message : '服务端创建策略缺失，不能初始化分镜。', 'error')
      return
    }
  }
  selectedShotVersionKey.value = episode.value.activeShotVersionKey || episode.value.shotVersions?.find((item)=>item.active)?.key || episode.value.shotVersions?.[0]?.key || ''
  const requestedShotId=String(Array.isArray(route.query.shot)?route.query.shot[0]||'':route.query.shot||'')
  selectedShotId.value=episode.value.shots.some((shot)=>shot.id===requestedShotId)?requestedShotId:episode.value.shots[0].id;await studio.refreshAssets()
  loading.value=false
}
onMounted(async()=>{await loadProductionCapability();await load()})
watch(selectedShotId,(id)=>{
  const routedShot=String(Array.isArray(route.query.shot)?route.query.shot[0]||'':route.query.shot||'')
  if(id&&routedShot!==id)void router.replace({query:{...route.query,shot:id}})
})
watch(()=>route.query.shot,(value)=>{const id=String(Array.isArray(value)?value[0]||'':value||'');if(id&&id!==selectedShotId.value&&episode.value?.shots.some((shot)=>shot.id===id))selectedShotId.value=id})
watch(selectedPromptInputs,(current,previous)=>{
  if(!current||!previous||current.shotId!==previous.shotId||current.fingerprint===previous.fingerprint)return
  const shot=selectedShot.value
  if(shot?.id===current.shotId)markShotInputsChanged(shot)
})
watch(() => selectedShot.value?.mediaType, mediaType => {
  if(mediaType==='image' && selectedShot.value){
    selectedShot.value.audioEnabled=false
    if(inspectorTab.value==='audio')inspectorTab.value='visual'
  }
})
const addShot = () => {
  if (!episode.value) return
  try {
    const item = newShot(episode.value.shots.length + 1)
    episode.value.shots.push(item)
    selectedShotId.value = item.id
  } catch (error) {
    studio.notify(error instanceof Error ? error.message : '新增分镜失败', 'error')
  }
}
const deleteShot = (id:string) => { if(!episode.value || episode.value.shots.length===1){studio.notify('至少保留一个镜头','error');return}; const index=episode.value.shots.findIndex(item=>item.id===id); episode.value.shots.splice(index,1);episode.value.shots.forEach((item,i)=>item.order=i+1);selectedShotId.value=episode.value.shots[Math.max(0,index-1)].id }
const moveShot=(from:number,to:number)=>{if(!episode.value||to<0||to>=episode.value.shots.length)return;const [item]=episode.value.shots.splice(from,1);episode.value.shots.splice(to,0,item);episode.value.shots.forEach((x,i)=>x.order=i+1)}
const persistEpisodeKeepingSelection = async (): Promise<Shot | null> => {
  if (!project.value || !episode.value) return null
  const previous = selectedShot.value ? clone(selectedShot.value) : null
  const previousId = selectedShotId.value
  const previousOrder = previous?.order
  episode.value = clone(await studio.saveEpisode(project.value, episode.value))
  selectedShotVersionKey.value = episode.value.activeShotVersionKey || episode.value.shotVersions?.find((item)=>item.active)?.key || selectedShotVersionKey.value
  const restored = episode.value.shots.find((item) => item.id === previousId)
    || episode.value.shots.find((item) => item.order === previousOrder)
    || episode.value.shots[0]
    || null
  selectedShotId.value = restored?.id || ''
  return restored
}
const save = async()=>{if(!project.value||!episode.value)return;saving.value=true;saveError.value='';try{await persistEpisodeKeepingSelection()}catch(e){saveError.value=e instanceof Error?e.message:'保存分镜失败';studio.notify(saveError.value,'error')}finally{saving.value=false}}
const thumbFor=(assetIds:string[])=>studio.assets.find(a=>assetIds.includes(a.id))?.thumbnail
</script>

<template>
  <div v-if="loading" class="page-loading"><div class="loader-ring"></div><span>正在装载分镜台…</span></div>
  <div v-else-if="project&&episode" class="storyboard-page">
    <p v-if="saveError" role="alert" class="field-error">保存未完成：{{ saveError }}。请修正后重新保存；页面内容尚不能视为已入库。</p>
    <header class="storyboard-header">
      <div class="storyboard-title"><button class="icon-btn" aria-label="返回生产工作台剧集与分镜" @click="router.push(projectInputsTarget)"><ArrowLeft :size="17"/></button><div><span>{{ project.title }} / EP {{ String(episode.number).padStart(2,'0') }}</span><h2>{{ episode.title }}</h2></div><StatusBadge :status="episode.status" :label="episode.status==='completed' ? '已装配' : undefined" title="剧集装配状态；最终验收与封存请查看对应生产运行。"/></div>
      <div class="storyboard-global"><label><span title="同步开启或关闭本集现有全部视频镜头的声音生成">声音生成（全部视频镜头）</span><ToggleSwitch v-model="episodeAudioEnabled"/></label><i></i><span>{{ videoShotCount }} 视频 · {{ imageShotCount }} 图片 · {{ totalDurationLabel }} 秒叙事时长</span></div>
      <div class="storyboard-actions">
        <button class="button ghost small" :disabled="saving" @click="save"><Save :size="15"/>{{ saving?'保存中':'保存' }}</button>
        <div class="episode-tool-group" aria-label="整集工具">
          <RouterLink class="button primary small" :to="productionTarget"><Sparkles :size="15"/>进入生产工作台</RouterLink>
        </div>
      </div>
    </header>

    <div v-if="episodeShotVersions.length>1" class="shot-version-bar">
      <div><Layers3 :size="16"/><span><strong>分镜版本</strong><small>当前显示 {{ activeShotVersionLabel || '已选版本' }}；保存所选版本会把它设为当前版本，旧版本仍保留用于对比。</small></span></div>
      <select :value="selectedShotVersionKey" :disabled="saving" @change="selectShotVersion(($event.target as HTMLSelectElement).value)">
        <option v-for="version in episodeShotVersions" :key="version.key" :value="version.key">{{ version.label }} · {{ version.shotCount }} 镜{{ version.active ? ' · 当前' : '' }}</option>
      </select>
    </div>

    <div class="storyboard-workspace">
      <aside class="shot-rail">
        <header><div><span>镜头列表 / SHOT LIST</span><strong>{{ episode.shots.length }} 个镜头</strong></div><button class="icon-btn" @click="addShot"><Plus :size="16"/></button></header>
        <div class="shot-list">
          <article v-for="(shot,index) in episode.shots" :key="shot.id" :class="{active:selectedShotId===shot.id}" @click="selectedShotId=shot.id">
            <button class="drag-handle"><GripVertical :size="14"/></button>
            <div class="shot-thumb" :style="{background:style?.gradient}"><span>{{ String(shot.order).padStart(2,'0') }}</span><Play v-if="selectedShotId===shot.id" :size="14" fill="currentColor"/></div>
            <div class="shot-row-copy"><div><strong>{{ shot.title }}</strong><small>{{ shot.mediaType==='image' ? `图片展示 ${formatNarrativeDuration(shotNarrativeDurationSeconds(shot))}s · 一致性参考图` : `${formatNarrativeDuration(shotNarrativeDurationSeconds(shot))}s 视频 · ${shot.mode}` }}</small></div><div><i :class="shot.status"></i><span>{{ shot.status==='rendering'?`${shot.mediaType==='image'?'图片':'视频'}生成中`:shot.status==='done'?`${shot.mediaType==='image'?'图片':'视频'}就绪`:shot.status==='ready'?'提示词就绪':'待编辑' }}</span><AudioLines v-if="shot.mediaType==='video'&&shot.audioEnabled" :size="12"/></div></div>
            <div class="shot-row-actions"><button @click.stop="moveShot(index,index-1)" :disabled="index===0"><ChevronLeft :size="12"/></button><button @click.stop="moveShot(index,index+1)" :disabled="index===episode.shots.length-1"><ChevronRight :size="12"/></button></div>
          </article>
          <button class="add-shot-inline" @click="addShot"><Plus :size="17"/><span>新增镜头</span></button>
        </div>
      </aside>

      <main v-if="selectedShot" class="shot-editor">
        <section class="preview-stage">
          <div class="preview-toolbar"><span>镜头规划 / SHOT PLAN</span><RouterLink class="button ghost small" :to="productionTarget"><Film :size="14"/>查看生产与采用结果</RouterLink></div>
          <div class="preview-canvas" :class="[`ratio-${project.aspectRatio.replace(':','-')}`]" :style="{background:style?.gradient}">
            <div class="preview-grid-lines"></div><div class="preview-vignette"></div>
            <div class="preview-scene-label"><span>{{ project.bible.scenes.find(s=>s.id===selectedShot?.sceneId)?.name||'未指定场景' }}</span></div>
            <div class="preview-empty-state"><Film :size="24"/><strong>生成、预览与采用统一在生产工作台处理</strong><small>这里展示镜头规划，不按最近成功任务推断已采用结果，也不修改镜头的生产状态。</small></div>
            <div class="preview-hud top"><span>EP{{ String(episode.number).padStart(2,'0') }} / SHOT {{ String(selectedShot.order).padStart(2,'0') }}</span><span>规划内容 · 非生成画面</span></div>
            <div class="preview-hud bottom"><span>{{ selectedShot.mediaType==='image'?'图片规划':'视频规划' }}</span><span>{{ formatNarrativeDuration(shotNarrativeDurationSeconds(selectedShot)) }} SEC</span><span>{{ project.aspectRatio }} · {{ project.resolution }}</span></div>
          </div>
        </section>

        <section class="editor-panel panel">
          <nav class="inspector-tabs"><button :class="{active:inspectorTab==='visual'}" @click="inspectorTab='visual'"><MonitorPlay :size="15"/>分镜与独立字段</button><button :class="{active:inspectorTab==='continuity'}" @click="inspectorTab='continuity'"><Lock :size="15"/>一致性引用 <span>{{ continuityCount }}</span></button><button :disabled="selectedShot.mediaType==='image'" :class="{active:inspectorTab==='audio'}" @click="inspectorTab='audio'"><AudioLines :size="15"/>原生音频</button></nav>
          <div v-if="inspectorTab==='visual'" class="inspector-content">
            <ShotExecutionEvidence :project-id="project.id" :shot-id="selectedShot.id" />
            <ShotSourceFields :project-id="project.id" :shot-id="selectedShot.id" />
            <ShotFieldRevisions :project-id="project.id" :shot-id="selectedShot.id" @changed="markShotInputsChanged(selectedShot)" />
            <div class="form-grid four">
              <label class="field"><span class="field-label">镜头标题 <PromptImpactBadge kind="label" title="只用于镜头列表、任务识别和检索，不写入模型导演描述。"/></span><input v-model="selectedShot.title" placeholder="例如：雨夜入栈"></label>
              <label class="field"><span class="field-label">生成类型 <PromptImpactBadge kind="production" title="决定生成图片或视频，并切换对应工作流字段。"/></span><select v-model="selectedShot.mediaType"><option value="video">视频 · 已发布工作流</option><option value="image">图片 · 一致性参考帧</option></select></label>
              <div class="field"><ShotTimingEditor :shot="selectedShot" :minimum-seconds="selectedShot.mediaType === 'video' ? videoShotMinDurationSeconds : 0.001" :maximum-seconds="selectedShot.mediaType === 'video' ? videoShotMaxDurationSeconds : undefined" @change="Object.assign(selectedShot, $event)"/><small v-if="productionCapability && selectedShot.mediaType === 'video'" :class="{'field-error':selectedShotDurationOutOfRange}">当前正式视频范围 {{ videoShotMinDurationSeconds }}–{{ videoShotMaxDurationSeconds }} 秒<span v-if="selectedShotDurationOutOfRange">；请先调整到范围内</span></small><small v-else-if="selectedShot.mediaType === 'video'" class="field-error">{{ productionCapabilityError || '正在读取正式视频时长配置…' }}</small></div>
            </div>
            <div class="mode-guide"><Sparkles :size="16"/><div><strong>按本次执行的提示词策略与输入合同准备</strong><p>此处保存时序、原对白与明确的实体关系。动作、运镜、声景、配乐等创作内容通过上方独立字段读取和另存；没有结构化来源时请先在剧本页填写并解析，或在本次执行允许的人工输入中填写，不自动推造字段。提示词字段组装在策略中定义，素材来源、输入槽位及必要的模型用途在本次运行中配置；不会根据镜头描述猜测或自动绑定素材。</p></div></div>
          </div>
          <div v-else-if="inspectorTab==='continuity'" class="inspector-content continuity-selector">
            <div class="selector-group"><header><div><Link2 :size="16"/><span>首尾帧连续要求</span><PromptImpactBadge kind="continuity" title="这是导演与生产意图，不代表素材槽位已经绑定。"/></div><small>母稿默认值可在“生产工作台 → 视觉与术语”的拆解参数中调整</small></header><div class="form-grid two"><div class="setting-toggle"><div><strong>首帧承接上一镜</strong><small>开始状态需匹配上一镜结束状态</small></div><ToggleSwitch v-model="selectedShot.requiresFirstFrameContinuity"/></div><div class="setting-toggle"><div><strong>尾帧衔接下一镜</strong><small>结束时保留稳定、可匹配的状态与构图</small></div><ToggleSwitch v-model="selectedShot.requiresLastFrameContinuity"/></div></div></div>
            <div class="selector-group"><header><div><UserRound :size="16"/><span>本镜头出镜人物</span><PromptImpactBadge kind="continuity" title="人物 Bible 定义参与身份与服装一致性；具体素材通过运行中的逐次引用选择。"/></div><small>可多选 · 不包含仅发声人物</small></header><div class="continuity-options"><button v-for="item in project.bible.characters" :key="item.id" :class="{active:visibleCharacterIds(selectedShot).includes(item.id)}" @click="toggleEntity('character',item.id)"><div><AssetImage v-if="thumbFor(bibleAssetIds(item))" :src="thumbFor(bibleAssetIds(item))" :label="item.name" :accent="style?.accent"/><UserRound v-else :size="20"/></div><span><strong>{{ item.name }}</strong><small>一致性视觉锚点</small></span><i><Check :size="12"/></i></button></div></div>
            <div class="selector-group"><header><div><Image :size="16"/><span>场景锚点</span><PromptImpactBadge kind="continuity" title="场景 Bible 定义参与固定空间结构、光线与色彩连续性。"/></div><small>单选 · 明确场景关系</small></header><div class="continuity-options"><button v-for="item in project.bible.scenes" :key="item.id" :class="{active:selectedShot.sceneId===item.id}" @click="selectScene(item.id)"><div><AssetImage v-if="thumbFor(bibleAssetIds(item))" :src="thumbFor(bibleAssetIds(item))" :label="item.name" :accent="style?.accent"/><Image v-else :size="20"/></div><span><strong>{{ item.name }}</strong><small>已锁定空间结构</small></span><i><Check :size="12"/></i></button></div></div>
            <div class="selector-group"><header><div><Box :size="16"/><span>关键道具</span><PromptImpactBadge kind="continuity" title="道具 Bible 定义参与形状、材质、比例与持久标记的一致性。"/></div><small>可多选 · 明确道具关系</small></header><div class="continuity-options"><button v-for="item in project.bible.props" :key="item.id" :class="{active:selectedShot.propIds.includes(item.id)}" @click="toggleEntity('prop',item.id)"><div><AssetImage v-if="thumbFor(bibleAssetIds(item))" :src="thumbFor(bibleAssetIds(item))" :label="item.name" :accent="style?.accent"/><Box v-else :size="20"/></div><span><strong>{{ item.name }}</strong><small>一致性视觉锚点</small></span><i><Check :size="12"/></i></button></div></div>

            <section class="reference-mode-empty" aria-label="逐次素材引用入口"><Link2 :size="19"/><div><strong>素材按本次运行的逐次引用选择</strong><p>此处只编辑出镜人物、场景与道具关系，不会自动选图或分配首尾帧。先保存分镜，再到生产工作台的“选择素材 / 上传（单镜 / 多镜）”查看确切版本、选择库内素材或上传；自动来源按配方配置处理。模型用途、槽位和输入顺序以每次执行准备为准。</p><RouterLink class="button ghost small" :to="productionTarget">进入生产工作台选材</RouterLink></div></section>
          </div>
          <div v-else-if="selectedShot.mediaType==='video'" class="inspector-content audio-editor">
            <div class="audio-master"><div><AudioLines :size="20"/><span><strong>视频声音生成</strong><small>声音能力以本次执行实例合同为准；开关不会清空原对白</small></span></div><ToggleSwitch v-model="selectedShot.audioEnabled"/></div>
            <div :class="{disabled:!selectedShot.audioEnabled}" class="audio-fields">
              <ShotDialogueEditor :shot="selectedShot" :characters="project.bible.characters" :disabled="saving" @change="markShotInputsChanged(selectedShot)" />
            </div>
          </div>
        </section>

        <section class="timeline-panel panel">
          <header><div><span class="eyebrow">剧集时间线 / EPISODE TIMELINE</span><strong>{{ videoShotCount }} 个视频（VIDEO）· {{ imageShotCount }} 张图片（IMAGE）· {{ totalDurationLabel }} 秒叙事</strong></div><div class="timeline-scale"><span>0 秒</span><span>{{ formatNarrativeDuration(totalDuration/2) }} 秒</span><span>{{ totalDurationLabel }} 秒</span></div></header>
          <div class="timeline-track"><button v-for="shot in episode.shots" :key="shot.id" :class="{active:selectedShotId===shot.id}" :style="{flexGrow:shotNarrativeDurationSeconds(shot)}" @click="selectedShotId=shot.id"><i :style="{background:style?.accent}"></i><strong>{{ String(shot.order).padStart(2,'0') }}</strong><span>{{ shot.title }}</span><small>{{ shot.mediaType==='image'?`IMG · ${formatNarrativeDuration(shotNarrativeDurationSeconds(shot))}s`:`${formatNarrativeDuration(shotNarrativeDurationSeconds(shot))}s` }}</small></button></div>
          <div v-if="episode.audioEnabled&&episode.shots.some(shot=>shot.mediaType==='video')" class="audio-track"><AudioLines :size="13"/><div><i v-for="shot in episode.shots.filter(item=>item.mediaType==='video')" :key="shot.id" :style="{flexGrow:shot.duration}"><b v-for="n in 16" :key="n" :style="{height:`${20+((n*shot.order*13)%70)}%`}"></b></i></div></div>
        </section>
      </main>

      <aside v-if="selectedShot" class="prompt-panel">
        <header><div><span class="eyebrow">EXECUTION PREPARATION</span><h3>执行准备与提示词</h3></div></header>
        <div class="conditioning-mode-card"><div><component :is="selectedShot.mediaType==='image'?Image:Sparkles" :size="17"/><span><strong>{{ selectedShot.mediaType==='image'?'图片分镜':'视频分镜' }}</strong><small>此处编辑分镜事实，不代表已生成或已验收</small></span></div><span>{{ selectedShot.mediaType==='image'?'IMAGE':'VIDEO' }}</span></div>
        <div class="prompt-empty"><div><WandSparkles :size="27"/></div><h4>先保存分镜，再准备执行</h4><p>在生产工作台选择执行实例版本。提示词可按该实例的策略组装，也可在允许人工输入时填写；素材槽位与参数以该实例合同为准。</p><p>执行准备中查看并确认本次内容；任务创建后，在“任务中心 → 任务详情 → 输入与实际提示词”查看实际请求。其他任务的提示词不会套用到当前分镜。</p><RouterLink class="button primary full" :to="productionTarget">进入生产工作台</RouterLink></div>
        <button class="delete-shot" @click="deleteShot(selectedShot.id)"><Trash2 :size="14"/>删除当前镜头</button>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.storyboard-actions{align-items:center}.episode-tool-group{display:flex;align-items:center;gap:7px;padding-left:7px;border-left:1px solid var(--border)}.episode-tool-group .button{white-space:nowrap}@media(max-width:720px){.storyboard-actions{width:100%;overflow-x:auto;padding-bottom:3px;justify-self:stretch;flex-wrap:nowrap}.storyboard-actions>.button{flex:none}.episode-tool-group{min-width:max-content;flex-wrap:nowrap}}

.shot-version-bar{margin:10px 14px 0;padding:10px 12px;display:flex;align-items:center;justify-content:space-between;gap:12px;border:1px solid rgba(140,111,230,.24);border-radius:8px;background:rgba(140,111,230,.045)}.shot-version-bar>div{min-width:0;display:flex;align-items:flex-start;gap:8px;color:#bdaeff}.shot-version-bar span{min-width:0;display:flex;flex-direction:column;gap:3px}.shot-version-bar strong{font-size:11px;color:#d8d0ff}.shot-version-bar small{font-size:10px;line-height:1.55;color:#85809f}.shot-version-bar select{min-width:220px;height:34px;padding:0 30px 0 10px;border:1px solid rgba(140,111,230,.28);border-radius:6px;background:#0b0e14;color:#d7d1ff;font-size:11px}@media(max-width:720px){.shot-version-bar{align-items:stretch;flex-direction:column}.shot-version-bar select{width:100%;min-width:0}}
.field-label{display:flex;align-items:center;justify-content:space-between;gap:7px;flex-wrap:wrap}.field>small{font-size:11px;line-height:1.55;color:#6f757d}.prompt-example-list{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.prompt-example-list button{min-height:30px;padding:5px 9px;border:1px solid rgba(230,167,90,.18);border-radius:4px;background:rgba(230,167,90,.035);color:#aa8358;font-size:10px;cursor:pointer}.prompt-example-list button:hover{border-color:rgba(230,167,90,.4);color:var(--accent)}.prompt-example-list small{font-size:10px;color:#676d75}.selector-group>header>div{flex-wrap:wrap}.audio-fields .field>span{flex-wrap:wrap}.resolution-impact-header{display:flex;align-items:center;justify-content:space-between;gap:8px}.resolution-impact-header>span{font-size:11px;font-weight:600;color:#a9adb3}.prompt-quality-panel{padding:12px;display:flex;flex-direction:column;gap:10px;border:1px solid rgba(104,197,167,.2);border-radius:7px;background:rgba(104,197,167,.035)}.prompt-quality-panel.attention{border-color:rgba(229,113,117,.28);background:rgba(229,113,117,.035)}.prompt-quality-panel>header{display:flex;align-items:center;justify-content:space-between;gap:8px}.prompt-quality-panel>header>div{min-width:0;display:flex;align-items:center;gap:8px;color:var(--green)}.prompt-quality-panel.attention>header>div{color:#e57175}.prompt-quality-panel>header span{min-width:0;display:flex;flex-direction:column;gap:3px}.prompt-quality-panel>header small{font:10px Consolas,monospace;letter-spacing:.1em;color:#767e7d}.prompt-quality-panel>header strong{font-size:12px;color:#c8cdce}.quality-audit-note{margin:0;color:#777f86;font-size:10px;line-height:1.55}.prompt-quality-metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}.prompt-quality-metrics>span{min-width:0;padding:8px;border:1px solid rgba(255,255,255,.055);border-radius:4px;background:#0a0d11;color:#7b8289;font-size:10px;line-height:1.5}.prompt-quality-metrics strong{display:block;margin-top:3px;color:#bbc0c5;font-size:11px;word-break:break-word}.quality-detail{display:flex;flex-direction:column;gap:7px}.quality-detail>strong{font-size:11px;color:#aeb4ba}.quality-detail>div{display:flex;gap:5px;flex-wrap:wrap}.quality-detail code{padding:5px 7px;border:1px solid rgba(230,167,90,.18);border-radius:4px;background:#0a0d11;color:#cf995b;font-size:10px;word-break:break-word}.quality-detail ul{max-height:160px;margin:0;padding:9px 9px 9px 24px;overflow:auto;border-radius:4px;background:#0a0d11}.quality-detail li{margin:0 0 6px;color:#bf888b;font-size:11px;line-height:1.55}.quality-detail li:last-child{margin-bottom:0}
.image-resolution-guidance{padding:8px;display:flex;align-items:flex-start;gap:7px;border:1px solid rgba(104,197,167,.18);border-radius:6px;background:rgba(104,197,167,.035);color:var(--green)}.image-resolution-guidance span{font-size:8px;line-height:1.55;color:#788c85}.image-resolution-guidance strong{display:block;color:#9bc5b7;font-size:9px}
.shot-regeneration-confirm{display:flex;align-items:flex-start;gap:11px;padding:13px;border:1px solid rgba(218,158,73,.25);border-radius:8px;background:rgba(218,158,73,.055);color:#d2a15c}.shot-regeneration-confirm>span{display:grid;gap:5px}.shot-regeneration-confirm strong{font-size:11px;color:#d8b276}.shot-regeneration-confirm small,.shot-regeneration-confirm p{margin:0;color:#858d94;font-size:9px;line-height:1.6}
.whole-episode-idempotency-conflict{display:flex;align-items:flex-start;gap:8px;padding:10px;border:1px solid rgba(223,101,90,.28);border-radius:7px;background:rgba(223,101,90,.055);color:#df8177}.whole-episode-idempotency-conflict>span{display:grid;gap:5px}.whole-episode-idempotency-conflict small{color:#a68585;font-size:9px;line-height:1.55}.whole-episode-idempotency-conflict button{justify-self:start}
.whole-episode-image-resolution{padding:9px;border:1px solid rgba(104,197,167,.18);border-radius:7px;background:rgba(104,197,167,.025)}.whole-episode-image-resolution summary{color:#9bc5b7;font-size:10px;font-weight:650;cursor:pointer}.whole-episode-image-resolution>.resolution-picker{margin-top:9px}.whole-episode-image-resolution>p{margin:7px 0 0!important;color:#74857f!important;font-size:8px!important;line-height:1.55!important}
</style>
