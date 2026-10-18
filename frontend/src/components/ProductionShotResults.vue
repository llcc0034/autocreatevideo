<script setup lang="ts">
import {onBeforeUnmount,ref,watch} from 'vue'
import type {ProductionRun, GeneratedArtifact} from '@/services/platformTypes'
import {executionPreparationApi} from '@/services/executionPreparationApi'
import {selectedAttemptArtifacts} from '@/services/projectVersionContents'
import {apiErrorSummary} from '@/services/apiErrorPresentation'
import ProductionArtifactPreview from './ProductionArtifactPreview.vue'
import ExecutionPromptDisplay from './ExecutionPromptDisplay.vue'
const props=defineProps<{run:ProductionRun;shotId:string}>()
const opened=ref(false),loading=ref(false),error=ref(''),page=ref(1)
const result=ref<Awaited<ReturnType<typeof executionPreparationApi.forStage>> | null>(null)
const media=ref<Record<string,GeneratedArtifact[]>>({})
let epoch=0
async function load(number=1){
  const token=++epoch;opened.value=true;loading.value=true;error.value='';result.value=null;media.value={};page.value=number
  try{
    const stages=props.run.stages?.filter(row=>row.stageKey==='media_generation') || []
    if(stages.length!==1)throw Error('本轮缺少唯一的媒体生成阶段，请在制作台按阶段查看。')
    const rows=await executionPreparationApi.forStage(stages[0]!.id,number,'primary',`shot:${props.shotId}`)
    const selected=Object.fromEntries(rows.results.map(row=>[row.id,row.presentation.task ? selectedAttemptArtifacts(props.run,row.presentation.task.id) : []]))
    if(token===epoch){result.value=rows;media.value=selected}
  }catch(cause){if(token===epoch)error.value=apiErrorSummary(cause)}
  finally{if(token===epoch)loading.value=false}
}
watch(()=>[props.run.id,props.shotId],()=>{epoch++;opened.value=false;result.value=null;loading.value=false;error.value=''})
onBeforeUnmount(()=>{epoch++})
</script>
<template>
  <section class="shot-results">
    <button class="button ghost small" :disabled="loading" @click="load()">{{loading?'正在读取本镜结果…':opened?'刷新本镜输入与结果':'查看本镜实际输入与已选视频'}}</button>
    <p v-if="error" role="alert">{{error}}</p>
    <template v-if="result">
      <p v-if="!result.count">本轮此镜尚无执行准备记录。</p>
      <article v-for="row in result.results" :key="row.id"><p>{{row.outcome.message}}</p>
        <ExecutionPromptDisplay v-if="row.requestFrozenAt" :preparation-id="row.id" :revision="row.revision" :frozen="true" :disabled="false" :field-labels="{}"/>
        <ProductionArtifactPreview v-for="artifact in media[row.id] || []" :key="artifact.id" :id="artifact.id" :run-id="run.id" :sha256="artifact.sha256" label="本镜已选产物"/>
        <p v-if="!media[row.id]?.length">该次调用在本轮选片记录中没有已选产物。</p>
      </article>
      <nav v-if="result.previous || result.next" aria-label="本镜执行分页"><button class="button ghost small" :disabled="loading||!result.previous" @click="load(page-1)">上一页执行</button><span>第 {{page}} 页</span><button class="button ghost small" :disabled="loading||!result.next" @click="load(page+1)">下一页执行</button></nav>
    </template>
  </section>
</template>
<style scoped>.shot-results,.shot-results article{display:grid;gap:12px;min-width:0}.shot-results .button{justify-self:start;white-space:normal}.shot-results nav{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.shot-results p{overflow-wrap:anywhere}</style>
