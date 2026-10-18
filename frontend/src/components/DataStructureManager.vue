<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import BaseModal from './BaseModal.vue'
import ConfigurationAuthoringShell from './ConfigurationAuthoringShell.vue'
import ConfigurationDiscardNotice from './ConfigurationDiscardNotice.vue'
import RegistryResourceExplorer from './RegistryResourceExplorer.vue'
import DataStructureDefinitionView from './DataStructureDefinitionView.vue'
import DataStructureNodeEditor from './DataStructureNodeEditor.vue'
import DataStructureValuePreview from './DataStructureValuePreview.vue'
import { dataStructures, type DataStructureContent, type DataStructureDetail, type DataStructureSummary, type StructureNode, type StructureAuthoringOptions } from '@/services/dataStructures'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { sameStructureValue } from '@/services/structureAuthoring'
import { authoringIssues, isExplicitAuthoringRejection, type AuthoringIssue } from '@/services/configurationAuthoring'

const props = defineProps<{ initialId?: string }>()
const emit = defineEmits<{close: []}>()
const auth=useAuthStore(),account=computed(()=>String(auth.user?.id||''))
const rows=ref<DataStructureSummary[]>([]),selected=ref<DataStructureDetail|null>(null),predecessor=ref<DataStructureDetail|null>(null)
const cloneSource=computed(()=>predecessor.value?.id||'')
const options=ref<StructureAuthoringOptions|null>(null)
const catalogReady=ref(false)
const page=ref(1),count=ref(0),search=ref(''),status=ref(''),hasNext=ref(false),hasPrevious=ref(false)
const loading=ref(false),saving=ref(false),detailBusy=ref(false),error=ref(''),listError=ref(''),notice=ref(''),phase=ref('')
const editing=ref(false),nodeIndex=ref(-1),removeIndex=ref(-1),previewOpen=ref(false),leavePrompt=ref('')
const draftErrors=ref<string[]>([]),issues=ref<AuthoringIssue[]>([])
const schemaReport=ref<Awaited<ReturnType<typeof dataStructures.schema>>|null>(null)
const blank=():DataStructureContent=>({name:'',description:'',nodes:[]})
const form=reactive<DataStructureContent>(blank())
const readonly=computed(()=>Boolean(selected.value&&selected.value.status!=='draft'))
const changed=computed(()=>Boolean(draftErrors.value.length)||!selected.value||Object.keys(patch()).length>0)
const dirty=computed(()=>editing.value&&!readonly.value&&changed.value)
const node=computed(()=>form.nodes[nodeIndex.value])
const busy=computed(()=>saving.value||detailBusy.value)
type Pending={kind:'create'|'update'|'publish'|'retire';id?:string;source:DataStructureSummary|null;knownIds:string[];version?:number;family?:string;attempted:Partial<DataStructureContent>}
const unknown=ref<Pending|null>(null),recovery=ref<DataStructureSummary[]>([]),recoveryPage=ref(1),recoveryNext=ref(false),recoveryPrevious=ref(false),recoveryChecked=ref(false)
const locked=computed(()=>busy.value||Boolean(unknown.value)||Boolean(leavePrompt.value))
const sections=[
  {key:'meaning',title:'用途与身份',description:'为可复用的输入／输出结构命名，不在这里填写项目内容。'},
  {key:'nodes',title:'节点组织与规则',description:'引用精确字段版本，再定义层级、属性名、顺序及局部约束。'},
  {key:'review',title:'复核与使用',description:'保存草稿 → 检查结构或试填 → 启用。下游显式引用此版本，不自动切换。'},
]
const copy=<T,>(value:T):T=>JSON.parse(JSON.stringify(value))
const content=():DataStructureContent=>copy({name:form.name,description:form.description,nodes:form.nodes})
const statusLabel=(value:string)=>options.value?.statuses.find(item=>item.value===value)?.label||value
function patch():Partial<DataStructureContent>{return Object.fromEntries(Object.entries(content()).filter(([key,value])=>!sameStructureValue(key,value,selected.value?.[key as keyof DataStructureContent])))}
let listSerial=0,detailSerial=0,disposed=false,leaveAction:(()=>void)|null=null,routeCancel:(()=>void)|null=null
const current=(request:number,user:string)=>!disposed&&request===detailSerial&&user===account.value
function exact(row:DataStructureDetail,id?:string){
  if(!row?.id||id&&row.id!==id||!row.structure_key||!Number.isSafeInteger(row.version)||row.version<1||!['draft','published','retired'].includes(row.status)||!Array.isArray(row.nodes))throw Error('结构版本身份或内容不完整，未载入其他版本。')
  return row
}
function resetEditor(){nodeIndex.value=-1;removeIndex.value=-1;draftErrors.value=[];issues.value=[];schemaReport.value=null;previewOpen.value=false}
function apply(row:DataStructureDetail){
  selected.value=row;Object.assign(form,copy({name:row.name,description:row.description,nodes:row.nodes}));predecessor.value=null
  resetEditor();editing.value=true;unknown.value=null;recovery.value=[];recoveryChecked.value=false
}
function keepEditing(){routeCancel?.();routeCancel=null;leaveAction=null;leavePrompt.value=''}
function discardAndLeave(){const action=leaveAction;routeCancel=null;leaveAction=null;leavePrompt.value='';unknown.value=null;editing.value=false;resetEditor();action?.()}
function requestLeave(action:()=>void){
  if(busy.value||leavePrompt.value)return false
  if(dirty.value||unknown.value){leavePrompt.value=unknown.value?'写入结果尚未核实。离开不会撤销服务端操作；请优先只读核对，避免重复创建。':'当前结构有未保存或未完成的输入，放弃后会丢失本地修改。';leaveAction=action;return false}
  action();return true
}
function mayLeave(){if(busy.value||leavePrompt.value)return false;if(!dirty.value&&!unknown.value)return true;return new Promise<boolean>(resolve=>{requestLeave(()=>resolve(true));routeCancel=()=>resolve(false)})}
function close(){requestLeave(()=>emit('close'))}
function beforeUnload(event:BeforeUnloadEvent){if(dirty.value||unknown.value||busy.value){event.preventDefault();event.returnValue=''}}
onBeforeRouteLeave(mayLeave);onBeforeRouteUpdate(mayLeave)
onMounted(()=>window.addEventListener('beforeunload',beforeUnload))
onBeforeUnmount(()=>{disposed=true;listSerial++;detailSerial++;keepEditing();window.removeEventListener('beforeunload',beforeUnload)})
watch(account,()=>{listSerial++;detailSerial++;loading.value=false;saving.value=false;detailBusy.value=false;keepEditing();selected.value=null;predecessor.value=null;unknown.value=null;rows.value=[];recovery.value=[];options.value=null;catalogReady.value=false;editing.value=false;resetEditor();Object.assign(form,blank());notice.value='';error.value='账号已变化，请重新读取；旧账号的迟到结果不会覆盖当前页面。'},{flush:'sync'})
watch(()=>JSON.stringify(form),()=>{schemaReport.value=null;previewOpen.value=false})
async function load(){
  const request=++listSerial,user=account.value;loading.value=true;listError.value=''
  try{
    const [catalog,result]=await Promise.all([dataStructures.options(),dataStructures.list(page.value,search.value,status.value)])
    if(disposed||request!==listSerial||user!==account.value)return
    if(!catalog.node_defaults||!catalog.statuses.length)throw Error('结构编辑目录不完整')
    options.value=catalog;catalogReady.value=true;rows.value=result.results;count.value=result.count;hasNext.value=result.hasNext;hasPrevious.value=result.hasPrevious
  }catch(cause){if(!disposed&&request===listSerial&&user===account.value){catalogReady.value=false;hasNext.value=false;hasPrevious.value=false;listError.value=apiErrorSummary(cause,'结构目录未刷新，保留上次列表；新建和保存暂停')}}
  finally{if(!disposed&&request===listSerial&&user===account.value)loading.value=false}
}
async function readVersion(id:string){
  const request=++detailSerial,user=account.value;detailBusy.value=true;error.value='';notice.value='';phase.value='正在读取精确结构版本…'
  try{const row=exact(await dataStructures.get(id),id);if(current(request,user))apply(row)}
  catch(cause){if(current(request,user))error.value=apiErrorSummary(cause,'结构读取失败，当前输入保留')}
  finally{if(current(request,user)){detailBusy.value=false;phase.value=''}}
}
async function open(id:string){if(locked.value)return;let work:Promise<void>|undefined;requestLeave(()=>{work=readVersion(id)});await work}
function begin(clone=false){
  if(locked.value||!options.value||!catalogReady.value)return
  requestLeave(()=>{const source=clone?selected.value:null;predecessor.value=source;selected.value=null;Object.assign(form,source?copy({name:source.name,description:source.description,nodes:source.nodes}):blank());editing.value=true;resetEditor();error.value='';notice.value=''})
}
function add(){
  if(!options.value||!catalogReady.value||readonly.value||locked.value||draftErrors.value.length)return
  let sequence=form.nodes.length+1;while(form.nodes.some(row=>row.node_key==='node_'+sequence))sequence++
  form.nodes.push({...copy(options.value.node_defaults),node_key:'node_'+sequence,position:form.nodes.length});nodeIndex.value=form.nodes.length-1
}
function updateNode(next:StructureNode){
  if(readonly.value||locked.value||!node.value)return
  const previous=node.value.node_key;form.nodes[nodeIndex.value]=next
  if(next.node_key!==previous)for(const row of form.nodes)if(row.parent_key===previous)row.parent_key=next.node_key
}
function removeNode(){
  const row=form.nodes[removeIndex.value];if(!row||readonly.value||locked.value||draftErrors.value.length)return
  if(form.nodes.some(child=>child.parent_key===row.node_key)){error.value='该节点仍有子节点，请先调整父节点或逐项移除；未自动删除子树。';removeIndex.value=-1;return}
  form.nodes.splice(removeIndex.value,1);removeIndex.value=-1;nodeIndex.value=-1;draftErrors.value=[]
}
function pending(kind:Pending['kind']):Pending{return{kind,id:selected.value?.id,version:selected.value?.version,family:selected.value?.structure_key,source:predecessor.value?copy(predecessor.value):null,knownIds:rows.value.map(row=>row.id),attempted:kind==='update'?patch():content()}}
function setIssues(cause:unknown){
  issues.value=authoringIssues(cause,{name:'meaning',description:'meaning',source_version:'meaning',nodes:'nodes'})
  const index=form.nodes.findIndex(row=>issues.value.some(issue=>issue.field==='nodes.'+row.node_key||issue.field.startsWith('nodes.'+row.node_key+'.')))
  if(index>=0&&!draftErrors.value.length)nodeIndex.value=index
}
async function perform(operation:Pending,writer:()=>Promise<DataStructureDetail>){
  const request=++detailSerial,user=account.value;saving.value=true;error.value='';notice.value='';issues.value=[];phase.value=operation.kind==='create'||operation.kind==='update'?'正在保存结构草稿…':'正在更新结构状态…'
  try{
    const row=exact(await writer(),operation.id);if(!current(request,user))return false
    const target=operation.kind==='publish'?'published':operation.kind==='retire'?'retired':'draft'
    if(row.status!==target||operation.id&&(row.structure_key!==operation.family||row.version!==operation.version)||operation.kind==='create'&&(operation.knownIds.includes(row.id)||operation.source&&(row.structure_key!==operation.source.structure_key||row.version<=operation.source.version)))throw Error('写入回执与原操作不一致，需要只读核对。')
    apply(row);notice.value='v'+row.version+' · '+statusLabel(row.status)+'；已有引用不自动改选。';void load();return true
  }catch(cause){
    if(!current(request,user))return false
    error.value=apiErrorSummary(cause,'结构操作未完成，当前输入保留');setIssues(cause)
    if(!isExplicitAuthoringRejection(cause)){unknown.value=operation;recovery.value=[];recoveryChecked.value=false;error.value='写入结果尚未确认。请只读核对现存版本，不要重复提交。'}
    return false
  }finally{if(current(request,user)){saving.value=false;phase.value=''}}
}
async function save(){
  if(locked.value||readonly.value||!options.value||!catalogReady.value||draftErrors.value.length)return false
  if(selected.value&&!changed.value){notice.value='草稿未改变，无需重复保存。';return true}
  const operation=pending(selected.value?'update':'create'),payload=operation.id?patch():content(),source=cloneSource.value
  return perform(operation,()=>operation.id?dataStructures.update(operation.id,payload):dataStructures.create(payload,source||undefined))
}
async function transition(action:'publish'|'retire'){
  if(locked.value||!selected.value||draftErrors.value.length)return
  if(action==='publish'&&selected.value.status!=='draft'||action==='retire'&&selected.value.status!=='published')return
  const id=selected.value.id,user=account.value
  if(action==='publish'&&changed.value&&!(await save()))return
  if(disposed||user!==account.value||unknown.value||selected.value?.id!==id)return
  await perform(pending(action),()=>dataStructures.transition(id,action))
}
async function readback(nextPage=1){
  const operation=unknown.value;if(!operation||busy.value)return
  const request=++detailSerial,user=account.value;detailBusy.value=true;phase.value='只读核对现存结构，不重发写入…';error.value='';recoveryChecked.value=false
  try{
    const result=operation.id?{results:[exact(await dataStructures.get(operation.id),operation.id)],hasNext:false,hasPrevious:false}:await dataStructures.list(nextPage,'','',operation.source?.structure_key)
    if(!current(request,user))return
    if(operation.source&&result.results.some(row=>row.structure_key!==operation.source!.structure_key))throw Error('核对返回其他结构家族，未采用。')
    recovery.value=result.results;recoveryPage.value=nextPage;recoveryNext.value=result.hasNext;recoveryPrevious.value=result.hasPrevious;recoveryChecked.value=true
  }catch(cause){if(current(request,user)){recovery.value=[];error.value=apiErrorSummary(cause,'只读核对失败，结果仍未知')}}
  finally{if(current(request,user)){detailBusy.value=false;phase.value=''}}
}
async function adoptRecovery(id:string){
  const operation=unknown.value;if(!operation||busy.value||!recovery.value.some(row=>row.id===id))return
  const request=++detailSerial,user=account.value;detailBusy.value=true;phase.value='正在载入所选精确版本…';error.value=''
  try{
    const row=exact(await dataStructures.get(id),id);if(!current(request,user))return
    if(operation.id&&(id!==operation.id||row.structure_key!==operation.family||row.version!==operation.version)||operation.kind==='create'&&(operation.knownIds.includes(id)||operation.source&&(row.structure_key!==operation.source.structure_key||row.version<=operation.source.version)))throw Error('此记录不是原目标或在已知新建基线之前，不能解除未知状态。')
    if(operation.kind==='publish'&&row.status!=='published'||operation.kind==='retire'&&row.status!=='retired'||operation.kind==='update'&&Object.entries(operation.attempted).some(([key,value])=>!sameStructureValue(key,row[key as keyof DataStructureContent],value)))throw Error('回读尚未体现本次改动或目标状态，原请求是否完成仍未知；继续保留输入和提交锁定。')
    apply(row);notice.value='已明确载入此现存版本，替换原本地输入；未重发写入，也不认定它一定由上次请求创建。'
  }catch(cause){if(current(request,user))error.value=apiErrorSummary(cause,'载入失败，结果仍未知')}
  finally{if(current(request,user)){detailBusy.value=false;phase.value=''}}
}
async function checkSchema(){
  if(!selected.value||changed.value||locked.value)return
  const request=++detailSerial,user=account.value;detailBusy.value=true;phase.value='正在检查已保存结构…';error.value='';schemaReport.value=null
  try{const result=await dataStructures.schema(selected.value.id);if(current(request,user))schemaReport.value=result}
  catch(cause){if(current(request,user))error.value=apiErrorSummary(cause,'结构校验失败')}
  finally{if(current(request,user)){detailBusy.value=false;phase.value=''}}
}
watch(()=>props.initialId,id=>{if(id)void open(id)},{immediate:true})
void load()
</script>

