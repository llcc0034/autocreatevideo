import assert from 'node:assert/strict'
import test from 'node:test'
import {fileURLToPath} from 'node:url'
import {readFile} from 'node:fs/promises'
import {createServer} from 'vite'
import {createRenderer,createSSRApp,h,nextTick,ssrContextKey} from 'vue'
import {renderToString} from 'vue/server-renderer'
import {createPinia} from 'pinia'
import {createRouter,createMemoryHistory} from 'vue-router'
import {copy,profile,preview,source,scan,receipt,installReads,recoveredProof,recoveredVersion,previewReceipt} from './fixtures/workflow-import-data.ts'
const settle=async()=>{for(let i=0;i<30;i++){await Promise.resolve();await nextTick()}}
const deferred=()=>{let resolve!:(x:any)=>void;const promise=new Promise<any>(r=>resolve=r);return{promise,resolve}}

test('workflow import authors use exact source contracts and truthful progress',async t=>{
  const originalStorage=globalThis.localStorage,originalWindow=globalThis.window
  const originalNavigator=Object.getOwnPropertyDescriptor(globalThis,'navigator')
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{locks:{request:async(_key:string,work:any)=>work()}}})
  const values=new Map<string,string>()
  globalThis.localStorage={getItem:(k:string)=>values.get(k)||null,setItem:(k:string,v:string)=>values.set(k,v),removeItem:(k:string)=>values.delete(k)} as any
  globalThis.window={localStorage:globalThis.localStorage,setInterval:()=>1,clearInterval(){},setTimeout:()=>1,clearTimeout(){}} as any
  const vite=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  const {platformApi:api}=await vite.ssrLoadModule('/src/services/platformApi.ts'),originals={...api}
  const {useAuthStore}=await vite.ssrLoadModule('/src/stores/auth.ts')
  const {default:Component}=await vite.ssrLoadModule('/src/views/SettingsView.vue')
  async function mount(rows=[source()],retain=false){
    if(!retain)values.clear();installReads(api);api.scanComfySavedWorkflows=async()=>scan(rows);api.previewComfySavedWorkflow=async(id:any,input:any)=>previewReceipt(id,input)
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
    await t.test('contract generation unknown survives remount and recovers complete original contract without replay',async()=>{
      let m=await mount(),writes=0,proof:any
      try{api.previewComfySavedWorkflow=async(id:any,input:any)=>{writes++;proof=previewReceipt(id,input);throw Error('response lost')}
        await m.s.previewSavedWorkflow(m.item);await m.s.previewSavedWorkflow(m.item);assert.equal(writes,1)
        assert.equal(m.s.savedWorkflowPending.value[0].contractRequest.candidateSourceId,m.item.candidateSourceId)
        assert.match(m.draft.previewError,/暂停重复生成/);assert.equal(m.s.savedWorkflowPromotable(m.item),false)
        assert.equal(m.s.savedWorkflowStepLabel(m.item,m.draft,2),'原请求结果待核对');assert.equal(m.s.savedWorkflowStepState(m.item,m.draft,3),'blocked')
        assert.equal(m.s.blockedSavedWorkflowItems.value.length,1);assert.equal(m.s.savedWorkflowContractState.value,'blocked')
        m.app.unmount();m=await mount([source()],true);api.previewComfySavedWorkflow=async()=>{writes++;return copy(preview)}
        api.getComfyWorkflowPreviewReceipt=async()=>proof
        m.draft.newTemplateName='保持未提交名称';await m.s.savedWorkflowRecovery.readback()
        assert.equal(writes,1);assert.equal(m.s.savedWorkflowPending.value.length,0)
        assert.deepEqual(m.draft.preview.suggestedInputContract,preview.suggestedInputContract)
        assert.equal(m.draft.newTemplateName,'保持未提交名称');assert.equal(m.s.savedWorkflowPromotable(m.item),true)
        assert.match(m.s.savedWorkflowRecoveryResults.value[0].detail,/已按原请求找回完整合同/)
      }finally{m.app.unmount()}
    })
    await t.test('contract explicit rejection unlocks correction but mismatched success stays unknown',async()=>{
      const m=await mount();try{
        api.previewComfySavedWorkflow=async()=>{throw{response:{status:400,data:{message:'output invalid'}}}}
        await m.s.previewSavedWorkflow(m.item);assert.equal(m.s.savedWorkflowPending.value.length,0);assert.equal(m.draft.primaryOutputNodeId,'81')
        api.previewComfySavedWorkflow=async()=>({...copy(preview),sourceSha256:'8'.repeat(64)})
        await m.s.previewSavedWorkflow(m.item);assert.equal(m.s.savedWorkflowPending.value.length,1);assert.equal(m.draft.preview,null)
      }finally{m.app.unmount()}
    })
    await t.test('contract receipt reader queries exact original key and deployment without list or writes',async()=>{
      const {api:http}=await vite.ssrLoadModule('/src/services/api.ts'),oldGet=http.get,calls:any[]=[]
      try{http.get=async(url:any,options:any)=>{calls.push({url,options});return{data:{admission_tier:'candidate',actionability:'candidate_only',contract_draft_id:'draft',contract_draft_revision:1}}}
        const row=await originals.getComfyWorkflowPreviewReceipt('deployment','original-key');assert.equal(row.contractDraftId,'draft');assert.equal(calls.length,1)
        assert.equal(calls[0].url,'/model-deployments/deployment/saved-workflows/preview-receipt/');assert.deepEqual(calls[0].options.params,{request_key:'original-key'})
      }finally{http.get=oldGet}
    })
    await t.test('recovery preserves changed selection until original model and output are explicitly restored',async()=>{
      const m=await mount();let proof:any
      try{api.previewComfySavedWorkflow=async(id:any,input:any)=>{proof=previewReceipt(id,input);throw Error('lost')}
        await m.s.previewSavedWorkflow(m.item);api.getComfyWorkflowPreviewReceipt=async()=>proof
        m.draft.primaryOutputNodeId='changed';await m.s.savedWorkflowRecovery.readback()
        assert.equal(m.draft.primaryOutputNodeId,'changed');assert.equal(m.draft.preview,null);assert.equal(m.s.savedWorkflowPending.value.length,1)
        assert.ok(m.s.savedWorkflowRecoveryResults.value[0].preview)
        m.draft.primaryOutputNodeId='81';await m.s.savedWorkflowRecovery.readback()
        assert.equal(m.s.savedWorkflowPending.value.length,0);assert.equal(m.draft.preview.contractDraftId,proof.contractDraftId)
      }finally{m.app.unmount()}
    })
    await t.test('late contract receipt cannot apply to changed selection or release a newer account request',async()=>{
      const m=await mount(),old=deferred(),next=deferred();let calls=0
      try{api.previewComfySavedWorkflow=()=>{calls++;return calls===1?old.promise:next.promise}
        const first=m.s.previewSavedWorkflow(m.item);await settle();m.auth.user={...m.auth.user,id:'other'};await settle();await m.s.scanSavedWorkflows()
        const item=m.s.savedWorkflowItems.value[0],draft=m.s.savedWorkflowDrafts[m.s.savedWorkflowKey(item)]
        const second=m.s.previewSavedWorkflow(item);await settle();const busy=m.s.savedWorkflowBusy.value
        old.resolve(copy(preview));await first;assert.equal(m.s.savedWorkflowBusy.value,busy);assert.equal(draft.preview,null)
        draft.primaryOutputNodeId='changed';next.resolve(copy(preview));await second;assert.equal(draft.preview,null);assert.equal(m.s.savedWorkflowBusy.value,'')
      }finally{m.app.unmount()}
    })
    await t.test('new version follows server target and carries exact contract, never task values',async()=>{
      const m=await mount([source({suggestedTemplate:{useExisting:false,templateId:'',name:'新方案',slug:'fixture-new',outputKind:'video'}})])
      try{await m.s.previewSavedWorkflow(m.item);assert.equal(m.s.savedWorkflowPromotable(m.item),true);const payload=m.s.savedWorkflowBatchPayload(m.item)
        assert.equal(payload.template,null);assert.equal(payload.newTemplate.name,'新方案');assert.equal(payload.contractDraft,preview.contractDraftId)
        assert.equal(payload.expectedStructuralContractSha256,preview.structuralContractSha256);assert.equal(payload.modelProfileVersion,profile.id)
        assert.equal('values' in payload,false);assert.equal('assets' in payload,false);assert.deepEqual(m.s.savedWorkflowAuthorSections(m.item).map((r:any)=>r.key),['selection','contract','destination'])
      }finally{m.app.unmount()}
    })
    await t.test('candidate-only and changed selections never claim ready even with no blockers',async()=>{
      const m=await mount();try{await m.s.previewSavedWorkflow(m.item)
        m.draft.preview.actionability='candidate_only';assert.equal(m.s.savedWorkflowStepState(m.item,m.draft,2),'blocked');assert.match(m.s.savedWorkflowStepLabel(m.item,m.draft,2),/仅候选/);assert.equal(m.s.savedWorkflowContractState.value,'blocked')
        m.draft.preview.actionability='ready_for_executable_import';m.draft.reviewRequiresConfirmation=true;assert.equal(m.s.savedWorkflowStepState(m.item,m.draft,2),'current');assert.match(m.s.savedWorkflowStepLabel(m.item,m.draft,2),/重新生成/);assert.equal(m.s.savedWorkflowContractState.value,'current')
      }finally{m.app.unmount()}
    })
    await t.test('successor keeps exact predecessor and failed preview preserves selections',async()=>{
      const m=await mount([source({imported:true,workflowVersionId:'original-v1',sourceId:'old-source'})]);try{
        api.previewComfySavedWorkflow=async()=>{throw{response:{status:400,data:{message:'精确字段错误'}}}}
        await m.s.reprojectSavedWorkflowAsSuccessor(m.item)
        assert.equal(m.draft.successorOf,'original-v1');assert.equal(m.draft.projectorSuccessorRequested,true)
        assert.notEqual(m.s.savedWorkflowStepState(m.item,m.draft,2),'complete');assert.equal(m.draft.modelProfileVersionId,profile.id);assert.ok(m.draft.previewError)
        api.previewComfySavedWorkflow=async(id:any,input:any)=>previewReceipt(id,input);await m.s.previewSavedWorkflow(m.item)
        assert.equal(m.s.savedWorkflowBatchPayload(m.item).successorOf,'original-v1');assert.equal(m.item.workflowVersionId,'original-v1')
      }finally{m.app.unmount()}
    })
    await t.test('pending batch shows waiting and prevents double dispatch; list read failure retains receipt',async()=>{
      const m=await mount(),pending=deferred();let writes=0;try{
        await m.s.previewSavedWorkflow(m.item);m.draft.selected=true;api.batchImportComfySavedWorkflows=()=>{writes++;return pending.promise}
        const saving=m.s.batchImportSavedWorkflows();await m.s.batchImportSavedWorkflows();await settle();assert.equal(writes,1);assert.match(m.s.savedWorkflowImportProgress.value,/第 1\/1 批.*等待保存回执/)
        api.listWorkflowVersions=async()=>{throw Error('list failed')};pending.resolve(receipt(m.item.sourceDeploymentId,[m.s.savedWorkflowBatchPayload(m.item)]));await saving
        assert.equal(m.s.savedWorkflowResult.value.createdCount,1);assert.match(m.s.savedWorkflowError.value,/回执仍保留/);assert.equal(m.item.workflowVersionId,'fixture-saved-version');assert.match(m.s.savedWorkflowImportProgress.value,/未发布或运行/)
      }finally{m.app.unmount()}
    })
    await t.test('partial batches distinguish submitted unknown from unsubmitted; no automatic retry',async()=>{
      const rows=[source(),source({sourceKey:'second',sourcePath:'workflows/second.json',sourceSha256:'7'.repeat(64)}),source({sourceKey:'third',sourcePath:'workflows/third.json',sourceSha256:'8'.repeat(64)})]
      const m=await mount(rows);let writes=0;try{
        for(const item of m.s.savedWorkflowItems.value){await m.s.previewSavedWorkflow(item);m.s.savedWorkflowDrafts[m.s.savedWorkflowKey(item)].selected=true}
        api.batchImportComfySavedWorkflows=async(id:any,items:any)=>{writes++;if(writes===2)throw Error('unknown');return receipt(id,items)}
        await m.s.batchImportSavedWorkflows();assert.equal(writes,2);assert.equal(m.s.savedWorkflowResult.value.createdCount,1)
        assert.match(m.s.savedWorkflowImportProgress.value,/已确认 1\/3 批；第 2 批结果待核对；其后的 1 批未提交/)
        assert.equal(m.s.savedWorkflowItems.value[1].imported,false)
      }finally{m.app.unmount()}
    })
    await t.test('explicit batch rejection does not claim unknown or clear draft',async()=>{
      const m=await mount();try{await m.s.previewSavedWorkflow(m.item);m.draft.selected=true;api.batchImportComfySavedWorkflows=async()=>{throw{response:{status:400,data:{message:'合同有误'}}}}
        await m.s.batchImportSavedWorkflows();assert.match(m.s.savedWorkflowImportProgress.value,/明确拒绝/);assert.match(m.s.savedWorkflowError.value,/已填选择与合同保留/);assert.equal(m.draft.preview.contractDraftId,preview.contractDraftId)
      }finally{m.app.unmount()}
    })
    await t.test('unknown successor survives remount; exact readback restores actual result with no second write',async()=>{
      const rows=[source({imported:true,workflowVersionId:'original-v1',sourceId:'old-source'})]
      let m=await mount(rows),writes=0;let payload:any
      try{await m.s.reprojectSavedWorkflowAsSuccessor(m.item);m.draft.selected=true;payload=m.s.savedWorkflowBatchPayload(m.item)
        api.batchImportComfySavedWorkflows=async()=>{writes++;throw Error('response lost')};await m.s.batchImportSavedWorkflows()
        assert.equal(m.s.savedWorkflowPending.value.length,1);await m.s.batchImportSavedWorkflows();assert.equal(writes,1)
        m.app.unmount();m=await mount(rows,true);assert.equal(m.s.savedWorkflowPending.value[0].input.successorOf,'original-v1');assert.equal(m.s.savedWorkflowPromotable(m.item),false)
        api.listComfyWorkflowSources=async()=>[await recoveredProof(m.item.sourceDeploymentId,payload)];api.getWorkflowVersion=async()=>recoveredVersion(payload)
        await m.s.savedWorkflowRecovery.readback();assert.equal(m.s.savedWorkflowPending.value.length,0);assert.equal(m.item.workflowVersionId,'fixture-saved-version')
        assert.equal(m.draft.successorOf,'');assert.equal(m.draft.selected,false);assert.equal(writes,1)
      }finally{m.app.unmount()}
    })
    await t.test('unknown candidate admission recovers candidate only, never claims a workflow was imported',async()=>{
      const m=await mount([source({candidateManaged:false,candidateSourceId:''})]);let writes=0,payload:any
      try{api.batchImportComfySavedWorkflows=async(_id:any,items:any)=>{writes++;payload=items[0];throw Error('response lost')}
        await m.s.manageSavedWorkflowCandidate(m.item);await m.s.manageSavedWorkflowCandidate(m.item);assert.equal(writes,1);assert.equal(m.s.savedWorkflowPending.value.length,1)
        api.listComfyWorkflowSources=async()=>[await recoveredProof(m.item.sourceDeploymentId,payload)]
        await m.s.savedWorkflowRecovery.readback();assert.equal(m.s.savedWorkflowPending.value.length,0);assert.equal(m.item.candidateManaged,true);assert.equal(m.item.imported,false);assert.equal(m.item.workflowVersionId,'')
      }finally{m.app.unmount()}
    })
    await t.test('late candidate receipt cannot clear a new account admission busy state',async()=>{
      const m=await mount([source({candidateManaged:false,candidateSourceId:''})]),old=deferred(),next=deferred();let calls=0,oldInput:any
      try{api.batchImportComfySavedWorkflows=async(_id:any,items:any)=>{calls++;if(calls===1){oldInput=items;return old.promise}return next.promise}
        const oldRequest=m.s.manageSavedWorkflowCandidate(m.item);await settle();m.auth.user={...m.auth.user,id:'other'};await settle();await m.s.scanSavedWorkflows()
        const newItem=m.s.savedWorkflowItems.value[0];const newRequest=m.s.manageSavedWorkflowCandidate(newItem);await settle();const newBusy=m.s.savedWorkflowBusy.value
        assert.ok(newBusy);assert.equal(calls,2);old.resolve(receipt(m.item.sourceDeploymentId,oldInput));await oldRequest;assert.equal(m.s.savedWorkflowBusy.value,newBusy)
        next.resolve(receipt(newItem.sourceDeploymentId,oldInput));await newRequest;assert.equal(m.s.savedWorkflowBusy.value,'')
      }finally{m.app.unmount()}
    })
    await t.test('recovery readers use complete paginated database sources without filtering transport deployment',async()=>{
      const {api:http}=await vite.ssrLoadModule('/src/services/api.ts'),oldGet=http.get,calls:any[]=[]
      const payload={sourcePath:'workflows/a.json',expectedSha256:'1'.repeat(64),expectedSourceScopeSha256:'6'.repeat(64),expectedSourceScopeSchemaVersion:1,importKind:'candidate'}
      try{http.get=async(url:any,options:any)=>{calls.push({url,options});return{data:calls.length===1?{results:[],next:'/comfy-workflow-sources/?page=2'}:{results:[{id:'record',source_deployment:'old-transport',source_scope_schema_version:1,source_scope_sha256:payload.expectedSourceScopeSha256,source_path:payload.sourcePath,source_sha256:payload.expectedSha256,import_kind:'candidate'}],next:null}}}
        const rows=await originals.listComfyWorkflowSources('new-transport',payload);assert.equal(rows[0].deploymentId,'old-transport');assert.equal(calls.length,2)
        assert.equal(calls[0].url,'/comfy-workflow-sources/');assert.equal(calls[0].options.params.source_deployment,undefined);assert.equal(calls[0].options.params.source_scope_sha256,payload.expectedSourceScopeSha256)
      }finally{http.get=oldGet}
    })
    await t.test('account switch or unmount stops next batches and ignores late receipt',async()=>{
      for(const change of ['account','unmount']){
        const m=await mount([source(),source({sourceKey:'second',sourcePath:'workflows/second.json',sourceSha256:'7'.repeat(64)})]),pending=deferred();let writes=0
        try{for(const item of m.s.savedWorkflowItems.value){await m.s.previewSavedWorkflow(item);m.s.savedWorkflowDrafts[m.s.savedWorkflowKey(item)].selected=true}
          const payload=m.s.savedWorkflowBatchPayload(m.item);api.batchImportComfySavedWorkflows=()=>{writes++;return pending.promise};const saving=m.s.batchImportSavedWorkflows();await settle()
          if(change==='account')m.auth.user={...m.auth.user,id:'other'};else m.app.unmount();await settle()
          pending.resolve(receipt(m.item.sourceDeploymentId,[payload]));await saving;assert.equal(writes,1);assert.equal(m.s.savedWorkflowResult.value,null)
        }finally{if(change!=='unmount')m.app.unmount()}
      }
    })
    await t.test('author template reuses shared shell, exposes outputs before internals and links actual receipts',async()=>{
      const code=await readFile(new URL('../src/views/SettingsView.vue',import.meta.url),'utf8')
      assert.match(code,/:sections="savedWorkflowAuthorSections\(item\)"/);assert.ok(code.indexOf('<WorkflowPublicContract v-if="savedWorkflowDrafts')<code.indexOf('<details v-if="savedWorkflowDrafts'))
      assert.match(code,/查看已保存版本/);assert.match(code,/savedWorkflowImportProgress.*role="status"/)
      assert.match(code,/v-if="savedWorkflowImportUnresolved\(item\)"[^>]*>原请求结果尚未确认/)
      assert.match(code,/v-else-if="!savedWorkflowDrafts\[savedWorkflowKey\(item\)\]\.preview"/)
    })
    await t.test('recovery panel distinguishes unresolved, reading and complete states without a dead retry button',async()=>{
      const {default:Panel}=await vite.ssrLoadModule('/src/components/SavedWorkflowWriteRecovery.vue')
      const pending={id:'ticket',startedAt:'2026-10-09T00:00:00Z',deploymentId:'deployment',input:{sourcePath:'workflows/sample.json'}}
      const render=async(props:any)=>{const app=createSSRApp(Panel,{pending:[],results:[],error:'',busy:false,disabled:false,...props});app.component('RouterLink',{props:['to'],setup:(p:any,{slots}:any)=>()=>h('a',{href:p.to},slots.default?.())});return renderToString(app)}
      const unknown=await render({pending:[pending]});assert.match(unknown,/保存结果待核对/);assert.match(unknown,/只读核对原请求/)
      const reading=await render({pending:[pending],busy:true});assert.match(reading,/aria-busy="true"/);assert.match(reading,/disabled/)
      const done=await render({results:[{id:'ticket',path:'workflows/sample.json',detail:'已核对',versionId:'exact-version'}]})
      assert.match(done,/原请求核对完成/);assert.match(done,/\/settings\/workflows\/exact-version/);assert.doesNotMatch(done,/<button/)
      assert.match(done,/tabindex="-1"/)
      const panelSource=await readFile(new URL('../src/components/SavedWorkflowWriteRecovery.vue',import.meta.url),'utf8')
      assert.match(panelSource,/panel\.value\?\.focus\(\)/)
      assert.match(panelSource,/summary \{ min-height:var\(--config-control-height\)/)
      assert.match(panelSource,/--config-control-height:44px/)
    })
  }finally{Object.assign(api,originals);await vite.close();globalThis.localStorage=originalStorage;globalThis.window=originalWindow;if(originalNavigator)Object.defineProperty(globalThis,'navigator',originalNavigator)}
})
