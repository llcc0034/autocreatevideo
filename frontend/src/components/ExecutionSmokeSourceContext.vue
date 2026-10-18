<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { executionRegistryApi } from '@/services/executionRegistryApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { stageLabel } from '@/services/presentationLabels'
import type { SmokeSourceContext } from '@/services/executionSmokeSourceContext'

const props = defineProps<{versionId:string; disabled?:boolean}>()
const model = defineModel<SmokeSourceContext|null>({required:true})
const emit = defineEmits<{busy:[boolean]}>()
const rows=ref<SmokeSourceContext[]>([]), page=ref(1), count=ref(0), next=ref(false), previous=ref(false)
const loading=ref(false), error=ref(''), loaded=ref(false)
let sequence=0
function invalidate() { sequence++; rows.value=[]; model.value=null; loaded.value=false; error.value=''; loading.value=false; emit('busy',false) }
watch(()=>props.versionId,invalidate)
onBeforeUnmount(()=>{sequence++;emit('busy',false)})
async function load(target=1) {
  if(props.disabled||loading.value)return
  const token=++sequence; loading.value=true; emit('busy',true); error.value=''; model.value=null; rows.value=[]; loaded.value=false
  try {
    const data=await executionRegistryApi.smokeSourceContexts(props.versionId,target)
    if(token!==sequence)return
    rows.value=data.results; count.value=data.count; page.value=target; next.value=!!data.next; previous.value=!!data.previous; loaded.value=true
  } catch(cause) {if(token===sequence)error.value=apiErrorSummary(cause)}
  finally {if(token===sequence){loading.value=false;emit('busy',false)}}
}
function select(value:string) {
  if(props.disabled||loading.value)return
  model.value=rows.value.find(row=>row.run_id+'|'+row.stage_key===value)??null
}
</script>
<template>
  <fieldset class="smoke-source-context" :disabled="disabled || loading">
    <legend>已审核输出的来源上下文</legend>
    <p>只读取你有权访问的生产轮次。参考步骤用于核验前置审核范围；本次仍是独立烟测，不执行该步骤、不推进生产、不新增审核决定。</p>
    <button class="button ghost small" type="button" @click="load()">读取来源轮次与步骤</button>
    <p v-if="loading" role="status">正在读取来源选项…</p>
    <p v-if="error" role="alert">{{error}}</p>
    <template v-if="loaded">
      <p>共 {{count}} 项 · 第 {{page}} 页。这里只列身份，审核内容仍须在下方预览确认。</p>
      <p v-if="!rows.length" role="status">没有可选来源。请先在有权访问的项目轮次完成影视规则审核。</p>
      <label v-else>来源轮次／参考步骤<select :value="model ? model.run_id+'|'+model.stage_key : ''" @change="select(($event.target as HTMLSelectElement).value)"><option value="">请选择，不自动引用最新轮次</option><option v-for="row in rows" :key="row.run_id+'|'+row.stage_key" :value="row.run_id+'|'+row.stage_key">{{row.project_title}} · {{row.run_id.slice(0,8)}} · {{stageLabel(row.stage_key)}} · {{new Date(row.run_created_at).toLocaleString()}}</option></select></label>
      <div class="actions"><button class="button ghost small" type="button" :disabled="!previous" @click="load(page-1)">上一页</button><button class="button ghost small" type="button" :disabled="!next" @click="load(page+1)">下一页</button></div>
    </template>
  </fieldset>
</template>
<style scoped>
.smoke-source-context{display:grid;gap:10px;min-width:0}.smoke-source-context p{margin:0;overflow-wrap:anywhere}.smoke-source-context label{display:grid;gap:6px;min-width:0}.smoke-source-context select{width:100%;min-width:0}.actions{display:flex;gap:8px}
</style>
