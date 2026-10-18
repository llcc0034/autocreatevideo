<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { isDefinitiveCreationRejection, snapshotProductionRunInput } from '@/services/productionOnboarding'
import { useStudioStore } from '@/stores/studio'
import type { ProductionManualOptions, ProductionRun, ProductionRunCreateInput, ScriptRevision, ScriptSource } from '@/services/platformTypes'

const props = defineProps<{ projectId: string; episodes: { id: string; number: number; title: string }[] }>()
const emit = defineEmits<{ created: [run: ProductionRun] }>()
const studio = useStudioStore()
const options = ref<ProductionManualOptions | null>(null), error = ref(''), busy = ref(false), saving = ref(false)
const scopeKind = ref(''), episodeIds = ref<string[]>([]), deliveryKind = ref(''), initialKind = ref('')
const sources = ref<ScriptSource[]>([]), sourceId = ref(''), revisions = ref<ScriptRevision[]>([]), revisionId = ref('')
const idempotencyKey = ref(crypto.randomUUID())
const creationRequest = ref<ProductionRunCreateInput | null>(null), createdRun = ref<ProductionRun | null>(null)
const selectedRevision = computed(() => revisions.value.find(row => row.id === revisionId.value))
let sequence = 0
const ready = computed(() => options.value && !busy.value && !saving.value && !creationRequest.value && !createdRun.value
  && ['project', 'episodes'].includes(scopeKind.value) && (scopeKind.value === 'project' || episodeIds.value.length > 0)
  && deliveryKind.value === 'episode_masters' && (initialKind.value === 'blank' || (initialKind.value === 'script_revision' && selectedRevision.value))
  )

async function load() {
  if (saving.value || creationRequest.value) return
  const token = ++sequence; busy.value = true; error.value = ''; options.value = null
  try {
    const rows = await platformApi.getManualProductionOptions(props.projectId)
    if (token !== sequence) return
    if (rows.projectId !== props.projectId) throw new Error('返回的制作目录不属于当前项目')
    options.value = rows
  } catch (e) { if (token === sequence) error.value = apiErrorSummary(e) }
  finally { if (token === sequence) busy.value = false }
}
async function loadScripts() {
  if (saving.value || creationRequest.value) return
  const token = ++sequence; busy.value = true; error.value = ''; sourceId.value = ''; revisionId.value = ''; revisions.value = []
  try { const rows = await platformApi.listScriptSources(props.projectId); if (token === sequence) sources.value = rows }
  catch (e) { if (token === sequence) error.value = apiErrorSummary(e) }
  finally { if (token === sequence) busy.value = false }
}
async function loadRevisions() {
  if (saving.value || creationRequest.value) return
  const token = ++sequence; revisionId.value = ''; revisions.value = []; error.value = ''
  if (!sourceId.value) return
  busy.value = true
  try {
    const rows = await platformApi.listConfirmedScriptRevisions(sourceId.value)
    if (token === sequence) revisions.value = rows.filter(row => row.status === 'confirmed' && row.confirmedAt)
  } catch (e) { if (token === sequence) error.value = apiErrorSummary(e) }
  finally { if (token === sequence) busy.value = false }
}
async function create() {
  if (busy.value || saving.value || (!creationRequest.value && !createdRun.value && !ready.value)) return
  const token = sequence, expectedProject = props.projectId, session = studio.captureSessionEpoch()
  const isCurrent = () => token === sequence && props.projectId === expectedProject && session === studio.captureSessionEpoch()
  saving.value = true; error.value = ''
  try {
    if (!creationRequest.value && !createdRun.value) {
      const revision = selectedRevision.value
      creationRequest.value = snapshotProductionRunInput({ planSource: 'manual', projectId: expectedProject,
        idempotencyKey: idempotencyKey.value, allowExternalCalls: false,
        configOverrides: {}, manualPlan: {
          stageCatalogKey: String(options.value!.stageCatalog.value.key), expectedStageCatalogSha256: options.value!.stageCatalog.sha256,
          scope: scopeKind.value === 'project' ? { kind: 'project' } : { kind: 'episodes', episodeIds: episodeIds.value },
          delivery: { kind: 'episode_masters' }, initialContent: initialKind.value === 'blank' ? null : {
            kind: 'script_revision', id: revision!.id, expectedSha256: revision!.contentSha256,
          },
        } })
    }
    if (!createdRun.value) {
      let run: ProductionRun
      try { run = await platformApi.createProductionRun(creationRequest.value!) }
      catch (cause) {
        if (isCurrent() && isDefinitiveCreationRejection(cause)) { creationRequest.value = null; idempotencyKey.value = crypto.randomUUID() }
        throw cause
      }
      if (!isCurrent()) return
      if (!run.id || run.project !== expectedProject || run.planSource !== 'manual') throw new Error('创建结果与本次项目或制作方式不一致，请查询原项目运行记录')
      createdRun.value = run
    }
    emit('created', createdRun.value)
  } catch (e) { if (isCurrent()) error.value = apiErrorSummary(e) }
  finally { if (isCurrent()) saving.value = false }
}
watch(() => props.projectId, () => {
  scopeKind.value = ''; deliveryKind.value = ''; initialKind.value = ''; episodeIds.value = []
  sources.value = []; revisions.value = []; sourceId.value = ''; revisionId.value = ''
  saving.value = false; creationRequest.value = null; createdRun.value = null
  idempotencyKey.value = crypto.randomUUID(); void load()
}, { immediate: true })
onBeforeUnmount(() => { sequence++ })
</script>

