<script setup lang="ts">
import {computed,ref} from 'vue'
import type {Asset} from '@/types'
import {moveMediaItem,sourcePolicyErrors,type InputSourceCatalog,type MediaSourcePolicy,type MediaSourceSelection,type MediaSourceItem} from '@/services/inputSourcePolicies'
import ExecutionInputAssetPicker from './ExecutionInputAssetPicker.vue'
import StructureMediaValue from './StructureMediaValue.vue'

const props=defineProps<{modelValue:MediaSourceSelection|null;policy:MediaSourcePolicy;catalog:InputSourceCatalog;
  mode:'configure'|'request'|'input';supportedSources:string[];disabled?:boolean;
  selectedOutputs?:{label:string;decision_id:string;artifact_id:string}[]}>()
const emit=defineEmits<{'update:modelValue':[MediaSourceSelection];busy:[boolean]}>()
const contextLocked=computed(()=>props.mode==='request'&&!props.policy.allow_override||sourcePolicyErrors(props.policy,props.catalog).length>0)
const locked=computed(()=>Boolean(props.disabled)||contextLocked.value)
const pendingUploads=new Map<string,string>(),uploadNotice=ref('')
const uploadContext=(key:string)=>JSON.stringify([props.policy,props.modelValue?.source,props.modelValue?.items.find(row=>row.item_key===key)])
function busy(key:string,value:boolean){
  if(value){pendingUploads.set(key,uploadContext(key));uploadNotice.value=''}else pendingUploads.delete(key)
  emit('busy',pendingUploads.size>0)
}
const options=computed(()=>props.catalog.sources.filter(row=>row.kinds.includes(props.policy.media_kind)))
const usable=(key:string)=>options.value.some(row=>row.value===key&&row.available)
  &&props.policy.allowed_sources.includes(key)&&props.supportedSources.includes(key)
  &&(props.mode==='request'||props.mode==='input'&&props.policy.allow_override||key===props.policy.default_source)
const editable=computed(()=>!locked.value&&!!props.modelValue&&usable(props.modelValue.source))
function commit(value:MediaSourceSelection){if(!locked.value)emit('update:modelValue',value)}
function changeSource(source:string){
  if(locked.value||!usable(source)||props.modelValue?.source===source)return
  if(props.modelValue?.items.length&&!window.confirm('切换来源会清除此处选择，不会删除素材库文件。是否继续？'))return
  commit({source,items:[]})
}
function add(){if(editable.value&&props.modelValue!.items.length<props.policy.max_items)
  commit({...props.modelValue!,items:[...props.modelValue!.items,{item_key:crypto.randomUUID(),purpose:null,
    ...(props.modelValue!.source==='selected_output'?{decision_id:'',artifact_id:''}:{asset_id:''})}]})}
