import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'
import { createRouter, createMemoryHistory } from 'vue-router'
import { createPinia } from 'pinia'

const root = fileURLToPath(new URL('..', import.meta.url)), sha = 'a'.repeat(64)
const options = { projectId: 'project', scopeKinds: ['project'], deliveryKinds: ['episode_masters'], initialContentKinds: ['script_revision'], stageCatalog: { value: { key: 'test-catalog' }, sha256: sha } }
const revision = { id: 'script', source: 'source', status: 'confirmed', revision: 1, contentSha256: sha, content: '本轮采用的真实剧本' }
const run = { id: 'run', project: 'project', planSource: 'manual', status: 'awaiting_input', scriptRevision: revision.id, recipeVersion: null, createdAt: '2026-09-17T00:00:00Z', updatedAt: '2026-09-17T00:00:00Z' }
const settle = async () => { for (let i=0;i<12;i++) { await Promise.resolve(); await nextTick() } }
const projectFixture = (id='project') => ({id,title:id,translationGlossary:{},bible:{characters:[],scenes:[],props:[],negativePrompt:'',colorScript:''},episodes:[]})
function deferred() { let resolve!: (v:any)=>void, reject!: (e:Error)=>void; const promise = new Promise<any>((yes,no)=>{resolve=yes;reject=no}); return {promise,resolve,reject} }
async function modules(fn) {
  const vite = await createServer({ root, logLevel:'silent', appType:'custom', server:{middlewareMode:true,hmr:false} })
  try { await fn(vite) } finally { await vite.close() }
}
async function mount(Component, props, path='/projects/project', pinia=createPinia()) {
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const router=createRouter({history:createMemoryHistory(),routes:[
    {path:'/projects',name:'projects',component:{render:()=>null}},
    {path:'/projects/new',name:'project-new',component:{render:()=>null}},
    {path:'/projects/:projectId/:rest(.*)*',component:{render:()=>null}},
  ]})
  await router.push(path); await router.isReady()
  let state:any; const emitted:any[]=[]
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit(...args){emitted.push(args)}});return()=>h('div')}})
  app.use(router);app.use(pinia);app.provide(ssrContextKey,{modules:new Set()});app.mount({});await settle()
  return {state,router,app,emitted}
}

test('run navigation keeps independent creation explicit and never picks an ambiguous run',()=>modules(async vite=>{
  const m=await vite.ssrLoadModule('/src/services/productionRunPresentation.ts')
  assert.equal(m.productionModeLabel(run),'无配方人工制作')
  assert.equal(m.productionModeLabel({...run,planSource:'recipe'}),'按配方生产')
  for(const view of ['new','list',['new']])assert.equal(m.productionRunRestoreTarget([run],'',view),'')
  assert.equal(m.projectRunSelection([{...run,status:'succeeded'}]),'run')
  assert.equal(m.projectRunSelection([run,{...run,id:'other'}]),'')
  assert.equal(m.projectRunSelection([run,{...run,id:'other'}],'other'),'other')
  assert.equal(m.projectRunSelection([run],'foreign'),'')
}))

test('manual onboarding uses the server catalog and confirmed revision, not a hidden recipe',()=>modules(async vite=>{
  const m=await vite.ssrLoadModule('/src/services/productionOnboarding.ts')
  const input={projectId:'project',idempotencyKey:'key',revision,allowExternalCalls:false}
  const value=m.onboardingManualInput(options,input)
  assert.equal(value.planSource,'manual');assert.equal('recipeVersionId' in value,false)
  assert.equal('maxGenerationTasks' in value,false)
  assert.deepEqual(value.manualPlan.initialContent,{kind:'script_revision',id:'script',expectedSha256:sha})
  assert.equal(value.manualPlan.stageCatalogKey,'test-catalog');assert.equal(value.manualPlan.expectedStageCatalogSha256,sha)
  assert.equal(m.onboardingManualInput(options,{...input,revision:null}).manualPlan.initialContent,null)
  for(const patch of [{projectId:'foreign'},{scopeKinds:[]},{deliveryKinds:[]},{initialContentKinds:[]}])assert.throws(()=>m.onboardingManualInput({...options,...patch},input))
  assert.throws(()=>m.onboardingManualInput(options,{...input,revision:{...revision,status:'preview'}}))
  for(const status of [408,409,429,500])assert.equal(m.isDefinitiveCreationRejection({response:{status}}),false)
  assert.equal(m.isDefinitiveCreationRejection({response:{status:400}}),true)
  assert.equal(m.isDefinitiveCreationRejection(Error('lost response')),false)
}))

test('project manual creation leaves call limits to the server without granting external access',()=>modules(async vite=>{
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),old={...platformApi},calls:any[]=[]
  platformApi.getManualProductionOptions=async()=>options
  platformApi.createProductionRun=async input=>{calls.push(input);return run}
  const {default:Component}=await vite.ssrLoadModule('/src/components/ManualProductionCreateForm.vue')
  const mounted=await mount(Component,reactive({projectId:'project',episodes:[]}))
  try{
    const s=mounted.state;s.scopeKind.value='project';s.deliveryKind.value='episode_masters';s.initialKind.value='blank'
    await settle();assert.equal(Boolean(s.ready.value),true);await s.create()
    assert.equal(calls.length,1);assert.equal('maxGenerationTasks' in calls[0],false)
    assert.equal(calls[0].allowExternalCalls,false);assert.equal('budgetLimit' in calls[0],false);assert.equal('budgetCurrency' in calls[0],false)
    const {buildProductionRunRequestPayload}=await vite.ssrLoadModule('/src/services/productionRunPayload.ts')
    assert.equal('max_generation_tasks' in buildProductionRunRequestPayload(calls[0]),false)
  }finally{mounted.app.unmount();Object.assign(platformApi,old)}
}))

