import assert from 'node:assert/strict'
import { unusedLanguageForm } from './fixtures/executionLanguage.ts'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { readFileSync } from 'node:fs'
import { createServer } from 'vite'
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createRouter, createMemoryHistory, RouterView } from 'vue-router'

const root = fileURLToPath(new URL('..', import.meta.url)), sha = 'a'.repeat(64)
const run = { id:'run', project:'project', planSource:'manual', status:'awaiting_input', executionPlan:{sha256:sha}, stages:[] }
const actions = [
  {stageKey:'script_generate',action:'generate',operation:'script.generate',scopeKinds:['project'],outputKinds:[]},
  {stageKey:'media_generation',action:'generate',operation:null,scopeKinds:['project','episode','shot'],outputKinds:['image','video','audio']},
  {stageKey:'bible_image_generation',action:'generate',operation:null,scopeKinds:['character','scene','prop'],outputKinds:['image']},
]
const options = {runId:'run',runPlanSha256:sha,actions,scriptAdoption:{content:null},currentScript:null,
  currentScriptIdentity:null,visualRulesReview:{status:'not_reviewed',reasons:[]}}
const target = (kind='project', id='project') => ({scope:{kind,id},unitKey:`${kind}:${id}`,label:kind === 'project' ? '当前项目：寻找失踪的校花' : `${kind} ${id}`})
const page = (rows=[target()], count=rows.length, next:string|null=null) => ({runId:'run',runPlanSha256:sha,results:rows,count,next,previous:null})
const settle = async () => {for(let i=0;i<20;i++){await Promise.resolve();await nextTick()}}
async function mount(Component:any, props:any, emit: (...args:any[])=>void = ()=>{}) {
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  let state:any
  const host={setup(){state=Component.setup(props,{emit,expose(){}});return()=>h('div')}}
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:host},{path:'/workflows',name:'workflows',component:{render:()=>null}},{path:'/projects/:projectId/production',name:'production-run',component:{render:()=>null}}]})
  await router.push('/');await router.isReady()
  const app=renderer.createApp({render:()=>h(RouterView)})
  app.use(router);app.provide(ssrContextKey,{modules:new Set()});app.mount({});await settle()
  return {state,app,router}
}
async function modules(fn:(vite:any,api:any,registry:any)=>Promise<void>) {
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const {executionRegistryApi}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  // Render the real review inside a DOM-free modal shell in SSR tests.
  const {default:Modal}=await vite.ssrLoadModule('/src/components/BaseModal.vue')
  const modalSetup=Modal.setup,modalRender=Modal.ssrRender
  Modal.setup=(props:any,{slots}:any)=>()=>props.open?h('section',slots.default?.()):null
  delete Modal.ssrRender
  const oldApi={...platformApi},oldRegistry={...executionRegistryApi}
  platformApi.performManualProductionAction=async()=>{throw Error('must not write or call models')}
  platformApi.listProductionTargets=async()=>page()
  executionRegistryApi.versions=async()=>({results:[{id:'version',workflow_version_id:null}],next:null})
  executionRegistryApi.contract=async()=>{throw Error('no implicit instance selection')}
  try{await fn(vite,platformApi,executionRegistryApi)}finally{Object.assign(platformApi,oldApi);Object.assign(executionRegistryApi,oldRegistry);Modal.setup=modalSetup;Modal.ssrRender=modalRender;await vite.close()}
}

test('unknown assembly protects parent refresh and navigation without discarding its captured options',()=>modules(async(vite,api)=>{
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualProductionWorkbench.vue')
  const events:any[]=[]
  const m=await mount(C,{run},(...args)=>events.push(args))
  try {
    m.state.options.value=options
    m.state.openAssembly()
    const context=m.state.assemblyContext.value
    m.state.assemblyPending.value=true
    await nextTick()
    assert.equal(m.state.canLeave(),false)
    assert.ok(events.some(row=>row[0]==='contextProtected'&&row[1]===true))
    api.getManualProductionActions=async()=>{throw Error('refresh failed')}
    await m.state.load()
    assert.equal(m.state.assemblyContext.value,context)
    assert.equal(m.state.assemblyPending.value,true)
    assert.equal(m.state.assemblyOpen.value,true)
    m.state.assemblyPending.value=false
    assert.equal(m.state.canLeave(),true)
  } finally {m.app.unmount()}
}))

async function generationForm(vite:any) {
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualGenerationPreparation.vue')
  const m=await mount(C,{run,options,initialStageKey:'script_generate'})
  const {emptyExecutionRequestDraft}=await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
  const inputs=[['idea','text'],['duration','json']].map(([key,kind],position)=>({key,kind,position,required:true,min_items:1,max_items:1,
    semantic_contract:{schema_version:1,allowed_purposes:[],allowed_subject_kinds:['external'],allowed_scope_kinds:['request'],paired_with_input_key:null,allow_repeated_asset:true},
    targets:[{target_kind:'operation_input',target_key:key,is_primary:true}]}))
  const contract={versionId:'version',sha256:sha,operation:'script.generate',mode:'',outputKind:'text',workflowVersionId:null,
    inputs,parameters:[],promptMode:'none',channels:[],serverOwnedInputs:[],contextFields:[],languageForm:unusedLanguageForm(),
    sourceConstraints:Object.fromEntries(inputs.map(row=>[row.key,{kinds:['literal'],selectors:null}])),purposes:{},
    structureSchemas:{duration:[{type:'integer',minimum:1}]}}
  m.state.versionId.value='version';m.state.contract.value=contract
  m.state.draft.value=emptyExecutionRequestDraft(contract)
  m.state.draft.value.literals.idea={enabled:true,value:'保留已修改的创意',reference:{token:'test-reference',label:'项目梗概',content:'引用原文'}}
  m.state.draft.value.literals.duration.value=1
  return m
}
const rejection=(status:number)=>({response:{status,data:{error:{code:'series_concept_episode_duration_infeasible',message:'单集时长不足以容纳请求的镜头数'}}}})
const saved={action:'generate',result_kind:'preparation',target_id:'11111111-1111-4111-8111-111111111111'}

