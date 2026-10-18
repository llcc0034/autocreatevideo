<script setup lang="ts">
import {computed,onBeforeUnmount,onMounted,reactive,ref,watch} from 'vue'
import {onBeforeRouteLeave,onBeforeRouteUpdate} from 'vue-router'
import BaseModal from './BaseModal.vue'
import ConfigurationAuthoringShell from './ConfigurationAuthoringShell.vue'
import ConfigurationDiscardNotice from './ConfigurationDiscardNotice.vue'
import RegistryResourceExplorer from './RegistryResourceExplorer.vue'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
import BusinessOutputDefinitionView from './BusinessOutputDefinitionView.vue'
import {useAuthStore} from '@/stores/auth'
import {authoringIssues,isExplicitAuthoringRejection,type AuthoringIssue} from '@/services/configurationAuthoring'
import {outputEditableContent,outputContentMatches,outputWriteMatches} from '@/services/businessOutputAuthoring'
import ModelVisualRulesOutputEditor from './ModelVisualRulesOutputEditor.vue'
import OutputAllowedValuesEditor from './OutputAllowedValuesEditor.vue'
import DataStructureVersionSelect from './DataStructureVersionSelect.vue'
import StructuredOutputPreview from './StructuredOutputPreview.vue'
import ScriptSemanticOutputEditor from './ScriptSemanticOutputEditor.vue'
import {outputAllowedValueErrors,type OutputAllowedValueField} from '@/services/outputAllowedValues'
import {outputFieldSources,outputFieldSourceErrors,draftOutputSourceOptions,outputResponseKeyErrors,type OutputResponseKeyPolicy,type OutputFieldSourceOption} from '@/services/outputFieldSources'
import RegistryRelationSummary from './RegistryRelationSummary.vue'
import {useRegistryRelations} from '@/services/registryRelations'
import {businessOutputs,outputVersionOptions,type BusinessOutputVersion,type OutputParserOption} from '@/services/businessOutputs'
import {apiErrorSummary} from '@/services/apiErrorPresentation'
import type {JsonObject,VisualRulesOutputOptions} from '@/services/platformTypes'
const auth=useAuthStore()
const sections=[
  {key:'identity',title:'用途与协议',description:'为公共输出定义命名，选择平台已实现的解析协议。'},
  {key:'structure',title:'响应结构',description:'引用精确结构版本，字段的类型与组织方式在公共结构中维护。'},
  {key:'rules',title:'解析与处理规则',description:'只配置当前协议支持的转换、语义映射或质量建议，不重复定义字段。'},
  {key:'review',title:'复核与使用',description:'保存草稿后按精确合同预检；发布和生成是不同操作。'},
]
const issues=ref<AuthoringIssue[]>([]),phase=ref(''),catalogReady=ref(false),hasNext=ref(false),hasPrevious=ref(false)
const predecessor=ref<BusinessOutputVersion|null>(null)
const props=defineProps<{choose?:boolean;initialId?:string;supportedParserKeys?:string[]}>()
const emit=defineEmits<{close:[];select:[value:BusinessOutputVersion]}>()
const rows=ref<BusinessOutputVersion[]>([]),selected=ref<BusinessOutputVersion|null>(null)
const options=ref<VisualRulesOutputOptions|null>(null),parser=ref<JsonObject|null>(null)
const parsers=ref<OutputParserOption[]>([])
const frozenOptions=ref<VisualRulesOutputOptions|null>(null)
const allowedValueFields=ref<OutputAllowedValueField[]>([])
const fieldSourceOptions=ref<OutputFieldSourceOption[]>([])
const responseKeyPolicy=ref<OutputResponseKeyPolicy|null>(null)
const editorOptions=computed(()=>{const base=selected.value?frozenOptions.value:options.value;return base&&!sourceErrors.value.length&&!keyErrors.value.length?draftOutputSourceOptions(base,form.field_sources,fieldSourceOptions.value,form.response_keys):base})
const page=ref(1),count=ref(0),status=ref(props.choose?'published':''),search=ref('')
const busy=ref(false),error=ref(''),notice=ref(''),editing=ref(false),confirmed=ref(false),errors=ref<string[]>([])
const form=reactive({name:'',description:'',parser_key:'',data_structure_version:'',quality_advisories:undefined as JsonObject|undefined,allowed_values:{} as JsonObject,field_sources:{} as JsonObject,response_keys:{} as JsonObject,semantic_mapping:{} as JsonObject})
const structured=computed(()=>form.parser_key==='structured_json.v1')
const semantic=computed(()=>form.parser_key==='script_semantics.v1')
const visual=computed(()=>form.parser_key==='visual_rules.v2')
const semanticErrors=ref<string[]>([])
const structureChange=ref<string|null>(null)
function changeStructure(id:string){if(busy.value||pending.value||readonly.value||structureChange.value!==null)return;if(id===form.data_structure_version)return;if(semantic.value&&semanticErrors.value.length){structureChange.value=id;return}form.data_structure_version=id}
function confirmStructureChange(){if(busy.value||pending.value||structureChange.value===null)return;form.data_structure_version=structureChange.value;structureChange.value=null}
const knownParser=computed(()=>parsers.value.some(option=>option.key===form.parser_key))
const allowedErrors=computed(()=>!visual.value?[]:outputAllowedValueErrors(form.allowed_values,allowedValueFields.value))
const sourceErrors=computed(()=>!visual.value?[]:outputFieldSourceErrors(form.field_sources,fieldSourceOptions.value,form.allowed_values,form.quality_advisories))
const keyErrors=computed(()=>!visual.value?[]:outputResponseKeyErrors(form.response_keys,form.field_sources,responseKeyPolicy.value))
const supported=(row:BusinessOutputVersion)=>!props.supportedParserKeys||props.supportedParserKeys.includes(row.parser_key)
watch(()=>form.parser_key,()=>{errors.value=[]},{flush:'sync'})
function changeResponseKey(field:string,event:Event){form.response_keys={...form.field_sources,...form.response_keys,[field]:(event.target as HTMLInputElement).value}}
function toggleOutput(row:OutputFieldSourceOption,event:Event){
  if(!row.optional)return
  if((event.target as HTMLInputElement).checked){
    form.field_sources[row.field]=row.choices[0]!.response_field
    if(Object.keys(form.response_keys).length)form.response_keys[row.field]=row.choices[0]!.response_field
  }else{delete form.field_sources[row.field];delete form.response_keys[row.field]}
}
const readonly=computed(()=>!!selected.value&&selected.value.status!=='draft')
const payload=()=>({name:form.name,description:form.description,parser_key:form.parser_key,data_structure_version:form.data_structure_version,quality_advisories:visual.value?form.quality_advisories||{}:{},...(visual.value?{allowed_values:form.allowed_values,field_sources:form.field_sources,response_keys:form.response_keys}:{}),...(semantic.value?{semantic_mapping:form.semantic_mapping}:{})})
const dirty=computed(()=>!selected.value||!outputContentMatches(selected.value,payload()))
const relations=useRegistryRelations('output',()=>selected.value?[{id:selected.value.id,updatedAt:String(selected.value.revision)}]:[])
const unsaved=computed(()=>editing.value&&!readonly.value&&(dirty.value||errors.value.length>0||semanticErrors.value.length>0))
const leaveOpen=ref(false)
let resolvePendingLeave:((allow:boolean)=>void)|undefined
const names:Record<string,string>={draft:'草稿',published:'已发布',retired:'已退役'}
let epoch=0,disposed=false
async function canLeave(){if(busy.value||leaveOpen.value||pending.value||structureChange.value!==null)return false;if(!unsaved.value)return true;leaveOpen.value=true;return new Promise<boolean>(resolve=>{resolvePendingLeave=resolve})}
function confirmLeave(allow:boolean){if(busy.value)return;if(allow)editing.value=false;leaveOpen.value=false;const resolve=resolvePendingLeave;resolvePendingLeave=undefined;resolve?.(allow)}
async function close(){if(await canLeave())emit('close')}
async function selectVersion(row:BusinessOutputVersion){
  if(!await canLeave())return
  const serial=++epoch;busy.value=true;error.value=''
  try{const detail=await businessOutputs.detail(row.id);if(serial!==epoch)return
    if(detail.id!==row.id||detail.status!=='published'||!detail.data_structure_version||!supported(detail))throw new Error('输出版本缺少有效结构、已不可选或当前执行入口不支持，请重新读取')
    emit('select',detail)
  }catch(cause){if(serial===epoch)error.value=apiErrorSummary(cause)}finally{if(serial===epoch)busy.value=false}
}
function beforeUnload(event:BeforeUnloadEvent){if(busy.value||unsaved.value||pending.value){event.preventDefault();event.returnValue=''}}
onBeforeRouteLeave(canLeave)
onBeforeRouteUpdate(canLeave)
onBeforeUnmount(()=>{epoch++;disposed=true;resolvePendingLeave?.(false);window.removeEventListener('beforeunload',beforeUnload)})
async function load(){if(pending.value)return;const serial=++epoch;busy.value=true;catalogReady.value=false;phase.value='正在读取输出目录…';error.value='';try{
  const [list,metadata]=await Promise.all([businessOutputs.list(page.value,status.value,search.value),businessOutputs.options()])
  if(serial!==epoch)return
  rows.value=list.results;count.value=list.count;hasNext.value=list.hasNext;hasPrevious.value=list.hasPrevious;catalogReady.value=true;options.value=metadata.editor;parser.value=metadata.parser;parsers.value=metadata.parsers;allowedValueFields.value=metadata.allowedValueFields;fieldSourceOptions.value=metadata.fieldSourceOptions;responseKeyPolicy.value=metadata.responseKeyPolicy
}catch(cause){if(serial===epoch)error.value=apiErrorSummary(cause,'输出版本读取失败')}finally{if(serial===epoch){busy.value=false;phase.value=''}}}
function assign(row:BusinessOutputVersion|null){const frozen=row&&row.parser_key==='visual_rules.v2'&&options.value?outputVersionOptions(options.value,row):null;frozenOptions.value=frozen;selected.value=row;editing.value=true;confirmed.value=false;notice.value='';errors.value=[];semanticErrors.value=[];issues.value=[];Object.assign(form,{name:row?.name||'',description:row?.description||'',parser_key:row?.parser_key||'',data_structure_version:row?.data_structure_version||'',quality_advisories:row?.quality_advisories&&Object.keys(row.quality_advisories).length?structuredClone(row.quality_advisories):undefined,allowed_values:structuredClone(row?.parser_contract?.allowed_values||{}),field_sources:row?(row.parser_key==='visual_rules.v2'?outputFieldSources(row.parser_contract):{}):parser.value?outputFieldSources(parser.value):{},response_keys:structuredClone(row?.parser_contract?.response_keys||{}),semantic_mapping:structuredClone(row?.parser_contract?.mapping||{})})}
async function edit(row:BusinessOutputVersion|null){if(!await canLeave())return;if(!row){if(!catalogReady.value)return;predecessor.value=null;assign(null);return}const serial=++epoch;busy.value=true;error.value='';try{const detail=await businessOutputs.detail(row.id);if(serial!==epoch)return;if(detail.id!==row.id)throw new Error('返回的输出版本与目标不一致，请重新读取');predecessor.value=null;assign(detail)}catch(cause){if(serial===epoch)error.value=apiErrorSummary(cause)}finally{if(serial===epoch)busy.value=false}}
async function followContext(){if(props.initialId&&!disposed)await edit({id:props.initialId} as BusinessOutputVersion)}
type Pending={action:'save'|'publish'|'retire'|'successor';row:BusinessOutputVersion|null;attempt:ReturnType<typeof payload>;known:string[]}
const pending=ref<Pending|null>(null),recoveryRows=ref<BusinessOutputVersion[]>([]),recoveryPage=ref(1),recoveryNext=ref(false),recoveryPrevious=ref(false),recoveryBusy=ref(false)
function validReceipt(row:BusinessOutputVersion,source:BusinessOutputVersion|null,action:Pending['action']){
  if(action==='successor')return !!source&&row.id!==source.id&&row.output_key===source.output_key&&row.parser_key===source.parser_key&&row.version===source.version+1&&row.revision===1&&row.status==='draft'&&outputContentMatches(row,outputEditableContent(source))
  if(source&&(row.id!==source.id||row.output_key!==source.output_key||row.parser_key!==source.parser_key||row.version!==source.version||row.revision!==source.revision+1))return false
  if(!source&&(row.version!==1||row.revision!==1))return false
  if(source&&action!=='save'&&(row.contract_sha256!==source.contract_sha256||!outputContentMatches(row,outputEditableContent(source))))return false
  return row.status===(action==='publish'?'published':action==='retire'?'retired':'draft')
}
function failure(cause:unknown,operation:Pending){
  issues.value=authoringIssues(cause,{name:'identity',description:'identity',parser_key:'identity',data_structure_version:'structure',quality_advisories:'rules',allowed_values:'rules',field_sources:'rules',response_keys:'rules',semantic_mapping:'rules',expected_revision:'review'})
  error.value=apiErrorSummary(cause,'请求结果未知，已填内容保留；先只读核对，不要重复提交')
  if(!isExplicitAuthoringRejection(cause)||(cause as any)?.response?.status===409){pending.value=operation;recoveryRows.value=[];recoveryPage.value=1}
}
async function save(){
  if(busy.value||pending.value||structureChange.value!==null||!catalogReady.value||readonly.value||!knownParser.value||!form.name.trim()||!form.data_structure_version||errors.value.length||allowedErrors.value.length||sourceErrors.value.length||keyErrors.value.length||semantic.value&&semanticErrors.value.length||!dirty.value)return
  busy.value=true;phase.value='正在保存输出草稿…';error.value='';notice.value='';issues.value=[]
  const serial=++epoch,operation:Pending={action:'save',row:selected.value?JSON.parse(JSON.stringify(selected.value)):null,attempt:JSON.parse(JSON.stringify(payload())),known:rows.value.map(row=>row.id)}
  let submitted=false
  try{if(!operation.row){phase.value='正在读取新建前身份基线…';const known=new Set<string>();let p=1
      while(true){const list=await businessOutputs.list(p++,'','');if(serial!==epoch)return;const before=known.size;list.results.forEach(row=>known.add(row.id));if(!list.hasNext)break;if(known.size===before)throw new Error('分页未推进；未提交新建请求')}
      operation.known=[...known];phase.value='正在保存输出草稿…'
    }
    submitted=true;const row=await businessOutputs.save(operation.attempt,operation.row);if(serial!==epoch)return
    if(!validReceipt(row,operation.row,'save')||!outputWriteMatches(row,operation.attempt)||!operation.row&&operation.known.includes(row.id))throw new Error('保存回执身份、内容或修订不匹配；先只读核对，不重复提交')
    assign(row);notice.value='输出草稿已保存，未发布、未调用模型。';await load()
  }catch(cause){if(serial===epoch){if(submitted)failure(cause,operation);else error.value=apiErrorSummary(cause,'新建前身份读取失败；尚未提交，原输入保留')}}finally{if(serial===epoch){busy.value=false;phase.value=''}}
}
async function act(action:'publish'|'successor'|'retire'){
  if(structureChange.value!==null||action==='publish'&&(errors.value.length||allowedErrors.value.length||sourceErrors.value.length||keyErrors.value.length||semantic.value&&semanticErrors.value.length))return
  if(busy.value||pending.value||!catalogReady.value||!selected.value||action!=='successor'&&(!confirmed.value||dirty.value))return
  busy.value=true;phase.value=action==='successor'?'正在从此精确版本创建后继…':action==='publish'?'正在发布输出版本…':'正在退役输出版本…';error.value='';notice.value='';issues.value=[]
  const serial=++epoch,source=JSON.parse(JSON.stringify(selected.value)) as BusinessOutputVersion,operation:Pending={action,row:source,attempt:JSON.parse(JSON.stringify(payload())),known:rows.value.map(row=>row.id)}
  try{const row=await businessOutputs.action(source,action);if(serial!==epoch)return
    if(!validReceipt(row,source,action))throw new Error('状态回执与原目标不匹配；先只读核对，不重复提交')
    if(action==='successor')predecessor.value=source
    assign(row);notice.value=action==='successor'?'后继草稿已建立；来源与使用方均未改写。':'输出版本状态已更新；已有执行请求和审核事实未改变。';await load()
  }catch(cause){if(serial===epoch)failure(cause,operation)}finally{if(serial===epoch){busy.value=false;phase.value=''}}
}
async function recover(){
  const operation=pending.value;if(!operation||recoveryBusy.value)return
  recoveryBusy.value=true;error.value='';const serial=++epoch
  try{if(operation.row&&operation.action!=='successor'){
      const row=await businessOutputs.detail(operation.row.id);if(serial!==epoch)return
      if(!validReceipt(row,operation.row,operation.action)||operation.action==='save'&&!outputWriteMatches(row,operation.attempt))throw new Error('原目标尚未出现匹配的内容、新修订或状态；保持结果未知，不重发')
      recoveryRows.value=[row];recoveryNext.value=false;recoveryPrevious.value=false
    }else{
      const result=await businessOutputs.list(recoveryPage.value,'','',operation.action==='successor'?operation.row?.output_key:undefined);if(serial!==epoch)return
      recoveryRows.value=result.results.filter(row=>operation.action!=='successor'||row.output_key===operation.row?.output_key)
      recoveryNext.value=result.hasNext;recoveryPrevious.value=result.hasPrevious
    }
  }catch(cause){if(serial===epoch)error.value=apiErrorSummary(cause,'只读核对失败，未重发请求')}finally{if(serial===epoch)recoveryBusy.value=false}
}
async function loadRecovered(candidate:BusinessOutputVersion){
  const operation=pending.value;if(!operation||recoveryBusy.value)return
  recoveryBusy.value=true;error.value='';const serial=++epoch
  try{const row=await businessOutputs.detail(candidate.id);if(serial!==epoch)return
    if(row.id!==candidate.id||!validReceipt(row,operation.row,operation.action)||operation.action==='save'&&!outputWriteMatches(row,operation.attempt)||!operation.row&&operation.known.includes(row.id))throw new Error('目标身份、内容或修订不符合当前核对；原输入仍保留')
    if(operation.action==='successor')predecessor.value=operation.row
    assign(row);pending.value=null;notice.value='已显式载入核对的现存版本；不证明原请求的执行结果。';recoveryRows.value=[]
  }catch(cause){if(serial===epoch)error.value=apiErrorSummary(cause)}finally{if(serial===epoch)recoveryBusy.value=false}
}
watch(()=>auth.user?.id,()=>{epoch++;busy.value=false;recoveryBusy.value=false;pending.value=null;editing.value=false;selected.value=null;predecessor.value=null;catalogReady.value=false;resolvePendingLeave?.(false);resolvePendingLeave=undefined;leaveOpen.value=false;error.value='账号已变化，请重新读取输出目录'})
watch(()=>props.initialId,()=>{void followContext()})
onMounted(async()=>{window.addEventListener('beforeunload',beforeUnload);await load();await followContext()})
</script>
<template>
  <BaseModal open title="业务输出版本" width="1080px" configuration :dismissible="!busy&&!recoveryBusy" @close="close">
    <RegistryResourceExplorer v-slot="{inspect}" label="业务输出版本">
    <div class="output-manager" :aria-busy="busy||recoveryBusy">
      <ConfigurationDiscardNotice v-if="leaveOpen" :busy="busy" message="尚未保存的名称、协议、映射及建议将丢失；已保存版本和任务不受影响。" @keep="confirmLeave(false)" @discard="confirmLeave(true)"/>
      <p>响应结构 → 本版本解析与约束 → 执行实例与下游使用。定义可复用，任务具体结果另查任务记录。</p>
      <p v-if="error" role="alert">{{error}}<button v-if="initialId&&!pending" :disabled="busy" @click="followContext">重新读取指定输出版本</button></p>
      <p v-if="notice" role="status">{{notice}}</p><p v-if="busy" role="status">{{phase||'正在读取精确版本…'}}</p>
      <section v-if="pending" class="recovery" aria-label="输出请求结果核对">
        <h3>请求结果待核对</h3><p>没有重新提交。原目标：{{pending.row?pending.row.name+' · v'+pending.row.version:'新输出定义'}}。按精确身份只读回读后显式载入；同名或空列表都不代表原请求成功或失败。</p>
        <button :disabled="recoveryBusy" @click="recover">{{recoveryBusy?'正在只读核对…':'只读核对原请求'}}</button>
        <ul><li v-for="row in recoveryRows" :key="row.id">{{row.name}} · v{{row.version}} · {{names[row.status]}}<small>{{row.id}}</small><button :disabled="recoveryBusy" @click="loadRecovered(row)">载入此核对版本</button></li></ul>
        <p v-if="!recoveryRows.length">尚无可载入的核对记录，仍不重发。</p>
        <nav aria-label="核对记录分页"><button :disabled="recoveryBusy||!recoveryPrevious" @click="recoveryPage--;recover()">上一页核对记录</button><span>第{{recoveryPage}}页</span><button :disabled="recoveryBusy||!recoveryNext" @click="recoveryPage++;recover()">下一页核对记录</button></nav>
      </section>
      <ConfigurationDiscardNotice v-if="structureChange!==null" title="更换响应结构前请确认" message="当前语义映射有未完成输入或尚未通过目录检查。更换结构将重新读取字段目录，未完成的局部输入会丢失；已形成的映射原值和样例正文保留，不会自动迁移成新结构。" action-label="放弃局部输入并更换结构" :busy="busy||!!pending" @keep="structureChange=null" @discard="confirmStructureChange"/>
      <fieldset class="workspace" :disabled="busy||!!pending||recoveryBusy||leaveOpen||structureChange!==null">
      <form class="toolbar" @submit.prevent="page=1;load()"><label>搜索名称<input v-model="search"/></label><label>状态<select v-model="status"><option value="">全部</option><option v-for="(label,value) in names" :key="value" :value="value">{{label}}</option></select></label><button>查询／刷新</button><button type="button" :disabled="!catalogReady" @click="edit(null)">新建输出定义</button></form>
      <p v-if="!rows.length&&!error&&!busy">当前条件没有输出版本。</p>
      <ul class="outputs"><li v-for="row in rows" :key="row.id"><div><strong>{{row.name}} · v{{row.version}}</strong><small>{{names[row.status]}} · {{row.parser_key}}</small><small v-if="choose&&!supported(row)">当前执行入口尚不支持此协议</small></div><button @click="edit(row)">查看／配置</button><button v-if="choose&&row.status==='published'" :disabled="!supported(row)" @click="selectVersion(row)">使用此精确版本</button></li></ul>
      <nav class="toolbar" aria-label="输出版本分页"><button :disabled="!hasPrevious" @click="page--;load()">上一页</button><span>第{{page}}页 · 共{{count}}项</span><button :disabled="!hasNext" @click="page++;load()">下一页</button></nav>
      <section v-if="editing" class="editor"><h2>{{selected?`${selected.name} · v${selected.version}`:'新建输出草稿'}}</h2>
        <p v-if="predecessor">后继来源：{{predecessor.name}} · v{{predecessor.version}} · {{predecessor.id}}；只创建独立草稿，不替换使用方。</p>
        <BusinessOutputDefinitionView v-if="readonly&&selected" :definition="selected"/>
        <ConfigurationAuthoringShell v-else label="业务输出配置" :sections="sections" :busy="busy" :issues="issues" :status="selected?(dirty?'有未保存修改':'草稿已保存，未发布'):'尚未保存'">
          <template #identity>        <fieldset :disabled="busy||readonly"><label data-authoring-field="name">业务名称<input v-model="form.name" maxlength="120" required/></label><label data-authoring-field="description">使用说明<textarea v-model="form.description" maxlength="1000" rows="2"/></label><label data-authoring-field="parser_key">输出协议<select v-model="form.parser_key" :disabled="!!selected" required><option value="">请选择平台已实现协议</option><option v-for="option in parsers" :key="option.key" :value="option.key">{{option.label}} · {{option.key}}</option></select></label></fieldset>
        <p>{{parsers.find(option=>option.key===form.parser_key)?.description}}</p>
        <p v-if="form.parser_key&&!knownParser" role="alert">当前协议 {{form.parser_key}} 不在可用目录中，请重新读取；不会改用其他协议。</p>
        <p v-if="structured">自定义字段、层级、类型和显式缺键默认值在共用结构维护。本入口用于配置、字段目录与解析预览；是否适用于某业务由执行实例的明确输出合同决定，不因保存此定义就创建Bible、镜头或发起模型生成。</p>
        <p v-else-if="visual">三项基础输出必选，还可启用目录支持的镜头指导。所选字段均需完整返回并逐项审核；可配置对象字段或固定数组位置。镜头指导只存入本轮审核结果供下游引用，不写回项目三项规则。</p>
