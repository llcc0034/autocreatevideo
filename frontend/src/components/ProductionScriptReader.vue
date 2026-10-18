<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { platformApi } from '@/services/platformApi'
import { scriptRevisionStructures, type ScriptRevisionStructure } from '@/services/scriptRevisionStructures'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { asRecord, asRows, semanticStructure, savedShot, declaredAppearanceUses } from '@/services/projectVersionContents'
import type { ScriptRevision, ProductionRun } from '@/services/platformTypes'
import StructuredJsonEditor from './StructuredJsonEditor.vue'
import ProductionShotResults from './ProductionShotResults.vue'
const props = defineProps<{ revisionId: string; section: 'script' | 'terms' | 'episodes'; run: ProductionRun }>()
const revision = ref<ScriptRevision | null>(null), structure = ref<ScriptRevisionStructure | null>(null)
const loading = ref(false), error = ref(''), page = ref(1), episodeIndex = ref(0)
let serial = 0
async function load() {
  const token = ++serial, id = props.revisionId
  revision.value = null; structure.value = null; error.value = ''; loading.value = false; page.value = 1; episodeIndex.value = 0
  if (!id) return
  loading.value = true
  try {
    const [text, detail] = await Promise.all([platformApi.getScriptRevision(id), scriptRevisionStructures.read(id)])
    if (token !== serial) return
    if (text.id !== id || detail && (asRecord(detail.document.revision).id !== id || asRecord(detail.document.revision).content_sha256 !== text.contentSha256)) throw new Error('剧本内容与本轮采用修订不一致。')
    revision.value = text; structure.value = detail
  } catch (cause) { if (token === serial) error.value = apiErrorSummary(cause) }
  finally { if (token === serial) loading.value = false }
}
const semantic = computed(() => semanticStructure(structure.value?.document || {}))
const episode = computed(() => semantic.value.episodes[episodeIndex.value])
const shots = computed(() => asRows(episode.value?.shots))
const visibleShots = computed(() => shots.value.slice((page.value - 1) * 8, page.value * 8))
const entityName = (key: unknown) => semantic.value.entities.find(entity => entity.key === key)?.name || '未记录人物名称'
const output = (shot: unknown) => savedShot(props.run, episode.value?.episode, shot)
const uses = (entity: unknown, variant: unknown) => declaredAppearanceUses(semantic.value.episodes, entity, variant)
  .map(use => `第 ${use.episode} 集 · 镜头 ${use.shot}`).join('、') || '剧本未声明使用镜头'
