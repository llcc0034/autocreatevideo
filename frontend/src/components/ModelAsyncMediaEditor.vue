<script setup lang="ts">
import {computed,ref,watch} from 'vue'
import type {AsyncMediaEditorOptions,JsonObject} from '@/services/platformTypes'
import {asyncMediaErrors,asyncObject as obj,newAsyncOperation,asyncHas as has} from '@/services/modelAsyncMedia'
import {numericInputDrafts} from '@/services/numericInputDrafts'
import AsyncScalarInput from './AsyncScalarInput.vue'
const props=defineProps<{modelValue:unknown;options?:AsyncMediaEditorOptions|null;required?:boolean;disabled?:boolean;requestFieldMap?:unknown}>()
const emit=defineEmits<{'update:modelValue':[value:JsonObject];validity:[errors:string[]]}>()
const spec=computed(()=>obj(props.modelValue)),media=computed(()=>obj(spec.value.async_media)),operations=computed(()=>obj(media.value.operations))
const caps=computed(()=>Array.isArray(spec.value.capabilities)?spec.value.capabilities:[])
const rows=computed(()=>[...new Set([...(props.options?.operations.filter(row=>caps.value.includes(row.key)).map(row=>row.key)||[]),...Object.keys(operations.value)])])
const blocked=computed(()=>props.disabled||!props.options)
const selected=ref<Record<string,string>>({})
const data=(key:string)=>obj(operations.value[key]),schema=(key:string)=>obj(data(key).request_schema),properties=(key:string)=>obj(schema(key).properties),rule=(key:string,name:string)=>obj(properties(key)[name]),smoke=(key:string)=>obj(data(key).smoke_inputs)
const fields=(key:string)=>Object.keys(properties(key))
const descriptor=(key:string)=>props.options?.fields.find(row=>row.key===key)
const bounds=(key:string,name:string)=>rule(key,name).type==='string'?['minLength','maxLength']:['integer','number'].includes(String(rule(key,name).type))?['minimum','maximum']:[]
const boundLabels:Record<string,string>={minLength:'最小字符数',maxLength:'最大字符数',minimum:'最小值',maximum:'最大值'}
const boundKey=(key:string,name:string,bound:string)=>JSON.stringify([key,name,rule(key,name).type,bound])
const numbers=numericInputDrafts(()=>props.options?rows.value.flatMap(key=>fields(key).flatMap(name=>bounds(key,name).map(bound=>({key:boundKey(key,name,bound),label:`${key} ${descriptor(name)?.label??name} ${boundLabels[bound]}`})))):[],()=>blocked.value)
const errors=computed(()=>[...asyncMediaErrors(spec.value,props.options,props.required),...numbers.errors.value])
watch(()=>JSON.stringify(errors.value),value=>emit('validity',JSON.parse(value)),{immediate:true})
const requiredFields=(key:string)=>Array.isArray(schema(key).required)?schema(key).required as string[]:[]
function commit(key:string,value:unknown){if(blocked.value)return;const next={...operations.value};if(value===undefined)delete next[key];else next[key]=value;emit('update:modelValue',{...spec.value,async_media:{...media.value,schema_version:props.options!.schemaVersion,operations:next}})}
function initialize(key:string){const operation=props.options?.operations.find(row=>row.key===key);if(blocked.value||!operation)return;if(operations.value[key]!==undefined&&!window.confirm('重建当前未保存草稿中的此能力合同？现有字段规则和 Smoke 样例将清空，不影响已发布版本。'))return;commit(key,newAsyncOperation(operation.outputKind))}
function editRule(key:string,name:string,property:string,value:unknown){const next={...rule(key,name)};if(value===undefined)delete next[property];else next[property]=value;commit(key,{...data(key),request_schema:{...schema(key),properties:{...properties(key),[name]:next}}})}
function inputBound(key:string,name:string,bound:string,event:Event){if(numbers.capture(boundKey(key,name,bound),event)){const raw=(event.target as HTMLInputElement).value;editRule(key,name,bound,raw===''?undefined:Number(raw))}}
function add(key:string){const name=selected.value[key],field=descriptor(name);if(blocked.value||!field||has(properties(key),name))return;commit(key,{...data(key),request_schema:{...schema(key),properties:{...properties(key),[name]:field.types.length===1?{type:field.types[0]}:{}}}});selected.value[key]=''}
function setRequired(key:string,name:string,value:boolean){if(name==='prompt')return;commit(key,{...data(key),request_schema:{...schema(key),required:value?[...new Set([...requiredFields(key),name])]:requiredFields(key).filter(item=>item!==name)}})}
function setSmoke(key:string,name:string,value:unknown){const next={...smoke(key)};if(value===undefined)delete next[name];else next[name]=value;commit(key,{...data(key),smoke_inputs:next})}
function remove(key:string,name?:string){if(blocked.value||!window.confirm(name?'移除此字段及其 Smoke 样例？只改变当前未保存草稿。':'移除此未保存草稿中的能力合同？'))return;if(!name){commit(key,undefined);return}const nextProperties={...properties(key)},nextSmoke={...smoke(key)};delete nextProperties[name];delete nextSmoke[name];commit(key,{...data(key),request_schema:{...schema(key),properties:nextProperties,required:requiredFields(key).filter(item=>item!==name)},smoke_inputs:nextSmoke})}
function enumValue(key:string,name:string,index:number,value:unknown){const values=Array.isArray(rule(key,name).enum)?[...rule(key,name).enum as unknown[]]:[];values[index]=value;editRule(key,name,'enum',values)}
const enumValues=(key:string,name:string):unknown[]=>Array.isArray(rule(key,name).enum)?rule(key,name).enum as unknown[]:[]
function repairRoot(){if(blocked.value||!window.confirm('仅修正 async_media 根协议结构并移除其未知根字段，保留各能力合同？'))return;emit('update:modelValue',{...spec.value,async_media:{schema_version:props.options!.schemaVersion,operations:operations.value}})}
</script>
<template>
  <section v-if="required||spec.async_media!==undefined" class="async-media-editor">
    <h3>异步媒体请求合同与 Smoke 样例</h3><p>{{ options?.description }}</p>
    <p>位置：模型版本 → 本表单（请求约束）；管理服务连接 → 请求字段映射（远程字段名）；执行实例 → 策略与运行参数（生产取值）。此处不配置响应解析器，也不注册新的接口能力。</p>
    <p v-if="!options" role="alert">目录未读取，编辑与保存已阻止。</p>
    <button v-if="Object.keys(media).some(key=>!['schema_version','operations'].includes(key))" type="button" :disabled="blocked" @click="repairRoot">修正根协议结构</button>
    <article v-for="key in rows" :key="key" :aria-label="`${key} 请求合同`">
      <header><h4>{{ key }}</h4><button type="button" :disabled="blocked||!options?.operations.some(row=>row.key===key)" @click="initialize(key)">{{ operations[key]===undefined?'创建能力合同':'重建此能力合同' }}</button></header>
      <p>产物类型：{{ data(key).output_kind??'尚未创建' }}（由能力固定）；新增合同不猜数值上限，不自动填写样例。</p>
      <p v-if="!caps.includes(key)||!options?.operations.some(row=>row.key===key)" role="alert">此合同未被选择或不受支持。<button type="button" :disabled="blocked" @click="remove(key)">移除此能力合同</button></p>
      <template v-if="operations[key]!==undefined">
        <div class="add-field"><label>添加标准字段<select v-model="selected[key]" :disabled="blocked"><option value="">请选择</option><option v-for="field in options?.fields.filter(row=>!fields(key).includes(row.key))" :key="field.key" :value="field.key">{{ field.label }} · {{ field.key }}</option></select></label><button type="button" :disabled="blocked||!selected[key]" @click="add(key)">添加字段</button></div>
        <details v-for="name in fields(key)" :key="name" open>
          <summary>{{ descriptor(name)?.label??'未注册字段' }} · {{ name }}</summary>
          <p>{{ descriptor(name)?.sourceDescription }} → 标准字段 <code>{{ name }}</code> → 远程字段 <code>{{ obj(requestFieldMap)[name]??'尚未映射（请检查服务连接／部署配置）' }}</code></p>
          <p>这里显示服务连接基线映射；模型部署覆盖及模型约束交集仍以执行实例检查为准。</p>
          <div class="controls">
            <label>类型<select :aria-label="`${key} ${name} 类型`" :value="rule(key,name).type??''" :disabled="blocked" @change="editRule(key,name,'type',($event.target as HTMLSelectElement).value)"><option value="">请选择</option><option v-for="kind in descriptor(name)?.types" :key="kind" :value="kind">{{ kind }}</option></select></label>
            <label>必填性<select :aria-label="`${key} ${name} 必填性`" :value="String(requiredFields(key).includes(name))" :disabled="blocked||name==='prompt'" @change="setRequired(key,name,($event.target as HTMLSelectElement).value==='true')"><option value="true">必填</option><option value="false">可选</option></select></label>
            <label v-for="bound in bounds(key,name)" :key="bound">{{ boundLabels[bound] }}<input type="number" :aria-label="`${key} ${name} ${bound}`" :aria-invalid="numbers.invalid(boundKey(key,name,bound))" :step="rule(key,name).type==='number'?'any':1" :min="bound==='maxLength'?1:bound==='minLength'?0:undefined" :max="['minLength','maxLength'].includes(bound)?options?.maximumStringLength:undefined" :disabled="blocked" :value="numbers.value(boundKey(key,name,bound),rule(key,name)[bound])" @input="inputBound(key,name,bound,$event)"/><button v-if="numbers.invalid(boundKey(key,name,bound))" type="button" :disabled="blocked" :aria-label="`撤销 ${key} ${name} ${boundLabels[bound]} 的未完成数字输入`" @click="numbers.discard(boundKey(key,name,bound))">撤销未完成数字输入（保留原配置）</button></label>
          </div>
          <p>留空表示未声明该边界；prompt 必须填写最大字符数。枚举是约束，不是默认值。</p>
          <div v-for="(value,index) in enumValues(key,name)" :key="index" class="enum-row"><AsyncScalarInput :label="`${key} ${name} 枚举 ${index+1}`" :type="String(rule(key,name).type)" :model-value="value" :disabled="blocked" @update:model-value="enumValue(key,name,index,$event)"/><button type="button" :disabled="blocked" @click="editRule(key,name,'enum',enumValues(key,name).filter((_,i)=>i!==index))">移除此枚举值</button></div>
          <button type="button" :disabled="blocked||!descriptor(name)?.types.includes(String(rule(key,name).type))" @click="editRule(key,name,'enum',[...enumValues(key,name),null])">添加枚举值</button> <button v-if="has(rule(key,name),'enum')" type="button" :disabled="blocked" @click="editRule(key,name,'enum',undefined)">取消枚举限制</button>
          <label class="smoke">Smoke 样例（仅测试材料）<AsyncScalarInput :label="`${key} ${name} Smoke 样例`" :model-value="smoke(key)[name]" :type="String(rule(key,name).type)" :choices="Array.isArray(rule(key,name).enum)?enumValues(key,name):undefined" :disabled="blocked" @update:model-value="setSmoke(key,name,$event)"/></label>
          <small>{{ has(smoke(key),name)?`已提供：${JSON.stringify(smoke(key)[name])}`:'尚未提供；空文本、0 和 false 均是明确的值' }}</small>
          <button v-if="has(smoke(key),name)" type="button" :disabled="blocked" @click="setSmoke(key,name,undefined)">移除样例值</button> <button v-if="name!=='prompt'" type="button" :disabled="blocked" @click="remove(key,name)">移除字段</button>
          <details><summary>核对当前字段规则</summary><pre>{{ JSON.stringify(rule(key,name),null,2) }}</pre></details>
        </details>
        <details><summary>核对此能力完整合同（只读）</summary><pre>{{ JSON.stringify(data(key),null,2) }}</pre></details>
      </template>
    </article>
    <ul v-if="errors.length" role="alert"><li v-for="error in errors" :key="error">{{ error }}</li></ul>
  </section>