</template>
          <template #structure><div data-authoring-field="data_structure_version">
            <DataStructureVersionSelect :model-value="form.data_structure_version" :disabled="busy" @update:model-value="changeStructure"/>
            <button v-if="form.data_structure_version" type="button" @click="inspect('structure',form.data_structure_version,$event)">查看所选结构与使用关系</button>
            <p>结构中的字段和层级只在公共结构维护；此处引用精确版本，不填本次业务值。</p>
          </div></template>
          <template #rules>        <fieldset v-if="visual" :disabled="busy||readonly" class="source-mapping" data-authoring-field="field_sources"><legend>模型响应来源 → 业务输出字段</legend>
          <p>从已实现的转换中选择；保存后请求结构、解析及下游审核字段遵循同一版本。不会猜测另一种返回格式。</p>
          <div v-for="row in fieldSourceOptions" :key="row.field">
            <label v-if="row.optional" class="confirm"><input type="checkbox" :checked="Object.prototype.hasOwnProperty.call(form.field_sources,row.field)" @change="toggleOutput(row,$event)"/>启用{{row.label}}</label>
            <template v-if="Object.prototype.hasOwnProperty.call(form.field_sources,row.field)">
              <label>{{row.label}} · 响应格式<select v-model="form.field_sources[row.field]" :disabled="row.choices.length===1"><option v-for="choice in row.choices" :key="choice.response_field" :value="choice.response_field">{{choice.response_field}} · {{choice.value_type}}</option></select></label>
              <label>{{row.label}} · 响应字段位置<input :value="form.response_keys[row.field]??form.field_sources[row.field]" :maxlength="responseKeyPolicy?.max_length" :disabled="!responseKeyPolicy" spellcheck="false" @input="changeResponseKey(row.field,$event)"/></label>
              <small><code>{{form.response_keys[row.field]??form.field_sources[row.field]}}</code> → <code>{{row.field}}</code> → 人工审核采用后可引用 <code>reviewed_visual_rules.{{row.field}}</code></small><small>{{editorOptions?.outputs.find(output=>output.field===row.field)?.transformation}}</small>
            </template>
          </div>
          <p>{{responseKeyPolicy?.description}} 例如 <code>rules.0.summary</code> 表示 rules 数组第1项的 summary 字段，<code>visual.palette</code> 表示对象字段，仅从本次响应取值。数组项顺序必须符合约定，不会按名称搜索或重排。修改位置不改变内容类型或审核目标；已有任务保持冻结合同，不接受旧位置作为别名。</p><button type="button" :disabled="!Object.keys(form.response_keys).length" @click="form.response_keys={}">使用所选格式的字段名</button>
        </fieldset>
        <ul v-if="keyErrors.length&&form.parser_key" role="alert"><li v-for="item in keyErrors" :key="item">{{item}}</li></ul>
        <ul v-if="sourceErrors.length&&form.parser_key" role="alert"><li v-for="item in sourceErrors" :key="item">{{item}}</li></ul>
        <div data-authoring-field="allowed_values"><OutputAllowedValuesEditor v-if="visual&&(form.field_sources.color_script!=='color_script'||Object.keys(form.allowed_values).length)" v-model="form.allowed_values" :fields="allowedValueFields" :disabled="busy||readonly"/></div>
        <div data-authoring-field="quality_advisories"><ModelVisualRulesOutputEditor v-if="visual" v-model="form.quality_advisories" :options="editorOptions" :field-sources="form.field_sources" :response-keys="form.response_keys" :allowed-values="form.allowed_values" :disabled="busy||readonly" :preview-disabled="busy||!!selected&&dirty||allowedErrors.length>0||sourceErrors.length>0||keyErrors.length>0" :output-version="selected&&!dirty?{id:selected.id,revision:selected.revision,sha256:selected.contract_sha256}:undefined" owner="output" @validity="errors=$event"/></div>
        <div data-authoring-field="semantic_mapping"><ScriptSemanticOutputEditor v-if="semantic" v-model="form.semantic_mapping" :structure-id="form.data_structure_version" :version="selected" :disabled="busy||readonly" :preview-disabled="busy||dirty" @validity="semanticErrors=$event"/></div>
        <StructuredOutputPreview v-if="structured&&selected" :version="selected" :disabled="busy||dirty"/>
        <p v-else-if="structured">保存输出草稿后，可按该精确合同解析样例并查看可引用字段。</p>
