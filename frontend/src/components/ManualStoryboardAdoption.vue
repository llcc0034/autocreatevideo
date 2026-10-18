<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { buildManualStoryboardAdoption, buildManualStoryboardFromScript, readManualStoryboard, type ManualStoryboardDetail } from '@/services/productionManualStoryboard'
import type { ManualActionOptions } from '@/services/productionManual'
import type { JsonObject, ProductionRun } from '@/services/platformTypes'

const props = defineProps<{ run: ProductionRun; options: ManualActionOptions }>()
const emit = defineEmits<{ changed: []; busy: [value: boolean] }>()
const detail = ref<ManualStoryboardDetail | null>(null), loading = ref(false), submitting = ref(false), error = ref(''), notice = ref('')
const checked = ref<string[]>([]), comment = ref(''), pending = ref<JsonObject | null>(null), key = ref(crypto.randomUUID())
const scriptConfirmed = ref(false)
const terminal = computed(() => ['succeeded','failed','cancelled'].includes(props.run.status))
const ready = computed(() => detail.value && detail.value.content.episodes.length > 0
  && detail.value.content.episodes.every(episode => episode.shots.length > 0 && episode.shots.every(shot => checked.value.includes(shot.id))))
const labels = { not_adopted: '尚未采用', current: '与已采用内容一致', changed: '分镜或剧本依据已变化，需要重新核对' }
let sequence = 0
async function load() {
  if (submitting.value) return
  const token = ++sequence; loading.value = true; error.value = ''; detail.value = null; checked.value = []; scriptConfirmed.value = false
  try {
    const raw = await platformApi.getManualStoryboard(props.run.id)
    if (token !== sequence) return
    detail.value = readManualStoryboard(raw, props.run); pending.value = null; key.value = crypto.randomUUID()
  } catch (e) { if (token === sequence) error.value = apiErrorSummary(e) }
  finally { if (token === sequence) loading.value = false }
}
async function adopt(fromScript = false) {
  if (loading.value || submitting.value || terminal.value || !detail.value
    || (!pending.value && (fromScript ? !scriptConfirmed.value || !detail.value.materialization.ready : !ready.value))) return
  const token = sequence; submitting.value = true; error.value = ''; notice.value = ''; emit('busy', true)
  try {
    pending.value ??= fromScript
      ? buildManualStoryboardFromScript(detail.value, props.run, props.options, key.value, scriptConfirmed.value, comment.value)
      : buildManualStoryboardAdoption(detail.value, props.run, props.options, key.value, checked.value, comment.value)
    const result = await platformApi.performManualProductionAction(props.run.id, pending.value)
    if (result.action !== 'adopt' || result.result_kind !== 'approval_decision' || typeof result.target_id !== 'string'
      || !/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(result.target_id)) throw new Error('未返回正式采用记录，请重试同一请求核对结果')
    if (token !== sequence) return
    pending.value = null; notice.value = '已记录本轮明确采用的分镜内容；请继续核对分镜。没有生成媒体、选择执行实例或开始下一步。'; emit('changed')
  } catch (e) { if (token === sequence) error.value = apiErrorSummary(e) }
  finally { submitting.value = false; emit('busy', false) }
  if (notice.value) await load()
}
watch(() => props.run.id, () => { pending.value = null; notice.value = ''; void load() }, { immediate: true })
onBeforeUnmount(() => { sequence++ })
</script>