test('background action poll cannot discard confirmed script versions being loaded',()=>modules(async(vite,api)=>{
  let reads=0,finish:any
  api.getManualProductionActions=async()=>{reads++;return options}
  api.listScriptSources=async()=>[]
  api.listConfirmedScriptRevisions=()=>new Promise(resolve=>{finish=resolve})
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualProductionWorkbench.vue')
  const props=reactive({run:{...run}}),m=await mount(C,props)
  try{
    m.state.sourceId.value='source';const pending=m.state.loadRevisions(),before=reads
    props.run={...props.run};await settle()
    assert.equal(reads,before);assert.equal(m.state.busy.value,true)
    const revision={id:'revision',status:'confirmed',confirmedAt:'2026-10-04T00:00:00Z'}
    finish([revision]);await pending
    assert.deepEqual(m.state.revisions.value,[revision]);assert.equal(m.state.busy.value,false)
    props.run={...props.run};await settle();assert.equal(reads,before+1)
    assert.deepEqual(m.state.revisions.value,[revision])
    let finishOptions:any
    api.getManualProductionActions=()=>new Promise(resolve=>{finishOptions=resolve})
    props.run={...props.run};await settle()
    assert.deepEqual(m.state.options.value,options)
    assert.equal(m.state.busy.value,true)
    assert.equal(m.state.canAct('script_generate','generate'),false)
    finishOptions(options);await settle()
    api.getManualProductionActions=async()=>{throw Error('options unavailable')}
    await m.state.load();assert.equal(m.state.options.value,null)
    assert.match(m.state.error.value,/options unavailable/)
    api.getManualProductionActions=async()=>options
    await m.state.load()
    m.state.sourceId.value='source-2';const stale=m.state.loadRevisions()
    props.run={...props.run,id:'other-run'};await settle()
    assert.equal(m.state.options.value,null)
    finish([revision]);await stale;assert.deepEqual(m.state.revisions.value,[])
  }finally{m.app.unmount()}
}))
test('workspace tabs split visual rules from Bible and project drafts block generation and adoption',()=>modules(async(vite,api)=>{
  api.getManualProductionActions=async()=>options;api.listScriptSources=async()=>[]
  const {productionWorkspaceSections,workspaceSectionIncludes}=await vite.ssrLoadModule('/src/services/productionWorkspacePresentation.ts')
  assert.equal(productionWorkspaceSections.length,7)
  assert.equal(workspaceSectionIncludes('visual','visual_rules_review'),true)
  assert.equal(workspaceSectionIncludes('bible','visual_rules_review'),false)
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualProductionWorkbench.vue')
  const props=reactive({run,inputsProtected:true,initialSection:'bible'}),m=await mount(C,props)
  try{
    assert.equal(m.state.section.value,'bible');assert.equal(m.state.canAct('script_generate','generate'),false)
    m.state.openGeneration('script_generate');assert.equal(m.state.generationOpen.value,false)
    props.inputsProtected=false;assert.equal(m.state.canAct('script_generate','generate'),true)
    props.initialSection='storyboard';await settle();assert.equal(m.state.section.value,'storyboard')
  }finally{m.app.unmount()}
}))

test('shot selection retains original action context across failed polls and closed modal, protects unknown request until context changes',()=>modules(async(vite,api)=>{
  api.getManualProductionActions=async()=>({...options,actions:[{stageKey:'storyboard_review',action:'select',scopeKinds:['shot']}]})
  api.listScriptSources=async()=>[]
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualProductionWorkbench.vue')
  const props=reactive({run:{...run}}),events:any[]=[]
  const m=await mount(C,props,(...args)=>events.push(args))
  try{
    m.state.openShotSelection();await settle();const context=m.state.shotSelectionContext.value
    assert.ok(context);assert.equal(m.state.shotSelectionProtected.value,true)
    m.state.shotSelectionRetained.value={dirty:true,pending:true};m.state.closeShotSelection();await settle()
    assert.equal(m.state.shotSelectionOpen.value,false);assert.equal(m.state.shotSelectionProtected.value,true)
    api.getManualProductionActions=async()=>{throw Error('poll failed')};await m.state.load()
    m.state.openShotSelection();assert.equal(m.state.shotSelectionContext.value,context)
    m.state.shotSelectionBusy.value=true;m.state.closeShotSelection();assert.equal(m.state.shotSelectionOpen.value,true)
    props.run={...run,id:'other'};await settle();assert.equal(m.state.shotSelectionContext.value,null)
    assert.deepEqual(m.state.shotSelectionRetained.value,{dirty:false,pending:false})
    assert.ok(events.some(row=>row[0]==='contextProtected'&&row[1]===true))
  }finally{m.app.unmount()}
}))

