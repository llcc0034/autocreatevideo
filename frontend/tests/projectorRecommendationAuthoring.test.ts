import assert from 'node:assert/strict'
import test from 'node:test'
import {fileURLToPath} from 'node:url'
import {createServer} from 'vite'
import {createRenderer,h,nextTick,ssrContextKey} from 'vue'
import {createPinia} from 'pinia'
import {createRouter,createMemoryHistory} from 'vue-router'
import {copy,scan,installReads} from './fixtures/workflow-import-data.ts'
import {recommendation,recommendationSource,complexDefinition} from './fixtures/projector-recommendation-data.ts'
import {rawOptions} from './fixtures/projector-authoring-data.ts'
const settle=async()=>{for(let i=0;i<30;i++){await Promise.resolve();await nextTick()}}
const deferred=()=>{let resolve!:(x:any)=>void;const promise=new Promise<any>(r=>resolve=r);return{promise,resolve}}

test('Projector recommendation authors preserve exact graph evidence and configured rules',async t=>{
  const oldStorage=globalThis.localStorage,oldWindow=globalThis.window
  const values=new Map<string,string>()
  globalThis.localStorage={getItem:(k:string)=>values.get(k)||null,setItem:(k:string,v:string)=>values.set(k,v),removeItem:(k:string)=>values.delete(k)} as any
  globalThis.window={localStorage:globalThis.localStorage,setInterval:()=>1,clearInterval(){},setTimeout:()=>1,clearTimeout(){}} as any
  const vite=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  const {platformApi:api}=await vite.ssrLoadModule('/src/services/platformApi.ts'),originals={...api}
  const {adaptWorkflowProjectorOptions}=await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const {workflowProjectorDefinitionErrors}=await vite.ssrLoadModule('/src/services/workflowProjectorDefinition.ts')
  const {useAuthStore}=await vite.ssrLoadModule('/src/stores/auth.ts')
  const {default:Component}=await vite.ssrLoadModule('/src/views/SettingsView.vue')
  const options=adaptWorkflowProjectorOptions(rawOptions)
  async function mount(){
    values.clear();installReads(api);api.getWorkflowProjectorOptions=async()=>copy(options)
    api.scanComfySavedWorkflows=async()=>scan([recommendationSource]);api.recommendWorkflowProjector=async()=>recommendation()
    const router=createRouter({history:createMemoryHistory(),routes:[{path:'/settings',component:{render:()=>null}}]})
    await router.push('/settings?section=workflows');await router.isReady()
    const pinia=createPinia(),auth=useAuthStore(pinia);auth.user={id:'fixture-user',isStaff:true,isSuperuser:true}
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    let s:any;const app=renderer.createApp({setup(){s=Component.setup({},{expose(){}});return()=>h('div')}})
    app.use(pinia);app.use(router);app.provide(ssrContextKey,{modules:new Set()});app.mount({});await settle();await s.scanSavedWorkflows()
    const item=s.savedWorkflowItems.value[0],draft=s.savedWorkflowDrafts[s.savedWorkflowKey(item)]
    return{s,item,draft,app,auth}
  }
  try{
    await t.test('recommendation to complex draft saves only declared configuration and retains source',async()=>{
      const m=await mount(),sent:any[]=[];let request:any
      try{api.recommendWorkflowProjector=async(payload:any)=>{request=payload;return recommendation()}
        await m.s.recommendWorkflowProjectorFromCandidate(m.item)
        assert.deepEqual(request,{candidate_source_id:recommendationSource.candidateSourceId,expected_source_scope_schema_version:1,expected_source_scope_sha256:recommendationSource.sourceScopeSha256,expected_source_sha256:recommendationSource.sourceSha256,primary_output_node_id:'81',output_kind:'image'})
        assert.equal(m.s.workflowProjectorCreateInitialized.value,true)
        const edited=m.s.workflowProjectorCreateDefinition.value;edited.parameter_policies[0].constraints.max=32;edited.parameter_policies[1].value='detailed'
        const errors=workflowProjectorDefinitionErrors(edited,options,'generic.fixed',1);assert.deepEqual(errors,[])
        Object.assign(m.s.workflowProjectorCreateFamily,{key:'fixture.new',name:'新图片方案',description:'独立夹具'})
        m.s.workflowProjectorCreateDefinitionValidity.value={valid:true,errors:[]}
        api.createWorkflowProjector=async(p:any)=>({id:'new-family',...p,isActive:true})
        api.createWorkflowProjectorVersion=async(p:any)=>{sent.push(copy(p));return{id:'new-v1',projector:'new-family',projectorKey:'fixture.new',version:1,status:'draft',engineKey:p.engine_key,engineVersion:p.engine_version,providerDriverType:p.provider_driver_type,operation:p.operation,outputKind:p.output_kind,definition:copy(p.definition)}}
        await m.s.submitWorkflowProjectorCreate();assert.equal(sent.length,1)
        assert.equal(sent[0].definition.parameter_policies[0].constraints.max,32);assert.equal(sent[0].definition.parameter_policies[1].value,'detailed')
        assert.deepEqual(sent[0].definition.required_edges,complexDefinition.required_edges)
        assert.deepEqual(sent[0].definition.detector.optional_node_counts,complexDefinition.detector.optional_node_counts)
        assert.equal(complexDefinition.parameter_policies[0].constraints?.max,40);assert.equal(complexDefinition.parameter_policies[1].value,'balanced')
        assert.equal('assets' in sent[0],false);assert.equal('values' in sent[0],false)
      }finally{m.app.unmount()}
    })
    await t.test('scope version and selected output changes invalidate a recommendation before save',async()=>{
      const m=await mount();try{
        api.recommendWorkflowProjector=async()=>{const result=recommendation();result.sourceEvidence.sourceScopeSchemaVersion=2;return result}
        await m.s.recommendWorkflowProjectorFromCandidate(m.item);assert.equal(m.s.workflowProjectorCreateInitialized.value,false);assert.match(m.draft.previewError,/不一致/)
        api.recommendWorkflowProjector=async()=>recommendation();await m.s.recommendWorkflowProjectorFromCandidate(m.item)
        assert.equal(m.s.workflowProjectorCreateInitialized.value,true)
        m.item.sourceScopeSchemaVersion=2;assert.equal(m.s.workflowProjectorCreateInitialized.value,false)
        m.item.sourceScopeSchemaVersion=1;m.draft.outputKind='video';assert.equal(m.s.workflowProjectorCreateInitialized.value,false)
      }finally{m.app.unmount()}
    })
    await t.test('late same-path account response does not release the next recommendation busy state',async()=>{
      const m=await mount(),old=deferred(),next=deferred();let calls=0
      try{api.recommendWorkflowProjector=()=>++calls===1?old.promise:next.promise
        const first=m.s.recommendWorkflowProjectorFromCandidate(m.item);await settle()
        m.auth.user={...m.auth.user,id:'other'};await settle();await m.s.scanSavedWorkflows()
        const current=m.s.savedWorkflowItems.value[0],second=m.s.recommendWorkflowProjectorFromCandidate(current);await settle()
        const busy=m.s.savedWorkflowBusy.value;assert.ok(busy)
        old.resolve(recommendation());await first;assert.equal(m.s.savedWorkflowBusy.value,busy);assert.equal(m.s.workflowProjectorRecommendation.value,null)
        next.resolve(recommendation());await second;assert.equal(m.s.workflowProjectorCreateInitialized.value,true)
      }finally{m.app.unmount()}
    })
    await t.test('changed selection and explicit rejection preserve edited definitions without applying response',async()=>{
      const m=await mount(),pending=deferred();try{
        api.recommendWorkflowProjector=()=>pending.promise
        const operation=m.s.recommendWorkflowProjectorFromCandidate(m.item);await settle();m.draft.outputKind='video'
        pending.resolve(recommendation());await operation;assert.equal(m.s.workflowProjectorCreateInitialized.value,false)
        m.draft.outputKind='image';api.recommendWorkflowProjector=async()=>recommendation();await m.s.recommendWorkflowProjectorFromCandidate(m.item)
        const edited=m.s.workflowProjectorCreateDefinition.value;edited.parameter_policies[0].constraints.max=32
        m.s.workflowProjectorCreateFamilyId.value='existing';Object.assign(m.s.workflowProjectorCreateFamily,{key:'existing',name:'已存在方案'})
        m.s.workflowProjectorCreateDefinitionValidity.value={valid:true,errors:[]}
        api.createWorkflowProjectorVersion=async()=>{throw{response:{status:400,data:{message:'范围不符',fields:{'definition.parameter_policies':['校验失败']}}}}}
        await m.s.submitWorkflowProjectorCreate();assert.equal(edited.parameter_policies[0].constraints.max,32);assert.equal(m.s.workflowProjectorCreateOpen.value,true);assert.match(m.s.workflowProjectorError.value,/明确拒绝/)
      }finally{m.app.unmount()}
    })
    for(const closeEditor of [false,true])await t.test(`switching editor${closeEditor?' and closing it':''} discards late recommendation without losing edits`,async()=>{
      const m=await mount(),pending=deferred();try{
        api.recommendWorkflowProjector=()=>pending.promise
        const operation=m.s.recommendWorkflowProjectorFromCandidate(m.item);await settle()
        m.s.openWorkflowProjectorEditor({id:'other-v1',projector:'other',projectorKey:'other',version:1,status:'draft',engineKey:'generic.fixed',engineVersion:1,providerDriverType:'comfyui',operation:'workflow.import',outputKind:'image',definition:copy(complexDefinition)})
        if(closeEditor)m.s.closeWorkflowProjectorEditor()
        else m.s.workflowProjectorDefinition.value.parameter_policies[1].value='keep this edit'
        pending.resolve(recommendation());await operation
        assert.equal(m.s.workflowProjectorCreateOpen.value,false);assert.equal(m.s.workflowProjectorEditorOpen.value,!closeEditor)
        if(!closeEditor)assert.equal(m.s.workflowProjectorDefinition.value.parameter_policies[1].value,'keep this edit')
        assert.match(m.draft.previewError,/未应用/)
      }finally{m.app.unmount()}
    })
  }finally{Object.assign(api,originals);globalThis.localStorage=oldStorage;globalThis.window=oldWindow;await vite.close()}
})
