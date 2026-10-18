<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { executionPromptApi, type ExecutionPromptDisplay } from '@/services/executionPromptDisplay'
import { apiErrorSummary } from '@/services/apiErrorPresentation'

const props = defineProps<{ preparationId: string; revision: number; frozen: boolean; disabled: boolean; fieldLabels: Record<string, string> }>()
const result = ref<ExecutionPromptDisplay | null>(null), loading = ref(false), error = ref(''), opened = ref(false)
let serial = 0
function reset() { serial++; result.value = null; error.value = ''; loading.value = false; opened.value = false }
watch(() => [props.preparationId, props.revision, props.frozen], reset)
onBeforeUnmount(() => { serial++ })
async function load() {
  if (props.disabled || loading.value) return
  const request = ++serial, id = props.preparationId, revision = props.revision
  opened.value = true; loading.value = true; result.value = null; error.value = ''
  try {
    const value = await executionPromptApi.get(id, revision)
    if (request !== serial) return
    if ((value.mode === 'frozen') !== props.frozen) throw new Error('请求冻结状态已变化，请先刷新准备状态')
    result.value = value
  } catch (cause) { if (request === serial) error.value = apiErrorSummary(cause) }
  finally { if (request === serial) loading.value = false }
}
const channelLabel = (key: string) => ({ positive_prompt: '正向提示词', negative_prompt: '负向提示词', system_instruction: '系统约束', structured_input: '结构化字段' }[key] || key)
const messageLabel = (role: ExecutionPromptDisplay['messages'][number]['role']) => ({ system: '系统消息', user: '用户消息', assistant: '助手消息' }[role])
</script>

