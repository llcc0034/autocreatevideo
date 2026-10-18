import assert from 'node:assert/strict'
import test from 'node:test'
import {createPinia,setActivePinia} from 'pinia'
Object.defineProperty(globalThis,'localStorage',{configurable:true,value:{getItem:()=>null,setItem(){},removeItem(){}}})
import {createServer} from 'vite'
import {ref} from 'vue'
import {matchedRouteKey} from 'vue-router'
import {visualRulesOutputRaw,outputFieldSourceOptions,outputResponseKeyPolicy} from './visualRulesOutputFixture.ts'
async function setup(context:any){setActivePinia(createPinia());const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}});context.after(()=>vite.close());return vite}
const row={id:'f215ae78-cb2f-4b61-a5bf-fbc3064dfc29',output_key:'f215ae78-cb2f-4b61-a5bf-fbc3064dfc28',version:1,revision:1,
  name:'Visual output',description:'Rules',parser_key:'visual_rules.v2',data_structure_version:'f215ae78-cb2f-4b61-a5bf-fbc3064dfc27',status:'draft',contract_sha256:'a'.repeat(64),quality_advisories:{},
  parser_contract:{key:'visual_rules.v2',operation:'visual_rules.generate',response_schema_version:2,fields:visualRulesOutputRaw.outputs,response_example:visualRulesOutputRaw.response_example}}

test('output API keeps revision checks, explicit lifecycle, and distinguishes invalid metadata',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/businessOutputs.ts'),{api}=await vite.ssrLoadModule('/src/services/api.ts')
  assert.equal(m.adaptBusinessOutput(row,true).id,row.id)
  for(const change of [{revision:true},{version:0},{status:'success'},{quality_advisories:[]},{name:''},{parser_contract:undefined},{parser_contract:{key:'wrong'}},{data_structure_version:null},{data_structure_version:undefined}])assert.throws(()=>m.adaptBusinessOutput({...row,...change},true))
  assert.equal(m.adaptBusinessOutput({...row,data_structure_version:null}).data_structure_version,null,'old summary is visible, not usable detail')
  const calls:any[]=[]
  api.post=async(path:any,data:any)=>{calls.push({method:'post',path,data});return {data:row}}
  api.patch=async(path:any,data:any)=>{calls.push({method:'patch',path,data});return {data:row}}
  api.get=async()=>({data:{count:1,next:null,previous:null,results:[row]}})
  await m.businessOutputs.save({name:row.name,description:'',parser_key:row.parser_key,data_structure_version:row.data_structure_version,quality_advisories:{}},row)
  await m.businessOutputs.action(row,'publish')
  assert.equal(calls[0].data.expected_revision,1);assert.deepEqual(calls[1].data,{expected_revision:1})
  assert.equal(calls[0].data.data_structure_version,row.data_structure_version)
  assert.equal(calls[1].path,`/business-output-versions/${row.id}/publish/`)
  assert.equal((await m.businessOutputs.list()).count,1)
  api.get=async()=>({data:{count:'wrong',results:[]}})
  await assert.rejects(m.businessOutputs.list())
})

test('saved parser supplies version fields and preview rejects a mismatched version or digest',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/businessOutputs.ts')
  const {adaptVisualRulesOutputOptions}=await vite.ssrLoadModule('/src/services/visualRulesOutput.ts')
  const metadata=adaptVisualRulesOutputOptions(visualRulesOutputRaw)
  const frozen=structuredClone(row);frozen.parser_contract.fields[0].label='Frozen label'
  frozen.parser_contract.response_example.continuity_summary='Frozen example'
  const options=m.outputVersionOptions(metadata,frozen)
  assert.equal(options.outputs[0].label,'Frozen label');assert.equal(options.responseExample.continuity_summary,'Frozen example')
  assert.notEqual(metadata.outputs[0].label,'Frozen label')
  assert.throws(()=>m.outputVersionOptions(metadata,{...row,parser_contract:undefined}))
  const {previewVisualRulesOutput}=await vite.ssrLoadModule('/src/services/visualRulesOutputPreview.ts'),{api}=await vite.ssrLoadModule('/src/services/api.ts')
  const calls:any[]=[];let digest=row.contract_sha256
  api.post=async(path:any,data:any)=>{calls.push({path,data});return {data:{preview_only:true,response_schema_version:2,valid:false,candidate:null,warnings:[],errors:[{code:'invalid',message:'invalid sample'}],output_version_id:row.id,output_revision:1,output_contract_sha256:digest}}}
  const version={id:row.id,revision:1,sha256:row.contract_sha256}
  const response=await previewVisualRulesOutput('{}',{schema_version:1},version)
  assert.equal(response.valid,false)
  assert.deepEqual(calls[0].data,{response_text:'{}',output_version_id:row.id,expected_revision:1})
  digest='different';await assert.rejects(previewVisualRulesOutput('{}',undefined,version),/版本不一致/)
})

