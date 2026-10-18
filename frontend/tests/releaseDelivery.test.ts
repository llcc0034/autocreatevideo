import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'
import { releaseId as id, releaseIdentity, releaseDocument, releaseArtifact } from './fixtures/release-delivery.ts'

async function modules(fn) {
  const vite=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try { await fn(await vite.ssrLoadModule('/src/services/releaseDelivery.ts'),vite) } finally { await vite.close() }
}
test('verified document reader preserves all server fields and rejects incomplete or foreign bundles',()=>modules(m=>{
  const raw=releaseDocument(), result=m.readReleaseDelivery(raw,releaseIdentity)
  assert.deepEqual(result.document,raw);assert.equal(result.masters[0].artifactId,id(7))
  for(const change of [{bundle_id:id(90)},{project_id:id(90)},{production_run_id:id(90)},{schema:'old'},
    {verification:{...raw.verification,live_verified:false}},{verification:{...raw.verification,signature_verified:false}},
    {verification:{...raw.verification,verified_at:'bad'}},{assemblies:[]},{assemblies:[raw.assemblies[0],raw.assemblies[0]]},
    {assemblies:[{...raw.assemblies[0],generated_artifact_id:id(99)}]},
    {selection:{shots:[],episodes:[],series_artifact_id:null}},{integrity:{payload_sha256:''}}]) {
    assert.throws(()=>m.readReleaseDelivery({...raw,...change},releaseIdentity))
  }
}))
test('master reader requires exact nonempty file, run, sha, status and both formal URLs',()=>modules(async(m,vite)=>{
  const {verifyAssemblyMedia}=await vite.ssrLoadModule('/src/services/assemblyMedia.ts')
  const master=m.readReleaseDelivery(releaseDocument(),releaseIdentity).masters[0], artifact=releaseArtifact()
  assert.equal(verifyAssemblyMedia(artifact,master,releaseIdentity.runId),artifact)
  for(const change of [{id:id(99)},{productionRun:id(99)},{kind:'video'},{status:'failed'},{sizeBytes:0},{sizeBytes:null},
    {sha256:'c'.repeat(64)},{mimeType:'application/json'},{previewUrl:''},{downloadUrl:''},{objectKey:''}])
    assert.throws(()=>verifyAssemblyMedia({...artifact,...change},master,releaseIdentity.runId))
}))
async function mounted(vite,fn) {
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const originals={getVerifiedReleaseDocument:platformApi.getVerifiedReleaseDocument,getArtifact:platformApi.getArtifact}
  const reads=[],saved=[],blobs=[]
  platformApi.getVerifiedReleaseDocument=async key=>{reads.push(['verify',key]);return releaseDocument()}
  platformApi.getArtifact=async key=>{reads.push(['artifact',key]);return releaseArtifact()}
  const originalsGlobal=['document','window'].map(key=>[key,Object.getOwnPropertyDescriptor(globalThis,key)])
  const create=URL.createObjectURL,revoke=URL.revokeObjectURL
  URL.createObjectURL=blob=>{blobs.push(blob);return 'blob:isolated'}; URL.revokeObjectURL=()=>{}
  Object.defineProperty(globalThis, 'document', {
    configurable: true,
    value: {
      body: { appendChild() {} },
      createElement() {
        return { href: '', download: '', click() { saved.push({ url: this.href, name: this.download }) }, remove() {} }
      },
    },
  })
  Object.defineProperty(globalThis,'window',{configurable:true,value:{setTimeout:fn=>fn()}})
  const {default:Component}=await vite.ssrLoadModule('/src/components/ProductionReleaseDelivery.vue')
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const props=reactive({...releaseIdentity});let state:any
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){}});return()=>h('div')}})
  try{app.provide(ssrContextKey,{modules:new Set()});app.mount({});await nextTick();await fn({state,props,platformApi,reads,saved,blobs})}
  finally{app.unmount();Object.assign(platformApi,originals);URL.createObjectURL=create;URL.revokeObjectURL=revoke
    for(const [key,descriptor]of originalsGlobal){if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key]}}
}
test('component is on demand and refreshed failure never leaves old verified success or media',()=>modules(async(_m,vite)=>mounted(vite,async({state,platformApi,reads})=>{
  assert.deepEqual(reads,[]);await state.load();assert.equal(state.detail.value.masters.length,1)
  await state.media(state.detail.value.masters[0]);assert.ok(state.previews.value[id(7)])
  platformApi.getVerifiedReleaseDocument=async()=>{throw Error('403 forbidden')}
  await state.load();assert.equal(state.detail.value,null);assert.deepEqual(state.previews.value,{});assert.match(state.error.value,/403/)
})))
test('old bundle reads and old detached video errors cannot overwrite a changed context',()=>modules(async(_m,vite)=>mounted(vite,async({state,props,platformApi})=>{
  let finish;platformApi.getVerifiedReleaseDocument=()=>new Promise(resolve=>{finish=resolve})
  const request=state.load();props.bundleId=id(99);await nextTick();finish(releaseDocument());await request
  assert.equal(state.detail.value,null);assert.equal(state.busy.value,'')
  props.bundleId=id(1);await nextTick();platformApi.getVerifiedReleaseDocument=async()=>releaseDocument()
  await state.load();await state.media(state.detail.value.masters[0]);const old=state.previews.value[id(7)],events=state.mediaEvents(old)
  await state.media(state.detail.value.masters[0]);events.error();assert.deepEqual(state.mediaErrors.value,{})
  state.mediaEvents(state.previews.value[id(7)]).error();assert.match(state.mediaErrors.value[id(7)],/无法播放/)
})))
test('download refreshes the exact master only after live verify and stops on mismatch',()=>modules(async(_m,vite)=>mounted(vite,async({state,platformApi,reads,saved})=>{
  await state.load();reads.length=0;await state.media(state.detail.value.masters[0],true)
  assert.deepEqual(reads,[['verify',id(1)],['artifact',id(7)]]);assert.deepEqual(saved,[{url:'/isolated-download',name:''}])
  platformApi.getArtifact=async()=>({...releaseArtifact(),sha256:'c'.repeat(64)})
  await state.media(state.detail.value.masters[0],true);assert.equal(saved.length,1);assert.equal(state.detail.value,null)
})))
test('export re-verifies and saves full server JSON, not visible rows; failed verification does not save',()=>modules(async(_m,vite)=>mounted(vite,async({state,platformApi,reads,saved,blobs})=>{
  await state.load();reads.length=0;await state.exportManifest()
  assert.deepEqual(reads,[['verify',id(1)]]);assert.deepEqual(JSON.parse(await blobs[0].text()),releaseDocument())
  assert.equal(saved[0].name,`release-${id(1)}.json`)
  assert.equal(state.manifestDownload.value.name, saved[0].name)
  platformApi.getVerifiedReleaseDocument=async()=>{throw Error('file missing')}
  await state.exportManifest();assert.equal(saved.length,1);assert.equal(state.detail.value,null);assert.match(state.error.value,/file missing/)
  assert.equal(state.manifestDownload.value,null)
})))
test('formal Run shares one read-only delivery component and API uses the original verify route',()=>modules(async(_m,vite)=>{
  const {api}=await vite.ssrLoadModule('/src/services/api.ts'),{platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),old=api.get,reads=[]
  try{api.get=async path=>{reads.push(path);return {data:releaseDocument()}};assert.deepEqual(await platformApi.getVerifiedReleaseDocument(id(1)),releaseDocument())
    assert.deepEqual(reads,[`/release-bundles/${id(1)}/verify/`])}finally{api.get=old}
  const view=await readFile(new URL('../src/views/ProductionRunView.vue',import.meta.url),'utf8')
  assert.ok(view.includes('<ProductionReleaseDelivery v-if="currentRun.releaseBundleId"'))
}))
