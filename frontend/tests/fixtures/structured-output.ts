// Isolated UI responses only. Never calls business APIs, saves configs or runs a model.
import {createApp,h,ref} from 'vue'
import StructuredOutputPreview from '../../src/components/StructuredOutputPreview.vue'
import {businessOutputs,type BusinessOutputVersion,type StructuredOutputPreview} from '../../src/services/businessOutputs'
import '../../src/styles.css'
import '../../src/platform.css'
const version:BusinessOutputVersion={id:'fixture-output',output_key:'fixture',version:1,revision:1,name:'隔离输出',description:'仅供页面验收',parser_key:'structured_json.v1',status:'draft',contract_sha256:'a'.repeat(64),data_structure_version:'fixture-structure',quality_advisories:{},parser_contract:{key:'structured_json.v1',format:'json'}}
const schema={type:'object',properties:{'情绪.备注':{type:'string',title:'情绪备注'},active:{type:'boolean',title:'启用',default:false}},required:['情绪.备注']}
businessOutputs.previewJson=async(row,text)=>{
  if(text==='网络失败')throw new Error('隔离网络错误，未请求服务')
  const report:StructuredOutputPreview={preview_only:true,output_version_id:row.id,output_revision:row.revision,output_contract_sha256:row.contract_sha256,structure_version_id:'fixture-structure',json_schema:schema,fields:[{path:['情绪.备注'],field_definition:{label:'情绪备注',value_type:'text'}},{path:['active'],field_definition:{label:'启用',value_type:'boolean'}}],parsed:false,valid:false,raw_value:null,value:null,defaults:[],warnings:[],errors:{}}
  try{
    const raw=JSON.parse(text);report.raw_value=raw;report.value=raw;report.parsed=true
    if(!raw||Array.isArray(raw)||typeof raw!=='object'){report.errors={response:['应为对象（隔离夹具）']};return report}
    if(typeof raw['情绪.备注']!=='string')report.errors={'response["情绪.备注"]':['应为文本（隔离夹具）']}
    if(!Object.prototype.hasOwnProperty.call(raw,'active')){report.value={...raw,active:false};report.defaults=[{path:'response.active',value:false,structure_version_id:'fixture-structure'}]}
    report.warnings=Object.keys(raw).filter(key=>!Object.keys(schema.properties).includes(key)).map(key=>({path:`response.${key}`,message:'未声明字段，原值保留（隔离夹具）'}))
    report.valid=!Object.keys(report.errors).length
  }catch{report.errors={response:['JSON语法错误（隔离夹具）']}}
  return report
}
createApp({setup(){const disabled=ref(false);return()=>h('main',{style:{maxWidth:'900px',margin:'16px auto',padding:'14px',color:'#edf2f7',background:'#101820'}},[
  h('h1','通用输出 · 隔离页面验证'),h('p','仅前端夹具，接口返回为明确模拟；不读取/修改配置或业务记录。输入示例：{"情绪.备注":"安静","extra":0}'),
  h('button',{class:'button ghost',onClick:()=>disabled.value=!disabled.value},disabled.value?'恢复可编辑':'模拟未保存配置'),
  h(StructuredOutputPreview,{version,disabled:disabled.value}),
])}}).mount('#app')
