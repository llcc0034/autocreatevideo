// Actual review component, local responses only. No approval, live business or model calls.
import {createApp,h,ref} from 'vue'
import {createPinia} from 'pinia'
import {taskCenterApi} from '../../src/services/taskCenterApi'
import RecipeStoryboardReferences from '../../src/components/RecipeStoryboardReferences.vue'
import ManualStoryboardReferenceReview from '../../src/components/ManualStoryboardReferenceReview.vue'
import {createRouter,createMemoryHistory} from 'vue-router'
import {assetReferenceApi} from '../../src/services/assetReferenceApi'
import {repository} from '../../src/services/repository'
import {platformApi} from '../../src/services/platformApi'
import {api} from '../../src/services/api'
import {displayFixture} from '../storyboardReviewDisplayFixture'
import '../../src/styles.css'
import '../../src/platform.css'
api.interceptors.request.use(()=>{throw new Error('隔离夹具禁止真实业务API')})
const id=(n:number)=>`00000000-0000-4000-8000-${String(n).padStart(12,'0')}`, sha='a'.repeat(64)
const failed=ref(false),reads=ref(0),narrow=ref(false),disabled=ref(false),epoch=ref(0),ready=ref(false),message=ref('')
const previewFailed=ref(false),previewReads=ref(0)
const relationsMissing=ref(false)
const historyMissing=ref(false),historyReads=ref(0)
const historyRow=(n:number)=>({kind:'preparation' as const,id:id(n),category:'production' as const,title:n===70?'原 REST 视频准备（隔离）':'未冻结文本准备（隔离）',status:n===70?'succeeded':'preparing',operation:n===70?'video.generate':'script.generate',
  project_id:id(2),project_title:'隔离雨衣项目',production_run_id:id(52),execution_instance_version_id:id(54),execution_instance_name:n===70?'原 REST 实例':'原文本实例',execution_version:3,
  preparation_id:id(n),source_id:n===70?id(72):null,source_kind:n===70?'generation_attempt':'',created_at:'2026-10-03T08:00:00Z',updated_at:'2026-10-03T08:00:00Z',finished_at:null,
  estimated_cost:null,actual_cost:null,cost_currency:'',anomaly:false})
taskCenterApi.list=async filters=>{historyReads.value++;if(filters.project_id!==id(2))throw Error('错误历史项目')
  const results=filters.unit_key===`shot:${id(3)}`?[historyRow(70),historyRow(71)]:[]
  return {results,count:results.length,next:null,previous:null,counts:{all:results.length,production:results.length,asset:0,smoke:0},read_at:'2026-10-03T08:00:00Z'}}
taskCenterApi.detail=async(kind,pk)=>{historyReads.value++;if(kind!=='preparation'||![id(70),id(71)].includes(pk))throw Error('错误原准备')
  const frozen=pk===id(70)
  return {...historyRow(frozen?70:71),outcome:{message:frozen?'隔离成功状态，不代表真实模型运行':'尚未完成准备，没有原请求',artifact_count:0,selected_count:0},actions:{cancel_asset:false,cancel_smoke:false},purpose:'production',preparation_status:frozen?'dispatched':'preparing',request_frozen_at:frozen?'2026-10-03T08:00:00Z':null,request_sha256:frozen?sha:null,
    submission:{state:'unavailable',started_at:null,registered_at:null,identifier_state:'unavailable',message:'隔离展示，无真实外呼',notes:[]},
    input:{state:frozen?'frozen':'not_frozen',submitted:{},resolved:{},parameters:frozen?[{key:'frames',value:56,source:'原冻结参数'}]:[],prompts:frozen?[{key:'positive_prompt',role:'positive_prompt',text:'原 REST 请求：雨中旅人走过车站。此请求未声明参考素材输入。',source:'transport.prompt_channels.values'}]:[],
      prompt_trace:null,field_batch_trace:null,media_trace:null,api_mapping:null,native_input_trace:null,system_message_trace:null,
      notes:frozen?['原冻结请求隔离展示。','本次冻结定义未声明图片、视频或音频输入，原请求没有媒体槽位绑定；当前分镜选择不会补入历史。']:['请求尚未冻结，无最终提示词；未重新编译当前策略。']},
    output:{state:'unavailable',result:null,text:'',media:[],notes:['不伪造媒体结果']},validation:{status:'valid',warnings:[],error_code:'',error_message:''},
    calls:[],call_count:0,calls_next:null,calls_previous:null,output_count:0,output_next:null,output_previous:null,deferred_fields:[],links:{generation_task_id:null,asset_job_id:null}}
}
const frozenTask:any={id:id(50),taskId:id(50),projectId:id(2),createdAt:'2026-10-03T08:00:00Z',backendStatus:'succeeded',
  execution:{unitKey:`shot:${id(3)}`,preparationId:id(51),productionRunId:id(52)},inputContractStatus:'available',
  inputParameters:{preparation_id:id(51),attempt_id:id(53),request_sha256:sha,execution_instance_version_id:id(54),parameters:[]},
  referenceAssets:[{itemKey:'original-raincoat',inputKey:'portraits',assetId:id(55),displayName:'原请求旧雨衣（不是当前 v2）',mediaKind:'image',
    previewUrl:'/tests/fixtures/frozen-reference.svg',inputLabel:'<Picture 1>',purpose:null,checksum:sha,sourceSha256:sha,
    ports:[{targetKey:'reference_images',port:{kind:'semantic',semantic_key:'image.reference'}}],stagingStatus:'staged'}],
  contextMediaUses:[{itemKey:'context',inputKey:'setting_context',artifactId:id(56),displayName:'原背景说明素材',mediaKind:'image',
    previewUrl:'',purpose:null,checksum:sha,sourceSha256:sha,ports:[],stagingStatus:'not_applicable'}]}