test('rejected preparation preserves inputs and reference, unlocks editing and sends corrected input with a new key',()=>modules(async(vite,api)=>{
  const requests:any[]=[]
  api.performManualProductionAction=async(_run:any,body:any)=>{requests.push(JSON.parse(JSON.stringify(body)));if(requests.length===1)throw rejection(400);return saved}
  const m=await generationForm(vite)
  try{
    const before=JSON.stringify(m.state.draft.value)
    await m.state.create()
    assert.equal(m.state.locked.value,false);assert.equal(m.state.busy.value,false);assert.equal(m.state.pending.value,null)
    assert.equal(m.state.rejected.value,true);assert.equal(m.state.preparationId.value,'')
    assert.match(m.state.error.value,/单集时长不足/);assert.equal(JSON.stringify(m.state.draft.value),before)
    m.state.draft.value.literals.duration.value=60
    await m.state.create()
    assert.equal(requests.length,2);assert.notEqual(requests[0].idempotency_key,requests[1].idempotency_key)
    assert.equal(requests[0].payload.inputs.duration.value,1);assert.equal(requests[1].payload.inputs.duration.value,60)
    assert.equal(requests[1].payload.inputs.idea.reference,'test-reference')
    assert.equal(requests[1].payload.inputs.idea.value,'保留已修改的创意')
    assert.equal(m.state.preparationId.value,saved.target_id);assert.equal(m.state.pending.value,null)
  }finally{m.app.unmount()}
}))

test('Bible review retains the opened context across polls and refreshes written content after close',()=>modules(async(vite,api)=>{
  let reads=0
  api.getManualProductionActions=async()=>{reads++;return {...options,actions:[{stageKey:'bible_review',action:'approve'}]}}
  api.listScriptSources=async()=>[]
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualProductionWorkbench.vue')
  const props=reactive({run:{...run}}),events:any[]=[]
  const m=await mount(C,props,(...args)=>events.push(args))
  try{
    m.state.openBibleReview();const context=m.state.bibleContext.value,readCount=reads
    assert.ok(context);assert.deepEqual(events.at(-1),['contextProtected',true])
    props.run={...props.run};await settle();assert.equal(reads,readCount)
    api.getManualProductionActions=async()=>{throw Error('read failed')};await m.state.load()
    assert.equal(m.state.bibleContext.value,context);assert.equal(m.state.bibleOpen.value,true)
    m.state.bibleContentChanged.value=true;m.state.bibleBusy.value=true;m.state.closeBibleReview()
    assert.equal(m.state.bibleOpen.value,true)
    m.state.bibleBusy.value=false;m.state.closeBibleReview()
    assert.equal(m.state.bibleContext.value,null);assert.deepEqual(events.slice(-2),[['contextProtected',false],['contentChanged']])
    props.run={...run,id:'other'};await settle();assert.equal(m.state.bibleContentChanged.value,false)
  }finally{m.app.unmount()}
}))

test('script adoption rejects visibly across polling and retains unknown requests across refresh races',()=>modules(async(vite,api)=>{
  let reads=0
  api.getManualProductionActions=async()=>{reads++;return {...options,actions:[{stageKey:'script_input',action:'adopt'}],scriptAdoption:{revision:0,content:null}}}
  api.listScriptSources=async()=>[]
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualProductionWorkbench.vue')
  const props=reactive({run:{...run}}), events:any[]=[],requests:any[]=[]
  const m=await mount(C,props,(...args)=>events.push(args))
  try{
    m.state.revisions.value=[{id:'revision',contentSha256:sha}];m.state.revisionId.value='revision'
    m.state.comment.value='Keep this explicit selection'
    api.performManualProductionAction=async(_run:any,body:any)=>{requests.push(JSON.parse(JSON.stringify(body)));throw {response:{status:400,data:{error:{code:'manual_action_not_ready',details:['停止原准备后再采用']}}}}}
    await m.state.adopt();assert.equal(m.state.pendingRequest.value,null)
    assert.match(m.state.adoptionError.value,/停止原准备/)
    props.run={...props.run};await settle()
    assert.match(m.state.adoptionError.value,/停止原准备/,'background read must not erase action rejection')
    const html=await renderToString(createSSRApp({...C,setup:()=>m.state},props).use(m.router))
    assert.match(html,/剧本采用未完成/);assert.equal(m.state.revisionId.value,'revision')
    let rejectRequest:(cause:any)=>void=()=>{}
    api.performManualProductionAction=async(_run:any,body:any)=>{requests.push(JSON.parse(JSON.stringify(body)));return new Promise((_resolve,reject)=>{rejectRequest=reject})}
    const submitting=m.state.adopt();await settle();const original=JSON.stringify(m.state.pendingRequest.value),readCount=reads
    assert.equal(m.state.adoptionBusy.value,true);assert.deepEqual(events.at(-1),['contextProtected',true])
    props.run={...props.run};await settle();assert.equal(reads,readCount)
    rejectRequest(Error('network outcome unknown'));await submitting
    assert.equal(JSON.stringify(m.state.pendingRequest.value),original);assert.equal(m.state.adoptionBusy.value,false)
    api.performManualProductionAction=async(_run:any,body:any)=>{requests.push(JSON.parse(JSON.stringify(body)));throw {response:{status:400}}}
    await m.state.adopt();assert.equal(JSON.stringify(m.state.pendingRequest.value),original)
    assert.notEqual(requests[0].idempotency_key,requests[1].idempotency_key)
    assert.deepEqual(requests[1],requests[2])
    api.performManualProductionAction=async(_run:any,body:any)=>{requests.push(JSON.parse(JSON.stringify(body)));return {action:'adopt'}}
    await m.state.adopt();assert.equal(m.state.pendingRequest.value,null)
    assert.equal(m.state.adoptionError.value,'');assert.match(m.state.notice.value,/已明确采用/)
    assert.ok(events.some(row=>row[0]==='changed'));assert.deepEqual(requests[1],requests[3])
  }finally{m.app.unmount()}
}))

