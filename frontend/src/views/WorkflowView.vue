<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import BaseModal from '@/components/BaseModal.vue'
import PlatformStatusPill from '@/components/PlatformStatusPill.vue'
import TaskCenterChoice from '@/components/TaskCenterChoice.vue'
import { taskCenterApi } from '@/services/taskCenterApi'
import { pageCursor, taskCenterTarget, taskTime, type TaskChoice, type TaskFilters, type TaskKind, type TaskPage, type TaskRow } from '@/services/taskCenter'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
const TaskCenterDetail=defineAsyncComponent(()=>import('@/components/TaskCenterDetail.vue'))
const route=useRoute(),router=useRouter(),result=ref<TaskPage|null>(null),loading=ref(false),error=ref(''),optionsError=ref('')
const choices=ref<Record<string,TaskChoice[]>>({}),filtersOpen=ref(false),busy=ref(false),dirty=ref(false)
const draft=ref({status:'',project_id:'',execution_instance_version_id:'',operation:'',created_after:'',created_before:'',anomaly:''})
const str=(key:string)=>typeof route.query[key]==='string'?route.query[key] as string:''
const category=computed(()=>str('category'))
const selected=computed(()=>['preparation','generation_task','assembly'].includes(str('kind'))&&str('id')?{kind:str('kind') as TaskKind,id:str('id'),attemptId:str('attempt_id')}:null)
const categories=computed(()=>[{value:'',label:'全部任务'},...(choices.value.categories||[])])
const query=computed(()=>Object.fromEntries(['category','status','project_id','production_run_id','stage_run_id','execution_instance_version_id','operation','created_after','created_before','anomaly','source_id','cursor'].filter(k=>str(k)!=='').map(k=>[k,str(k)])) as TaskFilters)
let serial=0,timer:ReturnType<typeof setTimeout>|undefined,disposed=false
function allowed(){return !busy.value&&(!dirty.value||window.confirm('离开将放弃尚未保存的字段，继续吗？'))}
function polling(){clearTimeout(timer);if(disposed||error.value)return;timer=setTimeout(()=>{if(!document.hidden&&!selected.value)void load();else polling()},10000)}
async function load(){const n=++serial;loading.value=true;error.value=''
 try{const rows=await taskCenterApi.list(query.value);if(n===serial)result.value=rows}
 catch(e){if(n===serial)error.value=apiErrorSummary(e)}finally{if(n===serial){loading.value=false;polling()}}}
async function loadOptions(){optionsError.value='';try{const r=await taskCenterApi.options();if(!disposed){const {pagination,...values}=r;choices.value=values}}catch(e){if(!disposed)optionsError.value=apiErrorSummary(e)}}
function open(row:TaskRow){void router.push({name:'workflows',query:{...route.query,kind:row.kind,id:row.id,attempt_id:undefined}})}
function close(){if(!allowed())return;busy.value=false;dirty.value=false;const {kind,id,attempt_id,...rest}=route.query;void router.push({name:'workflows',query:rest})}
function apply(){const value:Record<string,string>={};for(const[k,v]of Object.entries(draft.value)){if(v)value[k]=k.startsWith('created_')?new Date(v).toISOString():v}
 for(const key of ['production_run_id','stage_run_id','source_id'])if(str(key))value[key]=str(key)
 if(category.value)value.category=category.value;void router.push({name:'workflows',query:value})}