<template>
  <BaseModal open configuration title="输入／输出结构" width="1100px" :dismissible="!busy" @close="close">
    <section class="structure-manager" :aria-busy="busy">
      <ConfigurationDiscardNotice v-if="leavePrompt" :message="leavePrompt" :busy="busy" @keep="keepEditing" @discard="discardAndLeave" />
      <RegistryResourceExplorer :key="account" label="输入／输出结构" :inert="Boolean(leavePrompt)" v-slot="{inspect}">
        <p>字段定义说明值的含义；结构组织这些字段。内容来源、提示词策略或业务输出引用结构版本，具体值仍由项目和本次任务提供。</p>
        <form class="toolbar" @submit.prevent="page=1;load()"><label>搜索结构<input v-model="search" /></label><label>状态<select v-model="status"><option value="">全部状态</option><option v-for="item in options?.statuses" :key="item.value" :value="item.value">{{item.label}}</option></select></label><button class="button ghost" :disabled="loading">查询／刷新</button><button type="button" class="button primary" :disabled="locked||!catalogReady" @click="begin()">新建结构草稿</button></form>
        <p v-if="loading" role="status">正在读取结构目录…</p><p v-if="listError" role="alert">{{listError}}（列表为上次结果）</p>
        <div class="table-wrap"><table><thead><tr><th>结构</th><th>版本／状态</th><th>操作</th></tr></thead><tbody><tr v-for="row in rows" :key="row.id"><td>{{row.name||'未命名草稿'}}</td><td>v{{row.version}} · {{statusLabel(row.status)}}</td><td><button class="button ghost" :disabled="locked" @click="open(row.id)">查看／编辑</button></td></tr></tbody></table></div>
        <p v-if="!loading&&!listError&&!rows.length">当前条件没有结构版本。</p>
        <nav class="toolbar" aria-label="结构版本分页"><button class="button ghost" :disabled="loading||!hasPrevious" @click="page--;load()">上一页</button><span>第 {{page}} 页 · 共 {{count}} 项</span><button class="button ghost" :disabled="loading||!hasNext" @click="page++;load()">下一页</button></nav>
        <p v-if="busy" role="status">{{phase}}</p><p v-if="error" role="alert">{{error}}</p><p v-if="notice" role="status">{{notice}}</p>
        <section v-if="unknown" class="recovery" aria-label="写入结果核对"><h3>先核对写入结果</h3><p>只读取现存版本。{{unknown.source?'后继核对限定原结构家族。':'新建结构的身份由服务器分配，列表不能按名称自动认定成功。'}} 空结果也不证明失败；本窗口内不会重复提交。</p><button class="button ghost" :disabled="busy" @click="readback()">只读核对现存版本</button><p v-if="recoveryChecked&&!recovery.length">没有返回记录，结果仍未知。</p><ul><li v-for="row in recovery" :key="row.id"><span>{{row.name}} · v{{row.version}} · {{statusLabel(row.status)}}<small>{{row.structure_key}} / {{row.id}}</small></span><button class="button ghost" :disabled="busy" @click="adoptRecovery(row.id)">载入此精确版本</button></li></ul><nav v-if="recoveryChecked" class="toolbar" aria-label="核对结果分页"><button class="button ghost" :disabled="busy||!recoveryPrevious" @click="readback(recoveryPage-1)">核对上一页</button><span>第 {{recoveryPage}} 页</span><button class="button ghost" :disabled="busy||!recoveryNext" @click="readback(recoveryPage+1)">核对下一页</button></nav></section>
        <section v-if="editing" class="editor">
          <header><h2>{{selected?selected.name+' · v'+selected.version:'新建结构草稿'}}</h2><p v-if="predecessor">基于 {{predecessor.name}} · v{{predecessor.version}}。复制此精确版本；新版本号由服务器按家族分配，不改写来源。</p><button v-if="selected" class="button ghost" :disabled="locked" @click="inspect('structure',selected.id,$event)">查看此结构与使用关系</button></header>
          <DataStructureDefinitionView v-if="readonly&&selected" :definition="selected" />
          <form v-else @submit.prevent="save">
            <ConfigurationAuthoringShell label="结构配置" :sections="sections" :busy="busy" :issues="issues" :status="selected?statusLabel(selected.status)+(changed?' · 有未保存修改':' · 已保存'):'尚未保存'">
              <template #meaning><fieldset :disabled="locked"><label data-authoring-field="name">结构名称<input v-model="form.name" /></label><label data-authoring-field="description">用途说明<textarea v-model="form.description" rows="2" /></label><p v-if="selected">结构家族：{{selected.structure_key}} · 此版本 v{{selected.version}}</p></fieldset></template>
              <template #nodes>
                <div data-authoring-field="nodes" class="nodes-section">
                  <div class="toolbar"><button type="button" class="button ghost" :disabled="locked||!catalogReady||Boolean(draftErrors.length)" @click="add">添加节点</button><span>{{form.nodes.length}} 个节点 · 根／属性／列表元素显式设置</span></div>
                  <div class="table-wrap"><table><thead><tr><th>节点／父节点</th><th>属性名／顺序</th><th>字段定义与操作</th></tr></thead><tbody><tr v-for="(row,index) in form.nodes" :key="index"><td>{{row.node_key}}<small>父：{{row.parent_key??'根'}}</small></td><td>{{row.property_name||'未设置'}}<small>同层顺序 {{row.position}}</small></td><td><button :data-authoring-field="'nodes.'+row.node_key" type="button" class="button ghost" :disabled="locked||Boolean(draftErrors.length)" @click="nodeIndex=index">配置节点 {{row.node_key}}</button><button v-if="row.field_definition_version" type="button" class="button ghost" :disabled="busy" @click="inspect('field_version',row.field_definition_version,$event)">查看字段定义</button><button type="button" class="button danger" :disabled="locked||Boolean(draftErrors.length)" @click="removeIndex=index">移除节点</button></td></tr></tbody></table></div>
                  <DataStructureNodeEditor v-if="node&&options" :key="nodeIndex" :node="node" :nodes="form.nodes" :options="options" :disabled="locked" @change="updateNode" @draft-validity="draftErrors=$event" />
                  <p v-if="draftErrors.length" role="alert">有尚未完成的输入，请先修正：{{draftErrors.join('；')}}</p>
                  <section v-if="removeIndex>=0" class="recovery" aria-label="移除节点确认"><p>只从本地草稿移除 {{form.nodes[removeIndex]?.node_key}}；保存后写入，不删除字段定义或已有版本。有子节点时不会隐式删除子树。</p><div class="toolbar"><button type="button" class="button ghost" @click="removeIndex=-1">保留节点</button><button type="button" class="button danger" :disabled="locked" @click="removeNode">确认移除此节点</button></div></section>
                </div>
              </template>
              <template #review><p>公共字段版本 → 本结构的层级与使用规则 → 来源／策略／业务输出 → 本次任务的实际值。结构本身不调用模型。</p><p>当前本地节点 {{form.nodes.length}} 项。草稿可以未完成；启用时由服务器完整校验，不按名称补齐字段或接线。</p><p v-if="changed">有未保存修改；先保存再检查或试填，避免用旧版本结果代表当前草稿。</p></template>
              <template #actions><button class="button primary" :disabled="locked||!catalogReady||Boolean(draftErrors.length)">保存草稿</button><button v-if="selected?.status==='draft'" type="button" class="button ghost" :disabled="locked||!catalogReady||Boolean(draftErrors.length)" @click="transition('publish')">保存并启用结构</button></template>
            </ConfigurationAuthoringShell>
          </form>
          <div class="toolbar"><button v-if="selected" class="button ghost" :disabled="locked||!catalogReady" @click="begin(true)">以此版本创建草稿</button><button v-if="selected?.status==='published'" class="button ghost" :disabled="locked" @click="transition('retire')">停用此版本（保留已有引用）</button></div>
          <div v-if="selected&&!changed" class="toolbar"><button class="button ghost" :disabled="locked" @click="checkSchema">检查已保存结构</button><button class="button ghost" :disabled="locked" @click="previewOpen=!previewOpen"> {{previewOpen?'收起':'试填'}}结构样例</button></div>
          <section v-if="schemaReport"><p role="status">{{schemaReport.valid?'结构可解析，不代表已启用或任务成功':'结构存在配置错误'}}</p><p v-for="(messages,path) in schemaReport.errors" :key="path" role="alert">{{path}}：{{messages.join('；')}}</p></section>
          <DataStructureValuePreview v-if="selected&&previewOpen&&!changed" :key="selected.id" :structure-version-id="selected.id" />
        </section>
      </RegistryResourceExplorer>
    </section>
  </BaseModal>