</template>
          <template #review>
            <p>公共结构 → {{form.parser_key||'待选择协议'}} → 执行实例显式引用 → 下游按原业务合同使用。保存和发布都不会调用模型。</p>
            <p v-if="dirty">有未保存内容；保存后再用该版本预检，不能以旧合同结果代表新配置。</p>
            <p v-if="!catalogReady" role="alert">输出协议目录尚不可用，请查询／刷新；原输入保留，不能改用默认协议。</p>
          </template>
          <template #actions><button class="button primary" :disabled="busy||!catalogReady||!form.name.trim()||!knownParser||!form.data_structure_version||errors.length>0||allowedErrors.length>0||sourceErrors.length>0||keyErrors.length>0||semantic&&semanticErrors.length>0||!dirty" @click="save">保存输出草稿（不调用模型）</button></template>
        </ConfigurationAuthoringShell>
        <template v-if="selected">
          <div class="toolbar"><button @click="inspect('output',selected.id,$event)">查看此输出与使用关系</button><button v-if="readonly&&selected.data_structure_version" @click="inspect('structure',selected.data_structure_version,$event)">查看所选结构与使用关系</button></div>
          <details><summary>精确版本与实现合同</summary><small>版本ID：{{selected.id}}<br/>合同摘要：{{selected.contract_sha256}}<br/>家族：{{selected.output_key}}</small><ReadonlyDataValue :value="selected.parser_contract" label="冻结解析合同"/></details>
          <details v-if="readonly"><summary>按此版本预检样例（不生成业务）</summary>
            <StructuredOutputPreview v-if="structured" :version="selected" :disabled="busy"/>
            <ScriptSemanticOutputEditor v-else-if="semantic" :model-value="form.semantic_mapping" :structure-id="form.data_structure_version" :version="selected" disabled hide-guide :preview-disabled="busy"/>
            <ModelVisualRulesOutputEditor v-else-if="visual" :model-value="form.quality_advisories" :options="frozenOptions" :field-sources="form.field_sources" :response-keys="form.response_keys" :allowed-values="form.allowed_values" disabled :preview-disabled="busy" :output-version="{id:selected.id,revision:selected.revision,sha256:selected.contract_sha256}" owner="output"/>
          </details>
          <section aria-label="输出版本引用与影响"><h3>引用与变更影响</h3><p>下列为已保存的精确配置引用；新版本不会自动替换使用方，不代表任务调用次数。</p><RegistryRelationSummary :row="relations.rows.value[selected.id]" :loading="relations.loading.value" :error="relations.error.value" @retry="relations.reload" condensed/></section>
          <label v-if="selected.status!=='retired'" class="confirm"><input v-model="confirmed" type="checkbox" :disabled="busy||dirty||errors.length>0||allowedErrors.length>0||sourceErrors.length>0||keyErrors.length>0||semantic&&semanticErrors.length>0"/>{{selected.status==='draft'?'我已保存并核对输出协议与质量建议；发布后只能新建后继版本':'确认退役：禁止新的执行组合引用；保留已有版本及历史事实'}}</label>
          <div class="toolbar"><button v-if="selected.status==='draft'" :disabled="busy||!catalogReady||!confirmed||dirty||errors.length>0||allowedErrors.length>0||sourceErrors.length>0||keyErrors.length>0||semantic&&semanticErrors.length>0" @click="act('publish')">发布输出版本</button><button v-else :disabled="busy||!catalogReady" @click="act('successor')">从此精确版本新建后继</button><button v-if="selected.status==='published'" :disabled="busy||!catalogReady||!confirmed" @click="act('retire')">退役输出版本</button></div>
        </template>
      </section>
      </fieldset>
    </div>
    </RegistryResourceExplorer>
  </BaseModal>
