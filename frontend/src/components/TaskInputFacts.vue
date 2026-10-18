<script setup lang="ts">
import { computed } from 'vue'
import {groupTaskParameters, summarizeTaskPrompts, type TaskDetail} from '@/services/taskCenter'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
import TaskMediaTrace from './TaskMediaTrace.vue'
import TaskApiMapping from './TaskApiMapping.vue'
import TaskNativeInputTrace from './TaskNativeInputTrace.vue'
import TaskSystemMessageTrace from './TaskSystemMessageTrace.vue'
const props=defineProps<{data:TaskDetail;reading:string}>()
const emit=defineEmits<{read:[field:string];copy:[text:string]}>()
const deferred=(field:string)=>props.data.deferred_fields.find(item=>item.field===field)
const stateLabels:Record<string,string>={frozen:'已冻结',not_frozen:'尚未冻结',error:'校验异常',deferred:'大文本待加载',recorded:'已记录',unavailable:'尚无记录'}
const readField=(field:string)=>emit('read',field)
const copy=(text:string)=>emit('copy',text)
const hasChatMessages=computed(()=>props.data.input.prompts.some(p=>p.source==='payload.messages'))
const promptSize=computed(()=>deferred('input.prompts')?null:summarizeTaskPrompts(props.data.input.prompts))
const promptLabel=(index:number)=>{const p=props.data.input.prompts[index];return `#${index+1} ${p.role} · ${p.key}`}
const parameters=computed(()=>groupTaskParameters(props.data.input.parameters))
const hasParameterConflicts=computed(()=>parameters.value.some(p=>p.values.length>1))
const trace=computed(()=>props.data.input.prompt_trace)
const fieldTrace=computed(()=>props.data.input.field_batch_trace)
const channelLabels:Record<string,string>={system_instruction:'系统约束',positive_prompt:'正向提示',negative_prompt:'负向提示'}
const traceLabels:Record<string,string>={matched:'与冻结请求对应层一致',mismatch:'与冻结请求对应层不一致',unavailable:'映射尚不可核对',omitted:'空通道未附加'}
</script>
<template>
 <section  class="detail-section">
 <p>请求：{{stateLabels[data.input.state]||data.input.state}}。下方展示保存的最终参数和提示词，不用当前模板重新生成；尚未派发的冻结请求不代表模型已收到。</p>
 <template v-if="data.input.state==='frozen' && data.input.notes.length>1">
 <p class="muted">{{data.input.notes[0]}}</p>
 <details class="prompt-trace protocol-notes"><summary>请求协议与配置归属（只读说明）</summary>
 <p v-for="(note,i) in data.input.notes.slice(1)" :key="i" class="muted">{{note}}</p>
 </details></template>
 <template v-else><p v-for="(note,i) in data.input.notes" :key="i" class="muted">{{note}}</p></template>
 <h4>核心参数 · 最终值与来源</h4>
 <p class="muted">同键同值合并展示，并保留各层记录来源；不推断别名或物理槽位映射。不同工作流节点的参数分别展示。</p>
 <p v-if="!deferred('input.parameters') && hasParameterConflicts" class="parameter-conflict" role="status">参数记录存在冲突：同一标识的记录值不一致，已逐项列出。页面不选择覆盖值，也不改变原任务状态；请核对冻结请求及槽位映射。</p>
 <button v-if="deferred('input.parameters')" class="button ghost small" :disabled="!!reading" @click="readField('input.parameters')">加载完整参数</button>
 <div v-else-if="parameters.length" class="table-scroll"><table><thead><tr><th>参数</th><th>已记录值</th><th>各层记录来源</th></tr></thead><tbody>
 <template v-for="(p,i) in parameters" :key="p.key+':'+i"><tr v-for="(variant,j) in p.values" :key="j">
 <th v-if="j===0" :rowspan="p.values.length" scope="rowgroup">{{p.key}}<span v-if="p.values.length>1" class="parameter-conflict conflict-label">记录值不一致</span></th>
 <td data-label="已记录值"><ReadonlyDataValue :value="variant.value" :label="p.key"/></td>
 <td data-label="各层记录来源" class="muted"><p v-for="(source,k) in variant.sources" :key="k" class="parameter-source">{{source}}</p></td>
 </tr></template>
 </tbody></table></div>
 <p v-else>没有可展示的已记录参数；不填入默认值。</p>
 <details v-if="data.input.api_mapping" class="prompt-trace"><summary>REST 标准字段 → 供应商字段（非送达证明）</summary>
 <TaskApiMapping :trace="data.input.api_mapping"/>
 </details>
 <details v-if="data.input.media_trace || deferred('input.media_trace')" class="prompt-trace"><summary>工作流接线与媒体回执</summary>
 <button v-if="deferred('input.media_trace')" class="button ghost small" :disabled="!!reading" @click="readField('input.media_trace')">加载完整工作流接线解释</button>
 <TaskMediaTrace v-else-if="data.input.media_trace" :trace="data.input.media_trace"/>
 </details>
 <details v-if="fieldTrace || deferred('input.field_batch_trace')" class="prompt-trace field-batch-trace"><summary>辅助字段批次 · 来源 → 字段合同 → 实际消息</summary>
 <button v-if="deferred('input.field_batch_trace')" class="button ghost small" :disabled="!!reading" @click="readField('input.field_batch_trace')">加载完整字段批次解释</button>
 <template v-else-if="fieldTrace"><p role="status">{{traceLabels[fieldTrace.state]}} · {{fieldTrace.message}}</p>
 <p v-if="fieldTrace.profile_id" class="muted">来源策略 {{fieldTrace.profile_id}} · 摘要 {{fieldTrace.profile_sha256}}<br/>批次摘要 {{fieldTrace.batch_sha256}}</p>
 <p v-if="fieldTrace.destination"><code>{{fieldTrace.destination}}</code></p>
 <dl class="trace-fields"><div v-for="f in fieldTrace.items" :key="f.request_id"><dt>{{f.field_key}} · {{f.generation_mode==='translate_only'?'翻译':'生成'}}<br/><small>请求 {{f.request_id}}<br/>单元 {{f.unit_key}}</small></dt><dd>
 <p>主要事实来源：<code>{{f.generation_source_selector}}</code> → <code>{{f.item_path}}.source_text</code> · {{f.source_characters}} 字符</p>
 <p>额外上下文：<template v-if="f.context_selectors.length"><code v-for="s in f.context_selectors" :key="s">{{s}}<br/></code>在原批次中按此来源标识查看；共享结构由 context_refs 引用 shared_facts，旧记录保留原 context_facts。</template><span v-else>明确无额外上下文</span></p>
 <p>来源语言 {{f.source_language||'明确未提供'}} → 目标语言 {{f.target_language||'明确未提供'}} · 策略 {{f.language_policy}}</p>
 <p>字段生成要求（{{f.item_path}}.field_contract.generation_instruction）：<span class="field-instruction">{{f.generation_instruction}}</span></p>
 <p>参考事实：保护文本 {{f.protected_count}} 项、逻辑输入 {{f.logical_input_count}} 项、主体事实 {{f.subject_fact_count}} 项；查看 {{f.item_path}}.reference_facts。若为 _ref 引用，正文保存在 shared_facts 对应条目中，同一单元的相同资料只发送一次。</p>
 <p>父策略声明的审核后主策略装配位置（不表示已采用）：<code>{{f.source_selector}}</code> → {{channelLabels[f.channel]||f.channel}} · 输出语义 {{f.generation_output_semantics}}</p>
 </dd></div></dl>
 <p class="muted">来源标识来自父策略冻结字段合同，不代表本次重新读取项目。正文只在下方原消息展示；位置均相对于该user JSON。返回按request_id对应字段，按原流程的人工或自动策略审核与采用；自动采用不记为人工确认。此处不是Smoke通过证明；字符数不是token或费用。</p></template>
 </details>
 <details v-else class="prompt-trace"><summary>来源 → 策略字段 → 通道 → 冻结请求</summary>
 <button v-if="deferred('input.prompt_trace')" class="button ghost small" :disabled="!!reading" @click="readField('input.prompt_trace')">加载完整提示词来源</button>
 <template v-else-if="trace"><p>{{trace.message}}</p><p v-if="trace.profile_id" class="muted">冻结策略 {{trace.profile_id}} · 摘要 {{trace.profile_sha256}}</p>
 <dl v-if="trace.fields.length" class="trace-fields"><div v-for="f in trace.fields" :key="f.field_key"><dt>{{f.field_key}} → {{channelLabels[f.channel]||f.channel}}</dt><dd>
 <p>来源标识：<code>{{f.source_selector}}</code></p>
 <p v-if="f.reviewed">实际采用本轮已审核的辅助生成／翻译字段值；不是直接读取第一层来源。</p><p v-else-if="f.defaulted">来源键缺失，实际采用本规则冻结的缺失默认值；不是字典全局默认。</p><p v-else-if="f.source_selector==='fixed.value'">实际采用本规则冻结的固定值；无需本次输入，不读取字典全局默认。</p>
 <template v-else><p v-for="(s,i) in f.sources" :key="i">输入 {{s.input_key}} · {{s.kind||'来源类型未记录'}}<br/>{{s.location||'填写位置未记录；请核对下方原始填写与解析结果'}}<br/>来源对象 {{s.source_id||'未记录'}} · {{s.edited===true?'确认时已编辑':s.edited===false?'确认时未编辑':'编辑状态未记录'}}<br/><small v-if="s.value_sha256">值摘要 {{s.value_sha256}}</small></p><p v-if="!f.sources.length">未记录可直接对应的输入来源；不按同名字段猜测。请核对原始填写与解析结果。</p></template>
 <p v-if="f.projection_primitive">来源转换：{{f.projection_primitive}}</p>
 </dd></div></dl>
 <article v-for="c in trace.channels" :key="c.channel" class="trace-channel"><strong>{{channelLabels[c.channel]||c.channel}} · {{c.characters}} 字符 · {{traceLabels[c.state]}}</strong><p v-if="c.destination"><code>{{c.destination}}</code></p><p>{{c.message}}</p></article>
 <p class="muted">字段装配记录不等于每个字段独立发送一次；多个字段可以合成一条通道。核对层以各项说明为准：文本消息、ComfyUI语义输入或REST标准体；不将语义／标准体一致当成物理端口、供应商映射或送达证明。适配器输出协议及其他业务输入仍需查看原请求；字符数不是token数或费用。</p></template>
 <p v-else>尚无可展示的冻结策略追溯；不会根据当前配置推断。</p>
 </details>
 <details v-if="data.input.system_message_trace" class="prompt-trace"><summary>system 消息分段来源（组装记录核对）</summary>
 <TaskSystemMessageTrace :trace="data.input.system_message_trace"/>
 </details>
 <details v-if="data.input.native_input_trace" class="prompt-trace"><summary>业务输入 → user 消息（原记录核对）</summary>
 <TaskNativeInputTrace :trace="data.input.native_input_trace"/>
 </details>
 <h4>实际提示词 · 最终请求正文</h4>
 <p v-if="hasChatMessages" class="muted">以下 messages 是同一次请求中的不同消息，不是多次模型调用。system 用于系统约束与输出格式；user 承载本次任务内容。同一内容是否重复发送，以各段保存的正文为准；历史请求不会按新配置重写。</p>
 <template v-if="deferred('input.prompts')"><p class="muted">提示词正文待完整加载，暂不计算正文总量或比较相同内容。</p><button class="button ghost small" :disabled="!!reading" @click="readField('input.prompts')">加载完整提示词</button></template>
 <template v-else>
 <div v-if="data.input.prompts.length && promptSize" class="prompt-size">
 <p>可查看正文合计：{{data.input.prompts.length}} 段 · {{promptSize.characters}} 字符 · {{promptSize.utf8Bytes}} UTF-8字节</p>
 <p class="muted">按下方安全过滤后的展示文本计算；字符按Unicode码点计数，字节不含角色、JSON包装或传输开销，也不代表过滤前原始请求大小、token数或费用。合计保留每段，不扣除相同内容。</p>
 <details v-if="promptSize.identicalGroups.length" class="prompt-trace"><summary>有 {{promptSize.identicalGroups.length}} 组展示正文整段相同</summary>
 <ul><li v-for="(indexes,i) in promptSize.identicalGroups" :key="i">{{indexes.map(promptLabel).join('；')}}</li></ul>
 <p class="muted">仅比较整段展示文本，包含空白且不归一化；不检查局部重复或语义相似。安全过滤可能让原本不同的正文显示相同；不同角色／通道也可能有意复用。这不是重复调用或可删除内容的判定，不改原任务状态和请求。</p>
 </details>
 </div>
 <article v-for="(p,i) in data.input.prompts" :key="p.key+':'+i" class="prompt-card"><header><strong>{{promptLabel(i)}}</strong><button class="button ghost small" @click="copy(p.text)">复制正文</button></header><small>{{p.source}} · {{promptSize?.rows[i]?.characters}} 字符 · {{promptSize?.rows[i]?.utf8Bytes}} UTF-8字节</small><pre tabindex="0">{{p.text===''?'（明确的空提示词）':p.text}}</pre></article><p v-if="!data.input.prompts.length">没有最终提示词记录：可能尚未冻结或此能力不使用提示词。不会用输入或模板代替。</p></template>
 <h4>原始填写与解析结果</h4>
 <template v-for="field in ['submitted','resolved']" :key="field"><button v-if="deferred('input.'+field)" class="button ghost small" :disabled="!!reading" @click="readField('input.'+field)">加载完整{{field==='submitted'?'原始填写':'解析结果'}}</button><ReadonlyDataValue v-else :value="data.input[field as 'submitted'|'resolved']" :label="field==='submitted'?'原始填写内容':'解析后的素材、输入与参数'"/></template>
 </section>

