<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { recipeExecutionOverview } from '@/services/recipeExecutionOverview'
import { recipeObject } from '@/services/productionRecipeAuthoring'
import { stageLabel } from '@/services/presentationLabels'
import type { ProductionRun } from '@/services/platformTypes'

const props=defineProps<{run:ProductionRun;busy:boolean;stageKeys?:string[]}>()
const emit=defineEmits<{select:[choice:{stageKey:string;routeIndex:number;appearanceRole:'primary'|'variant'}]}>()
const selections=ref<Record<string,number|null>>({})
const overview=computed(()=>recipeExecutionOverview(recipeObject(props.run.executionPlan?.value.recipe).definition))
const stages=computed(()=>overview.value.stages.flatMap(row=>{
  const state=props.run.stages?.find(stage=>stage.stageKey===row.key)
  const groups=[{...row,appearanceRole:'primary' as const,label:row.key==='bible_image_generation'?'主图':'执行',selectedIndex:state?.selectedRouteIndex}]
  const variant=row.variantRouting
  return [
    ...groups,
    ...(variant?[{...row,...variant,appearanceRole:'variant' as const,label:'变体',selectedIndex:state?.selectedVariantRouteIndex}]:[]),
  ].filter(group=>group.routingMode==='manual_choice').map(group=>({...group,state,choiceKey:group.key+':'+group.appearanceRole}))
}))
watch(()=>[props.run.id,props.run.executionPlan?.sha256,props.run.stages?.map(stage=>`${stage.id}:${stage.selectedRouteIndex}:${stage.selectedVariantRouteIndex}`).join('|')],()=>{
  selections.value=Object.fromEntries(stages.value.map(stage=>[stage.choiceKey,stage.selectedIndex??null]))
},{immediate:true})
const visibleStages=computed(()=>stages.value.filter(stage=>!props.stageKeys || props.stageKeys.includes(stage.key)))
function selectable(key:string){
  const stage=stages.value.find(row=>row.choiceKey===key)?.state
  return !props.busy&&!['succeeded','failed','cancelled'].includes(props.run.status)&&Boolean(stage&&['pending','awaiting_approval'].includes(stage.status))
}
function select(key:string){
  const index=selections.value[key],stage=stages.value.find(row=>row.choiceKey===key)
  if(!stage||!selectable(key)||typeof index!=='number'||!Number.isInteger(index)||index<0||index>=stage.routes.length)return
  emit('select',{stageKey:stage.key,routeIndex:index,appearanceRole:stage.appearanceRole})
}
</script>
<template>
  <section v-if="run.planSource==='recipe'&&(visibleStages.length||overview.error)" class="platform-card route-choices">
    <strong>选择本轮执行实例</strong>
    <p v-if="overview.error" role="alert">{{overview.error}}</p>
    <template v-else>
      <p>候选来自本轮冻结配方。媒体执行实例可在参考素材审核前选择；已有执行准备不能更换实例，重试仍使用原请求。</p>
      <article v-for="stage in visibleStages" :key="stage.choiceKey">
        <label>{{stageLabel(stage.key)}} · {{stage.label}}<select v-model="selections[stage.choiceKey]" :disabled="!selectable(stage.choiceKey)">
          <option :value="null">请选择本阶段执行实例</option>
          <option v-for="(route,index) in stage.routes" :key="index" :value="index">执行实例 {{index+1}} · 版本 {{route.versionId}}</option>
        </select></label>
        <p>已确认：{{stage.selectedIndex==null?'尚未选择':`执行实例 ${stage.selectedIndex+1}`}}</p>
        <button class="button primary small" type="button" :disabled="!selectable(stage.choiceKey)||selections[stage.choiceKey]==null||selections[stage.choiceKey]===stage.selectedIndex" @click="select(stage.choiceKey)">确认{{stage.label}}实例</button>
      </article>
    </template>
  </section>
</template>
<style scoped>
.route-choices{display:grid;gap:10px;padding:18px}.route-choices article,.route-choices label{display:grid;gap:8px}.route-choices p{margin:0;color:var(--text-secondary)}.route-choices select{width:100%;min-width:0;padding:9px;background:var(--bg-input,#090c10);color:inherit;border:1px solid var(--border,#303743)}
</style>