<template>
  <section class="platform-card manual-create">
    <h3>无配方人工制作</h3>
    <p>先建立本轮制作记录，再逐步选择执行实例、填写输入或采用已有内容。创建不会调用模型，也不会自动批准剧本。</p>
    <p v-if="busy" role="status">正在读取当前项目的制作选项…</p>
    <div v-if="error" class="platform-alert danger" role="alert">{{ error }}<button v-if="!creationRequest" type="button" class="button ghost small" :disabled="busy || saving" @click="load">重新读取制作目录</button></div>
    <section v-if="creationRequest && !saving" class="platform-alert warning" role="status">
      <p v-if="createdRun">已创建记录 {{ createdRun.id }}。再次打开只跳转到该记录，不会重复创建。</p>
      <p v-else>创建回执尚未核实，原输入和请求标识已保留。恢复会重新提交同一请求：已创建则返回原记录，未创建则建立记录；这不是只读查询。人工制作的创建不会调用模型。</p>
      <button type="button" class="button ghost small" :disabled="busy || saving" @click="create">{{ createdRun ? '打开已创建的制作记录（不再创建）' : '按原请求恢复创建' }}</button>
      <RouterLink :to="`/projects/${projectId}/production?view=list`">查看原项目制作记录</RouterLink>
    </section>
    <fieldset :disabled="busy || saving || !options || Boolean(creationRequest)">
      <div class="platform-form-grid">
        <label class="platform-field"><span>本轮制作范围</span><select v-model="scopeKind"><option value="">请选择</option><option v-if="options?.scopeKinds.includes('project')" value="project">整个项目（也可从空白项目开始）</option><option v-if="options?.scopeKinds.includes('episodes')" value="episodes" :disabled="!episodes.length">明确选择已有剧集</option></select></label>
        <label class="platform-field"><span>交付内容</span><select v-model="deliveryKind"><option value="">请选择</option><option v-if="options?.deliveryKinds.includes('episode_masters')" value="episode_masters">逐集成片</option></select></label>
      </div>
      <fieldset v-if="scopeKind === 'episodes'"><legend>本轮剧集（至少一集）</legend><label v-for="episode in episodes" :key="episode.id" class="episode-option"><input v-model="episodeIds" type="checkbox" :value="episode.id">第 {{ episode.number }} 集 · {{ episode.title }}</label></fieldset>
      <label class="platform-field"><span>开始时的内容</span><select v-model="initialKind" @change="initialKind === 'script_revision' && loadScripts()"><option value="">请选择</option><option value="blank">从空白开始，稍后逐步添加</option><option v-if="options?.initialContentKinds.includes('script_revision')" value="script_revision">参考一个已确认的剧本修订</option></select></label>
      <template v-if="initialKind === 'script_revision'">
        <label class="platform-field"><span>剧本来源</span><select v-model="sourceId" @change="loadRevisions"><option value="">请选择</option><option v-for="source in sources" :key="source.id" :value="source.id">{{ source.originalName || source.sourceFormat }} · {{ source.id.slice(0,8) }}</option></select></label>
        <label class="platform-field"><span>已确认修订</span><select v-model="revisionId"><option value="">请选择精确修订</option><option v-for="revision in revisions" :key="revision.id" :value="revision.id">修订 {{ revision.revision }} · {{ revision.confirmedAt }}</option></select><small>这里只保存初始引用；进入制作后仍需明确“采用”才能用于后续步骤。</small></label>
        <p v-if="!busy && !error && (!sources.length || (sourceId && !revisions.length))">尚无可选的已确认剧本，请先在项目剧本管理中确认，或从空白开始。</p>
      </template>
      <p>此时不授权外部调用、不要求预算；准备生成时在制作台确认。</p>
      <button type="button" class="button primary" :disabled="!ready" @click="create">{{ saving ? '正在创建制作记录…' : '开始人工制作' }}</button>
    </fieldset>
    <small>请求标识 {{ idempotencyKey }} · 回执未知时保留原请求；已明确拒绝后可调整输入并使用新标识。</small>
  </section>
</template>

<style scoped>
.manual-create{padding:20px;display:grid;gap:14px}.manual-create h3,.manual-create p{margin:0}.manual-create fieldset{border:0;padding:0;margin:0;display:grid;gap:14px;min-width:0}.manual-create small,.manual-create p{color:var(--text-secondary);font-size:13px}.manual-create select{width:100%}.manual-create .episode-option{display:flex;align-items:center;gap:8px}.manual-create .platform-alert{display:flex;gap:12px;flex-wrap:wrap}
</style>
