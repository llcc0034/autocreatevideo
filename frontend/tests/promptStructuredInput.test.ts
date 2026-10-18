import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createSSRApp, h, reactive } from 'vue'
import { renderToString } from 'vue/server-renderer'

test('renderer positions save exact paths, preserve invalid bindings and never guess source names', async context => {
  const vite = await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  const {default:Settings}=await vite.ssrLoadModule('/src/components/PromptProfileSettings.vue')
  let state:any
  await renderToString(createSSRApp({setup(){state=Settings.setup({}, {expose(){}});return()=>h('div')}}))
  state.options.value.referenceRenderingTextSlots=[{value:'action',label:'动作'}]
  state.profileForm.sourceProjectionKey='declarative_v1'
  state.profileForm.inputStructureVersion='exact-v2'
  state.loadDeclarativeProjectionConfig(reactive({primitive_options:{reference_context_ir_v1:{rendering:{unit_text_bindings:{action:['a.b',0,'words']}}}}}))
  const schema={type:'object',properties:{'a.b':{type:'array',items:{type:'object',properties:{words:{type:'string'},count:{type:'integer'}}}}}}
  state.inputStructureSchema.value=schema
  assert.equal(state.hasRenderBinding('action'),true)
  const payload=state.buildProfilePayload()
  assert.deepEqual(payload.source_projection_config.primitive_options.reference_context_ir_v1.rendering.unit_text_bindings,{action:['a.b',0,'words']})
  assert.equal(state.editorValidation.value.some((text:string)=>text.startsWith('action：')),false)
  state.profileForm.renderingUnitTextBindings.action=['a.b',0,'count']
  assert.equal(state.editorValidation.value.some((text:string)=>text.includes('只能选择文本路径')),true)
  state.inputStructureSchema.value={type:'object',properties:{different:{type:'string'}}}
  assert.deepEqual(state.profileForm.renderingUnitTextBindings.action,['a.b',0,'count'],'changing structure does not silently clear a path')
  state.profileForm.renderingUnitTextBindings.unknown=['x']
  assert.deepEqual(state.unknownRenderSlots.value,['unknown'])
  assert.equal(state.editorValidation.value.some((text:string)=>text.includes('未登记的原文渲染位置')),true)
  state.toggleRenderBinding('unknown');state.toggleRenderBinding('action')
  assert.deepEqual(state.profileForm.renderingUnitTextBindings,{})
  state.toggleRenderBinding('action')
  assert.deepEqual(state.profileForm.renderingUnitTextBindings.action,[],'root is an explicit draft selection, not guessed first text field')
})

