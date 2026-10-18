// Isolated UI fixture. Not an application route and never contacts business APIs.
import {createApp,h,ref} from 'vue'
import StructuredJsonEditor from '../../src/components/StructuredJsonEditor.vue'
import DataStructureValuePreview from '../../src/components/DataStructureValuePreview.vue'
import {dataStructures} from '../../src/services/dataStructures'
import {repository} from '../../src/services/repository'
import {jsonInputErrors,jsonObject} from '../../src/services/structuredInputForm'
import '../../src/styles.css'
import '../../src/platform.css'

const field=(type:string,title:string,extra={})=>({type,title,'x-field-definition':{field_definition_version_id:title,label:title,value_type:type,unit:'',description:`${title}定义说明`},...extra})
const schema=field('object','样例',{properties:{
  'mood.note':field('string','情绪说明',{default:'谨慎'}),
  active:field('boolean','启用'),count:field('integer','数量',{minimum:0}),
  mode:{anyOf:[field('string','档位',{enum:['甲','乙']}),{type:'null'}]},
  rows:field('array','内容列表',{items:field('string','列表正文')}),
  reference:field('object','图片参考',{'x-media-types':['image'],'x-media-usage':'外观参考',properties:{asset_id:{type:'string',format:'uuid'}},required:['asset_id'],additionalProperties:false}),
},required:['mood.note'],additionalProperties:true})
dataStructures.schema=async()=>({valid:true,json_schema:schema,errors:{}})
dataStructures.preview=async(id,value)=>{
  const raw=jsonObject(value), missing=!Object.hasOwn(raw,'mood.note')
  const resolved={...raw,...(missing?{'mood.note':'谨慎'}:{})}, messages=jsonInputErrors(resolved,[schema])
  return {preview_only:true,structure_version_id:id,valid:!messages.length,value:resolved,
    defaults:missing?[{path:'value["mood.note"]',node_key:'mood',structure_version_id:id,field_definition_version_id:'情绪说明',source:'configured_default',value:'谨慎'}]:[],errors:messages.length?{value:messages}:{}}
}
repository.getAsset=async()=>{throw new Error('隔离夹具：素材不可读，原引用保留')}
repository.getAssetPage=async()=>({count:0,next:null,previous:null,results:[]})
repository.createAsset=async()=>{throw new Error('隔离样例禁止上传')}

createApp({setup(){
  const narrow=ref(false),readonly=ref(false),value=ref<unknown>({active:false,count:0,mode:'甲',rows:['原始正文'],reference:{asset_id:'11111111-1111-4111-8111-111111111111'},unknown:'未映射的真实内容'})
  return()=>h('main',{style:{maxWidth:narrow.value?'360px':'1000px',margin:'24px auto',padding:'16px',fontFamily:'sans-serif',background:'#101820',color:'#e6edf3'}},[
    h('h1','M1-06 隔离表单样例（无业务写入）'),
    h('button',{onClick:()=>narrow.value=!narrow.value},narrow.value?'恢复宽屏':'切换360px窄屏'),
    h('button',{onClick:()=>readonly.value=!readonly.value},readonly.value?'切换编辑':'切换只读'),
    h(StructuredJsonEditor,{modelValue:value.value,label:'结构字段',schemas:[schema],readonly:readonly.value,fieldErrors:{'value["mood.note"]':['此路径需要补充说明']},'onUpdate:modelValue':(next:unknown)=>value.value=next}),
    h('h2','独立预览入口'),h(DataStructureValuePreview,{structureVersionId:'fixture-v1'}),
  ])
}}).mount('#app')