repository.getShotGenerationTasks=async(projectId,shotId)=>{historyReads.value++;if(projectId!==id(2))throw Error('错误项目')
  return shotId===id(3)?[structuredClone(frozenTask)]:[]}
repository.getWorkflow=async(pk)=>{historyReads.value++;if(pk!==frozenTask.id)throw Error('错误历史任务')
  const row=structuredClone(frozenTask);if(historyMissing.value)row.referenceAssets[0].previewUrl='';return row}
repository.getTaskInputPrompt=async()=>({taskId:id(50),preparationId:id(51),attemptId:id(53),requestSha256:sha,executionInstanceVersionId:id(54),
  promptMode:'manual',promptProfileVersionId:null,channels:[{key:'positive_prompt',semanticKey:'prompt.positive',text:'Original request raincoat; not current selection',characterCount:48,valueSource:'manual'}],declaredReferenceLabels:['<Picture 1>']})
function display(ids:string[]){
  const value:any=displayFixture(id(20),ids)
  value.shots[0].scene={id:id(30),name:'雨中车站',status:'available'}
  value.shots[0].characters=[{role:'visible',position:0,entity:{id:id(31),name:'旅人',status:'available'}},
    {role:'speaker',position:2,entity:relationsMissing.value?{id:id(32),name:null,status:'unavailable'}:{id:id(32),name:'广播员（画外音）',status:'available'}}]
  value.shots[0].props=[{id:id(33),name:'折叠雨伞',status:'available'}]
  return value
}
const picture='data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180"><rect width="320" height="180" fill="#315d69"/><text x="24" y="98" fill="white" font-size="22">RAINCOAT - FIXTURE v2</text></svg>')
const pictureAsset:any={id:id(7),name:'旅人雨衣（隔离图片）',type:'image',mimeType:'image/png',checksum:sha,isActive:true,mediaUrl:picture}
repository.getAsset=async(pk)=>{previewReads.value++;if(previewFailed.value)throw Error('隔离预览：原素材已失效')
  if(pk===id(7))return pictureAsset
  throw Error('隔离预览：该声音文件不可读取')}
