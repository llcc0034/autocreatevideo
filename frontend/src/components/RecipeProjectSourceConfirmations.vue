<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { ProductionRecipe, RecipeProjectSourceConfirmation } from '@/services/platformTypes'
import { recipeObject as obj, recipeRows as rows } from '@/services/productionRecipeAuthoring'
import { executionRegistryApi } from '@/services/executionRegistryApi'
import { executionRequestFormContract, type ExecutionRequestDraft, type ExecutionRequestFormContract } from '@/services/executionRequestForm'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { stageLabel } from '@/services/presentationLabels'
import ExecutionInputSuggestions from './ExecutionInputSuggestions.vue'

const props=defineProps<{recipe:ProductionRecipe;projectId?:string;disabled?:boolean;modelValue?:RecipeProjectSourceConfirmation[]}>()
const emit=defineEmits<{'update:modelValue':[RecipeProjectSourceConfirmation[]];ready:[boolean]}>()
type Entry={identity:string;versionId:string;inputKey:string;uses:string[];contract:ExecutionRequestFormContract;row:ExecutionRequestDraft['literals'][string]}
const entries=ref<Entry[]>([]), loading=ref(false), error=ref(''), busy=ref<Record<string,boolean>>({})
let sequence=0
async function load(){
  const current=++sequence
  entries.value=[]; busy.value={}; error.value=''; loading.value=true
  try{
    const targets=new Map<string,{versionId:string;sha:string;inputKey:string;uses:string[]}>()
    for(const stage of rows(props.recipe.definition.stages)) for(const route of ['routing','variant_routing'].flatMap(key=>rows(obj(stage[key]).routes))){
      for(const [key,source] of Object.entries(obj(route.input_sources))){
        if(obj(source).kind!=='project_snapshot')continue
        const versionId=String(route.execution_instance_version_id),sha=String(route.expected_definition_sha256),identity=JSON.stringify([versionId,key])
        const previous=targets.get(identity)
        if(previous && previous.sha!==sha)throw new Error('同一输入的配方合同摘要冲突，请重新核对配方')
        if(previous)previous.uses.push(String(stage.key));else targets.set(identity,{versionId,sha,inputKey:key,uses:[String(stage.key)]})
      }
    }
    const contracts=new Map<string,ExecutionRequestFormContract>()
    const result:Entry[]=[]
    for(const [identity,target] of targets){
      let contract=contracts.get(target.versionId)
      if(!contract){contract=executionRequestFormContract(await executionRegistryApi.contract(target.versionId));contracts.set(target.versionId,contract)}
      if(current!==sequence)return
      if(contract.sha256!==target.sha || !contract.sourceConstraints[target.inputKey]?.kinds.includes('project_snapshot')
        || !contract.inputForms?.[target.inputKey]?.suggestions.length)throw new Error('配方项目输入与精确读取合同不一致，请重新配置')
      result.push({identity,...target,contract,row:{enabled:true,value:''}})
    }
    if(current===sequence)entries.value=result
  }catch(cause){if(current===sequence)error.value=apiErrorSummary(cause)}
  finally{if(current===sequence)loading.value=false}
}
watch(()=>JSON.stringify([props.recipe.id,props.recipe.definitionSha256,props.recipe.definition,props.projectId]),()=>void load(),{immediate:true})
const ready=computed(()=>!loading.value && !error.value && !Object.values(busy.value).some(Boolean)
  && entries.value.every(entry=>!!props.projectId && !!entry.row.reference && entry.row.value===entry.row.reference.content))
watch(()=>JSON.stringify({ready:ready.value,rows:entries.value.filter(entry=>entry.row.reference).map(entry=>({
  executionInstanceVersionId:entry.versionId,inputKey:entry.inputKey,reference:entry.row.reference!.token}))}),value=>{
  const state=JSON.parse(value) as {ready:boolean;rows:RecipeProjectSourceConfirmation[]}
  emit('update:modelValue',state.rows);emit('ready',state.ready)
},{immediate:true})
onBeforeUnmount(()=>{sequence++})
</script>

<template>
  <section v-if="loading||error||entries.length" class="recipe-project-inputs" aria-label="本轮项目来源确认">
    <h4>本轮项目来源确认</h4>
    <p>按配方精确合同预览并确认，不读取完整剧本。确认内容随本轮创建请求冻结；不是模型调用授权或产物审核。</p>
    <p v-if="loading" role="status">正在读取配方输入合同…</p>
    <p v-if="error" role="alert">{{ error }} <button type="button" class="button ghost small" :disabled="disabled||loading" @click="load">重新读取来源合同</button></p>
    <p v-if="entries.length&&!projectId" role="status">先保存项目，然后在此核对项目已保存的梗概与风格，再继续创建本轮。此时不会启动模型任务。</p>
    <article v-for="entry in entries" :key="entry.identity">
      <strong>{{ entry.contract.inputForms?.[entry.inputKey]?.label || entry.inputKey }}</strong>
      <small>输入 {{ entry.inputKey }} · 使用阶段 {{ [...new Set(entry.uses)].map(stageLabel).join('、') }}</small>
      <ExecutionInputSuggestions v-if="projectId" v-model="entry.row" :version-id="entry.versionId" :definition-sha256="entry.contract.sha256"
        :input-key="entry.inputKey" :project-id="projectId" :choices="entry.contract.inputForms![entry.inputKey]!.suggestions"
        :disabled="disabled" snapshot @busy="busy[entry.identity]=$event"/>
    </article>
  </section>
</template>

<style scoped>
.recipe-project-inputs{display:grid;gap:10px;min-width:0}.recipe-project-inputs article{display:grid;gap:8px;min-width:0;border-top:1px solid var(--border);padding-top:10px}.recipe-project-inputs h4,.recipe-project-inputs p{margin:0}.recipe-project-inputs p,.recipe-project-inputs small{overflow-wrap:anywhere;color:var(--text-muted)}.recipe-project-inputs [role=alert]{color:var(--danger,#e57b7b)}
</style>