test('uncertain or conflicting creation results stay locked and retry the exact request',()=>modules(async(vite,api)=>{
  for(const cause of [new Error('network timeout'),rejection(408),rejection(409),rejection(429),rejection(500),'malformed']){
    const requests:any[]=[]
    api.performManualProductionAction=async(_run:any,body:any)=>{requests.push(JSON.parse(JSON.stringify(body)));if(requests.length===1){if(cause==='malformed')return {};throw cause}return saved}
    const m=await generationForm(vite)
    try{
      await m.state.create();assert.equal(m.state.locked.value,true);assert.equal(m.state.busy.value,false)
      assert.equal(m.state.rejected.value,false);assert.equal(m.state.preparationId.value,'')
      await m.state.create();assert.deepEqual(requests[1],requests[0]);assert.equal(m.state.preparationId.value,saved.target_id)
    }finally{m.app.unmount()}
  }
}))

test('a failed reconciliation cannot unlock an earlier unknown submission',()=>modules(async(vite,api)=>{
  const requests:any[]=[]
  api.performManualProductionAction=async(_run:any,body:any)=>{requests.push(JSON.parse(JSON.stringify(body)));if(requests.length===1)throw new Error('timeout');throw rejection(400)}
  const m=await generationForm(vite)
  try{
    await m.state.create();await m.state.create()
    assert.equal(m.state.locked.value,true);assert.equal(m.state.rejected.value,false);assert.deepEqual(requests[0],requests[1])
  }finally{m.app.unmount()}
}))

test('in-flight double click is ignored and local validation failure remains editable without sending',()=>modules(async(vite,api)=>{
  let calls=0,resolve:any
  api.performManualProductionAction=()=>{calls++;return new Promise(r=>{resolve=r})}
  const m=await generationForm(vite)
  try{
    m.state.draft.value.literals.duration.value=0
    await m.state.create();assert.equal(calls,0);assert.equal(m.state.locked.value,false);assert.equal(m.state.pending.value,null)
    m.state.draft.value.literals.duration.value=60
    const first=m.state.create();await settle();await m.state.create()
    assert.equal(calls,1);assert.equal(m.state.locked.value,true)
    resolve(saved);await first;assert.equal(m.state.preparationId.value,saved.target_id)
  }finally{m.app.unmount()}
}))

test('stage shortcut uses the sole server object but never selects an execution instance',()=>modules(async(vite,api)=>{
  const reads:any[]=[];api.listProductionTargets=async(...args:any[])=>{reads.push(args);return page()}
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualGenerationPreparation.vue')
  const m=await mount(C,{run,options,initialStageKey:'script_generate'})
  try{
    assert.equal(m.state.stageKey.value,'script_generate');assert.equal(m.state.targetKind.value,'project')
    assert.equal(m.state.selectedTarget.value.label,'当前项目：寻找失踪的校花')
    assert.equal(m.state.uniqueTarget.value,true);assert.deepEqual(reads,[['run','project',1]])
    assert.equal(m.state.versionId.value,'');assert.equal(m.state.contract.value,null)
    assert.equal(m.state.pending.value,null)
  }finally{m.app.unmount()}
}))

test('media generation requires an explicit kind and an explicit choice among multiple objects',()=>modules(async(vite,api)=>{
  let reads=0;api.listProductionTargets=async()=>{reads++;return page([target('shot','one'),target('shot','two')])}
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualGenerationPreparation.vue')
  const m=await mount(C,{run,options,initialStageKey:'media_generation'})
  try{
    assert.equal(m.state.targetKind.value,'');assert.equal(reads,0)
    m.state.targetKind.value='shot';await m.state.loadTargets()
    assert.equal(m.state.targetKey.value,'');assert.equal(m.state.uniqueTarget.value,false)
    assert.equal(m.state.targetLabel.value,'选择镜头')
    m.state.targetKey.value='shot:two';m.state.selectTarget()
    assert.equal(m.state.selectedTarget.value.scope.id,'two');assert.equal(m.state.versionId.value,'')
  }finally{m.app.unmount()}
}))

test('a partial page is not a sole target; failed or foreign catalogs cannot retain selection',()=>modules(async(vite,api)=>{
  api.listProductionTargets=async()=>page([target()],2,'next')
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualGenerationPreparation.vue')
  const m=await mount(C,{run,options,initialStageKey:'script_generate'})
  try{
    assert.equal(m.state.uniqueTarget.value,false);assert.equal(m.state.targetKey.value,'')
    api.listProductionTargets=async()=>page();await m.state.loadTargets();assert.equal(m.state.targetKey.value,'project:project')
    api.listProductionTargets=async()=>{throw Error('catalog unavailable')};await m.state.loadTargets()
    assert.equal(m.state.targetKey.value,'');assert.match(m.state.error.value,/catalog unavailable/)
    api.listProductionTargets=async()=>({...page(),runPlanSha256:'b'.repeat(64)});await m.state.loadTargets()
    assert.equal(m.state.targetKey.value,'');assert.match(m.state.error.value,/不一致/)
  }finally{m.app.unmount()}
}))

test('instance lookup failure is explicit and retry can load the sole project',()=>modules(async(vite,_api,registry)=>{
  registry.versions=async()=>{throw Error('instance unavailable')}
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualGenerationPreparation.vue')
  const m=await mount(C,{run,options,initialStageKey:'script_generate'})
  try{
    assert.match(m.state.error.value,/instance unavailable/);assert.equal(m.state.targetKey.value,'')
    registry.versions=async()=>({results:[],next:null});await m.state.changeStage()
    assert.equal(m.state.error.value,'');assert.equal(m.state.targetKey.value,'project:project');assert.equal(m.state.versionId.value,'')
  }finally{m.app.unmount()}
}))