</template>

<style scoped>
.structure-manager,.structure-manager :deep(.resource-owner),.editor,.editor header,.editor form,.nodes-section{display:grid;gap:var(--config-space-lg);min-width:0}.structure-manager{font:400 var(--config-body-size)/1.6 var(--font)}.structure-manager p,.structure-manager h2,.structure-manager h3{margin:0;overflow-wrap:anywhere}.structure-manager p{color:var(--text-secondary)}.structure-manager h2{font-size:var(--config-title-size)}.structure-manager h3{font-size:var(--config-section-size)}.toolbar{display:flex!important;gap:var(--config-space-sm);align-items:end;flex-wrap:wrap}.structure-manager label{display:grid;gap:var(--config-space-xs);min-width:0}.structure-manager input,.structure-manager select,.structure-manager textarea{box-sizing:border-box;min-width:0;width:100%;min-height:44px;font:inherit;background:var(--surface);color:var(--text);border:1px solid var(--border-strong);border-radius:var(--config-radius);padding:var(--config-space-sm)}.structure-manager fieldset{display:grid;gap:var(--config-space-md);border:0;margin:0;padding:0;min-width:0}.table-wrap{overflow:auto;max-width:100%}table{border-collapse:collapse;width:100%;text-align:left}th,td{padding:var(--config-space-sm);border-bottom:1px solid var(--border);overflow-wrap:anywhere}td button{margin:var(--config-space-xs)}.structure-manager button{min-height:44px;white-space:normal;overflow-wrap:anywhere}.structure-manager small{display:block;font-size:var(--config-help-size);overflow-wrap:anywhere}.structure-manager [role=alert]{color:var(--red)}.recovery{border:1px solid var(--border);padding:var(--config-space-md);display:grid;gap:var(--config-space-md)}.recovery ul{list-style:none;padding:0;margin:0}.recovery li{display:flex;gap:var(--config-space-sm);flex-wrap:wrap}.recovery span{flex:1;min-width:0;overflow-wrap:anywhere}@media(max-width:640px){.toolbar>label{width:100%}.table-wrap table{min-width:360px}}
</style>
