import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { readFile } from 'node:fs/promises'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, ssrContextKey } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

const root = fileURLToPath(new URL('..', import.meta.url))
const settle = async () => { for (let i = 0; i < 30; i++) { await Promise.resolve(); await nextTick() } }
function deferred() {
  let resolve!: (value: any) => void, reject!: (error: Error) => void
  const promise = new Promise<any>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

test('model resource authoring keeps exact versions and recoverable original writers', async (t) => {
  const previousStorage = globalThis.localStorage, previousWindow = globalThis.window
  const values = new Map<string, string>()
  globalThis.localStorage = { getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) } as any
  globalThis.window = { setInterval: () => 1, clearInterval() {}, setTimeout: () => 1, clearTimeout() {}, localStorage: globalThis.localStorage } as any
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  const { platformApi: api } = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const { repository } = await vite.ssrLoadModule('/src/services/repository.ts')
  const { useAuthStore } = await vite.ssrLoadModule('/src/stores/auth.ts')
  const { default: Component } = await vite.ssrLoadModule('/src/views/SettingsView.vue')
  const originals = { ...api }, originalCapability = repository.getProductionCapability
  let capabilityCalls = 0
  const reset = () => {
    Object.assign(api, originals)
    for (const key of ['listProviders', 'listModelProfiles', 'listDeployments', 'listModelArtifacts', 'listRuntimeHosts', 'listSecretBindings',
      'listAcceptanceSmokeDeploymentRows', 'listDeploymentSmokeAttempts', 'listWorkflowVersions', 'listWorkflowProjectors',
      'listWorkflowProjectorVersions', 'listLegacyWorkflowTemplates', 'listRuntimeHostProbeAttempts', 'listRecipes',
      'listConfigDefinitions', 'listConfigRevisions', 'listPlatformLifecycleAuditEvents', 'listSecretAuditEvents']) api[key] = async () => []
    for (const key of ['getModelProfileRegistryOptions', 'getWorkflowProjectorOptions', 'getRecipeEditorOptions', 'getConfigDefinitionOptions']) api[key] = async () => ({})
    api.resolveConfiguration = async () => ({ values: {}, sources: {} })
    api.getProductionReadiness = async () => ({ status: 'unconfigured', recipes: [], reasonCodes: [] })
    repository.getProductionCapability = async () => { capabilityCalls++; return { effectiveLimits: {} } }
    capabilityCalls = 0
  }
  const mount = async (section = 'models', extra = '') => {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: { render: () => null } }] })
    await router.push(`/settings?section=${section}${extra}`); await router.isReady()
    const pinia = createPinia(), auth = useAuthStore(pinia)
    auth.user = { id: 'operator', isStaff: true, isSuperuser: true }
    const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
    let state: any
    const app = renderer.createApp({ setup() { state = Component.setup({}, { expose() {} }); return () => h('div') } })
    app.use(pinia); app.use(router); app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); await settle()
    return { state, app, router, auth }
  }

  const provider={id:'provider',key:'provider',name:'隔离连接',driverType:'comfyui',driverConfig:{},isActive:true,modalities:['image'],operations:[]}
  const model=(id='source',version=1,status='published')=>({id,profileKey:'family',version,status,provider:'provider',modelIdentifier:'declared-model',modality:'image',specification:{schema_version:1,model_family:'family',capabilities:['image.generate']}})
  const options={specificationSchemaVersion:1,modalities:[{value:'image',label:'图片'}],modelFamilies:['family'],capabilities:[],providerDrivers:[],artifactRoles:[]}
  function catalog(){reset();api.listProviders=async()=>[provider];api.getModelProfileRegistryOptions=async()=>options}
  try {
    await t.test('runtime resource maintenance routes select exact identities, including disabled hosts; missing does not fall back',async()=>{
      catalog();api.listProviders=async()=>[provider,{...provider,id:'second',isActive:false}]
      const m=await mount('models','&provider=second')
      try{assert.equal(m.state.providerManagementOpen.value,true);assert.deepEqual(m.state.providerMaintenanceRows.value.map(r=>r.id),['second']);await m.router.push('/settings?section=models&provider=missing');await settle();assert.equal(m.state.providerMaintenanceRows.value.length,0)}finally{m.app.unmount()}
      catalog();api.listRuntimeHosts=async()=>[{id:'a',status:'active'},{id:'b',status:'disabled'}];api.getLatestRuntimeSnapshot=async()=>{throw{response:{status:404}}}
      const host=await mount('runtime','&host=b')
      try{assert.equal(host.state.selectedHostId.value,'b');assert.equal(host.state.showRetiredSettingsRecords.value,true);await host.router.push('/settings?section=runtime&host=missing');await settle();assert.equal(host.state.selectedHostId.value,'');assert.match(host.state.sectionErrors.value.runtimeHostContext,/未改选/)}finally{host.app.unmount()}
    })
    await t.test('file maintenance requires exact parent and selects the file section, never a same-name alternative',async()=>{
      catalog();api.listModelProfiles=async()=>[model(),model('draft',2,'draft')];api.listModelArtifacts=async()=>[{id:'f',profileVersion:'draft'}]
      const originalDocument=globalThis.document;globalThis.document={getElementById:()=>null} as any
      const m=await mount('models','&model_profile_version=draft&artifact=f')
      try{assert.equal(m.state.selectedModelProfileId.value,'draft');assert.equal(m.state.selectedModelSection.value,'artifacts');await m.router.push('/settings?section=models&model_profile_version=source&artifact=f');await settle();assert.equal(m.state.selectedModelProfileId.value,'');assert.match(m.state.error.value,/不属于/)}finally{m.app.unmount();globalThis.document=originalDocument}
      api.listModelArtifacts=async()=>[]
      const missing=await mount('models','&model_profile_version=draft&artifact=missing')
      try{assert.equal(missing.state.selectedModelProfileId.value,'');assert.match(missing.state.error.value,/指定文件不存在/)}finally{missing.app.unmount()}
    })
    await t.test('four author variants partition every existing field once and preserve the original payload',async()=>{
      const {resourceEditorFields}=await vite.ssrLoadModule('/src/services/registryResourceSchema.ts')
      const {registryAuthorFieldSection,registryAuthorSections}=await vite.ssrLoadModule('/src/services/registryAuthoringPresentation.ts')
      const {createSSRApp}=await import('vue'),{renderToString}=await import('@vue/server-renderer')
      const {default:Editor}=await vite.ssrLoadModule('/src/components/RegistryResourceEditor.vue')
      for(const resource of ['model','deployment','provider','artifact']){
        const payload=resource==='model'?{profile_key:'family',provider:'provider',modality:'image',specification:model().specification}:resource==='deployment'?{key:'d',name:'部署',profile_version:'source',priority:0,is_default:false,runtime_config:{}}:resource==='provider'?{key:'p',name:'连接',driver_type:'comfyui',driver_config:{},is_active:false}:{profile_version:'draft',role:'checkpoint',logical_name:'file',size_bytes:0}
        const before=structuredClone(payload),deps={providers:[provider],modelProfiles:[model(),model('draft',2,'draft')],deployments:[],runtimeHosts:[],modelProfileOptions:options}
        const fields=resourceEditorFields(resource,payload,deps),sections=registryAuthorSections(resource)
        assert.equal(sections.length,4)
        assert.ok(fields.every(field=>sections.some(s=>s.key===registryAuthorFieldSection(resource,field))))
        const html=await renderToString(createSSRApp(Editor,{resource,modelValue:payload,dependencies:deps,authoring:true,authorStatus:'新建 · 未保存'}))
        assert.equal((html.match(/data-authoring-section=/g)||[]).length,4,resource)
        for(const field of fields.filter(f=>!['chat-options'].includes(f.kind)))assert.equal(html.split('data-authoring-field="'+field.path+'"').length-1,1,resource+field.path)
        assert.deepEqual(payload,before)
      }
    })
    await t.test('successor requires the explicitly selected latest version and never silently changes source',async()=>{
      catalog();const older=model(),newer=model('latest',9);newer.modelIdentifier='different'
      api.listModelProfiles=async()=>[older,newer];let request:any
      api.createNextModelProfile=async(id,payload)=>{request={id,payload:structuredClone(payload)};return model('child',10,'draft')}
      const m=await mount()
      try{
        m.state.selectedModelProfileId.value=older.id
        m.state.openNextModelVersionDraft(m.state.modelRegistryFamilies.value[0])
        assert.equal(m.state.editorOpen.value,false);assert.match(m.state.error.value,/未自动切换来源/)
        m.state.selectedModelProfileId.value=newer.id
        m.state.openNextModelVersionDraft(m.state.modelRegistryFamilies.value[0])
        assert.equal(m.state.editorSourceId.value,newer.id);assert.equal(m.state.editorPayload.value.model_identifier,newer.modelIdentifier)
        m.state.editorPayload.value.specification.custom=false
        assert.equal(newer.specification.custom,undefined);assert.equal(older.specification.custom,undefined)
        m.state.editorValidity.value={valid:true,errors:[],warnings:[]};await m.state.submitEditor()
        assert.equal(request.id,newer.id);assert.equal(request.payload.profile_key,undefined);assert.equal(request.payload.version,undefined)
        assert.equal(request.payload.specification.custom,false);assert.equal(m.state.modelWriteUnknown.value,null)
      }finally{m.app.unmount()}
    })
    await t.test('model published edit is rejected; unrelated selection does not imply latest; unchanged saving form cannot navigate',async()=>{
      catalog();const m=await mount()
      try{
        m.state.openEditor('model',model());assert.equal(m.state.editorOpen.value,false)
        m.state.openNextModelVersionDraft({key:'family',versions:[{profile:model()}]})
        assert.equal(m.state.editorOpen.value,false);assert.match(m.state.error.value,/选择确切/)
        m.state.openEditor('provider',provider);assert.equal(m.state.modelHasUnsavedInput.value,false)
        m.state.saving.value=true;await m.router.push('/settings?section=prompts')
        assert.equal(m.router.currentRoute.value.query.section,'models');m.state.saving.value=false
      }finally{m.app.unmount()}
    })
    await t.test('dirty credentials and child-local edits survive close and navigation until explicit discard',async()=>{
      catalog();const m=await mount()
      try{
        m.state.openEditor('provider',provider)
        m.state.editorCredentialId.value='__clear__';m.state.closeRegistryEditor()
        assert.equal(m.state.recipeDiscard.value,true);assert.equal(m.state.editorOpen.value,true)
        m.state.keepRecipeEditor();m.state.editorCredentialId.value='__keep__'
        m.state.modelPendingInput.value=true;await m.router.push('/settings?section=prompts')
        assert.equal(m.router.currentRoute.value.query.section,'models');assert.equal(m.state.recipeDiscard.value,true)
        m.state.discardRecipeEditor();await settle();assert.equal(m.router.currentRoute.value.query.section,'prompts')
      }finally{m.app.unmount()}
    })
    await t.test('known rejection preserves fields and can resubmit; source credential text never added',async()=>{
      catalog();let writes=0
      api.updateProvider=async(id,payload)=>{writes++;assert.equal(payload.credential_id,undefined);if(writes===1)throw{response:{status:400,data:{name:['请填写合法名称']}}};return{...provider,name:payload.name}}
      const m=await mount()
      try{
        m.state.openEditor('provider',provider);m.state.editorPayload.value.name='变更';m.state.editorValidity.value={valid:true,errors:[],warnings:[]}
        await m.state.submitEditor();assert.equal(m.state.modelWriteUnknown.value,null);assert.equal(m.state.editorOpen.value,true);assert.equal(m.state.modelEditorIssues.value[0].section,'identity')
        await m.state.submitEditor();assert.equal(writes,2);assert.equal(m.state.editorOpen.value,false)
      }finally{m.app.unmount()}
    })
    await t.test('unknown create forbids resubmit and requires explicit fresh identity, never latest',async()=>{
      catalog();let writes=0,reads=0
      api.createProvider=async()=>{writes++;throw Error('response lost')}
      const m=await mount()
      try{
        m.state.openEditor('provider');Object.assign(m.state.editorPayload.value,{key:'exact',name:'输入保留',driver_type:'comfyui'});m.state.editorValidity.value={valid:true,errors:[],warnings:[]}
        await m.state.submitEditor();await m.state.submitEditor();assert.equal(writes,1)
        const row={...provider,id:'exact-id',key:'exact',name:'已保存'}
        api.listProviders=async()=>{reads++;return[row,{...row,id:'unrelated',key:'other'}]}
        await m.state.modelRecovery.read();assert.deepEqual(m.state.modelRecoveryRecords.value.map(r=>r.id),['exact-id'])
        assert.equal(m.state.editorPayload.value.name,'输入保留');assert.ok(m.state.modelWriteUnknown.value)
        row.name='最新回读但精确同一记录';await m.state.loadRecoveredModelResource('exact-id')
        assert.equal(reads,2);assert.equal(m.state.editorId.value,'exact-id');assert.equal(m.state.editorPayload.value.name,row.name);assert.equal(m.state.modelWriteUnknown.value,null);assert.equal(writes,1)
      }finally{m.app.unmount()}
    })
    await t.test('unknown successor excludes source and retired draft; late results cannot cross scope',async()=>{
      catalog();const m=await mount()
      try{
        m.state.modelRecovery.mark({resource:'model',key:'family',id:'',sourceId:'source',profileId:''})
        api.listModelProfiles=async()=>[model(),model('candidate',2,'published'),model('draft',3,'draft')]
        await m.state.modelRecovery.read();assert.deepEqual(m.state.modelRecoveryRecords.value.map(r=>r.id),['candidate','draft'])
        await m.state.loadRecoveredModelResource('candidate');assert.ok(m.state.modelWriteUnknown.value)
        const pending=deferred();api.listModelProfiles=()=>pending.promise;const reading=m.state.modelRecovery.read()
        m.auth.user={...m.auth.user,id:'new'};await settle();pending.resolve([model('late',4,'draft')]);await reading
        assert.equal(m.state.modelRecoveryBusy.value,false);assert.equal(m.state.modelRecoveryRecords.value.length,0);assert.equal(m.state.modelWriteUnknown.value,null)
      }finally{m.app.unmount()}
    })
    await t.test('catalog refresh after acknowledgement is a receipt not an unknown or second write',async()=>{
      catalog();let writes=0
      api.updateProvider=async()=>{writes++;api.listProviders=async()=>{throw Error('refresh failed')};return provider}
      const m=await mount()
      try{
        m.state.openEditor('provider',provider);m.state.editorPayload.value.name='变更';m.state.editorValidity.value={valid:true,errors:[],warnings:[]}
        await m.state.submitEditor();await m.state.submitEditor()
        assert.equal(writes,1);assert.equal(m.state.modelWriteUnknown.value,null);assert.ok(m.state.registryWriteReceipt.value);assert.equal(m.state.editorOpen.value,false)
      }finally{m.app.unmount()}
    })
    await t.test('deployment and file writers preserve exact parent, false and zero; recovery refuses a published file parent',async()=>{
      for(const resource of ['deployment','artifact']){
        catalog();api.listModelProfiles=async()=>[model(),model('draft',3,'draft')]
        const m=await mount();let payload:any,writes=0
        const row:any=resource==='deployment'?{id:'deployment',key:'deployment',name:'部署',profileVersion:'source',runtimeConfig:{},runtimeHost:null,priority:0,isDefault:false,status:'active'}:{id:'file',profileVersion:'draft',role:'checkpoint',logicalName:'file',filename:'model.bin',inventoryFolder:'',inventoryNodeType:'',inventoryInputName:'',storageUri:'',sha256:'',sizeBytes:0}
        const writer=resource==='deployment'?'updateDeployment':'updateModelArtifact'
        api[writer]=async(id,p)=>{writes++;payload=structuredClone(p);assert.equal(id,row.id);return row}
        try{
          m.state.openEditor(resource,row);m.state.editorValidity.value={valid:true,errors:[],warnings:[]}
          await m.state.submitEditor();assert.equal(payload.profile_version,row.profileVersion);assert.equal(writes,1)
          if(resource==='deployment'){assert.equal(payload.priority,0);assert.equal(payload.is_default,false);assert.equal(payload.credential_id,undefined)}
          else{
            assert.equal(payload.size_bytes,0);m.state.modelRecovery.mark({resource:'artifact',key:'file',id:'file',sourceId:'',profileId:'draft'})
            api.listModelArtifacts=async()=>[row];api.listModelProfiles=async()=>[model('draft',3,'published')]
            await m.state.loadRecoveredModelResource('file');assert.ok(m.state.modelWriteUnknown.value);assert.match(m.state.modelRecoveryError.value,/草稿未确认/);assert.equal(writes,1)
          }
        }finally{m.app.unmount()}
      }
    })
    await t.test('author actions stay inside the retained owner and source limits reflect the original API',async()=>{
      const source=await readFile(new URL('../src/views/SettingsView.vue',import.meta.url),'utf8')
      const author=source.slice(source.indexOf('<RegistryResourceExplorer :key='),source.indexOf('</RegistryResourceExplorer>',source.indexOf('<RegistryResourceExplorer :key=')))
      assert.match(author,/#actions/);assert.match(author,/@click="submitEditor"/)
      assert.match(author,/:disabled="saving\|\|\(isModelResource\(editorResource\)/)
      assert.match(author,/<ModelResourceWriteRecovery v-if="isModelResource\(editorResource\)&&modelWriteUnknown"/)
      const frame=await readFile(new URL('../src/components/RegistryAuthoringFrame.vue',import.meta.url),'utf8')
      assert.match(frame,/#actions><slot name="actions"/)
      const m=await mount()
      try{catalog();const old=model(),latest=model('latest',2);m.state.modelProfiles.value=[old,latest];m.state.providers.value=[provider];m.state.modelProfileOptions.value=options;m.state.selectedModelFamilyKey.value='family';m.state.selectedModelProfileId.value=old.id
        assert.equal(m.state.modelFamilyCanCreateVersion(m.state.modelRegistryFamilies.value[0]),false)
        assert.match(m.state.modelFamilyNextVersionTitle(m.state.modelRegistryFamilies.value[0]),/明确选中/)
      }finally{m.app.unmount()}
    })
  } finally {
    Object.assign(api,originals);repository.getProductionCapability=originalCapability
    await vite.close();globalThis.localStorage=previousStorage;globalThis.window=previousWindow
  }
})

