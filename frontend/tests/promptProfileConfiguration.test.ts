import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { createServer } from 'vite'
import { createSSRApp, h, reactive } from 'vue'
import { renderToString } from 'vue/server-renderer'

test('prompt strategy configuration preserves saved historical and current values in a readonly view', async context => {
  const vite = await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(() => vite.close())
  const { default: Viewer } = await vite.ssrLoadModule('/src/components/PromptProfileConfiguration.vue')
  const { promptConfigurationLabel } = await vite.ssrLoadModule('/src/services/promptProfileConfiguration.ts')
  const options = {sourceProjectionKeys:[],channels:[{value:'positive_prompt',label:'正向提示词'}],referenceRenderingFields:[{key:'reference',label:'参考素材模板'}]}
  const profile = {id:'published-v19',profileKey:'reference-strategy',name:'历史策略',version:19,status:'published',
    modelFamily:'example',operation:'video.generate',mode:'Ref2VA',fieldRuleCount:1,reviewState:'ready',
    contractSchemaVersion:6,contractSha256:'f'.repeat(64),contractSizeBytes:1024,description:'原始说明',
    sourceProjectionKey:'declarative_v1',sourceProjectionConfig:{schema_version:3,primitive_options:{reference_context_ir_v1:{
      reference_context_policy:{detailed_description:{style_opening_max_sentences:2,subject_reference_placement:'first_clear_appearance'},
        retention_analysis:{include_subject_shot_usage:true,assignment_separator:' - '}}}},
      quality_advisories:{short_shot_action_density:{maximum_duration_ms:5500}},unknown:{disabled:false,zero:0,list:[],object:{},nullable:null,blank:'',whitespace:'\n \t'}},
    channelLimits:{positive_prompt:{maxChars:7000,warnAtChars:6200}},publishedAt:'2026-09-01T12:00:00+08:00',publishedBy:'author',
  }
  const rule = {id:'rule-1',profileVersion:profile.id,fieldKey:'detail',fieldLabel:'详细描述',fieldDefinition:'field-1',
    position:1,sourceSelector:'projection.output',projectionPrimitive:'reference_context.detailed_description',
    channel:'positive_prompt',labelStyle:'label_colon',separator:'newline',required:true,omitIfEmpty:false,maxChars:7000,
    generationMode:'model_generated',generationSourceSelector:'shot.detailed_description',generationContextSelectors:['project.style'],
    generationInstruction:'完整保存的生成说明，不得添加未确认事实。',generationOutputSemantics:'contribution',generationRequired:true,
    languagePolicy:'en',lengthUnit:'characters',generationMin:0,generationMax:null,generationValidatorKeys:['protected_tokens_preserve_count'],
  }
  const render = (p = profile, rules = [rule], choices: any = options) => renderToString(createSSRApp({render:()=>h(Viewer,{profile:p,rules,options:choices})}))
  await context.test('v19 shows old policy, every rule setting and metadata without creating v21 defaults', async () => {
    const before = JSON.stringify({profile,rule})
    const html = await render()
    for(const value of ['风格开场最多句数','>2<','first_clear_appearance','关系分隔符',' - ', '5500','7000','6200',
      '完整保存的生成说明','生成长度下限','protected_tokens_preserve_count','project.style','contribution','2026-09-01','f'.repeat(64),
      '空值（null）','空字符串','空列表（0 项）','空对象','>否<','>0<','不能在这里还原','\\n \\t','空白字符，按 JSON 转义显示']) assert.ok(html.includes(value),value)
    assert.doesNotMatch(html,/<(?:input|select|textarea|form|button)\b/)
    assert.doesNotMatch(html,/reference_context_configured_v1|字段规则数据不完整/)
    assert.equal(JSON.stringify({profile,rule}),before)
  })
  await context.test('schema 4 templates are rendered as literal text, including unknown keys', async () => {
    const current = {...profile,id:'draft-v21',version:21,status:'draft',fieldRuleCount:0,sourceProjectionConfig:{schema_version:4,
      primitive_options:{reference_context_ir_v1:{rendering:{prompt_template_key:'configured',templates:{reference:'{label}: <script>alert(1)</script>',future_template:'{future}'}}}}}}
    const html = await render(current as any,[])
    for(const value of ['参考素材模板','{label}','&lt;script&gt;','future_template','{future}','此版本尚未发布']) assert.ok(html.includes(value),value)
    assert.doesNotMatch(html,/<script>/)
  })
  await context.test('retired/unregistered projections remain readable without a current catalog', async () => {
    const historical = {...profile,status:'retired',sourceProjectionKey:'historical_projection',sourceProjectionConfig:{schema_version:1,old_key:'original'}}
    const html = await render(historical as any,[rule],null)
    assert.match(html,/historical_projection/);assert.match(html,/original/)
    assert.equal(promptConfigurationLabel('constructor'),'constructor')
    assert.equal(promptConfigurationLabel('__proto__'),'__proto__')
  })
  await context.test('current schema exposes exact saved purpose rules and shot markers', async () => {
    const config = {schema_version:5,primitive_options:{reference_context_ir_v1:{rendering:{
      task_separator:' + ',unreferenced_task:'explicit empty task',
      reference_layout:{subject_label_template:'<Subject {index}>',source_separator:', ',summary_reference_template:'{label}: {usage}',shot_reference_template:''},
      reference_rules:[{category:'video',purpose:'custom_purpose',task:'custom task',relationship:'custom retention',usage:'custom usage'}],
      templates:{shot_opening:'[Shot {index}]',shot_transition:'[Shot {index}] At {timecode},'}
    }}}}
    const before = JSON.stringify(config)
    const html = await render({...profile,sourceProjectionConfig:config} as any,[rule])
    for (const value of ['custom_purpose','custom task','custom retention','custom usage','{timecode}','explicit empty task']) assert.ok(html.includes(value),value)
    for (const value of ['主体编号与分段参考布局','主体编号模板','摘要参考模板','逐镜参考模板','Subject {index}']) assert.ok(html.includes(value),value)
    assert.equal(JSON.stringify(config),before)
  })
  await context.test('missing rules and empty/invalid configs are not presented as a complete version', async () => {
    const html = await render(profile,[{...rule,profileVersion:'another-version'}])
    assert.match(html,/字段规则数据不完整/)
    assert.doesNotMatch(html,/完整保存的生成说明/)
    assert.match(await render({...profile,sourceProjectionConfig:{}} as any),/此版本未保存额外来源投影配置/)
    assert.match(await render({...profile,sourceProjectionConfig:null} as any),/来源投影配置格式异常/)
  })
  await context.test('changing the selected version replaces source data and never keeps previous rules', async () => {
    const props = reactive({profile,rules:[rule],options})
    let state:any
    await renderToString(createSSRApp({setup(){state=Viewer.setup(props,{expose:()=>{}});return()=>h('div')}}))
    assert.equal(state.versionRules.value.length,1)
    props.profile = {...profile,id:'next',sourceProjectionConfig:{}} as any
    assert.equal(state.versionRules.value.length,0)
    assert.deepEqual(state.projection.value,{})
  })
  await context.test('both entry points share one snapshot viewer with no save/preview side effects', async () => {
    const source = await readFile(new URL('../src/components/PromptProfileSettings.vue',import.meta.url),'utf8')
    assert.match(source,/@click="openConfiguration\(item\)"/)
    assert.match(source,/@click="openConfiguration\(selectedProfile\)"/)
    assert.match(source,/<PromptProfileConfiguration v-if="configurationSnapshot"/)
    assert.match(source,/@close="configurationSnapshot=null"/)
    const openerStart = source.indexOf('const openConfiguration =')
    const openerEnd = source.indexOf('const profileForm =', openerStart)
    assert.ok(openerStart >= 0 && openerEnd > openerStart,'inspect the actual configuration opener boundary')
    const opener = source.slice(openerStart,openerEnd)
    assert.match(opener,/JSON\.parse\(JSON\.stringify/)
    assert.doesNotMatch(opener,/platformApi|openProfile|preview|save/i)
    const viewer = await readFile(new URL('../src/components/PromptProfileConfiguration.vue',import.meta.url),'utf8')
    assert.doesNotMatch(viewer,/v-model|platformApi|onMounted|watch\(/)
  })
})