test('output editor saves once, requires saved publication confirmation and preserves failed input',async context=>{
  const vite=await setup(context),{businessOutputs}=await vite.ssrLoadModule('/src/services/businessOutputs.ts'),{adaptVisualRulesOutputOptions}=await vite.ssrLoadModule('/src/services/visualRulesOutput.ts')
  const {default:Manager}=await vite.ssrLoadModule('/src/components/BusinessOutputManager.vue'),{createSSRApp,h}=await import('vue'),{renderToString}=await import('vue/server-renderer')
  let state:any,saves=0,actions=0
  businessOutputs.list=async()=>({count:1,results:[row],hasNext:false,hasPrevious:false});businessOutputs.options=async()=>({parsers:[{key:row.parser_key,label:'影视规则',description:'规则转换',configuration:'visual_rules'}],parser:row.parser_contract,editor:adaptVisualRulesOutputOptions(visualRulesOutputRaw),allowedValueFields:[{key:'lighting',label:'光线',choices:['low_key','soft_diffuse']}],fieldSourceOptions:outputFieldSourceOptions,responseKeyPolicy:outputResponseKeyPolicy})
  businessOutputs.save=async(data:any,source:any)=>{saves++;return {...row,...data,id:source?.id||'created-output',revision:source?source.revision+1:1}}
  businessOutputs.action=async(source:any)=>{actions++;return {...source,status:'published',revision:source.revision+1}}
  const app=createSSRApp({setup(){state=Manager.setup({},{expose:()=>{},emit:()=>{}});return()=>h('div')}})
  app.provide(matchedRouteKey,ref({leaveGuards:new Set(),updateGuards:new Set()}) as any)
  await renderToString(app)
  await state.load();state.assign(null);state.form.name=row.name;state.form.parser_key=row.parser_key
  await state.save();assert.equal(saves,0,'structure must be explicitly selected')
  state.form.data_structure_version=row.data_structure_version
  state.form.quality_advisories={schema_version:1,continuity_summary_max_chars:1}
  state.form.response_keys={continuity_summary:'rules',color_script:'rules',negative_prompt:'avoid'}
  await state.save();assert.equal(saves,0);assert.ok(state.keyErrors.value.length)
  state.form.response_keys={}
  await state.save();assert.equal(saves,1);assert.equal(state.dirty.value,false)
  state.form.data_structure_version='another-structure';assert.equal(state.dirty.value,true)
  state.form.data_structure_version=row.data_structure_version;assert.equal(state.dirty.value,false)
  await state.act('publish');assert.equal(actions,0)
  state.confirmed.value=true;state.form.description='unsaved';await state.act('publish');assert.equal(actions,0)
  await state.save();state.confirmed.value=true;await state.act('publish');assert.equal(actions,1);assert.equal(state.readonly.value,true)
  await state.save();assert.equal(saves,2)
  state.assign(null);state.form.name='Must keep input';state.form.parser_key=row.parser_key
  state.form.data_structure_version=row.data_structure_version
  businessOutputs.save=async()=>{throw new Error('isolated failure')}
  await state.save();assert.equal(state.form.name,'Must keep input');assert.ok(state.error.value);assert.equal(state.busy.value,false)
})

test('execution output reference is exact, removable and cannot disagree with detail identity',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/executionRegistry.ts')
  const configuration={deployment_id:'deployment',operation:'visual_rules.generate',mode:'',prompt_mode:'manual',prompt_profile_version_id:null,workflow_version_id:null,inputs:[],parameters:[],auxiliaries:[],output_version_id:row.id}
  const original={status:'draft',revision:2,configuration}
  assert.equal(m.adaptExecutionConfiguration(configuration).output_version_id,row.id)
  const cleared={...configuration};delete cleared.output_version_id
  assert.deepEqual(m.executionDraftPatch(original,cleared),{expected_revision:2,output_version_id:null})
  assert.equal(m.executionSelectionMatches(configuration,cleared),false)
  assert.equal(m.executionSelectionMatches(configuration,{...configuration}),true)
})

