import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const server = () => createServer({ root: fileURLToPath(new URL('..', import.meta.url)), logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
const rawSchema = () => ({
  schema_version: 2, available: true, reason_code: '', parameters: [],
  dynamic_reference_modes: {}, dynamic_references: null,
  asset_slots: ['asset.image.source', 'asset.image.source_2', 'asset.image.source_3'].map((key, index) => ({ semantic_key: key, input_slot: index+1, media_kind: 'image', category: 'image', required: index===0, min_items: index===0?1:0, max_items: 1 })),
  asset_groups: [{ key: 'references', media_kind: 'image', min_items: 1, max_items: 3, semantic_keys: ['asset.image.source', 'asset.image.source_2', 'asset.image.source_3'], wiring: 'ordered_ports' }],
})

test('dynamic groups validate shape, exact slot membership, media and cardinality', async (context) => {
  const vite = await server(); context.after(()=>vite.close())
  const {adaptWorkflowSmokeFixtureSchema: adapt} = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const schema = adapt(rawSchema())
  assert.equal(schema.assetGroups[0].maxItems,3)
  for (const mutate of [
    (raw:any)=>{raw.asset_groups[0].max_items=4},
    (raw:any)=>{raw.asset_groups[0].semantic_keys[1]=raw.asset_groups[0].semantic_keys[0]},
    (raw:any)=>{raw.asset_groups[0].media_kind='audio'},
    (raw:any)=>{raw.asset_groups[0].wiring='execute_script'},
    (raw:any)=>{raw.asset_slots[1].required=true},
  ]) { const raw=rawSchema(); mutate(raw); assert.throws(()=>adapt(raw),/动态素材组/) }
})

test('dynamic groups reuse ordered Asset ID requests and reject holes', async (context) => {
  const vite = await server(); context.after(()=>vite.close())
  const {adaptWorkflowSmokeFixtureSchema: adapt} = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const inputs = await vite.ssrLoadModule('/src/services/workflowExecutionInputs.ts')
  const schema = adapt(rawSchema())
  const assets = [1,2,3].map(index=>({id:String(index),name:`ref ${index}`,type:'image',mimeType:'image/png',checksum:'a'.repeat(64),isActive:true}))
  for (const count of [1,2,3]) {
    const draft=inputs.createWorkflowExecutionInputDraft(schema)
    schema.assetGroups[0].semanticKeys.slice(0,count).forEach((key:string,index:number)=>{draft.assetIds[key]=String(index+1)})
    assert.equal(inputs.validateWorkflowExecutionInputDraft(schema,draft,assets),'')
    assert.equal(inputs.buildWorkflowExecutionInput(schema,draft).assets.length,count)
  }
  const invalid=inputs.createWorkflowExecutionInputDraft(schema)
  invalid.assetIds['asset.image.source']='1';invalid.assetIds['asset.image.source_3']='3'
  assert.match(inputs.validateWorkflowExecutionInputDraft(schema,invalid,assets),/不能跳过/)
})

test('shared execution editor renders dynamic count controls and only initial slots', async (context) => {
  const vite = await server(); context.after(()=>vite.close())
  const {createSSRApp,h}=await import('vue')
  const {renderToString}=await import('vue/server-renderer')
  const {default: Editor}=await vite.ssrLoadModule('/src/components/WorkflowExecutionInputEditor.vue')
  const {adaptWorkflowSmokeFixtureSchema: adapt}=await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const {createWorkflowExecutionInputDraft}=await vite.ssrLoadModule('/src/services/workflowExecutionInputs.ts')
  const schema=adapt(rawSchema())
  const html=await renderToString(createSSRApp({render:()=>h(Editor,{schema,modelValue:createWorkflowExecutionInputDraft(schema),assets:[],allowAssetUpload:true})}))
  assert.match(html,/增加素材/);assert.match(html,/减少素材/);assert.match(html,/允许 1–3 份/)
  assert.match(html,/aria-label="asset.image.source"/)
  assert.doesNotMatch(html,/aria-label="asset.image.source_2"/)
  assert.match(html,/上传本地/)
})

test('contract source preview reads the exact declared model path without fallback or mutation', async (context) => {
  const vite=await server();context.after(()=>vite.close())
  const {modelAssetQuantitySource: resolve,assetGroupForSlot}=await vite.ssrLoadModule('/src/services/workflowAssetContractSource.ts')
  const group={key:'refs',conditioning_mode:'Ref2VA',model_input:'total_images'}
  const profile={id:'exact-v2',specification:{conditioning_contract:{schema_version:1,modes:[{key:'Ref2VA',inputs:{total_images:{minimum_items:0,maximum_items:6}}}]}}}
  const before=JSON.stringify(profile)
  assert.match(resolve(null,group).message,/尚未读取/)
  assert.match(resolve({specification:{}},group).message,/未声明 conditioning_contract/)
  assert.match(resolve(profile,{...group,conditioning_mode:'unknown'}).message,/未唯一声明/)
  assert.match(resolve(profile,{...group,model_input:'images'}).message,/未声明数量输入/)
  assert.match(resolve(profile,{...group,model_input:''}).message,/尚未填写/)
  assert.equal(resolve(profile,group).message,'')
  assert.deepEqual(resolve(profile,group).input,{minimum_items:0,maximum_items:6})
  const malformed=structuredClone(profile);malformed.specification.conditioning_contract.modes[0].inputs.total_images.maximum_items=true as any
  assert.match(resolve(malformed,group).message,/缺少有效/)
  assert.equal(JSON.stringify(profile),before)
  assert.equal(assetGroupForSlot({projection:{config:{groups:[group]}}},'refs'),group)
  assert.equal(assetGroupForSlot({projection:{config:{groups:[group,group]}}},'refs'),null)
})