</template>
<style scoped>
.field-instruction{display:block;white-space:pre-wrap}
.prompt-size{min-width:0;overflow-wrap:anywhere}.prompt-size ul{margin:8px 0;padding-left:24px}.prompt-card header strong{min-width:0;overflow-wrap:anywhere}.prompt-card header button{flex-shrink:0}
.parameter-conflict{color:var(--warning,var(--accent))}.conflict-label{display:block;font-size:12px;margin-top:6px}.detail-section .parameter-source{font-size:12px;line-height:1.6}.parameter-source+.parameter-source{margin-top:8px}
.prompt-trace{min-width:0;overflow-wrap:anywhere}.prompt-trace summary{cursor:pointer;min-height:36px;align-content:center}.prompt-trace summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.trace-fields{margin:12px 0}.trace-fields>div{display:grid;grid-template-columns:minmax(120px,1fr) minmax(0,2fr);gap:12px;padding:10px 0;border-bottom:1px solid var(--border)}.trace-fields dt{font-weight:600}.trace-fields dd{margin:0;min-width:0}.trace-channel{margin-top:12px}.prompt-trace code{white-space:normal}@media(max-width:640px){.trace-fields>div{grid-template-columns:1fr;gap:6px}}
.detail-section{display:grid;gap:14px;min-width:0}.detail-section h4,.detail-section p{margin:0}.detail-section p{font-size:13px;line-height:1.8}.muted,small{font-size:12px;color:var(--text-secondary)}.prompt-card{border:1px solid var(--border);border-radius:8px;padding:14px;min-width:0}.prompt-card header{display:flex;align-items:center;justify-content:space-between;gap:10px}.prompt-card pre{max-height:420px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;line-height:1.9;margin:12px 0 0;background:var(--bg-input);padding:12px}.table-scroll{overflow:auto;max-height:440px}table{border-collapse:collapse;width:100%;font-size:12px}th,td{padding:12px;text-align:left;vertical-align:top;border-bottom:1px solid var(--border);overflow-wrap:anywhere;max-width:560px}
@media(max-width:640px){.table-scroll{max-height:none}table,tbody,tr,th,td{display:block;width:100%;box-sizing:border-box}thead{display:none}tr{padding:10px 0;border-bottom:1px solid var(--border)}th,td{border:0;padding:6px 0;max-width:none}td::before{content:attr(data-label);display:block;margin-bottom:6px;color:var(--text-secondary);font-size:12px}}
</style>