watch(episodeIndex, () => { page.value = 1 })
watch(() => props.revisionId, load, { immediate: true })
onBeforeUnmount(() => { serial++ })
</script>
<template>
  <section class="script-reader" aria-label="本版本可读剧本">
    <p v-if="loading" role="status">正在读取本版本采用的剧本…</p>
    <p v-else-if="error" role="alert">{{error}} <button class="button ghost small" @click="load">重新读取剧本</button></p>
    <p v-else-if="!revisionId">本版本尚未采用剧本，不展示其他制作版本的内容。</p>
    <template v-else-if="revision">
      <p class="reader-source">来源：本轮采用剧本修订 {{revision.revision}} · 只读</p>
      <template v-if="!structure"><p>本修订没有保存可读结构化结果。请从创作输入查看原稿或在制作台完成拆解；不会自动重新解析或调用模型。</p></template>
      <template v-else-if="section==='terms'">
        <p>统一名称与参考名称来自本轮剧本的实体定义，用于对应人物、场景、道具及确切素材，不按译名猜测身份。未冻结的项目英文术语不会混入本版本。</p>
        <p>主图与变体属于同一实体，不新增角色身份。下面的镜头来自剧本中明确的实体键／变体键；实际采用哪张图，以各镜头冻结的素材输入与任务回执为准。</p>
        <div class="reader-table"><table><thead><tr><th>统一名称</th><th>类型</th><th>主图参考名称与使用镜头</th><th>变体参考名称与使用镜头</th></tr></thead><tbody><tr v-for="entity in semantic.entities" :key="String(entity.key)"><th scope="row">{{entity.name}}</th><td>{{({character:'人物',scene:'场景',prop:'道具'} as Record<string,string>)[String(entity.kind)] || entity.kind}}</td><td><strong>{{asRecord(entity.primary).reference_name || '未指定'}}</strong><p class="reader-source">{{uses(entity.key, null)}}</p></td><td><div v-for="variant in asRows(entity.variants)" :key="String(variant.key)" class="term-variant"><strong>{{variant.reference_name || '未指定变体参考名称'}}</strong><p class="reader-source">{{uses(entity.key, variant.key)}}</p><p class="reader-prose">{{variant.anchor}}</p></div><template v-if="!asRows(entity.variants).length">未声明变体</template></td></tr></tbody></table></div>
        <p v-if="!semantic.entities.length">本修订没有实体定义。</p>
      </template>
      <template v-else>
        <template v-if="section==='script'"><h3>故事中的人物、场景与道具</h3><div class="reader-entities"><article v-for="entity in semantic.entities" :key="String(entity.key)"><h4>{{entity.name}}</h4><p class="reader-prose">{{entity.identity_anchor}}</p><p class="reader-prose">{{entity.description}}</p><details v-if="asRows(entity.variants).length"><summary>变体设定</summary><div v-for="variant in asRows(entity.variants)" :key="String(variant.key)"><strong>{{variant.reference_name}}</strong><p class="reader-prose">{{variant.anchor}}</p></div></details></article></div></template>
        <h3>{{section==='script'?'结构化剧情':'剧集与分镜内容'}}</h3>
        <p v-if="!semantic.episodes.length">本修订尚无剧集与镜头。</p>
        <template v-else>
          <label class="field"><span>剧集</span><select v-model.number="episodeIndex"><option v-for="(item,index) in semantic.episodes" :key="index" :value="index">第 {{item.episode}} 集{{item.title ? ` · ${item.title}` : ''}}</option></select></label>
          <p>共 {{shots.length}} 镜 · 显示 {{shots.length ? (page-1)*8+1 : 0}}–{{Math.min(page*8,shots.length)}} 镜</p>
          <article v-for="shot in visibleShots" :key="String(shot.shot)" class="reader-shot"><header><h4>镜头 {{shot.shot}}</h4><span>{{typeof shot.duration_ms==='number' ? `${shot.duration_ms/1000} 秒` : '时长未记录'}}</span></header><p class="reader-prose">{{shot.action || '未记录镜头动作'}}</p>
            <blockquote v-if="asRecord(shot.dialogue).text"><strong>{{entityName(asRecord(shot.dialogue).speaker_entity_key)}}{{asRecord(shot.dialogue).is_voiceover?'（画外音）':''}}</strong><p class="reader-source">本句语言：{{typeof asRecord(shot.dialogue).language === 'string' && String(asRecord(shot.dialogue).language).trim() ? asRecord(shot.dialogue).language : '未声明'}}</p><p v-if="asRecord(shot.dialogue).delivery" class="reader-source">演绎说明（不发声）：{{asRecord(shot.dialogue).delivery}}</p><p class="reader-prose">{{asRecord(shot.dialogue).text}}</p></blockquote><p v-else class="reader-source">无对白</p>
            <ul v-if="asRows(shot.references).length" class="shot-references"><li v-for="item in asRows(shot.references)" :key="String(item.use_key)"><strong>{{entityName(item.entity_key)}}</strong> · {{item.purpose}}</li></ul>
            <template v-if="section==='episodes'">
              <p v-if="output(shot.shot).error" role="alert">{{output(shot.shot).error}}</p>
              <template v-else-if="output(shot.shot).shot"><p class="reader-source">本轮已采用分镜 · {{asRows(output(shot.shot).shot?.reference_uses).length}} 项冻结素材输入</p>
                <ProductionShotResults :key="String(output(shot.shot).shot?.id)" :run="run" :shot-id="String(output(shot.shot).shot?.id)"/>
              </template><p v-else class="reader-source">本轮尚未保存此剧本镜头对应的分镜结果。</p>
            </template>
          </article>
          <nav class="reader-pagination" aria-label="镜头分页"><button class="button ghost small" :disabled="page<=1" @click="page--">上一页镜头</button><span>第 {{page}} 页</span><button class="button ghost small" :disabled="page*8>=shots.length" @click="page++">下一页镜头</button></nav>
        </template>
        <details v-if="section==='script'"><summary>全部配置字段（含自定义内容）</summary><StructuredJsonEditor :model-value="asRecord(structure.document.projection).source_value" :schemas="[structure.json_schema]" label="剧本字段" readonly/></details>
      </template>
    </template>
  </section>
</template>
<style scoped>
.script-reader{display:grid;gap:16px;min-width:0;font-size:14px;line-height:1.7}.script-reader :is(p,h3,h4){margin:0}.reader-source{color:var(--text-muted)}.reader-prose{white-space:pre-wrap;overflow-wrap:anywhere}.reader-entities{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:16px}.reader-entities article,.reader-shot{display:grid;gap:10px;padding:18px;border:1px solid var(--border);border-radius:9px;background:var(--bg-soft)}.reader-shot header,.reader-pagination{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.script-reader h3{font-size:17px}.script-reader h4{font-size:15px}.script-reader blockquote{margin:0;border-left:3px solid var(--accent);padding:8px 14px;background:var(--bg-soft)}.reader-table{overflow:auto}.reader-table table{border-collapse:collapse;width:100%;min-width:480px;text-align:left}.reader-table :is(th,td){padding:12px;border-bottom:1px solid var(--border)}.reader-table th{font-weight:600}.script-reader summary{cursor:pointer;padding:10px 0}.shot-references{padding-left:20px;color:var(--text-muted)}.script-reader select{max-width:100%}
</style>
