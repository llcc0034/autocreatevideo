<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink, onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import BaseModal from './BaseModal.vue'
import FieldDefinitionVersions from './FieldDefinitionVersions.vue'
import PlatformStatusPill from './PlatformStatusPill.vue'
import RegistryDeletionReviewModal from './RegistryDeletionReviewModal.vue'
import { relationOverviewUrl } from '@/services/registryRelations'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { fieldDefinitionVersions } from '@/services/fieldDefinitionVersions'
import { fieldDefinitionDisplay } from '@/services/fieldDefinitionDisplay'
import type { FieldDefinition } from '@/services/platformTypes'
import { useAuthStore } from '@/stores/auth'
import ConfigurationAuthoringShell from './ConfigurationAuthoringShell.vue'
import ConfigurationDiscardNotice from './ConfigurationDiscardNotice.vue'
import RegistryResourceExplorer from './RegistryResourceExplorer.vue'
import { authoringIssues, isExplicitAuthoringRejection, type AuthoringIssue } from '@/services/configurationAuthoring'

const ContentSourceManager = defineAsyncComponent(() => import('./ContentSourceManager.vue'))
const DataStructureManager = defineAsyncComponent(() => import('./DataStructureManager.vue'))
const DataStructureMappingPreview = defineAsyncComponent(() => import('./DataStructureMappingPreview.vue'))
const props = defineProps<{ fieldDefinitionId?: string; fieldVersionId?: string; structureVersionId?: string; sourceVersionId?: string }>()
const auth=useAuthStore(), account=computed(()=>String(auth.user?.id||''))
const rows = ref<FieldDefinition[]>([]), count = ref(0), page = ref(1)
const displayNames = ref<Record<string, { value: ReturnType<typeof fieldDefinitionDisplay>; loading: boolean; error: string }>>({})
let nameSerial = 0
const nameRequests = new Map<string, number>()
const listHasNext = ref(false), listHasPrevious = ref(false)
const search = ref(''), includeInactive = ref(false), loading = ref(false), saving = ref(false)
const error = ref(''), notice = ref(''), editorError = ref('')
const editorOpen = ref(false), versions = ref<FieldDefinition | null>(null), versionBusy = ref(false)
const versionEditor = ref<InstanceType<typeof FieldDefinitionVersions> | null>(null)
const sourceOpen = ref(false), deleting = ref<FieldDefinition | null>(null)
const structuresOpen = ref(false)
const mappingOpen = ref(false)
const original = ref<FieldDefinition | null>(null)
const form = reactive({ key: '', isActive: true })
const identityReading = ref(false), identityPhase = ref(''), leavePrompt = ref('')
const identityIssues = ref<AuthoringIssue[]>([])
type IdentityWrite = { id?: string; key: string; active?: boolean }
const unknownIdentity = ref<IdentityWrite | null>(null), recovered = ref<FieldDefinition[]>([])
const recoveryPage = ref(1), recoveryCount = ref(0), recoveryChecked = ref(false)
const recoveryHasNext = ref(false), recoveryHasPrevious = ref(false)
const identityBusy = computed(() => saving.value || identityReading.value)
const identityDirty = computed(() => editorOpen.value && (original.value ? form.isActive !== original.value.isActive : form.key !== ''))
const identitySections = [
  {key:'identity', title:'稳定身份', description:'登记公共字段唯一键；名称、含义与类型由定义版本维护。'},
  {key:'usage', title:'影响与后续', description:'身份管理可选范围，定义版本供结构和策略精确引用；具体值由任务提供。'},
]
let leaveAction: (()=>void) | null = null, routeCancel: (()=>void) | null = null
let listSerial = 0, detailSerial = 0, writeSerial=0, disposed=false
function keepIdentity() { routeCancel?.(); routeCancel=null; leaveAction=null; leavePrompt.value='' }
function discardIdentity() { const action=leaveAction;routeCancel=null;leaveAction=null;leavePrompt.value='';unknownIdentity.value=null;editorOpen.value=false;action?.() }
function requestIdentityLeave(action:()=>void) {
  if(identityBusy.value || leavePrompt.value)return false
  if(identityDirty.value || unknownIdentity.value){leavePrompt.value=unknownIdentity.value?'写入结果尚未核实。关闭会丢失本窗口核对上下文，不会撤销服务端操作；请优先只读核对。':'字段身份有未保存修改，放弃后将丢失本地输入。';leaveAction=action;return false}
  action();return true
}
function closeIdentity() { requestIdentityLeave(()=>{editorOpen.value=false}) }
function identityMayLeave() {
  if(!editorOpen.value)return true
  if(identityBusy.value || leavePrompt.value)return false
  if(!identityDirty.value && !unknownIdentity.value)return true
  return new Promise<boolean>(resolve=>{requestIdentityLeave(()=>resolve(true));routeCancel=()=>resolve(false)})
}
function identityBeforeUnload(event:BeforeUnloadEvent) {if(editorOpen.value&&(identityDirty.value||unknownIdentity.value||identityBusy.value)){event.preventDefault();event.returnValue=''}}
onBeforeRouteLeave(identityMayLeave);onBeforeRouteUpdate(identityMayLeave)
onMounted(()=>window.addEventListener('beforeunload',identityBeforeUnload))
onBeforeUnmount(() => { disposed=true;listSerial++; detailSerial++;writeSerial++;keepIdentity();window.removeEventListener('beforeunload',identityBeforeUnload) })
watch(account,()=>{
  listSerial++;detailSerial++;writeSerial++;loading.value=false;saving.value=false;identityReading.value=false;keepIdentity()
  unknownIdentity.value=null;recovered.value=[];recoveryChecked.value=false;identityIssues.value=[];identityPhase.value=''
  rows.value=[];count.value=0;versions.value=null;editorOpen.value=false;sourceOpen.value=false;structuresOpen.value=false;mappingOpen.value=false;deleting.value=null;original.value=null
  form.key='';form.isActive=true;notice.value='';editorError.value='';error.value='账号已变化，请重新查询字段；旧账号结果不会覆盖当前页面。'
},{flush:'sync'})
function openVersions(row:FieldDefinition){detailSerial++;versions.value=row}
function closeVersions(){detailSerial++;const row=versions.value;versions.value=null;if(row)void loadDisplayName(row,listSerial)}