test('manual creation freezes unknown requests and reopens confirmed receipts without another POST',()=>modules(async vite=>{
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),old={...platformApi},calls:any[]=[]
  platformApi.getManualProductionOptions=async()=>options
  const pending=deferred()
  platformApi.createProductionRun=input=>{calls.push(JSON.parse(JSON.stringify(input)));return calls.length===1?pending.promise:Promise.resolve(run)}
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualProductionCreateForm.vue')
  const m=await mount(C,reactive({projectId:'project',episodes:[]}))
  try{
    const s=m.state;s.scopeKind.value='episodes';s.episodeIds.value=['episode-a'];s.deliveryKind.value='episode_masters';s.initialKind.value='blank'
    const request=s.create();await s.create();assert.equal(calls.length,1)
    s.episodeIds.value.push('episode-b')
    pending.reject(Error('response lost'));await request
    assert.deepEqual(s.creationRequest.value.manualPlan.scope.episodeIds,['episode-a'])
    const originalKey=s.idempotencyKey.value
    await s.load();assert.ok(s.creationRequest.value)
    await s.create();assert.equal(calls.length,2);assert.deepEqual(calls[0],calls[1]);assert.equal(s.idempotencyKey.value,originalKey)
    assert.equal(s.createdRun.value.id,'run');assert.equal(m.emitted.length,1)
    await s.create();assert.equal(calls.length,2);assert.equal(m.emitted.length,2);assert.equal(m.emitted[1][1].id,'run')
  }finally{m.app.unmount();Object.assign(platformApi,old)}
}))

test('definitive manual rejection permits corrected input under a fresh key, but invalid receipts stay frozen',()=>modules(async vite=>{
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),old={...platformApi}
  platformApi.getManualProductionOptions=async()=>options
  platformApi.createProductionRun=async()=>{throw Object.assign(Error('invalid request'),{response:{status:400}})}
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualProductionCreateForm.vue')
  const m=await mount(C,reactive({projectId:'project',episodes:[]}))
  try{
    const s=m.state;s.scopeKind.value='project';s.deliveryKind.value='episode_masters';s.initialKind.value='blank'
    const originalKey=s.idempotencyKey.value;await s.create()
    assert.equal(s.creationRequest.value,null);assert.notEqual(s.idempotencyKey.value,originalKey);assert.equal(Boolean(s.ready.value),true)
    platformApi.createProductionRun=async()=>({...run,project:'foreign'})
    await s.create();assert.match(s.error.value,/不一致/);assert.ok(s.creationRequest.value);assert.equal(s.createdRun.value,null);assert.equal(m.emitted.length,0)
  }finally{m.app.unmount();Object.assign(platformApi,old)}
}))

test('manual creation ignores late receipts across project, session and unmount boundaries',()=>modules(async vite=>{
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),old={...platformApi}
  const {useStudioStore}=await vite.ssrLoadModule('/src/stores/studio.ts')
  const {default:C}=await vite.ssrLoadModule('/src/components/ManualProductionCreateForm.vue')
  const storage=globalThis.localStorage;globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}} as any
  try{
    for(const boundary of ['project','session','unmount'])for(const failure of [false,true]){
      const slow=deferred(),props=reactive({projectId:'project',episodes:[]}),pinia=createPinia()
      platformApi.getManualProductionOptions=async id=>({...options,projectId:id})
      platformApi.createProductionRun=()=>slow.promise
      const m=await mount(C,props,undefined,pinia)
      try{
        const s=m.state;s.scopeKind.value='project';s.deliveryKind.value='episode_masters';s.initialKind.value='blank'
        const request=s.create()
        if(boundary==='project'){props.projectId='other';await settle();assert.equal(s.saving.value,false);assert.equal(s.creationRequest.value,null)}
        if(boundary==='session')useStudioStore(pinia).resetSessionState()
        if(boundary==='unmount')m.app.unmount()
        s.error.value='current message'
        if(failure)slow.reject(Error('old error'));else slow.resolve(run)
        await request;assert.equal(s.error.value,'current message');assert.equal(m.emitted.length,0);assert.equal(s.createdRun.value,null)
      }finally{if(boundary!=='unmount')m.app.unmount()}
    }
  }finally{Object.assign(platformApi,old);globalThis.localStorage=storage}
}))

test('project versions require explicit selection and clear stale run reads',()=>modules(async vite=>{
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const old={getProductionRun:platformApi.getProductionRun,getScriptRevision:platformApi.getScriptRevision,getRecipe:platformApi.getRecipe}
  const slow=deferred(),calls:string[]=[]
  platformApi.getProductionRun=async id=>{calls.push(id);return id==='run'?slow.promise:{...run,id,scriptRevision:'other-script'}}
  platformApi.getScriptRevision=async()=>{throw Error('list must not read script')}
  platformApi.getRecipe=async()=>{throw Error('manual must not read recipe')}
  const {default:Component}=await vite.ssrLoadModule('/src/components/ProjectVersionWorkspace.vue')
  const props=reactive({projectId:'project',title:'project',runs:[run],error:'',loading:false}), mounted=await mount(Component,props)
  try{
    assert.equal(mounted.state.detail.value,null);assert.equal(calls.length,0)
    await mounted.router.replace({query:{run:'run'}});await settle()
    props.runs=[run,{...run,id:'other'}];await mounted.router.replace({query:{run:'other'}});await settle()
    assert.equal(mounted.state.detail.value.scriptRevision,'other-script')
    slow.resolve(run);await settle()
    assert.equal(mounted.state.detail.value.id,'other')
    await mounted.router.replace({query:{run:'missing'}});await settle()
    assert.equal(mounted.state.detail.value,null)
    assert.match(mounted.state.detailError.value,/不属于本项目/)
    props.runs=[{...run,id:'foreign'}];await mounted.router.replace({query:{run:'foreign'}})
    platformApi.getProductionRun=async()=>({...run,project:'foreign'})
    await mounted.state.load();assert.match(mounted.state.detailError.value,/不一致/)
  }finally{mounted.app.unmount();Object.assign(platformApi,old)}
}))

