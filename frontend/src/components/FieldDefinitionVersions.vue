<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { authoringIssues, isExplicitAuthoringRejection, type AuthoringIssue } from '@/services/configurationAuthoring'
import { fieldDefinitionVersions, type FieldDefinitionContent, type FieldDefinitionVersion, type FieldDefinitionVersionSummary } from '@/services/fieldDefinitionVersions'
import ConfigurationAuthoringShell from './ConfigurationAuthoringShell.vue'
import ConfigurationDiscardNotice from './ConfigurationDiscardNotice.vue'
import RegistryResourceExplorer from './RegistryResourceExplorer.vue'
import FieldDefinitionView from './FieldDefinitionView.vue'

const props = defineProps<{ fieldDefinition: string; initialId?: string }>()
const emit = defineEmits<{ changed: []; busy: [boolean]; close: [] }>()
const auth = useAuthStore(), account = computed(() => String(auth.user?.id || ''))
const rows = ref<FieldDefinitionVersionSummary[]>([])
const options = ref<Awaited<ReturnType<typeof fieldDefinitionVersions.options>> | null>(null)
const selected = ref<FieldDefinitionVersion | null>(null), predecessor = ref<FieldDefinitionVersion | null>(null)
const sourceVersion = computed(() => predecessor.value?.id || '')
const editing = ref(false), loading = ref(false), saving = ref(false)
const error = ref(''), notice = ref(''), phase = ref(''), enumText = ref(''), leavePrompt = ref('')
const directoryError = ref(''), directoryComplete = ref(false)
const issues = ref<AuthoringIssue[]>([])
const blank = (): FieldDefinitionContent => ({ label: '', category: '', value_type: '', unit: '', description: '', impact: '', value_options: [] })
const form = reactive(blank())
const readonly = computed(() => Boolean(selected.value && selected.value.status !== 'draft'))
const busy = computed(() => loading.value || saving.value)
const statusLabel = (status: string) => options.value?.statuses.find(item => item.value === status)?.label || status
const payload = (): FieldDefinitionContent => ({ ...form, value_options: form.value_type === 'enum' ? enumText.value.split('\n').map(value => value.trim()).filter(Boolean) : [] })
const content = (row: FieldDefinitionVersion | null): FieldDefinitionContent => row ? { label:row.label, category:row.category, value_type:row.value_type, unit:row.unit, description:row.description, impact:row.impact, value_options:[...row.value_options] } : blank()
const dirtyPayload = () => Object.fromEntries(Object.entries(payload()).filter(([key,value]) => JSON.stringify(value) !== JSON.stringify(content(selected.value)[key as keyof FieldDefinitionContent]))) as Partial<FieldDefinitionContent>
const dirty = computed(() => editing.value && !readonly.value && Object.keys(dirtyPayload()).length > 0)
type Pending = { kind: 'create' | 'update' | 'publish' | 'retire'; id?: string; source?: string; knownIds: string[]; maximum: number; field: string; attempted: FieldDefinitionContent }
const unknown = ref<Pending | null>(null), recovery = ref<FieldDefinitionVersionSummary[]>([]), recoveryChecked = ref(false)
const sections = [
  { key:'meaning', title:'语义与用途', description:'定义可复用字段的含义，不在此填写项目或镜头的具体值。' },
  { key:'type', title:'类型与单位', description:'类型来自平台目录。这里声明值的形态；使用处负责必填、默认值及更具体约束。' },
  { key:'review', title:'复核与使用', description:'先保存草稿，启用后供结构与策略按精确版本引用，不自动升级已有使用方。' },
]
let serial = 0, disposed = false, leaveAction: (() => void) | null = null, routeCancel: (() => void) | null = null
const current = (request: number, user: string) => !disposed && request === serial && user === account.value
function exact(row: FieldDefinitionVersion, id?: string) {
  if (!row?.id || (id && row.id !== id) || row.field_definition !== props.fieldDefinition || !Number.isInteger(row.version) || !['draft','published','retired'].includes(row.status) || !Array.isArray(row.value_options)) throw new Error('返回版本身份、所属字段或合同不一致，未载入其他定义。')
  return row
}
function assign(row: FieldDefinitionVersion | null) { Object.assign(form,content(row)); enumText.value = form.value_options.join('\n') }
function install(row: FieldDefinitionVersion | null) { selected.value=row; assign(row); editing.value=true; unknown.value=null; recovery.value=[]; recoveryChecked.value=false; issues.value=[] }
function keepEditing() { routeCancel?.(); routeCancel=null; leaveAction=null; leavePrompt.value='' }
function discardAndLeave() { const action=leaveAction; routeCancel=null; leaveAction=null; leavePrompt.value=''; assign(selected.value); unknown.value=null; action?.() }
function requestLeave(action: () => void) {
  if (busy.value || leavePrompt.value) return false
  if (dirty.value || unknown.value) { leavePrompt.value=unknown.value ? '写入结果尚未核实。离开只会丢失本窗口核对上下文，不会撤销服务端操作；请优先核对现存版本。' : '当前字段定义有未保存修改，放弃后将丢失本地输入。'; leaveAction=action; return false }
  action(); return true
}
function mayLeave() {
  if (busy.value || leavePrompt.value) return false
  if (!dirty.value && !unknown.value) return true
  return new Promise<boolean>(resolve => { requestLeave(() => resolve(true)); routeCancel=() => resolve(false) })
}
function requestClose() { requestLeave(() => emit('close')) }
defineExpose({ requestClose })
function beforeUnload(event: BeforeUnloadEvent) { if (dirty.value || unknown.value || busy.value) { event.preventDefault(); event.returnValue='' } }
onBeforeRouteLeave(mayLeave); onBeforeRouteUpdate(mayLeave)
onMounted(() => window.addEventListener('beforeunload',beforeUnload))
onBeforeUnmount(() => { disposed=true; serial++; keepEditing(); window.removeEventListener('beforeunload',beforeUnload); emit('busy',false) })
watch(busy,value => emit('busy',value),{flush:'sync'})
watch(account,() => { serial++; loading.value=false; saving.value=false; keepEditing(); install(null); editing.value=false; predecessor.value=null; rows.value=[]; options.value=null; directoryComplete.value=false;directoryError.value='';notice.value=''; error.value='账号已变化，请重新读取；旧账号的迟到结果不会覆盖当前页面。' },{flush:'sync'})

