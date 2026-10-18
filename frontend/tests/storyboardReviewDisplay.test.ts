import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {fileURLToPath} from 'node:url'
import {createSSRApp} from 'vue'
import {renderToString} from '@vue/server-renderer'
import {displayFixture} from './storyboardReviewDisplayFixture.ts'

const root=fileURLToPath(new URL('..',import.meta.url))
const id=(n:number)=>`00000000-0000-4000-8000-${String(n).padStart(12,'0')}`
async function modules(fn:any){const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true}})
  try{await fn(vite,await vite.ssrLoadModule('/src/services/storyboardReviewDisplay.ts'))}finally{await vite.close()}}
function fixture():any{
  const value:any=displayFixture(id(20),[id(1),id(2)])
  value.shots[0].scene={id:id(30),name:'车站',status:'available'}
  value.shots[0].characters=[{role:'speaker',position:2,entity:{id:id(31),name:'画外音',status:'available'}},
    {role:'visible',position:0,entity:{id:id(32),name:'旅人',status:'available'}},
    {role:'speaker',position:0,entity:{id:id(31),name:'画外音',status:'available'}}]
  value.shots[0].props=[{id:id(33),name:null,status:'unavailable'}]
  return value
}
test('original episode/shot numbers sort display copies only, independent speaker rows remain separate',()=>modules(async(_v:any,m:any)=>{
  const raw=fixture();raw.shots.reverse();raw.shots[0].episode_number=2;raw.shots[0].episode_id=id(21)
  const before=JSON.stringify(raw),result=m.readStoryboardReviewDisplay(raw,[id(1),id(2)])
  assert.deepEqual(result.shots.map((s:any)=>s.shot_id),[id(1),id(2)])
  assert.deepEqual(result.shots[0].characters.filter((r:any)=>r.role==='speaker').map((r:any)=>r.position),[0,2])
  assert.equal(result.shots[0].characters.filter((r:any)=>r.role==='visible').length,1)
  assert.equal(JSON.stringify(raw),before)
}))
test('missing metadata, hidden/duplicate shot, unknown role and falsely available names fail explicitly',()=>modules(async(_v:any,m:any)=>{
  for(const raw of [null,{...fixture(),name_basis:'guessed'}, {...fixture(),shots:[]},
    {...fixture(),shots:[fixture().shots[0],fixture().shots[0]]}])assert.throws(()=>m.readStoryboardReviewDisplay(raw,[id(1),id(2)]))
  for(const change of [{number:0},{episode_id:'bad'},{scene:{id:id(30),name:'',status:'available'}},
    {characters:[{role:'inferred',position:0,entity:{id:id(30),name:'name',status:'available'}}]},
    {characters:[fixture().shots[0].characters[0],fixture().shots[0].characters[0]]},
    {props:[{id:id(33),name:'hidden',status:'unavailable'}]}]){
    const raw=fixture();Object.assign(raw.shots[0],change);assert.throws(()=>m.readStoryboardReviewDisplay(raw,[id(1),id(2)]))
  }
}))
test('shared markup reads names without JSON, distinguishes off-screen speakers and shows unavailable names',()=>modules(async(v:any,m:any)=>{
  const {default:Component}=await v.ssrLoadModule('/src/components/StoryboardShotRelations.vue')
  const result=m.readStoryboardReviewDisplay(fixture(),[id(1),id(2)])
  const html=await renderToString(createSSRApp(Component,{shot:result.shots[0]}))
  const visible=html.split('<details')[0]
  for(const text of ['车站','旅人','画外音','发声不代表出镜','对白第1条','对白第3条','原关联名称不可用'])assert.ok(visible.includes(text),text)
  assert.doesNotMatch(visible,/[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}|<input|<button|<select/)
  const empty=await renderToString(createSSRApp(Component,{shot:result.shots[1]}))
  assert.match(empty,/未声明出镜角色/);assert.match(empty,/未声明发声角色/);assert.match(empty,/未关联道具/)
}))