test('output selection verifies detail, refuses missing structures and ignores superseded reads',async context=>{
  const vite=await setup(context),{businessOutputs}=await vite.ssrLoadModule('/src/services/businessOutputs.ts')
  const {default:Manager}=await vite.ssrLoadModule('/src/components/BusinessOutputManager.vue'),{createSSRApp,h}=await import('vue'),{renderToString}=await import('vue/server-renderer')
  const emitted:any[]=[];let state:any
  const app=createSSRApp({setup(){state=Manager.setup({choose:true},{expose:()=>{},emit:(...args:any[])=>emitted.push(args)});return()=>h('div')}})
  app.provide(matchedRouteKey,ref({leaveGuards:new Set(),updateGuards:new Set()}) as any)
  await renderToString(app)
  for(const change of [{status:'retired'},{data_structure_version:null},{id:'wrong-id'}]){
    businessOutputs.detail=async()=>({...row,status:'published',...change})
    await state.selectVersion(row);assert.equal(emitted.length,0);assert.ok(state.error.value)
  }
  businessOutputs.detail=async()=>{throw new Error('missing detail')}
  await state.selectVersion(row);assert.equal(emitted.length,0)
  let resolve:any;businessOutputs.detail=()=>new Promise(done=>{resolve=done})
  const late=state.selectVersion(row);await Promise.resolve()
  businessOutputs.list=async()=>({count:0,results:[],hasNext:false,hasPrevious:false})
  businessOutputs.options=async()=>({})
  await state.load();resolve({...row,status:'published'});await late;assert.equal(emitted.length,0)
  businessOutputs.detail=async()=>({...row,status:'published'})
  await state.selectVersion(row);assert.equal(emitted.length,1)
  assert.equal(emitted[0][1].data_structure_version,row.data_structure_version)
})

test('output navigation preserves unsaved drafts, rejects mismatched detail and keeps historical identity',async context=>{
  const vite=await setup(context),{businessOutputs}=await vite.ssrLoadModule('/src/services/businessOutputs.ts')
  const {default:Manager}=await vite.ssrLoadModule('/src/components/BusinessOutputManager.vue'),{createSSRApp,h}=await import('vue'),{renderToString}=await import('vue/server-renderer')
  let reads=0,state:any
  const props={initialId:row.id}
  const app=createSSRApp({setup(){state=Manager.setup(props,{expose:()=>{},emit:()=>{}});return()=>h('div')}})
  const record={leaveGuards:new Set<()=>boolean>(),updateGuards:new Set<()=>boolean>()}
  app.provide(matchedRouteKey,ref(record) as any)
  await renderToString(app)
  assert.equal(record.leaveGuards.size,1);assert.equal(record.updateGuards.size,1)
  state.assign(null);state.form.name='Unsubmitted output';state.form.parser_key=row.parser_key
  businessOutputs.detail=async()=>{reads++;return {...row,status:'retired'}}
  const cancelled=state.followContext();assert.equal(state.leaveOpen.value,true);assert.equal(reads,0)
  assert.equal(await state.canLeave(),false,'another navigation cannot replace the pending confirmation')
  state.confirmLeave(false);await cancelled;assert.equal(state.form.name,'Unsubmitted output');assert.equal(reads,0)
  const confirmed=state.followContext();state.confirmLeave(true);await confirmed
  assert.equal(reads,1);assert.equal(state.selected.value.id,row.id);assert.equal(state.readonly.value,true)
  state.busy.value=true;assert.equal(await state.canLeave(),false);state.busy.value=false
  businessOutputs.detail=async()=>({...row,id:'wrong-version'})
  await state.followContext();assert.match(state.error.value,/与目标不一致/);assert.equal(state.selected.value.id,row.id)
  businessOutputs.detail=async()=>{throw new Error('missing output version')}
  await state.followContext();assert.ok(state.error.value);assert.equal(state.selected.value.status,'retired')
  assert.equal(state.busy.value,false)
  let prevented=0;const event={preventDefault(){prevented++},returnValue:undefined}
  state.assign(null);state.form.name='Keep through refresh';state.beforeUnload(event)
  assert.equal(prevented,1);assert.equal(event.returnValue,'')
})
