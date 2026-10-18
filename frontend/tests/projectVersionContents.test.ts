import assert from 'node:assert/strict'
import test from 'node:test'
import {fileURLToPath} from 'node:url'
import {createServer} from 'vite'
import {createRenderer,createSSRApp,h,nextTick,reactive,ssrContextKey} from 'vue'
import {renderToString} from 'vue/server-renderer'
import {createRouter,createMemoryHistory} from 'vue-router'
const root=fileURLToPath(new URL('..',import.meta.url)), sha='a'.repeat(64)
const run={id:'run',project:'project',scriptRevision:'script',planSource:'recipe',inputSnapshot:{request_intent:{creation_inputs:{idea:'原始要求',episode_count:0}}},stages:[],executionPlan:{value:{}}}
const stage=(key,kind,canonical,extra={})=>({id:key,stageKey:key,mode:'ai',outputSnapshot:{formal_object_type:kind,canonical,...extra}})
async function modules(fn){const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}});try{await fn(vite)}finally{await vite.close()}}
async function settle(){for(let i=0;i<12;i++){await Promise.resolve();await nextTick()}}
test('appearance uses match entity and exact variant keys, not equal display names',()=>modules(async v=>{
  const m=await v.ssrLoadModule('/src/services/projectVersionContents.ts')
  const episodes=[{episode:1,shots:[{shot:1,references:[{entity_key:'a',variant_key:null}]},{shot:2,references:[{entity_key:'a',variant_key:'swim'},{entity_key:'b',variant_key:'swim'}]},{shot:3,references:[{entity_key:'a'}]}]}]
  assert.deepEqual(m.declaredAppearanceUses(episodes,'a',null),[{episode:1,shot:1,count:1}])
  assert.deepEqual(m.declaredAppearanceUses(episodes,'a','swim'),[{episode:1,shot:2,count:1}])
  assert.deepEqual(m.declaredAppearanceUses(episodes,'a','unknown'),[])
}))
async function mount(Component,props,path='/projects/project'){
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:all(.*)*',component:{render:()=>null}}]});await router.push(path)
  let state:any;const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit(){}});return()=>h('div')}})
  app.use(router);app.provide(ssrContextKey,{modules:new Set()});app.mount({});await settle();return{state,app,router}
}
test('version source uses original intent or explicit imported revision, never current project fields',()=>modules(async v=>{
  const m=await v.ssrLoadModule('/src/services/projectVersionContents.ts')
  assert.equal(m.creationInput({...run,stages:[stage('script_generate','',{})]}).inputs.idea,'原始要求')
  assert.equal(m.creationInput({...run,executionPlan:{value:{initial_content:{kind:'script_revision',id:'import'}}}}).importedRevisionId,'import')
  assert.equal(m.creationInput(run).inputs.episode_count,0)
  assert.equal(m.approvedScriptPreparation({...run,stages:[stage('script_review','script',{}, {review_payload:{script_revision_id:'foreign',preparation_id:'wrong'}})]}), '')
  assert.equal(m.approvedScriptPreparation({...run,stages:[stage('script_review','script',{}, {review_payload:{script_revision_id:'script',preparation_id:'exact'}})]}), 'exact')
}))