test('shot execution evidence is explicit, guarded and never writes a translation or starts a task',()=>modules(async vite=>{
  const {repository}=await vite.ssrLoadModule('/src/services/repository.ts')
  const old={getShotGenerationTasks:repository.getShotGenerationTasks,getWorkflow:repository.getWorkflow,getTaskInputPrompt:repository.getTaskInputPrompt}
  const task={id:'task',taskId:'task',projectId:'project',execution:{unitKey:'shot:shot',preparationId:'prep',productionRunId:'run'},
    inputContractStatus:'available',inputParameters:{preparation_id:'prep',attempt_id:'attempt',request_sha256:'request',execution_instance_version_id:'version'}}
  let calls=0
  repository.getShotGenerationTasks=async(projectId,shotId)=>{assert.equal(projectId,'project');assert.equal(shotId,'shot');calls++;return [task]}
  repository.getWorkflow=async()=>task
  const evidence={taskId:'task',preparationId:'prep',attemptId:'attempt',requestSha256:'request',executionInstanceVersionId:'version',promptMode:'manual',channels:[{key:'positive_prompt',text:'Actual frozen text'}]}
  repository.getTaskInputPrompt=async()=>evidence
  const {default:Component}=await vite.ssrLoadModule('/src/components/ShotExecutionEvidence.vue')
  const props=reactive({projectId:'project',shotId:'shot'}), mounted=await mount(Component,props)
  try{
    assert.equal(calls,0);await mounted.state.load();assert.equal(calls,1)
    assert.equal(mounted.state.prompt.value.channels[0].text,'Actual frozen text')
    repository.getTaskInputPrompt=async()=>({taskId:'task',preparationId:'foreign'})
    await mounted.state.select();assert.equal(mounted.state.prompt.value,null);assert.match(mounted.state.error.value,/不一致/)
    for(const key of ['attemptId','requestSha256','executionInstanceVersionId']){
      repository.getTaskInputPrompt=async()=>({...evidence,[key]:'another'})
      await mounted.state.select();assert.equal(mounted.state.task.value,null);assert.match(mounted.state.error.value,/不一致/)
    }
    repository.getWorkflow=async()=>({...task,id:'another'});await mounted.state.select()
    assert.equal(mounted.state.task.value,null);assert.match(mounted.state.error.value,/所选记录/)
    repository.getWorkflow=async()=>task
    const slow=deferred();repository.getTaskInputPrompt=async()=>slow.promise
    const request=mounted.state.select();await settle();props.shotId='another';await settle()
    slow.resolve({taskId:'task',preparationId:'prep',promptMode:'manual'});await request
    assert.equal(mounted.state.prompt.value,null);assert.equal(mounted.state.open.value,false)
  }finally{mounted.app.unmount();Object.assign(repository,old)}
}))

for (const rejected of [false, true]) test(`new series retries after ${rejected ? 'a 400 rejection' : 'a lost response'} without recreating the project`,()=>modules(async vite=>{
  const previousStorage=globalThis.localStorage
  globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}} as any
  const pinia=createPinia(),{useStudioStore}=await vite.ssrLoadModule('/src/stores/studio.ts'),studio=useStudioStore(pinia)
  const {repository}=await vite.ssrLoadModule('/src/services/repository.ts'),{platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const old={getProjectCreationCapability:repository.getProjectCreationCapability,listRecipes:platformApi.listRecipes,getManualProductionOptions:platformApi.getManualProductionOptions,createProductionRun:platformApi.createProductionRun}
  studio.initialize=async()=>{throw Error('creation must not load tasks')};studio.styles=[{id:'style',name:'测试风格',description:'',tags:[],isActive:true}];studio.loadStyles=async()=>{};let created=0;studio.createProject=async()=>{created++;return{id:'project'}}
  repository.getProjectCreationCapability=async()=>({episodeCount:1,shotsPerEpisode:2,episodeDurationSeconds:4,maxGenerationTasks:10,defaultLanguage:'zh',defaultAspectRatio:'9:16',aspectRatioOptions:[{value:'9:16',width:352,height:608}]})
  platformApi.listRecipes=async()=>[];platformApi.getManualProductionOptions=async()=>options
  const calls:any[]=[]
  const failure = rejected ? Object.assign(Error('request rejected'), {response:{status:400,
    data:{error:{code:'invalid',details:{non_field_errors:['request rejected']}}}}}) : Error('lost response')
  platformApi.createProductionRun=async input=>{calls.push(JSON.parse(JSON.stringify(input)));if(calls.length===1)throw failure;return run}
  const {default:Component}=await vite.ssrLoadModule('/src/views/SeriesStudioView.vue')
  const mounted=await mount(Component,{},'/projects/new',pinia)
  let destination='';mounted.router.replace=async value=>{destination=String(value)}
  try{
    assert.equal('defaultMode' in mounted.state.newProject.value,false)
    mounted.state.newProject.value.title='测试';mounted.state.newProject.value.styleId='style';mounted.state.aiAllowExternalCalls.value=false
    await mounted.state.createProject();assert.equal(created,1);assert.equal(calls.length,1)
    assert.equal('maxGenerationTasks' in calls[0],false)
    assert.ok(mounted.state.creationError.value.includes(failure.message))
    const {buildProductionRunRequestPayload}=await vite.ssrLoadModule('/src/services/productionRunPayload.ts')
    const wire=buildProductionRunRequestPayload(calls[0])
    assert.equal('budget_currency' in wire,false);assert.equal('budget_limit' in wire,false)
    assert.equal('aiBudgetLimit' in mounted.state,false);await mounted.state.createProject()
    assert.equal(created,1);assert.equal(calls.length,2)
    if (rejected) {
      assert.notEqual(calls[0].idempotencyKey,calls[1].idempotencyKey)
      assert.deepEqual({...calls[0],idempotencyKey:''},{...calls[1],idempotencyKey:''})
    } else assert.deepEqual(calls[0],calls[1])
    assert.equal(destination,'/projects/project/production/run')
  }finally{mounted.app.unmount();globalThis.localStorage=previousStorage;repository.getProjectCreationCapability=old.getProjectCreationCapability;Object.assign(platformApi,{listRecipes:old.listRecipes,getManualProductionOptions:old.getManualProductionOptions,createProductionRun:old.createProductionRun})}
}))

