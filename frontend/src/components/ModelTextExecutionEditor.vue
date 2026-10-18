<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { ChatRequestOptionsEditorOptions, JsonObject, TextExecutionEditorOptions } from '@/services/platformTypes'
import ModelChatRequestOptionsEditor from './ModelChatRequestOptionsEditor.vue'
import StructuredJsonEditor from './StructuredJsonEditor.vue'
import { initializeTextExecution, textContractObject as obj, textExecutionErrors, updateTextExecutionParameter, setTextExecutionConstraint } from '@/services/modelTextExecution'
import { numericInputDrafts } from '@/services/numericInputDrafts'
const props=defineProps<{modelValue?:unknown;capabilities:unknown;options?:TextExecutionEditorOptions|null;chatOptions?:ChatRequestOptionsEditorOptions|null;disabled?:boolean}>()
const emit=defineEmits<{'update:modelValue':[value:JsonObject|undefined];validity:[errors:string[]]}>()
const contract=computed(()=>obj(props.modelValue))
const operations=computed(()=>obj(contract.value.operations))
const selected=computed(()=>props.options?.operations.filter(row=>Array.isArray(props.capabilities)&&props.capabilities.includes(row.value))||[])
const visible=computed(()=>selected.value.length>0||props.modelValue!==undefined)
const blocked=computed(()=>props.disabled||!props.options?.integerConstraints)
const localError=ref('')
const operationLabel=(key:string)=>props.options?.operations.find(row=>row.value===key)?.label||key
const parameterLabel=(key:string)=>props.options?.parameters.find(row=>row.value===key)?.label||key
const list=(value:unknown):unknown[]=>Array.isArray(value)?value:[]
const numberKey=(operation:string,key:string,field:string)=>JSON.stringify([operation,key,field])
const listKey=(operation:string,key:string,field:string,index:number,part:string)=>JSON.stringify([operation,key,field,index,part])
const numbers=numericInputDrafts(()=>props.options?.integerConstraints?Object.entries(operations.value).flatMap(([operation,raw])=>{
  if(!props.options?.operations.some(row=>row.value===operation))return []
  return Object.entries(obj(obj(raw).parameters)).flatMap(([key,rawRule])=>{
    if(!props.options?.parameters.some(row=>row.value===key))return []
    const rule=obj(rawRule),label=`${operationLabel(operation)} ${parameterLabel(key)}`
    return [
      ...props.options.integerConstraints.valueFields.map(field=>({key:numberKey(operation,key,field.value),label:`${label} ${field.label}`})),
      ...list(rule.enum).map((_,index)=>({key:listKey(operation,key,'enum',index,''),label:`${label} 允许值 ${index+1}`})),
      ...list(rule.alignments).flatMap((_,index)=>['modulus','remainder'].map(part=>({key:listKey(operation,key,'alignments',index,part),label:`${label} ${part==='modulus'?'步长':'余数'} ${index+1}`}))),
    ]
  })
}):[],()=>blocked.value)
const optionErrors=ref<Record<string,string[]>>({})
const errors=computed(()=>[...textExecutionErrors(props.modelValue,props.capabilities,props.options),...Object.keys(operations.value).flatMap(key=>optionErrors.value[key]||[]),...numbers.errors.value,localError.value].filter(Boolean))
watch(errors,value=>emit('validity',value),{immediate:true,deep:true})
function commit(value:JsonObject|undefined) { if(!blocked.value) { localError.value='';emit('update:modelValue',value) } }
function initialize() { if(blocked.value||!props.options)return;try{commit(initializeTextExecution(props.modelValue,props.capabilities,props.options))}catch(exc){localError.value=String(exc)} }
function policy(operation:string, value:JsonObject) { commit({...contract.value,operations:{...operations.value,[operation]:value}}) }
function parameter(operation:string,key:string,field:string,raw:string) { if(!blocked.value)commit(updateTextExecutionParameter(props.modelValue,operation,key,field,raw)) }
function numberInput(operation:string,key:string,field:string,event:Event) {
  if(numbers.capture(numberKey(operation,key,field),event))parameter(operation,key,field,(event.target as HTMLInputElement).value)
}
const numericValue=(operation:string,key:string,field:string,rule:unknown)=>numbers.value(numberKey(operation,key,field),obj(rule)[field])
function clearNumericErrors(operation:string,key?:string) {
  numbers.remapKeys(path=>{const [savedOperation,savedKey]=JSON.parse(path);return savedOperation!==operation||(key!==undefined&&savedKey!==key)?path:undefined})
}
function constraint(operation:string,key:string,field:string,value:unknown) { if(!blocked.value)commit(setTextExecutionConstraint(props.modelValue,operation,key,field,value)) }
function listItem(operation:string,key:string,field:string,index:number,value:unknown) {
  const values=[...list(obj(obj(obj(operations.value[operation]).parameters)[key])[field])]
  values[index]=value;constraint(operation,key,field,values)
}
function listInput(operation:string,key:string,field:string,index:number,part:string,event:Event){
  if(!numbers.capture(listKey(operation,key,field,index,part),event))return
  const raw=(event.target as HTMLInputElement).value,value=raw===''?null:Number(raw)
  const current=list(obj(obj(obj(operations.value[operation]).parameters)[key])[field])[index]
  listItem(operation,key,field,index,field==='enum'?value:{...obj(current),[part]:value})
}
function removeItem(operation:string,key:string,field:string,index:number) {
  const current=list(obj(obj(obj(operations.value[operation]).parameters)[key])[field])
  if(blocked.value||!['enum','alignments'].includes(field)||!Number.isInteger(index)||index<0||index>=current.length)return
  numbers.remapKeys(path=>{
    const parts=JSON.parse(path)
    if(parts[0]!==operation||parts[1]!==key||parts[2]!==field||!Number.isInteger(parts[3]))return path
    if(parts[3]===index)return undefined
    if(parts[3]>index)parts[3]--
    return JSON.stringify(parts)
  })
  const values=current.filter((_,position)=>position!==index)
  constraint(operation,key,field,values.length?values:undefined)
}
function addParameter(operation:string,key:string) {
  const definition=props.options?.parameters.find(row=>row.value===key), row=obj(operations.value[operation])
  if(blocked.value||!definition||Object.prototype.hasOwnProperty.call(obj(row.parameters),key))return
  clearNumericErrors(operation,key)
  policy(operation,{...row,parameters:{...obj(row.parameters),[key]:{...definition.template}}})
}
function removeParameter(operation:string,key:string) {
  if(blocked.value||props.options?.parameters.find(row=>row.value===key)?.required)return
  clearNumericErrors(operation,key)
  const row=obj(operations.value[operation]); policy(operation,{...row,parameters:Object.fromEntries(Object.entries(obj(row.parameters)).filter(([name])=>name!==key))})
}
function removeOperation(operation:string) {
  if(blocked.value||!window.confirm('删除当前草稿中此能力的参数配置？不改变已发布版本。'))return
  clearNumericErrors(operation)
  const next=Object.fromEntries(Object.entries(operations.value).filter(([key])=>key!==operation))
  commit(Object.keys(next).length?{...contract.value,operations:next}:undefined)
}
const extraRule=(value:unknown)=>Object.fromEntries(Object.entries(obj(value)).filter(([key])=>!['type','required','minimum','maximum','default','const','enum','alignments','seed_policy'].includes(key)))
</script>
<template>
  <section v-if="visible" class="text-execution-editor">
    <header><strong>文本执行协议与参数范围</strong><button v-if="!disabled" type="button" class="button ghost small" :disabled="blocked||!selected.length" @click="initialize">补齐所选能力配置</button></header>
    <p>为勾选的文本能力声明可用范围。后续各层不能放宽约束；必填参数未设默认／固定值时需明确提供，可选参数缺省不发送。保存时由服务端核对全部约束交集，不会猜测模型上限。</p>
    <p v-if="!options" role="alert">配置目录未读取，请重新读取系统设置。</p>
    <article v-for="(raw,operation) in operations" :key="operation">
      <header><strong>{{ operationLabel(operation) }} <small>{{ operation }}</small></strong><button v-if="!selected.some(row=>row.value===operation)" type="button" class="button ghost small" :disabled="blocked" @click="removeOperation(operation)">删除未勾选能力的配置</button></header>
      <div class="parameters">
        <template v-for="(rule,key) in obj(obj(raw).parameters)" :key="key">
          <header><strong>{{ parameterLabel(key) }} <small>{{ key }}</small></strong>
            <button v-if="!options?.parameters.find(row=>row.value===key)?.required" type="button" class="button ghost small" :disabled="blocked" @click="removeParameter(operation,key)">移除此参数</button>
          </header>
          <label class="required-constraint"><input type="checkbox" :checked="obj(rule).required===true" :disabled="blocked||options?.parameters.find(row=>row.value===key)?.required" @change="constraint(operation,key,'required',($event.target as HTMLInputElement).checked)"/>必须提供此参数</label>
          <div class="parameter-row">
            <label v-for="field in options?.integerConstraints.valueFields" :key="field.value">{{ field.label }}
              <input type="number" step="1" :aria-label="`${operationLabel(operation)} ${parameterLabel(key)} ${field.label}`" :aria-invalid="numbers.invalid(numberKey(operation,key,field.value))" :value="numericValue(operation,key,field.value,rule)" :disabled="blocked" @input="numberInput(operation,key,field.value,$event)"/>
              <button v-if="numbers.invalid(numberKey(operation,key,field.value))" type="button" class="button ghost small" :disabled="blocked" :aria-label="`撤销 ${operationLabel(operation)} ${parameterLabel(key)} ${field.label} 的未完成数字输入`" @click="numbers.discard(numberKey(operation,key,field.value))">撤销未完成输入（保留原配置）</button>
            </label>
          </div>
          <p v-if="options?.parameters.find(row=>row.value===key)?.description">{{ options?.parameters.find(row=>row.value===key)?.description }}</p>
          <details class="constraints"><summary>允许值、对齐与种子策略限制</summary>
            <div class="constraints-body">
            <label>模型种子策略限制<select :value="obj(rule).seed_policy??''" :disabled="blocked" @change="constraint(operation,key,'seed_policy',($event.target as HTMLSelectElement).value||undefined)">
              <option value="">模型不限定，由实例选择</option><option v-for="choice in options?.integerConstraints.seedPolicies" :key="choice.value" :value="choice.value">{{ choice.label }}</option>
              <option v-if="obj(rule).seed_policy!==undefined&&!options?.integerConstraints.seedPolicies.some(row=>row.value===obj(rule).seed_policy)" :value="String(obj(rule).seed_policy)" disabled>未登记：{{ obj(rule).seed_policy }}</option>
            </select></label>
            <p>固定值是硬限制，默认值可被后续显式取值替代。生成一次需要有限范围或允许值，且不能同时声明默认／固定值；切换不会自动删除已有内容。</p>
            <strong>允许值列表（未设置表示不限制枚举）</strong>
            <div v-for="(item,index) in list(obj(rule).enum)" :key="index" class="constraint-row">
              <label>允许值 {{ index+1 }}<input type="number" step="1" :aria-label="`${operationLabel(operation)} ${parameterLabel(key)} 允许值 ${index+1}`" :aria-invalid="numbers.invalid(listKey(operation,key,'enum',index,''))" :value="numbers.value(listKey(operation,key,'enum',index,''),item)" :disabled="blocked" @input="listInput(operation,key,'enum',index,'',$event)"/><button v-if="numbers.invalid(listKey(operation,key,'enum',index,''))" type="button" class="button ghost small" :disabled="blocked" :aria-label="`撤销 ${operationLabel(operation)} ${parameterLabel(key)} 允许值 ${index+1} 的未完成数字输入`" @click="numbers.discard(listKey(operation,key,'enum',index,''))">撤销未完成输入（保留原配置）</button></label>
              <button type="button" class="button ghost small" :disabled="blocked" @click="removeItem(operation,key,'enum',index)">移除允许值 {{ index+1 }}</button>
            </div>
            <button type="button" class="button ghost small" :disabled="blocked" @click="constraint(operation,key,'enum',[...list(obj(rule).enum),null])">添加允许值</button>
            <strong>对齐规则（多项须同时满足）</strong>
            <div v-for="(item,index) in list(obj(rule).alignments)" :key="index" class="constraint-row">
              <label v-for="part in ['modulus','remainder']" :key="part">{{ part==='modulus'?'步长':'余数' }} {{ index+1 }}<input type="number" step="1" :min="part==='modulus'?1:0" :aria-label="`${operationLabel(operation)} ${parameterLabel(key)} ${part==='modulus'?'步长':'余数'} ${index+1}`" :aria-invalid="numbers.invalid(listKey(operation,key,'alignments',index,part))" :value="numbers.value(listKey(operation,key,'alignments',index,part),obj(item)[part])" :disabled="blocked" @input="listInput(operation,key,'alignments',index,part,$event)"/><button v-if="numbers.invalid(listKey(operation,key,'alignments',index,part))" type="button" class="button ghost small" :disabled="blocked" :aria-label="`撤销 ${operationLabel(operation)} ${parameterLabel(key)} ${part==='modulus'?'步长':'余数'} ${index+1} 的未完成数字输入`" @click="numbers.discard(listKey(operation,key,'alignments',index,part))">撤销未完成输入（保留原配置）</button></label>
              <button type="button" class="button ghost small" :disabled="blocked" @click="removeItem(operation,key,'alignments',index)">移除对齐规则 {{ index+1 }}</button>
            </div>
            <button type="button" class="button ghost small" :disabled="blocked" @click="constraint(operation,key,'alignments',[...list(obj(rule).alignments),{modulus:null,remainder:null}])">添加对齐规则</button>
            <template v-for="field in ['enum','alignments']" :key="field"><div v-if="Object.prototype.hasOwnProperty.call(obj(rule),field)&&!Array.isArray(obj(rule)[field])" role="alert">{{ field }} 不是列表，原值保留：<StructuredJsonEditor :model-value="obj(rule)[field]" :label="field" disabled/><button type="button" class="button ghost small" :disabled="blocked" @click="constraint(operation,key,field,undefined)">移除无效 {{ field }} 声明</button></div></template>
            </div>
          </details>
          <details v-if="Object.keys(extraRule(rule)).length"><summary>不适用于整数合同的已保存字段</summary><StructuredJsonEditor :model-value="extraRule(rule)" label="未支持约束" fixed-object disabled/><p>原值保留并阻止保存；请移除此参数后按目录重新声明。</p></details>
        </template>
      </div>
      <div v-if="!disabled" class="toolbar"><button v-for="option in options?.parameters.filter(row=>!Object.prototype.hasOwnProperty.call(obj(obj(raw).parameters),row.value))" :key="option.value" type="button" class="button ghost small" :disabled="blocked" @click="addParameter(operation,option.value)">添加{{ option.label }}</button></div>
      <ModelChatRequestOptionsEditor :model-value="obj(raw).chat_request_options" :options="chatOptions" :label="`${operation} 请求选项`" :source-path="`specification.text_execution.operations[${operation}].chat_request_options`" :disabled="blocked" @update:model-value="policy(operation,{...obj(raw),chat_request_options:$event})" @validity="optionErrors[operation]=$event"/>
    </article>
    <ul v-if="errors.length" role="alert"><li v-for="error in errors" :key="error">{{ error }}</li></ul>
  </section>