test('read-only tabs keep exact run and section across refresh and browser return',()=>modules(async v=>{
  const {default:Component}=await v.ssrLoadModule('/src/components/ProductionVersionContents.vue')
  const m=await mount(Component,{run},'/projects/project?run=run&section=bible')
  try {
    assert.equal(m.state.section.value,'bible')
    assert.equal(m.state.editSection,undefined)
    assert.equal(m.router.currentRoute.value.query.run,'run')
    await m.router.push('/projects/project?run=run&section=storyboard')
    assert.equal(m.state.section.value,'storyboard')
    assert.equal(m.router.currentRoute.value.query.run,'run')
    await m.router.push('/projects/project?run=another&section=unknown')
    assert.equal(m.state.section.value,'script')
  } finally {m.app.unmount()}
}))
test('stage kinds do not collapse generation and selection; ambiguous snapshots fail visibly',()=>modules(async v=>{
  const m=await v.ssrLoadModule('/src/services/projectVersionContents.ts')
  const board={episodes:[{shots:[{id:'shot-id',script_source:{structure_id:'script',episode_number:1,shot_number:1}}]}]}
  const value={...run,stages:[stage('storyboard','storyboard',{episodes:[]}),stage('storyboard_review','storyboard',board),stage('artifact_selection','media_manifest',{artifacts:[{id:'video',variant_group:'shot:shot-id',sha256:sha,status:'selected'},{id:'other',variant_group:'shot:foreign',status:'selected'}]})]}
  assert.match(m.savedStageContent(value,'storyboard').error,/多份/)
  assert.equal(m.savedShot(value,1,1).shot.id,'shot-id')
  assert.equal(m.savedShot({...value,scriptRevision:'another'},1,1).shot,null)
  assert.equal(m.savedShot(value,1,2).shot,null)
  const candidate={id:'video',generationAttempt:'attempt',sha256:sha,variantGroup:'shot:shot-id'}
  assert.deepEqual(m.selectedAttemptArtifacts({...value,artifacts:[candidate]},'attempt'),[candidate])
  assert.deepEqual(m.selectedAttemptArtifacts({...value,artifacts:[{...candidate,sha256:'other'}]},'attempt'),[])
  assert.deepEqual(m.selectedAttemptArtifacts({...value,artifacts:[candidate]},'foreign-attempt'),[])
}))
test('Bible media handles library assets and generated files with their frozen checksum',()=>modules(async v=>{
  const m=await v.ssrLoadModule('/src/services/projectVersionContents.ts')
  const value={...run,stages:[stage('bible_image_review','bible_image_bindings',{bindings:[{entity_id:'character',entity_name:'甲',anchor_asset:{asset_id:'upload',checksum:sha},reference_assets:[{asset_id:'generated',checksum:'b'.repeat(64)}]}]})]}
  assert.deepEqual(m.bibleMedia(value).bindings[0].images,[{assetId:'upload',sha256:sha},{assetId:'generated',sha256:'b'.repeat(64)}])
}))
test('readable script validates exact revision and content, paginates and discards late responses',()=>modules(async v=>{
  const {platformApi}=await v.ssrLoadModule('/src/services/platformApi.ts'),{scriptRevisionStructures}=await v.ssrLoadModule('/src/services/scriptRevisionStructures.ts')
  const old=platformApi.getScriptRevision,oldRead=scriptRevisionStructures.read
  const document={revision:{id:'script',content_sha256:sha},projection:{value:{entities:[],episodes:[{episode:1,shots:Array.from({length:24},(_,i)=>({shot:i+1,action:'剧情'}))}]}}}
  platformApi.getScriptRevision=async id=>({id,contentSha256:sha,content:'should not become raw json'})
  scriptRevisionStructures.read=async()=>({document,json_schema:{}})
  const {default:C}=await v.ssrLoadModule('/src/components/ProductionScriptReader.vue'),props=reactive({revisionId:'script',section:'script',run}),m=await mount(C,props)
  try{
    assert.equal(m.state.visibleShots.value.length,8);m.state.page.value=3;assert.equal(m.state.visibleShots.value[0].shot,17)
    scriptRevisionStructures.read=async()=>({document:{...document,revision:{id:'foreign',content_sha256:sha}}})
    await m.state.load();assert.equal(m.state.structure.value,null);assert.match(m.state.error.value,/不一致/)
    let resolve;scriptRevisionStructures.read=()=>new Promise(done=>resolve=done);const slow=m.state.load();await settle()
    props.revisionId='';await settle();resolve({document});await slow;assert.equal(m.state.structure.value,null)
    scriptRevisionStructures.read=async()=>null;props.revisionId='script';await settle();assert.equal(m.state.structure.value,null);assert.equal(m.state.error.value,'')
  }finally{m.app.unmount();platformApi.getScriptRevision=old;scriptRevisionStructures.read=oldRead}
}))
test('version catalog is explicit, paged, searchable, and handles missing or foreign runs',()=>modules(async v=>{
  const {platformApi}=await v.ssrLoadModule('/src/services/platformApi.ts'),old=platformApi.getProductionRun
  let calls=0;platformApi.getProductionRun=async id=>{calls++;return{...run,id,project:'foreign'}}
  const {default:C}=await v.ssrLoadModule('/src/components/ProjectVersionWorkspace.vue')
  const props=reactive({projectId:'project',title:'项目',runs:Array.from({length:25},(_,i)=>({...run,id:`id-${i}`})),loading:false,error:''}),m=await mount(C,props)
  try{
    assert.equal(calls,0);assert.equal(m.state.visible.value.length,12);m.state.page.value=3;assert.equal(m.state.visible.value.length,1)
    m.state.search.value='id-24';await settle();assert.equal(m.state.page.value,1);assert.equal(m.state.visible.value.length,1)
    await m.router.push({query:{run:'missing'}});await settle();assert.equal(calls,0);assert.match(m.state.detailError.value,/不属于本项目/)
    await m.router.push({query:{run:'id-1'}});await settle();assert.equal(m.state.detail.value,null);assert.match(m.state.detailError.value,/不一致/)
    props.error='permission denied';await settle();assert.equal(m.state.detail.value,null)
  }finally{m.app.unmount();platformApi.getProductionRun=old}
}))
test('media previews verify frozen file identity and expose stale or forbidden files',()=>modules(async v=>{
  const {repository}=await v.ssrLoadModule('/src/services/repository.ts'),{platformApi}=await v.ssrLoadModule('/src/services/platformApi.ts')
  const old=repository.getAsset,oldArtifact=platformApi.getArtifact
  repository.getAsset=async id=>({id,checksum:sha,mimeType:'image/png',mediaUrl:'/test.png'})
  const {default:C}=await v.ssrLoadModule('/src/components/ProductionArtifactPreview.vue'),props=reactive({assetId:'asset',id:'',runId:'run',sha256:sha,label:'素材'}),m=await mount(C,props)
  try{
    assert.equal(m.state.result.value.kind,'image')
    props.sha256='b'.repeat(64);await settle();assert.equal(m.state.result.value,null);assert.match(m.state.error.value,/不一致/)
    repository.getAsset=async()=>{throw Error('403 无权读取')};await m.state.load();assert.match(m.state.error.value,/403/)
    props.assetId='';props.id='video';props.sha256=sha;platformApi.getArtifact=async()=>({id:'video',productionRun:'other-run',sha256:sha,previewUrl:'/video.mp4'});await settle();assert.equal(m.state.result.value,null)
  }finally{m.app.unmount();repository.getAsset=old;platformApi.getArtifact=oldArtifact}
}))
test('shot results load only on demand using original stage and unit, not variant-group names',()=>modules(async v=>{
  const {executionPreparationApi}=await v.ssrLoadModule('/src/services/executionPreparationApi.ts'),old=executionPreparationApi.forStage
  const selected={id:'video',generationAttempt:'attempt',sha256:sha,variantGroup:'opaque-unit-hash'}
  const value={...run,artifacts:[selected],stages:[stage('media_generation','media_manifest',{}),stage('artifact_selection','media_manifest',{artifacts:[{id:'video',sha256:sha,status:'selected',variant_group:'opaque-unit-hash'}]})]}
  let calls=0;executionPreparationApi.forStage=async(stage,page,kind,unit)=>{
    calls++;assert.equal(stage,'media_generation');assert.equal(page,1);assert.equal(kind,'primary');assert.equal(unit,'shot:shot-id')
    return{count:1,results:[{id:'prep',presentation:{task:{id:'attempt'}},outcome:{message:'调用成功'}}],previous:null,next:null}
  }
  const {default:C}=await v.ssrLoadModule('/src/components/ProductionShotResults.vue'),props=reactive({run:value,shotId:'shot-id'}),m=await mount(C,props)
  try{assert.equal(calls,0);await m.state.load();assert.equal(calls,1);assert.equal(m.state.media.value.prep[0].id,'video')
    let resolve;executionPreparationApi.forStage=()=>new Promise(done=>resolve=done);const slow=m.state.load();await settle();props.shotId='other';await settle();resolve({results:[]});await slow;assert.equal(m.state.result.value,null)
  }finally{m.app.unmount();executionPreparationApi.forStage=old}
}))
test('shared catalog enters the explicitly chosen workspace and keeps its requested section',()=>modules(async v=>{
  const {default:C}=await v.ssrLoadModule('/src/components/ProjectVersionWorkspace.vue')
  const props=reactive({projectId:'project',title:'项目',runs:[run],loading:false,error:'',openWorkspace:true})
  const m=await mount(C,props,'/projects/project/production?step=bible')
  try{
    assert.equal(m.state.requested.value,'');await m.state.open('run');await settle()
    assert.equal(m.router.currentRoute.value.path,'/projects/project/production/run')
    assert.equal(m.router.currentRoute.value.query.step,'bible')
  }finally{m.app.unmount()}
}))

