import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { readFileSync } from 'node:fs'

const setup = async (Component: any, props: any) => {
  let state: any
  await renderToString(createSSRApp({setup() {state=Component.setup(props,{expose(){},emit(){}});return()=>h('div')}}))
  return state
}
const server = () => createServer({logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false},optimizeDeps:{noDiscovery:true}})

test('reference kinds share the existing relation page and exact maintenance routes', async context => {
  const vite=await server();context.after(()=>vite.close())
  const {adaptRelationPage,relationOverviewUrl,relationConfigUrl}=await vite.ssrLoadModule('/src/services/registryRelations.ts')
  for(const kind of ['field','field_version','structure','source']) {
    const row={id:'exact',kind,label:kind,status:'retired',groups:[],notices:[],capabilities:[],acceptance_url:'',config_url:'/settings?section=fields',overview_url:relationOverviewUrl(kind,'exact')}
    assert.equal(adaptRelationPage({count:1,results:[row],next:null,previous:null,generated_at:'now',context_label:''}).results[0].kind,kind)
    assert.ok(relationConfigUrl(row).includes(`relation_context=${kind}%3Aexact`))
  }
  const view=readFileSync(new URL('../src/views/SettingsView.vue',import.meta.url),'utf8')
  for(const name of ['field_version','structure_version','source_version']) assert.ok(view.includes(`route.query.${name}`))
  const center=readFileSync(new URL('../src/components/FieldMaintenanceSettings.vue',import.meta.url),'utf8')
  assert.doesNotMatch(center,/previewRegistryDeletion|impact\.dependencies/)
  assert.match(center,/RegistryRelationSummary/)
})

test('definition deep link reads the exact off-list version and rejects a different field', async context => {
  const vite=await server();context.after(()=>vite.close())
  const {default:Component}=await vite.ssrLoadModule('/src/components/FieldDefinitionVersions.vue')
  const {fieldDefinitionVersions:api}=await vite.ssrLoadModule('/src/services/fieldDefinitionVersions.ts')
  const row={id:'v-old',field_definition:'field',version:1,status:'retired',label:'固定版本',category:'test',value_type:'text',unit:'',description:'',impact:'',value_options:[]}
  const reads:string[]=[]
  api.options=async()=>({statuses:[],value_types:[]});api.list=async()=>[]
  api.get=async(id:string)=>{reads.push(id);return row}
  api.update=api.create=api.transition=async()=>assert.fail('navigation cannot write')
  const state=await setup(Component,{fieldDefinition:'field',initialId:'v-old'})
  await state.reload()
  assert.equal(state.selected.value.id,'v-old');assert.equal(state.readonly.value,true)
  assert.ok(reads.every(id=>id==='v-old'));assert.equal(state.rows.value[0].id,'v-old')
  api.get=async()=>({...row,field_definition:'other'})
  await state.open('wrong');assert.match(state.error.value,/不属于当前字段/)
  assert.equal(state.editing.value,false)
})

test('structure and source navigation load exact versions outside pagination without any writes', async context => {
  const vite=await server();context.after(()=>vite.close())
  const {default:Structure}=await vite.ssrLoadModule('/src/components/DataStructureManager.vue')
  const {dataStructures:api}=await vite.ssrLoadModule('/src/services/dataStructures.ts')
  const reads:string[]=[]
  api.options=async()=>({node_defaults:{},statuses:[{value:'retired'}]});api.list=async()=>({count:0,results:[]})
  api.get=async(id:string)=>{reads.push(id);return{id,version:9,status:'retired',name:'结构',description:'',nodes:[]}}
  api.create=api.update=api.transition=async()=>assert.fail('reference navigation cannot write')
  const structure=await setup(Structure,{initialId:'structure-v9'})
  await new Promise(resolve=>setTimeout(resolve,0))
  assert.equal(structure.selected.value.id,'structure-v9');assert.equal(structure.readonly.value,true)
  assert.deepEqual(reads,['structure-v9'])
  const {default:Source}=await vite.ssrLoadModule('/src/components/ContentSourceManager.vue')
  const {contentSources:sources}=await vite.ssrLoadModule('/src/services/contentSources.ts')
  sources.list=async()=>({count:0,results:[]});sources.readers=async()=>[]
  sources.get=async(id:string)=>({id,version:3,status:'retired',name:'来源',reader_selector:'request.text',data_structure_version:'structure-v9'})
  sources.save=sources.action=async()=>assert.fail('reference navigation cannot write')
  const source=await setup(Source,{initialId:'source-v3'})
  await source.initialize()
  assert.equal(source.selected.value.id,'source-v3');assert.equal(source.form.data_structure_version,'structure-v9')
  sources.get=async()=>{throw Error('确切版本不存在')}
  await source.initialize();assert.match(source.error.value,/确切版本不存在/);assert.equal(source.selected.value,null)
})
