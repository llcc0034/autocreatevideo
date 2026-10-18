import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {createSSRApp,h} from 'vue'
import {renderToString} from 'vue/server-renderer'

test('mixed input shows exact strategy structure once instead of another structure picker',async context=>{
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom'})
  context.after(()=>vite.close())
  const {default:Editor}=await vite.ssrLoadModule('/src/components/ExecutionConfigurationEditor.vue')
  const structure={id:'00000000-0000-4000-8000-000000000001',name:'镜头业务输入',version:2}
  const input={key:'shared',kind:'json',position:0,required:true,min_items:1,max_items:1,semantic_contract:{allowed_scope_kinds:['request']},
    targets:[{target_kind:'operation_input',target_key:'facts',is_primary:true},{target_kind:'profile_input',target_key:'request.structured_input',is_primary:false}]}
  const props={modelValue:{inputs:[input],parameters:[],auxiliaries:[],prompt_mode:'manual'},auxiliaryVersions:[],
    choices:{input_kinds:[],target_kinds:[],parameter_types:[],identity_options:{subject_kinds:[],scope_kinds:[]}},
    options:{input_targets:{},parameters:{},input_purposes:{},auxiliary_purposes:[],profile_inputs:{facts:{target_kind:'profile_input',target_key:'request.structured_input',kind:'json',input_structure:structure}}}}
  let writes=0
  const render=(changes={})=>renderToString(createSSRApp({render:()=>h(Editor,{...props,...changes,'onUpdate:modelValue':()=>writes++})}))
  const html=await render()
  assert.match(html,/输入结构 · 继承所接策略/);assert.match(html,/镜头业务输入 v2/)
  assert.doesNotMatch(html,/通用输入结构（可选）|移除结构引用/)
  const invalid=await render({modelValue:{...props.modelValue,inputs:[{...input,data_structure_version_id:'different'}]}})
  assert.match(invalid,/已有输入结构与策略不同/)
  const readonly=await render({readonly:true})
  assert.match(readonly,/<fieldset[^>]*disabled/);assert.equal(writes,0)
})

test('format-only options reject duplicate schemas but preserve strict false and obsolete original values',async context=>{
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom'})
  context.after(()=>vite.close())
  const m=await vite.ssrLoadModule('/src/services/modelChatRequestOptions.ts')
  const {default:Editor}=await vite.ssrLoadModule('/src/components/ModelChatRequestOptionsEditor.vue')
  const options={schemaVersion:1,temperatureMinimum:0,responseFormatTypes:[{value:'json_schema',label:'JSON Schema'}],assemblyDescription:'结构来自业务输出',boundaryDescription:'不代表模型已支持'}
  const original={response_format:{type:'json_schema',json_schema:{name:'output_v1',strict:false,description:'业务结构'}},temperature:0}
  assert.deepEqual(m.commonChatOptionErrors(original,options),[])
  const bad=structuredClone(original);bad.response_format.json_schema.schema={type:'object'}
  const before=structuredClone(bad)
  assert.ok(m.commonChatOptionErrors(bad,options).some((x:string)=>x.includes('模型不能重复定义')))
  assert.deepEqual(bad,before)
  const html=await renderToString(createSSRApp({render:()=>h(Editor,{modelValue:original,options,label:'输出接口格式'})}))
  assert.match(html,/无需另填 Schema/);assert.match(html,/响应格式接口选项/)
})