<template>
  <section class="complete-prompt" aria-label="完整提示词与系统组装">
    <h4>完整提示词与系统组装</h4>
    <p>按本次准备的提示词类型展示完整内容；冻结请求读取保存原文，策略预览标明字段来源。</p>
    <button type="button" class="button ghost" :disabled="disabled || loading" :aria-expanded="opened" @click="opened ? (opened = false) : load()">
      {{ loading ? '正在读取完整提示词…' : opened ? '收起完整提示词' : frozen ? '查看最终冻结的完整提示词' : '预览完整提示词与系统组装' }}
    </button>
    <template v-if="opened">
      <p v-if="loading" role="status">正在核对本次准备来源和修订…</p>
      <div v-if="error" class="platform-alert danger" role="alert">{{ error }}<button type="button" class="button ghost small" :disabled="disabled || loading" @click="load">重新读取完整提示词</button></div>
      <template v-if="result">
        <div class="prompt-state" :class="result.mode" role="status">
          <strong>{{ result.contentKind === 'messages' ? '冻结文本请求 · 实际请求保存内容' : result.contentKind === 'not_applicable' ? '提示词不适用 · 本次冻结请求无需提示词' : result.mode === 'frozen' ? '最终冻结提示词 · 实际请求保存内容' : result.mode === 'diagnostic' ? '组装诊断 · 未通过校验，不可提交' : result.mode === 'preview' ? '完整提示词预览 · 尚未冻结，不代表已发送' : '暂不能生成完整预览' }}</strong>
          <p v-if="result.mode === 'frozen'">直接读取本次保存的请求原文，不随新修订或配置变化重组。冻结不等于已发送，实际调用状态请看任务记录。</p>
          <p v-else-if="result.promptMode === 'profile'">优先使用已采用修订；未采用字段仅取最新可预览候选。查看不会确认、采用或提交；字段审核与采用按本次配置处理，最终请求以冻结内容为准。</p>
          <p v-if="result.contentKind === 'messages'">按保存顺序展示消息原文与其中的结构化协议；本次无需提示词通道。</p>
          <p v-if="result.contentKind === 'not_applicable'">本次为无提示词的非聊天请求，没有提示词通道或聊天消息可展示。</p>
          <p v-if="result.mode === 'diagnostic'">下方保留触发超限的完整组装原文，没有截断，也没有放宽限制；其他提交校验尚未完成。字符数包含全部字段、系统组装内容、标签和分隔符，不是单个导演描述的长度。</p>
          <ul v-if="result.issues.length"><li v-for="issue in result.issues" :key="issue">{{ issue }}</li></ul>
        </div>
        <div v-if="result.promptMode === 'profile'" class="prompt-sources">
          <section><h5>冻结的参考关系</h5><p v-if="!result.sources.references.length">本准备未记录参考素材绑定。</p>
            <ul v-else><li v-for="(row, index) in result.sources.references" :key="index"><strong>{{ row.label }}</strong> · {{ row.name || row.category }}<small v-if="row.guidance">{{ row.guidance }}</small></li></ul>
          </section>
          <section><h5>冻结的原对白</h5><p v-if="!result.sources.dialogue.length">本准备没有原对白记录。</p>
            <ul v-else><li v-for="(row, index) in result.sources.dialogue" :key="index"><strong>{{ row.subject }}</strong> · {{ row.startMs / 1000 }}–{{ row.endMs / 1000 }} 秒 · {{ row.voiceover ? '画外音' : '普通台词' }} · 本句语言：{{ row.language?.trim() || '未声明' }}<span v-if="row.delivery"> · {{ row.delivery }}</span><p class="dialogue-text">{{ row.text }}</p></li></ul>
          </section>
        </div>
        <section v-if="result.fieldBasis.length"><h5>本次组装使用的字段版本</h5><ul class="field-basis"><li v-for="row in result.fieldBasis" :key="JSON.stringify([row.unitKey,row.fieldKey])">
          {{ fieldLabels[row.fieldKey] || row.fieldKey }} · 修订 {{ row.revision }} · <strong>{{ row.origin === 'adopted' ? '已采用' : '仅用于预览，尚未采用' }}</strong>
        </li></ul></section>
        <section v-for="channel in result.channels" :key="channel.key" class="prompt-channel"><h5>{{ channelLabel(channel.key) }} · 完整原文 · {{ channel.characterCount.toLocaleString() }} 字符</h5>
          <template v-for="length in result.lengthDiagnostics.filter(row => row.channel === channel.key)" :key="length.channel">
            <p :class="{ 'length-exceeded': length.overBy > 0 }">通道合计 {{ length.characterCount.toLocaleString() }} 字符 · <template v-if="length.maxChars !== null">生效上限 {{ length.maxChars.toLocaleString() }} 字符</template><template v-else>通道上限未配置</template><span v-if="length.overBy"> · 超出 {{ length.overBy.toLocaleString() }} 字符</span></p>
            <details v-if="length.limitSources.length" class="limit-sources"><summary>上限来源</summary><ul><li v-for="source in length.limitSources" :key="source.source">{{ source.source }} · <template v-if="source.maxChars !== null">{{ source.maxChars.toLocaleString() }} 字符</template><template v-else>未配置</template></li></ul></details>
            <details v-if="length.fragments.length" class="length-breakdown" open><summary>组装长度明细（含标签，分隔符单列）</summary>
              <ol><li v-for="(fragment, index) in length.fragments" :key="index">
                <div class="fragment-counts"><strong>{{ fragment.label || fieldLabels[fragment.fieldKey] || fragment.fieldKey }}</strong><span>正文与标签 {{ fragment.textChars.toLocaleString() }} 字符 · 分隔符 {{ fragment.separatorChars.toLocaleString() }} 字符 · 字段上限 {{ fragment.maxChars.toLocaleString() }} 字符</span></div>
                <details class="fragment-proof"><summary>字段版本与来源</summary><dl><dt>字段标识</dt><dd>{{ fragment.fieldKey }}</dd><dt>字段定义版本 ID</dt><dd>{{ fragment.fieldDefinitionVersionId }}</dd><dt>来源选择器</dt><dd>{{ fragment.sourceSelector }}</dd></dl></details>
              </li></ol>
            </details>
          </template>
          <pre v-if="channel.text.length" class="prompt-body" tabindex="0">{{ channel.text }}</pre><p v-else>{{ result.mode === 'frozen' ? '此通道保存为空文本。' : '此通道组装为空文本。' }}</p>
        </section>
        <section v-for="(message, index) in result.messages" :key="index" class="prompt-channel">
          <h5>{{ messageLabel(message.role) }} · 完整原文 · {{ message.characterCount.toLocaleString() }} 字符</h5>
          <pre v-if="message.content.length" class="prompt-body" tabindex="0">{{ message.content }}</pre><p v-else>此消息保存为空文本。</p>
        </section>
        <details class="prompt-proof"><summary>策略确切版本、来源与请求指纹</summary><p>准备修订 {{ result.preparationRevision }}</p>
          <dl v-if="result.profileBasis"><dt>本次策略版本</dt><dd>v{{ result.profileBasis.version }}</dd><dt>策略版本 ID</dt><dd>{{ result.profileBasis.versionId }}</dd><dt>策略合同 SHA256</dt><dd>{{ result.profileBasis.contractSha256 }}</dd></dl>
          <p v-else>{{ result.mode === 'unavailable' ? '本次无法提供策略依据和长度诊断。' : '本次冻结请求未使用策略组装，无策略长度诊断。' }}</p>
          <p v-if="result.requestSha256">请求 SHA256：{{ result.requestSha256 }}</p><p v-if="result.frozenAt">冻结时间：{{ new Date(result.frozenAt).toLocaleString() }}</p>
          <ul><li v-for="row in result.fieldBasis" :key="row.fieldValueId">{{ row.unitKey }} / {{ row.fieldKey }} · {{ row.fieldValueId }}</li></ul>
        </details>
      </template>
    </template>
  </section>