test('schema3 onboarding gates project and run creation on the exact recipe interaction mode',()=>modules(async vite=>{
  const previousStorage=globalThis.localStorage;globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}} as any
  const {repository}=await vite.ssrLoadModule('/src/services/repository.ts'),{platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const oldCapability=repository.getProjectCreationCapability,old={...platformApi}
  const {useStudioStore}=await vite.ssrLoadModule('/src/stores/studio.ts')
  const {default:Component}=await vite.ssrLoadModule('/src/views/SeriesStudioView.vue')
  try{
    for(const mode of ['unattended','assisted','unknown']){
      const pinia=createPinia(),studio=useStudioStore(pinia);let projects=0;const writes:any[]=[]
      studio.initialize=async()=>{throw Error('creation must not load tasks')}
      studio.styles=[{id:'style',name:'测试风格',description:'',tags:[],isActive:true}];studio.loadStyles=async()=>{}
      studio.createProject=async()=>{projects++;return{id:'project'}}
      repository.getProjectCreationCapability=async()=>({episodeCount:1,shotsPerEpisode:2,episodeDurationSeconds:4,defaultLanguage:'zh',defaultAspectRatio:'9:16',aspectRatioOptions:[{value:'9:16',width:352,height:608}]})
      platformApi.listRecipes=async()=>[{id:'recipe',definition:{schema_version:3,interaction_mode:mode,stages:[
        {key:'script_generate',mode:'ai',approval:'auto'},{key:'script_review',mode:'review',approval:'auto'}]}}]
      platformApi.createProductionRun=async input=>{writes.push(input);return{...run,planSource:'recipe'}}
      const mounted=await mount(Component,{},'/projects/new',pinia)
      try{
        const s=mounted.state;s.newProject.value.styleId='style';s.creationPlanSource.value='recipe';s.creationMode.value='ai';s.aiIdea.value='测试故事';await settle()
        s.creationRecipeId.value='recipe';await settle()
        s.projectSourceReady.value=true // Source confirmation is exercised separately from this setup-only harness.
        assert.equal(s.creationIsUnattendedRecipe.value,mode==='unattended')
        if(mode==='unattended'){
          await s.createProject();assert.match(s.creationError.value,/请明确确认/);assert.equal(projects,0);assert.equal(writes.length,0)
          s.creationUnattendedConfirmed.value=true
          s.creationRecipeId.value='';await settle();assert.equal(s.creationUnattendedConfirmed.value,false)
          s.creationRecipeId.value='recipe';await settle();s.projectSourceReady.value=true
        }
        s.creationUnattendedConfirmed.value=true
        await s.createProject()
        if(mode==='unknown'){
          assert.match(s.creationError.value,/交互模式/);assert.equal(projects,0);assert.equal(writes.length,0)
        }else{
          assert.equal(projects,1);assert.equal(writes.length,1)
          const {buildProductionRunRequestPayload}=await vite.ssrLoadModule('/src/services/productionRunPayload.ts')
          assert.equal(buildProductionRunRequestPayload(writes[0]).unattended_confirmed,mode==='unattended'?true:undefined)
        }
      }finally{mounted.app.unmount()}
    }
  }finally{globalThis.localStorage=previousStorage;repository.getProjectCreationCapability=oldCapability;Object.assign(platformApi,old)}
}))