async function loadDirectory() {
  const request=++serial,user=account.value
  loading.value=true; phase.value='正在读取字段版本和可选类型…'; directoryError.value='';directoryComplete.value=false
  try {
    const [catalog,versions]=await Promise.allSettled([fieldDefinitionVersions.options(),fieldDefinitionVersions.list(props.fieldDefinition)])
    if (!current(request,user)) return
    const failures:string[]=[]
    if (catalog.status==='fulfilled') options.value=catalog.value
    else { options.value=null; failures.push(apiErrorSummary(catalog.reason,'类型目录不可用，新建和保存暂停')) }
    if (versions.status==='fulfilled') {
      if (versions.value.some(row => row.field_definition!==props.fieldDefinition || !Number.isInteger(row.version))) throw new Error('目录包含其他字段或不完整版本，未采用。')
      rows.value=versions.value;directoryComplete.value=true
    } else failures.push(apiErrorSummary(versions.reason,'版本目录未刷新，原列表可能已过期'))
    directoryError.value=failures.join('；')
  } catch(cause) { if(current(request,user)) directoryError.value=apiErrorSummary(cause,'版本目录读取失败') }
  finally { if(current(request,user)) { loading.value=false;phase.value='' } }
}
async function reload() { if (!busy.value) await loadDirectory() }
async function readVersion(id:string) {
  const request=++serial,user=account.value
  loading.value=true;phase.value='正在读取所选精确版本…';error.value=''
  try {
    const row=exact(await fieldDefinitionVersions.get(id),id)
    if(!current(request,user)) return
    install(row); predecessor.value=null; notice.value=''
    if(!rows.value.some(item=>item.id===id))rows.value=[row,...rows.value]
  } catch(cause){if(current(request,user))error.value=apiErrorSummary(cause,'版本读取失败，当前输入保留')}
  finally{if(current(request,user)){loading.value=false;phase.value=''}}
}
async function open(id:string) {
  if(!id || busy.value || unknown.value)return
  let work:Promise<void>|undefined
  requestLeave(()=>{work=readVersion(id)});await work
}
function begin(clone=false) {
  if(unknown.value || !options.value || !directoryComplete.value)return
  requestLeave(()=>{const source=clone?selected.value:null;install(null);predecessor.value=source;assign(source);error.value='';notice.value=''})
}
function apply(row:FieldDefinitionVersion) { install(row);rows.value=[row,...rows.value.filter(item=>item.id!==row.id)].sort((a,b)=>b.version-a.version);emit('changed') }
function pending(kind:Pending['kind']):Pending {return{kind,id:selected.value?.id,field:props.fieldDefinition,source:predecessor.value?.id,knownIds:rows.value.map(row=>row.id),maximum:Math.max(0,...rows.value.map(row=>row.version)),attempted:payload()}}
async function perform(operation:Pending, writer:()=>Promise<FieldDefinitionVersion>) {
  const request=++serial,user=account.value
  saving.value=true;phase.value=operation.kind==='create'||operation.kind==='update'?'正在保存字段草稿…':'正在更新版本状态…';error.value='';notice.value='';issues.value=[]
  try {
    const row=exact(await writer(),operation.id)
    if(!current(request,user))return false
    const targetStatus=operation.kind==='publish'?'published':operation.kind==='retire'?'retired':'draft'
    if(row.status!==targetStatus || (operation.kind==='create'&&(operation.knownIds.includes(row.id)||row.version<=operation.maximum)) || (operation.id&&row.version!==selected.value?.version))throw new Error('写入回执与本次操作不一致，需要只读核对。')
    apply(row);notice.value=targetStatus==='draft'?'草稿已保存，尚未启用；已有规则和任务未改变。':`此版本状态：${statusLabel(row.status)}；已有引用不会自动切换。`
    return true
  }catch(cause){
    if(!current(request,user))return false
    error.value=apiErrorSummary(cause,'字段操作未完成，输入已保留')
    issues.value=authoringIssues(cause,{label:'meaning',category:'meaning',description:'meaning',impact:'meaning',value_type:'type',unit:'type',value_options:'type'})
    if(!isExplicitAuthoringRejection(cause)){unknown.value=operation;recovery.value=[];recoveryChecked.value=false;error.value='写入结果尚未确认；请只读核对现存版本，不要重复提交。'}
    return false
  }finally{if(current(request,user)){saving.value=false;phase.value=''}}
}
async function save() {
  if(busy.value||readonly.value||!options.value||unknown.value||leavePrompt.value||!selected.value&&!directoryComplete.value)return false
  const patch=dirtyPayload(),id=selected.value?.id
  if(id&&!Object.keys(patch).length){notice.value='草稿内容未改变，无需重复保存。';return true}
  return perform(pending(id?'update':'create'),()=>id?fieldDefinitionVersions.update(id,patch):fieldDefinitionVersions.create(props.fieldDefinition,payload(),sourceVersion.value||undefined))
}
async function transition(action:'publish'|'retire') {
  if(busy.value||unknown.value||leavePrompt.value||!selected.value)return
  if(action==='publish'&&selected.value.status!=='draft'||action==='retire'&&selected.value.status!=='published')return
  if(action==='publish'&&dirty.value&&!(await save()))return
  if(!selected.value||disposed||unknown.value)return
  const id=selected.value.id
  await perform(pending(action),()=>fieldDefinitionVersions.transition(id,action))
}
async function readback() {
  const requestInfo=unknown.value
  if(!requestInfo||busy.value)return
  const request=++serial,user=account.value
  loading.value=true;phase.value='只读核对原字段的现存版本…';error.value='';recoveryChecked.value=false
  try {
    const result=requestInfo.id?[exact(await fieldDefinitionVersions.get(requestInfo.id),requestInfo.id)]:await fieldDefinitionVersions.list(requestInfo.field)
    if(!current(request,user))return
    if(result.some(row=>row.field_definition!==requestInfo.field))throw new Error('核对结果包含其他字段，未采用。')
    recovery.value=result;recoveryChecked.value=true
  }catch(cause){if(current(request,user)){recovery.value=[];error.value=apiErrorSummary(cause,'只读核对失败，结果仍未知')}}
  finally{if(current(request,user)){loading.value=false;phase.value=''}}
}
async function adoptRecovery(id:string) {
  const operation=unknown.value
  if(!operation||busy.value||!recovery.value.some(row=>row.id===id))return
  const request=++serial,user=account.value
  loading.value=true;phase.value='正在载入所选精确版本…';error.value=''
  try{
    const row=exact(await fieldDefinitionVersions.get(id),id)
    if(!current(request,user))return
    if(operation.id&&id!==operation.id || operation.kind==='create'&&(operation.knownIds.includes(id)||id===operation.source||row.version<=operation.maximum))throw new Error('该版本在原请求前已存在或不高于原目录版本基线，不能作为新建结果解除未知状态。')
    rows.value=[row,...rows.value.filter(item=>item.id!==row.id)].sort((a,b)=>b.version-a.version)
    install(row);notice.value='已明确载入此现存版本，原本地编辑已替换；未重发写入，也不认定它一定由上次请求创建。'
  }catch(cause){if(current(request,user))error.value=apiErrorSummary(cause,'载入失败，结果仍未知')}
  finally{if(current(request,user)){loading.value=false;phase.value=''}}
}
watch(()=>props.fieldDefinition,async()=>{
  const field=props.fieldDefinition,user=account.value
  serial++;install(null);editing.value=false;predecessor.value=null;rows.value=[];options.value=null;directoryComplete.value=false
  await loadDirectory();if(props.initialId&&!disposed&&field===props.fieldDefinition&&user===account.value)await open(props.initialId)
},{immediate:true})
watch(()=>props.initialId,id=>{if(id)void open(id)})
</script>

