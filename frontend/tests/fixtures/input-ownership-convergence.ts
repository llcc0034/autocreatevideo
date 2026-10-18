import {createApp,h,ref} from 'vue'
import ExecutionConfigurationEditor from '../../src/components/ExecutionConfigurationEditor.vue'
import ModelChatRequestOptionsEditor from '../../src/components/ModelChatRequestOptionsEditor.vue'
import {api} from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'

api.interceptors.request.use(()=>{throw new Error('隔离夹具禁止真实业务API请求')})
const structure={id:'00000000-0000-4000-8000-000000000001',name:'镜头业务输入',version:2}
createApp({setup(){
  const readonly=ref(false),configuration=ref({inputs:[{key:'shared_facts',kind:'json',position:0,required:true,min_items:1,max_items:1,
    semantic_contract:{allowed_scope_kinds:['request']},targets:[{target_kind:'operation_input',target_key:'facts',is_primary:true},
      {target_kind:'profile_input',target_key:'request.structured_input',is_primary:false}]}],parameters:[],auxiliaries:[],prompt_mode:'manual'})
  const options=ref({temperature:0,response_format:{type:'json_schema',json_schema:{name:'short_drama',strict:false}}})
  return()=>h('main',{style:{maxWidth:'960px',padding:'16px',margin:'auto',minWidth:0}},[
    h('h1','输入输出归属 · 隔离验证'),h('p','正式共享组件＋隔离合同；不保存模型、策略或业务数据，不执行生成。'),
    h('button',{class:'button ghost',onClick:()=>readonly.value=!readonly.value},readonly.value?'切换可编辑':'切换只读'),
    h(ExecutionConfigurationEditor,{modelValue:configuration.value as any,readonly:readonly.value,auxiliaryVersions:[],
      choices:{input_kinds:[{value:'json',label:'JSON'}],target_kinds:[],parameter_types:[],identity_options:{subject_kinds:[],scope_kinds:[]}} as any,
      options:{input_targets:{},parameters:{},input_purposes:{},auxiliary_purposes:[],profile_inputs:{facts:{target_kind:'profile_input',target_key:'request.structured_input',kind:'json',input_structure:structure}}} as any,
      'onUpdate:modelValue':value=>configuration.value=value as any}),
    h(ModelChatRequestOptionsEditor,{modelValue:options.value,label:'模型的输出格式（不再维护业务 Schema）',disabled:readonly.value,
      options:{schemaVersion:1,temperatureMinimum:0,responseFormatTypes:[{value:'json_schema',label:'JSON Schema（来自业务输出）'},{value:'json_object',label:'JSON 对象'}],
        assemblyDescription:'接口格式选项随模型版本冻结；输出结构来自执行实例的精确业务输出。',boundaryDescription:'不推断供应商支持，不自动选择输出；最终请求与解析使用同一结构。'},
      'onUpdate:modelValue':value=>options.value=value as any}),
  ])
}}).mount('#app')
