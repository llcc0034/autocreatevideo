<script setup lang="ts">
import { RouterLink, onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useAuthStore } from '@/stores/auth'
import BaseModal from './BaseModal.vue'
import DataStructureVersionSelect from './DataStructureVersionSelect.vue'
import ConfigurationAuthoringShell from './ConfigurationAuthoringShell.vue'
import ConfigurationDiscardNotice from './ConfigurationDiscardNotice.vue'
import RegistryResourceExplorer from './RegistryResourceExplorer.vue'
import ContentSourceDefinitionView from './ContentSourceDefinitionView.vue'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { authoringIssues, authoringResponseStatus, isExplicitAuthoringRejection, type AuthoringIssue } from '@/services/configurationAuthoring'
import { contentSources, type ContentSourceVersion, type ContentSourceSummary, type SourceReaderOption } from '@/services/contentSources'

const props = defineProps<{ choose?: boolean; initialId?: string }>()
const emit = defineEmits<{ close: []; select: [value: ContentSourceVersion] }>()
const auth = useAuthStore()
const account = computed(() => String(auth.user?.id || ''))
const rows = ref<ContentSourceSummary[]>([]), readers = ref<SourceReaderOption[]>([])
const count = ref(0), page = ref(1), status = ref(props.choose ? 'published' : ''), search = ref(''), hasNext = ref(false)
const busy = ref(false), phase = ref(''), error = ref(''), notice = ref(''), editing = ref(false), confirmed = ref(false)
const selected = ref<ContentSourceVersion | null>(null), predecessor = ref<ContentSourceVersion | null>(null)
const form = reactive({ name: '', description: '', reader_selector: '', data_structure_version: '' })
const issues = ref<AuthoringIssue[]>([])
type WriteKind = 'save' | 'publish' | 'successor' | 'retire'
const unknown = ref<{ kind: WriteKind; source: ContentSourceVersion | null; name: string } | null>(null)
const recovery = ref<ContentSourceSummary[]>([]), recoveryPage = ref(1), recoveryCount = ref(0), recoveryNext = ref(false), recoveryChecked = ref(false)
const leavePrompt = ref('')
const reader = computed(() => readers.value.find(item => item.value === form.reader_selector))
const savedReader = computed(() => selected.value?.reader)
const choosingReader = computed(() => !selected.value || selected.value.reader_selector !== form.reader_selector)
const editable = computed(() => !props.choose && (!selected.value || selected.value.status === 'draft'))
const dirty = computed(() => editing.value && editable.value && Object.entries(form).some(([key,value]) => value !== (selected.value?.[key as keyof ContentSourceVersion] ?? '')))
const canSave = computed(() => editable.value && !busy.value && !unknown.value && Boolean(form.name.trim() && form.reader_selector && form.data_structure_version && (!choosingReader.value || reader.value)))
const statusName = (value: string) => ({ draft: '草稿', published: '已发布', retired: '已退役' })[value] || value
const sections = [
  { key: 'identity', title: '用途与身份', description: '为可复用的读取定义命名。不要在这里填写项目正文。' },
  { key: 'reader', title: '实际读取位置', description: '选择平台已有的读取能力；权限、作用范围和确认方式由该能力确定。' },
  { key: 'structure', title: '输出结构', description: '引用一个精确的公共结构版本，不重复定义字段或另写 Schema。' },
  { key: 'review', title: '复核与使用', description: '先保存草稿，再发布给策略选用。不会自动替换已有引用。' },
]
let serial = 0, disposed = false
let leaveAction: (() => void) | null = null, routeCancel: (() => void) | null = null
function current(request: number, user: string) { return !disposed && request === serial && user === account.value }
function exact(value: ContentSourceVersion, id?: string) {
  if (!value?.id || (id && value.id !== id) || !['draft','published','retired'].includes(value.status) || !value.source_key || !Number.isInteger(value.revision)) throw new Error('来源详情身份或版本信息不完整，未载入其他版本。')
  return value
}
function install(source: ContentSourceVersion | null) {
  selected.value = source; editing.value = true; confirmed.value = false; issues.value = []
  unknown.value = null; recovery.value = []; recoveryChecked.value = false
  Object.assign(form, { name: source?.name || '', description: source?.description || '', reader_selector: source?.reader_selector || '', data_structure_version: source?.data_structure_version || '' })
}
function keepEditing() { routeCancel?.(); routeCancel = null; leaveAction = null; leavePrompt.value = '' }
function discardAndLeave() { const action = leaveAction; routeCancel = null; leaveAction = null; leavePrompt.value = ''; install(selected.value); action?.() }
function requestLeave(action: () => void) {
  if (busy.value || leavePrompt.value) return false
  if (dirty.value || unknown.value) {
    leavePrompt.value = unknown.value ? '上次写入结果尚未核实。关闭只会丢弃本地核对上下文，不会撤销服务端操作；建议继续编辑并先核对现存版本。' : '当前来源有未保存修改；放弃后切换或关闭会丢失这些输入。'
    leaveAction = action; return false
  }
  action(); return true
}
function mayLeave() {
  if (busy.value || leavePrompt.value) return false
  if (!dirty.value && !unknown.value) return true
  return new Promise<boolean>(resolve => { requestLeave(() => resolve(true)); routeCancel = () => resolve(false) })
}
function beforeUnload(event: BeforeUnloadEvent) { if (dirty.value || unknown.value || busy.value) { event.preventDefault(); event.returnValue = '' } }
function close() { requestLeave(() => emit('close')) }
onBeforeRouteLeave(mayLeave); onBeforeRouteUpdate(mayLeave)
onBeforeUnmount(() => { disposed = true; serial++; keepEditing(); window.removeEventListener('beforeunload', beforeUnload) })
watch(account, () => {
  serial++; busy.value = false; keepEditing(); install(null); editing.value = false; predecessor.value = null
  rows.value = []; readers.value = []; count.value = 0; hasNext.value = false; notice.value = ''; error.value = '账号已变化，请重新查询；旧账号的迟到结果不会覆盖当前页面。'
}, { flush: 'sync' })
async function load() {
  if (busy.value || leavePrompt.value) return
  const request = ++serial, user = account.value
  busy.value = true; phase.value = '正在读取来源目录与可选读取位置…'; error.value = ''
  try {
    const [result, options] = await Promise.allSettled([contentSources.list(page.value, status.value, search.value), props.choose ? Promise.resolve([]) : contentSources.readers()])
    if (!current(request,user)) return
    const failures: string[] = []
    if (result.status === 'fulfilled') {
      rows.value = result.value.results; count.value = result.value.count; hasNext.value = result.value.next !== undefined ? Boolean(result.value.next) : page.value * 20 < result.value.count
    } else failures.push(apiErrorSummary(result.reason,'来源目录读取失败') + '；列表可能已过期。')
    if (options.status === 'fulfilled') readers.value = options.value
    else { readers.value = []; failures.push(apiErrorSummary(options.reason,'可选读取位置加载失败') + '；已有版本仍可按保存的规则查看。') }
    error.value = failures.join(' ')
  } catch (cause) { if (current(request,user)) { readers.value = []; error.value = apiErrorSummary(cause, '来源读取失败') + '；已填内容保留，列表可能已过期。' } }
  finally { if (current(request,user)) { busy.value = false; phase.value = '' } }
}
function edit(source: ContentSourceVersion | null) { requestLeave(() => { install(source); predecessor.value = null; error.value = ''; notice.value = '' }) }
async function readSource(id: string, choose: boolean) {
  const request = ++serial, user = account.value
  busy.value = true; phase.value = '正在读取所选精确版本…'; error.value = ''
  try {
    const detail = exact(await contentSources.get(id), id)
    if (!current(request,user)) return
    if (choose && detail.status !== 'published') throw new Error('此来源已不再处于已发布状态，请重新查询后选择')
    if (choose) emit('select', detail)
    else { install(detail); predecessor.value = null; notice.value = '' }
  } catch (cause) { if (current(request,user)) error.value = apiErrorSummary(cause, '来源详情读取失败；保留当前内容') }
  finally { if (current(request,user)) { busy.value = false; phase.value = '' } }
}
async function openSource(source: Pick<ContentSourceSummary,'id'>, choose = false) {
  if (busy.value || unknown.value) return
  let work: Promise<void> | undefined
  requestLeave(() => { work = readSource(source.id, choose) })
  await work
}
async function write(kind: WriteKind) {
  if (props.choose || busy.value || leavePrompt.value || unknown.value || (kind === 'save' && !canSave.value)) return
  const source = selected.value
  if (kind !== 'save' && (!source || dirty.value || (kind !== 'successor' && !confirmed.value))) return
  if ((kind === 'successor' && source?.status === 'draft') || (kind === 'publish' && source?.status !== 'draft') || (kind === 'retire' && source?.status !== 'published')) return
  const payload = { ...form }, request = ++serial, user = account.value
  busy.value = true; phase.value = kind === 'save' ? '正在保存草稿，请勿重复提交…' : '正在提交版本操作，请勿重复提交…'; error.value = ''; notice.value = ''; issues.value = []
  try {
    const value = exact(kind === 'save' ? await contentSources.save(payload,source) : await contentSources.action(source!,kind), kind === 'successor' ? undefined : source?.id)
    if (!current(request,user)) return
    if (source && (value.source_key !== source.source_key || (kind === 'successor' && (value.id === source.id || value.version !== source.version + 1 || value.status !== 'draft')))) throw new Error('写入回执与原来源版本不一致，需核对现存版本。')
    if (value.status !== ({save:'draft',publish:'published',successor:'draft',retire:'retired'} as const)[kind] || (source && kind !== 'successor' && value.version !== source.version)) throw new Error('写入回执的状态或版本与本次操作不一致，需核对现存版本。')
    install(value); predecessor.value = kind === 'successor' ? source : predecessor.value
    notice.value = kind === 'save' ? '草稿已保存，尚未发布；未调用模型。列表可查询刷新。' : kind === 'successor' ? '基于所选原版本创建了下一版本草稿；原版本与已有引用未改变。' : '来源状态已更新；不改写已有策略、任务或审核记录。列表可查询刷新。'
  } catch (cause) {
    if (!current(request,user)) return
    issues.value = authoringIssues(cause, { name:'identity', description:'identity', reader_selector:'reader', data_structure_version:'structure', expected_revision:'review' })
    error.value = apiErrorSummary(cause, '来源操作失败；已填内容保留')
    if (!isExplicitAuthoringRejection(cause) || authoringResponseStatus(cause) === 409) {
      error.value = '写入结果尚未确认；已保留本次输入，请先只读核对现存版本，不要重复提交。'
      unknown.value = { kind, source, name:payload.name }; recoveryPage.value = 1; recovery.value = []; recoveryChecked.value = false
    }
  } finally { if (current(request,user)) { busy.value = false; phase.value = '' } }
}
async function save() { await write('save') }
async function act(action: 'publish' | 'successor' | 'retire') { await write(action) }
async function readback() {
  const pending = unknown.value
  if (!pending || busy.value) return
  const request = ++serial, user = account.value
  busy.value = true; phase.value = '只读核对现存版本，不重发写入…'; error.value = ''; recoveryChecked.value = false
  try {
    const result = pending.source && pending.kind !== 'successor'
      ? { count:1, next:null, results:[exact(await contentSources.get(pending.source.id), pending.source.id)] }
      : await contentSources.list(recoveryPage.value, '', pending.source ? '' : pending.name, pending.source?.source_key)
    if (!current(request,user)) return
    if (pending.source && result.results.some(item => item.source_key !== pending.source!.source_key)) throw new Error('核对列表包含其他来源族，未采用。')
    recovery.value = result.results; recoveryCount.value = result.count; recoveryNext.value = result.next !== undefined ? Boolean(result.next) : recoveryPage.value * 20 < result.count; recoveryChecked.value = true
  } catch (cause) { if (current(request,user)) { recovery.value = []; error.value = apiErrorSummary(cause, '现存版本读取失败；结果仍未知') } }
  finally { if (current(request,user)) { busy.value = false; phase.value = '' } }
}
async function adoptRecovery(id: string) {
  const pending = unknown.value
  if (!pending || busy.value || !recovery.value.some(row => row.id === id)) return
  const request = ++serial, user = account.value
  busy.value = true; phase.value = '正在重新读取并载入所选精确版本…'; error.value = ''
  try {
    const value = exact(await contentSources.get(id),id)
    if (!current(request,user)) return
    if (pending.source && (value.source_key !== pending.source.source_key || (pending.kind !== 'successor' && value.id !== pending.source.id))) throw new Error('所选版本不属于本次核对范围。')
    if (pending.kind === 'successor' && pending.source && (value.id === pending.source.id || value.version !== pending.source.version + 1)) throw new Error('此记录不是原版本的下一版，不能解除后继请求的未知状态。')
    install(value); predecessor.value = pending.kind === 'successor' ? pending.source : null
    notice.value = '已明确载入所选现存版本。此操作未重发写入，也不认定该版本一定由上次请求创建。'
  } catch (cause) { if (current(request,user)) error.value = apiErrorSummary(cause, '版本载入失败；结果仍未知') }
  finally { if (current(request,user)) { busy.value = false; phase.value = '' } }
}
onMounted(async () => { window.addEventListener('beforeunload',beforeUnload); await load(); if (props.initialId && !disposed) await openSource({id:props.initialId}) })
watch(() => props.initialId, id => { if (id) void openSource({id}) })
</script>

