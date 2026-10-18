<script setup lang="ts">
import {computed,ref,watch} from 'vue'
import type {JsonObject,MediaConstraintsEditorOptions} from '@/services/platformTypes'
import {fixedResolutionFields,mediaConstraintsErrors,updateFixedResolution,updateMediaConstraint} from '@/services/modelMediaConstraints'
const props=defineProps<{modelValue?:unknown;options?:MediaConstraintsEditorOptions|null;disabled?:boolean}>()
const emit=defineEmits<{'update:modelValue':[value:JsonObject|undefined];validity:[errors:string[]]}>()
const object=(v:unknown):JsonObject=>v&&typeof v==='object'&&!Array.isArray(v)?v as JsonObject:{}
const contract=computed(()=>object(props.modelValue))
const blocked=computed(()=>props.disabled||!props.options)
const localError=ref(''), fpsDraft=ref<string|number>('')
const fixedFields=computed(()=>fixedResolutionFields(props.options))
const fixedDraft=ref<{width:string;height:string}|null>(null)
const numericDrafts=ref<Record<string,{raw:string;label:string}>>({})
const incompleteNumbers=computed(()=>Object.values(numericDrafts.value))
const errors=computed(()=>[...mediaConstraintsErrors(props.modelValue,props.options),localError.value,
  ...incompleteNumbers.value.map(draft=>`${draft.label}尚未输入完整数字；原配置保留，请修正或撤销后再保存`),
  ...(fixedDraft.value?['固定宽高尚未应用，请应用或取消后再保存模型草稿']:[])].filter(Boolean))