</template>
<style scoped>
.text-execution-editor{display:grid;gap:12px;padding:18px;border:1px solid var(--border-color,#354350);border-radius:8px;min-width:0;overflow-wrap:anywhere}.text-execution-editor header,.toolbar{display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px;align-items:center}.text-execution-editor article{display:grid;gap:12px;padding-top:12px;border-top:1px solid var(--border-color,#354350);min-width:0}.parameters,.constraints-body{display:grid;gap:10px;min-width:0}.constraints-body>button{justify-self:start}.parameter-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,150px),1fr));gap:10px;padding:6px 0}.constraint-row{display:flex;flex-wrap:wrap;gap:10px;align-items:end}.text-execution-editor label{display:grid;gap:4px;min-width:0}.constraint-row label{flex:1 1 130px}.text-execution-editor input:not([type=checkbox]),.text-execution-editor select{width:100%;min-width:0;min-height:36px;box-sizing:border-box}.text-execution-editor .required-constraint{display:flex;align-items:center;min-height:36px}.text-execution-editor p,small{font-size:12px;color:var(--text-secondary,#96a5b3);line-height:1.6}.text-execution-editor summary{cursor:pointer;min-height:36px}.text-execution-editor [role=alert]{color:var(--warning,#e9aa52);font-size:12px}
.text-execution-editor label{grid-template-columns:minmax(0,1fr)}
.text-execution-editor label>.button{min-width:0;max-width:100%;min-height:36px;white-space:normal}
</style>