<template>
  <BaseModal open configuration title="内容来源版本" width="1080px" :dismissible="!busy" @close="close">
    <div class="source-manager" :aria-busy="busy">
      <ConfigurationDiscardNotice v-if="leavePrompt" :message="leavePrompt" :busy="busy" @keep="keepEditing" @discard="discardAndLeave" />
      <RegistryResourceExplorer :key="account" label="内容来源" :inert="Boolean(leavePrompt)" v-slot="{ inspect }">
        <p>内容来源定义“从哪里读、按什么结构交给策略”。具体文字属于本次项目或任务；此处不读取业务正文，也不调用模型。</p>
        <p v-if="error" role="alert" class="error">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
        <p v-if="busy" role="status">{{ phase }}</p>
        <form class="toolbar" @submit.prevent="page=1; load()">
          <label>搜索名称<input v-model="search" :disabled="busy" /></label>
          <label>版本状态<select v-model="status" :disabled="busy"><option value="">全部状态</option><option value="published">已发布</option><option value="draft">草稿</option><option value="retired">已退役</option></select></label>
          <button :disabled="busy">查询／刷新</button><button v-if="!choose" type="button" :disabled="busy || Boolean(unknown)" @click="edit(null)">新建来源定义</button><RouterLink v-else to="/settings?section=fields" target="_blank" rel="noopener">前往字段维护管理来源（新窗口）</RouterLink>
        </form>
        <p v-if="!busy && !rows.length && !error">当前条件没有来源版本。可从已支持的读取位置新建。</p>
        <ul class="sources"><li v-for="item in rows" :key="item.id">
          <div><strong>{{ item.name }} · v{{ item.version }}</strong><small>{{ statusName(item.status) }} · {{ item.reader_selector }}</small></div>
          <button :disabled="busy || Boolean(unknown)" @click="openSource(item)">{{ !choose && item.status==='draft' ? '查看／编辑草稿' : '查看版本' }}</button>
          <button v-if="choose && item.status==='published'" :disabled="busy" @click="openSource(item,true)">使用此精确版本</button>
        </li></ul>
        <div class="toolbar"><button :disabled="busy || page===1" @click="page--; load()">上一页</button><span>第 {{ page }} 页 · 共 {{ count }} 项</span><button :disabled="busy || !hasNext" @click="page++; load()">下一页</button></div>
        <section v-if="unknown" class="recovery" aria-label="写入结果核对">
          <h3>先核对写入结果</h3><p>未收到可靠回执，不能认定失败或自动重试。下面仅查询现存记录，名称相同不代表是上次请求的结果。</p>
          <p v-if="unknown.source">原版本：{{ unknown.source.name }} · v{{ unknown.source.version }} · {{ unknown.source.id }}；原修订 {{ unknown.source.revision }}</p>
          <button :disabled="busy" @click="readback">只读核对现存版本</button>
          <p v-if="recoveryChecked && !recovery.length">本页没有匹配记录，结果仍未知；不自动重新创建。</p>
          <ul class="sources"><li v-for="item in recovery" :key="item.id"><div><strong>{{ item.name }} · v{{ item.version }} · {{ statusName(item.status) }}</strong><small>{{ item.id }} · 修订 {{ item.revision }}</small></div><button :disabled="busy" @click="adoptRecovery(item.id)">载入此精确版本</button></li></ul>
          <div v-if="recoveryChecked" class="toolbar"><button :disabled="busy || recoveryPage===1" @click="recoveryPage--; readback()">核对上一页</button><span>第 {{ recoveryPage }} 页 · 共 {{ recoveryCount }} 项</span><button :disabled="busy || !recoveryNext" @click="recoveryPage++; readback()">核对下一页</button></div>
        </section>
        <section v-if="editing" class="editor">
          <header><h2>{{ selected ? `${selected.name} · v${selected.version}` : '新建来源草稿' }}</h2><p v-if="selected">{{ statusName(selected.status) }} · 修订 {{ selected.revision }}</p><p v-if="predecessor">基于明确选择的 {{ predecessor.name }} · v{{ predecessor.version }}；不会改用同名或最新版本。</p><button v-if="selected" :disabled="busy" @click="inspect('source',selected.id,$event)">查看定义与使用关系</button></header>
          <ContentSourceDefinitionView v-if="selected && !editable" :source="selected" @inspect-structure="(id,event) => inspect('structure',id,event)" />
          <form v-else @submit.prevent="save">
            <ConfigurationAuthoringShell label="内容来源配置" :status="selected ? `${statusName(selected.status)} · 修订 ${selected.revision}${dirty ? ' · 有未保存修改' : ''}` : '未保存的新定义'" :sections="sections" :busy="busy" :issues="issues">
              <template #identity><fieldset :disabled="busy || Boolean(unknown)"><label data-authoring-field="name">业务名称<input v-model="form.name" maxlength="120" required placeholder="例如：连续剧创作梗概" /></label><label data-authoring-field="description">使用说明<textarea v-model="form.description" maxlength="1000" rows="2" /></label></fieldset></template>
              <template #reader><fieldset :disabled="busy || Boolean(unknown)"><label data-authoring-field="reader_selector">实际读取位置<select v-model="form.reader_selector" required><option value="">请选择已支持的来源</option><option v-if="form.reader_selector && !reader" :value="form.reader_selector" disabled>当前版本：{{ form.reader_selector }}（目录未返回）</option><option v-for="option in readers" :key="option.value" :value="option.value">{{ option.group_label }} · {{ option.label }} · {{ option.value }}</option></select></label></fieldset>
                <div v-if="reader" class="explanation"><strong>{{ choosingReader ? '待保存选择' : '当前目录导航帮助（不覆盖已保存规则）' }} · {{ reader.record_location }}</strong><p>{{ reader.description }}</p><p>{{ reader.execution_usage }}</p><small>{{ reader.group_label }} · {{ reader.reader.value_kind }} / {{ reader.reader.scope_kind }}</small><p v-if="reader.reader.protocol==='request_text/v1'">本次填写文字，保存准备时确认并冻结；不是自动查询项目或其他任务的输出。</p></div>
                <p v-if="choosingReader && !reader">当前读取目录不可用或尚未选择，不能保存新的读取位置。</p>
              </template>
              <template #structure><div data-authoring-field="data_structure_version"><DataStructureVersionSelect v-model="form.data_structure_version" :disabled="busy || Boolean(unknown)" /></div><small>当前读取协议输出文字，请选择文字根节点结构。结构约束在实际读取后校验；定义与具体值分开。</small><button v-if="form.data_structure_version" type="button" :disabled="busy" @click="inspect('structure',form.data_structure_version,$event)">查看精确输出结构</button></template>
              <template #review><p>项目／本次任务中的内容 → 读取规则 → 精确输出结构 → 引用此来源的提示词策略。需要生成或翻译时由使用方配置辅助能力，来源本身不是 AI 调用。</p><ContentSourceDefinitionView v-if="selected" :source="selected" @inspect-structure="(id,event) => inspect('structure',id,event)" /><p v-else>草稿保存后可以核对已保存的读取合同，再决定发布。</p></template>
              <template #actions><button type="submit" class="primary" :disabled="!canSave">保存草稿（不调用模型）</button></template>
            </ConfigurationAuthoringShell>
          </form>
          <template v-if="selected && !choose">
            <p v-if="selected.status==='draft'">先保存修改，再发布已保存的内容。发布后只能新建下一版本；策略不会自动切换。</p>
            <label v-if="selected.status!=='retired'" class="confirm"><input v-model="confirmed" type="checkbox" :disabled="busy || Boolean(unknown) || dirty" />{{ selected.status==='draft' ? '我已保存并核对来源位置、范围与确认要求' : '确认退役：禁止新规则选用，保留既有精确引用与历史事实' }}</label>
            <div class="toolbar"><button v-if="selected.status==='draft'" :disabled="busy || Boolean(unknown) || !confirmed || dirty" @click="act('publish')">发布此版本</button><button v-else :disabled="busy || Boolean(unknown)" @click="act('successor')">基于此版本新建下一版</button><button v-if="selected.status==='published'" :disabled="busy || Boolean(unknown) || !confirmed" @click="act('retire')">退役此版本</button></div>
          </template>
        </section>
      </RegistryResourceExplorer>
    </div>
  </BaseModal>
