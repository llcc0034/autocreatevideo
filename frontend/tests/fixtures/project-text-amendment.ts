import { createApp, h, ref } from 'vue'
import RecipeStageInputAmendment from '../../src/components/RecipeStageInputAmendment.vue'
import { platformApi } from '../../src/services/platformApi'
import { api } from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'
api.defaults.adapter=async()=>{throw Error('隔离夹具禁止业务网络请求。')}

const sha='a'.repeat(64),after=ref('尚未写入隔离夹具')
platformApi.amendProductionStageInputs=async(_run,input)=>({status:input.action==='apply'?'applied':'proposed',sha256:sha,
  evidence:{schema_version:1,run_plan_sha256:sha,policy:input.policy,changes:input.source_changes},
  id:input.action==='apply'?'isolated-amendment':null,notice:'隔离夹具：未访问正式API，未创建任务或调用模型。'})
createApp({setup:()=>()=>h('main',{style:'max-width:960px;margin:20px auto;padding:16px;min-width:0'},[
  h('h1','隔离验收：项目文本来源修订'),h('p','明确标注的隔离夹具；不修改正式Run。'),
  h(RecipeStageInputAmendment,{runId:'isolated-run',stageId:'isolated-visual-stage',planSha:sha,projectText:true,
    sources:[{index:0,sources:{'project.master_script':{kind:'context_field',selector_key:'script.content',scope:{kind:'project'}}}}],
    onApplied:()=>{after.value='隔离修订已确认；尚未启动生成'}}),h('p',{role:'status'},after.value)
])}).mount('#app')
