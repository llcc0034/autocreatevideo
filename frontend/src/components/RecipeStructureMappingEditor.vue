<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import StructuredJsonEditor from './StructuredJsonEditor.vue'
import SchemaPathSelect from './SchemaPathSelect.vue'
import ShotFieldMappingSelect from './ShotFieldMappingSelect.vue'
import ConfigurationDiscardNotice from './ConfigurationDiscardNotice.vue'
import { useShotFieldCatalog, shotFieldSchema } from '@/services/shotFieldMapping'
import { mappingObject as obj, mappingHas, mappingHasDefault, initializeMapping, mappingSourceChoices, mappingOutputChoices, mappingSourceSchema, mappingValueSchema, mappingOutputIdentity, mappingSelectedOutput } from '@/services/recipeStructureMapping'
import { jsonChildSchemas, jsonPropertyOptions, jsonRules } from '@/services/structuredInputForm'
import type { JsonObject, FormCatalogOption, ProductionRecipeEditorOptions } from '@/services/platformTypes'
import type { ExecutionContract } from '@/services/executionRegistry'
import { initializePreviewMapping, previewMappingSchema, type MappingPreviewContext } from '@/services/structureMappingPreview'
import { pathType } from '@/services/structurePaths'

const props=defineProps<{modelValue:JsonObject;schema:JsonObject;label:string;nodes:FormCatalogOption[];
  fields:ExecutionContract['context_fields'];outputs:ProductionRecipeEditorOptions['textOutputOptions'];scopes:string[];priorStages:string[];itemSchema?:JsonObject;disabled?:boolean;defaultSchemas?:JsonObject[];sourceOnly?:boolean;
  previewContext?:MappingPreviewContext;allowedKinds?:string[]}>()
