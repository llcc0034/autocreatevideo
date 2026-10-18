import test from 'node:test'
import assert from 'node:assert/strict'
import {createServer} from 'vite'
import {scriptFixture} from './fixtures/script-semantics-data.ts'
const copy=(value:any)=>JSON.parse(JSON.stringify(value))
async function setup(t:any){
  const store=new Map<string,string>(),opened:string[]=[],handlers:any[]=[]
  const globals:any={localStorage:{getItem:(k:string)=>store.get(k)??null,setItem:(k:string,v:string)=>store.set(k,v)},location:{origin:'http://fixture.test',pathname:'/tests/fixtures/business-output-authoring.html',search:'?fixture_session=test'},document:{addEventListener:(_event:string,handler:any)=>handlers.push(handler)},window:{open:(url:URL)=>opened.push(String(url))}}
  function install(key:string,value:any){const old=Object.getOwnPropertyDescriptor(globalThis,key);Object.defineProperty(globalThis,key,{configurable:true,value});t.after(()=>old?Object.defineProperty(globalThis,key,old):Reflect.deleteProperty(globalThis,key))}
  install('localStorage',globals.localStorage)
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}});t.after(()=>vite.close())
  const helper=await vite.ssrLoadModule('/tests/fixtures/output-maintenance-session.ts'),{dataStructures:api}=await vite.ssrLoadModule('/src/services/dataStructures.ts')
  for(const [key,value] of Object.entries(globals))if(key!=='localStorage')install(key,value)
  const metrics={read:0,write:0,blocked:0},deny=()=>{metrics.blocked++;throw Error('blocked')}
  const session=helper.outputMaintenanceSession(()=>metrics.read++,()=>metrics.write++,deny,[{id:'script-structure',structure_key:'script-structure-family',version:3,status:'published',name:'原结构',description:'原中文 🟢',nodes:[]}])
  return{helper,api,session,metrics,store,opened,click:(url:string)=>{let prevented=false;handlers[0]({target:{closest:()=>({href:url})},preventDefault:()=>{prevented=true}});assert.equal(prevented,true)}}
}
test('isolated maintenance shares only its namespaced catalog and preserves original nodes on successor',async t=>{
  const {api,session,metrics,store}=await setup(t)
  const original=await api.get('script-structure');assert.equal(original.nodes.length,41)
  const next=await api.create({...copy(original),name:'明确后继'},original.id)
  assert.equal(next.version,4);assert.equal(next.status,'draft');assert.deepEqual(next.nodes,original.nodes)
  assert.deepEqual(await api.get(original.id),original)
  assert.equal((await api.list(1,'','published')).results.length,1)
  await api.transition(next.id,'publish')
  assert.equal((await api.list(1,'','published')).results.length,2)
  assert.deepEqual(await api.get(original.id),original);assert.equal(metrics.write,2)
  assert.deepEqual([...store.keys()],[session.key]);assert.equal(metrics.blocked,0)
  await assert.rejects(api.create({name:'不允许'},'unrelated'),/blocked/)
  await assert.rejects(api.transition(original.id,'retire'),/blocked/)
})
test('maintenance link transport keeps exact route and rejects foreign origins and unrelated resources',async t=>{
  const {api,click,opened}=await setup(t)
  const next=await api.create({...await api.get('script-structure'),name:'后继'},'script-structure')
  for(const id of ['script-structure',next.id]){
    const route='/settings?section=fields&structure_version='+id+'&relation_context=structure%3A'+id
    click('http://fixture.test'+route)
    const url=new URL(opened.at(-1)!);assert.equal(url.pathname,'/tests/fixtures/business-output-authoring.html');assert.equal(url.searchParams.get('maintenance_route'),route);assert.equal(url.searchParams.get('fixture_session'),'test')
  }
  for(const url of ['https://foreign.example/settings?section=fields&structure_version=script-structure','http://fixture.test/settings?section=fields&structure_version=missing'])assert.throws(()=>click(url),/blocked/)
  assert.equal(opened.length,2)
})
test('fixture never labels original parser results as success for a modified semantic mapping',async t=>{
  const {helper}=await setup(t),row={id:'output',revision:2,data_structure_version:'script-structure',contract_sha256:'fixture',parser_contract:{mapping:copy(scriptFixture.layouts.b.mapping)}}
  const text=JSON.stringify(scriptFixture.layouts.b.raw,null,2)
  assert.equal(helper.semanticFixtureReport(row,row,text).output_version_id,row.id)
  const modified=copy(row);modified.parser_contract.mapping.node.fields.episodes.item.fields.shots.item.fields.duration_ms={kind:'literal',value:3400}
  assert.throws(()=>helper.semanticFixtureReport(modified,modified,text),/不执行修改后映射/)
  assert.throws(()=>helper.semanticFixtureReport(row,{...row,revision:3},text),/不执行修改后映射/)
})