test('one section catalog governs all modes without inferring custom stage meanings',()=>modules(async v=>{
  const m=await v.ssrLoadModule('/src/services/productionWorkspacePresentation.ts')
  assert.deepEqual(m.productionWorkspaceSections.map(row=>row.key),['script','visual','bible','storyboard','media','delivery','records'])
  assert.equal(m.workspaceStageSection('bible_image_review'),'bible')
  assert.equal(m.workspaceStageSection('artifact_selection'),'media')
  assert.equal(m.workspaceStageSection('my_script_extra'),'records')
  assert.equal(m.workspaceSection('unknown'),'script')
  assert.equal(m.workspaceArtifactSection({kind:'image',stageRun:'b'},[{id:'b',stageKey:'bible_image_generation'}]),'bible')
  assert.equal(m.workspaceArtifactSection({kind:'assembly',stageRun:'a'},[]),'delivery')
  const {readFileSync}=await import('node:fs')
  for(const file of ['components/ManualProductionWorkbench.vue','views/ProductionRunView.vue','components/ProductionVersionContents.vue']){
    const source=readFileSync(root+'/src/'+file,'utf8')
    assert.match(source,/:items="productionWorkspaceSections"/)
    assert.doesNotMatch(source,/key:'progress'|key:'release'|key:'episodes'/)
  }
}))