watch(errors,value=>emit('validity',value),{immediate:true,deep:true})
watch(()=>JSON.stringify(props.modelValue),()=>{
  if(fixedDraft.value){fixedDraft.value=null;localError.value='媒体约束已变化，固定宽高暂存输入已取消，请重新打开设置'}
},{flush:'sync'})
const value=(group:string,key:string)=>object(contract.value[group])[key]
const rates=computed(()=>Array.isArray(value('fps','allowed'))?value('fps','allowed') as unknown[]:[])
function startFixed() {
  if(blocked.value||!fixedFields.value||incompleteNumbers.value.length)return
  const draft={width:'',height:''}
  for(const field of fixedFields.value){const lower=value('resolution',field.minimumKey),upper=value('resolution',field.maximumKey)
    if(typeof lower==='number'&&Number.isSafeInteger(lower)&&lower>0&&lower===upper)draft[field.dimension]=String(lower)
  }
  localError.value='';fixedDraft.value=draft
}
function cancelFixed(){if(!blocked.value){fixedDraft.value=null;localError.value=''}}
function applyFixed(){
  if(blocked.value||!props.options||!fixedDraft.value)return
  try {
    const next=updateFixedResolution(props.modelValue,props.options,Number(fixedDraft.value.width),Number(fixedDraft.value.height))
    fixedDraft.value=null;localError.value='';emit('update:modelValue',next)
  }catch(error){localError.value=String(error)}
}
function update(group:'resolution'|'duration_ms'|'fps',key:string,next:unknown) {
  if(blocked.value||fixedDraft.value||!props.options)return false
  try {const result=updateMediaConstraint(props.modelValue,props.options,group,key,next);localError.value='';emit('update:modelValue',result);return true} catch(error){localError.value=String(error);return false}
}
function number(group:'resolution'|'duration_ms',key:string,raw:string) {update(group,key,raw===''?undefined:Number(raw))}
function rate(index:number,raw:string) {const next=[...rates.value];next[index]=raw===''?null:Number(raw);update('fps','allowed',next)}
function completeNumber(path:string,label:string,event:Event) {
  if(blocked.value||fixedDraft.value)return false
  const input=event.target as HTMLInputElement
  if(input.validity.badInput){numericDrafts.value[path]={raw:input.value,label};return false}
  delete numericDrafts.value[path]
  return true
}
function numberInput(group:'resolution'|'duration_ms',key:string,label:string,event:Event) {
  if(completeNumber(`${group}.${key}`,label,event))number(group,key,(event.target as HTMLInputElement).value)
}
function rateInput(index:number,event:Event) {
  if(completeNumber(`fps.${index}`,`帧率 ${index+1}`,event))rate(index,(event.target as HTMLInputElement).value)
}
const numericValue=(group:string,key:string,fallback:unknown)=>numericDrafts.value[`${group}.${key}`]?.raw??fallback??''
function discardIncompleteNumbers(){if(!blocked.value&&!fixedDraft.value)numericDrafts.value={}}
function addRate() {if(blocked.value)return;if(!String(fpsDraft.value).trim()){localError.value='请填写要添加的帧率';return};update('fps','allowed',[...rates.value,Number(fpsDraft.value)]);fpsDraft.value=''}
function removeRate(index:number) {
  if(!update('fps','allowed',rates.value.filter((_,i)=>i!==index)))return
  numericDrafts.value=Object.fromEntries(Object.entries(numericDrafts.value).flatMap(([path,draft])=>{
    if(!path.startsWith('fps.'))return [[path,draft]]
    const position=Number(path.slice(4))
    return position===index?[]:[[position>index?`fps.${position-1}`:path,{...draft,label:`帧率 ${position>index?position:position+1}`}]]
  }))
}
function clear() {if(!blocked.value&&!fixedDraft.value&&window.confirm('清除当前草稿的尺寸、帧率与时长约束？已发布版本和历史请求不受影响。')){localError.value='';numericDrafts.value={};emit('update:modelValue',undefined)}}
</script>
<template>
  <section class="media-constraints-editor">
    <header><strong>尺寸、帧率与时长</strong><button v-if="modelValue!=null" type="button" class="button ghost small" :disabled="blocked||!!fixedDraft" @click="clear">清除此草稿的媒体约束</button></header>
    <p>声明模型实际能力，不是本次生成参数或预算提醒。留空表示此项未声明，不能据此认定模型无限制；工作流约束仍会共同校验。</p>
    <section aria-label="固定宽高快捷设置" class="fixed-resolution">
      <button v-if="!fixedDraft" type="button" class="button ghost small" :disabled="blocked||!fixedFields||!!incompleteNumbers.length" @click="startFixed">设置固定宽高</button>
      <p v-if="!fixedFields">宽高成对字段目录未就绪，不能使用快捷设置。</p>
      <template v-if="fixedDraft">
        <p>宽高各填一次，应用后写入下方同一组最小值与最大值；不会修改倍数、边长、帧率或时长。范围不相等时不预选任一端点。</p>
        <div class="media-grid"><label v-for="field in fixedFields" :key="field.dimension">固定{{field.dimension==='width'?'宽度':'高度'}}（{{field.unit}}）<input v-model="fixedDraft[field.dimension]" type="number" step="1" :min="field.minimum" :max="field.maximum" :disabled="blocked" placeholder="请明确填写" @keydown.enter.prevent="applyFixed"/></label></div>
        <div class="fixed-actions"><button type="button" class="button ghost small" :disabled="blocked" @click="applyFixed">应用固定宽高到草稿</button><button type="button" class="button ghost small" :disabled="blocked" @click="cancelFixed">取消固定宽高设置</button></div>
      </template>
      <p>这是模型能力声明，不是本次生成尺寸；实际请求仍须通过模型与工作流的共同校验。应用只改本地草稿，保存并发布后才可被新执行组合引用。</p>
    </section>
    <fieldset :disabled="blocked||!!fixedDraft"><legend>分辨率</legend><div class="media-grid">
      <label v-for="field in options?.resolutionFields" :key="field.key"><span>{{field.label}}（{{field.unit}}）</span><input type="number" step="1" :min="field.minimum" :max="field.maximum" :value="numericValue('resolution',field.key,value('resolution',field.key))" :aria-invalid="!!numericDrafts['resolution.'+field.key]" placeholder="未声明" @input="numberInput('resolution',field.key,field.label,$event)"/><small>{{field.key}}</small></label>
    </div><p>范围内须至少存在一组满足倍数及长短边上限的宽高；范围端点不必为倍数。冲突时请调整声明，系统不会自动取整或修改尺寸。</p></fieldset>
    <fieldset :disabled="blocked||!!fixedDraft"><legend>允许的帧率（帧/秒）</legend>
      <p v-if="!rates.length">尚未声明帧率范围。</p>
      <div class="rate-row" v-for="(item,index) in rates" :key="index"><label>帧率 {{index+1}}<input type="number" step="1" :min="options?.fps.minimum" :max="options?.fps.maximum" :value="numericValue('fps',String(index),item)" :aria-invalid="!!numericDrafts['fps.'+index]" @input="rateInput(index,$event)"/></label><button type="button" class="button ghost small" @click="removeRate(index)">移除帧率 {{index+1}}</button></div>
      <div class="rate-row"><label>新增帧率<input v-model="fpsDraft" type="number" step="1" :min="options?.fps.minimum" :max="options?.fps.maximum" @keydown.enter.prevent="addRate"/></label><button type="button" class="button ghost small" :disabled="blocked||rates.length >= (options?.fps.maximumItems ?? 0)" @click="addRate">添加帧率</button></div>
    </fieldset>
    <fieldset :disabled="blocked||!!fixedDraft"><legend>时长范围</legend><div class="media-grid"><label v-for="field in [{key:'minimum',label:'最小时长'},{key:'maximum',label:'最大时长'}]" :key="field.key">{{field.label}}（毫秒）<input type="number" step="1" :min="options?.durationMs.minimum" :max="options?.durationMs.maximum" :value="numericValue('duration_ms',field.key,value('duration_ms',field.key))" :aria-invalid="!!numericDrafts['duration_ms.'+field.key]" placeholder="未声明" @input="numberInput('duration_ms',field.key,field.label,$event)"/></label></div><p>1秒＝1000毫秒；两项同时填写或同时留空。最小值等于最大值表示固定时长。</p></fieldset>
    <p>只修改当前模型版本草稿；发布后，执行实例需明确引用此版本。Projector 的节点接线不能覆盖或放宽这里的模型能力。</p>
    <ul v-if="errors.length" role="alert"><li v-for="error in errors" :key="error">{{error}}</li></ul>
    <button v-if="incompleteNumbers.length" type="button" class="button ghost small" :disabled="blocked||!!fixedDraft" @click="discardIncompleteNumbers">撤销未完成数字输入（保留原配置）</button>
  </section>
