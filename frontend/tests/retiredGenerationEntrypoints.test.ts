import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

test('asset operations guide uses the single execution-instance entry, not retired direct-target requests', () => {
  const guide = readFileSync(new URL('../../docs/asset-generation-model-configuration.md', import.meta.url), 'utf8')
  const operations = readFileSync(new URL('../../docs/operations.md', import.meta.url), 'utf8')
  for (const source of [guide, operations]) {
    assert.match(source, /执行实例/)
    assert.match(source, /Preparation/)
    assert.doesNotMatch(source, /target_kind=deployment\|comfy_workflow|workflow_inputs\s*=|workflow_inputs\.(assets|references)/)
    assert.doesNotMatch(source, /二选一提交.*model_deployment_id|模型生成页面展示两类方案/)
  }
  assert.match(guide, /目录中可选.*实例已发布.*组合烟测通过.*本次输入可以执行/)
  assert.match(guide, /真实模型.*用户完成/)
  assert.match(guide, /人工完整通道/)
})

test('project detail and shot save use formal facts without loading or resurrecting historical prompts', async () => {
  const root=fileURLToPath(new URL('..', import.meta.url))
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try {
    const {api}=await vite.ssrLoadModule('/src/services/api.ts')
    const {adaptProject,syncShot}=await vite.ssrLoadModule('/src/services/backendAdapters.ts')
    const shotId='11111111-1111-4111-8111-111111111111'
    const episodeId='22222222-2222-4222-8222-222222222222'
    const rawShot={id:shotId,episode:episodeId,number:1,title:'分镜事实',media_type:'video',duration_ms:5000,
      episode_start_ms:45000,episode_end_ms:50000,include_audio:false,conditioning_mode:'fixture.mode',
      narrative:'A subject walks.',detailed_description:'Reviewed direction.',dialogue:[],character_relations:[],props:[],metadata:{prompt_field_version:2}}
    const rows:Record<string,unknown[]>={
      '/episodes/':[{id:episodeId,number:1,title:'测试集',status:'draft',prompt_settings:{include_audio:false}}],
      '/characters/':[], '/scenes/':[], '/props/':[], '/shots/':[rawShot],
    }
    const requested:string[]=[]
    const originalGet=api.get,originalPatch=api.patch
    try {
      api.get=async (url:string) => {
        requested.push(url)
        assert.ok(url in rows,`Unexpected endpoint: ${url}`)
        return {data:{results:rows[url],next:null}}
      }
      const project=await adaptProject({id:'project-1',title:'Test',status:'draft',language:'zh',aspect_ratio:'9:16',width:720,height:1280})
      assert.deepEqual(requested.sort(),Object.keys(rows).sort())
      const episode=project.episodes[0],shot=episode.shots[0]
      assert.equal(shot.status,'draft')
      assert.equal('prompt' in shot,false)
      assert.equal('generatedPrompt' in shot,false)
      assert.equal(shot.episodeStartMs,45000)
      assert.equal(shot.duration,5)
      assert.equal('detailedDescription' in shot,false)
      assert.equal('narrative' in shot,false)
      // A stale object left in an open editor must not survive a formal reload/save.
      shot.generatedPrompt={prompt_text:'not this request'};shot.prompt='old text';shot.status='ready'
      api.patch=async (url:string,payload:Record<string,unknown>) => {
        assert.equal(url,`/shots/${shotId}/`)
        for(const key of ['generatedPrompt','prompt','promptRefreshReason','narrative','detailed_description','camera','action','sound_effects','music','continuity_notes']) assert.equal(key in payload,false)
        return {data:{...rawShot,narrative:'Saved authoritative narrative.',action:'Saved authoritative action.'}}
      }
      const saved=await syncShot(project,episode,shot)
      assert.equal('action' in saved,false)
      assert.equal('narrative' in saved,false)
      assert.equal(saved.status,'draft')
      assert.equal('prompt' in saved,false)
      assert.equal('generatedPrompt' in saved,false)
      api.get=async () => { throw new Error('formal facts unavailable') }
      await assert.rejects(adaptProject({id:'project-1',title:'Test',status:'draft',language:'zh',aspect_ratio:'9:16',width:720,height:1280}),/formal facts unavailable/)
    } finally { api.get=originalGet;api.patch=originalPatch }
    assert.ok((await vite.ssrLoadModule('/src/views/StoryboardView.vue')).default)
    const source=readFileSync(new URL('../src/views/StoryboardView.vue',import.meta.url),'utf8')
    assert.doesNotMatch(source,/generatedPromptsByShot|latestPrompt|promptArtifactNeedsRegeneration/)
    assert.doesNotMatch(source,/referenceBindings|referenceAssetIds|autoBind|pruneBindings|setShotMode|selectedModeProfile|referenceVideoAssets/)
    assert.match(source,/逐次素材引用入口/)
    assert.match(source,/进入生产工作台选材/)
    assert.match(source,/此处只编辑出镜人物、场景与道具关系/)
    assert.match(source,/任务中心 → 任务详情 → 输入与实际提示词/)
    assert.match(source,/其他任务的提示词不会套用到当前分镜/)
  } finally { await vite.close() }
})

test('task view renders actual task nodes without an implicit next-shot chain or history adapter', async () => {
  const root=fileURLToPath(new URL('..', import.meta.url))
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try {
    const {repository}=await vite.ssrLoadModule('/src/services/repository.ts')
    for (const method of ['getDeferredGenerationRequests','createDeferredGenerationRequest','cancelDeferredGenerationRequest']) assert.equal(method in repository,false)
    const storeSource=readFileSync(new URL('../src/stores/studio.ts',import.meta.url),'utf8')
    assert.doesNotMatch(storeSource,/retryAssetGenerationJob|已创建重试任务/)
    assert.equal(typeof repository.retryAssetGenerationJob,'function')
    const {default: component}=await vite.ssrLoadModule('/src/views/WorkflowView.vue')
    assert.ok(component)
    const source=readFileSync(new URL('../src/components/TaskMediaInspector.vue',import.meta.url),'utf8')
    assert.doesNotMatch(source,/deferredRequests|loadDeferredRequests|deferred-generation|自动连续生成请求/)
    assert.match(source,/selectedDisplayNodes=computed<WorkflowNode\[\]>\(\(\)=>selected.value\?\.nodes \|\| \[\]\)/)
    assert.match(source,/<TaskExecutionChecks/)
    assert.match(source,/实际执行准备/)
  } finally { await vite.close() }
})