</template>
<style scoped>
.async-media-editor{display:grid;gap:14px;min-width:0;padding:18px;border:1px solid var(--border-color,#354350);border-radius:8px}.async-media-editor article{min-width:0;border-top:1px solid var(--border-color,#354350);padding-top:12px}.async-media-editor header,.add-field{display:flex;flex-wrap:wrap;gap:12px;justify-content:space-between;align-items:center}.controls{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,170px),1fr));gap:12px}.async-media-editor label{display:grid;gap:6px;min-width:0;font-size:13px}.async-media-editor input,.async-media-editor select{width:100%;box-sizing:border-box;min-width:0;min-height:38px}.async-media-editor details{padding:12px 0;border-top:1px solid var(--border-color,#354350)}.async-media-editor summary{cursor:pointer;min-height:36px;overflow-wrap:anywhere}.async-media-editor p,.async-media-editor small{font-size:12px;line-height:1.6;color:var(--text-secondary,#96a5b3);overflow-wrap:anywhere}.async-media-editor pre,.async-media-editor code{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}.async-media-editor [role=alert]{color:var(--warning,#e9aa52);font-size:12px;overflow-wrap:anywhere}.async-media-editor h3,.async-media-editor h4{margin:0;overflow-wrap:anywhere}.enum-row{display:flex;gap:8px;margin:8px 0;flex-wrap:wrap}.enum-row>*{flex:1 1 180px}.smoke{margin-top:12px}.async-media-editor button{min-height:36px;max-width:100%;white-space:normal}
</style>
