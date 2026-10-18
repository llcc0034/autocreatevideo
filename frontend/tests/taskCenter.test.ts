import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { createServer } from 'vite'
import { createSSRApp,h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import {createHash} from 'node:crypto'
import {createRenderer,nextTick,reactive,ssrContextKey} from 'vue'
import {adaptTaskDetail,adaptTaskPage,adaptTaskOptions,pageCursor,taskCenterTarget,groupTaskParameters,summarizeTaskPrompts} from '../src/services/taskCenter.ts'
import {mediaTraceFixture} from './fixtures/mediaTrace.ts'
const row=()=>({kind:'preparation',id:'p',category:'smoke',title:'烟测',status:'succeeded',operation:'script.generate',
 project_id:null,project_title:'',production_run_id:null,execution_instance_version_id:'v',execution_instance_name:'实例',execution_version:1,
 preparation_id:'p',source_id:'s',source_kind:'deployment_smoke',created_at:'2026-09-21T01:00:00Z',updated_at:'2026-09-21T01:00:00Z',finished_at:null,
 estimated_cost:'0.01',actual_cost:null,cost_currency:'USD',anomaly:false})
const detail=()=>({...row(),outcome:null,actions:{cancel_asset:false,cancel_smoke:false},purpose:'instance_smoke',preparation_status:'dispatched',request_frozen_at:'2026-09-21T01:00:00Z',request_sha256:'abc',
 submission:{state:'recorded',started_at:null,registered_at:null,identifier_state:'not_recorded',message:'空时间不证明从未外呼',notes:['仅为本地登记，不含实际发送正文。']},
 input:{state:'frozen',submitted:{idea:'draft'},resolved:{},prompt_trace:null,field_batch_trace:null,media_trace:null,api_mapping:null,native_input_trace:null,system_message_trace:null,parameters:[{key:'max_tokens',value:8192,source:'payload.max_tokens'},{key:'seed',value:0,source:'request'}],
 prompts:[{key:'messages.0',role:'system',text:'<script>unsafe()</script>\n真实 system 消息',source:'payload.messages'},{key:'messages.1',role:'user',text:'完整 user 提示词',source:'payload.messages'}],notes:[]},
 output:{state:'available',text:'输出正文',result:{answer:'ok'},media:[],notes:[]},validation:{status:'valid',warnings:[],error_code:'',error_message:''},
 calls:[],call_count:0,calls_next:null,calls_previous:null,output_count:1,output_next:null,output_previous:null,deferred_fields:[],
 links:{generation_task_id:null,asset_job_id:null}})

test('embedded preparation history cannot open mutations or poll and rejects another project',async context=>{
 const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
 const saved=globalThis.localStorage;globalThis.localStorage={getItem:()=>null} as any
 const {createPinia}=await import('pinia'),{default:C}=await vite.ssrLoadModule('/src/components/TaskCenterDetail.vue')
 const {taskCenterApi:api}=await vite.ssrLoadModule('/src/services/taskCenterApi.ts')
 let state:any,reads=0,project='project'
 context.mock.method(api,'detail',async()=>{reads++;return {...detail(),project_id:project,status:'running',actions:{cancel_asset:false,cancel_smoke:true}}})
 const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
 const app=renderer.createApp({setup(){state=C.setup({kind:'preparation',id:'p',readonly:true,expectedProjectId:'project'},{emit(){},expose(){}});return()=>h('div')}})
 context.after(async()=>{app.unmount();globalThis.localStorage=saved;await vite.close()})
 app.use(createPinia());app.provide(ssrContextKey,{modules:new Set()});app.mount({})
 for(let i=0;i<12;i++){await Promise.resolve();await nextTick()}
 assert.equal(reads,1);assert.equal(state.data.value.id,'p')
 state.openCancelTask();await state.cancelTask();assert.equal(state.cancelTarget.value,null)
 state.changeTab('media');assert.equal(state.tab.value,'input')
 state.changeTab('calls');assert.equal(state.tab.value,'calls')
 project='foreign';await state.refresh();assert.match(state.error.value,/不属于当前项目/)
 const source=readFileSync(new URL('../src/components/TaskCenterDetail.vue',import.meta.url),'utf8')
 for(const component of ['ExecutionPreparationWorkbench','TaskMediaInspector'])assert.match(source,new RegExp('<'+component+' v-if="!readonly'))
 assert.match(source,/props.readonly\|\|disposed\|\|error.value/)
})
test('local submission evidence is closed and does not infer submission from task success',async()=>{
 const raw:any=detail()
 assert.equal(adaptTaskDetail(raw).submission.registered_at,null)
 for(const change of ['missing','bad_date','missing_time','bad_state','bad_identifier','contradiction','reversed','inconsistent_as_recorded']){
  const bad=structuredClone(raw)
  if(change==='missing')delete bad.submission
  if(change==='bad_date')bad.submission.started_at='not a date'
  if(change==='missing_time')delete bad.submission.registered_at
  if(change==='bad_state')bad.submission.state='delivered'
  if(change==='bad_identifier')bad.submission.identifier_state='confirmed_received'
  if(change==='contradiction'){bad.submission.state='unavailable';bad.submission.started_at='2026-09-24T00:00:00Z'}
  if(change==='reversed'){bad.submission.started_at='2026-09-24T00:00:01Z';bad.submission.registered_at='2026-09-24T00:00:00Z'}
  if(change==='inconsistent_as_recorded')bad.submission.identifier_state='inconsistent'
  assert.throws(()=>adaptTaskDetail(bad),change)
 }
 const server=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
 try{
  const {default:View}=await server.ssrLoadModule('/src/components/TaskSubmissionEvidence.vue')
  const render=(evidence:any)=>renderToString(createSSRApp({render:()=>h(View,{evidence,sourceKind:'workflow_smoke',sourceId:'exact-call'})}))
  const html=await render(adaptTaskDetail(raw).submission)
  const bodyNote='HTTP库序列化记录：168 字节，SHA256 '+ 'a'.repeat(64)+'；不是已发送或已送达证明。'
  raw.submission.notes=[bodyNote,'<script>private</script>']
  const bodyHtml=await render(adaptTaskDetail(raw).submission)
  assert.ok(bodyHtml.includes(bodyNote));assert.ok(!bodyHtml.includes('<script>private</script>'))
  assert.equal(adaptTaskDetail(raw).submission.registered_at,null)
  assert.match(html,/尚无记录/);assert.match(html,/不证明从未外呼/);assert.match(html,/exact-call/)
  raw.submission={...raw.submission,state:'inconsistent',identifier_state:'inconsistent',message:'<script>unsafe</script>',notes:['不自动重试']}
  const invalid=await render(adaptTaskDetail(raw).submission)
  assert.match(invalid,/role="alert"/);assert.match(invalid,/&lt;script&gt;/);assert.doesNotMatch(invalid,/<script>unsafe/)
  for(const state of ['recorded','digest_only','not_recorded']){
   raw.submission={...raw.submission,state:'recorded',identifier_state:state}
   assert.ok((await render(adaptTaskDetail(raw).submission)).includes('提交证据'))
  }
 }finally{await server.close()}
})

test('task adapters preserve actual values, unknown cost, final prompts and source identities',()=>{
 const d=adaptTaskDetail(detail());assert.equal(d.input.parameters[1].value,0);assert.equal(d.actual_cost,null)
 assert.equal(d.input.prompts[0].text,detail().input.prompts[0].text);assert.equal(d.source_id,'s')
 for(const state of ['recorded','unavailable','unverifiable']) {
   const raw=detail(),usage=state==='recorded'?{prompt_tokens:12,completion_tokens:0,total_tokens:12}:{}
   const receipt={state,task_id:'attempt',observed_at:state==='recorded'?'2026-09-23T11:00:00Z':null,usage}
   const result=adaptTaskDetail({...raw,output:{...raw.output,result:{provider_response:receipt},notes:['用量不是实际扣费；未知不按0计。']}})
   assert.deepEqual(result.output.result,{provider_response:receipt})
   assert.equal(result.output.text,'输出正文');assert.equal(result.actual_cost,null)
   assert.match(result.output.notes[0],/未知不按0计/)
   const asset=adaptTaskDetail({...raw,source_kind:'asset_job',source_id:'asset-aux-call',
     output:{...raw.output,result:{provider_response:{...receipt,task_id:'asset-aux-call'}},notes:['服务商用量：输入token 12；输出token 0。不是实际扣费。']}})
   assert.equal(asset.source_kind,'asset_job')
   assert.equal(asset.output.result.provider_response.task_id,'asset-aux-call')
   assert.equal(asset.actual_cost,null)
   assert.match(asset.output.notes[0],/输出token 0/)
 }
 const p=adaptTaskPage({results:[row()],count:1,next:'/api/v1/task-center/?cursor=signed',previous:null,counts:{all:1,production:0,asset:0,smoke:1},read_at:'2026-09-21'})
 assert.equal(pageCursor(p.next),'signed');assert.equal(pageCursor(null),undefined)
})

test('native input trace exposes bounded mappings, partial evidence and original prompts',async context=>{
 const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
 context.after(()=>vite.close())
 const {default:Component}=await vite.ssrLoadModule('/src/components/TaskInputFacts.vue')
 const raw=detail()
 raw.input.native_input_trace={state:'partial',message:'仅核对user，不是system协议或送达证明',unmapped_count:1,
   rows:[{label:'<script>unsafe()</script>',source:'operation_plan.plan.request.source_text',destination:'payload.messages.1.content → JSON.source_text',state:'matched'}]}
 const render=value=>renderToString(createSSRApp({render:()=>h(Component,{data:adaptTaskDetail(value),reading:''})}))
 const html=await render(raw)
 assert.ok(html.includes('业务输入 → user 消息（原记录核对）'))
 assert.ok(html.includes('部分可核对'));assert.ok(html.includes('另有 1 个未解释的JSON字段'))
 assert.ok(html.includes('operation_plan.plan.request.source_text'));assert.ok(html.includes('&lt;script&gt;unsafe()'))
 assert.ok(!html.includes('<script>unsafe()'));assert.ok(html.includes('完整 user 提示词'))
 for(const field of ['state','message','rows','unmapped_count']) {
   const broken=structuredClone(raw);delete broken.input.native_input_trace[field];assert.throws(()=>adaptTaskDetail(broken),field)
 }
 for(const field of ['label','source','destination','state']) {
   const broken=structuredClone(raw);delete broken.input.native_input_trace.rows[0][field];assert.throws(()=>adaptTaskDetail(broken),field)
 }
 for(const change of ['state','count','row']) {
   const broken=structuredClone(raw)
   if(change==='state')broken.input.native_input_trace.state='success'
   else if(change==='count')broken.input.native_input_trace.unmapped_count=-1
   else broken.input.native_input_trace.rows[0].state='success'
   assert.throws(()=>adaptTaskDetail(broken),change)
 }
 const missing=structuredClone(raw);delete missing.input.native_input_trace;assert.throws(()=>adaptTaskDetail(missing))
 raw.input.native_input_trace={state:'partial',message:'按原冻结条目核对保护词转换；返回骨架不是模型输出，不证明送达',unmapped_count:0,
   rows:[{label:'规范化条目与保护词',source:'operation_plan.plan.request.items（保护词转换，保留顺序与类型）',destination:'payload.messages.1.content → JSON.items',state:'matched'},
         {label:'系统约束（作为user数据）',source:'operation_plan.plan.prompt_profile_assembly.channels.system_instruction（保护词转换）',destination:'payload.messages.1.content → JSON.prompt_profile.system_constraints_as_data',state:'matched'},
         {label:'负向提示词',source:'operation_plan.plan.prompt_profile_assembly.channels.negative_prompt',destination:'payload.messages.1.content → JSON.prompt_profile.negative_prompt',state:'unavailable'}]}
 const normalizationHtml=await render(raw)
 for(const text of ['返回骨架不是模型输出','规范化条目与保护词','保留顺序与类型','系统约束（作为user数据）','无法核对','不能将消息中的空值反推','完整 user 提示词'])assert.ok(normalizationHtml.includes(text),text)
 assert.doesNotMatch(normalizationHtml,/<details[^>]*class="prompt-trace"[^>]*\bopen\b/)
 raw.input.native_input_trace=null;assert.ok(!(await render(raw)).includes('业务输入 → user 消息'))
})

test('frozen protocol help is collapsed while errors and unfrozen notes remain visible',async context=>{
 const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
 context.after(()=>vite.close())
 const {default:Component}=await vite.ssrLoadModule('/src/components/TaskInputFacts.vue')
 const render=(d:any)=>renderToString(createSSRApp({render:()=>h(Component,{data:adaptTaskDetail(d),reading:''})}))
 const raw=detail();raw.input.notes=['安全过滤后的原请求','协议说明 <script>not code</script>','配置归属与证据边界',
  'payload.chat_request_options["temperature"] → JSON["temperature"]。模型版本选项展开。',
  'payload.timeout_seconds → HTTP客户端超时；不进入JSON正文。',
  '这是适配器JSON字段声明，不是逐字节HTTP正文、发送记录或送达证明。']
 let html=await render(raw)
 assert.match(html,/<details[^>]*class="[^"]*protocol-notes[^"]*"[^>]*>/)
 assert.doesNotMatch(html,/<details[^>]*class="[^"]*protocol-notes[^"]*"[^>]*\bopen\b/)
 assert.ok(html.includes('请求协议与配置归属（只读说明）'))
 assert.ok(html.includes('&lt;script&gt;not code&lt;/script&gt;'))
 assert.ok(html.includes('payload.chat_request_options[&quot;temperature&quot;] → JSON[&quot;temperature&quot;]'))
 assert.ok(html.includes('不进入JSON正文'))
 assert.ok(html.includes('不是逐字节HTTP正文'))
 for(const state of ['error','not_frozen','unavailable']){
  const data={...raw,input:{...raw.input,state}}
  html=await render(data)
  assert.ok(!html.includes('class="prompt-trace protocol-notes"'))
  assert.ok(html.includes('配置归属与证据边界'))
 }
 raw.input.notes=['单条说明'];html=await render(raw)
 assert.ok(!html.includes('class="prompt-trace protocol-notes"'))
 assert.ok(html.includes('单条说明'))
 raw.input.notes=['原请求','本次没有冻结Chat组包位置记录；不能按当前适配器补造历史HTTP正文。']
 html=await render(raw)
 assert.ok(html.includes('不能按当前适配器补造历史HTTP正文'))
 assert.ok(!html.includes('JSON[&quot;temperature&quot;]'))
})

test('media trace preserves per-use wiring and receipt states, escapes text and rejects missing evidence fields',async context=>{
 const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
 context.after(()=>vite.close())
 const {default:Component}=await vite.ssrLoadModule('/src/components/TaskInputFacts.vue')
 const raw={...detail(),input:{...detail().input,media_trace:mediaTraceFixture()}}
 const render=(d:any)=>renderToString(createSSRApp({render:()=>h(Component,{data:adaptTaskDetail(d),reading:''})}))
 let html=await render(raw)
 for(const expected of ['工作流接线与媒体回执','提示词／参数 → 声明节点端口','prompt.positive','TextEncoder','声明端口核对一致','尚无暂存回执','cr_images_0','LoadImage','原图接线','输出 0','原始字节','素材入口 → 声明消费端口','原图连通','最短连通路径'])assert.ok(html.includes(expected),expected)
 assert.ok(html.includes('&lt;script&gt;not executable&lt;/script&gt;'));assert.ok(!html.includes('<script>not executable'))
 assert.ok(html.includes('仅有处理声明'));assert.ok(html.includes('冻结处理声明（非实际结果）'))
 for(const expected of ['编译请求新增节点','与原图值相同','编译后值已变化','graph[','原请求值','敏感或路径内容已隐藏','相对原同类型节点已移除端口','image2','本次素材占位引用'])assert.ok(html.includes(expected),expected)
 for(const change of ['origin','missingInputs','missingRemoved','kind','value','link','leak','falseBaseline','removedConflict','duplicate']){
   const changed:any=structuredClone(raw),node=changed.input.media_trace.nodes[0],row=node.inputs[0]
   if(change==='origin')node.origin='guessed'
   if(change==='missingInputs')delete node.inputs
   if(change==='missingRemoved')delete node.removed_inputs
   if(change==='kind')row.kind='executed'
   if(change==='value')delete row.value
   if(change==='link')Object.assign(row,{kind:'connection',value:['node',-1]})
   if(change==='leak')row.value='unredacted-token'
   if(change==='falseBaseline')row.change='unchanged'
   if(change==='removedConflict')node.removed_inputs=['image']
   if(change==='duplicate')node.inputs.push({...row})
   assert.throws(()=>adaptTaskDetail(changed),change)
 }
 for(const state of ['recorded','partial','unverifiable']){
   const changed:any=structuredClone(raw),s=changed.input.media_trace.items[0].processing_summary
   Object.assign(s,{state,shared_with:'video-use-2',byte_identity:'changed',observed:[
     {key:'source_duration_ms',label:'源时长',unit:'ms',source:'processing_metadata.source_duration_ms',state:'recorded',value:0},
     {key:'cfr_guaranteed',label:'记录声明恒定帧率',unit:'',source:'processing_metadata.cfr_guaranteed',state:'recorded',value:false},
     {key:'effective_fps',label:'有效帧率',unit:'fps',source:'processing_metadata.effective_fps',state:'recorded',value:23.976},
     {key:'verified_audio_duration_ms',label:'音频时长',unit:'ms',source:'processing_metadata.verified_audio_duration_ms',state:'unavailable',value:null},
     {key:'verified_frame_count',label:'探测帧数',unit:'帧',source:'processing_metadata.verified_frame_count',state:'missing',value:null},
   ]})
   html=await render(changed)
   for(const expected of ['video-use-2','不是独立音轨转换或上传','0 ms','否','23.976 fps','原记录未提供确定值','原记录缺字段','来源与提交字节摘要不同'])assert.ok(html.includes(expected),expected)
 }
 for(const change of ['missing','state','bytes','rows','value','rowState','fakeObserved']){
   const changed:any=structuredClone(raw),s=changed.input.media_trace.items[0].processing_summary
   if(change==='missing')delete changed.input.media_trace.items[0].processing_summary
   if(change==='state')s.state='success'
   if(change==='bytes')s.byte_identity='maybe'
   if(change==='rows')delete s.observed
   if(['value','rowState','fakeObserved'].includes(change))s.observed=[{key:'duration',label:'时长',unit:'ms',source:'metadata',state:change==='rowState'?'guessed':'recorded',value:change==='value'?{}:1}]
   assert.throws(()=>adaptTaskDetail(changed),change)
 }
 for(const [mode,state] of [['embedded_audio','recorded'],['context_only','not_applicable']]){
   const changed=structuredClone(raw);changed.input.media_trace.items[0].mode=mode;changed.input.media_trace.items[0].receipt_state=state
   html=await render(changed);assert.ok(html.includes(mode==='embedded_audio'?'共用配对视频上传':'仅提示词上下文'))
 }
 const deferred={...raw,input:{...raw.input,media_trace:null},deferred_fields:[{field:'input.media_trace',sha256:'b'.repeat(64),size_bytes:80000,total_characters:80000,encoding:'json'}]}
 assert.ok((await render(deferred)).includes('加载完整工作流接线解释'))
 for(const state of ['unavailable','mismatch','partial']){
   const changed=structuredClone(raw);changed.input.media_trace.scalar_wiring.state=state
   html=await render(changed);assert.ok(html.includes('标量接线：'));assert.ok(html.includes('LoadImage'))
 }
 for(const state of ['disconnected','unavailable']){
   const changed:any=structuredClone(raw),route=changed.input.media_trace.items[0].port_routes[0]
   Object.assign(route,{state,entry_node_id:null,edge_indexes:[],destination:state==='unavailable'?null:route.destination})
   html=await render(changed);assert.ok(html.includes(state==='disconnected'?'原图未连通':'目标无法核对'))
 }
 for(const change of ['missing','state','negative','outOfRange','fraction','duplicate','wrongTarget','missingStart','falseConnection']){
   const changed:any=structuredClone(raw),item=changed.input.media_trace.items[0],route=item.port_routes[0]
   if(change==='missing')delete item.port_routes
   if(change==='state')route.state='success'
   if(change==='negative')route.edge_indexes=[-1]
   if(change==='outOfRange')route.edge_indexes=[1]
   if(change==='fraction')route.edge_indexes=[0.5]
   if(change==='duplicate')route.edge_indexes=[0,0]
   if(change==='wrongTarget')route.destination.input_name='other'
   if(change==='missingStart')route.entry_node_id=null
   if(change==='falseConnection')route.state='disconnected'
   assert.throws(()=>adaptTaskDetail(changed),change)
 }
 for(const change of ['missing','mode','receipt','path','metadata','edge','wiring','wiringState','wiringRows','wiringType','wiringMessage']){
   const changed:any=structuredClone(raw),trace=changed.input.media_trace
   if(change==='missing')delete changed.input.media_trace
   if(change==='mode')trace.items[0].mode='guessed'
   if(change==='receipt')trace.items[0].receipt_state='success'
   if(change==='path')trace.items[0].graph_inputs[0].input_path=[-1]
   if(change==='metadata')delete trace.items[0].processing_metadata
   if(change==='edge')trace.edges[0].output_index=true
   if(change==='wiring')delete trace.scalar_wiring
   if(change==='wiringState')trace.scalar_wiring.state='success'
   if(change==='wiringRows')trace.scalar_wiring.rows=null
   if(change==='wiringType')trace.scalar_wiring.rows[0].value_type='guess'
   if(change==='wiringMessage')delete trace.scalar_wiring.rows[0].message
   assert.throws(()=>adaptTaskDetail(changed),change)
 }
})
test('no reference media does not hide frozen text-only graph nodes or imply they ran',async context=>{
 const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
 context.after(()=>vite.close())
 const {default:Component}=await vite.ssrLoadModule('/src/components/TaskInputFacts.vue')
 const trace=mediaTraceFixture()
 trace.items=[];trace.edges=[]
 trace.nodes=[{node_id:'unconnected',class_type:'TextOnlyFixture',origin:'original',removed_inputs:[],inputs:[
  {input_name:'seed',kind:'literal',change:'unchanged',value:0},
  {input_name:'enabled',kind:'literal',change:'unchanged',value:false},
 ]}]
 const raw={...detail(),input:{...detail().input,media_trace:trace}}
 const html=await renderToString(createSSRApp({render:()=>h(Component,{data:adaptTaskDetail(raw),reading:''})}))
 for(const text of ['无已解析媒体用途','完整冻结图节点与连接','TextOnlyFixture','unconnected','未连通节点','节点存在不代表已执行'])assert.ok(html.includes(text),text)
 assert.doesNotMatch(html,/从媒体入口可达的原图节点/)
 assert.doesNotMatch(html,/<details[^>]*\bopen\b/)
})

test('REST task mappings expose destinations without claiming delivery, including drift and omission',async context=>{
 const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
 context.after(()=>vite.close())
 const {default:Component}=await vite.ssrLoadModule('/src/components/TaskInputFacts.vue')
 const raw:any=detail()
 raw.input.api_mapping={state:'verified',code:'api_mapping_verified',deployment_id:'exact-deployment',configuration_sha256:'a'.repeat(64),
  message:'同一规则；不是已发送正文或送达回执',rows:[{key:'prompt',field:'input_text<script>',state:'included'},
   {key:'negative_prompt',field:'avoid',state:'omitted'},{key:'audio',field:null,state:'unmapped'}]}
 const render=(d:any)=>renderToString(createSSRApp({render:()=>h(Component,{data:adaptTaskDetail(d),reading:''})}))
 let html=await render(raw)
 for(const expected of ['REST 标准字段 → 供应商字段（非送达证明）','配置摘要核对一致','未提供或 null','未配置映射','实际提示词','不是已发送正文'])assert.ok(html.includes(expected),expected)
 assert.ok(html.includes('input_text&lt;script&gt;'));assert.ok(!html.includes('input_text<script>'))
 assert.doesNotMatch(html,/<details[^>]*\bopen\b[^>]*><summary>REST/)
 const changed=structuredClone(raw);Object.assign(changed.input.api_mapping,{state:'unavailable',code:'deployment_mapping_changed',message:'配置漂移，不补造历史',rows:[]})
 html=await render(changed);assert.ok(html.includes('无法核对映射'));assert.ok(html.includes('配置漂移'));assert.ok(!html.includes('input_text'))
 for(const field of ['state','code','message','deployment_id','configuration_sha256','rows']){
   const broken=structuredClone(raw);delete broken.input.api_mapping[field];assert.throws(()=>adaptTaskDetail(broken),field)
 }
 for(const field of ['key','field','state']){
   const broken=structuredClone(raw);delete broken.input.api_mapping.rows[0][field];assert.throws(()=>adaptTaskDetail(broken),field)
 }
 const broken=structuredClone(raw);delete broken.input.api_mapping;assert.throws(()=>adaptTaskDetail(broken))
 raw.input.api_mapping=null;assert.ok(!(await render(raw)).includes('REST 标准字段 →'))
})
test('incomplete task responses fail explicitly rather than fabricating defaults',()=>{
 for(const field of ['outcome','purpose','input','output','calls','call_count','deferred_fields','request_sha256']){const d:any=detail();delete d[field];assert.throws(()=>adaptTaskDetail(d),field)}
 assert.throws(()=>adaptTaskPage({count:0,results:[]}))
 const d:any=detail();delete d.input.parameters[0].source;assert.throws(()=>adaptTaskDetail(d))
})
test('exact version and task links carry canonical filters only',()=>{
 assert.deepEqual(taskCenterTarget({versionId:'version',category:'smoke',kind:'preparation',id:'prep'}),{name:'workflows',query:{execution_instance_version_id:'version',category:'smoke',kind:'preparation',id:'prep'}})
 assert.equal('scope' in taskCenterTarget({sourceId:'source'}).query,false)
 assert.deepEqual(taskCenterTarget({kind:'preparation',id:'prep',attemptId:'original-call'}),
   {name:'workflows',query:{kind:'preparation',id:'prep',attempt_id:'original-call'}})
 assert.equal('attempt_id' in taskCenterTarget({kind:'assembly',id:'assembly',attemptId:'foreign-call'}).query,false)
 assert.equal('attempt_id' in taskCenterTarget({attemptId:'orphan-call'}).query,false)
 assert.deepEqual(taskCenterTarget({category:'production',runId:'run',stageId:'stage'}),
   {name:'workflows',query:{category:'production',production_run_id:'run',stage_run_id:'stage'}})
})
test('options require explicit server lists and paging',()=>{
 const x={categories:[],statuses:[],operations:[],projects:[],execution_versions:[],pagination:{projects:{count:0,next:null,previous:null}}}
 assert.equal(adaptTaskOptions(x).pagination.projects.count,0)
 assert.throws(()=>adaptTaskOptions({...x,statuses:undefined}))
})

test('parameter presentation groups exact keys and JSON values while preserving source facts',()=>{
 const parameters=[
  {key:'seed',value:0,source:'transport.parameters.values:request'},
  {key:'max_tokens',value:40960,source:'transport.parameters.values:system.execution.resources.review_policy@revision'},
  {key:'seed',value:0,source:'payload.seed'},
  {key:'max_tokens',value:40960,source:'payload.max_tokens'},
  {key:'options',value:{nested:{enabled:false,values:[0,null,'']}},source:'transport.parameters.values:instance'},
  {key:'options',value:{nested:{values:[0,null,''],enabled:false}},source:'payload.options'},
 ]
 const before=structuredClone(parameters),groups=groupTaskParameters(parameters)
 assert.deepEqual(groups.map(g=>g.key),['seed','max_tokens','options'])
 assert.deepEqual(groups[0].values,[{value:0,sources:['transport.parameters.values:request','payload.seed']}])
 assert.equal(groups[2].values.length,1);assert.equal(groups[2].values[0].sources.length,2)
 assert.deepEqual(parameters,before)
})

test('parameter disagreement keeps every value and never coerces empty, boolean, numeric or array values',()=>{
 const values=[0,false,null,'','0',[],[1,2],[2,1],{a:null},{}]
 const parameters=values.map((value,i)=>({key:'value',value,source:'record.'+i}))
 const [group]=groupTaskParameters(parameters)
 assert.deepEqual(group.values.map(v=>v.value),values)
 assert.deepEqual(group.values.map(v=>v.sources),parameters.map(p=>[p.source]))
 assert.deepEqual(groupTaskParameters([]),[])
})

test('graph nodes and differently named semantic parameters retain separate identities',()=>{
 const groups=groupTaskParameters([
  {key:'seed',value:1,source:'payload.seed'},
  {key:'sampling.seed',value:1,source:'payload.compiled_workflow.resolved_semantics'},
  {key:'KSampler.seed',value:1,source:'graph.10.inputs.seed'},
  {key:'KSampler.seed',value:2,source:'graph.11.inputs.seed'},
  {key:'KSampler.seed',value:2,source:'graph.12.inputs.seed'},
 ])
 assert.equal(groups.length,5)
 assert.ok(groups.every(g=>g.values.length===1))
 assert.deepEqual(groups.slice(2).map(g=>g.values[0].sources),[['graph.10.inputs.seed'],['graph.11.inputs.seed'],['graph.12.inputs.seed']])
})
test('prompt display size counts Unicode code points and UTF-8 without mutating frozen rows',()=>{
 const prompts=['A中😀\ne\u0301','', '👩\u200d💻'].map((text,index)=>({key:`messages.${index}`,role:'user',source:'payload.messages',text}))
 const before=structuredClone(prompts), summary=summarizeTaskPrompts(prompts)
 assert.deepEqual(summary.rows,[{characters:6,utf8Bytes:12},{characters:0,utf8Bytes:0},{characters:3,utf8Bytes:11}])
 assert.equal(summary.characters,9);assert.equal(summary.utf8Bytes,23)
 assert.deepEqual(prompts,before)
 assert.deepEqual(summarizeTaskPrompts([]),{rows:[],characters:0,utf8Bytes:0,identicalGroups:[]})
})

test('identical display groups retain all rows and do not normalize, deduplicate or infer semantic equality',()=>{
 const prompts=['same',' same','same','same\n','','','é','e\u0301','[redacted]','[redacted]'].map((text,index)=>({key:`messages.${index}`,role:index?'user':'system',source:'payload.messages',text}))
 const summary=summarizeTaskPrompts(prompts)
 assert.deepEqual(summary.identicalGroups,[[0,2],[8,9]])
 assert.equal(summary.rows.length,prompts.length)
 assert.equal(summary.characters,prompts.reduce((n,p)=>n+Array.from(p.text).length,0))
 assert.equal(summary.utf8Bytes,prompts.reduce((n,p)=>n+Buffer.byteLength(p.text,'utf8'),0))
 const large='中😀'.repeat(100_000)
 const measured=summarizeTaskPrompts([{...prompts[0],text:large},{...prompts[1],text:large}])
 assert.equal(measured.characters,400_000);assert.equal(measured.utf8Bytes,1_400_000)
 assert.deepEqual(measured.identicalGroups,[[0,1]])
})

test('real input component renders core parameters and actual messages as escaped text',async context=>{
 const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
 context.after(()=>vite.close())
 const {default:View}=await vite.ssrLoadModule('/src/components/TaskInputFacts.vue')
 const render=(data:any)=>renderToString(createSSRApp({render:()=>h(View,{data,reading:''})}))
 const html=await render(adaptTaskDetail(detail()))
 for(const value of ['max_tokens','8192','seed','payload.max_tokens','system','完整 user 提示词','实际提示词']) assert.ok(html.includes(value),value)
 assert.ok(html.includes('&lt;script&gt;'));assert.ok(!html.includes('<script>unsafe'))
 assert.ok(html.includes('同一次请求中的不同消息，不是多次模型调用'))
 assert.ok(html.includes('历史请求不会按新配置重写'))
 const unicode=detail();unicode.input.prompts=[{key:'messages.0',role:'system',text:'中😀',source:'payload.messages'},{key:'messages.1',role:'user',text:'中😀',source:'payload.messages'}]
 const measured=await render(unicode)
 for(const text of ['2 段 · 4 字符 · 14 UTF-8字节','2 字符 · 7 UTF-8字节','1 组展示正文整段相同','#1 system · messages.0','#2 user · messages.1','Unicode码点','安全过滤可能','不改原任务状态和请求'])assert.ok(measured.includes(text),text)
 assert.equal((measured.match(/>中😀<\/pre>/g)||[]).length,2)
 const empty=detail();empty.input.prompts=[{key:'positive_prompt',role:'positive_prompt',text:'',source:'transport.prompt_channels.values'}]
 const blank=await render(empty)
 assert.ok(blank.includes('1 段 · 0 字符 · 0 UTF-8字节'));assert.ok(blank.includes('明确的空提示词'))
 assert.ok(!blank.includes('组展示正文整段相同'))
 const media=detail();media.input.prompts=[{key:'positive_prompt',role:'positive_prompt',text:'media prompt',source:'payload.channels'}]
 assert.ok(!(await render(media)).includes('同一次请求中的不同消息'))
 const d=detail();d.input.state='not_frozen';d.input.prompts=[]
 assert.ok((await render(d)).includes('没有最终提示词记录'))
 assert.ok(!(await render(d)).includes('同一次请求中的不同消息'))
 const large:any=detail();large.deferred_fields=[{field:'input.prompts',size_bytes:80000,total_characters:80000,encoding:'json',sha256:'a'.repeat(64)}];large.input.prompts=[]
 // Even if stale rows remain during a pending read, never count them as complete.
 large.input.prompts=unicode.input.prompts
 const waiting=await render(large)
 assert.ok(waiting.includes('加载完整提示词'));assert.ok(waiting.includes('暂不计算正文总量'))
 assert.ok(!waiting.includes('可查看正文合计'));assert.ok(!waiting.includes('组展示正文整段相同'))
 const duplicate=detail();duplicate.input.parameters.push({key:'max_tokens',value:8192,source:'transport.parameters.values:system.execution.resources.review_policy@revision'})
 const grouped=await render(duplicate)
 assert.equal((grouped.match(/scope="rowgroup"/g)||[]).length,2)
 assert.equal((grouped.match(/>8192<\/pre>/g)||[]).length,1)
 assert.ok(grouped.includes('payload.max_tokens'));assert.ok(grouped.includes('system.execution.resources.review_policy@revision'))
 assert.ok(!grouped.includes('参数记录存在冲突'))
 duplicate.input.parameters[2].value=40960
 const conflict=await render(duplicate)
 for(const text of ['参数记录存在冲突','记录值不一致','8192','40960','页面不选择覆盖值','rowspan="2"']) assert.ok(conflict.includes(text),text)
 const deferred:any=duplicate;deferred.deferred_fields=[{field:'input.parameters',size_bytes:80000,total_characters:80000,encoding:'json',sha256:'b'.repeat(64)}]
 const pending=await render(deferred)
 assert.ok(pending.includes('加载完整参数'));assert.ok(!pending.includes('参数记录存在冲突'));assert.ok(!pending.includes('scope="rowgroup"'))
})
test('system sections expose original ranges without reconstructing missing history',async context=>{
 const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
 context.after(()=>vite.close())
 const {default:View}=await vite.ssrLoadModule('/src/components/TaskInputFacts.vue')
 const raw:any=detail()
 raw.input.system_message_trace={state:'matched',message:'只核对原分段，不证明送达',rows:[
  {label:'平台协议',source:'平台适配器 <script>',start:0,end:8,state:'matched'},
  {label:'策略系统通道',source:'原冻结策略，不读取当前配置',start:8,end:12,state:'matched'}]}
 const render=(value:any)=>renderToString(createSSRApp({render:()=>h(View,{data:adaptTaskDetail(value),reading:''})}))
 const html=await render(raw)
 for(const value of ['system 消息分段来源','与组装记录一致','第 1–8 字符','第 9–12 字符','不是Token数','脱敏展示可能改变可见长度'])assert.ok(html.includes(value),value)
 assert.ok(html.includes('&lt;script&gt;'));assert.ok(!html.includes('<script>'))
 raw.input.system_message_trace.state='mismatch';raw.input.system_message_trace.rows[1].state='mismatch'
 assert.ok((await render(raw)).includes('与组装记录不一致'))
 for(const change of ['missing','state','row_state','gap','negative','bool','end','too_many']){
  const broken=structuredClone(raw),trace=broken.input.system_message_trace
  if(change==='missing')delete broken.input.system_message_trace
  else if(change==='state')trace.state='success'
  else if(change==='row_state')trace.rows[1].state='omitted'
  else if(change==='gap')trace.rows[1].start=9
  else if(change==='negative')trace.rows[0].start=-1
  else if(change==='bool')trace.rows[1].end=true
  else if(change==='end')trace.rows[1].end=8
  else trace.rows=Array(12).fill(trace.rows[0])
  assert.throws(()=>adaptTaskDetail(broken),change)
 }
 raw.input.system_message_trace={state:'unavailable',message:'原记录没有分段，不补造历史',rows:[]}
 assert.ok((await render(raw)).includes('原记录没有分段'))
 for(const operation of ['prompt.fields.generate','prompt.fields.translate']){
  raw.operation=operation
  raw.input.system_message_trace={state:'matched',message:'字段协议，非策略system通道',rows:[
   {label:'字段批次返回协议',source:'平台固定JSON结构',start:0,end:137,state:'matched'},
   {label:'字段批次消费与安全约束',source:'具体规则在user批次items.*.field_contract，不是策略system通道',start:137,end:293,state:'matched'}]}
  const fieldHtml=await render(raw)
  for(const text of ['字段批次返回协议','items.*.field_contract','不是策略system通道','第 138–293 字符'])assert.ok(fieldHtml.includes(text),text)
  assert.doesNotMatch(fieldHtml,/<details[^>]*class="prompt-trace"[^>]*\bopen\b/)
 }
 raw.input.system_message_trace=null
 assert.ok(!(await render(raw)).includes('system 消息分段来源'))
})

test('one history entry, bounded lists and explicit task actions remain',()=>{
 const src=(p:string)=>readFileSync(new URL('../src/'+p,import.meta.url),'utf8')
 const view=src('views/WorkflowView.vue'),input=src('components/TaskCenterDetail.vue'),smoke=src('components/ExecutionSmokeEvidence.vue')
 assert.ok(view.includes('taskCenterApi.list'));assert.ok(view.includes('onBeforeRouteLeave'));assert.ok(view.includes('pageCursor'))
 for(const retired of ['studio.initialize()', 'getAssetGenerationJobs()', 'buildTaskListEntries'])assert.ok(!view.includes(retired),retired)
 assert.ok(input.includes('hash!==c.sha256'));assert.ok(input.includes('c.next_offset<=offset'))
 assert.ok(!smoke.includes('<table'));assert.ok(smoke.includes('taskCenterTarget'))
})

test('saved prompt trace shows exact sources, overrides, message checks and deferred reads',async context=>{
 const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
 context.after(()=>vite.close())
 const {default:View}=await vite.ssrLoadModule('/src/components/TaskInputFacts.vue')
 const trace={state:'recorded',message:'仅冻结记录',profile_id:'profile',profile_sha256:'a'.repeat(64),
  fields:[{field_key:'theme',source_selector:'project.logline',channel:'positive_prompt',defaulted:false,reviewed:false,projection_primitive:'',
   sources:[{input_key:'input-1',kind:'project_snapshot',location:'项目设定 → 一句话梗概 <script>',source_id:'project-1',value_sha256:'b'.repeat(64),edited:false}]}],
  channels:[{channel:'positive_prompt',characters:0,state:'matched',destination:'payload.messages.1.content → JSON.prompt_profile.positive_prompt',message:'明确空值也核对'}]}
 const raw={...detail(),input:{...detail().input,prompt_trace:trace}}
 const render=(value:any)=>renderToString(createSSRApp({render:()=>h(View,{data:adaptTaskDetail(value),reading:''})}))
 const html=await render(raw)
 for(const text of ['来源 → 策略字段 → 通道 → 冻结请求','project.logline','input-1','project-1','确认时未编辑','0 字符','与冻结请求对应层一致','JSON.prompt_profile.positive_prompt'])assert.ok(html.includes(text),text)
 assert.ok(html.includes('&lt;script&gt;'));assert.ok(!html.includes('<script>'))
 for(const flag of ['defaulted','reviewed']){
  const value=structuredClone(raw);(value.input.prompt_trace.fields[0] as any)[flag]=true
  const rendered=await render(value)
  assert.ok(rendered.includes(flag==='defaulted'?'本规则冻结的缺失默认值':'实际采用本轮已审核'))
  assert.ok(!rendered.includes('来源对象 project-1'))
 }
 const fixed=structuredClone(raw);fixed.input.prompt_trace.fields[0].source_selector='fixed.value'
 assert.ok((await render(fixed)).includes('本规则冻结的固定值'))
 assert.ok(!(await render(fixed)).includes('来源对象 project-1'))
 const mismatch=structuredClone(raw);mismatch.input.prompt_trace.channels[0].state='mismatch'
 assert.ok((await render(mismatch)).includes('与冻结请求对应层不一致'))
 for(const destination of ['payload.compiled_workflow.resolved_semantics["prompt.positive"]','payload["prompt"]']){
  const media=structuredClone(raw)
  media.input.prompt_trace.channels[0].destination=destination
  media.input.prompt_trace.channels[0].message='仅核对冻结层，不证明供应商已收到或执行成功'
  const rendered=await render(media)
  assert.ok(rendered.includes(destination.replaceAll('"','&quot;')))
  for(const boundary of ['ComfyUI语义输入或REST标准体','不将语义／标准体一致当成物理端口','不证明供应商已收到'])assert.ok(rendered.includes(boundary),boundary)
 }
 const deferred={...raw,input:{...raw.input,prompt_trace:null},deferred_fields:[{field:'input.prompt_trace',sha256:'a'.repeat(64),size_bytes:80000,total_characters:80000,encoding:'json'}]}
 const pending=await render(deferred);assert.ok(pending.includes('加载完整提示词来源'));assert.ok(!pending.includes('source_id'))
 const unavailable={...raw,input:{...raw.input,prompt_trace:{state:'unavailable',message:'装配记录不完整',profile_id:null,profile_sha256:null,fields:[],channels:[]}}}
 assert.ok((await render(unavailable)).includes('装配记录不完整'))
 for(const change of ['missing','bad_state','bad_count','bad_flag']){
  const value:any=structuredClone(raw)
  if(change==='missing')delete value.input.prompt_trace
  if(change==='bad_state')value.input.prompt_trace.channels[0].state='ok'
  if(change==='bad_count')value.input.prompt_trace.channels[0].characters=-1
  if(change==='bad_flag')value.input.prompt_trace.fields[0].sources[0].edited=0
  assert.throws(()=>adaptTaskDetail(value),change)
 }
})

test('field batches explain frozen source and eventual destination without inventing main prompt adoption',async context=>{
 const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
 context.after(()=>vite.close())
 const {default:View}=await vite.ssrLoadModule('/src/components/TaskInputFacts.vue')
 const trace={state:'matched',message:'原user JSON一致',profile_id:'parent-profile',profile_sha256:'a'.repeat(64),batch_sha256:'b'.repeat(64),destination:'payload.messages.1.content → JSON',
  items:[{request_id:'request-1',unit_key:'shot:1',field_key:'subject',source_selector:'request.text',generation_source_selector:'shot.detailed_description',channel:'positive_prompt',
   generation_mode:'model_generated',generation_instruction:'Use facts <script>only</script>',language_policy:'en',generation_output_semantics:'value',item_path:'items.0',source_characters:0,
   source_language:'',target_language:'English',context_selectors:['project.logline'],protected_count:0,logical_input_count:2,subject_fact_count:1}]}
 const raw={...detail(),input:{...detail().input,field_batch_trace:trace}}
 const render=(value:any)=>renderToString(createSSRApp({render:()=>h(View,{data:adaptTaskDetail(value),reading:''})}))
 const html=await render(raw)
 for(const text of ['辅助字段批次','parent-profile','shot.detailed_description','items.0.source_text','0 字符','project.logline','request.text','审核后主策略','明确未提供','按原流程的人工或自动策略审核与采用','不是Smoke通过证明','context_refs','shared_facts','同一单元的相同资料只发送一次'])assert.ok(html.includes(text),text)
 assert.ok(html.includes('&lt;script&gt;only'));assert.ok(!html.includes('<script>only'))
 assert.ok(!html.includes('尚无可展示的冻结策略追溯'))
 for(const state of ['mismatch','unavailable']){
  const changed=structuredClone(raw);changed.input.field_batch_trace.state=state
  assert.ok((await render(changed)).includes(state==='mismatch'?'与冻结请求对应层不一致':'映射尚不可核对'))
 }
 const translated=structuredClone(raw);translated.input.field_batch_trace.items[0].generation_mode='translate_only';translated.input.field_batch_trace.items[0].context_selectors=[]
 assert.ok((await render(translated)).includes('明确无额外上下文'));assert.ok((await render(translated)).includes('翻译'))
 const deferred={...raw,input:{...raw.input,field_batch_trace:null},deferred_fields:[{field:'input.field_batch_trace',sha256:'b'.repeat(64),size_bytes:80000,total_characters:80000,encoding:'json'}]}
 assert.ok((await render(deferred)).includes('加载完整字段批次解释'))
 for(const change of ['missing','bad_state','bad_count','bad_context']){
  const value:any=structuredClone(raw)
  if(change==='missing')delete value.input.field_batch_trace
  if(change==='bad_state')value.input.field_batch_trace.state='success'
  if(change==='bad_count')value.input.field_batch_trace.items[0].source_characters=-1
  if(change==='bad_context')value.input.field_batch_trace.items[0].context_selectors=['good',null]
  assert.throws(()=>adaptTaskDetail(value),change)
 }
})

test('task page preserves exact production scope across filtering, paging, detail and refresh',async context=>{
 const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
 const {createRouter,createMemoryHistory,RouterView}=await import('vue-router')
 const {default:View}=await vite.ssrLoadModule('/src/views/WorkflowView.vue')
 const {taskCenterApi:api}=await vite.ssrLoadModule('/src/services/taskCenterApi.ts')
 const original={...api},requests:any[]=[]
 api.options=async()=>({categories:[{value:'production',label:'项目生产'}],statuses:[],operations:[],projects:[],execution_versions:[],pagination:{}})
 api.list=async(filters:any)=>{requests.push({...filters});return {count:0,results:[],next:null,previous:null,counts:{all:0,production:0,asset:0,smoke:0},read_at:'2026-09-23'}}
 let state:any,app:any
 const host={setup(){state=View.setup({}, {emit(){},expose(){}});return()=>h('div')}}
 const router=createRouter({history:createMemoryHistory(),routes:[{path:'/workflows',name:'workflows',component:host}]})
 context.after(async()=>{app?.unmount();Object.assign(api,original);await vite.close()})
 const settle=async()=>{for(let i=0;i<20;i++){await Promise.resolve();await nextTick()}}
 const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
 await router.push(taskCenterTarget({category:'production',runId:'run-a',stageId:'stage-a'}));await router.isReady()
 app=renderer.createApp({render:()=>h(RouterView)});app.use(router);app.provide(ssrContextKey,{modules:new Set()});app.mount({});await settle()
 const scoped=()=>{assert.equal(requests.at(-1).production_run_id,'run-a');assert.equal(requests.at(-1).stage_run_id,'stage-a')}
 scoped();state.draft.value.status='failed';state.apply();await settle();scoped();assert.equal(requests.at(-1).status,'failed')
 state.move('/api/v1/task-center/?cursor=exact-scope');await settle();scoped();assert.equal(requests.at(-1).cursor,'exact-scope')
 state.chooseCategory('');await settle();scoped();assert.equal(requests.at(-1).cursor,undefined)
 state.open({...row(),id:'prep-a'});await settle();state.close();await settle();scoped()
 await state.load();scoped()
 await router.push({name:'workflows'});await settle()
 assert.equal(requests.at(-1).stage_run_id,undefined);assert.equal(requests.at(-1).production_run_id,undefined)
})

test('task validation is one wrapping column and keeps original failure and warning text',async context=>{
 const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
 const previousStorage=globalThis.localStorage
 globalThis.localStorage={getItem:()=>null} as any
 context.after(async()=>{globalThis.localStorage=previousStorage;await vite.close()})
 const {createPinia}=await import('pinia')
 const {default:Panel}=await vite.ssrLoadModule('/src/components/TaskCenterDetail.vue')
 const {taskCenterApi:api}=await vite.ssrLoadModule('/src/services/taskCenterApi.ts')
 context.mock.method(api,'detail',()=>new Promise(()=>{}))
 for(const kind of ['success','failed','invalid','warning']){
  const raw:any=detail()
  raw.outcome={message:'主调用成功，但未读取到关联产物；请核对输出解析与落库记录。',artifactCount:0,selectedCount:0}
  if(kind==='failed')raw.status='failed'
  if(kind==='failed')raw.outcome.message='主调用失败；辅助成功不代表媒体成功。'
  if(kind==='invalid')raw.validation.status='invalid'
  if(kind!=='success'){
   raw.validation.error_code='error_'+ 'long_identifier_'.repeat(40)
   raw.validation.error_message='原始错误 <script>unsafe</script>'
   raw.validation.warnings=['原始警告一','原始警告二']
  }
  const Tested={...Panel,setup(props:any,ctx:any){const state=Panel.setup(props,ctx);state.data.value=raw;return state}}
  const app=createSSRApp({render:()=>h(Tested,{kind:'preparation',id:'p'})});app.use(createPinia())
  const html=await renderToString(app)
  assert.match(html,/validation-summary/)
  assert.match(html,/选择方式由对应流程决定/)
  assert.doesNotMatch(html,/执行成功，已记录结果/)
  assert.match(html,/调用与产物状态/)
  if(kind==='success')assert.match(html,/未读取到关联产物/)
  if(kind==='failed')assert.match(html,/主调用失败/)
  if(kind==='invalid')assert.match(html,/记录校验未通过/)
  if(kind!=='success'){
   assert.ok(html.includes(raw.validation.error_code));assert.match(html,/原始警告一/);assert.match(html,/原始警告二/)
   assert.match(html,/&lt;script&gt;unsafe&lt;\/script&gt;/)
  }
 }
 const source=readFileSync(new URL('../src/components/TaskCenterDetail.vue',import.meta.url),'utf8')
 assert.match(source,/\.validation-summary\{display:grid;grid-template-columns:minmax\(0,1fr\);min-width:0;overflow-wrap:anywhere\}/)
 assert.match(source,/:disabled="locked" :aria-disabled="loading\|\|locked" @click="refresh"/)
})

test('focus-preserving task refresh ignores repeated activation and dirty or busy state',async context=>{
 const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
 const previousStorage=globalThis.localStorage
 globalThis.localStorage={getItem:()=>null} as any
 const {createPinia}=await import('pinia')
 const {default:Panel}=await vite.ssrLoadModule('/src/components/TaskCenterDetail.vue')
 const {taskCenterApi:api}=await vite.ssrLoadModule('/src/services/taskCenterApi.ts')
 let app:any,state:any,count=0,complete!:(value:any)=>void
 context.after(async()=>{app?.unmount();globalThis.localStorage=previousStorage;await vite.close()})
 context.mock.method(api,'detail',async()=>{count++;return detail()})
 const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
 app=renderer.createApp({setup(){state=Panel.setup(reactive({kind:'preparation',id:'p'}),{emit(){},expose(){}});return()=>h('div')}})
 app.use(createPinia());app.provide(ssrContextKey,{modules:new Set()});app.mount({});await nextTick()
 assert.equal(count,1)
 api.detail=()=>{count++;return new Promise(resolve=>{complete=resolve})}
 const reading=state.refresh();await nextTick()
 assert.equal(state.loading.value,true)
 await state.refresh();assert.equal(count,2,'aria-disabled blocks repeat Enter and click')
 complete(detail());await reading
 for(const key of ['busy','dirty','reading']){
  state[key].value=key==='reading'?'output.text':true
  await state.refresh();assert.equal(count,2,key)
  state[key].value=key==='reading'?'':false
 }
 api.detail=async()=>{count++;throw Error('refresh failed')}
 await state.refresh()
 assert.equal(state.loading.value,false);assert.equal(state.data.value.id,'p');assert.match(state.error.value,/refresh failed/)
 api.detail=async()=>{count++;return detail()};await state.refresh()
 assert.equal(state.error.value,'');assert.equal(count,4)
})

test('full output is pinned to its original call and survives unchanged refresh only',async context=>{
 const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
 const beforeDocument=globalThis.document,beforeStorage=globalThis.localStorage
 globalThis.document={hidden:false} as any;globalThis.localStorage={getItem:()=>null} as any
 let app:any
 context.after(async()=>{app?.unmount();globalThis.document=beforeDocument;globalThis.localStorage=beforeStorage;await vite.close()})
 const {createPinia}=await import('pinia')
 const {default:Panel}=await vite.ssrLoadModule('/src/components/TaskCenterDetail.vue')
 const {taskCenterApi:api}=await vite.ssrLoadModule('/src/services/taskCenterApi.ts')
 const encoded=JSON.stringify('完整旧调用正文😀'),sha=createHash('sha256').update(encoded).digest('hex')
 const value:any={...detail(),source_kind:'generation_attempt',source_id:'attempt-a'}
 value.output.text='';value.deferred_fields=[{field:'output.text',sha256:sha,size_bytes:80000,total_characters:Array.from(encoded).length,encoding:'json'}]
 let wrongCall=true,requestedAttempt='',state:any
 api.detail=async(_kind:string,_id:string,q:any)=>{
   const r=structuredClone(value)
   if(q.content_field){requestedAttempt=q.attempt_id;if(wrongCall)r.source_id='attempt-b';r.content={field:'output.text',text:encoded,offset:0,next_offset:null,total_characters:Array.from(encoded).length,sha256:sha}}
   return r
 }
 const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
 app=renderer.createApp({setup(){state=Panel.setup(reactive({kind:'preparation',id:'p'}),{emit(){},expose(){}});return()=>h('div')}})
 app.use(createPinia());app.provide(ssrContextKey,{modules:new Set()});app.mount({});await nextTick()
 await state.readField('output.text')
 assert.equal(requestedAttempt,'attempt-a');assert.match(state.error.value,/记录发生变化/);assert.equal(state.data.value.output.text,'')
 wrongCall=false;await state.readField('output.text');assert.equal(state.data.value.output.text,'完整旧调用正文😀')
 await state.load();assert.equal(state.data.value.output.text,'完整旧调用正文😀');assert.equal(state.data.value.deferred_fields.length,0)
 value.source_id='attempt-b';await state.load();assert.equal(state.data.value.output.text,'');assert.equal(state.data.value.deferred_fields.length,1)
})

test('smoke cancellation confirms the original call in-app and isolates repeat, failed and late actions',async context=>{
 const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
 const previousStorage=globalThis.localStorage
 globalThis.localStorage={getItem:()=>null} as any
 const {createPinia}=await import('pinia')
 const {default:Panel}=await vite.ssrLoadModule('/src/components/TaskCenterDetail.vue')
 const {taskCenterApi:api}=await vite.ssrLoadModule('/src/services/taskCenterApi.ts')
 const {platformApi:platform}=await vite.ssrLoadModule('/src/services/platformApi.ts')
 const {repository}=await vite.ssrLoadModule('/src/services/repository.ts')
 const props=reactive({kind:'preparation',id:'p',attemptId:undefined as string|undefined})
 let app:any,state:any,reads=0,failRead=false,resolveCancel!:(v:any)=>void,rejectCancel!:(e:any)=>void
 const writes:string[]=[],events:string[]=[]
 context.after(async()=>{app?.unmount();globalThis.localStorage=previousStorage;await vite.close()})
 const ready=()=>({...detail(),id:props.id,actions:{cancel_asset:false,cancel_smoke:true},source_kind:'workflow_smoke',source_id:'smoke-'+props.id})
 context.mock.method(api,'detail',async()=>{reads++;if(failRead)throw Error('read unavailable');return ready()})
 context.mock.method(repository,'cancelAssetGenerationJob',async(id:string)=>{writes.push('asset:'+id);return {status:'cancelled'}})
 for(const [method,kind] of [['cancelWorkflowSmokeAttempt','workflow'],['cancelDeploymentSmokeAttempt','deployment']])context.mock.method(platform,method,async(id:string)=>{writes.push(kind+':'+id);return new Promise((resolve,reject)=>{resolveCancel=resolve;rejectCancel=reject})})
 const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
 app=renderer.createApp({setup(){state=Panel.setup(props,{emit(name:string){events.push(name)},expose(){}});return()=>h('div')}})
 app.use(createPinia());app.provide(ssrContextKey,{modules:new Set()});app.mount({})
 const settle=async()=>{for(let i=0;i<12;i++){await Promise.resolve();await nextTick()}}
 await settle()
 await state.cancelTask();assert.equal(writes.length,0,'cannot skip confirmation')
 state.openCancelTask();assert.equal(state.cancelTarget.value.sourceId,'smoke-p');assert.equal(state.locked.value,true)
 await state.refresh();assert.equal(reads,1,'confirmation prevents polling replacement')
 state.closeCancelTask();assert.equal(state.cancelTarget.value,null);assert.equal(writes.length,0)
 for(const key of ['busy','dirty','reading','loading']){state[key].value=key==='reading'?'output.text':true;state.openCancelTask();assert.equal(state.cancelTarget.value,null,key);state[key].value=key==='reading'?'':false}
 state.data.value.source_kind='asset_job';state.openCancelTask();assert.equal(state.cancelTarget.value,null)
 state.data.value=ready();state.openCancelTask();state.data.value.source_id='replaced';await state.cancelTask();assert.equal(writes.length,0);assert.equal(state.cancelTarget.value,null)
 state.data.value=ready();state.openCancelTask();const failed=state.cancelTask();await state.cancelTask();state.closeCancelTask();assert.ok(state.cancelTarget.value)
 rejectCancel(Error('cancel unavailable'));await failed;assert.equal(writes.length,1);assert.match(state.cancelError.value,/cancel unavailable/);assert.ok(state.cancelTarget.value);assert.equal(events.filter(e=>e==='changed').length,0)
 const success=state.cancelTask();failRead=true;resolveCancel({status:'cancel_requested'});await success;await settle()
 assert.equal(writes.length,2);assert.equal(state.cancelTarget.value,null);assert.match(state.notice.value,/已受理/);assert.match(state.error.value,/read unavailable/)
 failRead=false;await state.refresh();assert.equal(writes.length,2,'refresh failure cannot resend cancellation')
 state.openCancelTask();props.id='other';await settle();await state.cancelTask();assert.equal(writes.length,2,'changed task invalidates confirmation')
 state.data.value.source_kind='deployment_smoke';state.openCancelTask();const late=state.cancelTask();assert.equal(writes.at(-1),'deployment:smoke-other')
 props.id='third';await settle();resolveCancel({});await late;await settle();assert.equal(state.data.value.id,'third');assert.equal(state.notice.value,'');assert.equal(events.filter(e=>e==='changed').length,1)
 state.openCancelTask();state.auth.user={id:99};await settle();await state.cancelTask();assert.equal(writes.length,3,'changed account invalidates confirmation')
 state.data.value.source_kind='asset_job';state.data.value.source_id='asset-original';state.data.value.actions={cancel_asset:true,cancel_smoke:false}
 state.openCancelTask();await state.cancelTask();await settle();assert.equal(writes.at(-1),'asset:asset-original');assert.equal(events.filter(e=>e==='changed').length,2)
 state.data.value.actions.cancel_asset=false;state.data.value.actions.cancel_smoke=false;state.openCancelTask();assert.equal(state.cancelTarget.value,null,'terminal or cancel-requested action not offered')
 state.data.value=ready();state.openCancelTask();const unmounted=state.cancelTask();app.unmount();resolveCancel({});await unmounted;assert.equal(events.filter(e=>e==='changed').length,2)
 const source=readFileSync(new URL('../src/components/TaskCenterDetail.vue',import.meta.url),'utf8')
 assert.doesNotMatch(source,/window\.confirm/);assert.match(source,/title="确认取消任务"/);assert.match(source,/不会删除原输入、输出、审核或费用记录/)
})