<template>
  <section class="field-versions" :aria-busy="busy">
    <ConfigurationDiscardNotice v-if="leavePrompt" :message="leavePrompt" :busy="busy" @keep="keepEditing" @discard="discardAndLeave" />
    <RegistryResourceExplorer :key="account" label="字段定义" :inert="Boolean(leavePrompt)" v-slot="{inspect}">
      <p>同一个字段身份可以有多个定义版本。结构与策略引用精确版本；名称、类型和单位不从最新版本自动替换。</p>
      <p v-if="error" role="alert" class="error">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p><p v-if="busy" role="status">{{ phase }}</p>
      <p v-if="directoryError" role="alert" class="error">{{directoryError}}。目录未恢复前不自动选择或补全定义。</p>
      <div class="actions"><button type="button" class="button ghost" :disabled="busy" @click="reload">重新读取目录</button><button type="button" class="button primary" :disabled="busy||!options||!directoryComplete||Boolean(unknown)" @click="begin()">新建空白版本</button><button type="button" class="button ghost" :disabled="busy" @click="inspect('field',fieldDefinition,$event)">查看所属字段与使用关系</button></div>
      <label>查看具体版本<select :value="selected?.id||''" :disabled="busy||Boolean(unknown)" @change="open(($event.target as HTMLSelectElement).value)"><option value="" disabled>选择版本，不自动取最新</option><option v-for="row in rows" :key="row.id" :value="row.id">v{{row.version}} · {{row.label||'未命名草稿'}} · {{statusLabel(row.status)}}</option></select></label>
      <p v-if="!busy&&!error&&!rows.length">尚无定义版本，请创建草稿。</p>
      <section v-if="unknown" aria-label="写入结果核对" class="recovery"><h3>先核对写入结果</h3><p>只查询原字段的现存记录。名称相同不是成功回执，空结果也不能证明写入失败；本窗口内不会重复提交。</p><button class="button ghost" :disabled="busy" @click="readback">只读核对现存版本</button><p v-if="recoveryChecked&&!recovery.length">没有返回记录，结果仍未知，不自动重试。</p><ul><li v-for="row in recovery" :key="row.id"><span>{{row.label}} · v{{row.version}} · {{statusLabel(row.status)}}<small>{{row.id}}</small></span><button class="button ghost" :disabled="busy" @click="adoptRecovery(row.id)">载入此精确版本</button></li></ul></section>
      <section v-if="editing" class="editor">
        <header><h2>{{selected?`${selected.label||'未命名草稿'} · v${selected.version}`:'新建字段定义草稿'}}</h2><p v-if="predecessor">基于 {{predecessor.label}} · v{{predecessor.version}}。复制此精确版本；新版本号由服务器分配，不更改原定义。</p><button v-if="selected" class="button ghost" :disabled="busy" @click="inspect('field_version',selected.id,$event)">查看此版本与使用关系</button></header>
        <FieldDefinitionView v-if="readonly&&selected" :definition="selected" />
        <form v-else @submit.prevent="save">
          <ConfigurationAuthoringShell label="字段定义配置" :status="selected?`${statusLabel(selected.status)}${dirty?' · 有未保存修改':' · 已保存'}`:'尚未保存'" :sections="sections" :busy="busy" :issues="issues">
            <template #meaning><fieldset :disabled="busy||Boolean(unknown)"><label data-authoring-field="label">显示名称（建议中文，与业务页面一致）<input v-model="form.label" maxlength="160" /></label><label data-authoring-field="category">分类标识<input v-model="form.category" maxlength="80" /></label><label data-authoring-field="description">含义说明<textarea v-model="form.description" rows="3" /></label><label data-authoring-field="impact">影响说明<textarea v-model="form.impact" rows="2" /></label></fieldset></template>
            <template #type><fieldset :disabled="busy||Boolean(unknown)||!options"><label data-authoring-field="value_type">值类型<select v-model="form.value_type"><option value="">草稿暂未设置</option><option v-if="form.value_type&&!options?.value_types.some(item=>item.value===form.value_type)" :value="form.value_type" disabled>原值 {{form.value_type}}（目录未返回）</option><option v-for="item in options?.value_types" :key="item.value" :value="item.value">{{item.label}}</option></select></label><label data-authoring-field="unit">单位（可选）<input v-model="form.unit" maxlength="80" /></label><label v-if="form.value_type==='enum'" data-authoring-field="value_options">枚举选项（每行一项）<textarea v-model="enumText" rows="4" /></label></fieldset></template>
            <template #review><p>公共字段定义 → 结构组织或策略选用 → 本次任务填写／生成具体值。字段定义本身不执行 AI，也不保存项目内容。</p><p>本地类型：{{form.value_type||'尚未选择'}}；单位：{{form.unit||'未设置'}}。保存后可核对服务器返回的类型样例；样例不是默认值。</p><FieldDefinitionView v-if="selected" :definition="selected" /></template>
            <template #actions><button type="submit" class="button primary" :disabled="busy||Boolean(unknown)||!options||!selected&&!directoryComplete">保存草稿</button><button v-if="selected?.status==='draft'" type="button" class="button ghost" :disabled="busy||Boolean(unknown)||!options" @click="transition('publish')">保存并启用此版本</button></template>
          </ConfigurationAuthoringShell>
        </form>
        <div class="actions"><button v-if="selected" class="button ghost" :disabled="busy||Boolean(unknown)||!options||!directoryComplete" @click="begin(true)">以此版本创建草稿</button><button v-if="selected?.status==='published'" class="button ghost" :disabled="busy||Boolean(unknown)" @click="transition('retire')">停用此版本（保留已有引用）</button></div>
      </section>
    </RegistryResourceExplorer>
  </section>