function chooseCategory(value:string){const {cursor,kind,id,attempt_id,...q}=route.query;void router.push({name:'workflows',query:{...q,category:value||undefined}})}
function move(url:string|null){if(url)void router.push({name:'workflows',query:{...route.query,cursor:pageCursor(url)}})}
function select(kind:TaskKind,id:string,attemptId?:string){void router.push({name:'workflows',query:{...route.query,kind,id,attempt_id:attemptId}})}
function selectSource(id:string){void router.push(taskCenterTarget({sourceId:id}))}
function syncDraft(){for(const key of Object.keys(draft.value) as (keyof typeof draft.value)[]){let value=str(key);if(key.startsWith('created_')&&value){const d=new Date(value);if(!Number.isNaN(d.valueOf())){d.setMinutes(d.getMinutes()-d.getTimezoneOffset());value=d.toISOString().slice(0,16)}}draft.value[key]=value}}
watch(()=>JSON.stringify(query.value),()=>{syncDraft();void load()},{immediate:true})
watch(()=>selected.value?.id,()=>{busy.value=false;dirty.value=false})
onBeforeRouteLeave(()=>allowed())
onBeforeRouteUpdate((to,from)=>to.query.id!==from.query.id||to.query.kind!==from.query.kind||to.query.attempt_id!==from.query.attempt_id?allowed():true)
onBeforeUnmount(()=>{disposed=true;serial++;clearTimeout(timer)})
void loadOptions()
</script>
<template>
<section class="task-center">
 <header class="task-center-header"><div><p class="eyebrow">EXECUTION HISTORY</p><h1>任务中心</h1><p>统一查看项目生产、素材生成和 Smoke；输入、实际提示词、输出及辅助调用均可追溯。</p></div><button class="button ghost" :disabled="loading" @click="load">刷新列表</button></header>
 <nav class="task-categories" aria-label="任务类别"><button v-for="c in categories" :key="c.value" class="button ghost" :class="{active:category===c.value}" :aria-pressed="category===c.value" @click="chooseCategory(c.value)">{{c.label}} <span>{{result?.counts[(c.value||'all') as 'all'|'production'|'asset'|'smoke']??'—'}}</span></button></nav>
 <p v-if="optionsError" class="platform-alert" role="alert">筛选选项未加载：{{optionsError}} <button class="button ghost small" @click="loadOptions">重试选项</button></p>
 <div class="filter-toolbar"><button class="button ghost small" :aria-expanded="filtersOpen" @click="filtersOpen=!filtersOpen">{{filtersOpen?'收起筛选':'筛选任务'}}</button><span v-if="str('source_id')">当前原任务：{{str('source_id')}}</span><span v-if="str('execution_instance_version_id')">已按精确执行版本筛选</span><button class="button ghost small" @click="router.push({name:'workflows'})">清除筛选</button></div>
 <div v-if="str('production_run_id') || str('stage_run_id')" class="production-task-scope" aria-label="生产任务筛选范围"><span v-if="str('production_run_id')">生产轮次：{{str('production_run_id')}}</span><span v-if="str('stage_run_id')">步骤 ID：{{str('stage_run_id')}}</span><p>仅显示实际归属此范围的任务；辅助调用在原任务详情内。继续筛选、翻页和刷新保留此范围，“清除筛选”才解除。任务成功不代表内容已审核或采用。</p></div>
 <form v-if="filtersOpen" class="task-filters" @submit.prevent="apply">
 <TaskCenterChoice v-model="draft.project_id" domain="projects" label="所属项目"/><TaskCenterChoice v-model="draft.execution_instance_version_id" domain="execution_versions" label="执行实例版本"/>
 <label>任务状态<select v-model="draft.status"><option value="">全部状态</option><option v-for="c in choices.statuses||[]" :key="c.value" :value="c.value">{{c.label}}</option></select></label>
 <label>执行能力<select v-model="draft.operation"><option value="">全部能力</option><option v-for="c in choices.operations||[]" :key="c.value" :value="c.value">{{c.label}}</option></select></label>
 <label>创建时间从<input v-model="draft.created_after" type="datetime-local"/></label><label>创建时间至<input v-model="draft.created_before" type="datetime-local"/></label>
 <label class="anomaly-filter"><input v-model="draft.anomaly" type="checkbox" true-value="true" false-value=""/>仅看关联异常</label><button class="button primary small" :disabled="loading">查询</button>
 </form>
 <p v-if="error" class="platform-alert" role="alert">{{error}}<span v-if="result"> · 下方为上次读取记录，当前筛选结果未更新。</span></p>
 <div class="list-meta" role="status"><span>{{loading?'正在读取当前页…':result?'共 '+result.count+' 项':'尚未读取'}}</span><span v-if="result">读取于 {{taskTime(result.read_at)}} · 列表只读，不自动生成或重试</span></div>
 <div class="task-table-wrap"><table class="task-table"><thead><tr><th>任务 / 类别</th><th>执行实例 / 能力</th><th>状态</th><th>创建 / 完成</th><th>实际费用</th><th>详情</th></tr></thead><tbody>
 <tr v-for="row in result?.results||[]" :key="row.kind+row.id"><td data-label="任务 / 类别"><strong>{{row.title}}</strong><small>{{categories.find(c=>c.value===row.category)?.label||row.category}} · {{row.project_title||'无项目归属'}}</small><small v-if="row.anomaly" class="danger">关联异常</small></td><td data-label="执行实例 / 能力">{{row.execution_instance_name||'无业务实例关联'}}<span v-if="row.execution_version!==null"> · v{{row.execution_version}}</span><small>{{row.operation||row.source_kind}}</small></td><td data-label="状态"><PlatformStatusPill :status="row.status"/></td><td data-label="创建 / 完成">{{taskTime(row.created_at)}}<small>{{taskTime(row.finished_at)}}</small></td><td data-label="实际费用">{{row.actual_cost??'未知'}} {{row.cost_currency}}</td><td data-label="详情"><button class="button ghost small" @click="open(row)">输入 / 输出与记录</button></td></tr>
 <tr v-if="result&&!result.results.length"><td colspan="6">当前筛选没有任务。可清除筛选查看其他类别；不会用历史缓存补齐。</td></tr>
 </tbody></table></div>
 <nav class="list-pagination" aria-label="任务分页"><button class="button ghost small" :disabled="loading||!result?.previous" @click="move(result?.previous||null)">上一页</button><span>每页最多 25 项 · 按创建时间倒序</span><button class="button ghost small" :disabled="loading||!result?.next" @click="move(result?.next||null)">下一页</button></nav>
 <BaseModal :open="!!selected" title="任务详情" width="1280px" :dismissible="!busy" @close="close"><TaskCenterDetail v-if="selected" :key="selected.kind+selected.id+selected.attemptId" :kind="selected.kind" :id="selected.id" :attempt-id="selected.attemptId" @busy="busy=$event" @dirty="dirty=$event" @changed="load" @select="select" @source="selectSource"/></BaseModal>