async function loadDisplayName(row: FieldDefinition, request: number) {
  const ticket = ++nameSerial; nameRequests.set(row.id, ticket)
  displayNames.value[row.id] = { value: null, loading: true, error: '' }
  try {
    const value = fieldDefinitionDisplay(row.id, await fieldDefinitionVersions.list(row.id))
    if (request === listSerial && !disposed && nameRequests.get(row.id) === ticket) displayNames.value[row.id] = { value, loading: false, error: '' }
  } catch (cause) {
    if (request === listSerial && !disposed && nameRequests.get(row.id) === ticket) displayNames.value[row.id] = { value: null, loading: false, error: apiErrorSummary(cause, '显示名称读取失败') }
  }
}
async function loadDisplayNames(currentRows: FieldDefinition[], request: number) {
  // Only the visible server page, at most four reads concurrently; no whole-catalog scan.
  for (let index=0; index<currentRows.length; index+=4) {
    if (request !== listSerial || disposed) return
    await Promise.all(currentRows.slice(index,index+4).map(row=>loadDisplayName(row,request)))
  }
}

async function load() {
  const request = ++listSerial
  loading.value = true; error.value = ''
  try {
    const result = await platformApi.fieldDefinitionPage(page.value, search.value.trim(), includeInactive.value)
    if (request !== listSerial) return
    rows.value = result.results; count.value = result.count; listHasNext.value=result.hasNext;listHasPrevious.value=result.hasPrevious
    displayNames.value = {}; void loadDisplayNames(result.results, request)
  } catch (cause) { if (request === listSerial) error.value = apiErrorSummary(cause, '字段读取失败，当前列表未刷新') }
  finally { if (request === listSerial) loading.value = false }
}
function begin(row: FieldDefinition | null = null) {
  requestIdentityLeave(()=>{
    detailSerial++;original.value = row; form.key = row?.key || ''; form.isActive = row?.isActive ?? true
    editorError.value = ''; identityIssues.value=[];unknownIdentity.value=null;recovered.value=[];recoveryChecked.value=false;notice.value='';editorOpen.value = true
  })
}
function exactIdentity(row:FieldDefinition, target:IdentityWrite) {
  if(!row?.id || row.key!==target.key || typeof row.isActive!=='boolean' || target.id&&row.id!==target.id)throw Error('返回的字段身份不匹配，未载入其他记录。')
  return row
}
const identityCurrent=(request:number,user:string)=>!disposed&&request===writeSerial&&user===account.value
async function save() {
  if (identityBusy.value || unknownIdentity.value || leavePrompt.value || !editorOpen.value) return
  const request=++writeSerial,user=account.value,creating=!original.value
  const intent:IdentityWrite={id:original.value?.id,key:original.value?.key||form.key.trim(),...(original.value?{active:form.isActive}:{})}
  saving.value = true; editorError.value = ''; notice.value = '';identityIssues.value=[];identityPhase.value='正在保存字段身份…'
  try {
    if (original.value && original.value.isActive === form.isActive) {
      editorError.value = '启停未改变，无需重复保存。'; return
    }
    const result = exactIdentity(intent.id
      ? await platformApi.updateFieldDefinition(intent.id, { is_active: intent.active! })
      : await platformApi.createFieldDefinition({ key: intent.key }),intent)
    if(!identityCurrent(request,user))return
    if(intent.id&&result.isActive!==intent.active)throw Error('状态回执与本次修改不一致，需要只读核对。')
    editorOpen.value = false
    notice.value = `字段 ${result.key} 已保存；${creating?'接着在定义版本中配置名称与类型。':'已有定义版本及引用未改变。'}`
    if (creating) openVersions(result)
    await load()
  } catch (cause) {
    if(!identityCurrent(request,user))return
    editorError.value = apiErrorSummary(cause, '字段保存未完成，输入已保留')
    identityIssues.value=authoringIssues(cause,{key:'identity',is_active:'usage'})
    if(!isExplicitAuthoringRejection(cause)){unknownIdentity.value=intent;recoveryPage.value=1;recoveryChecked.value=false;recovered.value=[];editorError.value='写入结果尚未确认。请只读核对现存字段，不要重复提交。'}
  }
  finally { if(identityCurrent(request,user)){saving.value = false;identityPhase.value=''} }
}
async function readIdentityResult(nextPage=1) {
  const intent=unknownIdentity.value
  if(!intent||identityBusy.value||leavePrompt.value||nextPage<1)return
  const request=++writeSerial,user=account.value
  identityReading.value=true;identityPhase.value='只读核对原字段…';editorError.value='';recoveryChecked.value=false
  try{
    if(intent.id){const row=exactIdentity(await platformApi.getFieldDefinition(intent.id),intent);if(!identityCurrent(request,user))return;recovered.value=[row];recoveryCount.value=1}
    else{
      const result=await platformApi.fieldDefinitionPage(nextPage,intent.key,true)
      if(!identityCurrent(request,user))return
      recovered.value=result.results.filter(row=>row.key===intent.key).map(row=>exactIdentity(row,intent));recoveryCount.value=result.count
      recoveryHasNext.value=result.hasNext;recoveryHasPrevious.value=result.hasPrevious
    }
    recoveryPage.value=nextPage;recoveryChecked.value=true
  }catch(cause){if(identityCurrent(request,user)){recovered.value=[];editorError.value=apiErrorSummary(cause,'核对失败，写入结果仍未知')}}
  finally{if(identityCurrent(request,user)){identityReading.value=false;identityPhase.value=''}}
}
async function adoptIdentity(id:string) {
  const intent=unknownIdentity.value
  if(!intent||identityBusy.value||leavePrompt.value||!recovered.value.some(row=>row.id===id))return
  const request=++writeSerial,user=account.value
  identityReading.value=true;identityPhase.value='正在载入所选精确身份…';editorError.value=''
  try{
    const row=exactIdentity(await platformApi.getFieldDefinition(id),{...intent,id})
    if(!identityCurrent(request,user))return
    original.value=row;form.key=row.key;form.isActive=row.isActive;unknownIdentity.value=null;recovered.value=[];identityIssues.value=[]
    notice.value='已明确载入此现存身份，替换本地编辑；未重发写入，不认定该记录由上次请求创建或修改。'
  }catch(cause){if(identityCurrent(request,user))editorError.value=apiErrorSummary(cause,'载入失败，结果仍未知')}
  finally{if(identityCurrent(request,user)){identityReading.value=false;identityPhase.value=''}}
}
function goToIdentityVersions(){const row=original.value;if(row&&!unknownIdentity.value)requestIdentityLeave(()=>{editorOpen.value=false;openVersions(row)})}
async function openDeepLink() {
  const request = ++detailSerial
  const id=props.fieldDefinitionId,user=account.value
  versions.value = null
  if (!id) return
  try {
    const row = await platformApi.getFieldDefinition(id)
    if (request !== detailSerial||user!==account.value||id!==props.fieldDefinitionId||disposed)return
    if(row.id!==id)throw new Error('字段身份不匹配，未打开其他记录。')
    versions.value = row
  } catch (cause) { if (request === detailSerial) error.value = apiErrorSummary(cause, '指定字段不存在或无法读取') }
}
watch(() => props.fieldDefinitionId, openDeepLink, { immediate: true })
watch(() => props.structureVersionId, value => { structuresOpen.value = Boolean(value) }, { immediate: true })
watch(() => props.sourceVersionId, value => { sourceOpen.value = Boolean(value) }, { immediate: true })
void load()
</script>

