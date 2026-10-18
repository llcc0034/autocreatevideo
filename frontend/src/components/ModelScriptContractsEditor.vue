<script setup lang="ts">
import {computed,nextTick,ref,watch} from 'vue'
import type {ChatRequestOptionsEditorOptions,JsonObject,ScriptContractEditorOptions,ScriptContractPolicyOption} from '@/services/platformTypes'
import {initializeScriptPolicy,scriptContractErrors,scriptObject as obj,scriptPolicyExtras} from '@/services/modelScriptContracts'
import {numericInputDrafts} from '@/services/numericInputDrafts'
import ModelChatRequestOptionsEditor from './ModelChatRequestOptionsEditor.vue'
import ConfigurationDiscardNotice from './ConfigurationDiscardNotice.vue'
const props=defineProps<{modelValue:unknown;options?:ScriptContractEditorOptions|null;chatOptions?:ChatRequestOptionsEditorOptions|null;disabled?:boolean}>()
const emit=defineEmits<{'update:modelValue':[value:JsonObject];validity:[errors:string[]]}>()
const spec=computed(()=>obj(props.modelValue))
const caps=computed(()=>Array.isArray(spec.value.capabilities)?spec.value.capabilities:[])
const rows=computed(()=>props.options?.policies.filter(row=>caps.value.includes(row.operation)||spec.value[row.key]!==undefined)||[])
const blocked=computed(()=>props.disabled||!props.options)
const numberKey=(key:string,name:string)=>JSON.stringify([key,'number',name])
const numbers=numericInputDrafts(()=>rows.value.flatMap(row=>row.numberFields.map(item=>({key:numberKey(row.key,item.key),label:`${row.label} ${item.label}`}))),()=>blocked.value)
const chatErrors=ref<Record<string,string[]>>({})
const errors=computed(()=>[...new Set([...scriptContractErrors(spec.value,props.options),...numbers.errors.value,...rows.value.flatMap(row=>chatErrors.value[row.key]||[])])])
watch(()=>JSON.stringify(errors.value),value=>emit('validity',JSON.parse(value)),{immediate:true})
const policy=(key:string)=>obj(spec.value[key])
const root=ref<HTMLElement|null>(null)
const pendingRemoval=ref<{key:string;name?:string;snapshot:string}|null>(null)
const removalSnapshot=computed(()=>JSON.stringify([props.modelValue,props.options,blocked.value]))
watch(removalSnapshot,()=>{pendingRemoval.value=null},{flush:'sync'})
const removalPath=computed(()=>pendingRemoval.value?`specification.${pendingRemoval.value.key}${pendingRemoval.value.name===undefined?'':`.${pendingRemoval.value.name}`}`:'')
function commit(key:string,value:unknown){if(!blocked.value){const next={...spec.value};if(value===undefined)delete next[key];else next[key]=value;emit('update:modelValue',next)}}
function initialize(row:ScriptContractPolicyOption){if(props.options)commit(row.key,initializeScriptPolicy(spec.value[row.key]))}
function number(key:string,name:string,raw:string){commit(key,{...policy(key),[name]:raw===''?undefined:Number(raw)})}
function inputNumber(key:string,name:string,event:Event){if(numbers.capture(numberKey(key,name),event))number(key,name,(event.target as HTMLInputElement).value)}
function remove(key:string,name?:string){
  if(blocked.value||pendingRemoval.value)return
  const row=rows.value.find(item=>item.key===key)
  if(!row||(name===undefined?caps.value.includes(row.operation):!scriptPolicyExtras(policy(key),row).includes(name)))return
  pendingRemoval.value={key,name,snapshot:removalSnapshot.value}
}
function confirmRemoval(){
  const pending=pendingRemoval.value
  pendingRemoval.value=null
  if(!pending||blocked.value||pending.snapshot!==removalSnapshot.value)return
  const {key,name}=pending
  if(name===undefined)commit(key,undefined)
  else commit(key,Object.fromEntries(Object.entries(policy(key)).filter(([field])=>field!==name)))
  void nextTick(()=>root.value?.focus({preventScroll:true}))
}
</script>
<template>
  <section v-if="rows.length||!options" ref="root" tabindex="-1" class="script-contracts-editor">
    <h3>剧本模型传输设置</h3><p>{{ options?.description }}</p>
    <ConfigurationDiscardNotice v-if="pendingRemoval" title="移除草稿配置确认" :message="`仅从当前未保存草稿移除 ${removalPath}。不会改变已发布版本；确认后仍需保存。选择“继续编辑”可取消。`" action-label="确认移除" :busy="Boolean(blocked)" @keep="pendingRemoval=null" @discard="confirmRemoval" />
    <p v-if="!options" role="alert">传输设置目录未读取，请重新读取系统设置。</p>
    <article v-for="row in rows" :key="row.key" :aria-label="`${row.label}模型传输设置`">
      <header><h4>{{ row.label }} <small>{{ row.operation }}</small></h4><button type="button" class="button ghost small" :disabled="blocked" @click="initialize(row)">初始化{{ row.label }}传输设置</button></header>
      <p><code>specification.{{ row.key }}</code> · 发布后冻结；等待时限须明确填写。字段和素材声明规则在所选业务输出版本维护，不在模型中重复配置。</p>
      <p v-if="!caps.includes(row.operation)">尚未勾选此能力，不会因保留设置而自动启用。<button type="button" :disabled="blocked" @click="remove(row.key)">移除未启用的设置</button></p>
      <div class="number-fields">
        <label v-for="item in row.numberFields" :key="item.key">{{ item.label }}（{{ item.unit }}）
          <input type="number" :aria-label="`${row.label} ${item.label}`" :aria-invalid="numbers.invalid(numberKey(row.key,item.key))" :step="item.integer?1:'any'" :min="item.minimum" :max="item.maximum??undefined" :value="numbers.value(numberKey(row.key,item.key),policy(row.key)[item.key])" :disabled="blocked" @input="inputNumber(row.key,item.key,$event)"/>
          <button v-if="numbers.invalid(numberKey(row.key,item.key))" type="button" :disabled="blocked" :aria-label="`撤销 ${row.label} ${item.label} 的未完成数字输入`" @click="numbers.discard(numberKey(row.key,item.key))">撤销未完成数字输入（保留原配置）</button>
          <small>{{ item.key }} · {{ item.exclusiveMinimum?'大于':'不小于' }} {{ item.minimum }}<template v-if="item.maximum!==null">，不超过 {{ item.maximum }}</template></small>
        </label>
      </div>
      <details v-for="extra in scriptPolicyExtras(policy(row.key),row)" :key="extra"><summary>已移除或未注册配置 {{ extra }}</summary><pre>{{ JSON.stringify(policy(row.key)[extra],null,2) }}</pre><button type="button" :disabled="blocked" @click="remove(row.key,extra)">从草稿移除此配置</button></details>
      <ModelChatRequestOptionsEditor :model-value="policy(row.key).chat_request_options" :options="chatOptions" :label="`${row.label}请求选项`" :source-path="`specification.${row.key}.chat_request_options`" :disabled="blocked" @update:model-value="commit(row.key,{...policy(row.key),chat_request_options:$event})" @validity="chatErrors[row.key]=$event"/>
    </article>
    <ul v-if="errors.length" role="alert"><li v-for="error in errors" :key="error">{{ error }}</li></ul>
  </section>
</template>
<style scoped>
.script-contracts-editor{display:grid;gap:14px;min-width:0;padding:18px;border:1px solid var(--border-color,#354350);border-radius:8px}
.script-contracts-editor button{min-height:36px;max-width:100%;white-space:normal}
.script-contracts-editor article{display:grid;gap:12px;min-width:0;border-top:1px solid var(--border-color,#354350);padding-top:12px}
.script-contracts-editor header{display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px;align-items:center}
.number-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,190px),1fr));gap:12px}
.script-contracts-editor label{display:grid;gap:6px;align-content:start;font-size:13px;min-width:0}
.script-contracts-editor input{min-width:0;width:100%;box-sizing:border-box;min-height:38px}
.script-contracts-editor small,.script-contracts-editor p{font-size:12px;line-height:1.6;color:var(--text-secondary,#96a5b3)}
.script-contracts-editor small{display:block}
.script-contracts-editor pre,.script-contracts-editor code{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}
.script-contracts-editor [role=alert]{color:var(--warning,#e9aa52);font-size:12px}
.script-contracts-editor h3,.script-contracts-editor h4{margin:0}
.script-contracts-editor summary{cursor:pointer;min-height:36px}
</style>