</section>
</template>
<style scoped>
.production-task-scope{display:grid;gap:6px;min-width:0;font-size:12px;color:var(--text-secondary);overflow-wrap:anywhere}.production-task-scope p{margin:0;line-height:1.6}
.task-center{display:grid;gap:18px;min-width:0;padding:28px}.task-center-header,.task-categories,.filter-toolbar,.list-meta,.list-pagination{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.task-center-header,.list-meta{justify-content:space-between}.task-center h1{font-size:28px;margin:4px 0 10px}.task-center-header p,.filter-toolbar,.list-meta,.list-pagination{font-size:12px;color:var(--text-secondary)}.eyebrow{color:var(--accent)!important;letter-spacing:2px}.task-categories .active{border-color:var(--accent);color:var(--accent)}.task-categories span{background:var(--bg-input);border-radius:12px;padding:2px 8px;font-size:12px}.task-filters{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;padding:16px;border:1px solid var(--border);border-radius:10px}.task-filters>label{display:grid;gap:8px;font-size:12px}.task-filters select,.task-filters input:not([type=checkbox]){width:100%;min-width:0;padding:10px;border:1px solid var(--border);border-radius:6px;background:var(--bg-input);color:var(--text-primary)}.task-filters .anomaly-filter{display:flex;align-items:center}.task-filters>button{justify-self:end}.task-table-wrap{overflow:auto;border:1px solid var(--border);border-radius:10px}.task-table{border-collapse:collapse;width:100%;min-width:960px;font-size:12px}.task-table th,.task-table td{text-align:left;padding:16px 12px;border-bottom:1px solid var(--border);vertical-align:top;max-width:300px;overflow-wrap:anywhere}.task-table th{background:var(--bg-panel);color:var(--text-secondary)}.task-table small{display:block;font-size:11px;line-height:1.8;color:var(--text-secondary);margin-top:5px}.task-table .danger{color:var(--danger)}.list-pagination{justify-content:flex-end}.platform-alert{padding:14px;border:1px solid var(--danger);border-radius:8px;color:var(--danger);font-size:13px}@media(max-width:720px){.task-center{padding:16px}.task-filters{grid-template-columns:1fr}.task-categories .button{flex:1}}
.task-table{min-width:800px}@media(max-width:900px){.task-table{min-width:0}.task-table thead{display:none}.task-table tbody{display:block}.task-table tr{display:grid;grid-template-columns:1fr 1fr;padding:12px;border-bottom:1px solid var(--border)}.task-table td{max-width:none;min-width:0;border:0;padding:8px;display:block}.task-table td:first-child,.task-table td:nth-child(2){grid-column:1/-1}.task-table td::before{content:attr(data-label);display:block;color:var(--text-secondary);font-size:11px;margin-bottom:8px}.task-table td[colspan]{grid-column:1/-1}.task-table td:last-child .button{width:100%}}
</style>