</template>

<style scoped>
.field-versions{display:grid;gap:var(--config-space-lg);min-width:0;font:400 var(--config-body-size)/1.6 var(--font);padding:var(--config-space-lg)}.field-versions :deep(.resource-owner){display:grid;gap:var(--config-space-lg)}.field-versions p,.field-versions h2,.field-versions h3{margin:0;overflow-wrap:anywhere}.field-versions p{color:var(--text-secondary)}.field-versions h2{font-size:var(--config-title-size)}.field-versions h3{font-size:var(--config-section-size)}.actions{display:flex;gap:var(--config-space-sm);flex-wrap:wrap}.editor,.editor header,.editor form{display:grid;gap:var(--config-space-lg);min-width:0}.field-versions label{display:grid;gap:var(--config-space-xs);min-width:0}.field-versions input,.field-versions select,.field-versions textarea{box-sizing:border-box;width:100%;min-width:0;min-height:44px;font:inherit;background:var(--surface);color:var(--text);border:1px solid var(--border-strong);border-radius:var(--config-radius);padding:var(--config-space-sm)}.field-versions fieldset{display:grid;gap:var(--config-space-md);min-width:0;border:0;padding:0;margin:0}.field-versions button{white-space:normal;overflow-wrap:anywhere;min-height:44px}.field-versions .error{color:var(--red)}.recovery{border:1px solid var(--border);padding:var(--config-space-md);display:grid;gap:var(--config-space-md)}.recovery ul{list-style:none;padding:0;margin:0}.recovery li{display:flex;flex-wrap:wrap;gap:var(--config-space-sm);padding:var(--config-space-sm) 0}.recovery span{flex:1;min-width:0;overflow-wrap:anywhere}.recovery small{display:block;font-size:var(--config-help-size)}
</style>