for (const mode of ['manual','recipe']) test(`${mode} import confirms once and starts the selected run without repeating project or import`,()=>modules(async vite=>{
  const previousStorage=globalThis.localStorage;globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}} as any
  const pinia=createPinia(),{useStudioStore}=await vite.ssrLoadModule('/src/stores/studio.ts'),studio=useStudioStore(pinia)
  const {repository}=await vite.ssrLoadModule('/src/services/repository.ts'),{platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const oldCapability=repository.getProjectCreationCapability, old={...platformApi}
  let projects=0,imports=0,confirms=0,reads=0;const calls:any[]=[]
  studio.initialize=async()=>{throw Error('creation must not load tasks')};studio.styles=[{id:'style',name:'测试风格',description:'',tags:[],isActive:true}];studio.loadStyles=async()=>{};studio.createProject=async()=>{projects++;return{id:'project'}}
  repository.getProjectCreationCapability=async()=>({episodeCount:1,shotsPerEpisode:2,episodeDurationSeconds:4,maxGenerationTasks:10,defaultLanguage:'zh',defaultAspectRatio:'9:16',aspectRatioOptions:[{value:'9:16',width:352,height:608}]})
platformApi.listRecipes=async()=>[{id:'recipe',defaultBudgetLimit:'2.5000',defaultBudgetCurrency:'USD',defaultMaxGenerationTasks:8,definition:{schema_version: 3, interaction_mode: 'assisted', stages:[{key:'script_input',mode:'confirmed_revision',approval:'auto'}]}}]
  platformApi.getManualProductionOptions=async()=>options
  platformApi.createPastedScript=async()=>{imports++;return{revision:{...revision,status:'preview',content:undefined}}}
  platformApi.getScriptRevision=async()=>{reads++;if(reads===1)throw Error('preview detail unavailable');return{...revision,status:'preview'}}
  platformApi.confirmScriptRevision=async()=>{confirms++;return revision}
  platformApi.createProductionRun=async input=>{calls.push(JSON.parse(JSON.stringify(input)));if(calls.length===1)throw Error('lost run response');return{...run,planSource:mode}}
  const {default:Component}=await vite.ssrLoadModule('/src/views/SeriesStudioView.vue'),mounted=await mount(Component,{},'/projects/new',pinia)
  let destination='';mounted.router.replace=async value=>{destination=String(value)}
  try{
    const s=mounted.state;s.newProject.value.styleId='style';s.creationPlanSource.value=mode;s.creationMode.value='import';s.importText.value='输入剧本';s.aiAllowExternalCalls.value=false;await settle()
    if(mode==='recipe'){
      assert.equal('aiBudgetLimit' in s,false);assert.equal('aiBudgetCurrency' in s,false)
      assert.equal(s.aiAllowExternalCalls.value,false)
    }else{
      // A stale recipe edit must never authorize the manual onboarding path.
      s.aiAllowExternalCalls.value=true
    }
    await s.createProject();assert.match(s.creationError.value,/preview detail unavailable/);assert.equal(s.importReviewOpen.value,false)
    await s.createProject();assert.equal(s.importPreview.value.content,revision.content);assert.equal(s.importReviewOpen.value,true)
    assert.equal(projects,1);assert.equal(imports,1);assert.equal(confirms,0)
    if(mode==='recipe'){
      await s.confirmImportedScriptAndContinue()
      assert.match(s.creationError.value,/尚未创建模型任务/);assert.equal(calls.length,0)
      assert.equal(confirms,1,'script confirmation remains separate from project snapshot confirmation')
      s.projectSourceReady.value=true // Contract/confirmation child is tested separately; this harness renders only setup.
    }
    await s.confirmImportedScriptAndContinue();assert.match(s.creationError.value,/lost run response/)
    await s.confirmImportedScriptAndContinue();assert.equal(confirms,1);assert.deepEqual(calls[0],calls[1]);assert.equal(calls[0].planSource,mode)
    if(mode==='manual'){assert.equal(calls[0].manualPlan.initialContent.id,revision.id);assert.equal('recipeVersionId' in calls[0],false);assert.equal(calls[0].allowExternalCalls,false);assert.equal('budgetLimit' in calls[0],false);assert.equal('budgetCurrency' in calls[0],false)}
    else{assert.equal(calls[0].recipeVersionId,'recipe');assert.equal(calls[0].scriptRevisionId,revision.id);assert.equal('budgetLimit' in calls[0],false);assert.equal('budgetCurrency' in calls[0],false);assert.equal(calls[0].allowExternalCalls,false)}
    assert.equal('maxGenerationTasks' in calls[0],false)
    const {buildProductionRunRequestPayload}=await vite.ssrLoadModule('/src/services/productionRunPayload.ts')
    assert.equal('max_generation_tasks' in buildProductionRunRequestPayload(calls[0]),false)
    assert.equal(destination,'/projects/project/production/run')
  }finally{mounted.app.unmount();globalThis.localStorage=previousStorage;repository.getProjectCreationCapability=oldCapability;Object.assign(platformApi,old)}
}))

test('creation loads independently, lazily loads recipes and preserves edits across failed retries',()=>modules(async vite=>{
  const previousStorage=globalThis.localStorage;globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}} as any
  const pinia=createPinia(),{useStudioStore}=await vite.ssrLoadModule('/src/stores/studio.ts'),studio=useStudioStore(pinia)
  const {repository}=await vite.ssrLoadModule('/src/services/repository.ts'),{platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const old={...repository},oldRecipes=platformApi.listRecipes,policy=deferred(),styles=deferred()
  let recipeReads=0,projectWrites=0
  repository.getStyles=async()=>styles.promise
  repository.getProjectCreationCapability=async()=>policy.promise
  repository.getProductionCapability=async()=>{throw Error('full capability must not be loaded')}
  studio.initialize=async()=>{throw Error('task history must not be loaded')}
  studio.createProject=async()=>{projectWrites++;return{id:'unexpected'}}
  platformApi.listRecipes=async()=>{recipeReads++;throw Error('recipe unavailable')}
  const {default:Component}=await vite.ssrLoadModule('/src/views/SeriesStudioView.vue'),mounted=await mount(Component,{},'/projects/new',pinia),s=mounted.state
  try{
    assert.equal(s.loading.value,false);assert.equal(s.creationStylesLoading.value,true);assert.equal(s.creationPolicyLoading.value,true)
    assert.equal(s.newProject.value.styleId,'');assert.equal(recipeReads,0)
    s.newProject.value.title='用户输入保留';s.aiEpisodeCount.value=7
    await s.createProject();assert.equal(projectWrites,0)
    styles.resolve([{id:'style',name:'真实风格',description:'真实说明',tags:[],isActive:true}]);await settle()
    assert.equal(s.creationStylesLoading.value,false);assert.equal(s.creationPolicyLoading.value,true)
    s.newProject.value.styleId='style'
    const defaults={episodeCount:1,shotsPerEpisode:2,episodeDurationSeconds:4,maxGenerationTasks:10,defaultLanguage:'zh',defaultAspectRatio:'9:16',aspectRatioOptions:[{value:'9:16',width:352,height:608},{value:'1:1',width:512,height:512}]}
    policy.resolve(defaults);await settle();assert.equal(s.creationReady.value,true);assert.equal(s.aiEpisodeCount.value,7)
    s.applyNewProjectAspectRatio('1:1');assert.equal(s.newProjectDimensions.value,'512 × 512 px')
    repository.getProjectCreationCapability=async()=>{throw Error('policy unavailable')}
    await s.loadCreationPolicy();assert.match(s.creationPolicyError.value,/policy unavailable/);assert.equal(s.creationReady.value,false)
    assert.equal(s.newProject.value.title,'用户输入保留');assert.equal(s.newProject.value.aspectRatio,'1:1')
    repository.getProjectCreationCapability=async()=>defaults;await s.loadCreationPolicy();assert.equal(s.creationReady.value,true)
    assert.equal(s.newProject.value.aspectRatio,'1:1');assert.equal(s.aiEpisodeCount.value,7)
    s.creationPlanSource.value='recipe';await settle();assert.equal(recipeReads,1);assert.match(s.creationRecipesError.value,/recipe unavailable/)
    platformApi.listRecipes=async()=>{recipeReads++;return[]};await s.loadCreationRecipes();assert.equal(s.creationRecipesError.value,'');assert.equal(recipeReads,2)
    s.creationPlanSource.value='manual';await settle();assert.equal(s.creationReady.value,true)
    studio.resetSessionState();repository.getStyles=async()=>{throw Error('style unavailable')};await s.loadCreationStyles();assert.match(s.creationStylesError.value,/style unavailable/)
    repository.getStyles=async()=>[];await s.loadCreationStyles();assert.match(s.creationStylesError.value,/尚无/)
    repository.getStyles=async()=>[{id:'style',name:'风格',tags:[],isActive:true}];await s.loadCreationStyles();assert.equal(s.creationStylesError.value,'')
    assert.equal(projectWrites,0)
  }finally{mounted.app.unmount();globalThis.localStorage=previousStorage;Object.assign(repository,old);platformApi.listRecipes=oldRecipes}
}))