<template>
  <section class="field-maintenance" :aria-busy="loading">
    <header><div><h2>字段维护</h2><p>统一维护字段身份与定义版本，供结构、配方、执行实例和提示词策略复用。实际内容、默认值与必填要求由使用处保存；模型部署负责连接与运行资源，不另维护一份创作字段。</p></div><RouterLink class="button ghost small" to="/settings?section=prompts">提示词策略</RouterLink></header>
    <p>中文名称复用定义版本的显示名，建议与业务页面用词一致。列表优先展示最新可用定义；页面仍使用其实际引用版本，不会随列表名称自动升级。删除字段不是隐藏文本框的开关，请先查看“层级与引用”及删除预览。</p>
    <div class="actions"><button class="button primary" :disabled="loading || saving" @click="begin()">新建字段</button><button class="button ghost" @click="structuresOpen=true">输入／输出结构</button><button class="button ghost" @click="mappingOpen=true">字段映射预览</button><button class="button ghost" @click="sourceOpen=true">内容来源版本</button></div>
    <form class="actions" @submit.prevent="page=1; load()"><label>搜索字段标识<input v-model="search" /></label><label class="check"><input v-model="includeInactive" type="checkbox" />包含停用字段</label><button class="button ghost" :disabled="loading">查询／刷新</button></form>
    <p v-if="loading" role="status">正在读取字段…</p><p v-if="error" role="alert">{{ error }} <button class="button ghost small" @click="load">重试</button></p><p v-if="notice" role="status">{{ notice }}</p>
    <div class="table-wrap"><table><caption>字段身份 · 共 {{ count }} 项 · 第 {{ page }} 页<span v-if="error">（上次结果，未刷新）</span></caption><thead><tr><th scope="col">稳定标识</th><th scope="col">中文名称 / 定义版本</th><th scope="col">状态</th><th scope="col">管理</th></tr></thead><tbody><tr v-for="row in rows" :key="row.id"><th scope="row">{{ row.key }}</th><td class="field-display-name"><span v-if="displayNames[row.id]?.loading || !displayNames[row.id]">正在读取名称…</span><template v-else-if="displayNames[row.id]?.error"><span role="alert">{{ displayNames[row.id]?.error }}</span><button type="button" class="button ghost small" @click="loadDisplayName(row,listSerial)">重读名称</button></template><template v-else-if="displayNames[row.id]?.value"><strong>{{ displayNames[row.id]?.value?.label || '未填写显示名' }}</strong><small>{{ displayNames[row.id]?.value?.basis }} · v{{ displayNames[row.id]?.value?.version }} · {{ displayNames[row.id]?.value?.status==='published'?'可用':displayNames[row.id]?.value?.status==='draft'?'草稿':'停用' }}</small><small v-if="!displayNames[row.id]?.value?.chinese">尚未设置中文显示名，可在定义版本中维护。</small></template><span v-else>尚无定义版本，未设置中文显示名。</span></td><td><PlatformStatusPill :status="row.isActive?'active':'inactive'" /></td><td class="actions"><button class="button ghost small" :disabled="loading" @click="openVersions(row)">定义版本</button><button class="button ghost small" :disabled="loading" @click="begin(row)">管理身份</button><RouterLink class="button ghost small" :to="relationOverviewUrl('field',row.id)">层级与引用</RouterLink><button class="button danger small" :disabled="loading" @click="deleting=row" :aria-label="`删除字段 ${row.key}`">删除</button></td></tr></tbody></table></div>
    <p v-if="!loading && !error && !rows.length">当前条件没有字段。可调整查询条件或新建字段。</p>
    <nav class="actions" aria-label="字段分页"><button class="button ghost" :disabled="loading || !listHasPrevious" @click="page--; load()">上一页</button><span>第 {{ page }} 页 · 共 {{ count }} 项</span><button class="button ghost" :disabled="loading || !listHasNext" @click="page++; load()">下一页</button></nav>
    <BaseModal configuration :open="editorOpen" :dismissible="!identityBusy" :title="original?'管理字段身份':'新建字段身份'" width="800px" @close="closeIdentity">
      <div class="identity-author">
        <ConfigurationDiscardNotice v-if="leavePrompt" :message="leavePrompt" :busy="identityBusy" @keep="keepIdentity" @discard="discardIdentity" />
        <RegistryResourceExplorer v-if="editorOpen" :key="`${account}:${original?.id||'new'}`" label="字段身份" :inert="Boolean(leavePrompt)" v-slot="{inspect}">
          <p v-if="identityBusy" role="status">{{identityPhase}}</p><p v-if="editorError" role="alert">{{editorError}}</p><p v-if="notice" role="status">{{notice}}</p>
          <section v-if="unknownIdentity" class="identity-recovery" aria-label="字段身份写入核对">
            <h3>先核对写入结果</h3><p>只读核对原唯一键（包括停用记录）；当前窗口不重发写入。现存记录或空结果都不能证明上次请求成功或失败。</p>
            <button class="button ghost" :disabled="identityBusy" @click="readIdentityResult()">只读核对现存字段</button>
            <p v-if="recoveryChecked&&!recovered.length">本页未找到精确唯一键，结果仍未知；有后续页时可继续查看。</p>
            <ul><li v-for="row in recovered" :key="row.id"><span>{{row.key}} · {{row.isActive?'启用':'停用'}}<small>{{row.id}}</small></span><button class="button ghost" :disabled="identityBusy" @click="adoptIdentity(row.id)">载入此精确身份</button></li></ul>
            <nav v-if="recoveryChecked&&!unknownIdentity.id" class="actions" aria-label="身份核对分页"><button class="button ghost" :disabled="identityBusy||!recoveryHasPrevious" @click="readIdentityResult(recoveryPage-1)">上一页匹配结果</button><span>第{{recoveryPage}}页 · 搜索匹配{{recoveryCount}}项（仅展示精确唯一键）</span><button class="button ghost" :disabled="identityBusy||!recoveryHasNext" @click="readIdentityResult(recoveryPage+1)">下一页匹配结果</button></nav>
          </section>
          <form class="identity-form" @submit.prevent="save">
            <ConfigurationAuthoringShell label="字段身份配置" :status="original?(identityDirty?'有未保存修改':'已保存的身份'):'尚未创建'" :busy="identityBusy" :issues="identityIssues" :sections="identitySections">
              <template #identity>
                <div v-if="original"><p>唯一键：<strong>{{original.key}}</strong></p><p>创建后不改名；名称和说明在定义版本维护。</p><button type="button" class="button ghost" :disabled="identityBusy" @click="inspect('field',original.id,$event)">查看身份与使用关系</button></div>
                <div v-else data-authoring-field="key"><label>唯一键<input v-model="form.key" :disabled="identityBusy||Boolean(unknownIdentity)" required maxlength="120" /></label><small>填写稳定、可复用的业务标识；此处不填写具体项目内容。</small></div>
              </template>
              <template #usage>
                <label v-if="original" class="check" data-authoring-field="is_active"><input v-model="form.isActive" :disabled="identityBusy||Boolean(unknownIdentity)" type="checkbox" />启用字段身份</label><p v-else>初始启停状态与排序由服务端设置，此处不预填或覆盖。</p>
                <p>字段身份 → 名称／类型等定义版本 → 结构或策略引用 → 本次任务的具体值。停用不是删除，不改写已有定义版本、引用或历史内容。</p>
                <p v-if="!original">创建身份后进入定义版本继续配置；不会自动创建定义版本、发布配置或执行任务。</p>
                <button v-else type="button" class="button ghost" :disabled="identityBusy||Boolean(unknownIdentity)" @click="goToIdentityVersions">进入定义版本</button>
              </template>
              <template #actions><button class="button primary" :disabled="identityBusy||Boolean(unknownIdentity)">{{identityBusy?'处理中…':original?'保存身份修改':'创建身份并配置定义'}}</button></template>
            </ConfigurationAuthoringShell>
          </form>
        </RegistryResourceExplorer>
      </div>
    </BaseModal>
    <BaseModal configuration :open="Boolean(versions)" :dismissible="!versionBusy" :title="`定义版本 · ${versions?.key || ''}`" width="1000px" @close="versionEditor?.requestClose()"><FieldDefinitionVersions v-if="versions" ref="versionEditor" :key="versions.id" :field-definition="versions.id" :initial-id="versions.id===fieldDefinitionId ? fieldVersionId : undefined" @busy="versionBusy=$event" @close="closeVersions" /></BaseModal>
    <ContentSourceManager v-if="sourceOpen" :initial-id="sourceVersionId" @close="sourceOpen=false" />
    <DataStructureManager v-if="structuresOpen" :initial-id="structureVersionId" @close="structuresOpen=false" />
    <BaseModal :open="mappingOpen" title="字段映射预览（不保存、不执行）" width="1100px" @close="mappingOpen=false"><DataStructureMappingPreview v-if="mappingOpen" /></BaseModal>
    <RegistryDeletionReviewModal v-if="deleting" open resource-type="field_definition" :object-id="deleting.id" :title="`字段 ${deleting.key}`" @close="deleting=null" @completed="deleting=null; load()" />
  </section>
