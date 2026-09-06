import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { createServer } from 'vite'
import { createSSRApp, h, reactive } from 'vue'
import { renderToString } from 'vue/server-renderer'

test('model capability contract is a lossless readonly view of the selected version', async context => {
  const vite = await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(() => vite.close())
  const helper = await vite.ssrLoadModule('/src/services/modelCapabilityContract.ts')
  const { default: Viewer } = await vite.ssrLoadModule('/src/components/ModelCapabilityContract.vue')
  const { default: Fields } = await vite.ssrLoadModule('/src/components/ModelContractFields.vue')
  const render = async (profile: any, options: any = null) => (await renderToString(createSSRApp({render: () => h(Viewer, {profile, options})}))).replace(/ data-v-[a-z0-9-]+/g, '')
  const options = {capabilities:[{value:'video.reference_to_video',label:'参考生视频'}],conditioningContract:{
    inputKinds:[{value:'classified_references',label:'分类参考素材'}],
    inputSlots:[{value:'entity_images',label:'参考图片'},{value:'total_images',label:'图片总数'},{value:'last_frame',label:'尾帧图片'}],
  }}
  const mode = {key:'Ref2VA',label:'多参考模式',input_kind:'classified_references',supports_native_audio:false,
    inputs:{entity_images:{minimum_items:0,maximum_items:9},total_images:{minimum_items:0,maximum_items:10},last_frame:{minimum_items:0,maximum_items:0}},
    dynamic_reference_topology:{preprocessing:{videos:{minimum_duration_ms:2000,target_fps:24}},
      pairing_rules:[{label:'配对规则名称',maximum_targets_per_source:1,allow_external_target:true}]},
  }
  const profile = {id:'media-v2',version:2,specification:{schema_version:1,model_family:'test_family',capabilities:['video.reference_to_video'],
    conditioning_contract:{schema_version:1,modes:[mode,{...mode,key:'Another',label:'另一模式'}]},
    media_constraints:{resolution:{minimum_width:352,maximum_height:608},fps:{allowed:[24]},duration_ms:{}},
    execution_monitoring:{default_idle_timeout_seconds:1800,node_type_overrides:{SamplerCustomAdvanced:5400}},
    extension_contract:{unknown_field:'keep-me',disabled:false,zero:0,empty:[],empty_object:{},nullable:null},
  }}

  await context.test('renders every mode, bounds, rules and declared limits without changing input', async () => {
    const before = JSON.stringify(profile)
    const html = await render(profile, options)
    for (const text of ['参考生视频','test_family','2 个模式','另一模式','分类参考素材','可选 · 动态数量','不支持','合计约束','<td>9</td>','<td>10</td>',
      '最小宽度（像素）','352','最大高度（像素）','608','帧率（FPS）','用途、预处理与配对规则','最短时长（毫秒）','2000','配对规则名称',
      '默认静默时限（秒）','1800','SamplerCustomAdvanced','5400','未声明子项','keep-me','空值（null）','空列表（0 项）','不代表烟测已通过','工作流实际可用范围']) assert.ok(html.includes(text),text)
    assert.doesNotMatch(html,/<(?:input|select|textarea|button)\b/)
    assert.equal(JSON.stringify(profile),before)
  })
  await context.test('missing, malformed and zero bounds remain distinct', () => {
    assert.equal(helper.quantityDeclaration(undefined),'未声明')
    assert.equal(helper.quantityDeclaration(null),'数量声明格式异常')
    assert.equal(helper.quantityDeclaration({minimum_items:0}),'数量声明不完整')
    for (const value of [{minimum_items:-1,maximum_items:2},{minimum_items:2,maximum_items:1},{minimum_items:0,maximum_items:'3'},{minimum_items:0,maximum_items:1.5}]) assert.equal(helper.quantityDeclaration(value),'数量声明无效')
    assert.equal(helper.quantityDeclaration({minimum_items:0,maximum_items:0}),'不支持')
    assert.equal(helper.quantityDeclaration({minimum_items:2,maximum_items:2}),'固定数量')
    assert.equal(helper.quantityDeclaration({minimum_items:1,maximum_items:2}),'动态数量')
    assert.equal(helper.contractScalar(0),'0');assert.equal(helper.contractScalar(false),'否')
  })
  await context.test('catalog adds labels, never quantities, and missing catalog does not hide data', async () => {
    const partial = {...profile,specification:{conditioning_contract:{modes:[{...mode,inputs:{entity_images:{maximum_items:4},new_slot:{minimum_items:1,maximum_items:1,custom_rule:'preserved'}}}]}}}
    const html = await render(partial, options)
    assert.match(html,/数量声明不完整/);assert.match(html,/<td>未声明<\/td>/)
    assert.match(html,/new_slot/);assert.match(html,/preserved/)
    const withoutCatalog = await render(profile)
    assert.match(withoutCatalog,/<td>9<\/td>/);assert.match(withoutCatalog,/参考图片/)
  })
  await context.test('text models show operation-specific limits without fabricated media constraints', async () => {
    const html = await render({id:'text',specification:{capabilities:['prompt.normalize'],prompt_normalization:{max_input_bytes:50000,max_output_tokens:4096,max_items_per_batch:8,timeout_seconds:90,chat_request_options:{}},visual_rules:{max_input_bytes:100000}}})
    for(const text of ['提示词标准化容量','每批最多字段数','50000','4096','请求超时（秒）','影视规则生成容量','100000','此版本未声明素材数量合同']) assert.ok(html.includes(text),text)
    assert.doesNotMatch(html,/<table|数量声明格式异常|媒体规格限制/)
  })
  await context.test('unrecognized nested fields and null/false/zero remain visible and text is escaped', async () => {
    const value = JSON.parse('{"constructor":"own-value","__proto__":{"payload":"<script>alert(1)</script>"},"nested":[false,0,null,""]}')
    const html = await renderToString(createSSRApp({render:()=>h(Fields,{value})}))
    for(const text of ['constructor','own-value','__proto__','&lt;script&gt;','空值（null）','空字符串','>否<','>0<']) assert.ok(html.includes(text),text)
    assert.doesNotMatch(html,/<script>/)
    assert.equal(helper.capabilityFieldLabel('constructor'),'constructor')
  })
  await context.test('invalid contracts and modes are explicit rather than filtered out', async () => {
    assert.match(await render({id:'bad',specification:null}),/能力合同格式异常/)
    assert.match(await render({id:'bad',specification:{capabilities:false,conditioning_contract:{modes:[null,{},'bad']}}}),/能力列表格式异常/)
    const html = await render({id:'bad',specification:{conditioning_contract:{modes:[null,{},'bad']}}})
    assert.match(html,/模式 1 格式异常/);assert.match(html,/模式 3 格式异常/);assert.match(html,/素材数量声明缺失或格式异常/)
    assert.match(await render({id:'empty',specification:{conditioning_contract:{modes:[]}}}),/缺少模式声明/)
  })
  await context.test('reactive model switch replaces all derived data without stale mode state', async () => {
    const props = reactive({profile,options})
    let state:any
    await renderToString(createSSRApp({setup(){state=Viewer.setup(props,{expose:()=>{}});return()=>h('div')}}))
    assert.equal(state.modes.value.length,2)
    props.profile = {id:'next',version:1,specification:{capabilities:['script.generate']}} as any
    assert.equal(state.modes.value.length,0)
    assert.equal(state.conditioning.value,undefined)
    assert.deepEqual(state.capabilities.value,['script.generate'])
    assert.deepEqual(state.remaining.value,{})
  })
  await context.test('model inspector uses the viewer only on the capability tab and retains raw JSON', async () => {
    const source = await readFile(new URL('../src/views/SettingsView.vue',import.meta.url),'utf8')
    assert.match(source,/<ModelCapabilityContract v-if="selectedModelSection==='capabilities'" :profile="group.profile" :options="modelProfileOptions"/)
    assert.match(source,/查看完整 JSON/)
    assert.doesNotMatch(source,/<div class="model-capability-chips"/)
  })
})