</template>

<style scoped>
.complete-prompt { border:1px solid var(--border-color, #354149); border-radius:10px; padding:16px; min-width:0; margin:16px 0; }
h4,h5 { margin:0 0 10px; } h5 { font-size:inherit; }
p { line-height:1.6; } .prompt-state { margin:16px 0; padding:12px; border-left:3px solid #cf8d38; background:rgba(207,141,56,.08); }
.prompt-state.frozen { border-color:#6eb4a0; } .prompt-state p { margin-bottom:0; }
.prompt-sources { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; margin-bottom:16px; }
.prompt-sources section { padding:12px; border:1px solid var(--border-color,#354149); border-radius:8px; }
ul { padding-left:20px; margin:0; } li { line-height:1.7; overflow-wrap:anywhere; } small { display:block; }
.dialogue-text { white-space:pre-wrap; margin:4px 0 10px; }
.prompt-channel { margin-top:18px; min-width:0; }
.prompt-body { white-space:pre-wrap; overflow-wrap:anywhere; font:inherit; line-height:1.75; padding:14px; border:1px solid var(--border-color,#354149); background:rgba(0,0,0,.14); user-select:text; }
.prompt-proof { margin-top:16px; overflow-wrap:anywhere; }
.prompt-state.diagnostic { border-color:var(--danger, #df7777); }
.length-exceeded { color:var(--danger, #df7777); }
.length-breakdown { margin:12px 0; }
.length-breakdown ol { padding:8px 0; list-style:none; margin:0; }
.length-breakdown li { padding:8px 0; border-bottom:1px solid var(--border-color,#354149); }
.fragment-counts { display:flex; justify-content:space-between; gap:8px; flex-wrap:wrap; }
.fragment-proof { margin-top:4px; }
summary { min-height:36px; cursor:pointer; overflow-wrap:anywhere; }
dl { margin:8px 0; } dt { font-weight:600; } dd { margin:2px 0 10px; overflow-wrap:anywhere; }
@media(max-width:700px) { .complete-prompt { padding:12px; } .prompt-sources { grid-template-columns:1fr; } .button { white-space:normal; } }
</style>
