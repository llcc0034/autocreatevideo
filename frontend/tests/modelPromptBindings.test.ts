import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { createServer } from 'vite'

const createVite = () => createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})

test('model entry points preserve exact context and use the existing Binding management surface', async () => {
  const settings = await readFile(new URL('../src/views/SettingsView.vue',import.meta.url),'utf8')
  const manager = await readFile(new URL('../src/components/PromptProfileSettings.vue',import.meta.url),'utf8')
  const detail = await readFile(new URL('../src/components/ModelPromptBindings.vue',import.meta.url),'utf8')
  assert.match(settings, /modelPromptContext=\{profile:group.profile,deployment:item\}/)
  assert.match(settings, /route.query.model_profile_version/)
  assert.match(detail, /promptBindingsForModel\(bindings.value, props.profile\)/)
  assert.match(manager, /existing\?\.modelProfileVersion \|\| modelContext.value\?\.id/)
  assert.match(manager, /绑定必须使用当前页面的精确模型版本/)
  assert.match(manager, /服务端绑定目录未返回当前模型版本/)
  assert.match(manager, /model_profile_version:modelProfileVersionId/)
  assert.match(manager, /查看全部提示词策略/)
  assert.match(detail, /serial === requestSerial/)
  assert.match(detail, /onBeforeUnmount/)
})

test('model association includes exact and family bindings without selecting one or leaking other exact models', async (context) => {
  const vite = await createVite()
  context.after(() => vite.close())
  const {promptBindingsForModel, modelPromptSettingsRoute} = await vite.ssrLoadModule('/src/services/modelPromptBindings.ts')
  const profile = {id:'model-a',specification:{model_family:'family-a'}}
  const bindings = [
    {id:'family',key:'family',modelProfileVersion:'',modelFamily:'family-a',priority:100,isActive:true},
    {id:'exact',key:'exact',modelProfileVersion:'model-a',modelFamily:'family-a',priority:0,isActive:false},
    {id:'other-model',key:'other',modelProfileVersion:'model-b',modelFamily:'family-a',priority:200,isActive:true},
    {id:'other-family',key:'else',modelProfileVersion:'',modelFamily:'family-b',priority:200,isActive:true},
  ]
  assert.deepEqual(promptBindingsForModel(bindings, profile).map((row: {id:string})=>row.id), ['exact','family'])
  assert.deepEqual(promptBindingsForModel(bindings, {...profile,specification:{}}).map((row: {id:string})=>row.id), ['exact'])
  assert.equal(bindings[0].id, 'family')
  assert.deepEqual(modelPromptSettingsRoute('model-a','prompt-a','deployment-a').query, {
    section:'prompts',model_profile_version:'model-a',prompt_profile_version:'prompt-a',deployment:'deployment-a',
  })
})

test('scoped binding API follows pagination and preserves strategy identity', async (context) => {
  const vite = await createVite()
  context.after(() => vite.close())
  const {platformApi} = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const {api} = await vite.ssrLoadModule('/src/services/api.ts')
  const original = api.get
  context.after(() => {api.get=original})
  const calls: unknown[] = []
  api.get = async (url: string, options?: {params:unknown}) => {
    calls.push({url,params:options?.params})
    return {data:{results:[{
      id:url.includes('page=2')?'second':'first', prompt_profile_name:'策略 A',
      prompt_profile_version_number:3,prompt_profile_status:'published',
      prompt_profile_version:'profile-v3', prompt_profile_contract_sha256:'abc',
    }],next:url.includes('page=2')?null:'/prompt-profile-bindings/?model_family=family-a&page=2'}}
  }
  const rows = await platformApi.listPromptProfileBindings({model_family:'family-a'})
  assert.deepEqual(rows.map((row:{id:string})=>row.id),['first','second'])
  assert.equal(rows[0].promptProfileName,'策略 A')
  assert.equal(rows[0].promptProfileVersionNumber,3)
  assert.equal(rows[0].promptProfileStatus,'published')
  assert.deepEqual(calls,[
    {url:'/prompt-profile-bindings/',params:{model_family:'family-a',page_size:50}},
    {url:'/prompt-profile-bindings/?model_family=family-a&page=2',params:undefined},
  ])
})
