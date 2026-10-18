<script setup lang="ts">
import {computed,onBeforeUnmount,ref,watch} from 'vue'
import type {JsonObject,VisualRulesOutputOptions} from '@/services/platformTypes'
import {previewVisualRulesOutput,type VisualRulesOutputPreview,type OutputPreviewVersion} from '@/services/visualRulesOutputPreview'
import {apiErrorSummary} from '@/services/apiErrorPresentation'
import ConfigurationDiscardNotice from './ConfigurationDiscardNotice.vue'

const props=defineProps<{options:VisualRulesOutputOptions;qualityAdvisories?:JsonObject;allowedValues?:JsonObject;fieldSources?:JsonObject;responseKeys?:JsonObject;outputVersion?:OutputPreviewVersion;disabled?:boolean}>()
const responseText=ref(''),busy=ref(false),error=ref(''),result=ref<VisualRulesOutputPreview|null>(null)
const replaceOpen=ref(false)
let epoch=0
const fingerprint=computed(()=>JSON.stringify([responseText.value,props.qualityAdvisories,props.allowedValues,props.fieldSources,props.responseKeys,props.options,props.disabled,props.outputVersion]))
function invalidate(){epoch++;result.value=null;error.value=''}
watch(fingerprint,invalidate,{flush:'sync'})
onBeforeUnmount(()=>epoch++)
function example(){if(props.disabled||busy.value||replaceOpen.value)return;if(responseText.value){replaceOpen.value=true;return}applyExample()}
function applyExample(){if(props.disabled||busy.value)return;replaceOpen.value=false;responseText.value=JSON.stringify(props.options.responseExample,null,2)}
async function validateSample(){
  if(busy.value||props.disabled||replaceOpen.value||!responseText.value.trim())return
  invalidate()
  const requestEpoch=epoch
  busy.value=true
  try{
    const report=await previewVisualRulesOutput(responseText.value,props.qualityAdvisories,props.outputVersion,props.allowedValues,props.fieldSources,props.responseKeys)
    if(epoch!==requestEpoch)return
    const fields=props.options.outputs.map(row=>row.field)
    if(report.responseSchemaVersion!==props.options.responseSchemaVersion||
      (report.valid&&(!report.candidate||Object.keys(report.candidate).length!==fields.length||fields.some(field=>!Object.prototype.hasOwnProperty.call(report.candidate,field)))))throw new Error('校验结果与已读取输出目录不一致，请刷新后再试')
    result.value=report
  }catch(cause){if(epoch===requestEpoch)error.value=apiErrorSummary(cause)}
  finally{busy.value=false}
}
</script>
<template>
  <section class="output-preview">
    <p>仅校验粘贴的响应与{{outputVersion?'指定版本的已保存合同和建议':'当前未保存草稿的允许取值与建议'}}，并预览所选输出字段的转换内容。不调用模型、不保存正文、不产生费用，也不创建候选、批准或 Smoke 通过记录。通过仅表示格式有效，不代表内容符合创作预期。未保存草稿的格式示例仍来自目录，可按所选允许取值编辑样例后预检。</p>
    <p v-if="outputVersion">预检输出版本：{{outputVersion.id}} · 修订 {{outputVersion.revision}} · 摘要 {{outputVersion.sha256}}</p>
    <div class="actions"><button type="button" class="button ghost small" :disabled="disabled||busy" @click="example">填入格式示例（非生成结果）</button></div>
    <ConfigurationDiscardNotice v-if="replaceOpen" title="替换样例正文前请确认" message="将当前样例正文替换为所选协议的格式示例；不是模型生成结果，不改变输出定义或历史响应。" action-label="用格式示例替换正文" :busy="!!disabled||busy" @keep="replaceOpen=false" @discard="applyExample"/>
    <label>待校验的模型响应正文<textarea v-model="responseText" :disabled="disabled||replaceOpen" rows="10" spellcheck="false" placeholder="粘贴完整的 JSON 响应，不含 Markdown 代码围栏" /></label>
    <button type="button" class="button secondary" :disabled="disabled||busy||replaceOpen||!responseText.trim()" @click="validateSample">{{busy?'正在校验样例…':'校验输出样例（不调用模型）'}}</button>
    <p v-if="error" role="alert">{{error}}；样例正文已保留，可重试。</p>
    <section v-if="result" aria-live="polite">
      <strong>{{result.valid?(result.warnings.length?'结构有效，有质量提醒':'结构有效，尚未经人工审核'):'样例未通过输出校验'}}</strong>
      <ul v-if="result.errors.length" role="alert"><li v-for="item in result.errors" :key="item.code+item.message">{{item.message}} <code>{{item.code}}</code></li></ul>
      <ul v-if="result.warnings.length"><li v-for="warning in result.warnings" :key="warning">{{warning}}</li></ul>
      <template v-if="result.valid&&result.candidate">
        <p>下列均为临时预览，不能直接作为已审核来源使用。</p>
        <div v-for="row in options.outputs" :key="row.field" class="parsed-output">
          <strong>{{row.field}} · 转换预览</strong>
          <p>{{row.responseField}} → {{row.field}} → 人工审核采用后才可引用 {{row.sourceSelector}}</p>
          <pre>{{result.candidate[row.field]||'空字符串（允许无负面约束）'}}</pre>
        </div>
      </template>
    </section>
  </section>
</template>
<style scoped>
.output-preview{display:grid;gap:var(--config-space-md);min-width:0;font:var(--config-body-size)/1.6 var(--font)}.output-preview label{display:grid;gap:var(--config-space-xs)}.output-preview textarea{box-sizing:border-box;min-width:0;width:100%;resize:vertical;background:var(--bg-soft);color:var(--text);border:1px solid var(--border-strong);border-radius:var(--config-radius);padding:var(--config-space-sm);font:var(--config-body-size)/1.6 monospace}.output-preview :focus-visible{outline:2px solid var(--accent);outline-offset:2px}.output-preview p,.output-preview li{overflow-wrap:anywhere}.parsed-output{padding:var(--config-space-md) 0;border-top:1px solid var(--border)}.output-preview pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:320px;overflow:auto;font:var(--config-body-size)/1.7 monospace}.output-preview [role=alert]{color:var(--warning)}.output-preview button{justify-self:start;white-space:normal;min-height:var(--config-control-height);font-size:var(--config-body-size)}.actions{display:flex;flex-wrap:wrap}@media(max-width:640px){.output-preview button{min-height:44px}}
</style>