</template>
<style scoped>
.output-manager,.workspace,.editor{display:grid;gap:var(--config-space-lg);min-width:0}.output-manager{padding:var(--config-space-xl);font:var(--config-body-size)/1.6 var(--font);letter-spacing:0}.workspace,.editor fieldset{border:0;margin:0;padding:0}.editor{border-top:1px solid var(--border);padding-top:var(--config-space-lg)}.editor h2{font-size:var(--config-title-size);margin:0}.output-manager h3{font-size:var(--config-section-size);margin:0}.output-manager p,.output-manager small{color:var(--text-secondary);overflow-wrap:anywhere}.output-manager small{font-size:var(--config-help-size);display:block}.output-manager [role=alert]{color:var(--red)}.toolbar,.outputs li,.recovery nav{display:flex;gap:var(--config-space-sm);align-items:end;flex-wrap:wrap}.outputs{padding:0;list-style:none}.outputs li{padding:var(--config-space-md) 0;border-bottom:1px solid var(--border)}.outputs li div{flex:1;min-width:150px}.output-manager label,.editor fieldset,.source-mapping>div{display:grid;gap:var(--config-space-sm)}.output-manager input,.output-manager textarea,.output-manager select{box-sizing:border-box;min-width:0;max-width:100%;width:100%;padding:var(--config-space-sm);font:inherit;background:var(--bg-soft);color:var(--text);border:1px solid var(--border-strong);border-radius:var(--config-radius)}.output-manager button{min-height:var(--config-control-height);padding:var(--config-space-sm) var(--config-space-md);font:inherit;color:var(--text);background:var(--surface);border:1px solid var(--border-strong);border-radius:var(--config-radius);white-space:normal}.output-manager button:disabled{opacity:.45}.output-manager .button.primary{background:var(--accent);color:var(--bg)}.output-manager .confirm{display:flex;align-items:start;gap:var(--config-space-sm)}.confirm input{width:auto}.output-manager :focus-visible{outline:2px solid var(--accent);outline-offset:2px}.recovery{border:1px solid var(--accent);padding:var(--config-space-lg);overflow-wrap:anywhere}
@media(max-width:640px){.output-manager{--config-control-height:44px;padding:var(--config-space-md)}.toolbar label{width:100%}}
</style>