function edit(key:string,changes:Partial<MediaSourceItem>){if(editable.value)commit({...props.modelValue!,items:props.modelValue!.items.map(row=>row.item_key===key?{...row,...changes}:row)})}
function select(key:string,source:string,kind:string,asset:Asset){
  if(pendingUploads.has(key)&&pendingUploads.get(key)!==uploadContext(key)){
    uploadNotice.value='素材已入库，但上传期间此项或来源策略已改变，未回填当前选择；请到素材库核对结果。';return
  }
  // A parent's busy lock stops new edits, not the exact upload already in flight.
  if(contextLocked.value||!props.modelValue||!usable(source)||props.modelValue.source!==source||props.policy.media_kind!==kind
    ||props.disabled&&!pendingUploads.has(key)||!props.modelValue.items.some(row=>row.item_key===key))return
  emit('update:modelValue',{...props.modelValue,items:props.modelValue.items.map(row=>row.item_key===key?{...row,asset_id:asset.id}:row)})
}
function output(key:string,identity:string){
  const row=props.selectedOutputs?.find(value=>JSON.stringify([value.decision_id,value.artifact_id])===identity)
  if(row)edit(key,{decision_id:row.decision_id,artifact_id:row.artifact_id})
}
function move(index:number,offset:number){if(editable.value)commit(moveMediaItem(props.modelValue!,index,offset))}
function remove(key:string){if(editable.value)commit({...props.modelValue!,items:props.modelValue!.items.filter(row=>row.item_key!==key)})}
</script>
<template>
  <section class="media-selection" aria-label="媒体来源与有序素材">
    <p v-if="uploadNotice" role="alert">{{uploadNotice}}</p>
    <p v-if="mode==='request'&&!policy.allow_override">本次不可改选，保留配置的确切选择；缺失或失效需修改所属配置，不会自动换图。</p>
    <label>本组取值来源<select :value="modelValue?.source||''" :disabled="locked" @change="changeSource(($event.target as HTMLSelectElement).value)">
      <option value="">请选择（默认方式：{{catalog.sources.find(row=>row.value===policy.default_source)?.label||'未配置'}}）</option>
      <option v-if="modelValue&&!options.some(row=>row.value===modelValue!.source)" :value="modelValue.source" disabled>未登记来源：{{modelValue.source}}</option>
      <option v-for="row in options" :key="row.value" :value="row.value" :disabled="!usable(row.value)">{{row.label}}{{!row.available?' · 尚未实现':!supportedSources.includes(row.value)?' · 此入口未接入':!policy.allowed_sources.includes(row.value)?' · 未允许':mode==='configure'&&row.value!==policy.default_source?' · 非默认来源':''}}</option>
    </select></label>
    <p v-if="modelValue&&!usable(modelValue.source)" role="alert">当前来源在此入口不可用。原选择保留，不能视为已成功装配。</p>
    <p>允许 {{Number.isFinite(policy.min_items)?policy.min_items:'未填写'}}–{{Number.isFinite(policy.max_items)?policy.max_items:'未填写'}} 项；按下列顺序装配。移动只改变顺序，不改变使用标识或按素材ID去重。</p>
    <ol v-if="modelValue">
      <li v-for="(item,index) in modelValue.items" :key="item.item_key">
        <div class="row-actions"><strong>第 {{index+1}} 项</strong><button type="button" class="button ghost small" :disabled="!editable||index===0" @click="move(index,-1)">上移</button><button type="button" class="button ghost small" :disabled="!editable||index===modelValue.items.length-1" @click="move(index,1)">下移</button><button type="button" class="button ghost small" :disabled="!editable" @click="remove(item.item_key)">移除此选择</button></div>
        <template v-if="['asset_library','asset_upload'].includes(modelValue.source)">
          <p v-if="item.asset_id===''">尚未选择正式素材；不自动采用示例或默认图。</p>
          <StructureMediaValue v-else :model-value="{asset_id:item.asset_id}" :schemas="[{'x-media-types':[policy.media_kind]}]" label="所选素材" readonly/>
          <ExecutionInputAssetPicker v-if="!contextLocked&&usable(modelValue.source)" :key="JSON.stringify([item.item_key,modelValue!.source,policy.media_kind])" :disabled="!editable" :kind="policy.media_kind" :purpose="item.purpose" :allow-library="modelValue!.source==='asset_library'" :allow-upload="modelValue!.source==='asset_upload'" @selected="(asset,source)=>select(item.item_key,source,policy.media_kind,asset)" @busy="busy(item.item_key,$event)"/>
        </template>
        <label v-else-if="modelValue.source==='selected_output'">已明确选择的上游产物<select :disabled="!editable" :value="JSON.stringify([item.decision_id,item.artifact_id])" @change="output(item.item_key,($event.target as HTMLSelectElement).value)"><option :value="JSON.stringify([item.decision_id,item.artifact_id])">{{selectedOutputs?.some(row=>row.decision_id===item.decision_id&&row.artifact_id===item.artifact_id)?'当前确切产物':'原引用未在当前目录／尚未选择'}}</option><option v-for="row in selectedOutputs" :key="JSON.stringify([row.decision_id,row.artifact_id])" :value="JSON.stringify([row.decision_id,row.artifact_id])">{{row.label}}</option></select></label>
        <label v-if="policy.allowed_purposes.length">本项用途<select :disabled="!editable" :value="item.purpose||''" @change="edit(item.item_key,{purpose:($event.target as HTMLSelectElement).value||null})"><option value="">请选择用途</option><option v-if="item.purpose&&!policy.allowed_purposes.includes(item.purpose)" :value="item.purpose" disabled>原用途不可用：{{item.purpose}}</option><option v-for="purpose in policy.allowed_purposes" :key="purpose" :value="purpose">{{purpose}}</option></select></label>
        <p v-else>用途：{{item.purpose===null?'明确未声明':item.purpose+'（不在允许目录）'}}</p>
        <details><summary>确切来源引用</summary><pre>{{JSON.stringify(item,null,2)}}</pre></details>
      </li>
    </ol>
    <p v-if="!modelValue?.items.length">尚无素材选择；不会从默认图、同名素材或示例中补齐。</p>
    <button type="button" class="button ghost small" :disabled="!editable||modelValue!.items.length>=policy.max_items" @click="add">添加素材使用项</button>
  </section>
</template>
<style scoped>
.media-selection{display:grid;gap:12px;min-width:0}.media-selection label{display:grid;gap:6px;min-width:0}.media-selection p{margin:0;font-size:12px;overflow-wrap:anywhere}.media-selection ol{margin:0;padding-left:24px;min-width:0}.media-selection li{padding:12px 0;display:grid;gap:10px;border-bottom:1px solid var(--border)}.row-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.media-selection pre{white-space:pre-wrap;overflow-wrap:anywhere}.media-selection select{min-width:0;max-width:100%;min-height:36px}.media-selection [role=alert]{color:var(--warning,#e9aa52)}.media-selection :is(button,select,summary):focus-visible{outline:2px solid var(--accent-color,#e4ad60);outline-offset:2px}
</style>