</template>

<style scoped>
.table-wrap table{min-width:680px}.table-wrap th[scope=row]{min-width:135px;max-width:200px;overflow-wrap:anywhere}
.field-display-name{min-width:160px;max-width:300px;overflow-wrap:anywhere}.field-display-name strong,.field-display-name small{display:block}.field-display-name small{font-size:var(--config-help-size);color:var(--text-muted);margin-top:4px}
.field-maintenance,.identity-form{display:grid;gap:var(--config-space-lg);min-width:0}.field-maintenance header{display:flex;gap:var(--config-space-lg);align-items:start;justify-content:space-between}.field-maintenance h2,.field-maintenance p{margin:0;line-height:1.65}.actions{display:flex;gap:var(--config-space-sm);align-items:center;flex-wrap:wrap}.field-maintenance label{display:grid;gap:var(--config-space-xs)}.field-maintenance .check{display:flex;align-items:center;gap:var(--config-space-sm)}.field-maintenance input{min-height:36px;max-width:100%;box-sizing:border-box}.field-maintenance input[type=checkbox]{min-height:auto}.table-wrap{max-width:100%;overflow:auto}table{width:100%;border-collapse:collapse;text-align:left}caption{text-align:left;padding:var(--config-space-sm) 0}th,td{padding:12px 8px;border-bottom:1px solid var(--border);overflow-wrap:anywhere}[role=alert]{color:var(--red)}.identity-author{padding:var(--config-space-xl);min-width:0;font:400 var(--config-body-size)/1.6 var(--font)}.identity-author :deep(.resource-owner),.identity-recovery{display:grid;gap:var(--config-space-lg)}.identity-author button{white-space:normal;min-height:44px}.identity-author p,.identity-author strong{overflow-wrap:anywhere}.identity-recovery h3{font-size:var(--config-section-size)}.identity-recovery small{display:block;font-size:var(--config-help-size)}.identity-recovery li{display:flex;gap:var(--config-space-sm);flex-wrap:wrap;padding:var(--config-space-sm) 0}.identity-recovery span{min-width:0;overflow-wrap:anywhere}.identity-author input{width:100%;box-sizing:border-box}.identity-author input[type=checkbox]{width:auto}@media(max-width:640px){.field-maintenance header{flex-direction:column}.actions{align-items:stretch}.actions>label:not(.check){width:100%}th,td{padding:8px}.actions button{min-height:44px}table{min-width:360px}.identity-author{padding:var(--config-space-lg)}}
</style>
