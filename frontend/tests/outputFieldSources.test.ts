import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {visualRulesOutputRaw,outputFieldSourceOptions,outputResponseKeyPolicy} from './visualRulesOutputFixture.ts'
async function setup(context:any){const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}});context.after(()=>vite.close());return vite}
test('optional output is explicitly added and removed without changing the frozen three-field example',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/outputFieldSources.ts'),v=await vite.ssrLoadModule('/src/services/visualRulesOutput.ts')
  const optional={field:'camera_guidance',label:'已审核镜头指导',optional:true,choices:[{response_field:'camera_guidance',value_type:'text',transformation:'保留审核结果',example:'Keep eye-level framing.'}]}
  const options=m.adaptOutputFieldSourceOptions([...outputFieldSourceOptions,optional]),base=v.adaptVisualRulesOutputOptions(visualRulesOutputRaw)
  const original=m.outputFieldSources({fields:visualRulesOutputRaw.outputs}),sources={...original,camera_guidance:'camera_guidance'}
  assert.deepEqual(m.outputFieldSourceErrors(original,options,{}),[])
  assert.deepEqual(m.outputFieldSourceErrors(sources,options,{}),[])
  assert.ok(m.outputFieldSourceErrors({...sources,custom:'invented'},options,{}).length)
  assert.ok(m.outputFieldSourceErrors({camera_guidance:'camera_guidance'},options,{}).length)
  assert.throws(()=>m.adaptOutputFieldSourceOptions([{...optional,optional:'yes'}]))
  const added=m.draftOutputSourceOptions(base,sources,options)
  assert.equal(added.outputs.length,4);assert.equal(added.responseExample.camera_guidance,optional.choices[0].example)
  assert.equal(added.outputs[3].sourceSelector,'reviewed_visual_rules.camera_guidance')
  assert.deepEqual(m.draftOutputSourceOptions(added,sources,options),added)
  assert.deepEqual(m.draftOutputSourceOptions(added,original,options),base)
  assert.deepEqual(base.responseExample,visualRulesOutputRaw.response_example)
})
test('fixed array positions roundtrip through frozen versions and reject sparse or mixed containers',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/outputFieldSources.ts'),v=await vite.ssrLoadModule('/src/services/visualRulesOutput.ts')
  const {outputVersionOptions}=await vite.ssrLoadModule('/src/services/businessOutputs.ts')
  const {responseValueAt,packResponseExample,validateResponseExample}=await vite.ssrLoadModule('/src/services/outputResponseLayout.ts')
  const policy=m.adaptOutputResponseKeyPolicy(outputResponseKeyPolicy),sources=m.outputFieldSources({fields:visualRulesOutputRaw.outputs})
  const base=v.adaptVisualRulesOutputOptions(visualRulesOutputRaw)
  const keys={continuity_summary:'rules.0.summary',color_script:'rules.1.palette',negative_prompt:'rules.2.avoid'}
  assert.deepEqual(m.outputResponseKeyErrors(keys,sources,policy),[])
  const array=m.draftOutputSourceOptions(base,sources,outputFieldSourceOptions,keys)
  assert.deepEqual(array.responseExample,{schema_version:2,rules:[{summary:base.responseExample.continuity_summary},{palette:base.responseExample.color_design},{avoid:base.responseExample.negative_constraints}]})
  assert.deepEqual(m.draftOutputSourceOptions(array,sources,outputFieldSourceOptions,{}),base)
  assert.equal(responseValueAt(array.responseExample,keys.continuity_summary),base.responseExample.continuity_summary)
  const version={parser_key:'visual_rules.v2',parser_contract:{key:'visual_rules.v2',operation:'visual_rules.generate',response_schema_version:2,fields:visualRulesOutputRaw.outputs,response_keys:keys,response_example:array.responseExample}}
  assert.deepEqual(outputVersionOptions(base,version),array)
  for(const items of [null,{},[],array.responseExample.rules.slice(0,2),[...array.responseExample.rules,null],{...array.responseExample.rules},[null,...array.responseExample.rules.slice(1)]]){
    assert.throws(()=>outputVersionOptions(base,{...version,parser_contract:{...version.parser_contract,response_example:{schema_version:2,rules:items}}}))
  }
  for(const key of ['rules.3.summary','rules.999999999999999999999999','rules.01','rules.-1','rules.+0','rules.1e0','rules.name','rules.1','0.rules','rules.٠'])assert.ok(m.outputResponseKeyErrors({...keys,continuity_summary:key},sources,policy).length,key)
  const nestedKeys={continuity_summary:'rules.0.0',color_script:'rules.0.1',negative_prompt:'rules.1'}
  const nested=m.draftOutputSourceOptions(array,sources,outputFieldSourceOptions,nestedKeys)
  assert.deepEqual(m.draftOutputSourceOptions(nested,sources,outputFieldSourceOptions,keys),array)
  assert.throws(()=>responseValueAt({rules:{0:{summary:'not an array'}}},keys.continuity_summary))
  assert.throws(()=>packResponseExample(2,[{path:'rules.999999999999999999999999',value:null}]))
  const sparse=structuredClone(array.responseExample);delete sparse.rules[0]
  assert.throws(()=>validateResponseExample(sparse,Object.values(keys)))
  assert.throws(()=>responseValueAt(sparse,keys.continuity_summary))
})
test('nested object mapping shares one frozen tree across edits and rejects unknown containers',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/outputFieldSources.ts'),v=await vite.ssrLoadModule('/src/services/visualRulesOutput.ts')
  const {outputVersionOptions}=await vite.ssrLoadModule('/src/services/businessOutputs.ts')
  const policy=m.adaptOutputResponseKeyPolicy(outputResponseKeyPolicy),sources=m.outputFieldSources({fields:visualRulesOutputRaw.outputs})
  const base=v.adaptVisualRulesOutputOptions(visualRulesOutputRaw)
  const keys={continuity_summary:'visual.rules.summary',color_script:'visual.palette',negative_prompt:'constraints.avoid'}
  assert.deepEqual(m.outputResponseKeyErrors(keys,sources,policy),[])
  const nested=m.draftOutputSourceOptions(base,sources,outputFieldSourceOptions,keys)
  assert.deepEqual(nested.responseExample,{schema_version:2,visual:{rules:{summary:base.responseExample.continuity_summary},palette:base.responseExample.color_design},constraints:{avoid:base.responseExample.negative_constraints}})
  assert.deepEqual(m.draftOutputSourceOptions(nested,sources,outputFieldSourceOptions,keys),nested)
  assert.deepEqual(m.draftOutputSourceOptions(nested,sources,outputFieldSourceOptions,{}),base)
  const swapped=m.draftOutputSourceOptions(nested,sources,outputFieldSourceOptions,{...keys,color_script:keys.continuity_summary,continuity_summary:keys.color_script})
  assert.deepEqual(swapped.responseExample.visual.rules.summary,base.responseExample.color_design)
  const version={parser_key:'visual_rules.v2',parser_contract:{key:'visual_rules.v2',operation:'visual_rules.generate',response_schema_version:2,fields:visualRulesOutputRaw.outputs,response_keys:keys,response_example:nested.responseExample}}
  assert.deepEqual(outputVersionOptions(base,version),nested)
  for(const sample of [{...nested.responseExample,extra:1},{...nested.responseExample,visual:null},
    {...nested.responseExample,visual:{...nested.responseExample.visual,extra:1}},
    {...nested.responseExample,visual:{...nested.responseExample.visual,rules:{}}}])assert.throws(()=>outputVersionOptions(base,{...version,parser_contract:{...version.parser_contract,response_example:sample}}))
  assert.ok(m.outputResponseKeyErrors({...keys,color_script:'visual'},sources,policy).length)
  assert.ok(m.outputResponseKeyErrors({...keys,color_script:'visual.rules.summary.child'},sources,policy).length)
  assert.deepEqual(base.responseExample,visualRulesOutputRaw.response_example)
})
test('authored keys use server rules, remap one example and preserve exact downstream semantics',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/outputFieldSources.ts'),v=await vite.ssrLoadModule('/src/services/visualRulesOutput.ts')
  const policy=m.adaptOutputResponseKeyPolicy(outputResponseKeyPolicy),sources=m.outputFieldSources({fields:visualRulesOutputRaw.outputs})
  const keys={continuity_summary:'rules',color_script:'palette',negative_prompt:'avoid'}
  assert.deepEqual(m.outputResponseKeyErrors(keys,sources,policy),[])
  for(const key of ['rules','rules.child','schema_version','a.schema_version','constructor','prototype','__proto__','a.b.c.d.e','a..b','a[0]','$.value','Name','a'.repeat(65),'tail\n',''])assert.ok(m.outputResponseKeyErrors({...keys,color_script:key},sources,policy).length)
  assert.ok(m.outputResponseKeyErrors({continuity_summary:'rules'},sources,policy).length)
  assert.throws(()=>m.adaptOutputResponseKeyPolicy({}))
  assert.ok(m.outputResponseKeyErrors({},sources,null).length)
  const base=v.adaptVisualRulesOutputOptions(visualRulesOutputRaw)
  const named=m.draftOutputSourceOptions(base,sources,outputFieldSourceOptions,keys)
  assert.equal(named.responseExample.palette.lighting,'soft_diffuse');assert.equal(named.outputs[1].responseField,'palette');assert.equal(named.outputs[1].sourceFormat,'color_design')
  assert.equal(named.outputs[1].sourceSelector,'reviewed_visual_rules.color_script')
  assert.deepEqual(m.draftOutputSourceOptions(named,sources,outputFieldSourceOptions,keys),named)
  assert.deepEqual(m.draftOutputSourceOptions(named,sources,outputFieldSourceOptions,{}),base)
  const direct=m.draftOutputSourceOptions(named,{...sources,color_script:'color_script'},outputFieldSourceOptions,keys)
  assert.equal(direct.responseExample.palette,'Original color description.')
  const swapped=m.draftOutputSourceOptions(base,sources,outputFieldSourceOptions,{...sources,continuity_summary:'color_design',color_script:'continuity_summary'})
  assert.equal(swapped.responseExample.color_design,base.responseExample.continuity_summary)
  assert.deepEqual(swapped.responseExample.continuity_summary,base.responseExample.color_design)
  const {outputVersionOptions}=await vite.ssrLoadModule('/src/services/businessOutputs.ts')
  const saved=outputVersionOptions(base,{parser_key:'visual_rules.v2',parser_contract:{key:'visual_rules.v2',operation:'visual_rules.generate',response_schema_version:2,fields:visualRulesOutputRaw.outputs,response_keys:keys,response_example:named.responseExample}})
  assert.deepEqual(saved,named)
})
test('controlled source mapping retains frozen metadata, exposes type changes and refuses implicit policy cleanup',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/outputFieldSources.ts'),v=await vite.ssrLoadModule('/src/services/visualRulesOutput.ts')
  const options=m.adaptOutputFieldSourceOptions(outputFieldSourceOptions)
  for(const invalid of [undefined,[],[options[0],options[0]],[{...options[0],choices:[{response_field:'bad'}]}]])assert.throws(()=>m.adaptOutputFieldSourceOptions(invalid))
  const sources=m.outputFieldSources({fields:visualRulesOutputRaw.outputs}),direct={...sources,color_script:'color_script'}
  assert.deepEqual(m.outputFieldSourceErrors(direct,options,{}),[])
  for(const [mapping,allowed,quality] of [[{}, {}, {}],[{...sources,color_script:'$.path'}, {}, {}],[direct,{lighting:['low_key']},{}],[direct,{}, {base_colors_min_items:0}]])assert.ok(m.outputFieldSourceErrors(mapping,options,allowed,quality).length)
  const base=v.adaptVisualRulesOutputOptions(visualRulesOutputRaw);base.outputs[0].label='Frozen name';base.responseExample.continuity_summary='Frozen continuity'
  assert.deepEqual(m.draftOutputSourceOptions(base,sources,options),base)
  const draft=m.draftOutputSourceOptions(base,direct,options)
  assert.equal(draft.outputs[0].label,'Frozen name');assert.equal(draft.responseExample.continuity_summary,'Frozen continuity')
  assert.equal(draft.responseExample.color_script,'Original color description.');assert.equal('color_design' in draft.responseExample,false)
  assert.equal('color_design' in base.responseExample,true)
  assert.equal(draft.outputs[1].sourceSelector,'reviewed_visual_rules.color_script')
})
test('draft preview sends selected source mapping; saved preview sends only exact frozen identity',async context=>{
  const vite=await setup(context),{previewVisualRulesOutput}=await vite.ssrLoadModule('/src/services/visualRulesOutputPreview.ts'),{api}=await vite.ssrLoadModule('/src/services/api.ts')
  const sources={continuity_summary:'continuity_summary',color_script:'color_script',negative_prompt:'negative_constraints'},calls:any[]=[]
  const version={id:'saved-version',revision:2,sha256:'a'.repeat(64)}
  api.post=async(path:any,data:any)=>{calls.push(data);return {data:{preview_only:true,response_schema_version:2,valid:false,candidate:null,warnings:[],errors:[{code:'invalid',message:'invalid sample'}],output_version_id:version.id,output_revision:2,output_contract_sha256:version.sha256}}}
  const keys={continuity_summary:'rules',color_script:'palette',negative_prompt:'avoid'}
  await previewVisualRulesOutput('{}',undefined,undefined,{},sources,keys)
  assert.deepEqual(calls[0],{response_text:'{}',allowed_values:{},field_sources:sources,response_keys:keys})
  await previewVisualRulesOutput('{}',undefined,version,{},sources,keys)
  assert.deepEqual(calls[1],{response_text:'{}',output_version_id:version.id,expected_revision:2})
})

test('all output mappings select their server-declared types and preserve canonical downstream fields',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/outputFieldSources.ts'),v=await vite.ssrLoadModule('/src/services/visualRulesOutput.ts')
  const options=m.adaptOutputFieldSourceOptions(outputFieldSourceOptions),base=v.adaptVisualRulesOutputOptions(visualRulesOutputRaw)
  for(const continuity of options[0].choices)for(const color of options[1].choices)for(const negative of options[2].choices){
    const sources={continuity_summary:continuity.response_field,color_script:color.response_field,negative_prompt:negative.response_field}
    assert.deepEqual(m.outputFieldSourceErrors(sources,options,{}),[])
    const draft=m.draftOutputSourceOptions(base,sources,options)
    assert.deepEqual(Object.keys(draft.responseExample).sort(),['schema_version',...Object.values(sources)].sort())
    assert.deepEqual(draft.outputs.map((row:any)=>row.sourceSelector),base.outputs.map((row:any)=>row.sourceSelector))
    assert.deepEqual(m.outputFieldSources({fields:draft.outputs.map((row:any)=>({field:row.field,response_field:row.responseField}))}),sources)
  }
  assert.equal('continuity_points' in base.responseExample,false)
  assert.equal('negative_prompt' in base.responseExample,false)
})
