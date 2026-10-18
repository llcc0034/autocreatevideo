// Isolated UI fixture: no business API writes, provider calls or registry changes.
import {createApp,h,ref} from 'vue'
import ExecutionConfigurationEditor from '../../src/components/ExecutionConfigurationEditor.vue'
import StructuredJsonEditor from '../../src/components/StructuredJsonEditor.vue'
import {dataStructures} from '../../src/services/dataStructures'
import {api} from '../../src/services/api'
import type {ExecutionConfiguration,ExecutionSelectionOptions} from '../../src/services/executionRegistry'
import '../../src/styles.css'
import '../../src/platform.css'

api.interceptors.request.use(()=>{throw new Error('隔离夹具禁止真实API请求')})
const selected={id:'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',structure_key:'cccccccc-cccc-4ccc-8ccc-cccccccccccc',version:3,name:'镜头动态描述（自定义字段）',status:'published' as const}
let unavailable=false
dataStructures.list=async()=>({hasNext:false,hasPrevious:false,count:1,results:[selected]})
dataStructures.get=async()=>{if(unavailable)throw new Error('隔离样例：所选版本暂时不可读取，原引用保留');return selected}
dataStructures.schema=async()=>({valid:true,errors:{},json_schema:{type:'object',title:'镜头动态描述',properties:{动作:{type:'string',title:'动作',default:'wave'}},required:['动作'],additionalProperties:true}})
dataStructures.preview=async()=>{throw new Error('此夹具只验证引用控件；解析由隔离后端测试覆盖')}
const choices={prompt_modes:[],version_statuses:[],input_kinds:[{value:'json',label:'结构化数据'}],target_kinds:[{value:'operation_input',label:'执行输入'}],parameter_types:[],parameter_policies:[],seed_policies:[],identity_options:{subject_kinds:[],scope_kinds:[{value:'request',label:'本次请求'}]},target:null} as unknown as ExecutionSelectionOptions
createApp({setup(){
  const readonly=ref(false),status=ref('精确版本可读')
  const rawValue=ref<unknown>({}), valueErrors=ref<string[]>([])
  const schema={type:'object',properties:{动作:{type:'string',title:'动作',default:'wave'}},required:['动作']}
  const config=ref<ExecutionConfiguration>({deployment_id:'fixture',operation:'prompt.fields.generate',mode:'',prompt_mode:'none',prompt_profile_version_id:null,workflow_version_id:null,
    inputs:[{key:'custom_payload',position:0,kind:'json',required:true,min_items:1,max_items:1,
      semantic_contract:{schema_version:1,allowed_purposes:[],allowed_subject_kinds:[],allowed_scope_kinds:['request'],paired_with_input_key:null,allow_repeated_asset:false},
      targets:[{target_kind:'operation_input',target_key:'field_request',is_primary:true}]}],parameters:[],auxiliaries:[]})
  return()=>h('main',{style:{maxWidth:'960px',margin:'24px auto',padding:'16px',background:'#101820',color:'#e6edf3'}},[
    h('h1','M1-08 输入结构隔离验证'),h('p','使用正式编辑器，目录来自明确标注的隔离夹具；不保存配置、不调用业务或模型。'),
    h('button',{class:'btn btn-secondary',onClick:()=>readonly.value=!readonly.value},readonly.value?'切换可编辑':'切换只读'),
    h('button',{class:'btn btn-secondary',onClick:()=>{unavailable=!unavailable;status.value=unavailable?'模拟所选版本读取失败':'精确版本可读'}},'切换读取失败样例'),h('p',status.value),
    h(ExecutionConfigurationEditor,{modelValue:config.value,options:null,choices,auxiliaryVersions:[],readonly:readonly.value,'onUpdate:modelValue':(value:ExecutionConfiguration)=>config.value=value}),
    h('output',{'aria-label':'本地草稿结构引用',style:{display:'block',overflowWrap:'anywhere'}},config.value.inputs[0]?.data_structure_version_id||'未选择'),
    h('h2','请求值样例（不提交）'),h('p','只在明确结构引用后，将缺键留给服务端解析；控件不写入默认值。'),
    h(StructuredJsonEditor,{modelValue:rawValue.value,label:'镜头动态',schemas:[schema],defaultSchemas:config.value.inputs[0]?.data_structure_version_id?[schema]:[],
      'onUpdate:modelValue':(value:unknown)=>rawValue.value=value,onValidity:(value:string[])=>valueErrors.value=value}),
    h('output',{'aria-label':'原始请求值'},JSON.stringify(rawValue.value)),h('p',valueErrors.value.length?valueErrors.value.join('；'):'原值可提交服务端解析；不代表模型执行成功'),
  ])
}}).mount('#app')
