<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { useRouter, onBeforeRouteLeave } from 'vue-router'
import { executionRegistryApi } from '@/services/executionRegistryApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import type { ExecutionEvidence } from '@/services/executionEvidence'
import { taskCenterTarget, taskTime } from '@/services/taskCenter'
import PlatformStatusPill from './PlatformStatusPill.vue'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
import ExecutionSmokeCreateForm from './ExecutionSmokeCreateForm.vue'
const props=defineProps<{versionId:string;canCreate:boolean}>()
const emit=defineEmits<{configuration:[];busy:[value:boolean];dirty:[value:boolean]}>()
const router=useRouter(),result=ref<ExecutionEvidence|null>(null),loading=ref(false),error=ref(''),creating=ref(false),busy=ref(false),dirty=ref(false)
let serial=0
const formLocked=ref(false)
watch(()=>busy.value||formLocked.value,v=>emit('busy',v),{flush:'sync'})
watch(dirty,v=>emit('dirty',v))
function canLeave(){return !busy.value&&!formLocked.value&&(!dirty.value||window.confirm('离开将丢弃尚未保存的烟测输入，继续吗？'))}
onBeforeRouteLeave(canLeave)
async function load(){const n=++serial;loading.value=true;error.value=''
try{const r=await executionRegistryApi.evidence(props.versionId,1);if(n!==serial)return;if(r.summary.identity.versionId!==props.versionId)throw Error('验收摘要与版本不一致');result.value=r}
catch(e){if(n===serial)error.value=apiErrorSummary(e)}finally{if(n===serial)loading.value=false}}
function history(){if(canLeave()){dirty.value=false;void router.push(taskCenterTarget({category:'smoke',versionId:props.versionId}))}}
function closeCreate(){if(canLeave()){creating.value=false;dirty.value=false}}
function created(id:string){creating.value=false;dirty.value=false;busy.value=false;formLocked.value=false;void router.push(taskCenterTarget({category:'smoke',versionId:props.versionId,kind:'preparation',id}))}
watch(()=>props.versionId,()=>{result.value=null;void load()},{immediate:true})
onBeforeUnmount(()=>serial++)
</script>
<template>
<section class="execution-evidence">
 <p>这里管理此精确版本的验收与新建烟测。输入、实际提示词、输出及历史调用统一在任务中心查看。</p>
 <div class="evidence-actions"><button class="button ghost small" :disabled="loading" @click="load">刷新验收摘要</button><button class="button ghost small" :disabled="busy||formLocked||dirty" @click="$emit('configuration')">查看组合配置</button><button class="button ghost small" :disabled="busy||formLocked" @click="history">查看输入、输出与烟测历史</button><button v-if="canCreate&&!creating" class="button primary small" @click="creating=true">新建真实烟测</button></div>
 <p v-if="error" class="platform-alert danger" role="alert">{{error}}<span v-if="result"> · 下方为上次读取的摘要，尚未更新。</span></p>
 <section v-if="creating" class="evidence-summary"><button class="button ghost small" :disabled="busy||formLocked" @click="closeCreate">收起新建烟测</button><ExecutionSmokeCreateForm :version-id="versionId" @busy="busy=$event" @dirty="dirty=$event" @locked="formLocked=$event" @created="created"/></section>
 <section v-if="result" class="evidence-summary"><strong>最近完成的组合验收 <PlatformStatusPill :status="result.summary.evidenceState"/></strong><p>执行能力：{{result.summary.identity.operation}} · {{result.summary.identity.mode||'未声明模式'}}</p><p>完成：{{taskTime(result.summary.finishedAt)}} · 到期：{{taskTime(result.summary.expiresAt)}}</p><p>读取时间：{{taskTime(result.summary.readAt)}} · 共 {{result.count}} 次记录</p><p>通过表示记录的程序验收通过，不代表每次请求都可直接执行，也不代替人工核对内容质量。</p><details><summary>精确关联与准入策略</summary><dl><dt>模型部署</dt><dd>{{result.summary.deploymentId}}</dd><dt>工作流版本</dt><dd>{{result.summary.workflowVersionId??'不使用工作流'}}</dd><dt>提示词策略版本</dt><dd>{{result.summary.promptProfileVersionId??'不使用策略版本'}}</dd><dt>组合摘要</dt><dd>{{result.summary.identity.definitionSha256}}</dd></dl><ReadonlyDataValue :value="result.summary.policies" label="读取时的准入策略与来源"/></details></section>
</section>
</template>
<style scoped>
.execution-evidence{display:grid;gap:14px;min-width:0}.execution-evidence p{font-size:13px;line-height:1.8;color:var(--text-secondary);overflow-wrap:anywhere}.evidence-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.evidence-summary{padding:16px;border:1px solid var(--border);border-radius:8px;min-width:0}.execution-evidence dl{display:grid;grid-template-columns:140px 1fr;gap:10px;font-size:12px}.execution-evidence dd{margin:0;overflow-wrap:anywhere}
</style>
