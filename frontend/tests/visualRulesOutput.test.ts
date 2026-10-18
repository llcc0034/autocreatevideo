import assert from 'node:assert/strict'
import test from 'node:test'
import {scriptContractFixture} from './helpers/scriptContractFixture.ts'
import {createServer} from 'vite'
import {visualRulesOutputRaw} from './visualRulesOutputFixture.ts'
async function setup(context:any){const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}});context.after(()=>vite.close());return vite}

test('visual output catalog drives bounds, preserves omission and zero, and rejects malformed values',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/visualRulesOutput.ts'),options=m.adaptVisualRulesOutputOptions(visualRulesOutputRaw)
  for(const raw of [undefined,{}, {...visualRulesOutputRaw,schema_version:true},{...visualRulesOutputRaw,number_fields:[]},
    {...visualRulesOutputRaw,number_fields:[visualRulesOutputRaw.number_fields[0],visualRulesOutputRaw.number_fields[0]]},
    {...visualRulesOutputRaw,number_pairs:[['wrong','base_colors_max_items']]},{...visualRulesOutputRaw,outputs:[{}]},
    {...visualRulesOutputRaw,color_choices:{color:['teal','teal']}}])assert.throws(()=>m.adaptVisualRulesOutputOptions(raw))
  const value=m.updateVisualRulesAdvisory(undefined,options,'negative_constraints_max_items',0)
  assert.deepEqual(value,{schema_version:1,negative_constraints_max_items:0})
  assert.deepEqual(m.visualRulesAdvisoryErrors(value,options),[])
  assert.equal(m.updateVisualRulesAdvisory(value,options,'negative_constraints_max_items',undefined),undefined)
  assert.deepEqual(m.visualRulesAdvisoryErrors(undefined,options),[])
  assert.ok(m.visualRulesAdvisoryErrors(undefined).length)
  for(const raw of [null,false,[],{schema_version:true},{schema_version:1,extra:1},{schema_version:1,base_colors_min_items:22},
    {schema_version:1,continuity_summary_max_chars:0},{schema_version:1,base_colors_min_items:3,base_colors_max_items:2},
    ...[false,[''],['中文'],['a,b'],['line\nbreak'],['<b>x</b>'],['identity drift','Identity Drift']].map(v=>({schema_version:1,recommended_negative_clauses:v})),
  ])assert.ok(m.visualRulesAdvisoryErrors(raw,options).length,JSON.stringify(raw))
  options.numberFields.find((v:any)=>v.key==='continuity_summary_max_chars').maximum=8
  assert.ok(m.visualRulesAdvisoryErrors({schema_version:1,continuity_summary_max_chars:9},options).length)
  options.clausesMaxBytes=5
  assert.ok(m.visualRulesAdvisoryErrors({schema_version:1,recommended_negative_clauses:['long phrase']},options).length)
  assert.throws(()=>m.updateVisualRulesAdvisory([],options,'base_colors_max_items',3))
  assert.throws(()=>m.updateVisualRulesAdvisory(value,options,'unknown',3))
  const original={visual_rules:{quality_advisories:value,timeout_seconds:1}}
  m.assertVisualRulesAdvisoriesUnchanged(original,{visual_rules:{quality_advisories:{negative_constraints_max_items:0,schema_version:1},timeout_seconds:2}})
  assert.throws(()=>m.assertVisualRulesAdvisoriesUnchanged(original,{visual_rules:{}}),/固定表单/)
})