test('creation discards a late policy response after leaving the page',()=>modules(async vite=>{
  const previousStorage=globalThis.localStorage;globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}} as any
  const pinia=createPinia(),{useStudioStore}=await vite.ssrLoadModule('/src/stores/studio.ts'),studio=useStudioStore(pinia)
  const {repository}=await vite.ssrLoadModule('/src/services/repository.ts'),old=repository.getProjectCreationCapability,pending=deferred()
  repository.getProjectCreationCapability=async()=>pending.promise;studio.loadStyles=async()=>{}
  const {default:Component}=await vite.ssrLoadModule('/src/views/SeriesStudioView.vue'),mounted=await mount(Component,{},'/projects/new',pinia)
  mounted.app.unmount();pending.resolve({defaultLanguage:'must not apply'});await settle()
  assert.equal(mounted.state.creationDefaults.value,null)
  repository.getProjectCreationCapability=old;globalThis.localStorage=previousStorage
}))

async function projectPageHarness(vite, configure=(_s:any,_r:any,_a:any)=>{}, props:any=reactive({})) {
  const previousStorage=globalThis.localStorage;globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}} as any
  const pinia=createPinia(),{useStudioStore}=await vite.ssrLoadModule('/src/stores/studio.ts'),studio=useStudioStore(pinia)
  const {repository}=await vite.ssrLoadModule('/src/services/repository.ts'),{platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const old={...repository},oldApi={...platformApi}
  studio.initialize=async()=>{throw Error('project must not read task history')};studio.notify=()=>{}
  studio.loadProject=async id=>projectFixture(id);studio.loadStyles=async()=>{}
  platformApi.listProductionRuns=async()=>[];studio.refreshAssets=async()=>[]
  studio.getEpisodeAssemblies=async()=>[];studio.getReleaseBundles=async()=>[]
  repository.getVideoConditioningCapability=async()=>({conditioningModes:[]})
  repository.getBibleImageCandidates=async id=>({projectId:id,candidates:[]})
  configure(studio,repository,platformApi)
  const {default:Component}=await vite.ssrLoadModule('/src/views/SeriesStudioView.vue')
  const mounted=await mount(Component,props,'/projects/project',pinia)
  return {...mounted,props,studio,repository,platformApi,dispose(){mounted.app.unmount();Object.assign(repository,old);Object.assign(platformApi,oldApi);globalThis.localStorage=previousStorage}}
}

test('project first paint reads its style and content, not tasks or hidden tabs; failure preserves edits',()=>modules(async vite=>{
  const core=deferred(),runs=deferred(),style=deferred();let heavy=0
  const m=await projectPageHarness(vite,(studio,repository,api)=>{
    let stylesReady=false
    studio.loadProject=async()=>{assert.equal(stylesReady,true);return core.promise};studio.loadStyles=async()=>{await style.promise;stylesReady=true};api.listProductionRuns=async()=>runs.promise
    repository.getVideoConditioningCapability=async()=>{heavy++;return{conditioningModes:[]}}
    studio.refreshAssets=studio.getEpisodeAssemblies=studio.getReleaseBundles=repository.getBibleImageCandidates=async()=>{heavy++;return[]}
  });const s=m.state
  try{
    assert.equal(s.loading.value,true);assert.equal(s.project.value,null)
    style.resolve();await settle()
    core.resolve(projectFixture());await settle()
    assert.equal(s.loading.value,false);assert.equal(s.project.value.id,'project');assert.equal(heavy,0);assert.equal(s.projectRunsLoading.value,true)
    s.project.value.title='保留编辑';runs.resolve([run]);await settle()
    m.platformApi.listProductionRuns=async()=>{throw Error('runs unavailable')};await s.loadProjectRuns('project')
    assert.match(s.projectRunsError.value,/runs unavailable/);assert.equal(s.loading.value,false);assert.equal(s.project.value.title,'保留编辑')
    m.platformApi.listProductionRuns=async()=>[];await s.loadProjectRuns('project');assert.equal(s.projectRunsError.value,'')
  }finally{m.dispose()}
}))

