import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { createServer } from 'vite'
import { createSSRApp, h, reactive } from 'vue'
import { renderToString } from 'vue/server-renderer'

test('primitive summaries use saved version values and server-owned template relationships', async context => {
  const vite = await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  const {default:Summary} = await vite.ssrLoadModule('/src/components/PromptPrimitiveConfiguration.vue')
  const {default:Viewer} = await vite.ssrLoadModule('/src/components/PromptProfileConfiguration.vue')
  const helpers = await vite.ssrLoadModule('/src/services/promptPrimitiveConfiguration.ts')
  const options = {sourceProjectionKeys:[],channels:[],sourceProjectionPrimitives:[{value:'test.subject',engineKey:'test_engine',description:'读取参考主体。',renderingTemplateKeys:['reference','empty','missing']}],
    referenceRenderingFields:[{key:'reference',label:'参考素材声明',description:'每个素材各一段。',requirement:'必须保留标签。',example:{template:'DO NOT INJECT'}}]}
  const profile = {id:'v21',version:21,profileKey:'test',status:'draft',modelFamily:'any',operation:'video.generate',fieldRuleCount:1,sourceProjectionKey:'declarative_v1',
    sourceProjectionConfig:{schema_version:4,primitive_options:{test_engine:{rendering:{prompt_template_key:'saved-key',separator:'\n',list_prefix:'- ',
      templates:{reference:'{label}: <script>literal</script>',empty:'',unrelated:'NOT IN FOCUS',invalid:null},future_option:false}}},quality_advisories:{limit:0}},channelLimits:{}}
  const rule = {id:'r1',profileVersion:'v21',fieldKey:'subject',fieldLabel:'主体定义',sourceSelector:'projection.output',projectionPrimitive:'test.subject',generationMode:'server_locked',generationOutputSemantics:'final'}
  const render = (extra:any={})=>renderToString(createSSRApp({render:()=>h(Summary,{profile,rules:[rule],options,...extra})}))
  await context.test('summary preserves empty, whitespace, unknown and invalid data without defaults or edits', async()=>{
    const before = JSON.stringify({profile,rule,options})
    const html = await render()
    for(const text of ['内容生成与模板配置','已保存草稿','saved-key','\\n','已留空','格式异常','其他已保存的渲染配置','future_option','>否<','>0<','展开文案模板','不是实际执行结果']) assert.ok(html.includes(text),text)
    assert.match(html,/&lt;script&gt;literal&lt;\/script&gt;/)
    assert.doesNotMatch(html,/<script>|DO NOT INJECT|<details open|<(input|select|textarea|form)\b/)
    assert.equal(JSON.stringify({profile,rule,options}),before)
  })
  await context.test('field entry shows only catalog-related templates, with missing templates explicit',async()=>{
    const html = await render({focusRule:rule})
    for(const text of ['主体定义 · 相关模板','相关文案模板（3 项）','未保存','<details open','每个素材各一段']) assert.ok(html.includes(text),text)
    assert.doesNotMatch(html,/NOT IN FOCUS|invalid/)
    const viewer = await renderToString(createSSRApp({render:()=>h(Viewer,{profile,rules:[rule],options,focusRuleId:'r1'})}))
    assert.ok(viewer.indexOf('主体定义 · 相关模板') < viewer.indexOf('来源投影配置与模板'))
    assert.match(viewer,/NOT IN FOCUS/) // Full saved snapshot remains below the focused section.
  })
  await context.test('historical policy and missing catalog are not replaced with current templates', async()=>{
    const old = {...profile,id:'v19',version:19,status:'published',sourceProjectionConfig:{schema_version:3,primitive_options:{test_engine:{reference_context_policy:{style_opening_max_sentences:2}}}}}
    const html = await render({profile:old,rules:[]})
    assert.match(html,/不能据此还原历史内置文案/);assert.match(html,/>2</)
    assert.doesNotMatch(html,/saved-key|DO NOT INJECT|已保存草稿/)
    assert.equal(helpers.hasRuleTemplates(old,{...rule,profileVersion:'v19'},options),false)
    assert.equal(helpers.hasRuleTemplates(profile,rule,options),true)
    const unknown = await render({options:undefined,focusRule:rule})
    assert.match(unknown,/未提供此原语的模板对应关系/)
    assert.doesNotMatch(unknown,/相关文案模板/)
    assert.match(await render({profile:{...profile,sourceProjectionConfig:{}}}),/未保存该原语引擎的配置/)
    assert.match(await render({profile:{...profile,sourceProjectionConfig:null}}),/来源投影配置格式异常/)
  })
  await context.test('direct rules and version switches do not inherit primitive state', async()=>{
    const direct = {...profile,id:'direct',sourceProjectionKey:'direct_v1',sourceProjectionConfig:{}}
    assert.match(await render({profile:direct,rules:[]}),/没有引用投影原语/)
    assert.equal(helpers.rulePrimitive({...rule,sourceSelector:'request.subject'},options),undefined)
    assert.match(helpers.ruleContentMethod({...rule,generationMode:'model_generated',generationOutputSemantics:'contribution'}),/先由模型生成/)
    const props = reactive({profile,rules:[rule],options})
    let state:any
    await renderToString(createSSRApp({setup(){state=Summary.setup(props,{expose:()=>{}});return()=>h('div')}}))
    assert.equal(state.groups.value.length,1)
    props.profile=direct as any
    assert.equal(state.groups.value.length,0)
    assert.equal(state.projectionRules.value.length,0)
  })
  await context.test('the existing snapshot viewer is the only drilldown and no reads mutate the strategy',async()=>{
    const source = await readFile(new URL('../src/components/PromptProfileSettings.vue',import.meta.url),'utf8')
    assert.match(source,/@click="openConfiguration\(selectedProfile,item.id\)"/)
    assert.match(source,/<PromptPrimitiveConfiguration :key="selectedProfile.id"/)
    for(const file of ['PromptPrimitiveConfiguration.vue','PromptProfileConfiguration.vue']) {
      const component = await readFile(new URL(`../src/components/${file}`,import.meta.url),'utf8')
      assert.doesNotMatch(component,/platformApi|v-model|v-html|fetch\(/)
    }
  })
  await context.test('declarative authoring grids can shrink without clipping fieldsets or long validation errors',async()=>{
    const source = await readFile(new URL('../src/components/PromptProfileSettings.vue',import.meta.url),'utf8')
    assert.match(source,/\.projection-config-fieldset,\.prompt-live-preview\{min-width:0\}/)
    assert.match(source,/@media\(max-width:900px\)\{\.prompt-editor-live-layout\{grid-template-columns:minmax\(0,1fr\)\}/)
    assert.match(source,/\.prompt-live-preview p,\.prompt-live-preview code\{overflow-wrap:anywhere\}/)
    assert.match(source,/\.prompt-form-grid,\.projection-config-grid,\.preview-source-grid\{grid-template-columns:minmax\(0,1fr\)\}/)
  })
})