</template>
<style scoped>
.media-constraints-editor{display:grid;gap:14px;min-width:0;padding:18px;border:1px solid var(--border-color,#354350);border-radius:8px}.media-constraints-editor header{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}.media-constraints-editor fieldset{min-width:0;margin:0;padding:12px;border:1px solid var(--border-color,#354350);border-radius:6px}.media-constraints-editor legend{padding:0 6px}.media-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.media-constraints-editor label{display:grid;gap:6px;min-width:0}.media-constraints-editor input{box-sizing:border-box;width:100%;min-width:0;min-height:36px}.media-constraints-editor p,.media-constraints-editor small{margin:6px 0;color:var(--text-secondary,#96a5b3);font-size:12px;line-height:1.6}.media-constraints-editor small{overflow-wrap:anywhere}.rate-row{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin:8px 0}.rate-row label{flex:1 1 180px}.media-constraints-editor [role=alert]{color:var(--warning,#e9aa52);font-size:12px}@media(max-width:620px){.media-grid{grid-template-columns:1fr}.media-constraints-editor{padding:12px}}
.media-constraints-editor input{border:1px solid #303743;border-radius:7px;background:#070a0e;color:#f0f3f7;padding:9px 10px;font:inherit}
.media-constraints-editor input:focus-visible{outline:2px solid #e3a350;outline-offset:2px}
.media-constraints-editor input:disabled{opacity:.6;cursor:not-allowed}
.fixed-resolution{min-width:0}.fixed-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}
</style>
