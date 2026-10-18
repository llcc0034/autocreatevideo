// Real components with in-memory reads/writes. All HTTP, database and model requests are blocked.
import {createApp,h,ref} from 'vue'
import {createRouter,createMemoryHistory,RouterView} from 'vue-router'
import ProductionRunView from '../../src/views/ProductionRunView.vue'
import SeriesStudioView from '../../src/views/SeriesStudioView.vue'
import {createPinia} from 'pinia'
import {useStudioStore} from '../../src/stores/studio'
import {repository} from '../../src/services/repository'
import {bibleVariantApi} from '../../src/services/bibleVariantApi'
import {platformApi} from '../../src/services/platformApi'
import {scriptRevisionStructures} from '../../src/services/scriptRevisionStructures'
import {api} from '../../src/services/api'
import '../../src/styles.css'
import '../../src/series.css'
import '../../src/responsive.css'
import '../../src/platform.css'
api.interceptors.request.use(()=>{throw Error('隔离验证禁止真实 HTTP 请求')})
const sha='a'.repeat(64),failSave=ref(false),fail=ref(false),empty=ref(false),reload=ref(0),saves=ref(0),translationReads=ref(0)
const rows=Array.from({length:14},(_,i)=>({id:`draft-${String(i+1).padStart(2,'0')}`,project:'fixture-project',projectTitle:'隔离多版本项目',planSource:'manual',status:i?'awaiting_input':'succeeded',createdAt:`2026-10-${String(i+1).padStart(2,'0')}T01:00:00Z`,finishedAt:i?'':'2026-10-01T02:00:00Z',updatedAt:'2026-10-11T00:00:00Z',recipeVersion:'',scriptRevision:`script-${i+1}`,releaseBundleId:'',inputSnapshot:{request_intent:{script_revision_id:`import-${i+1}`}},executionPlan:{value:{},sha256:sha},stages:[]})) as any[]
platformApi.getProductionRun=async id=>{if(fail.value)throw Error('隔离读取失败：无权读取此版本');const row=rows.find(r=>r.id===id);if(!row)throw Error('版本不存在');return row}
platformApi.getScriptRevision=async id=>({id,revision:1,source:'fixture-source',contentSha256:sha,content:`人工导入原文 ${id}：雨夜车站，阿青拿起旧相册，回头微笑。`}) as any
scriptRevisionStructures.read=async id=>({document_sha256:sha,can_revise:false,json_schema:{type:'object',properties:{剧情补充:{type:'string'},旁白:{type:'boolean'},重试数:{type:'integer'}}},document:{revision:{id,content_sha256:sha},projection:{source_value:{剧情补充:'用户自定义内容',旁白:false,重试数:0},value:{entities:[{key:'qing',kind:'character',name:'阿青',identity_anchor:'年轻插画师',description:'蓝色外套，黑色短发',primary:{reference_name:'阿青主图'},variants:[]}],episodes:[{episode:1,shots:Array.from({length:17},(_,i)=>({shot:i+1,duration_ms:5000,action:`第 ${i+1} 镜：阿青在雨夜车站翻开相册。`,dialogue:{text:i===2?'还好赶上了。':'',speaker_entity_key:'qing'},references:[{entity_key:'qing',purpose:'保持本轮人物外观一致'}]}))}]}}}})
const pinia=createPinia(),studio=useStudioStore(pinia)
let draft:any={id:'fixture-project',title:'雨夜车站 · 项目工作台隔离验证',logline:'在同一个工作台查看制作版本，并保留手动创作入口。仅内存样例，不写真实项目。',styleId:'drama',status:'draft',aspectRatio:'9:16',resolution:'720p',continuitySummary:'Keep the same coat.',translationGlossary:{阿青:'Existing Name'},updatedAt:'2026-10-11T00:00:00Z',bible:{characters:[{id:'qing',name:'阿青',description:'蓝色外套，黑色短发',referenceAssetIds:[],anchorAssetId:null,locked:false}],scenes:[],props:[],negativePrompt:'',colorScript:'Cool blue palette.'},episodes:[{id:'ep-one',number:1,title:'雨夜车站',synopsis:'阿青在车站翻开相册',script:'原始手动剧集文本',shots:[],status:'draft'}]}
studio.styles=[{id:'drama',name:'都市现实剧情',englishName:'Drama',gradient:'linear-gradient(120deg,#302316,#122333)',accent:'#d9a15d',tags:[],isActive:true}] as any
studio.loadStyles=async()=>{}
studio.projects=[JSON.parse(JSON.stringify(draft))]
studio.loadProject=async()=>{studio.projects=[JSON.parse(JSON.stringify(draft))];return JSON.parse(JSON.stringify(draft))}
studio.refreshAssets=async()=>[]
studio.saveProject=async value=>{if(failSave.value)throw Error('隔离模拟保存失败');saves.value++;draft=JSON.parse(JSON.stringify(value));studio.projects=[JSON.parse(JSON.stringify(draft))];return JSON.parse(JSON.stringify(draft))}
studio.addEpisode=async (project,value)=>{const episode={...value,id:'ep-'+(project.episodes.length+1),number:project.episodes.length+1,shots:[],status:'draft'};project.episodes.push(episode as any);await studio.saveProject(project);return episode as any}
platformApi.listProductionRuns=async()=>empty.value?[]:rows
repository.getBibleImageCandidates=async()=>({projectId:draft.id,candidates:[]})
repository.getVideoConditioningCapability=async()=>({conditioningModes:[]}) as any
repository.getAssets=async()=>[]
repository.getProjectTranslations=async()=>{translationReads.value++;return[]}
bibleVariantApi.list=async()=>({count:0,next:null,previous:null,results:[]})
platformApi.getManualProductionActions=async id=>({runId:id,runPlanSha256:sha,actions:[{stageKey:'script_generate',action:'generate',operation:'script.generate'}],scriptAdoption:{content:null},currentScript:null,currentScriptIdentity:null,visualRulesReview:{status:'not_reviewed',reasons:[]}}) as any
platformApi.listScriptSources=async()=>[]
platformApi.listGenerationAttempts=async()=>[]
const router=createRouter({history:createMemoryHistory(),routes:[
 {path:'/projects/:projectId',name:'project-detail',component:SeriesStudioView},
 {path:'/projects/:projectId/production',name:'production-run',component:ProductionRunView},
 {path:'/projects/:projectId/production/:runId',name:'production-run-detail',component:ProductionRunView},
 {path:'/workflows',name:'workflows',component:{template:'<p>隔离任务中心入口：仅核对导航，不调用正式服务。</p>'}},
 {path:'/:all(.*)*',component:{template:'<p>隔离导航目标（不调用生产服务）</p>'}},
]})
await router.push('/projects/fixture-project')
createApp({setup(){return()=>h('div',{style:'padding:30px;max-width:1440px;margin:auto'},[
 h('p',{role:'status',style:'margin-bottom:36px'},`隔离验证 · 保存 ${saves.value} 次 · 翻译读取 ${translationReads.value} 次 · 历史词表 ${draft.translationGlossary?.阿青 === 'Existing Name' ? '完整保留' : '异常'} · 所有 HTTP 均禁止，操作只保留在内存`),
 h('div',{style:'display:flex;gap:12px;margin-bottom:36px;flex-wrap:wrap'},[
  h('button',{class:'button ghost small',onClick:()=>{fail.value=!fail.value;reload.value++}},fail.value?'恢复读取':'模拟版本读取失败'),
  h('button',{class:'button ghost small',onClick:()=>{failSave.value=!failSave.value}},failSave.value?'恢复保存':'模拟保存失败'),
  h('button',{class:'button ghost small',onClick:()=>{empty.value=!empty.value;reload.value++}},empty.value?'恢复版本列表':'模拟空项目'),
 ]),
 h(RouterView,{key:reload.value}),
])}}).use(pinia).use(router).mount('#app')
