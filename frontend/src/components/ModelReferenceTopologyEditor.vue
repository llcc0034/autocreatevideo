<script setup lang="ts">
import { computed, watch } from 'vue'
import type { ConditioningContractEditorOptions, JsonObject } from '@/services/platformTypes'
import { contractObject as object, contractRows as rows } from '@/services/modelConditioningContract'
import { newReferenceTopology, nextTopologyKey } from '@/services/modelReferenceTopology'
import { numericInputDrafts } from '@/services/numericInputDrafts'
const props=defineProps<{mode:JsonObject;options?:ConditioningContractEditorOptions|null;disabled?:boolean}>()
const emit=defineEmits<{'update:modelValue':[value:unknown];validity:[errors:string[]]}>()
const catalog=computed(()=>props.options?.dynamicReferenceTopology)
const topology=computed(()=>object(props.mode.dynamic_reference_topology))
const blocked=computed(()=>props.disabled||!catalog.value)
const groups=computed(()=>catalog.value?.media_groups||[])
const purposes=(group:string)=>rows(object(topology.value[group]).purposes)
const forKind=(kind:unknown)=>purposes(groups.value.find(group=>group.media_kind===kind)?.value||'')
const preprocessing=(key:string)=>object(object(topology.value.preprocessing)[key])
function set(value:unknown){if(!blocked.value)emit('update:modelValue',value)}
function initialize(){if(catalog.value)set(newReferenceTopology(props.mode,catalog.value))}
function groupValue(group:string,key:string,value:unknown){set({...topology.value,[group]:{...object(topology.value[group]),[key]:value}})}
function groupRow(group:string,index:number,key:string,value:unknown){groupValue(group,'purposes',purposes(group).map((row,i)=>i===index?{...row,[key]:value}:row))}
function addPurpose(group:string){const items=purposes(group);groupValue(group,'purposes',[...items,{key:nextTopologyKey('purpose',items),label:'',description:'',minimum_items:0,maximum_items:0}])}
function removePurpose(group:string,index:number){if(!blocked.value&&purposes(group)[index]&&window.confirm('移除此草稿的用途？引用它的默认用途、配对或互斥规则不会自动改写，须逐项调整后才能保存。')){removeDraftRow('purpose',group,index);groupValue(group,'purposes',purposes(group).filter((_,i)=>i!==index))}}
function processValue(slot:string,key:string,value:unknown){set({...topology.value,preprocessing:{...object(topology.value.preprocessing),[slot]:{...preprocessing(slot),[key]:value}}})}
function enableProcess(slot:string){const entry=catalog.value?.preprocessing_slots.find(row=>row.value===slot);if(entry)set({...topology.value,preprocessing:{...object(topology.value.preprocessing),[slot]:{adapter_key:'',minimum_duration_ms:null,maximum_duration_ms:null,...(entry.include_target_fps?{target_fps:null,requires_source_fps:false}:{})}}})}
function removeProcess(slot:string){if(!blocked.value&&window.confirm('移除此草稿的预处理规则？已启用的媒体槽必须重新配置才能保存。')){numbers.remapKeys(key=>{const path=JSON.parse(key);return path[0]==='process'&&path[1]===slot?undefined:key});set({...topology.value,preprocessing:{...object(topology.value.preprocessing),[slot]:null}})}}
function ruleValue(field:string,index:number,key:string,value:unknown){set({...topology.value,[field]:rows(topology.value[field]).map((row,i)=>i===index?{...row,[key]:value}:row)})}
function addRule(field:string){const items=rows(topology.value[field]);set({...topology.value,[field]:[...items,{key:nextTopologyKey('rule',items),label:'',description:'',...(field==='pairing_rules'?{...catalog.value?.pairing_direction,source_purpose_keys:[],target_purpose_keys:[],default_target_purpose_key:'',minimum_targets_per_source:0,maximum_targets_per_source:0,allow_embedded_source:false,allow_external_target:false,source_and_external_mutually_exclusive:false}:{media_kind:'',purpose_keys:[],maximum_distinct_purposes:null})}]})}
function removeRule(field:string,index:number){if(!blocked.value&&rows(topology.value[field])[index]&&window.confirm('移除此未保存草稿中的规则？不会改变已发布版本或历史任务。')){removeDraftRow('rule',field,index);set({...topology.value,[field]:rows(topology.value[field]).filter((_,i)=>i!==index)})}}
function toggle(field:string,index:number,key:string,purpose:string,checked:boolean){const before=rows(topology.value[field])[index]?.[key];const values=Array.isArray(before)?before:[];ruleValue(field,index,key,checked?[...new Set([...values,purpose])]:values.filter(value=>value!==purpose))}
const selected=(value:unknown,key:unknown)=>Array.isArray(value)&&value.includes(key)
const missing=(value:unknown,items:JsonObject[])=>Array.isArray(value)?value.filter(key=>!items.some(row=>row.key===key)):[]
const cardinalities=[{key:'minimum_items',label:'最少数量'},{key:'maximum_items',label:'最多数量'}]
const durations=[{key:'minimum_duration_ms',label:'最短时长（毫秒）'},{key:'maximum_duration_ms',label:'最长时长（毫秒）'}]
const ruleFields=[{key:'pairing_rules',label:'视频→音频配对规则'},{key:'mutual_exclusion_rules',label:'用途互斥规则'}]
const sides=[{key:'source_purpose_keys',kind:'video',label:'来源视频用途'},{key:'target_purpose_keys',kind:'audio',label:'目标音频用途'}]
const pairBounds=[{key:'minimum_targets_per_source',label:'每视频最少配对数'},{key:'maximum_targets_per_source',label:'每视频最多配对数'}]
const flags=[{key:'allow_embedded_source',label:'允许使用视频内嵌音轨'},{key:'allow_external_target',label:'允许外部配对音频'},{key:'source_and_external_mutually_exclusive',label:'内嵌音轨与外部音频互斥'}]
type NumberPath=(string|number)[]
const numberKey=(path:NumberPath)=>JSON.stringify(path)
const numericFields=computed(()=>!catalog.value||!props.mode.dynamic_reference_topology?[]:[
  ...groups.value.flatMap(group=>purposes(group.value).flatMap((_,i)=>cardinalities.map(field=>({key:numberKey(['purpose',group.value,i,field.key]),label:`${group.label} ${i+1} ${field.label}`})))),
  ...catalog.value.preprocessing_slots.flatMap(slot=>object(topology.value.preprocessing)[slot.value]==null?[]:[...durations,...(slot.include_target_fps?[{key:'target_fps',label:'目标帧率（fps）'}]:[])].map(field=>({key:numberKey(['process',slot.value,field.key]),label:`${slot.label} ${field.label}`}))),
  ...ruleFields.flatMap(group=>rows(topology.value[group.key]).flatMap((_,i)=>(group.key==='pairing_rules'?pairBounds:[{key:'maximum_distinct_purposes',label:'同时最多允许的用途种类数'}]).map(field=>({key:numberKey(['rule',group.key,i,field.key]),label:`${group.label} ${i+1} ${field.label}`})))),
])
const numbers=numericInputDrafts(()=>numericFields.value,()=>blocked.value)
const numericLabel=(path:NumberPath)=>numericFields.value.find(field=>field.key===numberKey(path))?.label||''
watch(numbers.errors,value=>emit('validity',value),{immediate:true})
function numeric(path:NumberPath,event:Event){
  if(!numbers.capture(numberKey(path),event))return
  const raw=(event.target as HTMLInputElement).value,value=raw===''?null:Number(raw)
  const [kind,group,index,field]=path
  if(kind==='purpose')groupRow(String(group),Number(index),String(field),value)
  else if(kind==='process')processValue(String(group),String(index),value)
  else if(kind==='rule')ruleValue(String(group),Number(index),String(field),value)
}
function removeDraftRow(kind:string,group:string,index:number){
  numbers.remapKeys(key=>{const path=JSON.parse(key);if(path[0]!==kind||path[1]!==group)return key;if(path[2]===index)return undefined;if(path[2]>index)path[2]--;return JSON.stringify(path)})
}
</script>
<template>
  <details class="topology-editor">
    <summary>视频／音频用途、预处理与配对规则</summary>
    <p>用途名称由模型能力定义；这里声明“允许怎样使用”，不是选择本次素材。Projector 引用精确模型版本、模式与素材槽；此处不会自动改写已发布工作流。最终可用范围仍须同时满足模型合同、Projector 接线和节点能力。</p>
    <p v-if="!catalog" role="alert">规则目录未读取，不能编辑；请重新读取系统设置。</p>
    <ul v-if="numbers.errors.value.length" role="alert"><li v-for="error in numbers.errors.value" :key="error">{{ error }}</li></ul>
    <button v-if="!mode.dynamic_reference_topology" type="button" :disabled="blocked" class="button ghost small" @click="initialize">配置用途与预处理</button>
    <template v-else>
      <section v-for="group in groups" :key="group.value">
        <h4>{{ group.label }}</h4>
        <p>组数量来自上方 {{ group.input_slot }}，不重复填写。音频用途同时供独立音频及配对音频引用。</p>
        <label>默认用途<select :value="object(topology[group.value]).default_purpose_key??''" :disabled="blocked" @change="groupValue(group.value,'default_purpose_key',($event.target as HTMLSelectElement).value||null)"><option value="">未选择（没有用途时为 null）</option><option v-for="purpose in purposes(group.value)" :key="String(purpose.key)" :value="purpose.key">{{ purpose.label || purpose.key }} · {{ purpose.key }}</option><option v-if="object(topology[group.value]).default_purpose_key && !purposes(group.value).some(p=>p.key===object(topology[group.value]).default_purpose_key)" :value="object(topology[group.value]).default_purpose_key">未声明：{{ object(topology[group.value]).default_purpose_key }}</option></select></label>
        <div v-for="(purpose,i) in purposes(group.value)" :key="i" class="rule-row">
          <div class="fields"><label>用途标识<input :value="purpose.key" :pattern="options?.identifierPattern" :disabled="blocked" @input="groupRow(group.value,i,'key',($event.target as HTMLInputElement).value)"/></label><label>用途名称<input :value="purpose.label" :maxlength="catalog?.label_max_characters" :disabled="blocked" @input="groupRow(group.value,i,'label',($event.target as HTMLInputElement).value)"/></label><label v-for="field in cardinalities" :key="field.key">{{ field.label }}<input type="number" min="0" :max="options?.maximumItems" step="1" :value="numbers.value(numberKey(['purpose',group.value,i,field.key]),purpose[field.key])" :aria-label="numericLabel(['purpose',group.value,i,field.key])" :aria-invalid="numbers.invalid(numberKey(['purpose',group.value,i,field.key]))" :disabled="blocked" @input="numeric(['purpose',group.value,i,field.key],$event)"/><button v-if="numbers.invalid(numberKey(['purpose',group.value,i,field.key]))" type="button" class="button ghost small" :disabled="blocked" :aria-label="`撤销 ${numericLabel(['purpose',group.value,i,field.key])} 的未完成数字输入`" @click="numbers.discard(numberKey(['purpose',group.value,i,field.key]))">撤销未完成输入（保留原配置）</button></label></div>
          <label>用途说明<input :value="purpose.description" :maxlength="catalog?.text_max_characters" :disabled="blocked" @input="groupRow(group.value,i,'description',($event.target as HTMLInputElement).value)"/></label><button type="button" class="button ghost small" :disabled="blocked" @click="removePurpose(group.value,i)">移除用途</button>
        </div>
        <button type="button" class="button ghost small" :disabled="blocked || purposes(group.value).length >= (options?.maximumItems||0)" @click="addPurpose(group.value)">添加{{ group.media_kind==='video'?'视频':'音频' }}用途</button>
      </section>
      <section>
        <h4>素材预处理</h4>
        <p>仅选择服务端已实现的预处理器；不会加载模型或调用供应商。时长单位为毫秒，目标帧率留空表示 null。</p>
        <div v-for="slot in catalog?.preprocessing_slots" :key="slot.value" class="rule-row">
          <strong>{{ slot.label }}</strong>
          <button v-if="object(topology.preprocessing)[slot.value] == null" type="button" class="button ghost small" :disabled="blocked" @click="enableProcess(slot.value)">配置{{ slot.label }}预处理</button>
          <template v-else>
            <div class="fields"><label>预处理器<select :value="preprocessing(slot.value).adapter_key" :disabled="blocked" @change="processValue(slot.value,'adapter_key',($event.target as HTMLSelectElement).value)"><option value="">请选择处理方式</option><option v-for="adapter in slot.adapters" :key="adapter.value" :value="adapter.value">{{ adapter.label }} · {{ adapter.value }}</option><option v-if="preprocessing(slot.value).adapter_key&&!slot.adapters.some(a=>a.value===preprocessing(slot.value).adapter_key)" :value="preprocessing(slot.value).adapter_key">未注册：{{ preprocessing(slot.value).adapter_key }}</option></select></label><label v-for="field in durations" :key="field.key">{{ field.label }}<input type="number" min="1" :max="catalog?.maximum_duration_ms" step="1" :value="numbers.value(numberKey(['process',slot.value,field.key]),preprocessing(slot.value)[field.key])" :aria-label="numericLabel(['process',slot.value,field.key])" :aria-invalid="numbers.invalid(numberKey(['process',slot.value,field.key]))" :disabled="blocked" @input="numeric(['process',slot.value,field.key],$event)"/><button v-if="numbers.invalid(numberKey(['process',slot.value,field.key]))" type="button" class="button ghost small" :disabled="blocked" :aria-label="`撤销 ${numericLabel(['process',slot.value,field.key])} 的未完成数字输入`" @click="numbers.discard(numberKey(['process',slot.value,field.key]))">撤销未完成输入（保留原配置）</button></label></div>
            <p>{{ slot.adapters.find(a=>a.value===preprocessing(slot.value).adapter_key)?.description }}</p>
            <div v-if="slot.include_target_fps" class="fields"><label>目标帧率（fps）<input type="number" min="1" :max="catalog?.maximum_fps" step="1" :value="numbers.value(numberKey(['process',slot.value,'target_fps']),preprocessing(slot.value).target_fps)" :aria-label="numericLabel(['process',slot.value,'target_fps'])" :aria-invalid="numbers.invalid(numberKey(['process',slot.value,'target_fps']))" :disabled="blocked" @input="numeric(['process',slot.value,'target_fps'],$event)"/><button v-if="numbers.invalid(numberKey(['process',slot.value,'target_fps']))" type="button" class="button ghost small" :disabled="blocked" :aria-label="`撤销 ${numericLabel(['process',slot.value,'target_fps'])} 的未完成数字输入`" @click="numbers.discard(numberKey(['process',slot.value,'target_fps']))">撤销未完成输入（保留原配置）</button></label><label class="check"><input type="checkbox" :checked="preprocessing(slot.value).requires_source_fps===true" :disabled="blocked" @change="processValue(slot.value,'requires_source_fps',($event.target as HTMLInputElement).checked)"/>必须能核验源帧率</label></div>
            <button type="button" class="button ghost small" :disabled="blocked" @click="removeProcess(slot.value)">移除预处理规则</button>
          </template>
        </div>
      </section>
      <section v-for="field in ruleFields" :key="field.key">
        <h4>{{ field.label }}</h4>
        <p>只引用上方声明的用途。改名或移除用途不会自动改写规则；失效引用必须明确修正。</p>
        <div v-for="(rule,i) in rows(topology[field.key])" :key="i" class="rule-row">
          <div class="fields"><label>规则标识<input :value="rule.key" :pattern="options?.identifierPattern" :disabled="blocked" @input="ruleValue(field.key,i,'key',($event.target as HTMLInputElement).value)"/></label><label>规则名称<input :value="rule.label" :maxlength="catalog?.label_max_characters" :disabled="blocked" @input="ruleValue(field.key,i,'label',($event.target as HTMLInputElement).value)"/></label></div><label>规则说明<input :value="rule.description" :maxlength="catalog?.text_max_characters" :disabled="blocked" @input="ruleValue(field.key,i,'description',($event.target as HTMLInputElement).value)"/></label>
          <template v-if="field.key==='pairing_rules'">
            <p>配对方向：{{ rule.source_media_kind }} → {{ rule.target_media_kind }}（服务端支持的固定形态）</p>
            <fieldset v-for="side in sides" :key="side.key"><legend>{{ side.label }}</legend><label v-for="purpose in forKind(side.kind)" :key="String(purpose.key)" class="check"><input type="checkbox" :checked="selected(rule[side.key],purpose.key)" :disabled="blocked" @change="toggle(field.key,i,side.key,String(purpose.key),($event.target as HTMLInputElement).checked)"/>{{ purpose.label||purpose.key }} · {{ purpose.key }}</label><label v-for="key in missing(rule[side.key],forKind(side.kind))" :key="String(key)" class="check"><input type="checkbox" checked :disabled="blocked" @change="toggle(field.key,i,side.key,String(key),false)"/>失效引用：{{ key }}（取消选择以移除）</label></fieldset>
            <label>默认目标用途<select :value="rule.default_target_purpose_key" :disabled="blocked" @change="ruleValue(field.key,i,'default_target_purpose_key',($event.target as HTMLSelectElement).value)"><option value="">请选择目标用途</option><option v-for="purpose in forKind('audio').filter(p=>selected(rule.target_purpose_keys,p.key))" :key="String(purpose.key)" :value="purpose.key">{{ purpose.label||purpose.key }} · {{ purpose.key }}</option><option v-if="rule.default_target_purpose_key&&!forKind('audio').some(p=>p.key===rule.default_target_purpose_key&&selected(rule.target_purpose_keys,p.key))" :value="rule.default_target_purpose_key">失效引用：{{ rule.default_target_purpose_key }}</option></select></label>
            <div class="fields"><label v-for="bound in pairBounds" :key="bound.key">{{ bound.label }}<input type="number" min="0" :max="catalog?.maximum_targets_per_source" step="1" :value="numbers.value(numberKey(['rule',field.key,i,bound.key]),rule[bound.key])" :aria-label="numericLabel(['rule',field.key,i,bound.key])" :aria-invalid="numbers.invalid(numberKey(['rule',field.key,i,bound.key]))" :disabled="blocked" @input="numeric(['rule',field.key,i,bound.key],$event)"/><button v-if="numbers.invalid(numberKey(['rule',field.key,i,bound.key]))" type="button" class="button ghost small" :disabled="blocked" :aria-label="`撤销 ${numericLabel(['rule',field.key,i,bound.key])} 的未完成数字输入`" @click="numbers.discard(numberKey(['rule',field.key,i,bound.key]))">撤销未完成输入（保留原配置）</button></label></div>
            <label v-for="flag in flags" :key="flag.key" class="check"><input type="checkbox" :checked="rule[flag.key]===true" :disabled="blocked" @change="ruleValue(field.key,i,flag.key,($event.target as HTMLInputElement).checked)"/>{{ flag.label }}</label>
          </template>
          <template v-else>
            <label>媒体类型<select :value="rule.media_kind" :disabled="blocked" @change="ruleValue(field.key,i,'media_kind',($event.target as HTMLSelectElement).value)"><option value="">请选择媒体类型</option><option v-for="group in groups" :key="group.value" :value="group.media_kind">{{ group.label }}</option></select></label>
            <fieldset><legend>不能全部同时出现的用途</legend><label v-for="purpose in forKind(rule.media_kind)" :key="String(purpose.key)" class="check"><input type="checkbox" :checked="selected(rule.purpose_keys,purpose.key)" :disabled="blocked" @change="toggle(field.key,i,'purpose_keys',String(purpose.key),($event.target as HTMLInputElement).checked)"/>{{ purpose.label||purpose.key }} · {{ purpose.key }}</label><label v-for="key in missing(rule.purpose_keys,forKind(rule.media_kind))" :key="String(key)" class="check"><input type="checkbox" checked :disabled="blocked" @change="toggle(field.key,i,'purpose_keys',String(key),false)"/>失效引用：{{ key }}（取消选择以移除）</label></fieldset>
            <label>同时最多允许的用途种类数<input type="number" min="1" :max="Array.isArray(rule.purpose_keys)?rule.purpose_keys.length-1:0" step="1" :value="numbers.value(numberKey(['rule',field.key,i,'maximum_distinct_purposes']),rule.maximum_distinct_purposes)" :aria-label="numericLabel(['rule',field.key,i,'maximum_distinct_purposes'])" :aria-invalid="numbers.invalid(numberKey(['rule',field.key,i,'maximum_distinct_purposes']))" :disabled="blocked" @input="numeric(['rule',field.key,i,'maximum_distinct_purposes'],$event)"/><button v-if="numbers.invalid(numberKey(['rule',field.key,i,'maximum_distinct_purposes']))" type="button" class="button ghost small" :disabled="blocked" :aria-label="`撤销 ${numericLabel(['rule',field.key,i,'maximum_distinct_purposes'])} 的未完成数字输入`" @click="numbers.discard(numberKey(['rule',field.key,i,'maximum_distinct_purposes']))">撤销未完成输入（保留原配置）</button></label>
          </template>
          <button type="button" class="button ghost small" :disabled="blocked" @click="removeRule(field.key,i)">移除规则</button>
        </div>
        <button type="button" class="button ghost small" :disabled="blocked||rows(topology[field.key]).length >= (catalog?.maximum_rules||0)" @click="addRule(field.key)">添加{{ field.label }}</button>
      </section>
      <details><summary>规则原始结构（只读）</summary><pre>{{ JSON.stringify(topology,null,2) }}</pre></details>
    </template>
  </details>
</template>
<style scoped>
.topology-editor{min-width:0;overflow-wrap:anywhere}.topology-editor summary{cursor:pointer}.topology-editor p{font-size:12px;color:#96a5b3;line-height:1.6}.topology-editor section{padding:12px 0;border-top:1px solid #354350;display:grid;gap:12px}.topology-editor h4{font-size:14px;margin:0}.rule-row{display:grid;gap:10px;border-left:2px solid #354350;padding:8px 0 8px 12px;min-width:0}.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(180px,100%),1fr));gap:12px}.topology-editor label{display:grid;gap:6px;font-size:12px;min-width:0}.topology-editor input,.topology-editor select{min-width:0;min-height:36px;width:100%;box-sizing:border-box;max-width:100%}.topology-editor .check{display:flex;align-items:center;gap:8px}.check input{width:auto;flex-shrink:0}.topology-editor fieldset{min-width:0;border:1px solid #354350;display:grid;gap:8px}.topology-editor pre{white-space:pre-wrap;overflow-wrap:anywhere}.topology-editor button{justify-self:start;white-space:normal;max-width:100%}
.topology-editor label{grid-template-columns:minmax(0,1fr)}
.topology-editor label>.button{min-width:0;min-height:36px;height:auto}
</style>