const emit=defineEmits<{'update:modelValue':[JsonObject];draftValidity:[string[]]}>()
const keyDraft=ref(''),error=ref('')
const kind=computed(()=>String(props.modelValue.kind||''))
const childDrafts=ref<Record<string,string[]>>({})
const pendingChange=ref<{message:string;actionLabel:string;value:JsonObject;snapshot:string}|null>(null)
const changeSnapshot=computed(()=>JSON.stringify([props.modelValue,props.schema,props.defaultSchemas,props.previewContext,props.itemSchema,props.nodes,props.allowedKinds,props.fields,props.outputs,props.scopes,props.priorStages,props.sourceOnly]))
const draftErrors=computed(()=>[...Object.values(childDrafts.value).flat(),...(pendingChange.value?[`${props.label}：请先确认映射变更或继续编辑原配置。`]:[])])
watch(()=>JSON.stringify(draftErrors.value),value=>emit('draftValidity',JSON.parse(value)),{immediate:true,flush:'sync'})
watch(changeSnapshot,()=>{
  if(pendingChange.value){pendingChange.value=null;error.value='映射或来源目录已变化，已取消待确认操作；请核对当前内容后重新选择。'}
},{flush:'sync'})
watch(()=>props.disabled,value=>{if(value)pendingChange.value=null},{flush:'sync'})
watch(kind,()=>{childDrafts.value={}})
const properties=computed(()=>Object.fromEntries(jsonPropertyOptions([props.schema]).map(key=>[key,jsonChildSchemas([props.schema],key,false)[0]||{}])))
const requiredKeys=computed(()=>jsonRules([props.schema],'object').flatMap(rule=>Array.isArray(rule.required)?rule.required:[]))
const allowAdditional=computed(()=>!props.previewContext&&jsonRules([props.schema],'object').every(rule=>rule.additionalProperties!==false))
const entries=computed(()=>Object.entries(obj(props.modelValue.fields)))
const omittedFields=computed(()=>Object.keys(properties.value).filter(key=>!mappingHas(obj(props.modelValue.fields),key)))
const hasDefault=(key:string)=>mappingHasDefault(props.defaultSchemas||[],key)
const childDefaults=(key:string,array=false)=>jsonChildSchemas(props.defaultSchemas||[],key,array)
const items=computed(()=>Array.isArray(props.modelValue.items)?props.modelValue.items.map(obj):[])
const choices=computed(()=>mappingSourceChoices(props.fields,props.schema,props.scopes,props.priorStages))
const outputChoices=computed(()=>mappingOutputChoices(props.outputs,props.schema,props.scopes,props.priorStages))
const shotCatalog=useShotFieldCatalog(()=>[props.modelValue])
const shotFieldsAllowed=computed(()=>!props.previewContext&&props.scopes.includes('shot')&&props.priorStages.includes('storyboard_review'))
const catalog=computed(()=>({fields:props.fields,outputs:outputChoices.value,itemSchema:props.itemSchema,shotFields:shotCatalog.states}))
const selectedOutput=computed(()=>mappingSelectedOutput(props.modelValue,outputChoices.value))
const sourceSchema=computed(()=>mappingSourceSchema(props.modelValue,catalog.value))
const previewValueSchema=(node:JsonObject)=>previewMappingSchema(node,props.previewContext?.schema||{},props.itemSchema)
const eachSchema=computed(()=>jsonChildSchemas([props.previewContext?previewValueSchema(obj(props.modelValue.source)):mappingValueSchema(obj(props.modelValue.source),catalog.value).schema],'0',true)[0])
const pathOrigin=computed(()=>props.modelValue.scope==='root'?props.previewContext?.schema:props.modelValue.scope==='item'?props.itemSchema:undefined)
const lookupItemSchema=computed(()=>jsonChildSchemas([previewValueSchema(obj(props.modelValue.collection))],'0',true)[0])
const nodeOptions=computed(()=>props.nodes.filter(row=>{
  if(props.allowedKinds&&!props.allowedKinds.includes(row.value))return false
  if(props.previewContext){
    if(props.sourceOnly&&!['path','lookup'].includes(row.value))return false
    const types:Record<string,string>={object:'object',array:'array',each:'array'}
    return !types[row.value]||types[row.value]===pathType(props.schema)
  }
  if(props.sourceOnly&&!['context_field','selected_output','item_field','shot_field'].includes(row.value))return false
  if(row.value==='shot_field')return shotFieldsAllowed.value
  if(row.value==='item_field') return props.itemSchema!==undefined
  if(row.value==='context_field') return choices.value.length>0
  if(row.value==='selected_output') return outputChoices.value.length>0
  if(row.value==='each') return (!props.schema.type||props.schema.type==='array')&&(shotFieldsAllowed.value||choices.value.some(row=>row.kind==='json')||outputChoices.value.some(row=>row.kind==='json')||props.itemSchema!==undefined)
  const types:Record<string,string>={object:'object',array:'array',unit_map:'object',unit_key:'string',reference_subjects:'array',material_difference:'string'}
  if(row.value==='unit_map'&&!allowAdditional.value) return false
  return !props.schema.type||!types[row.value]||types[row.value]===props.schema.type
}))
function commit(value:JsonObject){if(!props.disabled&&!pendingChange.value)emit('update:modelValue',value)}
function requestChange(value:JsonObject,message:string,actionLabel:string){
  if(props.disabled||pendingChange.value)return
  error.value=''
  pendingChange.value={value:JSON.parse(JSON.stringify(value)),message,actionLabel,snapshot:changeSnapshot.value}
}
function keepMapping(){pendingChange.value=null}
function confirmChange(){
  const pending=pendingChange.value
  if(props.disabled||!pending)return
  if(pending.snapshot!==changeSnapshot.value){pendingChange.value=null;error.value='映射或来源目录已变化，请重新选择。';return}
  pendingChange.value=null
  childDrafts.value={}
  commit(pending.value)
}
// A controlled select must keep the accepted value until its owner accepts the change.
// Cancelled or parent-deferred changes do not necessarily trigger a Vue prop update.
function selectChange(event:Event,current:unknown,apply:(value:string)=>void){
  const select=event.currentTarget as HTMLSelectElement,value=select.value
  select.value=String(current??'')
  apply(value)
}
function changeKind(value:string){
  if(props.disabled||pendingChange.value||kind.value===value||(value&&!nodeOptions.value.some(row=>row.value===value)))return
  const next=value?(props.previewContext?initializePreviewMapping:initializeMapping)(value,props.schema,props.defaultSchemas||[]):{}
  if(Object.keys(props.modelValue).length)requestChange(next,`“${props.schema.title||props.label}”将切换为“${nodeOptions.value.find(row=>row.value===value)?.label||'未选择'}”，并清除此项原有映射及嵌套内容。其他映射不变。`,'清除原映射并切换')
  else commit(next)
}
function child(key:string,value:JsonObject){commit({...props.modelValue,fields:{...obj(props.modelValue.fields),[key]:value}})}
function item(index:number,value:JsonObject){commit({...props.modelValue,items:items.value.map((row,i)=>i===index?value:row)})}
function move(index:number,offset:number){if(draftErrors.value.length)return;const next=[...items.value],target=index+offset;if(target<0||target>=next.length)return;[next[index],next[target]]=[next[target]!,next[index]!];commit({...props.modelValue,items:next})}
function removeItem(index:number){if(draftErrors.value.length||index<0||index>=items.value.length)return;requestChange({...props.modelValue,items:items.value.filter((_,i)=>i!==index)},`将移除第 ${index+1} 项及其嵌套映射，其余条目的内容和相对顺序不变。`,'移除此项')}
function addField(){const key=keyDraft.value.trim();if(!key||mappingHas(obj(props.modelValue.fields),key)){error.value='字段名不能为空或重复';return}error.value='';child(key,{});keyDraft.value=''}
function addDeclaredField(key:string){if(mappingHas(properties.value,key)&&!mappingHas(obj(props.modelValue.fields),key))child(key,{})}
function removeField(key:string){
  if(props.disabled||draftErrors.value.length||!mappingHas(obj(props.modelValue.fields),key)||(requiredKeys.value.includes(key)&&!hasDefault(key)))return
  const fields={...obj(props.modelValue.fields)};delete fields[key]
  requestChange({...props.modelValue,fields},hasDefault(key)?`将省略“${obj(properties.value[key]).title||key}”及其映射，解析时使用所引用结构的默认值；不复制默认值到当前配置。`:`将移除“${obj(properties.value[key]).title||key}”及其嵌套映射，其他字段不变。`,hasDefault(key)?'省略此字段':'移除此字段')
}
function selectSource(selector:string,each=false){
  if(props.disabled||pendingChange.value)return
  const previous=each?obj(props.modelValue.source).selector_key:props.modelValue.selector_key
  if(previous===selector)return
  const choice=choices.value.find(row=>row.selector_key===selector)
  if(!choice)return
  const value={kind:'context_field',selector_key:selector,scope:{kind:choice.scope_kind}}
  if(each)setListSource(value)
  else replaceSource(value)
}
function replaceSource(value:JsonObject){
  if(JSON.stringify(value)===JSON.stringify(props.modelValue))return
  if(Array.isArray(props.modelValue.path)&&props.modelValue.path.length)requestChange(value,'更换来源将清除此项原有字段路径；其他映射不变。','更换来源并清除路径')
  else commit(value)
}
function changeScope(scope:string){
  if(scope===props.modelValue.scope||!props.previewContext?.scopes.some(row=>row.value===scope&&(scope!=='item'||props.itemSchema)))return
  replaceSource({kind:'path',scope})
}
function selectOutput(key:string){
  const choice=outputChoices.value.find(row=>mappingOutputIdentity(row.stageKey,row.outputKey)===key)
  if(!choice)return
  replaceSource({kind:'selected_output',stage_key:choice.stageKey,output_key:choice.outputKey,scope:{kind:choice.scopeKind},selection_policy:'reviewed_selected'})
}
function setListSource(value:JsonObject){
  if(props.disabled||pendingChange.value)return
  if(JSON.stringify(value)===JSON.stringify(props.modelValue.source))return
  const next={...props.modelValue,source:value,item:{}}
  if(Object.keys(obj(props.modelValue.item)).length)requestChange(next,'更换列表来源或路径将清除此列表的逐条映射；继续编辑会同时保留原来源和逐条映射。','更换来源并清除逐条映射')
  else commit(next)
}
function setPath(path:(string|number)[]){
  const next:JsonObject={...props.modelValue,path}
  // An explicit new selection replaces an invalid old form value, never a read-time conversion.
  if(kind.value==='item_field')delete next.field
  if(!path.length||typeof path[path.length-1]!=='string')delete next.if_missing
  commit(next)
}
</script>
<template>
  <fieldset class="mapping-node" :disabled="disabled">
    <legend>{{ schema.title || label }} <small v-if="schema.title">{{ label }}</small></legend>
    <ConfigurationDiscardNotice v-if="pendingChange" title="确认映射变更" :message="pendingChange.message" :action-label="pendingChange.actionLabel" :busy="Boolean(disabled)" @keep="keepMapping" @discard="confirmChange" />
    <fieldset class="mapping-controls" :disabled="Boolean(pendingChange)">
    <label>此项如何取值<select :value="kind" @change="selectChange($event,kind,changeKind)">
      <option v-if="kind&&!nodeOptions.some(row=>row.value===kind)" :value="kind" disabled>已保存但不可用：{{kind}}</option>
      <option value="">请选择来源或组装方式</option><option v-for="option in nodeOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
    </select></label>
    <p v-if="kind&&!nodeOptions.some(row=>row.value===kind)" role="alert">保存的映射不适用于当前范围或字段类型；原值未修改，请重新配置。</p>
    <template v-if="previewContext && kind==='path'">
      <label>路径起点<select :value="modelValue.scope" @change="selectChange($event,modelValue.scope,changeScope)"><option v-for="scope in previewContext.scopes.filter(row=>row.value!=='item'||itemSchema)" :key="scope.value" :value="scope.value">{{ scope.label }}</option></select></label>
      <SchemaPathSelect v-if="pathOrigin" :schema="pathOrigin" :model-value="modelValue.path" :disabled="disabled" label="来源字段路径" @update:model-value="setPath" />
      <p v-else role="alert">当前作用域没有来源结构，请明确选择路径起点。</p>
    </template>
    <template v-if="previewContext && kind==='lookup'">
      <p>按对象列表中声明的唯一文本 key 精确查找；缺失或重复不会选择第一项。</p>
      <RecipeStructureMappingEditor :model-value="obj(modelValue.collection)" :schema="{type:'array'}" label="查找集合" :nodes="nodes" :fields="fields" :outputs="outputs" :scopes="scopes" :prior-stages="priorStages" :preview-context="previewContext" :allowed-kinds="['path']" :item-schema="itemSchema" :disabled="disabled" @update:model-value="commit({...modelValue,collection:$event})" @draft-validity="childDrafts.collection=$event" />
      <template v-if="lookupItemSchema">
        <SchemaPathSelect :schema="lookupItemSchema" :model-value="modelValue.key_path" :disabled="disabled" label="集合条目的稳定 key 路径" @update:model-value="commit({...modelValue,key_path:$event})" />
        <SchemaPathSelect :schema="lookupItemSchema" :model-value="modelValue.value_path" :disabled="disabled" label="找到后读取的内容路径" @update:model-value="commit({...modelValue,value_path:$event})" />
      </template>
      <RecipeStructureMappingEditor :model-value="obj(modelValue.key)" :schema="{type:'string'}" label="要查找的 key" :nodes="nodes" :fields="fields" :outputs="outputs" :scopes="scopes" :prior-stages="priorStages" :preview-context="previewContext" :allowed-kinds="['path','literal']" :item-schema="itemSchema" :disabled="disabled" @update:model-value="commit({...modelValue,key:$event})" @draft-validity="childDrafts.key=$event" />
    </template>
    <p v-if="kind==='unit_key'">使用当前制作对象的真实标识，不需要填写UUID。</p>
    <p v-if="kind==='material_difference'">使用当前已审核变体的差异原文，不另填正文。此位置必须对应已发布策略的唯一直通文字字段或单元贡献字段；单元贡献须使用“当前制作单元标识”，保存和准备时由服务器核对。</p>
    <p v-if="kind==='reference_subjects'">仅列入本次已解析主参考图的主体；不会把项目中其他人物加入。</p>
    <label v-if="kind==='context_field'">受控内容来源<select :value="modelValue.selector_key" @change="selectChange($event,modelValue.selector_key,selectSource)"><option value="">请选择目录字段</option><option v-for="row in choices" :key="row.selector_key" :value="row.selector_key">{{ row.label }}</option></select></label>
    <p v-if="kind==='context_field'&&modelValue.selector_key">{{ choices.find(row=>row.selector_key===modelValue.selector_key)?.description || '当前来源不在可选目录内，请核对前置审核步骤和输入范围。' }}</p>
    <label v-if="kind==='selected_output'">上游已采用输出<select :value="mappingOutputIdentity(modelValue.stage_key,modelValue.output_key)" @change="selectChange($event,mappingOutputIdentity(modelValue.stage_key,modelValue.output_key),selectOutput)"><option value="">请选择明确输出</option><option v-if="modelValue.output_key&&!selectedOutput" :value="mappingOutputIdentity(modelValue.stage_key,modelValue.output_key)" disabled>已保存来源不可用 · {{ modelValue.stage_key }}</option><option v-for="row in outputChoices" :key="mappingOutputIdentity(row.stageKey,row.outputKey)" :value="mappingOutputIdentity(row.stageKey,row.outputKey)">{{ row.label }}</option></select><small>只读取本轮前置步骤按配置选择的产物，不取最新候选。名称前的序号对应当前配方步骤；已保存映射不会自动改写。</small></label>
    <p v-if="kind==='selected_output'&&modelValue.output_key&&!selectedOutput" role="alert">来源步骤、输出或适用范围已变化，请重新选择；已保存字段路径保持不变。</p>
    <ShotFieldMappingSelect v-if="kind==='shot_field'" :model-value="modelValue" :disabled="disabled||!shotFieldsAllowed" @update:model-value="commit"/>
    <SchemaPathSelect v-if="['context_field','selected_output','item_field'].includes(kind)||(kind==='shot_field'&&!shotFieldSchema(modelValue,shotCatalog.states).error)" :schema="sourceSchema" :model-value="mappingHas(modelValue,'path')?modelValue.path:kind==='item_field'?undefined:[]" :disabled="disabled" :label="kind==='item_field'?'当前条目字段路径':'来源内的字段路径'" @update:model-value="setPath"/>
    <p v-if="kind==='item_field'&&mappingHas(modelValue,'field')" role="alert">旧field映射不再可用；请明确重选路径。</p>
    <template v-if="kind==='item_field'&&Array.isArray(modelValue.path)&&typeof modelValue.path[modelValue.path.length-1]==='string'">
      <label>字段不存在时<select :value="mappingHas(modelValue,'if_missing')?'explicit':'reject'" @change="($event.target as HTMLSelectElement).value==='explicit'?commit({...modelValue,if_missing:schema.type==='boolean'?false:null}):commit(Object.fromEntries(Object.entries(modelValue).filter(([key])=>key!=='if_missing')))"><option value="reject">阻断并提示（不补值）</option><option value="explicit">使用明确配置的值</option></select></label>
      <template v-if="mappingHas(modelValue,'if_missing')">
        <label v-if="schema.type==='boolean'">明确布尔值<select :value="String(modelValue.if_missing)" @change="commit({...modelValue,if_missing:($event.target as HTMLSelectElement).value==='true'})"><option v-if="typeof modelValue.if_missing!=='boolean'" :value="String(modelValue.if_missing)" disabled>已保存值不是布尔值，请修正</option><option value="false">false</option><option value="true">true</option></select></label>
        <StructuredJsonEditor v-else :model-value="modelValue.if_missing" :schemas="[schema]" label="字段缺失时的明确值" :disabled="disabled" @update:model-value="commit({...modelValue,if_missing:$event})"/>
        <p>只处理字段不存在；已有 false、true、null 或错误类型均保留并接受合同校验。转换证据随执行输入保存，不改写原始来源。</p>
      </template>
    </template>
    <StructuredJsonEditor v-if="kind==='literal'" :label="'固定值 · '+label" :model-value="modelValue.value" :disabled="disabled||mappingHas(schema,'const')" @update:model-value="commit({...modelValue,value:$event})" @draft-validity="childDrafts.literal=$event"/>
    <template v-if="kind==='object'">
      <div v-for="[key,value] in entries" :key="key">
        <RecipeStructureMappingEditor :model-value="obj(value)" :schema="obj(properties[key])" :default-schemas="childDefaults(key)" :label="key" :nodes="nodes" :fields="fields" :outputs="outputs" :scopes="scopes" :prior-stages="priorStages" :item-schema="itemSchema" :disabled="disabled" :preview-context="previewContext" @draft-validity="childDrafts['field:'+key]=$event" @update:model-value="child(key,$event)"/>
        <button v-if="hasDefault(key)||!requiredKeys.includes(key)" type="button" class="button ghost tiny" :disabled="disabled||Boolean(draftErrors.length)" @click="removeField(key)">{{ hasDefault(key)?'省略此项，使用结构默认值':'移除字段' }}</button>
      </div>
      <div v-for="key in omittedFields" :key="'omitted-'+key"><p>{{ obj(properties[key]).title || key }}：{{ hasDefault(key)?'已省略；准备时按所引用结构补默认，不写入当前模板。':'尚未配置映射。' }}</p><button type="button" class="button ghost tiny" @click="addDeclaredField(key)">配置 {{ obj(properties[key]).title || key }} 的来源</button></div>
      <div v-if="allowAdditional"><label>新增字段名<input v-model="keyDraft"/></label><button type="button" class="button ghost tiny" @click="addField">添加字段</button></div>
    </template>
    <template v-if="kind==='array'">
      <div v-for="(value,index) in items" :key="index">
        <RecipeStructureMappingEditor :model-value="value" :schema="jsonChildSchemas([schema],'0',true)[0]||{}" :default-schemas="childDefaults(String(index),true)" :label="'第 '+(index+1)+' 项'" :nodes="nodes" :fields="fields" :outputs="outputs" :scopes="scopes" :prior-stages="priorStages" :item-schema="itemSchema" :disabled="disabled" :preview-context="previewContext" @draft-validity="childDrafts['item:'+index]=$event" @update:model-value="item(index,$event)"/>
        <button type="button" class="button ghost tiny" :disabled="disabled||index===0||Boolean(draftErrors.length)" @click="move(index,-1)">上移</button>
        <button type="button" class="button ghost tiny" :disabled="disabled||index===items.length-1||Boolean(draftErrors.length)" @click="move(index,1)">下移</button>
        <button type="button" class="button ghost tiny" :disabled="disabled||Boolean(draftErrors.length)" @click="removeItem(index)">移除此项</button>
      </div>
      <button type="button" class="button ghost tiny" @click="commit({...modelValue,items:[...items,{}]})">添加有序项</button>
    </template>
    <template v-if="kind==='unit_map'">
      <p>以下内容放入当前制作单元的分组，与参考单元使用同一个标识。</p>
      <RecipeStructureMappingEditor :model-value="obj(modelValue.value)" :schema="obj(schema.additionalProperties)" label="单元内的内容" :nodes="nodes" :fields="fields" :outputs="outputs" :scopes="scopes" :prior-stages="priorStages" :item-schema="itemSchema" :disabled="disabled" @draft-validity="childDrafts.value=$event" @update:model-value="commit({...modelValue,value:$event})"/>
    </template>
    <template v-if="kind==='each'">
      <RecipeStructureMappingEditor :model-value="obj(modelValue.source)" :schema="{type:'array'}" label="按原顺序读取的列表" :nodes="nodes" :fields="fields" :outputs="outputs" :scopes="scopes" :prior-stages="priorStages" :item-schema="itemSchema" :disabled="disabled" :preview-context="previewContext" @draft-validity="childDrafts.source=$event" source-only @update:model-value="setListSource($event)"/>
      <RecipeStructureMappingEditor v-if="eachSchema" :model-value="obj(modelValue.item)" :schema="jsonChildSchemas([schema],'0',true)[0]||{}" :default-schemas="childDefaults('0',true)" label="每一条如何填写" :nodes="nodes" :fields="fields" :outputs="outputs" :scopes="scopes" :prior-stages="priorStages" :item-schema="eachSchema" :disabled="disabled" :preview-context="previewContext" @draft-validity="childDrafts.item=$event" @update:model-value="commit({...modelValue,item:$event})"/>
    </template>
    <p v-if="error" role="alert">{{ error }}</p>
    </fieldset>
  </fieldset>
</template>
<style scoped>
.mapping-node{min-width:0;margin:8px 0;padding:12px;border:1px solid var(--border);border-radius:var(--config-radius);display:grid;gap:12px;font-size:var(--config-body-size);font-weight:400;line-height:1.6}.mapping-controls{min-width:0;margin:0;padding:0;border:0;display:grid;gap:8px}.mapping-node legend{font-size:var(--config-section-size);font-weight:600}.mapping-node legend,.mapping-node p{overflow-wrap:anywhere}.mapping-node label{display:grid;gap:5px;min-width:0}.mapping-node select,.mapping-node input{box-sizing:border-box;width:100%;min-width:0;min-height:var(--config-control-height);background:var(--bg-soft);color:inherit;border:1px solid var(--border-strong);border-radius:var(--config-radius);padding:6px;font:inherit}.mapping-node p,.mapping-node small{margin:0;color:var(--text-secondary);font-weight:400}.mapping-node small{font-size:var(--config-help-size)}.mapping-node button{max-width:100%;white-space:normal;overflow-wrap:anywhere}
@media(max-width:640px){.mapping-node{padding:10px}.mapping-node select,.mapping-node input,.mapping-node button{min-height:44px}}
</style>