test('output form supports typed edits, clauses, readonly states and shows the reviewed source chain',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/visualRulesOutput.ts'),{default:Editor}=await vite.ssrLoadModule('/src/components/ModelVisualRulesOutputEditor.vue')
  const {createSSRApp,h,reactive}=await import('vue'),{renderToString}=await import('vue/server-renderer')
  const props=reactive({modelValue:undefined as any,options:m.adaptVisualRulesOutputOptions(visualRulesOutputRaw),disabled:false})
  let state:any,updates=0
  await renderToString(createSSRApp({setup(){state=Editor.setup(props,{expose:()=>{},emit:(event:string,value:unknown)=>{if(event==='update:modelValue'){props.modelValue=value;updates++}}});return()=>h('div')}}))
  state.number('base_colors_min_items','0');state.number('base_colors_max_items','3')
  state.clauseDraft.value='identity drift';state.addClause();state.clauseDraft.value='readable text';state.addClause()
  state.clause(1,'blurred text');assert.deepEqual(props.modelValue.recommended_negative_clauses,['identity drift','blurred text'])
  state.removeClause(0);assert.deepEqual(state.errors.value,[])
  state.number('base_colors_min_items','');assert.equal(props.modelValue.base_colors_min_items,undefined)
  const html=await renderToString(createSSRApp({render:()=>h(Editor,props)}))
  assert.match(html,/type="number"/);assert.match(html,/reviewed_visual_rules.color_script/);assert.match(html,/color_design/);assert.match(html,/不直接判失败/);assert.doesNotMatch(html,/<textarea/)
  assert.equal(state.previewLoaded.value,false)
  state.onPreviewToggle({target:{open:false}});assert.equal(state.previewLoaded.value,false)
  state.onPreviewToggle({target:{open:true}});assert.equal(state.previewLoaded.value,true)
  state.onPreviewToggle({target:{open:false}});assert.equal(state.previewLoaded.value,true)
  const before=updates;props.disabled=true;state.number('base_colors_max_items','5');state.clause(0,'x');state.removeClause(0);state.addClause();state.clear();assert.equal(updates,before)
  assert.match(await renderToString(createSSRApp({render:()=>h(Editor,props)})),/<fieldset disabled/)
  props.disabled=false;props.options=undefined;state.number('base_colors_max_items','5');assert.equal(updates,before);assert.ok(state.errors.value.length)
})

test('registry renders output form only for declared or saved policy, blocks JSON second writer',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/visualRulesOutput.ts'),{default:Editor}=await vite.ssrLoadModule('/src/components/RegistryResourceEditor.vue')
  const {adaptScriptContracts}=await vite.ssrLoadModule('/src/services/modelScriptContracts.ts')
  const {createSSRApp,h,reactive}=await import('vue'),{renderToString}=await import('vue/server-renderer')
  const props=reactive({resource:'model',modelValue:{modality:'text',specification:{capabilities:['visual_rules.generate'],visual_rules:{quality_advisories:{schema_version:1,base_colors_max_items:3}}}} as any,
    dependencies:{providers:[],modelProfiles:[],deployments:[],runtimeHosts:[],workflowVersions:[],modelProfileOptions:{visualRulesOutput:m.adaptVisualRulesOutputOptions(visualRulesOutputRaw),scriptContracts:adaptScriptContracts(scriptContractFixture),capabilities:[],providerDrivers:[],modalities:[],modelFamilies:[],artifactRoles:[]}},disabled:false})
  let state:any
  await renderToString(createSSRApp({setup(){state=Editor.setup(props,{expose:()=>{},emit:(event:string,value:unknown)=>{if(event==='update:modelValue')props.modelValue=value}});return()=>h('div')}}))
  const old=JSON.stringify(props.modelValue.specification)
  state.updateJson({path:'specification',label:'完整模型规格'},{target:{value:JSON.stringify({capabilities:['visual_rules.generate'],visual_rules:{}})}})
  assert.equal(JSON.stringify(props.modelValue.specification),old);assert.match(state.jsonErrors.specification,/固定表单/)
  state.updateJson({path:'specification',label:'完整模型规格'},{target:{value:JSON.stringify({...props.modelValue.specification,unrelated:'preserved'})}})
  assert.equal(props.modelValue.specification.unrelated,'preserved');assert.equal(state.jsonErrors.specification,'')
  const beforeMissingCatalog=JSON.stringify(props.modelValue.specification)
  props.dependencies.modelProfileOptions.scriptContracts=undefined
  state.updateJson({path:'specification',label:'完整模型规格'},{target:{value:JSON.stringify({...props.modelValue.specification,unrelated:'blocked'})}})
  assert.equal(JSON.stringify(props.modelValue.specification),beforeMissingCatalog)
  assert.match(state.jsonErrors.specification,/传输设置目录未读取/)
  assert.ok(!state.advancedFields.value.some((field:any)=>field.path==='specification.visual_rules.quality_advisories'))
  assert.match(await renderToString(createSSRApp({render:()=>h(Editor,props)})),/影视规则输出与质量建议/)
  props.modelValue={modality:'image',specification:{capabilities:[]}}
  assert.equal(state.showVisualRules.value,false)
})