test('embedded editor lazily reads Bible and preserves draft across tabs and parent refresh',()=>modules(async vite=>{
  const counts={assets:0,candidates:0,capability:0},pending=deferred()
  const props=reactive({inputProject:projectFixture(),inputSection:'overview',inputRunId:'exact-run',inputActive:false,inputDisabled:false})
  const m=await projectPageHarness(vite,(studio,repository)=>{
    studio.loadProject=async()=>{throw Error('embedded editor must reuse parent read')}
    studio.refreshAssets=async()=>{counts.assets++;return[]}
    repository.getBibleImageCandidates=async()=>{counts.candidates++;throw Error('candidates unavailable')}
    repository.getVideoConditioningCapability=async()=>{counts.capability++;return pending.promise}
  },props),s=m.state
  try{
    assert.equal(s.loading.value,false);assert.equal(s.projectProductionTarget.value,'/projects/project/production/exact-run')
    assert.deepEqual(counts,{assets:0,candidates:0,capability:0})
    props.inputSection='bible';props.inputActive=true;await settle()
    assert.deepEqual(counts,{assets:1,candidates:1,capability:1});assert.match(s.bibleImageLoadError.value,/candidates unavailable/)
    s.project.value.continuitySummary='unsaved draft';await settle();assert.equal(s.inputProtected.value,true)
    props.inputSection='episodes';props.inputActive=false;props.inputProject={...projectFixture(),continuitySummary:'server change'};await settle()
    assert.equal(s.project.value.continuitySummary,'unsaved draft')
    props.inputSection='bible';props.inputActive=true;await settle();assert.equal(counts.candidates,1)
    m.repository.getBibleImageCandidates=async()=>({projectId:'project',candidates:[]});await s.loadBibleImageCandidates();assert.equal(s.bibleImageLoadError.value,'')
    m.app.unmount();pending.resolve({conditioningModes:[{key:'old'}]});await settle()
    assert.equal(s.videoConditioningCapability.value,null)
  }finally{Object.assign(props,{inputActive:false});m.dispose()}
}))

test('embedded Bible catalog failure stays unknown and retry clears stale counts without blocking editing',()=>modules(async vite=>{
  const props=reactive({inputProject:projectFixture(),inputSection:'bible',inputActive:true})
  const m=await projectPageHarness(vite,(_studio,repository)=>{
    repository.getVideoConditioningCapability=async()=>{throw Error('video catalog invalid')}
  },props),s=m.state
  try{
    assert.match(s.videoConditioningCapabilityError.value,/video catalog invalid/);assert.equal(s.entityReferenceShotCount.value,null)
    m.repository.getVideoConditioningCapability=async()=>({conditioningModes:[]});await s.loadVideoConditioningCapability('project')
    assert.equal(s.videoConditioningCapabilityError.value,'');assert.equal(s.entityReferenceShotCount.value,0)
    m.repository.getVideoConditioningCapability=async()=>{throw Error('catalog changed')};await s.loadVideoConditioningCapability('project')
    assert.equal(s.videoConditioningCapability.value,null);assert.match(s.videoConditioningCapabilityError.value,/catalog changed/)
  }finally{m.dispose()}
}))

test('embedded writer saves only project inputs, protects failures, and blocks writes during production operations',()=>modules(async vite=>{
  const writes:any[]=[],props=reactive({inputProject:projectFixture(),inputSection:'overview',inputRunId:'run',inputActive:true,inputDisabled:false})
  const m=await projectPageHarness(vite,(studio,_r,api)=>{
    studio.saveProject=async value=>{writes.push(JSON.parse(JSON.stringify(value)));return value}
    api.performManualProductionAction=api.createProductionRun=async()=>{throw Error('editing must not execute or adopt')}
  },props),s=m.state
  try{
    s.project.value.continuitySummary='keep jacket';await settle()
    assert.equal(s.inputDirty.value,true)
    props.inputDisabled=true;assert.equal(await s.save(),null);assert.equal(writes.length,0)
    props.inputDisabled=false;await s.save();await settle()
    assert.equal(writes.length,1);assert.equal(writes[0].continuitySummary,'keep jacket');assert.equal(s.inputDirty.value,false)
    s.project.value.continuitySummary='failed edit';m.studio.saveProject=async()=>{throw Error('write rejected')}
    assert.equal(await s.save(),null);assert.equal(s.inputDirty.value,true);assert.equal(s.project.value.continuitySummary,'failed edit')
    assert.equal(s.projectProductionTarget.value,'/projects/project/production/run')
  }finally{m.dispose()}
}))

test('original-name editing needs no English glossary and opens exact entity scopes without writes',()=>modules(async vite=>{
  const writes:any[]=[],source=projectFixture() as any
  source.bible.characters=[{id:'same-id',name:'阿青'}];source.bible.scenes=[{id:'same-id',name:'雨夜车站'}];source.bible.props=[{id:'book',name:'旧相册'}]
  const props=reactive({inputProject:source,inputSection:'overview',inputRunId:'run',inputActive:true,inputDisabled:false})
  const m=await projectPageHarness(vite,studio=>{studio.saveProject=async value=>{writes.push(JSON.parse(JSON.stringify(value)));return value}},props),s=m.state
  try{
    assert.equal(s.proseTranslationLoaded.value,false);assert.equal('translationGlossaryText' in s,false)
    for(const [section,kind] of [['characters','character'],['scenes','scene'],['props','prop']]){
      const item=s.project.value.bible[section][0];s.openVariantSettings(item,kind)
      assert.deepEqual(s.variantEntity.value,{name:item.name,scope:{project_id:'project',entity_kind:kind,entity_id:item.id}})
    }
    assert.equal(writes.length,0)
    s.variantEntity.value=null;await s.save();assert.equal(writes.length,1);assert.deepEqual(writes[0].translationGlossary,{})
    assert.deepEqual(writes[0].bible,source.bible)
    s.acceptInputProject({...source,translationGlossary:{阿青:'Existing Name'}})
    s.project.value.continuitySummary='edited description'
    s.toggleProseTranslation({target:{open:true}});assert.equal(s.proseTranslationLoaded.value,true)
    const saveProject=m.studio.saveProject;m.studio.saveProject=async()=>{throw Error('save rejected')}
    assert.equal(await s.save(),null);assert.equal(s.inputDirty.value,true)
    assert.deepEqual(s.project.value.translationGlossary,{阿青:'Existing Name'})
    assert.equal(s.project.value.continuitySummary,'edited description')
    m.studio.saveProject=saveProject
    await s.save();assert.equal(s.inputDirty.value,false)
    assert.deepEqual(writes[1].translationGlossary,{阿青:'Existing Name'})
    assert.equal(writes[1].bible.characters[0].name,'阿青')
    s.acceptInputProject({...source,id:'other'})
    assert.equal(s.proseTranslationLoaded.value,false);assert.equal(s.proseTranslationOpen.value,false)
    assert.deepEqual(s.project.value.translationGlossary,{})
  }finally{m.dispose()}
}))

