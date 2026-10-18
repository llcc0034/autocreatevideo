<script setup lang="ts">
import {computed,defineAsyncComponent,ref,watch} from 'vue'
import type {JsonObject,VisualRulesOutputOptions} from '@/services/platformTypes'
import {visualRulesAdvisoryErrors,updateVisualRulesAdvisory} from '@/services/visualRulesOutput'
import type {OutputPreviewVersion} from '@/services/visualRulesOutputPreview'
import ConfigurationDiscardNotice from './ConfigurationDiscardNotice.vue'
const props=defineProps<{modelValue?:unknown;options?:VisualRulesOutputOptions|null;allowedValues?:JsonObject;fieldSources?:JsonObject;responseKeys?:JsonObject;disabled?:boolean;previewDisabled?:boolean;outputVersion?:OutputPreviewVersion;owner?:'output'}>()
const emit=defineEmits<{'update:modelValue':[value:JsonObject|undefined];validity:[errors:string[]]}>()
const VisualRulesOutputPreview=defineAsyncComponent(()=>import('./VisualRulesOutputPreview.vue'))
const previewLoaded=ref(false)
function onPreviewToggle(event:Event){if((event.target as HTMLDetailsElement).open)previewLoaded.value=true}
const object=(value:unknown):JsonObject=>value&&typeof value==='object'&&!Array.isArray(value)?value as JsonObject:{}
const contract=computed(()=>object(props.modelValue))
const directColor=computed(()=>props.fieldSources?.color_script==='color_script')
const numberFields=computed(()=>props.options?.numberFields.filter(field=>!directColor.value||!field.key.startsWith('base_colors_')||Object.prototype.hasOwnProperty.call(contract.value,field.key)))
const clearOpen=ref(false)
const blocked=computed(()=>props.disabled||!props.options||clearOpen.value)
const localError=ref(''),clauseDraft=ref('')
const errors=computed(()=>[...visualRulesAdvisoryErrors(props.modelValue,props.options),localError.value,...(clearOpen.value?['请先确认清除建议或继续编辑']:[]),...(clauseDraft.value?['新增建议短语尚未添加，请添加或清空该输入后保存']:[])].filter(Boolean))
watch(errors,value=>emit('validity',value),{immediate:true,deep:true})
const clauses=computed(()=>Array.isArray(contract.value.recommended_negative_clauses)?contract.value.recommended_negative_clauses as unknown[]:[])
function update(key:string,value:unknown) {
  if(blocked.value||!props.options)return
  try {const next=updateVisualRulesAdvisory(props.modelValue,props.options,key,value);localError.value='';emit('update:modelValue',next)}catch(error){localError.value=String(error)}
}
function number(key:string,raw:string){update(key,raw===''?undefined:Number(raw))}
function clause(index:number,text:string){const next=[...clauses.value];next[index]=text;update('recommended_negative_clauses',next)}
function addClause(){if(blocked.value)return;if(!clauseDraft.value.trim()){localError.value='请先填写一条建议短语';return}update('recommended_negative_clauses',[...clauses.value,clauseDraft.value]);clauseDraft.value=''}
function removeClause(index:number){const next=clauses.value.filter((_,i)=>i!==index);update('recommended_negative_clauses',next.length?next:undefined)}
function clear(){if(!blocked.value)clearOpen.value=true}
function confirmClear(){if(props.disabled||!props.options)return;clearOpen.value=false;localError.value='';clauseDraft.value='';emit('update:modelValue',undefined)}
</script>
<template>
  <section class="visual-output-editor">
    <header><strong>影视规则输出与质量建议</strong><button v-if="modelValue!==undefined" type="button" class="button ghost small" :disabled="blocked" @click="clear">清除此草稿的质量建议</button></header>
    <ConfigurationDiscardNotice v-if="clearOpen" title="清除质量建议前请确认" message="仅清除此草稿的可选质量建议及未添加短语；响应位置、允许取值、公共结构和已发布版本不受影响。" action-label="清除此草稿的质量建议" :busy="!!disabled" @keep="clearOpen=false" @discard="confirmClear"/>
    <p>质量建议未达到时，保留完整候选并提醒人工审核，不截断、不重试、不直接判失败。留空表示不启用该项建议；数量为 0 也是明确建议。</p>
    <fieldset :disabled="blocked"><legend>可选质量建议</legend><div class="advisory-grid">
      <label v-for="field in numberFields" :key="field.key">{{field.label}}（{{field.unit}}）<input type="number" step="1" :min="field.minimum" :max="field.maximum" :value="contract[field.key]??''" placeholder="未设置" @input="number(field.key,($event.target as HTMLInputElement).value)"/><small>{{field.key}}</small></label>
    </div></fieldset>
    <details><summary>建议补充的负面短语（{{clauses.length}} 条）</summary>
    <fieldset :disabled="blocked"><legend>逐条编辑建议短语</legend>
      <p>每行填写一条英文缺陷短语，不含逗号、分号、HTML或换行。与模型返回的负面约束合并去重；不是固定不可删除的正式规则，审核时仍可修改。</p>
      <div v-for="(item,index) in clauses" :key="index" class="clause-row"><label>建议短语 {{index+1}}<input :value="item" @input="clause(index,($event.target as HTMLInputElement).value)"/></label><button type="button" class="button ghost small" @click="removeClause(index)">移除短语 {{index+1}}</button></div>
      <div class="clause-row"><label>新增建议短语<input v-model="clauseDraft" @keydown.enter.prevent="addClause"/></label><button type="button" class="button ghost small" :disabled="blocked" @click="addClause">添加短语</button></div>
    </fieldset></details>
    <details><summary>模型返回 → 人工审核 → 下游来源</summary>
      <p>下表展示当前所选映射（结构版本 {{options?.responseSchemaVersion??'未读取'}}），不是第二份 Schema。业务输出版本可选择连续性说明或要点数组、结构化色彩或英文说明、负面约束数组或逗号分隔短语，也可启用镜头指导文字，并配置对象字段或固定数组位置。转换后仍须人工审核；任意新字段、动态数组筛选、通配符及解析脚本尚不支持。</p>
      <div v-for="row in options?.outputs" :key="row.field" class="output-row"><strong>{{row.label}}</strong><p><code>{{row.responseField}}</code> → {{row.field}}</p><p>{{row.transformation}}</p><p>经人工审核采用后，可在策略中选择：<code>{{row.sourceSelector}}</code>。选择不代表已读取或批准内容，准备时仍需确认项目、轮次和具体审核版本。</p></div>
      <p v-if="directColor">英文色彩说明按原文、安全文本和长度建议校验，不应用结构化色彩枚举或基础色数量建议。</p>
      <details v-else><summary>色彩枚举与安全边界（平台维护）</summary><p>返回字段的类型、唯一性、合法枚举、英文安全文本及响应绝对字节上限仍需校验；与上方质量建议不同，不能通过人工确认让非法结构成为合法响应。</p><dl><template v-for="(values,key) in options?.colorChoices" :key="key"><dt>{{key}}</dt><dd>{{values.join('、')}}</dd></template></dl></details>
    </details>
    <details @toggle="onPreviewToggle"><summary>输出样例校验（不调用模型）</summary>
      <VisualRulesOutputPreview v-if="previewLoaded&&options" :options="options" :field-sources="outputVersion?undefined:fieldSources" :response-keys="outputVersion?undefined:responseKeys" :allowed-values="outputVersion?undefined:allowedValues" :quality-advisories="outputVersion?undefined:modelValue===undefined?undefined:contract" :output-version="outputVersion" :disabled="(previewDisabled??blocked)||errors.length>0"/>
    </details>
    <p v-if="owner==='output'">建议保存在此业务输出版本中。执行实例明确引用已发布版本后随请求冻结；不会改写旧请求或自动采用结果。</p>
    <p v-else>模型快照中的建议仅为已有配置事实；新的全剧影视规则执行需在“业务输出版本”配置并由执行实例明确引用，不再从模型读取这些建议。</p>
    <ul v-if="errors.length" role="alert"><li v-for="error in errors" :key="error">{{error}}</li></ul>
  </section>