</template>

<style scoped>
.source-manager{padding:var(--config-space-xl);display:grid;gap:var(--config-space-lg);min-width:0;font:400 var(--config-body-size)/1.6 var(--font)}.source-manager :deep(.resource-owner){display:grid;gap:var(--config-space-lg)}.source-manager p{line-height:1.6;margin:0;color:var(--text-secondary);overflow-wrap:anywhere}.source-manager h2{font-size:var(--config-title-size);margin:0}.source-manager h3{font-size:var(--config-section-size);margin:0}.toolbar{display:flex;gap:var(--config-space-sm);align-items:end;flex-wrap:wrap;margin:var(--config-space-md) 0}.sources{list-style:none;padding:0;margin:0}.sources li{padding:var(--config-space-md) 0;border-bottom:1px solid var(--border);display:flex;gap:var(--config-space-sm);align-items:center;flex-wrap:wrap}.sources li div{flex:1;min-width:min(160px,100%);overflow-wrap:anywhere}.source-manager small{display:block;font-size:var(--config-help-size);color:var(--text-secondary);overflow-wrap:anywhere}.source-manager label{display:grid;gap:var(--config-space-xs)}.source-manager input,.source-manager select,.source-manager textarea{font:inherit;background:var(--surface);color:var(--text);border:1px solid var(--border-strong);border-radius:var(--config-radius);padding:var(--config-space-sm);width:100%;min-width:0;min-height:44px;box-sizing:border-box}.source-manager button{font:inherit;min-height:44px;border:1px solid var(--border-strong);background:var(--surface);border-radius:var(--config-radius);padding:var(--config-space-sm) var(--config-space-md);color:var(--text);cursor:pointer;overflow-wrap:anywhere}.source-manager button.primary{background:var(--accent);color:var(--on-accent,#101010)}.source-manager button:disabled{opacity:.45;cursor:not-allowed}.editor,.recovery{border:1px solid var(--border-strong);padding:var(--config-space-lg);border-radius:var(--config-radius);display:grid;gap:var(--config-space-lg);margin-top:var(--config-space-lg);min-width:0}.editor header{display:grid;gap:var(--config-space-sm)}.editor form,.editor fieldset{display:grid;gap:var(--config-space-md);min-width:0}.editor fieldset{border:0;padding:0;margin:0}.explanation{padding:var(--config-space-md);background:var(--surface);border-radius:var(--config-radius)}.source-manager .confirm{display:flex;align-items:center;gap:var(--config-space-sm)}.confirm input{width:20px;min-height:20px;flex-shrink:0}.source-manager .error{color:var(--red)}button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid var(--accent);outline-offset:2px}@media(max-width:600px){.source-manager{padding:var(--config-space-md)}.toolbar label{width:100%}.sources li button{flex:1}.editor,.recovery{padding:var(--config-space-md)}}
</style>
