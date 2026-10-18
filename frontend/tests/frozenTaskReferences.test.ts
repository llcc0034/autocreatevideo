import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {fileURLToPath} from 'node:url'
import {createSSRApp,createRenderer,reactive,nextTick,ssrContextKey} from 'vue'
import {renderToString} from 'vue/server-renderer'

const root=fileURLToPath(new URL('..',import.meta.url))
const use=(key:string)=>({itemKey:key,inputKey:'portraits',assetId:'asset',displayName:'原请求雨衣',mediaKind:'image',
  referenceBinding:{position:0,original_name:'雨衣',reference_name:null,name_disambiguated:null,input_slot:null,shot_reference:null},
  inputLabel:'<Picture 1>',previewUrl:'/api/v1/media/isolated/',checksum:'a'.repeat(64),sourceSha256:'b'.repeat(64),
  ports:[{targetKey:'character_images',port:{kind:'semantic',semantic_key:'image.reference'}}],stagingStatus:'not_staged'})
const fixture=()=>({id:'task',inputContractStatus:'available',referenceAssets:[use('one'),{...use('two'),inputLabel:'<Picture 2>',purpose:'style',stagingStatus:'staged'}],
  contextMediaUses:[{...use('context'),displayName:'旧背景正文素材',artifactId:'artifact',assetId:undefined,ports:[],stagingStatus:'not_applicable',previewUrl:''}]})
async function modules(fn:any){const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try{await fn((await vite.ssrLoadModule('/src/components/FrozenTaskReferences.vue')).default)}finally{await vite.close()}}
function mount(C:any,props:any){let state:any
  const renderer=createRenderer({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=C.setup(props,{expose(){}});return()=>null}});app.provide(ssrContextKey,{});app.mount({})
  return {state,app}}

test('original repeated uses remain separate with actual input, target and context; no initial media fetch',()=>modules(async C=>{
  const task=fixture(),before=JSON.stringify(task),html=await renderToString(createSSRApp(C,{task}))
  for(const value of ['原请求雨衣','portraits','character_images','&lt;Picture 1&gt;','&lt;Picture 2&gt;','尚未暂存','已暂存','仅提示词上下文','旧背景正文素材','此槽位无额外用途值','未记录分镜选材版本','不等于远端收到'])assert.ok(html.includes(value),value)
  assert.equal((html.match(/原请求雨衣/g)||[]).length,2)
  assert.doesNotMatch(html,/<img|<video|<audio|<select|type="checkbox"|重试调用|更换素材/)
  assert.equal(JSON.stringify(task),before)
}))
test('unavailable contract is not empty; explicit empty and missing previews have distinct states',()=>modules(async C=>{
  const invalid=await renderToString(createSSRApp(C,{task:{...fixture(),inputContractStatus:'unavailable'}}))
  assert.match(invalid,/原输入合同不可核对/);assert.doesNotMatch(invalid,/原请求雨衣|明确没有媒体/)
  const empty=await renderToString(createSSRApp(C,{task:{...fixture(),referenceAssets:[],contextMediaUses:[]}}))
  assert.match(empty,/明确没有媒体素材/)
  const missing=fixture();missing.referenceAssets[0]!.previewUrl=''
  assert.match(await renderToString(createSSRApp(C,{task:missing})),/保留原绑定，不寻找替代文件/)
}))
test('preview toggles only local state, refuses unsafe URL and resets on another request',()=>modules(async C=>{
  const props=reactive({task:fixture()}),view=mount(C,props)
  try{assert.deepEqual(view.state.expanded.value,[]);view.state.toggle('one');assert.deepEqual(view.state.expanded.value,['one'])
    view.state.toggle('one');assert.deepEqual(view.state.expanded.value,[])
    for(const url of ['javascript:alert(1)','file:///secret','//foreign/media','/\\foreign/media','data:image/png;base64,AAAA'])assert.equal(view.state.previewUrl({...use('one'),previewUrl:url}),'')
    assert.equal(view.state.previewUrl(use('one')),'/api/v1/media/isolated/')
    view.state.toggle('one');view.state.failed.value=['two'];props.task={...fixture(),id:'other'};await nextTick()
    assert.deepEqual(view.state.expanded.value,[]);assert.deepEqual(view.state.failed.value,[])
  }finally{view.app.unmount()}
}))