test('closing before the instance response prevents follow-up target queries',()=>modules(async(vite,api,registry)=>{
  let resolve:any;registry.versions=()=>new Promise(r=>{resolve=r});let reads=0
  api.listProductionTargets=async()=>{reads++;return page()}
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualGenerationPreparation.vue')
  const m=await mount(C,{run,options,initialStageKey:'script_generate'});m.app.unmount()
  resolve({results:[],next:null});await settle();assert.equal(reads,0)
}))

test('cancelled step change preserves the current object, instance and unsaved input',()=>modules(async(vite)=>{
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualGenerationPreparation.vue')
  const m=await mount(C,{run,options,initialStageKey:'script_generate'})
  const previous=globalThis.window;globalThis.window={confirm:()=>false} as any
  try{
    m.state.versionId.value='chosen-version';m.state.draft.value={channels:{positive_prompt:'保留此输入'}}
    m.state.stageKey.value='media_generation';await m.state.changeStage()
    assert.equal(m.state.stageKey.value,'script_generate');assert.equal(m.state.targetKey.value,'project:project')
    assert.equal(m.state.versionId.value,'chosen-version');assert.equal(m.state.draft.value.channels.positive_prompt,'保留此输入')
  }finally{m.app.unmount();globalThis.window=previous}
}))

test('an empty target catalog is not replaced by a fabricated current project',()=>modules(async(vite,api)=>{
  api.listProductionTargets=async()=>page([])
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualGenerationPreparation.vue')
  const m=await mount(C,{run,options,initialStageKey:'script_generate'})
  try{assert.equal(m.state.uniqueTarget.value,false);assert.equal(m.state.targetKey.value,'');assert.equal(m.state.selectedTarget.value,undefined)}finally{m.app.unmount()}
}))

for (const [field, requested, handler] of [
  ['stageKey', 'media_generation', 'changeStage'],
  ['targetKind', 'scene', 'loadTargets'],
  ['targetKey', 'scene:archive', 'selectTarget'],
  ['versionId', 'another-version', 'loadContract'],
]) test(`cancelled ${field} selection restores the visible select before confirmation without changing sources`,()=>modules(async(vite,api,registry)=>{
  const m=await generationForm(vite), previous=globalThis.window
  const before=JSON.stringify(m.state.draft.value), current=m.state[field!].value
  const select={value:requested}, event={target:select}
  let confirmations=0, reads=0
  api.listProductionTargets=registry.versions=registry.contract=async()=>{reads++;throw Error('cancel must not read')}
  globalThis.window={confirm:()=>{
    confirmations++;assert.equal(select.value,current)
    assert.equal(m.state[field!].value,current)
    assert.equal(JSON.stringify(m.state.draft.value),before)
    return false
  }} as any
  try{
    await (handler==='loadTargets'?m.state.loadTargets(1,event):m.state[handler!](event));await settle()
    assert.equal(confirmations,1);assert.equal(reads,0);assert.equal(select.value,current)
    assert.equal(m.state[field!].value,current);assert.equal(JSON.stringify(m.state.draft.value),before)
    assert.equal(m.state.selectedTarget.value.scope.id,'project');assert.equal(m.state.pending.value,null)
  }finally{m.app.unmount();globalThis.window=previous}
}))

test('accepted object change discards only this unsaved draft and requires explicit instance selection',()=>modules(async(vite)=>{
  const m=await generationForm(vite),previous=globalThis.window
  let confirmations=0;globalThis.window={confirm:()=>{confirmations++;return true}} as any
  try{
    m.state.targetKind.value='scene';m.state.targets.value=[target('scene','classroom'),target('scene','archive')]
    m.state.targetKey.value=m.state.loadedTargetKey.value='scene:classroom'
    m.state.selectTarget({target:{value:'scene:archive'}});await settle()
    assert.equal(confirmations,1);assert.equal(m.state.selectedTarget.value.scope.id,'archive')
    assert.equal(m.state.loadedTargetKey.value,'scene:archive')
    assert.equal(m.state.draft.value,null);assert.equal(m.state.contract.value,null);assert.equal(m.state.versionId.value,'')
    assert.equal(m.state.pending.value,null);assert.equal(m.state.preparationId.value,'')
  }finally{m.app.unmount();globalThis.window=previous}
}))

test('locked selection changes restore the visible value without confirmation or draft mutation',()=>modules(async(vite)=>{
  const m=await generationForm(vite),previous=globalThis.window
  globalThis.window={confirm:()=>{throw Error('locked selection must not confirm')}} as any
  try{
    m.state.loading.value=true;const before=JSON.stringify(m.state.draft.value)
    for(const [field,handler] of [['stageKey','changeStage'],['targetKind','loadTargets'],['targetKey','selectTarget'],['versionId','loadContract']]){
      const current=m.state[field!].value,select={value:'unexpected'},event={target:select}
      await (handler==='loadTargets'?m.state.loadTargets(1,event):m.state[handler!](event))
      assert.equal(select.value,current);assert.equal(m.state[field!].value,current)
    }
    assert.equal(JSON.stringify(m.state.draft.value),before)
  }finally{m.app.unmount();globalThis.window=previous}
}))

test('confirmed-context selects are controlled and refresh never passes a button event as a selection',()=>{
  const source=readFileSync(new URL('../src/components/ManualGenerationPreparation.vue',import.meta.url),'utf8')
  for(const key of ['stageKey','targetKind','targetKey','versionId']){
    assert.ok(source.includes(`:value="${key}"`));assert.equal(source.includes(`v-model="${key}"`),false)
  }
  assert.ok(source.includes('@change="loadTargets(1, $event)"'))
  assert.ok(source.includes('@click="changeStage()">刷新实例'))
})

