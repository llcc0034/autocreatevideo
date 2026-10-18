// Formal Profile editor with explicit in-memory fixtures. No business writes or model calls.
import {createApp,h} from 'vue'
import {createRouter,createMemoryHistory} from 'vue-router'
import PromptProfileSettings from '../../src/components/PromptProfileSettings.vue'
import {platformApi} from '../../src/services/platformApi'
import {adaptPromptProfileVersion,adaptPromptProfileFieldRule} from '../../src/services/platformAdapters'
import {fieldDefinitionVersions} from '../../src/services/fieldDefinitionVersions'
import {dataStructures} from '../../src/services/dataStructures'
import {api} from '../../src/services/api'
import type {PromptProfileOptions} from '../../src/services/platformTypes'
import '../../src/styles.css'
import '../../src/platform.css'

const source='request.structured_input'
const structure={id:'fixture-structure-v2',structure_key:'fixture',version:2,name:'隔离动态输入',status:'published' as const}
const schema={type:'object',additionalProperties:false,properties:{'动态.输入':{type:'object',additionalProperties:false,properties:{action:{type:'string',title:'动作'},active:{type:'boolean',title:'启用'},count:{type:'integer',title:'数量'}}}}}
const field={id:'fixture-field',key:'custom_motion',isActive:true,displayOrder:0,createdAt:'',updatedAt:''}
const version={id:'fixture-field-v1',field_definition:field.id,field_key:field.key,version:1,status:'published',label:'自定义动作',category:'test',impact:'隔离说明',value_type:'text',value_options:[],preview_example:'单字段示例，不得充当整份输入',description:'隔离定义',unit:'',validation_rules:{},contract_sha256:'a'.repeat(64),contract_size_bytes:100}
const profile=adaptPromptProfileVersion({id:'fixture-profile',profile_key:'fixture',name:'结构化动作策略',version:1,model_family:'fixture',operation:'video.generate',mode:'T2VA',source_projection_key:'direct_v1',source_projection_config:{},channel_limits:{},input_structure_version:structure.id,status:'draft',review_state:'ready',contract_sha256:'b'.repeat(64),field_rule_count:1})
const rule=adaptPromptProfileFieldRule({id:'fixture-rule',profile_version:profile.id,field_definition:field.id,field_definition_version:version.id,field_key:field.key,field_label:version.label,field_value_type:'text',source_selector:source,source_path:['动态.输入','action'],source_version:null,channel:'positive_prompt',position:1,label_style:'none',separator:'newline',required:false,has_default:false,omit_if_empty:true,max_chars:4000,generation_mode:'passthrough',generation_source_selector:source,generation_context_selectors:[],generation_instruction:'',generation_output_semantics:'final',generation_required:false,language_policy:'any',length_unit:'none',generation_validator_keys:[]})
const choice=(value:string,label=value)=>({value,label})
const options={operations:['video.generate'],modelFamilies:['fixture'],modes:['T2VA'],
 customEntryFields:[],generationLeafOperations:['prompt.fields.generate','prompt.fields.translate'],
 ruleSystemDefaults:{label_style:'none',separator:'newline',required:false,omit_if_empty:true,max_chars:4000,generation_output_semantics:'final',generation_required:false,language_policy:'any',length_unit:'none'},
 sourceSelectors:[{...choice(source,'本次结构化输入字段'),groupLabel:'本次输入',description:'从结构中按路径读取',recordLocation:'本次请求结构化输入',executionUsage:'原值装配，不调用模型'}, {...choice('request.theme','请求主题'),groupLabel:'本次输入',description:'主题',recordLocation:'本次请求',executionUsage:'原值装配'}],
 channels:[choice('positive_prompt','正向提示词')],labelStyles:[choice('none','不显示字段名')],separators:[choice('newline','换行')],
 generationModes:[choice('passthrough','原值透传'),choice('model_generated','模型生成'),choice('translate_only','仅翻译')],generationSourceSelectors:[{...choice('request.action','本次请求动作'),scopeType:'request',group:'request_text/v1',groupLabel:'本次请求输入',description:'明确提供的本次动作原文，不读取旧镜头列',availability:'隔离输入合同',scope:'本次输入，不自动读取其他镜头',status:'active',generationModes:['model_generated','translate_only'],dialogueExposure:'explicit_input_only',crossShotExposure:'explicit_input_only',cardinality:'single'}],
 generationContextSelectors:[{...choice('project.visual_style'),description:'隔离目录'}],generationOutputSemantics:[choice('final','最终值')],
 sourceProjectionKeys:[choice('direct_v1','直接装配')],sourceProjectionPrimitives:[],referenceRenderingFields:[],referenceRenderingTextSlots:[choice('action','动作'),choice('description','镜头描述')],languagePolicies:[choice('any','不限语言'),choice('en','英文')],lengthUnits:[choice('none','不限制')],generationValidators:[],modelProfileVersions:[],
} as PromptProfileOptions
// Query selects a separate explicit contract fixture, never a live setting.
if (new URLSearchParams(location.search).get('contribution') === '1' || new URLSearchParams(location.search).get('render') === '1') {
  profile.sourceProjectionKey='declarative_v1'
  profile.sourceProjectionConfig={schema_version:5,primitive_options:{reference_context_ir_v1:{}}}
  rule.sourceSelector='projection.output'
  rule.projectionPrimitive='reference_context.detailed_description'
  rule.generationMode='model_generated'
  rule.generationOutputSemantics='contribution'
  rule.generationInstruction='Use this unit only.'
  rule.languagePolicy='en'
  options.sourceSelectors.push({...choice('projection.output','声明式投影输出'),groupLabel:'声明式来源',description:'所选原语结果',recordLocation:'本次渲染',executionUsage:'逐单元贡献'})
  options.sourceProjectionKeys.push(choice('declarative_v1','声明式装配'))
  options.sourceProjectionPrimitives=[{...choice(rule.projectionPrimitive,'导演时间线'),description:'独立接收单元贡献',engineKey:'reference_context_ir_v1',renderingTemplateKeys:['shot'],contributionSourceSelectors:['shot.detailed_description']}]
  options.referenceRenderingFields=[{key:'shot',label:'单元',placeholders:[],description:'隔离合同',assembly:'本次单元',requirement:'明确提供',placeholderHelp:{},example:{template:'',result:''}}]
  options.generationOutputSemantics.push(choice('contribution','逐单元贡献'))
}
if (new URLSearchParams(location.search).get('render') === '1') {
  profile.sourceProjectionConfig={schema_version:5,primitive_options:{reference_context_ir_v1:{rendering:{
    unit_text_bindings:{action:['动态.输入','action']},templates:{shot:'{action}'},separator:'\n',list_prefix:'',
    prompt_template_key:'isolated-render-path',task_separator:' + ',unreferenced_task:'isolated',reference_rules:[],
  }}}}
}
api.defaults.adapter=async(config)=>{
 if(config.method==='get'&&config.url==='/registry-relations/')return {data:{count:1,next:null,previous:null,generated_at:'2026-09-28T05:52:00Z',context_label:'隔离夹具',results:[{id:profile.id,kind:'prompt',label:profile.name,status:'draft',config_url:'/settings',overview_url:'/settings',acceptance_url:'',groups:[],notices:[],capabilities:[]}]},status:200,statusText:'OK',headers:{},config}
 throw Error('隔离夹具禁止真实业务API：'+config.url)
}
platformApi.listFieldDefinitions=async()=>[field]
platformApi.listPromptProfileVersions=async()=>[profile]
platformApi.listPromptProfileFieldRules=async()=>[rule]
platformApi.getPromptProfileOptions=async()=>options
fieldDefinitionVersions.get=async()=>version as Awaited<ReturnType<typeof fieldDefinitionVersions.get>>
fieldDefinitionVersions.list=async()=>[version] as Awaited<ReturnType<typeof fieldDefinitionVersions.list>>
dataStructures.list=async()=>({hasNext:false,hasPrevious:false,count:1,results:[structure]})
dataStructures.get=async()=>structure
dataStructures.schema=async()=>({valid:true,json_schema:schema,errors:{}})
dataStructures.preview=async()=>{throw Error('隔离页面不提交结构解析；后端解析由单独测试覆盖')}
platformApi.previewPromptProfileVersion=async()=>{throw Error('隔离预览：输入已到请求边界；此夹具不伪造后端装配成功')}
createApp({setup(){return()=>h('main',{style:{maxWidth:'1100px',margin:'12px auto',padding:'12px'}},[
 h('h1','Profile 结构路径 · 隔离验证'),h('p','真实编辑器 + 明确模拟目录。不保存配置，不读取业务库、不调用模型；预览返回显式隔离提示。'),
 h(PromptProfileSettings,{promptProfileVersionId:profile.id}),
])}}).use(createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]})).mount('#app')