test('Profile structure, rule paths and both preview candidates use the same exact contract', async context => {
  const vite = await createServer({ logLevel:'silent', server:{middlewareMode:true,hmr:false}, appType:'custom', optimizeDeps:{noDiscovery:true} })
  context.after(()=>vite.close())
  const {default:Settings} = await vite.ssrLoadModule('/src/components/PromptProfileSettings.vue')
  let state:any
  await renderToString(createSSRApp({setup(){state=Settings.setup({}, {expose(){}});return()=>h('div')}}))
  const source='request.structured_input'
  const profile={id:'profile',profileKey:'custom',version:1,name:'自定义结构',status:'draft',inputStructureVersion:'exact-structure-v2',
    sourceProjectionKey:'direct_v1',sourceProjectionConfig:{},channelLimits:{},modelFamily:'cloud',operation:'video.generate',mode:'T2VA'}
  state.profiles.value=[profile];state.selectedProfileId.value=profile.id;state.promptOptionsReady.value=true
  state.openProfile(profile)
  assert.equal(state.profileForm.inputStructureVersion,'exact-structure-v2')
  assert.equal(state.buildProfilePayload().input_structure_version,'exact-structure-v2')
  assert.equal(state.buildPreviewCandidate().input_structure_version,'exact-structure-v2')
  state.profileForm.inputStructureVersion=''
  assert.equal(state.buildProfilePayload().input_structure_version,null,'explicit clear is preserved')
  const common={profileVersion:profile.id,sourceSelector:source,sourcePath:['a.b',0,'active'],fieldDefinition:'field',fieldDefinitionVersion:'field-v1',
    fieldKey:'flag',fieldLabel:'启用',position:1,generationContextSelectors:[],generationValidatorKeys:[],generationMode:'passthrough',generationSourceSelector:source}
  const rule={...common,id:'rule-one'}
  state.rules.value=[rule,{...common,id:'rule-two',fieldKey:'other',position:2}]
  state.openRule(rule)
  assert.deepEqual(state.ruleForm.sourcePath,['a.b',0,'active'])
  assert.deepEqual(state.buildRulePayload().source_path,['a.b',0,'active'])
  assert.deepEqual(state.persistedRuleCandidatePayload(rule).source_path,['a.b',0,'active'])
  assert.equal(state.buildPreviewCandidate().input_structure_version,'exact-structure-v2')
  assert.equal(state.previewSourceRows.value.filter((row:any)=>row.selector===source).length,1)
  state.seedPreviewExamples([{selector:source,inputKey:source,field:{previewExample:'leaf example'}}])
  assert.equal(Object.hasOwn(state.previewDraft.value,source),false,'leaf example is never a root input')
  for(const value of [{'a.b':[{active:false,count:0}]},null,false,0,[]]){
    state.setPreviewInput(source,value)
    assert.deepEqual(state.buildPreviewSources()[source],value)
  }
  state.setPreviewInput(source,undefined)
  assert.equal(Object.hasOwn(state.buildPreviewSources(),source),false)
  state.previewStructureDraftErrors.value=['无效数字']
  const oldReset=state.previewStructureReset.value
  state.resetPreviewInput({selector:source,inputKey:source})
  assert.deepEqual(state.previewStructureDraftErrors.value,[])
  assert.equal(state.previewStructureReset.value,oldReset+1,'clear remounts root editor to reset internal invalid draft')
  const {dataStructures}=await vite.ssrLoadModule('/src/services/dataStructures.ts')
  dataStructures.schema=async()=>{throw Error('structure unavailable')}
  await state.loadInputStructure()
  assert.match(state.inputStructureError.value,/unavailable/)
  const root={type:'object',properties:{value:{type:'string'}}}
  dataStructures.schema=async(id:string)=>{assert.equal(id,'exact-structure-v2');return {valid:true,json_schema:root,errors:{}}}
  await state.loadInputStructure()
  assert.equal(state.inputStructureError.value,'')
  assert.deepEqual(state.inputStructureSchema.value,root)
  state.ruleForm.sourceSelector='request.theme'
  assert.equal(state.buildRulePayload().source_path,null)
  const adapters=await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  assert.equal(adapters.adaptPromptProfileVersion({input_structure_version:'s-v2'}).inputStructureVersion,'s-v2')
  assert.deepEqual(adapters.adaptPromptProfileFieldRule({source_path:[]}).sourcePath,[])
  assert.deepEqual(adapters.adaptPromptProfileFieldRule({source_path:['a.b',0]}).sourcePath,['a.b',0])
  for(const source_path of ['a.b',[-1],[false]]) assert.throws(()=>adapters.adaptPromptProfileFieldRule({source_path}),/路径/)
})

test('shared schema path selector handles dotted names, exact indices and nonexistent paths',async context=>{
  const vite=await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  const {default:Select}=await vite.ssrLoadModule('/src/components/SchemaPathSelect.vue')
  const schema={type:'object',properties:{'a.b':{type:'array',items:{type:'object',properties:{active:{type:'boolean'}}}}}}
  const render=(modelValue:unknown)=>renderToString(createSSRApp({render:()=>h(Select,{schema,modelValue,label:'装配字段路径'})}))
  const html=await render(['a.b',0,'active'])
  assert.match(html,/a.b/);assert.match(html,/active/);assert.doesNotMatch(html,/路径未在来源结构/)
  assert.match(await render(['wrong']),/路径未在来源结构/)
  assert.match(await render(null),/明确的字段路径/)
})

test('structured model and translation modes share one exact source path and reject divergent sources',async context=>{
  const vite=await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  const {default:Settings}=await vite.ssrLoadModule('/src/components/PromptProfileSettings.vue')
  let state:any
  await renderToString(createSSRApp({setup(){state=Settings.setup({}, {expose(){}});return()=>h('div')}}))
  const source='request.structured_input'
  state.profiles.value=[{id:'p',status:'draft',operation:'video.generate',sourceProjectionKey:'direct_v1',inputStructureVersion:'s'}]
  state.selectedProfileId.value='p';state.promptOptionsReady.value=true;state.editorKind.value='rule'
  state.fields.value=[{id:'f',isActive:true}]
  state.selectRuleVersion({id:'v',field_definition:'f',field_key:'custom',version:1,status:'published',value_type:'text',value_options:[]})
  state.inputStructureSchema.value={type:'object',properties:{'a.b':{type:'string'}}}
  state.options.value.languagePolicies=[{value:'en',label:'英文'}]
  Object.assign(state.ruleForm,{fieldDefinition:'f',fieldDefinitionVersion:'v',sourceSelector:source,sourcePath:['a.b'],
    generationSourceSelector:source,generationMode:'passthrough',generationOutputSemantics:'final',generationInstruction:'Use selected text.',
    generationContextSelectors:[],generationValidatorKeys:[],languagePolicy:'en',lengthUnit:'none',channel:'positive_prompt',position:1,maxChars:4000})
  for(const mode of ['model_generated','translate_only']){
    state.rememberGenerationModeBeforeUserChange();state.ruleForm.generationMode=mode;state.onRuleGenerationModeChange()
    assert.equal(state.ruleForm.generationSourceSelector,source)
    assert.equal(state.generationSourceUnknown.value,false)
    assert.equal(state.structuredGenerationSourceMode.value,true)
    assert.deepEqual(state.editorValidation.value,[])
    assert.deepEqual(state.buildRulePayload().source_path,['a.b'])
    assert.match(state.resolvedGenerationSourceLabel(mode,source,source),/字段路径/)
  }
  state.ruleForm.generationSourceSelector='shot.action'
  assert.match(state.editorValidation.value.join(' '),/不能另选/)
  state.ruleForm.generationSourceSelector=source;state.ruleForm.generationOutputSemantics='contribution'
  assert.match(state.editorValidation.value.join(' '),/最终字段.*逐单元贡献/)
  state.ruleForm.generationOutputSemantics='final'
  state.inputStructureSchema.value={type:'object',properties:{'a.b':{type:'boolean'}}}
  assert.match(state.editorValidation.value.join(' '),/结构中明确的文本路径/)
  state.selectedRuleVersion.value={...state.selectedRuleVersion.value,value_type:'integer'}
  assert.match(state.editorValidation.value.join(' '),/必须为文本路径/)
  state.ruleForm.sourceSelector='request.text';state.ruleForm.generationMode='passthrough'
  state.rememberGenerationModeBeforeUserChange();state.ruleForm.generationMode='model_generated';state.onRuleGenerationModeChange()
  assert.equal(state.ruleForm.generationSourceSelector,'','unstructured sources still require explicit source selection')
})