test('workbench navigation groups existing actions without imposing sequential gates',()=>modules(async(vite,api)=>{
  api.getManualProductionActions=async()=>options;api.listScriptSources=async()=>[]
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualProductionWorkbench.vue')
  const m=await mount(C,{run,projectName:'测试项目'})
  try{
    assert.equal(m.state.section.value,'script')
    assert.deepEqual(m.state.sectionGenerationActions.value.map((a:any)=>a.stageKey),['script_generate'])
    m.state.openGeneration('script_generate');assert.equal(m.state.generationContext.value.stageKey,'script_generate')
    m.state.closeGeneration();m.state.section.value='media';await settle()
    assert.deepEqual(m.state.sectionGenerationActions.value.map((a:any)=>a.stageKey),['media_generation'])
    m.state.openGeneration('not-allowed');assert.equal(m.state.generationOpen.value,false)
    m.state.openGeneration('media_generation');assert.equal(m.state.generationContext.value.stageKey,'media_generation')
    m.state.closeGeneration();m.state.section.value='records';await settle()
    assert.equal(m.state.sectionGenerationActions.value.length,0)
    m.state.comment.value='保留的剧本输入';m.state.section.value='script';await settle();assert.equal(m.state.comment.value,'保留的剧本输入')
  }finally{m.app.unmount()}
}))

test('preparation pauses parent polling synchronously until close, cancellation keeps the draft protected',()=>modules(async(vite,api)=>{
  api.getManualProductionActions=async()=>options;api.listScriptSources=async()=>[]
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualProductionWorkbench.vue')
  const events:any[][]=[], previous=globalThis.window
  globalThis.window={confirm:()=>false} as any
  const m=await mount(C,{run},(...event)=>events.push(event))
  try{
    m.state.openGeneration('script_generate')
    assert.deepEqual(events.at(-1),['contextProtected',true])
    m.state.generationDirty.value=true;m.state.closeGeneration()
    assert.equal(m.state.generationOpen.value,true);assert.deepEqual(events.at(-1),['contextProtected',true])
    globalThis.window.confirm=()=>true;m.state.closeGeneration()
    assert.deepEqual(events.at(-1),['contextProtected',false])
    m.state.openGeneration('script_generate');m.app.unmount()
    assert.deepEqual(events.at(-1),['contextProtected',false])
  }finally{m.app.unmount();globalThis.window=previous}
}))

test('unknown storyboard approval protects parent refresh, modal closure and route changes until reconciled',()=>modules(async(vite,api)=>{
  api.getManualProductionActions=async()=>options;api.listScriptSources=async()=>[]
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualProductionWorkbench.vue')
  const events:any[][]=[]
  const m=await mount(C,{run},(...event)=>events.push(event))
  try{
    m.state.storyboardReferenceBusy.value=true
    assert.deepEqual(events.at(-1),['contextProtected',true])
    assert.equal(m.state.canClosePreparation(),false);assert.equal(m.state.canLeave(),false)
    m.state.storyboardReferenceBusy.value=false
    assert.deepEqual(events.at(-1),['contextProtected',false]);assert.equal(m.state.canLeave(),true)
  }finally{m.app.unmount()}
}))

test('clean workbench can navigate to shared project script management without an adoption or model command',()=>modules(async(vite,api)=>{
  api.getManualProductionActions=async()=>options;api.listScriptSources=async()=>[]
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualProductionWorkbench.vue')
  const m=await mount(C,{run,projectName:'测试项目'})
  try{
    assert.equal(m.state.canLeave(),true)
    await m.router.push({name:'production-run',params:{projectId:'project'},query:{view:'scripts'}})
    assert.equal(m.router.currentRoute.value.fullPath,'/projects/project/production?view=scripts')
  }finally{m.app.unmount()}
}))

test('background poll does not unmount explicit candidate review or discard its observed adoption basis',()=>modules(async(vite,api)=>{
  const state={...options,actions:[{stageKey:'script_review',action:'approve',sourceStageKeys:['script_generate']}]}
  const review={id:'review',stageKey:'script_review',ordinal:4,status:'awaiting_approval',inputSnapshotSha256:sha}
  api.getManualProductionActions=async()=>state;api.listScriptSources=async()=>[]
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualProductionWorkbench.vue')
  const props=reactive({run:{...run,stages:[review]},projectName:'测试项目'}),m=await mount(C,props)
  try{
    m.state.openTextReview('script_review');const context=m.state.textReviewContext.value
    assert.equal(context.stage.id,'review')
    let finish:any;api.getManualProductionActions=()=>new Promise(resolve=>{finish=resolve})
    props.run={...props.run};await settle()
    assert.deepEqual(m.state.options.value,state);assert.equal(m.state.scriptReviewOpen.value,true)
    assert.equal(m.state.canAct('script_review','approve'),false)
    assert.equal(m.state.textReviewContext.value,context)
    finish({...state,currentScriptDecisionId:'changed-elsewhere'});await settle()
    assert.equal(m.state.textReviewContext.value,context)
    assert.notEqual(m.state.textReviewContext.value.options.currentScriptDecisionId,'changed-elsewhere')
    api.getManualProductionActions=async()=>state
    props.run={...props.run,status:'cancelled'};await settle()
    assert.equal(m.state.terminal.value,true);assert.equal(m.state.textReviewContext.value,context)
    const closedHtml=await renderToString(createSSRApp({...C,setup:()=>m.state},{...props}).use(m.router))
    assert.match(closedHtml,/文本候选只读查看/);assert.doesNotMatch(closedHtml,/确认采用此剧本/)
    m.state.closeTextReview();assert.equal(m.state.textReviewContext.value,null)
    m.state.openTextReview('script_review');props.run={...props.run,id:'other-run'};await settle()
    assert.equal(m.state.scriptReviewOpen.value,false);assert.equal(m.state.textReviewContext.value,null)
  }finally{m.app.unmount()}
}))