test('original-name directory preserves languages, paginates and never equates same-name entities',()=>modules(async vite=>{
  const {default:C}=await vite.ssrLoadModule('/src/components/ProjectOriginalNames.vue')
  const source=projectFixture() as any
  source.bible.characters=Array.from({length:13},(_,i)=>({id:`c${i}`,name:i===0?'Same Name':`人物${i}`}))
  source.bible.scenes=[{id:'c0',name:'Same Name'}];source.translationGlossary={'人物1':'Translated'}
  const m=await mount(C,reactive({project:source})),s=m.state
  try{
    assert.equal(s.rows.value.length,14);assert.equal(s.visible.value.length,12);assert.equal(s.pages.value,2)
    assert.equal(s.rows.value[1].item.name,'人物1')
    s.page.value=2;assert.equal(s.visible.value.length,2)
    s.search.value='Same Name';await settle();assert.equal(s.page.value,1)
    assert.deepEqual(s.visible.value.map(row=>[row.kind,row.item.id,row.item.name]),[['character','c0','Same Name'],['scene','c0','Same Name']])
    s.search.value='unmatched';await settle();assert.equal(s.visible.value.length,0);assert.equal(s.pages.value,1)
  }finally{m.app.unmount()}
}))

test('late project detail is ignored after project switch, session invalidation or unmount',()=>modules(async vite=>{
  const stale=deferred(),m=await projectPageHarness(vite,studio=>{studio.loadProject=async id=>id==='project'?stale.promise:projectFixture(id)})
  try{
    await m.router.push('/projects/other');await settle();assert.equal(m.state.project.value.id,'other')
    stale.resolve(projectFixture());await settle();assert.equal(m.state.project.value.id,'other')
    const pending=deferred();m.studio.loadProject=async()=>pending.promise
    const read=m.state.loadProject();m.studio.resetSessionState();pending.resolve(projectFixture('other'));await read
    assert.equal(m.state.project.value,null)
    const unmounted=deferred();m.studio.loadProject=async()=>unmounted.promise
    const after=m.state.loadProject();m.app.unmount();unmounted.resolve(projectFixture('other'));await after
    assert.equal(m.state.project.value,null)
  }finally{m.dispose()}
}))

test('failed project read is explicit and retry uses fresh content without loading tasks',()=>modules(async vite=>{
  const m=await projectPageHarness(vite,studio=>{studio.loadProject=async()=>{throw Error('project unavailable')}})
  try{
    assert.match(m.state.projectLoadError.value,/project unavailable/);assert.equal(m.state.loading.value,false);assert.equal(m.state.project.value,null)
    m.studio.loadProject=async(id,force)=>{assert.equal(force,true);return projectFixture(id)}
    await m.state.refreshProjectPage();assert.equal(m.state.project.value.id,'project');assert.equal(m.state.projectLoadError.value,'')
  }finally{m.dispose()}
}))

test('project list empty and retry states do not depend on workflow initialization',()=>modules(async vite=>{
  const pinia=createPinia(),{useStudioStore}=await vite.ssrLoadModule('/src/stores/studio.ts'),studio=useStudioStore(pinia)
  const pending=deferred();studio.initializeNavigation=async()=>pending.promise
  studio.initialize=async()=>{throw Error('must not load tasks')};studio.error='unrelated task error'
  const {default:C}=await vite.ssrLoadModule('/src/views/SeriesProjectsView.vue'),m=await mount(C,{},'/projects',pinia)
  try{
    assert.equal(m.state.loadingWithoutData.value,true)
    pending.resolve();await settle();assert.equal(m.state.loadingWithoutData.value,false);assert.equal(m.state.visibleLoadError.value,'');assert.equal(studio.ready,false)
    studio.refreshProjects=async()=>{throw Error('list unavailable')};await m.state.retry()
    assert.match(m.state.visibleLoadError.value,/list unavailable/);assert.equal(m.state.loadingWithoutData.value,false)
    studio.refreshProjects=async()=>{studio.projects=[projectFixture()];return studio.projects};await m.state.retry()
    assert.equal(m.state.visibleLoadError.value,'');assert.equal(studio.projects.length,1)
  }finally{m.app.unmount()}
}))

test('source editing cannot silently overwrite the adopted script and history is not binding readiness',()=>{
  const creation=readFileSync(new URL('../src/views/SeriesStudioView.vue',import.meta.url),'utf8')
  for(const stale of ['正在装载制作圣经','newProject.resolution',"styleId: 'wuxia'",'项目分辨率档位'])assert.ok(!creation.includes(stale))
  const series=readFileSync(new URL('../src/views/SeriesStudioView.vue',import.meta.url),'utf8')
  const production=readFileSync(new URL('../src/views/ProductionRunView.vue',import.meta.url),'utf8')
  assert.doesNotMatch(series,/v-model="project\.masterScript"|project_only|creationAction/)
  assert.doesNotMatch(series,/新分镜默认视频输入模式|newProject\.defaultMode|projectConditioningMode/)
  assert.match(series,/ProjectVersionWorkspace/);assert.match(series,/待核对候选历史/)
  assert.doesNotMatch(production,/importProjectMasterScript|projectMasterScript/)
  assert.ok(production.includes("view:'new'"));assert.ok(production.includes('<ProjectVersionWorkspace'))
})
