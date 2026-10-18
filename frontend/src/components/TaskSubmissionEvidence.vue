<script setup lang="ts">
import {taskTime,type TaskSubmissionEvidence} from '@/services/taskCenter'
defineProps<{evidence:TaskSubmissionEvidence;sourceKind:string;sourceId:string|null}>()
const states={recorded:'已读取本地登记',unavailable:'提交证据不可用',inconsistent:'提交字段存在异常'}
const identifiers={recorded:'原记录中有请求标识（已隐藏）',digest_only:'仅保留请求标识摘要（已隐藏）',not_recorded:'尚无请求标识记录',unavailable:'不适用或无原调用',inconsistent:'请求标识与摘要无法核对'}
</script>
<template>
 <section class="submission-evidence" aria-label="当前调用的提交证据">
 <h4>提交证据 · {{states[evidence.state]}}</h4>
 <p :role="evidence.state==='inconsistent'?'alert':'status'">{{evidence.message}}</p>
 <dl><dt>当前选中调用</dt><dd>{{sourceKind}} · {{sourceId??'尚无原调用'}}</dd>
 <dt>本地提交边界时间</dt><dd>{{taskTime(evidence.started_at)}}</dd>
 <dt>提交登记时间</dt><dd>{{taskTime(evidence.registered_at)}}</dd>
 <dt>请求标识记录</dt><dd>{{identifiers[evidence.identifier_state]}}</dd></dl>
 <details><summary>这些记录能证明什么？</summary><p v-for="note in evidence.notes" :key="note">{{note}}</p></details>
 </section>
</template>
<style scoped>
.submission-evidence{min-width:0;overflow-wrap:anywhere}.submission-evidence dl{display:grid;grid-template-columns:minmax(120px,1fr) minmax(0,3fr);gap:8px}.submission-evidence dd{margin:0}.submission-evidence summary{cursor:pointer;min-height:36px;align-content:center}.submission-evidence summary:focus-visible{outline:2px solid var(--accent)}.submission-evidence [role=alert]{color:var(--danger)}@media(max-width:640px){.submission-evidence dl{grid-template-columns:1fr}.submission-evidence dt{font-weight:600}}
</style>
