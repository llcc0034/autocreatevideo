// Explicit isolated component fixture: no application route or business requests.
import {createApp,h,ref} from 'vue'
import InputSourcePolicyEditor from '../../src/components/InputSourcePolicyEditor.vue'
import MediaSourceSelectionEditor from '../../src/components/MediaSourceSelectionEditor.vue'
import {selectedMediaSource,type InputSourceCatalog,type MediaSourcePolicy,type MediaSourceSelection} from '../../src/services/inputSourcePolicies'
import {repository} from '../../src/services/repository'
import type {Asset} from '../../src/types'
import '../../src/styles.css'
import '../../src/platform.css'

const asset={id:'12345678-1234-1234-1234-123456789012',name:'隔离图片样例',isActive:true,checksum:'a'.repeat(64),mimeType:'image/png',metadata:{}} as Asset
repository.getAssetPage=async()=>({count:1,next:null,previous:null,results:[asset]})
repository.getAsset=async()=>{throw new Error('隔离夹具：原引用不可读取，不会换图')}
repository.createAsset=async()=>{throw new Error('隔离夹具禁止真实上传；上传API另由隔离后端测试验证')}
const catalog:InputSourceCatalog={schema_version:1,configuration_owner:'recipe_instance_or_profile',description:'隔离来源目录',orders:[{value:'provided',label:'按明确提供的顺序'}],sources:[
  {value:'asset_library',label:'素材库已有素材',kinds:['image'],available:true,reader:'asset',requires:['owner'],description:'明确选择正式Asset'},
  {value:'asset_upload',label:'本地上传后入库',kinds:['image'],available:true,reader:'asset',requires:['owner','completed_asset_upload'],description:'先创建Asset再引用，不接受临时路径'},
  {value:'selected_output',label:'上游已选择产物',kinds:['image'],available:true,reader:'selected_output',requires:['production_run'],description:'确切选择决定与产物'},
  {value:'bible_reference',label:'Bible主图／变体素材版本',kinds:['image'],available:false,reader:null,requires:['asset_reference_version'],description:'M9/M5未接入，不按名称猜测'},
]}
createApp({setup(){
  const policy=ref<MediaSourcePolicy>({schema_version:1,media_kind:'image',allowed_sources:['asset_library','asset_upload'],default_source:'asset_library',allow_override:true,min_items:1,max_items:2,allowed_purposes:['appearance','motion'],order:'provided'})
  const base:MediaSourceSelection={source:'asset_library',items:[{item_key:'fixture-base',purpose:'appearance',asset_id:asset.id}]}
  const choice=ref<MediaSourceSelection|null>(JSON.parse(JSON.stringify(base))),narrow=ref(false),result=ref(''),busy=ref(false)
  function preview(){try{result.value='仅控件校验（不代表媒体或任务成功）：'+JSON.stringify(policy.value.allow_override?selectedMediaSource(policy.value,base,choice.value):selectedMediaSource(policy.value,base))}catch(cause){result.value=String(cause)}}
  return()=>h('main',{style:{maxWidth:narrow.value?'360px':'1000px',margin:'24px auto',padding:'16px',background:'#101820',color:'#e6edf3'}},[
    h('h1','M1-07 来源策略隔离验证'),h('p','不调用业务API，不保存配置或真实素材。业务编辑器接入另归M1-08。'),
    h('button',{class:'button ghost small',onClick:()=>narrow.value=!narrow.value},narrow.value?'恢复宽屏':'切换360px容器'),
    h(InputSourcePolicyEditor,{modelValue:policy.value,catalog,purposes:[{value:'appearance',label:'外观参考'},{value:'motion',label:'动作参考'}],'onUpdate:modelValue':(value:MediaSourcePolicy)=>{policy.value=value;result.value=''}}),
    h(MediaSourceSelectionEditor,{modelValue:policy.value.allow_override?choice.value:base,policy:policy.value,catalog,mode:'request',supportedSources:['asset_library','asset_upload'],disabled:busy.value,
      'onUpdate:modelValue':(value:MediaSourceSelection)=>{choice.value=value;result.value=''},onBusy:(value:boolean)=>{busy.value=value}}),
    h('button',{class:'button primary small',disabled:busy.value,onClick:preview},'校验明确选择'),h('p',{role:'status',style:{overflowWrap:'anywhere'}},result.value),
  ])
}}).mount('#app')