assetReferenceApi.getVersion=async()=>({id:id(5),series_id:id(6),version:2,source:'generated',asset:{id:id(7),name:pictureAsset.name,kind:'image',mime_type:'image/png',is_active:true},generated_from_id:null,setting_revision_id:null})
assetReferenceApi.getSeries=async()=>({id:id(6),name:'旅人服装',role:'variant',owner_id:1,primary_series_id:id(12),bible_variant_id:null,version_count:2,variant_count:0})
platformApi.getStoryboardReferenceSelection=async()=>{
  reads.value++
  if(failed.value)throw Error('隔离夹具：素材文件缺失，不得批准')
  return {display:display([id(3),id(10)]),source_snapshot_sha256:sha,selection_sha256:'b'.repeat(64),shot_count:2,selection:{schema_version:1,project_id:id(2),shots:[
    {shot_id:id(3),uses:[{use_id:id(4),use_key:'raincoat_appearance',position:0,media_kind:'image',purpose:'旅人雨衣的外观参考，不是视频首帧',source_mode:'existing',setting:null,
      source:{kind:'reference_version',reference_version_id:id(5),series_id:id(6),version:2,acquisition_source:'generated'},asset:{asset_id:id(7),sha256:sha,mime_type:'image/png'}},
    {use_id:id(8),use_key:'voice',position:1,media_kind:'audio',purpose:'',source_mode:'upload',setting:null,source:{kind:'asset',asset_id:id(9)},asset:{asset_id:id(9),sha256:sha,mime_type:'audio/wav'}}]},
    {shot_id:id(10),uses:[]}]}}
}
const run:any={id:id(1),project:id(2),stages:[{stageKey:'storyboard',status:'succeeded',outputSnapshot:{canonical:{episodes:[{number:1,shots:[{id:id(3),number:1},{id:id(10),number:2}]}]}}}]}
const manual=new URLSearchParams(location.search).get('plan')==='manual'
const manualRun:any={id:run.id,project:run.project,planSource:'manual',status:'awaiting_input',executionPlan:{sha256:sha}}
const manualOptions:any={runId:run.id,runPlanSha256:sha,actions:[{stageKey:'storyboard_review',action:'approve'}]}
platformApi.getManualStoryboardReview=async()=>{reads.value++;return {display:display([id(3)]),run_id:run.id,run_plan_sha256:sha,ready:true,reason:'',contract_sha256:sha,
  source_options:[{value:'images',label:'镜头用途图片',kind:'image'}],approval:{revision:0,decision_id:null,snapshot_sha256:null,status:'not_reviewed'},
  contract:{schema_version:1,kind:'manual_storyboard_review',stage_key:'storyboard_review',formal_object_type:'storyboard',canonical_sha256:sha,manual_basis:{run_plan_sha256:sha},
    assets:[{id:id(7),name:pictureAsset.name,kind:'image',mime_type:'image/png',checksum:sha}],canonical:{episodes:[{id:id(20),number:1,title:'雨中相遇',shots:[{id:id(3),number:1,title:'旅人雨衣',narrative:'隔离分镜原值',
      reference_uses:[{use_key:'raincoat_appearance',media_kind:'image',purpose:'外观参考，不是首帧',source_mode:'existing',reference_version_id:id(5),asset_id:null,asset_sha256:sha}]}]}]}},
  references:[{shot_id:id(3),items:[{reference_key:'shot-use:raincoat_appearance',source_key:'images',kind:'image',purpose:'外观参考，不是首帧',source:{kind:'asset',asset_id:id(7)},subject_ref:null,paired_reference_key:null}]}]}}
platformApi.performManualProductionAction=async()=>{throw Error('隔离夹具不执行任何正式审核')}
const fixtureRouter=createRouter({history:createMemoryHistory(),routes:[{path:'/projects/:projectId/episodes/:episodeId',name:'storyboard',component:{render:()=>h('p','仅隔离路由')}},
  {path:'/projects/:projectId/production/:runId',component:{render:()=>h('p','仅隔离历史制作路由，不读取真实运行')}}]})
await fixtureRouter.push('/projects/'+run.project+'/episodes/'+id(20));await fixtureRouter.isReady()
createApp({setup:()=>()=>h('main',{style:{width:narrow.value?'390px':'100%',maxWidth:'100%',boxSizing:'border-box',margin:'auto',padding:'16px',background:'#101820',color:'#e6edf3'}},[
  h('h1','分镜确切选择审核 · 隔离验收'),h('p','正式只读组件；全部响应为隔离夹具，不调用模型、不提交批准。'),
  h('div',{style:{display:'flex',gap:'8px',flexWrap:'wrap'}},[
    h('button',{onClick:()=>narrow.value=!narrow.value},narrow.value?'宽屏布局':'390px 窄屏布局'),
    h('button',{onClick:()=>failed.value=!failed.value},failed.value?'恢复正常响应':'模拟文件缺失'),
    h('button',{onClick:()=>previewFailed.value=!previewFailed.value},previewFailed.value?'恢复素材预览':'模拟预览失效'),
    h('button',{onClick:()=>relationsMissing.value=!relationsMissing.value},relationsMissing.value?'恢复关系名称':'模拟关系名称失效'),
    h('button',{onClick:()=>historyMissing.value=!historyMissing.value},historyMissing.value?'恢复历史预览':'模拟历史预览失效'),
    h('button',{onClick:()=>disabled.value=!disabled.value},disabled.value?'恢复读取':'锁定读取'),
    h('button',{onClick:()=>epoch.value++},'单镜改选已返回（重新核对）')]),
  manual?h(ManualStoryboardReferenceReview,{run:manualRun,options:manualOptions}):h(RecipeStoryboardReferences,{run,stageId:id(11),refreshEpoch:epoch.value,disabled:disabled.value,onReady:(value:any)=>ready.value=!!value,onError:(value:string)=>message.value=value}),
  h('output',{id:'preview-read-state'},`素材预览读取 ${previewReads.value} 次；真实业务请求 0`),
  h('output',{id:'history-read-state'},`历史任务读取 ${historyReads.value} 次（必须显式打开）`),
  h('output',{id:'review-read-state',style:{display:'block',overflowWrap:'anywhere'}},`隔离读取 ${reads.value} 次；批准依据：${ready.value?'已读取确切摘要':'不可用'}；${message.value}`),
])}).use(createPinia()).use(fixtureRouter).mount('#app')
