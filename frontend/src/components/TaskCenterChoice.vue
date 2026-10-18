<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { taskCenterApi } from '@/services/taskCenterApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import type { TaskChoice } from '@/services/taskCenter'
const props=defineProps<{modelValue:string;domain:'projects'|'execution_versions';label:string}>()
const emit=defineEmits<{'update:modelValue':[value:string]}>()
const search=ref(''), rows=ref<TaskChoice[]>([]), page=ref(1), next=ref(false), previous=ref(false), loading=ref(false), error=ref('')
const appliedSearch=ref('')
let serial=0
async function load(target=1, newSearch=true) {
 const n=++serial;loading.value=true;error.value=''
 const query=newSearch?search.value:appliedSearch.value
 try {const data=await taskCenterApi.options({domain:props.domain,search:query,page:target})
 if(n!==serial)return
 rows.value=data[props.domain]||[];const paging=data.pagination[props.domain];if(!paging)throw Error('选项分页缺失')
 next.value=!!paging.next;previous.value=!!paging.previous;page.value=target;appliedSearch.value=query
 }catch(e){if(n===serial)error.value=apiErrorSummary(e)}finally{if(n===serial)loading.value=false}
}
watch(()=>props.domain,()=>{rows.value=[];void load()}, {immediate:true})
onBeforeUnmount(()=>serial++)
</script>
<template>
 <fieldset class="task-choice"><legend>{{label}}</legend>
 <div class="choice-search"><input v-model="search" :aria-label="'搜索'+label" placeholder="按名称搜索" @keydown.enter.prevent="load()"/><button type="button" class="button ghost small" :disabled="loading" @click="load()">搜索</button></div>
 <select :aria-label="label" :value="modelValue" :disabled="loading" @change="emit('update:modelValue',($event.target as HTMLSelectElement).value)">
 <option value="">全部</option><option v-if="modelValue&&!rows.some(r=>r.value===modelValue)" :value="modelValue">当前筛选 · {{modelValue}}</option>
 <option v-for="r in rows" :key="r.value" :value="r.value">{{r.label}}</option></select>
 <p v-if="error" role="alert">{{error}} · 选项未更新，请重新搜索。</p>
 <div class="choice-pages"><span>{{loading?'读取中…':'选项第 '+page+' 页'}}</span><button type="button" :disabled="loading||!previous" @click="load(page-1,false)">上一页</button><button type="button" :disabled="loading||!next" @click="load(page+1,false)">下一页</button></div>
 </fieldset>
</template>
<style scoped>
.task-choice{min-width:0;border:1px solid var(--border);border-radius:8px;padding:10px;display:grid;gap:8px}.task-choice legend{font-size:12px;padding:0 4px}.choice-search,.choice-pages{display:flex;gap:8px;align-items:center}.choice-search input{min-width:0;flex:1}.task-choice input,.task-choice select{background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border);border-radius:6px;padding:8px;width:100%}.choice-pages{font-size:11px;justify-content:flex-end}.choice-pages span{margin-right:auto}.choice-pages button{background:transparent;color:var(--text-secondary);border:0;cursor:pointer}.choice-pages button:disabled{opacity:.4}.task-choice p{font-size:12px;color:var(--danger)}
</style>