test('declarative contributions select an exact unit path independently from the render destination', async context => {
  const vite=await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  const {default:Settings}=await vite.ssrLoadModule('/src/components/PromptProfileSettings.vue')
  let state:any
  await renderToString(createSSRApp({setup(){state=Settings.setup({}, {expose(){}});return()=>h('div')}}))
  const source='request.structured_input'
  const primitive={value:'reference_context.detailed_description',engineKey:'reference_context_ir_v1',contributionSourceSelectors:['shot.detailed_description']}
  state.profiles.value=[{id:'p',status:'draft',operation:'video.generate',sourceProjectionKey:'declarative_v1',inputStructureVersion:'s',sourceProjectionConfig:{primitive_options:{reference_context_ir_v1:{}}}}]
  state.selectedProfileId.value='p';state.promptOptionsReady.value=true;state.editorKind.value='rule'
  state.fields.value=[{id:'f',isActive:true}]
  state.selectRuleVersion({id:'v',field_definition:'f',field_key:'custom',version:1,status:'published',value_type:'text',value_options:[]})
  state.inputStructureSchema.value={type:'object',properties:{'a.b':{type:'string'}}}
  state.options.value.languagePolicies=[{value:'en',label:'英文'}]
  state.options.value.sourceSelectors=[{value:source},{value:'projection.output'}]
  state.options.value.sourceProjectionPrimitives=[primitive]
  Object.assign(state.ruleForm,{fieldDefinition:'f',fieldDefinitionVersion:'v',sourceSelector:'projection.output',sourcePath:['a.b'],
    projectionPrimitive:primitive.value,generationSourceSelector:source,generationMode:'model_generated',generationOutputSemantics:'contribution',
    generationInstruction:'Use this unit only.',generationContextSelectors:[],generationValidatorKeys:[],languagePolicy:'en',
    lengthUnit:'none',channel:'positive_prompt',position:1,maxChars:4000})
  assert.equal(state.structuredContributionAvailable.value,true)
  assert.equal(state.generationSourceUnknown.value,false)
  assert.equal(state.usesStructuredPath.value,true)
  assert.deepEqual(state.editorValidation.value,[])
  const payload=state.buildRulePayload()
  assert.deepEqual(payload.source_path,['a.b'])
  assert.equal(payload.source_selector,'projection.output')
  assert.equal(payload.generation_source_selector,source)
  assert.equal(payload.projection_primitive,primitive.value)
  state.ruleForm.generationMode='translate_only'
  assert.deepEqual(state.editorValidation.value,[])
  state.options.value.generationSourceSelectors=[{value:'shot.detailed_description',status:'active',generationModes:['translate_only']}]
  state.ruleForm.generationSourceSelector='shot.detailed_description'
  assert.deepEqual(state.compatibleGenerationSourceOptions.value,[],'render destinations cannot become unit input sources')
  assert.match(state.editorValidation.value.join(' '),/逐单元结构化来源/)
  state.ruleForm.generationSourceSelector=source
  assert.deepEqual(state.editorValidation.value,[])
  state.ruleForm.sourcePath=['missing']
  assert.match(state.editorValidation.value.join(' '),/路径/)
  state.ruleForm.sourcePath=['a.b'];state.options.value.sourceProjectionPrimitives=[{...primitive,contributionSourceSelectors:[]}]
  assert.equal(state.structuredContributionAvailable.value,false)
  assert.match(state.editorValidation.value.join(' '),/最终字段.*逐单元贡献/)
})
