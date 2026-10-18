import test from 'node:test'
import assert from 'node:assert/strict'
import {createServer} from 'vite'

async function setup(t:any,enabled=true,withField=false,withNodes=false){
  const storage=new Map<string,string>(),opened:string[]=[],handlers:any[]=[]
  const url=new URL('http://fixture.test/tests/fixtures/prompt-authoring.html?matrix=1&fixture_session=only-this-test'+(enabled?'&maintenance=1':''))
  const globals:any={localStorage:{getItem:(key:string)=>storage.get(key)??null,setItem:(key:string,value:string)=>storage.set(key,value)},location:{href:url.href,search:url.search,origin:url.origin},document:{addEventListener:(_name:string,fn:any)=>handlers.push(fn)},window:{open:(url:URL)=>opened.push(String(url))}}
  function install(key:string,value:any){const previous=Object.getOwnPropertyDescriptor(globalThis,key);Object.defineProperty(globalThis,key,{configurable:true,value});t.after(()=>previous?Object.defineProperty(globalThis,key,previous):Reflect.deleteProperty(globalThis,key))}
  install('localStorage',globals.localStorage)
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',logLevel:'silent',optimizeDeps:{noDiscovery:true}});t.after(()=>vite.close())
  const {authorMaintenanceSession}=await vite.ssrLoadModule('/tests/fixtures/author-maintenance-session.ts')
  const {dataStructures:api}=await vite.ssrLoadModule('/src/services/dataStructures.ts')
  const {fieldDefinitionVersions:fieldApi}=await vite.ssrLoadModule('/src/services/fieldDefinitionVersions.ts')
  for(const [key,value] of Object.entries(globals))if(key!=='localStorage')install(key,value)
  const metrics={read:0,write:0,blocked:0},deny=()=>{metrics.blocked++;throw Error('blocked')}
  for(const name of Object.keys(api))api[name]=async()=>deny()
  const seed={id:'structure-old',structure_key:'family',version:2,name:'原结构',status:'published',description:'原文 🙂',...(withNodes?{nodes:[{node_key:'unchanged-node',field_definition_version:'field-v1',position:7,default_value:'  原文🙂  '}]}:{})}
  const schema={type:'object',required:['value'],properties:{value:{type:['integer','null']},items:{type:'array',items:{type:'string'}}}}
  const fieldSeed={identity:{id:'field',key:'motion'},version:{id:'field-v1',field_definition:'field',field_key:'motion',version:1,status:'published',label:'原字段',value_type:'text'}}
  const session=authorMaintenanceSession(seed,schema,()=>metrics.read++,()=>metrics.write++,deny,withField?fieldSeed:undefined)
  return{api,fieldApi,session,metrics,storage,opened,handlers,click:(route:string)=>{let prevented=false;handlers[0]({target:{closest:()=>({href:route})},preventDefault:()=>{prevented=true}});assert.equal(prevented,true)}}
}

test('an existing structure keeps its actual node keys and exact field references across field maintenance',async t=>{
  const {api,fieldApi,session}=await setup(t,true,true,true)
  const before=await api.get('structure-old')
  assert.deepEqual(before.nodes,[{node_key:'unchanged-node',field_definition_version:'field-v1',position:7,default_value:'  原文🙂  '}])
  const successor=await fieldApi.create('field',{label:'新字段'},'field-v1')
  await fieldApi.transition(successor.id,'publish')
  assert.deepEqual(await api.get('structure-old'),before)
  assert.equal(session.report().originalUnchanged,true)
})

test('field maintenance requires exact identity and version, and preserves the original after successor publication',async t=>{
  const {fieldApi,session,metrics,click,opened}=await setup(t,true,true)
  const original=await fieldApi.get('field-v1')
  const successor=await fieldApi.create('field',{label:'独立后继'},original.id)
  await fieldApi.transition(successor.id,'publish')
  assert.equal((await fieldApi.list('field')).length,2)
  assert.deepEqual(await fieldApi.get(original.id),original);assert.equal(session.report().fieldOriginalUnchanged,true)
  click('http://fixture.test/settings?section=fields&field_definition=field&field_version=field-v1')
  assert.match(new URL(opened[0]).searchParams.get('maintenance_route')!,/field_version=field-v1/)
  for(const suffix of ['field_version=field-v1','field_definition=other&field_version=field-v1','field_definition=field&field_version=missing'])assert.throws(()=>click('http://fixture.test/settings?section=fields&'+suffix),/blocked/)
  await assert.rejects(fieldApi.create('other',{},original.id),/blocked/)
  await assert.rejects(fieldApi.transition(original.id,'retire'),/blocked/)
  assert.equal(metrics.write,2)
})

test('cross-maintenance fixtures are opt-in and leave other author matrices untouched',async t=>{
  const {session,storage,handlers,metrics}=await setup(t,false)
  assert.equal(session,null);assert.equal(storage.size,0);assert.equal(handlers.length,0);assert.deepEqual(metrics,{read:0,write:0,blocked:0})
})
test('only the isolated successor changes; exact original and public schema remain available',async t=>{
  const {api,session,storage,metrics}=await setup(t)
  const original=await api.get('structure-old')
  assert.equal(original.nodes.length,4);assert.equal(original.nodes[1].allow_null,true);assert.equal(original.nodes[1].required,true)
  const successor=await api.create({name:'明确后继',nodes:original.nodes},original.id)
  assert.equal(successor.version,3);assert.equal(successor.status,'draft')
  await api.transition(successor.id,'publish')
  assert.equal((await api.list(1,'','published')).count,2)
  assert.deepEqual(await api.get(original.id),original);assert.equal(session.report().originalUnchanged,true)
  assert.equal(metrics.write,2);assert.equal(metrics.blocked,0)
  assert.deepEqual([...storage.keys()],['autoprompt-test:author-maintenance:only-this-test'])
  await assert.rejects(api.create({name:'越界'},'other'),/blocked/)
  await assert.rejects(api.transition(original.id,'retire'),/blocked/)
})
test('maintenance navigation preserves exact route and fixture flags, never opens live or foreign pages',async t=>{
  const {click,opened}=await setup(t)
  const route='/settings?section=fields&structure_version=structure-old&relation_context=structure%3Astructure-old'
  click('http://fixture.test'+route)
  const target=new URL(opened[0]!)
  assert.equal(target.pathname,'/tests/fixtures/prompt-authoring.html');assert.equal(target.searchParams.get('matrix'),'1')
  assert.equal(target.searchParams.get('maintenance'),'1');assert.equal(target.searchParams.get('maintenance_route'),route)
  for(const url of ['https://foreign.test'+route,'http://fixture.test/settings?section=fields&structure_version=missing','http://fixture.test/settings?section=executions&execution_version=structure-old'])assert.throws(()=>click(url),/blocked/)
  assert.equal(opened.length,1)
})