test('image review stays mounted across action polling failure and closes on real context change',()=>modules(async(vite,api)=>{
  const reviewOptions={...options,actions:[{stageKey:'bible_image_review',action:'approve'}]}
  api.getManualProductionActions=async()=>reviewOptions;api.listScriptSources=async()=>[]
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualProductionWorkbench.vue')
  const props=reactive({run:{...run}}),m=await mount(C,props)
  try {
    m.state.openBibleImageReview();const context=m.state.bibleImageContext.value
    assert.ok(context);assert.equal(m.state.bibleImageOpen.value,true)
    let fail:any;api.getManualProductionActions=()=>new Promise((_resolve,reject)=>{fail=reject})
    props.run={...props.run};await settle()
    assert.deepEqual(m.state.options.value,reviewOptions);assert.equal(m.state.bibleImageContext.value,context)
    assert.equal(m.state.canAct('bible_image_review','approve'),false)
    m.state.closeBibleImageReview();assert.equal(m.state.bibleImageOpen.value,false)
    assert.equal(m.state.bibleImageContext.value,context)
    m.state.openBibleImageReview();assert.equal(m.state.bibleImageOpen.value,true)
    assert.equal(m.state.bibleImageContext.value,context,'reopen uses observed context even during polling')
    fail(new Error('background read failed'));await settle()
    assert.equal(m.state.options.value,null)
    assert.equal(m.state.bibleImageOpen.value,true);assert.equal(m.state.bibleImageContext.value,context)
    api.getManualProductionActions=async()=>({...reviewOptions,actions:[]})
    props.run={...props.run,status:'cancelled'};await settle()
    assert.equal(m.state.terminal.value,true);assert.equal(m.state.bibleImageContext.value,context)
    props.run={...props.run,executionPlan:{sha256:'b'.repeat(64)}};await settle()
    assert.equal(m.state.bibleImageOpen.value,false);assert.equal(m.state.bibleImageContext.value,null)
    const source=readFileSync(new URL('../src/components/ManualProductionWorkbench.vue',import.meta.url),'utf8')
    assert.match(source,/<ManualBibleImageReview v-if="bibleImageContext" :open="bibleImageOpen" :run="run" :options="bibleImageContext.options"/)
  } finally {m.app.unmount()}
}))

test('retained image review guards leaving the page but not closing another form, and stays scoped to its run',()=>modules(async(vite,api)=>{
  const reviewOptions={...options,actions:[...actions,{stageKey:'bible_image_review',action:'approve'}]}
  api.getManualProductionActions=async()=>reviewOptions;api.listScriptSources=async()=>[]
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualProductionWorkbench.vue')
  const props=reactive({run:{...run}}),previous=globalThis.window,questions:string[]=[],events:any[][]=[]
  globalThis.window={confirm:(message:string)=>{questions.push(message);return false}} as any
  const m=await mount(C,props,(...event)=>events.push(event))
  try {
    m.state.openBibleImageReview();const context=m.state.bibleImageContext.value
    assert.deepEqual(events.at(-1),['contextProtected',true])
    m.state.bibleImageRetained.value={dirty:true,pending:false}
    m.state.bibleImageBusy.value=true;m.state.closeBibleImageReview()
    assert.equal(m.state.bibleImageOpen.value,true);assert.equal(m.state.canLeave(),false);assert.equal(questions.length,0)
    m.state.bibleImageBusy.value=false;m.state.closeBibleImageReview()
    assert.equal(m.state.bibleImageOpen.value,false);assert.equal(m.state.bibleImageContext.value,context)
    assert.deepEqual(events.at(-1),['contextProtected',true],'closed dirty review must remain protected from whole-page failures')
    m.state.openGeneration('script_generate');m.state.closeGeneration()
    assert.equal(m.state.generationOpen.value,false);assert.equal(questions.length,0)
    await m.router.push('/workflows');assert.equal(m.router.currentRoute.value.path,'/')
    assert.match(questions.at(-1)!,/未提交/)
    m.state.bibleImageRetained.value={dirty:false,pending:true}
    assert.deepEqual(events.at(-1),['contextProtected',true])
    assert.equal(m.state.canLeave(),false);assert.match(questions.at(-1)!,/可能已经写入/)
    globalThis.window.confirm=()=>true;assert.equal(m.state.canLeave(),true)
    props.run={...props.run,id:'other-run'};await settle()
    assert.equal(m.state.bibleImageContext.value,null)
    assert.deepEqual(m.state.bibleImageRetained.value,{dirty:false,pending:false})
    assert.deepEqual(events.at(-1),['contextProtected',false])
  }finally{m.app.unmount();globalThis.window=previous}
}))