<template>
  <section class="manual-storyboard-adoption">
    <header><h4>采用本轮分镜内容</h4><button class="button ghost small" :disabled="loading || submitting" @click="load">重新读取完整分镜</button></header>
    <p>可以采用已保存的分镜，或明确采用本轮已确认剧本创建分镜；页面读取不自动拆稿或调用模型。这里核对创作内容，参考素材及槽位需单独审核；不会因后续更换参考素材而自动作废创作内容的采用。</p>
    <p v-if="loading" role="status">正在读取本轮范围的完整分镜…</p>
    <p v-if="error" role="alert" class="platform-alert danger">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
    <template v-if="detail">
      <p :class="{ 'platform-alert warning': detail.adoption.status === 'changed' }">{{ labels[detail.adoption.status] }} · 采用修订 {{ detail.adoption.revision }}</p>
      <p v-if="detail.content.script_decision_id">当前剧本采用决定：{{ detail.content.script_decision_id }}。重新采用剧本后需重新核对分镜，正文相同也不沿用旧确认。</p>
      <p v-else>本轮尚未采用剧本；可核对后采用手工分镜，不会为此补造剧本。</p>
      <section v-if="!detail.content.episodes.length" class="episode">
        <h5>从本轮已确认剧本创建分镜</h5>
        <p v-if="!detail.materialization.ready">{{ detail.materialization.reason }}也可以回项目工作台手工创建，再回此处采用。</p>
        <template v-else-if="detail.materialization.contract">
          <p>仅使用已采用的结构化修订 {{ detail.materialization.contract.source_basis.script.revision }} 及本轮 Bible 决定；不会使用后来编辑的母稿，不调用 AI，不重新解析正文或按名字猜测关系。独立创作字段保留原值及版本，组装只在提示词策略中配置。</p>
          <dl><dt>分集与镜头</dt><dd>按原结构中的声明编号创建，不推测集数、不自动切段</dd>
            <dt>默认镜头时长</dt><dd>{{ Number(detail.materialization.contract.parameters.default_shot_duration_ms) / 1000 }} 秒；有明确时长时使用原文并校验范围</dd>
            <dt>规划媒体</dt><dd>{{ detail.materialization.contract.parameters.default_shot_media_type === 'image' ? '图片' : '视频' }}</dd>
            <dt>参考模式</dt><dd>生成时由所选执行实例确定，此处不预选模式</dd></dl>
          <details><summary>查看确切结构、Bible依据与冻结参数</summary><pre>{{ JSON.stringify(detail.materialization.contract,null,2) }}</pre><p>创建默认值来自本轮冻结配置；如需调整，请修改对应配置后新建制作轮次。</p></details>
          <label><input v-model="scriptConfirmed" type="checkbox" :disabled="submitting || terminal || Boolean(pending)">我已核对本轮已采用的剧本和上述参数，将创建分镜供继续编辑、审核。</label>
          <button class="button primary small" :disabled="loading || submitting || terminal || (!pending && !scriptConfirmed)" @click="adopt(true)">{{ submitting ? '正在创建…' : pending ? '重试同一采用请求' : '采用该剧本并创建分镜' }}</button>
        </template>
      </section>
      <div v-if="detail.adoption.source?.kind === 'confirmed_script'">
        <p>这些分镜来自确切结构化声明；原来源与本次人工采用决定已记录，没有自动审核素材或生成媒体。</p>
        <details><summary>本次拆稿的准确来源与参数</summary><pre>{{ JSON.stringify(detail.adoption.source,null,2) }}</pre></details>
      </div>
      <section v-for="episode in detail.content.episodes" :key="episode.id" class="episode">
        <header><h5>第{{ episode.number }}集 · {{ episode.title }}</h5><RouterLink v-if="!submitting" :to="{ name:'storyboard', params:{projectId:run.project,episodeId:episode.id} }">查看 / 编辑原分镜</RouterLink></header>
        <p>{{ episode.synopsis }}</p><p v-if="!episode.shots.length">此集尚无镜头，不能采用空分镜。</p>
        <article v-for="shot in episode.shots" :key="shot.id">
          <h6>镜头{{ shot.number }} · {{ shot.title }} · {{ shot.media_type === 'image' ? '图片' : '视频' }} · {{ shot.duration_ms / 1000 }}秒</h6>
          <p v-if="shot.episode_start_ms != null && shot.episode_end_ms != null">原稿 / 人工声明的本集时间：{{ shot.episode_start_ms / 1000 }}–{{ shot.episode_end_ms / 1000 }} 秒；本镜头生成使用 {{ shot.duration_ms / 1000 }} 秒，镜头内从 0 秒计时。</p>
          <p v-if="shot.script_source">创作字段独立保存在所绑定的结构修订中，未拼入旧叙事/描述栏。本页核对来源、时长与明确关系；通过“查看 / 编辑原分镜”逐项查看或另存创作字段，提示词组装由所选策略配置。</p>
          <p v-else>创作原值与人工修订请在分镜页的独立字段中核对；不会从固定正文拼接。</p>
          <details><summary>来源、对白与出镜/发声关系、场景、道具</summary><pre>{{ JSON.stringify({script_source:shot.script_source,dialogue:shot.dialogue,scene_id:shot.scene_id,character_relations:shot.character_relations,reference_uses:shot.reference_uses,prop_ids:shot.prop_ids},null,2) }}</pre></details>
          <label><input v-model="checked" type="checkbox" :value="shot.id" :disabled="submitting || terminal || Boolean(pending)">已核对本镜头创作内容、时长及角色场景道具</label>
        </article>
      </section>
      <label>采用说明（可选）<textarea v-model="comment" rows="2" :disabled="submitting || terminal || Boolean(pending)"/></label>
      <button v-if="detail.content.episodes.length" class="button primary small" :disabled="loading || submitting || terminal || (!pending && !ready)" @click="adopt(false)">{{ submitting ? '正在提交…' : pending ? '重试同一采用请求' : '采用已核对的分镜内容' }}</button>
    </template>
  </section>
</template>

<style scoped>
.manual-storyboard-adoption{display:grid;gap:12px}.manual-storyboard-adoption header{display:flex;gap:12px;justify-content:space-between;align-items:center;flex-wrap:wrap}.manual-storyboard-adoption h4,.manual-storyboard-adoption h5,.manual-storyboard-adoption h6,.manual-storyboard-adoption p{margin:0}.manual-storyboard-adoption .episode,.manual-storyboard-adoption article{display:grid;gap:12px;padding:14px;border:1px solid var(--border);border-radius:8px}.manual-storyboard-adoption p,.manual-storyboard-adoption dd{white-space:pre-wrap;overflow-wrap:anywhere}.manual-storyboard-adoption dl{display:grid;grid-template-columns:90px 1fr;gap:8px;margin:0}.manual-storyboard-adoption dd{margin:0}.manual-storyboard-adoption label{display:flex;gap:8px;align-items:center}.manual-storyboard-adoption pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:260px;overflow:auto}
</style>
