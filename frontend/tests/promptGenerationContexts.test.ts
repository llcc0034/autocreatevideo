import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createSSRApp, h, reactive } from 'vue'
import { renderToString } from 'vue/server-renderer'

const schema = {type:'object',properties:{'a.b':{type:'array',items:{type:'object',properties:{words:{type:'string'},count:{type:'integer'},active:{type:'boolean'}}}}}}
test('context paths share the schema directory, keep types and reject malformed or stale choices', async context => {
  const vite = await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  const module = await vite.ssrLoadModule('/src/services/promptGenerationContexts.ts')
  const selector = module.structuredContextSelector(['a.b',0,'count'])
  assert.equal(selector,'request.structured_input:["a.b",0,"count"]')
  assert.deepEqual(module.structuredContextPath(selector),['a.b',0,'count'])
  const known = new Set(['project.style'])
  assert.equal(module.contextSelectorError(selector,known,schema,true),'')
  assert.equal(module.contextSelectorError('project.style',known,null,false),'')
  for (const value of ['request.structured_input:[false]','request.structured_input:[-1]','request.structured_input:[ "a.b"]','unknown']) {
    assert.notEqual(module.contextSelectorError(value,known,schema,true),'')
  }
  assert.notEqual(module.contextSelectorError(selector,known,{type:'object',properties:{}},true),'')
  assert.notEqual(module.contextSelectorError(selector,known,schema,false),'')
  assert.notEqual(module.contextSelectorError(selector,known,null,true),'')
})

test('real context picker adds multiple explicit paths without replacing preset contexts; disabled and duplicates do not emit', async context => {
  const vite = await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  const {default:Picker}=await vite.ssrLoadModule('/src/components/StructuredGenerationContexts.vue')
  const props=reactive({modelValue:['project.style'],schema,enabled:true,disabled:false})
  const emissions: string[][]=[];let state:any
  await renderToString(createSSRApp({setup(){state=Picker.setup(props,{expose(){},emit(_event:string,value:string[]){emissions.push(value);props.modelValue=value}});return()=>h('div')}}))
  state.add();assert.equal(emissions.length,0,'no implicit root selection')
  state.draft.value=['a.b',0,'words'];state.add()
  state.draft.value=['a.b',0,'count'];state.add()
  assert.deepEqual(props.modelValue,['project.style','request.structured_input:["a.b",0,"words"]','request.structured_input:["a.b",0,"count"]'])
  state.add();assert.equal(emissions.length,2,'duplicate does not emit')
  props.disabled=true;state.remove(props.modelValue[1]);assert.equal(emissions.length,2)
  props.disabled=false;state.remove(props.modelValue[1]);assert.deepEqual(props.modelValue,['project.style','request.structured_input:["a.b",0,"count"]'])
  props.enabled=false;state.draft.value=['a.b',0,'active'];state.add();assert.equal(emissions.length,3)
  const html=await renderToString(createSSRApp({render:()=>h(Picker,props)}))
  assert.match(html,/已有选择不会自动删除/);assert.match(html,/输入字段/);assert.match(html,/count/)
})

test('Profile author serializes and reloads explicit context selections in the existing contract', async context => {
  const vite = await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  const {default:Settings}=await vite.ssrLoadModule('/src/components/PromptProfileSettings.vue')
  let state:any
  await renderToString(createSSRApp({setup(){state=Settings.setup({}, {expose(){}});return()=>h('div')}}))
  const source='request.structured_input', selectors=['request.structured_input:["a.b",0,"count"]']
  state.profiles.value=[{id:'p',status:'draft',operation:'image.generate',sourceProjectionKey:'direct_v1',inputStructureVersion:'s'}]
  state.selectedProfileId.value='p';state.promptOptionsReady.value=true
  const rule={id:'r',profileVersion:'p',sourceSelector:source,sourcePath:['a.b',0,'words'],fieldDefinition:'f',fieldDefinitionVersion:'fv',
    fieldKey:'arbitrary',position:1,generationMode:'model_generated',generationSourceSelector:source,generationContextSelectors:selectors,generationValidatorKeys:[]}
  state.openRule(rule);state.inputStructureSchema.value=schema
  assert.deepEqual(state.buildRulePayload().generation_context_selectors,selectors)
  assert.deepEqual(state.persistedRuleCandidatePayload(rule).generation_context_selectors,selectors)
  assert.equal(state.editorValidation.value.some((error:string)=>error.includes('上下文')||error.includes('附加字段')),false)
  state.inputStructureSchema.value={type:'object',properties:{}}
  assert.deepEqual(state.ruleForm.generationContextSelectors,selectors,'invalid choices are retained for explicit correction')
  assert.equal(state.editorValidation.value.some((error:string)=>error.includes('路径未在来源结构')),true)
})

test('the same author permits generated final fields for text operations and independently selected structured context', async context => {
  const vite = await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  const {default:Settings}=await vite.ssrLoadModule('/src/components/PromptProfileSettings.vue')
  let state:any
  await renderToString(createSSRApp({setup(){state=Settings.setup({}, {expose(){}});return()=>h('div')}}))
  state.options.value.generationLeafOperations=['prompt.fields.generate','prompt.fields.translate']
  state.options.value.generationSourceSelectors=[{value:'projection.output',label:'本字段装配原文'},{value:'request.text',label:'原文'}]
    .map(row=>({...row,status:'active',generationModes:['model_generated','translate_only']}))
  state.promptOptionsReady.value=true
  for(const operation of ['script.generate','script.structure','visual_rules.generate','image.generate','video.generate']) {
    state.profiles.value=[{id:'p',status:'draft',operation,sourceProjectionKey:'declarative_v1',inputStructureVersion:'s'}]
    state.selectedProfileId.value='p'
    state.openRule({id:'r',profileVersion:'p',sourceSelector:'projection.output',projectionPrimitive:'reference_context.summary',
      fieldDefinition:'f',fieldDefinitionVersion:'fv',fieldKey:'custom',position:1,channel:'positive_prompt',
      generationMode:'model_generated',generationOutputSemantics:'final',generationSourceSelector:'projection.output',
      generationInstruction:'Use supplied facts.',generationContextSelectors:['request.structured_input:["a.b",0,"count"]'],generationValidatorKeys:[]})
    state.inputStructureSchema.value=schema
    assert.equal(state.selectedProfileSupportsModelGeneration.value,true,operation)
    assert.equal(state.structuredModelSourceAvailable.value,true)
    assert.equal(state.compatibleGenerationSourceOptions.value.some((row:any)=>row.value==='projection.output'),true)
    assert.equal(state.editorValidation.value.some((error:string)=>error.includes('附加字段')||error.includes('上下文')),false)
    assert.equal(state.buildRulePayload().generation_output_semantics,'final')
    state.ruleForm.channel='system_instruction'
    assert.equal(state.selectedProfileSupportsModelGeneration.value,false)
  }
  state.ruleForm.channel='positive_prompt'
  state.profiles.value=[{id:'p',operation:'prompt.fields.generate',sourceProjectionKey:'direct_v1'}]
  assert.equal(state.selectedProfileSupportsModelGeneration.value,false,'leaf operation cannot recursively generate itself')
})