test('closed manual workbench exposes script and visual result viewing while new generation remains disabled',()=>modules(async(vite,api)=>{
  const reviews=[{id:'script-review',stageKey:'script_review',ordinal:4,status:'awaiting_approval'},{id:'visual-review',stageKey:'visual_rules_review',ordinal:6,status:'awaiting_approval'}]
  api.getManualProductionActions=async()=>({...options,actions:[...actions,...reviews.map(row=>({stageKey:row.stageKey,action:'approve',sourceStageKeys:[]}))]})
  api.listScriptSources=async()=>[]
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualProductionWorkbench.vue')
  for(const status of ['succeeded','failed','cancelled']) {
    const props={run:{...run,status,stages:reviews},projectName:'Closed results'},m=await mount(C,props)
    try {
      for(const key of ['script_review','visual_rules_review']) {
        m.state.openTextReview(key);assert.equal(m.state.scriptReviewOpen.value,true)
        assert.equal(m.state.textReviewContext.value.stage.stageKey,key)
        m.state.closeTextReview()
      }
      const html=await renderToString(createSSRApp({...C,setup:()=>m.state},props).use(m.router))
      assert.match(html,/查看生成的剧本（只读）/)
      assert.match(html,/<button[^>]*disabled[^>]*>新建 AI 剧本生成 候选/)
      m.state.section.value='visual';await settle()
      const visualHtml=await renderToString(createSSRApp({...C,setup:()=>m.state},props).use(m.router))
      assert.match(visualHtml,/<button(?![^>]*disabled)[^>]*>查看影视规则候选（只读）/)
      m.state.openGeneration('script_generate');assert.equal(m.state.generationOpen.value,false)
    } finally {m.app.unmount()}
  }
}))

test('current script renders separate identities and collapses replacement without implying repeat adoption',()=>modules(async(vite,api)=>{
  const current={kind:'script_revision',id:'revision-id',source_id:'source-id',revision:2,sha256:sha,confirmed_at:'2026-09-23T01:00:00Z'}
  const identity={sourceName:'',sourceTypeLabel:'粘贴',sourceFormatLabel:'纯文本',decisionStageKey:'script_review',
    candidate:{artifactId:'candidate-id',variantIndex:3,stageId:'stage-id',stageKey:'script_generate',attemptId:'attempt-id',preparationId:'preparation-id'}}
  api.getManualProductionActions=async()=>({...options,currentScript:current,currentScriptDecisionId:'decision-id',currentScriptIdentity:identity})
  api.listScriptSources=async()=>[]
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualProductionWorkbench.vue')
  const props={run,projectName:'测试项目'},m=await mount(C,props)
  const render=()=>renderToString(createSSRApp({...C,setup:()=>m.state},props).use(m.router))
  try{
    const html=await render()
    for(const id of ['source-id','revision-id','decision-id','candidate-id']) assert.match(html,new RegExp(id))
    assert.match(html,/未命名原稿 · 修订 2/);assert.match(html,/候选 3，无需再次采用同一修订/)
    assert.match(html,/href="\/projects\/project\/production\?view=scripts&amp;run=run"/)
    assert.match(html,/导入／编辑项目剧本（不调用模型）/)
    assert.match(html,/不是候选 ID/);assert.match(html,/stage_run_id=stage-id/);assert.match(html,/preparation-id/)
    assert.match(html,/<details class="script-reselection"[^>]*>/)
    assert.doesNotMatch(html,/<details class="script-reselection"[^>]* open/)
    assert.equal(m.state.pendingRequest.value,null)
    m.state.options.value={...options,currentScriptIdentity:null};await settle()
    const blank=await render();assert.match(blank,/<details class="script-reselection"[^>]* open/)
    assert.match(blank,/本轮尚未采用剧本/);assert.doesNotMatch(blank,/candidate-id/)
    m.state.options.value={...options,currentScript:current,currentScriptIdentity:null};await settle()
    assert.match(await render(),/采用身份摘要缺失/)
  }finally{m.app.unmount()}
}))

test('closed manual run and records retain real call states even without generation actions',()=>modules(async(vite,api)=>{
  const stage={id:'stage',stageKey:'script_generate',status:'running'}
  const activity={stageId:'stage',stageKey:'script_generate',stageStatus:'running',lifecycleLabel:'步骤开放',preparations:[],
    calls:[{role:'primary',roleLabel:'主调用',status:'succeeded',label:'成功',count:1}]}
  api.getManualProductionActions=async()=>({...options,actions:[],executionActivity:[activity]});api.listScriptSources=async()=>[]
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualProductionWorkbench.vue')
  const props=reactive({run:{...run,status:'cancelled',stages:[stage]},projectName:'测试项目'})
  const m=await mount(C,props)
  const render=()=>renderToString(createSSRApp({...C,setup:()=>m.state},props).use(m.router))
  try{
    assert.equal(m.state.sectionGenerationActions.value.length,0)
    for(const section of ['script','records']){
      m.state.section.value=section;await settle()
      const html=await render()
      assert.match(html,/主调用：成功 1 次/);assert.match(html,/步骤开放/);assert.doesNotMatch(html,/运行中/)
      assert.match(html,/stage_run_id=stage/)
    }
    m.state.busy.value=true
    const pending=await render();assert.match(pending,/正在读取调用状态/);assert.doesNotMatch(pending,/主调用：成功 1 次/)
    m.state.comment.value='尚未提交的说明'
    api.getManualProductionActions=async()=>({...options,actions:[],executionActivity:[{...activity,calls:[{...activity.calls[0],count:2}]}]})
    props.run={...props.run};await settle()
    assert.equal(m.state.stageActivity('stage').calls[0].count,1)
    m.state.busy.value=false
    props.run={...props.run};await settle()
    assert.equal(m.state.stageActivity('stage').calls[0].count,2)
    assert.equal(m.state.comment.value,'尚未提交的说明');assert.equal(m.state.section.value,'records')
  }finally{m.app.unmount()}
}))
