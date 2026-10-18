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

test('settings reads only the current section and preserves asynchronous evidence gates', async (t) => {
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
  try {
    const projectorFamily = {id:'family',key:'example.rules',name:'示例方案',description:'原说明',isActive:true}
    const projectorVersion = {id:'v1',projector:'family',projectorKey:'example.rules',version:1,status:'draft',engineKey:'engine',engineVersion:1,providerDriverType:'comfyui',operation:'workflow.import',outputKind:'image',definition:{priority:0},definitionSha256:'exact-digest'}
    async function projectorCreate() {
      reset(); api.getWorkflowProjectorOptions=async()=>({engines:[{key:'engine',version:1}]})
      const m=await mount('workflows'),s=m.state
      s.openWorkflowProjectorCreate(); s.workflowProjectorCreateMode.value='blank'
      Object.assign(s.workflowProjectorCreateFamily,{key:projectorFamily.key,name:projectorFamily.name,description:projectorFamily.description})
      Object.assign(s.workflowProjectorCreateVersion,{engine:s.workflowProjectorEngineValue('engine',1),providerDriverType:'comfyui',operation:'workflow.import',outputKind:'image'})
      s.workflowProjectorCreateAppliedSeedId.value='blank:engine:1';s.workflowProjectorCreateDefinition.value={priority:0}
      s.workflowProjectorCreateDefinitionValidity.value={valid:true,errors:[]}
      return m
    }
    await t.test('Projector family acknowledgement cannot dispatch v1 after account, external section or unmount changes',async()=>{
      for(const change of ['account','section','unmount']){
        const m=await projectorCreate(),s=m.state,pending=deferred();let families=0,versions=0
        api.createWorkflowProjector=()=>{families++;return pending.promise};api.createWorkflowProjectorVersion=async()=>{versions++;return projectorVersion}
        try{
          const writing=s.submitWorkflowProjectorCreate();await s.submitWorkflowProjectorCreate();assert.equal(families,1)
          if(change==='account')m.auth.user={...m.auth.user,id:'different'}
          if(change==='section')s.tab.value='prompts'
          if(change==='unmount')m.app.unmount()
          await settle();pending.resolve(projectorFamily);await writing
          assert.equal(versions,0,change);assert.equal(s.workflowProjectorCreateFamilyId.value,'');assert.equal(s.notice.value,'')
        }finally{if(change!=='unmount')m.app.unmount()}
      }
    })
    await t.test('Projector family success plus explicit v1 rejection preserves family and retries only corrected v1',async()=>{
      const m=await projectorCreate(),s=m.state;let families=0,versions=0
      api.createWorkflowProjector=async()=>{families++;return projectorFamily}
      api.createWorkflowProjectorVersion=async()=>{versions++;throw{response:{status:400,data:{definition:['缺少输出']}}}}
      try{
        await s.submitWorkflowProjectorCreate();assert.equal(s.workflowProjectorCreateFamilyId.value,'family')
        assert.equal(s.projectorWriteUnknown.value,null);assert.match(s.workflowProjectorError.value,/明确拒绝/)
        api.createWorkflowProjectorVersion=async()=>{versions++;return projectorVersion}
        await s.submitWorkflowProjectorCreate();assert.equal(families,1);assert.equal(versions,2)
        assert.equal(s.workflowProjectorEditorVersion.value.id,'v1');assert.equal(s.workflowProjectorCreateOpen.value,false)
      }finally{m.app.unmount()}
    })
    await t.test('Projector unknown family or v1 response blocks replay, empty reads stay unknown, exact record requires explicit loading',async()=>{
      for(const phase of ['family','v1']){
        const m=await projectorCreate(),s=m.state;let writes=0
        api.createWorkflowProjector=async()=>{writes++;if(phase==='family')throw Error('lost');return projectorFamily}
        api.createWorkflowProjectorVersion=async()=>{writes++;throw{response:{status:504}}}
        try{
          await s.submitWorkflowProjectorCreate();assert.equal(s.projectorWriteUnknown.value.kind,phase)
          await s.submitWorkflowProjectorCreate();assert.equal(writes,phase==='family'?1:2)
          await s.projectorRecovery.read();assert.equal(s.projectorRecoveryChecked.value,true);assert.equal(s.projectorRecoveryRecords.value.length,0);assert.ok(s.projectorWriteUnknown.value)
          api.listWorkflowProjectors=async()=>[projectorFamily];api.listWorkflowProjectorVersions=async()=>[projectorVersion]
          api.getWorkflowProjectorVersion=async()=>projectorVersion
          await s.projectorRecovery.read();assert.ok(s.projectorWriteUnknown.value)
          if(phase==='family'){await s.loadRecoveredProjector('family');assert.equal(s.projectorWriteUnknown.value.kind,'v1')}
          await s.loadRecoveredProjector('v1');assert.equal(s.projectorWriteUnknown.value,null);assert.equal(s.workflowProjectorEditorVersion.value.id,'v1')
          assert.equal(writes,phase==='family'?1:2)
        }finally{m.app.unmount()}
      }
    })
    await t.test('Projector save and successor unknown or 409 lock writes across refresh and refuse foreign exact reads',async()=>{
      for(const kind of ['update','clone']){
        const m=await projectorCreate(),s=m.state;let writes=0
        s.workflowProjectorCreateOpen.value=false;s.openWorkflowProjectorEditor(projectorVersion)
        s.workflowProjectorDefinition.value={priority:9};s.workflowProjectorDefinitionValidity.value={valid:true,errors:[]}
        api.updateWorkflowProjectorVersion=async()=>{writes++;throw{response:{status:409}}}
        api.createNextWorkflowProjectorVersion=async()=>{writes++;throw Error('response unknown')}
        const write=()=>kind==='update'?s.saveWorkflowProjectorDraft():s.createNextWorkflowProjectorDraft(projectorVersion)
        try{
          await write();assert.equal(s.projectorWriteUnknown.value.kind,kind);await s.load();await write();assert.equal(writes,1)
          const draft={...projectorVersion,id:kind==='update'?'v1':'v2',version:kind==='update'?1:2}
          api.listWorkflowProjectorVersions=async()=>[draft];api.getWorkflowProjectorVersion=async()=>({...draft,id:'foreign'})
          await s.loadRecoveredProjector(draft.id);assert.ok(s.projectorWriteUnknown.value);assert.match(s.projectorRecoveryError.value,/身份/)
          api.getWorkflowProjectorVersion=async()=>draft;await s.loadRecoveredProjector(draft.id)
          assert.equal(s.projectorWriteUnknown.value,null);assert.equal(s.workflowProjectorEditorVersion.value.id,draft.id);assert.equal(writes,1)
        }finally{m.app.unmount()}
      }
    })
    await t.test('Projector navigation and refresh warnings include child-only drafts and pending writes',async()=>{
      const m=await projectorCreate(),s=m.state
      try{
        s.workflowProjectorCreateFamily.key='';s.workflowProjectorCreateFamily.name='';s.workflowProjectorCreateFamily.description=''
        s.workflowProjectorCreateDefinitionValidity.value={valid:false,errors:['未应用JSON'],pendingEdits:true}
        await m.router.push('/settings?section=prompts');assert.equal(s.tab.value,'workflows');assert.ok(s.projectorNavigationDiscard.value)
        s.projectorNavigationDiscard.value=null;assert.equal(s.workflowProjectorCreateOpen.value,true)
        let prevented=0;const event={preventDefault(){prevented++},returnValue:null};s.warnProjectorUnload(event);assert.equal(prevented,1)
        await m.router.push('/settings?section=prompts');s.discardProjectorNavigation();await settle();assert.equal(s.tab.value,'prompts')
      }finally{m.app.unmount()}
    })
    await t.test('Projector exact deep links reject wrong identities and obsolete account responses',async()=>{
      reset();const id='11111111-1111-4111-8111-111111111111',m=await mount('workflows')
      try{
        api.getWorkflowProjectorVersion=async()=>({...projectorVersion,id:'other'})
        await m.router.push(`/settings?section=workflows&projector_version=${id}`);await settle()
        assert.match(m.state.projectorNavigationError.value,/身份/);assert.equal(m.state.workflowProjectorEditorOpen.value,false)
        const pending=deferred();api.getWorkflowProjectorVersion=()=>pending.promise
        const read=m.state.followWorkflowProjectorRoute();m.auth.user={...m.auth.user,id:'new-account'};await settle()
        pending.resolve({...projectorVersion,id});await read;assert.equal(m.state.workflowProjectorEditorOpen.value,false)
      }finally{m.app.unmount()}
    })
    await t.test('workflow detail distinguishes pending, failed and exact identity before showing the contract', async () => {
      reset()
      const m = await mount('workflows')
      try {
        const pending = deferred()
        api.getWorkflowVersion = async () => pending.promise
        const request = m.state.showWorkflowDetail({id:'wf-one',templateName:'精确工作流',version:1})
        assert.equal(m.state.detailWorkflowLoading.value,true)
        pending.reject(new Error('读取失败')); await request
        assert.equal(m.state.detailWorkflowLoading.value,false)
        assert.match(m.state.detailWorkflowError.value,/读取失败/)
        api.getWorkflowVersion = async () => ({id:'wf-other',version:2})
        await m.state.showWorkflowDetail({id:'wf-one',version:1})
        assert.match(m.state.detailWorkflowError.value,/身份不匹配/)
        api.getWorkflowVersion = async () => ({id:'wf-one',version:1,outputKind:'video'})
        await m.state.showWorkflowDetail({id:'wf-one',version:1})
        assert.equal(m.state.detailWorkflowError.value,'')
        assert.equal(m.state.detailWorkflow.value.outputKind,'video')
      } finally { m.app.unmount() }
    })
    await t.test('all section loads omit unrelated production capability and invalid workflow context stays closed', async () => {
      reset()
      const m = await mount('prompts')
      try {
        assert.equal(m.state.loading.value, false)
        const firstRefresh = m.state.promptReloadGeneration.value
        await m.state.load()
        assert.equal(m.state.promptReloadGeneration.value, firstRefresh + 1)
        for (const section of ['models', 'workflows', 'routing', 'defaults', 'security', 'runtime', 'governance', 'prompts']) {
          await m.router.push(`/settings?section=${section}`); await settle()
          assert.equal(m.state.loading.value, false, section)
        }
        let workflowCalls = 0
        api.listWorkflowVersions = async () => { workflowCalls++; return [] }
        await m.router.push('/settings?section=workflows&model_profile_version=invalid'); await settle()
        assert.equal(workflowCalls, 0)
        assert.equal(m.state.workflowModelContextState.value, 'invalid')
        assert.equal(capabilityCalls, 0)
      } finally { m.app.unmount() }
    })
    await t.test('slow or failed acceptance does not block the model directory and retry is explicit', async () => {
      reset(); const pending = deferred()
      api.listAcceptanceSmokeDeploymentRows = () => pending.promise
      const m = await mount()
      try {
        assert.equal(m.state.loading.value, false)
        assert.equal(m.state.deploymentAcceptanceLoading.value, true)
        assert.equal(m.state.deploymentAcceptanceRows.value, null)
        pending.reject(Error('evidence unavailable')); await settle()
        assert.equal(m.state.deploymentAcceptanceLoading.value, false)
        assert.match(m.state.sectionErrors.value.deploymentAcceptance, /evidence unavailable/)
        api.listAcceptanceSmokeDeploymentRows = async () => [{ deployment: { id: 'deployment' } }]
        await m.state.loadDeploymentAcceptance()
        assert.ok(m.state.deploymentAcceptanceRows.value.deployment)
        assert.equal(m.state.sectionErrors.value.deploymentAcceptance, undefined)
      } finally { m.app.unmount() }
    })
    await t.test('readiness is pending not unconfigured while recipes can be read; failures and retry are independent', async () => {
      reset(); const pending = deferred(); api.getProductionReadiness = () => pending.promise
      const m = await mount('routing')
      try {
        assert.equal(m.state.loading.value, false)
        assert.equal(m.state.productionReadinessLoading.value, true)
        assert.equal(m.state.productionReadinessReportState.value, 'unavailable')
        pending.reject(Error('readiness unavailable')); await settle()
        assert.equal(m.state.productionReadinessLoading.value, false)
        assert.equal(m.state.productionReadinessReportState.value, 'error')
        api.getProductionReadiness = async () => ({ status: 'unconfigured', recipes: [], reasonCodes: [] })
        await m.state.loadProductionReadiness()
        assert.equal(m.state.productionReadinessReportState.value, 'loaded')
      } finally { m.app.unmount() }
    })
    await t.test('recipe catalog failures stay visible and recover by reading without writes', async () => {
      reset()
      let reads = 0, writes = 0
      api.getRecipeEditorOptions = async () => { reads++; throw Error('recipe options unavailable') }
      api.createRecipe = async () => { writes++; throw Error('must not write') }
      const m = await mount('routing')
      try {
        assert.equal(m.state.loading.value, false)
        assert.equal(m.state.recipeEditorOptions.value, null)
        assert.match(m.state.sectionErrors.value.recipeEditorOptions, /recipe options unavailable/)
        m.state.openEditor('recipe')
        assert.equal(m.state.editorOpen.value, false)
        const source = await readFile(`${root}/src/views/SettingsView.vue`, 'utf8')
        assert.ok(/v-if="sectionErrors\.recipeEditorOptions"[^>]*role="alert"/.test(source), 'render the captured catalog failure')
        assert.ok(/配方编辑目录读取失败：\s*\{\{ sectionErrors\.recipeEditorOptions \}\}/.test(source), 'show the actual reason')
        assert.ok(/@click="load\(\)">重新读取配方目录/.test(source), 'retry the read, not a write')
        api.getRecipeEditorOptions = async () => { reads++; return {} }
        await m.state.load(); await settle()
        assert.equal(m.state.sectionErrors.value.recipeEditorOptions, undefined)
        assert.ok(m.state.recipeEditorOptions.value)
        m.state.openEditor('recipe')
        assert.equal(m.state.editorOpen.value, true)
        assert.equal(reads, 2)
        assert.equal(writes, 0)
      } finally { m.app.unmount() }
    })
    await t.test('combination smoke reasons open the execution registry without starting a smoke or opening the host panel', async () => {
      reset()
      let hostReads = 0, smokeWrites = 0
      api.listRuntimeHosts = async () => { hostReads++; return [] }
      const m = await mount('routing')
      try {
        const { executionRegistryApi } = await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
        const originalCreate = executionRegistryApi.smoke
        executionRegistryApi.smoke = async () => { smokeWrites++; throw Error('navigation must not start smoke') }
        try {
          for (const code of ['execution_smoke_missing', 'execution_smoke_failed', 'execution_smoke_expired']) {
            assert.deepEqual(m.state.readinessReasonAction(code), {
              label: '查看执行实例与组合验收', section: 'executions',
            })
            await m.state.drillReadinessReason(code); await settle()
            assert.equal(m.router.currentRoute.value.query.section, 'executions')
            assert.equal(m.state.tab.value, 'executions')
            assert.equal(m.state.readinessReasonFilter.value, '')
          }
          assert.equal(smokeWrites, 0)
          // Instance navigation does not load model-file or runtime catalogs.
          assert.equal(hostReads, 0)
          assert.equal(capabilityCalls, 0)
        } finally { executionRegistryApi.smoke = originalCreate }
      } finally { m.app.unmount() }
    })
    await t.test('tab replacement, account changes and unmount discard old catalogs and evidence', async () => {
      for (const change of ['tab', 'account', 'unmount']) {
        reset(); const catalog = deferred(), evidence = deferred()
        api.listProviders = () => catalog.promise
        const m = await mount()
        if (change === 'tab') await m.router.push('/settings?section=prompts')
        if (change === 'account') { api.listProviders = async () => []; m.auth.user = { ...m.auth.user, id: 'other' } }
        if (change === 'unmount') m.app.unmount()
        await settle(); catalog.resolve([{ id: 'stale' }]); await settle()
        assert.deepEqual(m.state.providers.value, [], change)
        if (change !== 'unmount') {
          api.listAcceptanceSmokeDeploymentRows = () => evidence.promise
          await m.router.push('/settings?section=models'); await settle()
          const reload = m.state.load(); await settle()
          m.app.unmount(); evidence.resolve([{ deployment: { id: 'stale' } }]); await reload; await settle()
          assert.equal(m.state.deploymentAcceptanceRows.value, null)
        }
      }
    })
    await t.test('model limits come only from the model catalog even when production admission fails', async () => {
      reset()
      const limits = {maxSeconds: 3600, maxOverrides: 12}
      api.getModelProfileRegistryOptions = async () => ({executionMonitoring: limits,
        providerDrivers: [], modalities: [], artifactRoles: [], capabilities: [], modelFamilies: [], specificationSchemaVersion: 1})
      repository.getProductionCapability = async () => { capabilityCalls++; throw Error('production_conditioning_contract_invalid') }
      const m = await mount()
      try {
        m.state.openEditor('model'); await settle()
        assert.equal(m.state.editorOpen.value, true)
        assert.deepEqual(m.state.editorDependencies.value.executionMonitoringLimits, limits)
        assert.equal(capabilityCalls, 0)
        assert.equal(m.state.productionCapabilityError.value, '')
        m.state.modelProfileOptions.value = null
        m.state.sectionErrors.value.modelProfileOptions = 'catalog unavailable'
        m.state.editorValidity.value = {valid: true, errors: [], warnings: []}
        await m.state.submitEditor()
        assert.match(m.state.editorError.value, /catalog unavailable/)
      } finally { m.app.unmount() }
    })
    await t.test('smoke resolution loads limits on demand and rejects stale responses', async () => {
      reset(); const first = deferred()
      repository.getProductionCapability = () => { capabilityCalls++; return first.promise }
      const m = await mount()
      try {
        m.state.smokeResolutionDeployment.value = {id: 'resolve'}; await settle()
        assert.equal(capabilityCalls, 1); assert.equal(m.state.productionCapabilityLoading.value, true)
        m.state.smokeResolutionDeployment.value = null; await settle()
        first.resolve({ effectiveLimits: { auditNoteMinCharacters: 1 } }); await settle()
        assert.equal(m.state.productionCapability.value, null)
        repository.getProductionCapability = async () => { throw Error('limits unavailable') }
        m.state.smokeResolutionDeployment.value = {id: 'resolve'}; await settle()
        assert.match(m.state.productionCapabilityError.value, /limits unavailable/)
        repository.getProductionCapability = async () => ({ effectiveLimits: {} })
        await m.state.loadOperationCapability(); assert.ok(m.state.productionCapability.value)
      } finally { m.app.unmount() }
    })
    await t.test('model writes distinguish acknowledged changes from failed refresh and retry only reads', async () => {
      for (const action of ['create', 'update', 'successor', 'publish', 'retire']) {
        reset()
        api.getModelProfileRegistryOptions = async () => ({ providerDrivers: [], modalities: [], artifactRoles: [],
          capabilities: [], modelFamilies: [], specificationSchemaVersion: 1 })
        const m = await mount('models')
        let writes = 0
        const result = { id: action==='update'?'model-source':'model-result', profileKey: 'model-family', version: 2, status: 'draft' }
        const write = async () => { writes++; return result }
        api.createModelProfile = write; api.updateModelProfile = write; api.createNextModelProfile = write
        api.publishModelProfile = write; api.retireModelProfile = write
        try {
          api.listModelProfiles = async () => { throw Error('model read failed') }
          if (['publish', 'retire'].includes(action)) {
            m.state.openAction(`${action}-model`, 'model-source', `${action} model`, 'test')
            m.state.actionReasonCode.value = 'planned_change'
            await m.state.runAction()
            assert.equal(m.state.actionOpen.value, false)
            await m.state.runAction()
          } else {
            m.state.openEditor('model')
            m.state.editorPayload.value = { profile_key: 'model-family', provider: 'provider', model_identifier: 'model',
              modality: 'image', specification: { schema_version: 1, capabilities: [] } }
            m.state.editorValidity.value = { valid: true, errors: [], warnings: [] }
            if (action === 'update') m.state.editorId.value = 'model-source'
            if (action === 'successor') { m.state.editorIntent.value = 'next-model-version'; m.state.editorSourceId.value = 'model-source' }
            await m.state.submitEditor()
            assert.equal(m.state.editorOpen.value, false)
            await m.state.submitEditor()
          }
          assert.equal(writes, 1, action)
          assert.match(m.state.registryWriteReceipt.value?.message || '', /已/)
          assert.equal(m.state.registryWriteRefreshFailed.value, true)
          assert.doesNotMatch(m.state.notice.value, /已重新读取/)
          assert.equal(m.state.actionError.value, '')
          assert.equal(m.state.editorError.value, '')
          const receipt = m.state.registryWriteReceipt.value.message
          api.listModelProfiles = async () => []
          await m.state.load()
          assert.equal(m.state.registryWriteRefreshFailed.value, false)
          assert.equal(m.state.registryWriteReceipt.value.message, receipt)
          assert.equal(writes, 1)
        } finally { m.app.unmount() }
      }
    })
    await t.test('deletion acknowledgement survives failed catalog refresh without claiming a reread', async () => {
      reset(); const m = await mount('models')
      try {
        m.state.openAction('delete-model', 'model', 'delete model', 'review')
        await m.state.onRegistryDeletionCompleted({resourceType:'model_profile_version',objectId:'other'})
        assert.equal(m.state.actionOpen.value, true); assert.equal(m.state.registryWriteReceipt.value, null)
        api.listModelProfiles = async () => { throw Error('catalog offline') }
        await m.state.onRegistryDeletionCompleted({resourceType:'model_profile_version',objectId:'model'})
        assert.equal(m.state.actionOpen.value, false)
        assert.match(m.state.registryWriteReceipt.value.message, /服务端已确认删除完成/)
        assert.equal(m.state.registryWriteRefreshFailed.value, true)
        assert.doesNotMatch(m.state.notice.value, /已重新读取/)
        api.listModelProfiles = async () => []; await m.state.load()
        assert.equal(m.state.registryWriteRefreshFailed.value, false)
        assert.match(m.state.registryWriteReceipt.value.message, /model/)
      } finally { m.app.unmount() }
    })
    await t.test('deletion refresh late completion cannot overwrite a new settings scope', async () => {
      reset(); const m = await mount('models'), pending = deferred()
      try {
        m.state.openAction('delete-model', 'model', 'delete model', 'review')
        api.listModelProfiles = () => pending.promise
        const refreshing = m.state.onRegistryDeletionCompleted({resourceType:'model_profile_version',objectId:'model'})
        await m.router.push('/settings?section=prompts'); await settle()
        pending.resolve([]); await refreshing
        assert.equal(m.state.registryWriteReceipt.value, null); assert.equal(m.state.notice.value, '')
      } finally { m.app.unmount() }
    })
    await t.test('model mutation duplicate calls and late acknowledgements never cross settings scopes', async () => {
      for (const change of ['tab', 'account', 'unmount']) {
        reset(); const m = await mount('models'), pending = deferred()
        let writes = 0
        api.publishModelProfile = () => { writes++; return pending.promise }
        try {
          m.state.openAction('publish-model', 'model', 'publish model', 'test')
          m.state.actionReasonCode.value = 'planned_change'
          const first = m.state.runAction()
          const second = m.state.runAction()
          if (change === 'tab') await m.router.push('/settings?section=prompts')
          if (change === 'account') m.auth.user = { ...m.auth.user, id: 'other' }
          if (change === 'unmount') m.app.unmount()
          await settle(); pending.resolve({ id: 'model' }); await Promise.all([first, second]); await settle()
          assert.equal(writes, 1, change)
          assert.equal(m.state.registryWriteReceipt.value, null)
          assert.equal(m.state.notice.value, '')
        } finally { if (change !== 'unmount') m.app.unmount() }
      }
    })
    await t.test('rejected model publish retains confirmation and does not show a success receipt', async () => {
      reset(); const m = await mount('models')
      try {
        api.publishModelProfile = async () => { throw Error('model publication rejected') }
        m.state.openAction('publish-model', 'model', 'publish model', 'test')
        m.state.actionReasonCode.value = 'planned_change'
        await m.state.runAction()
        assert.equal(m.state.actionOpen.value, true)
        assert.match(m.state.actionError.value, /model publication rejected/)
        assert.equal(m.state.actionReasonCode.value, 'planned_change')
        assert.equal(m.state.registryWriteReceipt.value, null)
      } finally { m.app.unmount() }
    })
    await t.test('Projector lifecycle acknowledgements remain successful when the following catalog read fails', async () => {
      for (const [action, method] of [['publish-workflow-projector', 'publishWorkflowProjectorVersion'],
        ['retire-workflow-projector', 'retireWorkflowProjectorVersion']]) {
        reset(); const m = await mount('workflows')
        let writes = 0
        api[method] = async () => { writes++; return { id: 'projector-version' } }
        try {
          m.state.openAction(action, 'projector-version', action, 'test')
          m.state.actionReasonCode.value = 'planned_change'
          api.listWorkflowProjectorVersions = async () => { throw Error('catalog unavailable') }
          await m.state.runAction()
          assert.equal(writes, 1)
          assert.equal(m.state.actionOpen.value, false)
          assert.equal(m.state.actionError.value, '')
          assert.match(m.state.registryWriteReceipt.value.message, /projector-version/)
          assert.equal(m.state.registryWriteRefreshFailed.value, true)
          assert.equal(m.state.workflowProjectorCatalogState.value, 'stale')
          api.listWorkflowProjectorVersions = async () => []
          await m.state.load()
          assert.equal(m.state.registryWriteRefreshFailed.value, false)
          assert.equal(writes, 1)
        } finally { m.app.unmount() }
      }
    })
    await t.test('configuration draft acknowledgement after a scope change cannot trigger its publish step', async () => {
      reset(); const m = await mount('defaults'), pending = deferred()
      let creates = 0, publishes = 0
      api.createConfigRevision = () => { creates++; return pending.promise }
      api.publishConfigRevision = async () => { publishes++; return {} }
      try {
        m.state.openAction('save-defaults', '', 'publish configuration', 'test')
        m.state.actionReasonCode.value = 'planned_change'
        const writing = m.state.runAction()
        assert.equal(creates, 1)
        await m.router.push('/settings?section=models'); await settle()
        pending.resolve({ id: 'draft-already-created' }); await writing; await settle()
        assert.equal(publishes, 0)
        assert.equal(m.state.registryWriteReceipt.value, null)
      } finally { m.app.unmount() }
    })
    await t.test('Projector catalog failure retains one same-scope snapshot and blocks all dependent writes', async () => {
      for (const failed of ['listWorkflowProjectors', 'listWorkflowProjectorVersions', 'getWorkflowProjectorOptions']) {
        reset()
        const family = { id: 'family', key: 'family', isActive: true }
        const version = { id: 'v1', projector: 'family', version: 1, status: 'draft', definition: {} }
        const options = { engines: [{ key: 'engine', version: 1 }] }
        api.listWorkflowProjectors = async () => [family]
        api.listWorkflowProjectorVersions = async () => [version]
        api.getWorkflowProjectorOptions = async () => options
        const m = await mount('workflows')
        try {
          assert.equal(m.state.workflowProjectorCatalogState.value, 'ready')
          m.state.openAction('publish-workflow-projector', 'v1', 'publish', 'test')
          assert.equal(m.state.actionOpen.value, true)
          let writes = 0
          for (const key of ['createWorkflowProjector', 'createWorkflowProjectorVersion', 'createNextWorkflowProjectorVersion',
            'updateWorkflowProjectorVersion', 'publishWorkflowProjectorVersion', 'discardWorkflowProjectorVersion',
            'retireWorkflowProjectorVersion', 'updateWorkflowProjector']) api[key] = async () => { writes++; throw Error('must not write') }
          const healthy = api[failed], pending = deferred()
          api[failed] = () => pending.promise
          // Successful portions must not mix a newer family list with older versions.
          if (failed !== 'listWorkflowProjectors') api.listWorkflowProjectors = async () => [{ ...family, id: 'changed' }]
          const reading = m.state.load(); await settle()
          assert.equal(m.state.workflowProjectorCatalogState.value, 'loading')
          assert.ok(m.state.workflowProjectorCreateBlockedReason.value)
          assert.deepEqual(m.state.workflowProjectors.value, [family])
          pending.reject(Error('catalog failed')); await reading; await settle()
          assert.equal(m.state.workflowProjectorCatalogState.value, 'stale')
          assert.deepEqual(m.state.workflowProjectors.value, [family])
          assert.deepEqual(m.state.workflowProjectorVersions.value, [version])
          assert.deepEqual(m.state.workflowProjectorOptions.value, options)
          await m.state.submitWorkflowProjectorCreate()
          await m.state.createNextWorkflowProjectorDraft(version)
          m.state.openWorkflowProjectorEditor(version)
          await m.state.saveWorkflowProjectorDraft()
          await m.state.runAction() // an already open confirmation also rechecks
          for (const kind of ['workflow-projector-status', 'publish-workflow-projector', 'discard-workflow-projector', 'retire-workflow-projector']) {
            m.state.actionKind.value = kind; await m.state.runAction()
          }
          assert.equal(writes, 0)
          assert.match(m.state.actionError.value, /尚未完整更新/)
          api[failed] = healthy
          api.listWorkflowProjectors = async () => [family]
          await m.state.load()
          assert.equal(m.state.workflowProjectorCatalogState.value, 'ready')
          assert.equal(m.state.workflowProjectorMutationBlockedReason.value, '')
          assert.equal(Object.keys(m.state.sectionErrors.value).filter(k => k.startsWith('workflowProjector')).length, 0)
        } finally { m.app.unmount() }
      }
    })
    await t.test('Projector acknowledged writes survive catalog failure without offering a duplicate write', async () => {
      for (const operation of ['successor', 'save']) {
        reset()
        const family = { id: 'family', key: 'family', isActive: true }
        const version = { id: 'v1', projector: 'family', projectorKey: 'family', version: 1,
          status: operation === 'save' ? 'draft' : 'published', isCurrent: operation !== 'save',
          engineKey: 'engine', engineVersion: 1, providerDriverType: 'comfyui', operation: 'workflow.import',
          outputKind: 'image', definition: { priority: 1 } }
        const result = { ...version, id: operation === 'save' ? 'v1' : 'v2',
          version: operation === 'save' ? 1 : 2, status: 'draft', isCurrent: false, definition: { priority: 7 } }
        api.listWorkflowProjectors = async () => [family]
        api.listWorkflowProjectorVersions = async () => [version]
        api.getWorkflowProjectorOptions = async () => ({ engines: [{ key: 'engine', version: 1 }] })
        const m = await mount('workflows')
        let writes = 0
        const write = async () => { writes++; return result }
        api.createNextWorkflowProjectorVersion = write
        api.updateWorkflowProjectorVersion = write
        try {
          api.listWorkflowProjectorVersions = async () => { throw Error('catalog read unavailable') }
          if (operation === 'save') {
            m.state.openWorkflowProjectorEditor(version)
            m.state.workflowProjectorDefinition.value = { priority: 7 }
            m.state.workflowProjectorDefinitionValidity.value = { valid: true, errors: [] }
            await m.state.saveWorkflowProjectorDraft()
            assert.equal(m.state.workflowProjectorEditorOpen.value, false, 'acknowledged save is not a failed form')
          } else {
            await m.state.createNextWorkflowProjectorDraft(version)
            assert.equal(m.state.workflowProjectorEditorVersion.value?.id, result.id, 'open the already-created draft')
          }
          assert.match(m.state.notice.value, operation === 'save' ? /已保存/ : /已创建/)
          assert.match(m.state.notice.value, /目录刷新失败/)
          assert.match(m.state.sectionErrors.value.workflowProjectorRefresh, /重新读取/)
          assert.equal(m.state.workflowProjectorCatalogState.value, 'stale')
          assert.deepEqual(m.state.workflowProjectorVersions.value.find((row: any) => row.id === result.id), result)
          assert.equal(m.state.workflowProjectorError.value, '')
          await m.state.createNextWorkflowProjectorDraft(version)
          await m.state.saveWorkflowProjectorDraft()
          assert.equal(writes, 1, 'stale catalog cannot authorize another mutation')
          api.listWorkflowProjectorVersions = async () => operation === 'save' ? [result] : [version, result]
          await m.state.readWorkflowProjectorCatalog()
          assert.equal(m.state.workflowProjectorCatalogState.value, 'ready')
          assert.equal(m.state.sectionErrors.value.workflowProjectorRefresh, undefined)
          assert.equal(writes, 1, 're-reading only reads')
        } finally { m.app.unmount() }
      }
    })
    await t.test('Projector rejected save retains input and late write acknowledgements do not reopen another scope', async () => {
      reset()
      const version = { id: 'v1', projector: 'family', projectorKey: 'family', version: 1, status: 'draft',
        engineKey: 'engine', engineVersion: 1, providerDriverType: 'comfyui', operation: 'workflow.import',
        outputKind: 'image', definition: { priority: 1 } }
      api.listWorkflowProjectorVersions = async () => [version]
      api.getWorkflowProjectorOptions = async () => ({ engines: [{ key: 'engine', version: 1 }] })
      const m = await mount('workflows')
      try {
        m.state.openWorkflowProjectorEditor(version)
        m.state.workflowProjectorDefinition.value = { priority: 7 }
        m.state.workflowProjectorDefinitionValidity.value = { valid: true, errors: [] }
        api.updateWorkflowProjectorVersion = async () => { throw {response:{status:400,data:{message:'definition rejected'}}} }
        await m.state.saveWorkflowProjectorDraft()
        assert.equal(m.state.workflowProjectorEditorOpen.value, true)
        assert.deepEqual(m.state.workflowProjectorDefinition.value, { priority: 7 })
        assert.match(m.state.workflowProjectorError.value, /definition rejected/)
        assert.equal(m.state.notice.value, '')
        m.state.closeWorkflowProjectorEditor()
        m.state.confirmWorkflowProjectorEditorDiscard()
        const pending = deferred()
        api.createNextWorkflowProjectorVersion = () => pending.promise
        const writing = m.state.createNextWorkflowProjectorDraft(version)
        await settle()
        m.state.tab.value = 'prompts'; await settle() // external scope change, not guarded user navigation
        pending.resolve({ ...version, id: 'v2', version: 2 }); await writing; await settle()
        assert.equal(m.state.workflowProjectorEditorOpen.value, false)
        assert.deepEqual(m.state.workflowProjectorVersions.value, [])
        assert.equal(m.state.notice.value, '')
      } finally { m.app.unmount() }
    })
    await t.test('recipe successor acknowledges creation separately from failed catalog reads and retries only reads', async () => {
      for (const failedRead of ['recipes', 'options']) {
        reset(); let writes = 0
        const source = { id: 'recipe-v1', recipeKey: 'family', version: 1, status: 'published', definitionSha256: 'digest', definition: {} }
        const draft = { ...source, id: 'recipe-v2', version: 2, status: 'draft' }
        api.listRecipes = async () => [source]
        api.cloneRecipeSuccessor = async (...args: unknown[]) => {
          assert.deepEqual(args, [source.id, source.definitionSha256, 'settings.clone_successor'])
          writes++; return draft
        }
        const m = await mount('routing')
        try {
          if (failedRead === 'recipes') api.listRecipes = async () => { throw Error('recipe catalog unavailable') }
          if (failedRead === 'options') api.getRecipeEditorOptions = async () => { throw Error('recipe options unavailable') }
          await m.state.createRecipeSuccessor(source)
          assert.equal(m.state.editorOpen.value, false, failedRead)
          assert.match(m.state.registryWriteReceipt.value?.message || '', /family v2.*recipe-v2/)
          assert.equal(m.state.registryWriteRefreshFailed.value, true)
          await m.state.createRecipeSuccessor(source)
          assert.equal(writes, 1)
          api.listRecipes = async () => [source, draft]
          api.getRecipeEditorOptions = async () => ({})
          await m.state.load()
          assert.equal(m.state.registryWriteRefreshFailed.value, false)
          assert.equal(writes, 1)
          assert.equal(m.state.editorOpen.value, false)
          m.state.openEditor('recipe', m.state.recipes.value.find((item: any) => item.id === draft.id))
          assert.equal(m.state.editorId.value, draft.id)
        } finally { m.app.unmount() }
      }
    })
    await t.test('recipe successor blocks duplicate submits and discards late acknowledgements or reads outside their scope', async () => {
      for (const phase of ['write', 'read']) for (const change of ['tab', 'account', 'unmount']) {
        reset(); const pending = deferred(); let writes = 0
        const source = { id: 'recipe-v1', recipeKey: 'family', version: 1, status: 'published', definitionSha256: 'digest', definition: {} }
        const draft = { ...source, id: 'recipe-v2', version: 2, status: 'draft' }
        api.listRecipes = async () => [source]
        const m = await mount('routing')
        try {
          api.cloneRecipeSuccessor = async () => { writes++; return phase === 'write' ? pending.promise : draft }
          if (phase === 'read') api.listRecipes = () => pending.promise
          const writing = m.state.createRecipeSuccessor(source); await settle()
          await m.state.createRecipeSuccessor(source)
          assert.equal(writes, 1)
          if (change === 'tab') await m.router.push('/settings?section=prompts')
          if (change === 'account') { api.listRecipes = async () => []; m.auth.user = { ...m.auth.user, id: 'another' } }
          if (change === 'unmount') m.app.unmount()
          await settle(); pending.resolve(phase === 'write' ? draft : [source, draft]); await writing; await settle()
          assert.equal(m.state.editorOpen.value, false, `${phase}/${change}`)
          assert.equal(m.state.notice.value, '')
          if (change !== 'unmount') assert.equal(m.state.registryWriteReceipt.value, null)
        } finally { if (change !== 'unmount') m.app.unmount() }
      }
    })
    await t.test('recipe successor opens only the acknowledged draft from a healthy catalog and keeps rejected writes distinct', async () => {
      for (const result of ['success', 'reject', 'missing']) {
        reset()
        const source = { id: 'recipe-v1', recipeKey: 'family', version: 1, status: 'published', definitionSha256: 'digest', definition: {} }
        const draft = { ...source, id: 'recipe-v2', version: 2, status: 'draft' }
        api.listRecipes = async () => [source]
        api.cloneRecipeSuccessor = async () => {
          if (result === 'reject') throw Error('clone rejected')
          api.listRecipes = async () => result === 'missing' ? [source] : [source, draft]
          return draft
        }
        const m = await mount('routing')
        try {
          await m.state.createRecipeSuccessor(source)
          assert.equal(m.state.editorOpen.value, result === 'success')
          if (result === 'success') assert.equal(m.state.editorId.value, draft.id)
          if (result === 'reject') { assert.match(m.state.error.value, /clone rejected/); assert.equal(m.state.registryWriteReceipt.value, null) }
          if (result === 'missing') { assert.match(m.state.error.value, /未包含.*recipe-v2/); assert.match(m.state.registryWriteReceipt.value?.message || '', /recipe-v2/) }
        } finally { m.app.unmount() }
      }
    })
    await t.test('recipe author uses original create and minimal PATCH, and blocks published editing', async () => {
      reset(); const writes:any[]=[]
      const draft={id:'draft',recipeKey:'family',version:2,name:'原名',status:'draft',definition:{schema_version:3,stages:[]}}
      api.getRecipeEditorOptions=async()=>({definitionSchemaVersion:3})
      api.createRecipe=async (body:any,reason:string)=>{writes.push(['create',body,reason]);return {...draft,name:body.name}}
      api.updateRecipe=async(id:string,body:any,reason:string)=>{writes.push(['update',id,body,reason]);return {...draft,name:body.name}}
      const m=await mount('routing')
      try {
        m.state.openEditor('recipe',{...draft,status:'published'});assert.equal(m.state.editorOpen.value,false)
        m.state.openEditor('recipe');m.state.editorPayload.value={recipe_key:'family',name:' 中文 🌙 ',definition:{schema_version:3,stages:[]}}
        m.state.editorValidity.value={valid:true,errors:[],warnings:[]};await m.state.submitEditor()
        assert.deepEqual(writes[0],['create',{recipe_key:'family',name:' 中文 🌙 ',definition:{schema_version:3,stages:[]}},'settings.create'])
        m.state.openEditor('recipe',draft);m.state.editorPayload.value.name='后继新名';m.state.editorValidity.value={valid:true,errors:[],warnings:[]}
        await m.state.submitEditor();assert.deepEqual(writes[1],['update','draft',{name:'后继新名'},'settings.update'])
        assert.equal(draft.name,'原名');assert.equal(m.state.editorOpen.value,false)
      } finally {m.app.unmount()}
    })
    await t.test('recipe explicit field rejection retains input and locates fields without an unknown lock', async()=>{
      reset();api.getRecipeEditorOptions=async()=>({definitionSchemaVersion:3})
      api.createRecipe=async()=>{throw {response:{status:400,data:{name:['名称明确拒绝']}}}}
      const m=await mount('routing')
      try {
        m.state.openEditor('recipe');m.state.editorPayload.value={recipe_key:'family',name:'留住原文🌙',definition:{schema_version:3,stages:[]}}
        m.state.editorValidity.value={valid:true,errors:[],warnings:[]};await m.state.submitEditor()
        assert.equal(m.state.editorPayload.value.name,'留住原文🌙');assert.equal(m.state.recipeWriteUnknown.value,null)
        assert.deepEqual(m.state.recipeEditorIssues.value,[{field:'name',message:'名称明确拒绝',section:'identity'}])
      }finally{m.app.unmount()}
    })
    await t.test('recipe unknown create cannot resend or guess latest and loads only explicitly inspected draft', async()=>{
      reset();let writes=0,reads=0
      api.getRecipeEditorOptions=async()=>({definitionSchemaVersion:3})
      const draft={id:'draft-v2',recipeKey:'family',version:2,name:'原草稿',status:'draft',definition:{schema_version:3,stages:[]}}
      api.createRecipe=async()=>{writes++;throw Error('response lost')}
      api.listRecipes=async()=>[draft,{...draft,id:'latest',version:4,status:'published'},{...draft,id:'unrelated',recipeKey:'other'}]
      api.getRecipe=async(id:string)=>{reads++;return {...draft,id}}
      const m=await mount('routing')
      try {
        m.state.openEditor('recipe');m.state.editorPayload.value={recipe_key:'family',name:'未确认',definition:{schema_version:3,stages:[]}}
        m.state.editorValidity.value={valid:true,errors:[],warnings:[]};await m.state.submitEditor();await m.state.submitEditor()
        assert.equal(writes,1);assert.equal(m.state.recipeWriteUnknown.value.kind,'create')
        await m.state.readRecipeWriteResult();assert.deepEqual(m.state.recipeRecoveryRecords.value.map((r:any)=>r.id),['draft-v2','latest'])
        assert.equal(m.state.recipeRecoverySelected.value,null);await m.state.loadRecoveredRecipe();assert.equal(reads,0)
        await m.state.readRecipeWriteResult(draft);await m.state.loadRecoveredRecipe()
        assert.equal(reads,2);assert.equal(writes,1);assert.equal(m.state.recipeWriteUnknown.value,null)
        assert.equal(m.state.editorId.value,'draft-v2');assert.equal(m.state.editorPayload.value.name,'原草稿');assert.equal(m.state.recipeEditorDirty.value,false)
      }finally{m.app.unmount()}
    })
    await t.test('recipe unknown update reads exact id; wrong identity and fresh non-draft are never loaded',async()=>{
      reset();let calls=0
      api.getRecipeEditorOptions=async()=>({definitionSchemaVersion:3})
      const draft={id:'exact',recipeKey:'family',version:2,name:'原名',status:'draft',definition:{schema_version:3,stages:[]}}
      api.updateRecipe=async()=>{calls++;throw {response:{status:409}}}
      const m=await mount('routing')
      try {
        m.state.openEditor('recipe',draft);m.state.editorPayload.value.name='修改';m.state.editorValidity.value={valid:true,errors:[],warnings:[]};await m.state.submitEditor()
        api.getRecipe=async(id:string)=>{assert.equal(id,'exact');return {...draft,id:'wrong'}}
        await m.state.readRecipeWriteResult();assert.match(m.state.recipeRecoveryError.value,/身份/);assert.equal(m.state.recipeRecoveryRecords.value.length,0)
        api.getRecipe=async()=>draft;await m.state.readRecipeWriteResult(draft)
        api.getRecipe=async()=>({...draft,status:'published'});await m.state.loadRecoveredRecipe()
        assert.ok(m.state.recipeWriteUnknown.value);assert.equal(m.state.editorPayload.value.name,'修改');assert.equal(calls,1)
      }finally{m.app.unmount()}
    })
    await t.test('recipe close and navigation preserve dirty fields until explicit discard',async()=>{
      reset();api.getRecipeEditorOptions=async()=>({definitionSchemaVersion:3})
      const m=await mount('routing')
      try {
        m.state.openEditor('recipe');m.state.editorPayload.value.name='未保存'
        m.state.closeRegistryEditor();assert.equal(m.state.recipeDiscard.value,true);assert.equal(m.state.editorOpen.value,true)
        m.state.keepRecipeEditor();assert.equal(m.state.editorPayload.value.name,'未保存')
        await m.router.push('/settings?section=prompts');assert.equal(m.router.currentRoute.value.query.section,'routing');assert.equal(m.state.recipeDiscard.value,true)
        m.state.discardRecipeEditor();await settle();assert.equal(m.router.currentRoute.value.query.section,'prompts');assert.equal(m.state.editorOpen.value,false)
      }finally{m.app.unmount()}
    })
    await t.test('recipe late recovery cannot overwrite a changed account and can recover after discarded tab navigation',async()=>{
      for(const change of ['account','tab']){
        reset();const pending=deferred();api.getRecipeEditorOptions=async()=>({definitionSchemaVersion:3})
        const m=await mount('routing')
        try{
          m.state.markRecipeUnknown({kind:'update',key:'family',id:'exact',sourceId:''})
          api.getRecipe=()=>pending.promise;const reading=m.state.readRecipeWriteResult();await settle()
          if(change==='account')m.auth.user={...m.auth.user,id:'changed'}
          else m.state.tab.value='prompts'
          await settle();pending.resolve({id:'exact',recipeKey:'family',status:'draft'});await reading
          assert.equal(m.state.recipeRecoveryBusy.value,false);assert.equal(m.state.recipeRecoverySelected.value,null);assert.equal(m.state.recipeRecoveryRecords.value.length,0)
        }finally{m.app.unmount()}
      }
    })
    await t.test('recipe clone unknown excludes source and never creates a second successor',async()=>{
      reset();let writes=0;const source={id:'source',recipeKey:'family',version:1,status:'published',definitionSha256:'exact-digest',definition:{}}
      const child={...source,id:'child',version:2,status:'draft'}
      api.listRecipes=async()=>[source,child]
      api.cloneRecipeSuccessor=async(...args:any[])=>{assert.deepEqual(args,['source','exact-digest','settings.clone_successor']);writes++;throw Error('unknown')}
      const m=await mount('routing')
      try{
        await m.state.createRecipeSuccessor(source);await m.state.createRecipeSuccessor(source);await m.state.readRecipeWriteResult()
        assert.equal(writes,1);assert.deepEqual(m.state.recipeRecoveryRecords.value.map((r:any)=>r.id),['child']);assert.equal(source.status,'published')
      }finally{m.app.unmount()}
    })
    await t.test('recipe refresh during a dispatched clone retains unknown intent and prevents a second POST',async()=>{
      reset();let writes=0;const pending=deferred(),source={id:'source',recipeKey:'family',status:'published',definitionSha256:'digest'}
      api.cloneRecipeSuccessor=()=>{writes++;return pending.promise}
      const m=await mount('routing')
      try{
        const writing=m.state.createRecipeSuccessor(source);await settle();await m.state.load();pending.reject(Error('response lost'));await writing
        assert.equal(m.state.recipeWriteUnknown.value.sourceId,'source');await m.state.createRecipeSuccessor(source);assert.equal(writes,1)
        await m.state.selectTab('prompts');await settle();assert.equal(m.state.tab.value,'routing');assert.equal(m.router.currentRoute.value.query.section,'routing')
        m.state.keepRecipeEditor();assert.equal(m.state.tab.value,'routing');assert.equal(m.state.recipeDiscard.value,false)
      }finally{m.app.unmount()}
    })
    await t.test('recipe child-local text is protected even when serialized definition has not changed',async()=>{
      reset();const m=await mount('routing')
      try{
        m.state.openEditor('recipe',{id:'draft',recipeKey:'family',status:'draft',definition:{}})
        assert.equal(m.state.recipeEditorDirty.value,false);m.state.recipePendingInput.value=true
        m.state.closeRegistryEditor();assert.equal(m.state.recipeDiscard.value,true);assert.equal(m.state.editorOpen.value,true)
        m.state.keepRecipeEditor();assert.equal(m.state.recipeHasUnsavedInput.value,true)
      }finally{m.app.unmount()}
    })
    await t.test('Projector first-load failure is unknown, and scope changes reject old and late catalogs', async () => {
      for (const change of ['account', 'privilege', 'context', 'tab', 'unmount']) {
        reset()
        api.listWorkflowProjectors = async () => [{ id: 'old' }]
        api.listWorkflowProjectorVersions = async () => [{ id: 'old-version' }]
        api.getWorkflowProjectorOptions = async () => ({ engines: [] })
        const m = await mount('workflows')
        try {
          const pending = deferred()
          api.listWorkflowProjectors = () => pending.promise
          const reading = m.state.load(); await settle()
          api.listWorkflowProjectors = async () => { throw Error('new scope failed') }
          if (change === 'account') m.auth.user = { ...m.auth.user, id: 'new' }
          if (change === 'privilege') m.auth.user = { ...m.auth.user, isSuperuser: false }
          if (change === 'context') await m.router.push('/settings?section=workflows&model_profile_version=11111111-1111-4111-8111-111111111111')
          if (change === 'tab') await m.router.push('/settings?section=prompts')
          if (change === 'unmount') m.app.unmount()
          await settle(); pending.resolve([{ id: 'late' }]); await reading; await settle()
          assert.ok(!m.state.workflowProjectors.value.some((r: any) => r.id === 'late'), change)
          if (change !== 'unmount') {
            assert.deepEqual(m.state.workflowProjectors.value, [], change)
            assert.equal(m.state.workflowProjectorCatalogHasResult.value, false, change)
            assert.ok(m.state.workflowProjectorCreateBlockedReason.value, change)
          }
        } finally { if (change !== 'unmount') m.app.unmount() }
      }
      reset(); api.listWorkflowProjectorVersions = async () => { throw Error('version read failed') }
      const m = await mount('workflows')
      try {
        assert.equal(m.state.workflowProjectorCatalogState.value, 'error')
        assert.equal(m.state.workflowProjectorCatalogHasResult.value, false)
        assert.ok(m.state.workflowProjectorCreateBlockedReason.value)
      } finally { m.app.unmount() }
    })
    await t.test('resource Smoke discard requires an explicit local decision and does not submit or cancel a task', async () => {
      reset(); const m = await mount('models')
      try {
        const s = m.state
        s.openResourceSmoke('deployment-one'); s.closeResourceSmoke()
        assert.equal(s.resourceSmokeTarget.value, null, 'clean forms can close without a discard prompt')
        s.openResourceSmoke('deployment-one'); s.resourceSmokeDirty.value = true
        s.closeResourceSmoke(); const pending = s.resourceSmokeDiscard.value
        assert.equal(pending.target.id, 'deployment-one'); assert.equal(s.resourceSmokeTarget.value.id, 'deployment-one')
        s.closeResourceSmoke(); s.openResourceSmoke('deployment-two')
        assert.equal(s.resourceSmokeDiscard.value, pending, 'duplicate close cannot replace the original decision')
        assert.equal(s.resourceSmokeTarget.value.id, 'deployment-one')
        s.resourceSmokeDiscard.value = null // return / Escape only closes the confirmation
        assert.equal(s.resourceSmokeDirty.value, true); assert.equal(s.resourceSmokeTarget.value.id, 'deployment-one')
        s.resourceSmokeLocked.value = true; s.closeResourceSmoke(); s.openResourceSmoke('deployment-two')
        assert.equal(s.resourceSmokeDiscard.value, null); assert.equal(s.resourceSmokeTarget.value.id, 'deployment-one')
        s.resourceSmokeLocked.value = false
        s.closeResourceSmoke(); s.resourceSmokeBusy.value = true; s.confirmResourceSmokeDiscard()
        assert.ok(s.resourceSmokeDiscard.value); assert.equal(s.resourceSmokeTarget.value.id, 'deployment-one')
        s.resourceSmokeBusy.value = false; s.confirmResourceSmokeDiscard(); s.confirmResourceSmokeDiscard()
        assert.equal(s.resourceSmokeDiscard.value, null); assert.equal(s.resourceSmokeTarget.value, null); assert.equal(s.resourceSmokeDirty.value, false)
      } finally { m.app.unmount() }
    })
    await t.test('resource Smoke discard cannot close a different target, account, section, or disposed page', async () => {
      for (const change of ['target', 'account', 'section', 'unmount']) {
        reset(); const m = await mount('models'); const s = m.state
        try {
          s.openResourceSmoke('deployment-one'); s.resourceSmokeDirty.value = true; s.closeResourceSmoke()
          if (change === 'target') s.resourceSmokeTarget.value = {kind:'deployment',id:'deployment-two'}
          if (change === 'account') m.auth.user = {...m.auth.user,id:'another-operator'}
          if (change === 'section') s.tab.value = 'workflows'
          if (change === 'unmount') m.app.unmount()
          s.confirmResourceSmokeDiscard()
          assert.ok(s.resourceSmokeTarget.value, change); assert.equal(s.resourceSmokeDirty.value, true, change)
          assert.equal(s.resourceSmokeDiscard.value, null)
        } finally { if (change !== 'unmount') m.app.unmount() }
      }
    })
    await t.test('Projector draft close is explicit, cancel preserves edits and duplicate or busy actions cannot replace it', async () => {
      reset(); const m = await mount('workflows'); const s = m.state
      try {
        s.workflowProjectorCreateOpen.value = true
        await s.closeWorkflowProjectorCreate()
        assert.equal(s.workflowProjectorCreateOpen.value, false, 'clean form closes directly')
        s.workflowProjectorCreateOpen.value = true
        s.workflowProjectorCreateFamily.name = 'Unsubmitted family'
        s.workflowProjectorCreateFamilyId.value = 'already-created-family'
        const closing = s.closeWorkflowProjectorCreate(), pending = s.workflowProjectorConfirmation.value
        assert.equal(pending.kind, 'close'); assert.equal(pending.targetLabel, 'Unsubmitted family')
        await s.closeWorkflowProjectorCreate(); await s.changeWorkflowProjectorCreateMode('blank')
        await s.submitWorkflowProjectorCreate(); s.openWorkflowProjectorCreate()
        assert.equal(s.workflowProjectorConfirmation.value, pending)
        s.settleWorkflowProjectorConfirmation(); await closing
        assert.equal(s.workflowProjectorCreateOpen.value, true)
        assert.equal(s.workflowProjectorCreateFamily.name, 'Unsubmitted family')
        assert.equal(s.workflowProjectorCreateFamilyId.value, 'already-created-family')
        const busyClose = s.closeWorkflowProjectorCreate()
        s.savedWorkflowBusy.value = 'operation'; s.settleWorkflowProjectorConfirmation(true); await busyClose
        assert.equal(s.workflowProjectorCreateOpen.value, true, 'busy state checked again after approval')
        s.savedWorkflowBusy.value = ''
        const accepted = s.closeWorkflowProjectorCreate()
        s.settleWorkflowProjectorConfirmation(true); s.settleWorkflowProjectorConfirmation(true); await accepted
        assert.equal(s.workflowProjectorCreateOpen.value, false)
        assert.equal(s.workflowProjectorCreateFamily.name, '')
        assert.equal(s.workflowProjectorConfirmation.value, null)
      } finally { m.app.unmount() }
    })
    await t.test('Projector confirmations are invalidated by scope, draft replacement and disposal without hanging', async () => {
      for (const change of ['account', 'privilege', 'section', 'context', 'draft', 'unmount']) {
        reset(); const m = await mount('workflows'); const s = m.state
        try {
          s.workflowProjectorCreateOpen.value = true; s.workflowProjectorCreateFamily.name = 'Keep'
          const closing = s.closeWorkflowProjectorCreate()
          if (change === 'account') m.auth.user = {...m.auth.user,id:'another'}
          if (change === 'privilege') m.auth.user = {...m.auth.user,isSuperuser:false}
          if (change === 'section') s.tab.value = 'prompts'
          if (change === 'context') m.router.currentRoute.value = {...m.router.currentRoute.value,query:{section:'workflows',model_profile_version:'invalid'}}
          if (change === 'draft') s.workflowProjectorCreateFamily.name = 'Replacement'
          if (change === 'unmount') m.app.unmount()
          await settle()
          assert.equal(s.workflowProjectorConfirmation.value, null, change)
          await closing
          s.settleWorkflowProjectorConfirmation(true)
          assert.equal(s.workflowProjectorCreateOpen.value, true, change)
          assert.equal(s.workflowProjectorCreateFamily.name, change === 'draft' ? 'Replacement' : 'Keep', change)
        } finally { if (change !== 'unmount') m.app.unmount() }
      }
    })
    await t.test('Projector select changes keep the displayed value until approval and never reuse a replaced catalog', async () => {
      reset(); const m = await mount('workflows'); const s = m.state
      try {
        const engine = {key:'generic.fixed',version:1,label:'Fixed',creationTemplate:{providerDriverType:'comfyui',operation:'workflow.import',outputKind:'image',definition:{priority:0}}}
        s.workflowProjectorOptions.value = {engines:[engine]}
        await s.changeWorkflowProjectorCreateMode('blank')
        await s.initializeBlankWorkflowProjector({target:{value:'generic.fixed\u00001'}})
        s.workflowProjectorCreateDefinition.value.priority = 9
        const select = {value:'generic.fixed\u00001'}
        const cancelled = s.initializeBlankWorkflowProjector({target:select})
        assert.equal(s.workflowProjectorCreateDefinition.value.priority, 9)
        s.settleWorkflowProjectorConfirmation(); await cancelled
        assert.equal(s.workflowProjectorCreateDefinition.value.priority, 9)
        const replaced = s.initializeBlankWorkflowProjector({target:select})
        s.workflowProjectorOptions.value = {engines:[]}
        s.settleWorkflowProjectorConfirmation(true); await replaced
        assert.equal(s.workflowProjectorCreateDefinition.value.priority, 9)
        const seed = {id:'copy-v1',projectorKey:'copy',version:1,engineKey:'generic.fixed',engineVersion:1,definition:{priority:3}}
        s.workflowProjectorVersions.value = [seed]
        const seedSelect = {value:'copy-v1'}
        const copy = s.changeWorkflowProjectorCreateSeed({target:seedSelect})
        assert.equal(seedSelect.value, '', 'pending selection must not appear applied')
        assert.equal(s.workflowProjectorCreateDefinition.value.priority, 9)
        s.settleWorkflowProjectorConfirmation(true); await copy
        assert.equal(s.workflowProjectorCreateSeedVersionId.value, 'copy-v1')
        assert.equal(s.workflowProjectorCreateDefinition.value.priority, 3)
        assert.equal(seed.definition.priority, 3)
        s.workflowProjectorCreateDefinitionValidity.value.errors = ['高级 JSON 尚未应用']
        s.workflowProjectorCreateDefinitionValidity.value.pendingEdits = true
        const invalidJson = s.changeWorkflowProjectorCreateMode('recommendation')
        assert.equal(s.workflowProjectorConfirmation.value.kind, 'reinitialize')
        s.settleWorkflowProjectorConfirmation(); await invalidJson
        assert.deepEqual(s.workflowProjectorCreateDefinitionValidity.value.errors, ['高级 JSON 尚未应用'])
      } finally { m.app.unmount() }
    })
    await t.test('existing Projector editor close protects saved differences and local-only drafts without API writes', async () => {
      reset()
      const version = { id:'edit-v1',projectorKey:'edit-family',version:1,status:'draft',engineKey:'engine',engineVersion:1,providerDriverType:'comfyui',operation:'workflow.import',outputKind:'image',definition:{priority:1} }
      const m=await mount('workflows'),s=m.state
      try {
        api.updateWorkflowProjectorVersion=async()=>assert.fail('discard cannot save')
        s.openWorkflowProjectorEditor(version);s.closeWorkflowProjectorEditor()
        assert.equal(s.workflowProjectorEditorOpen.value,false,'clean editor closes directly')
        s.openWorkflowProjectorEditor(version);s.workflowProjectorDefinition.value={priority:7};s.closeWorkflowProjectorEditor()
        const pending=s.workflowProjectorEditorDiscard.value
        assert.equal(pending.targetLabel,'edit-family v1');assert.equal(s.workflowProjectorEditorOpen.value,true)
        s.closeWorkflowProjectorEditor();assert.equal(s.workflowProjectorEditorDiscard.value,pending)
        await s.saveWorkflowProjectorDraft();s.workflowProjectorEditorDiscard.value=null
        assert.deepEqual(s.workflowProjectorDefinition.value,{priority:7})
        s.closeWorkflowProjectorEditor();s.confirmWorkflowProjectorEditorDiscard()
        assert.equal(s.workflowProjectorEditorOpen.value,false);assert.deepEqual(version.definition,{priority:1})
        s.openWorkflowProjectorEditor(version);s.workflowProjectorDefinitionValidity.value={valid:false,errors:['unfinished'],pendingEdits:true}
        s.closeWorkflowProjectorEditor();assert.ok(s.workflowProjectorEditorDiscard.value,'local input unchanged in config still protected')
        s.workflowProjectorEditorDiscard.value=null;s.workflowProjectorDefinitionValidity.value={valid:false,errors:['stored invalid definition'],pendingEdits:false}
        s.closeWorkflowProjectorEditor();assert.equal(s.workflowProjectorEditorOpen.value,false,'existing validation error alone is not an edit')
        s.openWorkflowProjectorEditor({...version,status:'published'},true);s.closeWorkflowProjectorEditor();assert.equal(s.workflowProjectorEditorOpen.value,false)
      } finally {m.app.unmount()}
    })
    await t.test('existing Projector discard cannot close a changed version, stale scope, busy or unmounted editor', async () => {
      reset();const version={id:'v1',projectorKey:'family',version:1,status:'draft',engineKey:'engine',engineVersion:1,providerDriverType:'comfyui',operation:'workflow.import',outputKind:'image',definition:{priority:1}}
      const m=await mount('workflows'),s=m.state
      const pending=()=>{s.openWorkflowProjectorEditor(version);s.workflowProjectorDefinition.value={priority:2};s.closeWorkflowProjectorEditor();assert.ok(s.workflowProjectorEditorDiscard.value)}
      let unmounted=false
      try {
        pending();s.openWorkflowProjectorEditor({...version,id:'v2',version:2});s.confirmWorkflowProjectorEditorDiscard();assert.equal(s.workflowProjectorEditorVersion.value.id,'v2')
        pending();s.workflowProjectorDefinition.value={priority:3};s.confirmWorkflowProjectorEditorDiscard();assert.equal(s.workflowProjectorEditorOpen.value,true)
        pending();s.workflowProjectorBusy.value='saving';s.confirmWorkflowProjectorEditorDiscard();s.closeWorkflowProjectorEditor();assert.equal(s.workflowProjectorEditorOpen.value,true);s.workflowProjectorBusy.value=''
        pending();m.auth.user.id='other';s.confirmWorkflowProjectorEditorDiscard();assert.equal(s.workflowProjectorEditorOpen.value,true)
        await settle();pending();m.app.unmount();unmounted=true;s.confirmWorkflowProjectorEditorDiscard();assert.equal(s.workflowProjectorEditorOpen.value,true)
      } finally { if(!unmounted)m.app.unmount() }
    })
    await t.test('UI exposes pending evidence and operation retry rather than a success fallback', async () => {
      const source = await readFile(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
      assert.match(source, /:acceptance-loading="deploymentAcceptanceLoading"/)
      assert.match(source, /v-else-if="productionReadinessLoading"/)
      assert.match(source, /重新读取模型选项/)
      assert.match(source, /重试读取审计限制/)
      assert.match(source, /重试读取验收数据/)
      assert.match(source, /目录读取失败，当前保留同一账号及上下文的上次完整结果，尚未更新/)
      assert.match(source, /workflowProjectorCatalogState==='ready'&&!workflowProjectors.length/)
      const close = source.slice(source.indexOf('const closeResourceSmoke'), source.indexOf('const smokeResolutionOutcome'))
      assert.doesNotMatch(close, /window\.confirm|platformApi\./)
      assert.match(source, /title="放弃未提交的烟测表单？"/)
      assert.match(source, /返回继续填写/)
      assert.match(source, /不取消任务/)
      const projectorCreate = source.slice(source.indexOf('const currentWorkflowProjectorCreateSeedFingerprint'), source.indexOf('const submitWorkflowProjectorCreate'))
      assert.doesNotMatch(projectorCreate, /window\.confirm|platformApi\./)
      assert.match(source, /重新初始化当前方案？/)
      assert.match(source, /不删除已创建版本/)
    })
  } finally {
    Object.assign(api, originals); repository.getProductionCapability = originalCapability
    await vite.close(); globalThis.localStorage = previousStorage; globalThis.window = previousWindow
  }
})