</template>
<style scoped>
.visual-output-editor{display:grid;gap:var(--config-space-lg);min-width:0;font:var(--config-body-size)/1.6 var(--font);letter-spacing:0}.visual-output-editor header{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:var(--config-space-sm)}.visual-output-editor header>strong{font-size:var(--config-section-size)}.visual-output-editor fieldset{min-width:0;margin:0;padding:var(--config-space-md);border:1px solid var(--border);border-radius:var(--config-radius)}.advisory-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--config-space-md)}.visual-output-editor label{display:grid;gap:var(--config-space-xs);min-width:0}.visual-output-editor input{box-sizing:border-box;width:100%;min-width:0;min-height:var(--config-control-height);border:1px solid var(--border-strong);border-radius:var(--config-radius);background:var(--bg-soft);color:var(--text);padding:var(--config-space-sm);font:inherit}.visual-output-editor :focus-visible{outline:2px solid var(--accent);outline-offset:2px}.visual-output-editor p,.visual-output-editor small{margin:var(--config-space-xs) 0;color:var(--text-secondary);line-height:1.6}.visual-output-editor small{font-size:var(--config-help-size)}.visual-output-editor p,.visual-output-editor small,.visual-output-editor dt,.visual-output-editor dd{overflow-wrap:anywhere}.clause-row{display:flex;gap:var(--config-space-md);align-items:end;flex-wrap:wrap;margin:var(--config-space-sm) 0}.clause-row label{flex:1 1 180px}.output-row{padding:var(--config-space-md) 0;border-bottom:1px solid var(--border)}.visual-output-editor summary{cursor:pointer;min-height:var(--config-control-height);align-content:center}.visual-output-editor dd{margin:var(--config-space-xs) 0 var(--config-space-lg)}.visual-output-editor [role=alert]{color:var(--warning)}.visual-output-editor button{font-size:var(--config-body-size);white-space:normal}@media(max-width:640px){.advisory-grid{grid-template-columns:1fr}.visual-output-editor{--config-control-height:44px}.visual-output-editor button{min-height:44px}}
</style>