test('read-only grouped source, media and records never expose production mutations',()=>modules(async v=>{
  const {default:C}=await v.ssrLoadModule('/src/components/ProductionVersionContents.vue')
  const value={...run,artifacts:[{id:'media-1',kind:'video',status:'selected',sha256:sha,variantGroup:'opaque-group',variantIndex:1}],
    stages:[{...stage('custom','',{},{}),status:'pending',ordinal:1}]}
  const m=await mount(C,{run:value},'/projects/project?run=run&section=media')
  try{
    assert.equal(m.state.previewId.value,'')
    for(const section of ['media','records','delivery']){
      await m.router.push({query:{run:'run',section}});await settle()
      const html=await renderToString(createSSRApp({...C,setup:()=>m.state},{run:value}).use(m.router))
      assert.match(html,section==='media'?/media-1/:section==='records'?/custom/:/尚无封存发行候选/)
      assert.doesNotMatch(html,/<input|<textarea|<select|确认采用|提交审批|取消运行|重试当前阶段/)
      if(section==='media')assert.doesNotMatch(html,/<video/,'media loads only after an explicit preview')
    }
    await m.router.push({query:{run:'run',section:'visual'}});await settle()
    assert.deepEqual(m.state.visualParts.map(part=>part.key),['visual','terms'])
  }finally{m.app.unmount()}
}))
test('both modes share the presentation shell while historical content exposes no editing controls',()=>modules(async v=>{
  const {default:Header}=await v.ssrLoadModule('/src/components/ProductionWorkspaceHeader.vue')
  const props={title:'项目',run:{...run,status:'succeeded',createdAt:'2026-10-11T02:00:00Z'},gradient:'linear-gradient(#111,#222)'}
  for(const mode of ['operate','readonly']){
    const html=await renderToString(createSSRApp(Header,{...props,mode}))
    assert.match(html,/production-workspace-header/)
    assert.match(html,mode==='readonly'?/版本内容 · 只读/:/生产工作台 · 可操作/)
    assert.match(html,/项目/);assert.match(html,/创建/);assert.match(html,/制作版本 run/)
    assert.doesNotMatch(html,/<input|<textarea|上传封面|新建制作版本/)
  }
  const {default:Tabs}=await v.ssrLoadModule('/src/components/ProductionWorkspaceTabs.vue')
  const tabs=await renderToString(createSSRApp(Tabs,{items:[{key:'source',label:'创作输入'},{key:'bible',label:'Bible 素材'}],modelValue:'bible',label:'版本内容栏目'}))
  assert.match(tabs,/production-workspace-tabs/)
  assert.match(tabs,/aria-current="page"[^>]*>Bible 素材/)
  const {default:C}=await v.ssrLoadModule('/src/components/ProductionVersionContents.vue')
  const sourceRun={...run,stages:[stage('script_generate','',{})]}
  const m=await mount(C,{run:sourceRun},'/projects/project?run=run&section=script')
  try{
    const html=await renderToString(createSSRApp({...C,setup:()=>m.state},{run:sourceRun}).use(m.router))
    assert.match(html,/原始要求/)
    assert.match(html,/production-workspace-tabs/)
    assert.doesNotMatch(html,/<input|<textarea|编辑当前输入|保存当前|新建候选|version-edit-entry/)
    assert.doesNotMatch(html,/production\/run/)
  }finally{m.app.unmount()}
}))
