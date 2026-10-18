import {createApp,h,ref} from 'vue'
import ExecutionRequestInputEditor from '../../src/components/ExecutionRequestInputEditor.vue'
import {executionRequestFormContract,emptyExecutionRequestDraft,buildExecutionRequestFixture} from '../../src/services/executionRequestForm'
import {repository} from '../../src/services/repository'
import {inputSourcePolicies} from '../../src/services/inputSourcePolicies'
import {api} from '../../src/services/api'
import {mediaAssets,mediaContractRaw,mediaSources,mediaId} from './execution-media-authoring-data'
import '../../src/styles.css'
const reads=ref(0),writes=ref(0),blocked=ref(0),outcome=ref('success'),busy=ref(false),errors=ref<string[]>([]),result=ref('')
const copy=(v:any)=>JSON.parse(JSON.stringify(v)),deny=()=>{blocked.value++;throw Error('内存隔离：禁止未声明网络与写入')}
window.fetch=async()=>deny();XMLHttpRequest.prototype.open=function(){deny()};navigator.sendBeacon=()=>deny();api.interceptors.request.use(()=>deny())
for(const client of [repository,inputSourcePolicies])for(const key of Object.keys(client))(client as any)[key]=async()=>deny()
inputSourcePolicies.options=async()=>{reads.value++;return copy(mediaSources)}
const assets=copy(mediaAssets)
repository.getAssetPage=async(page=1,query='')=>{reads.value++;await new Promise(r=>setTimeout(r,100));const rows=assets.filter(a=>a.name.includes(query));return{count:rows.length,next:page*3<rows.length?'next':null,previous:page>1?'previous':null,results:copy(rows.slice((page-1)*3,page*3))}}
repository.createAsset=async payload=>{
  writes.value++;await new Promise(r=>setTimeout(r,350))
  if(outcome.value==='reject')throw{isAxiosError:true,response:{status:400,data:{detail:'隔离校验拒绝；可以修改后重试'}}}
  const asset={id:mediaId(50+writes.value),name:payload.name,isActive:true,type:payload.mediaType,mimeType:payload.file.type,checksum:'f'.repeat(64),metadata:{}}
  assets.push(asset);if(outcome.value==='unknown')throw{response:{status:504}};return copy(asset)
}
const contract=executionRequestFormContract(copy(mediaContractRaw)),draft=ref(emptyExecutionRequestDraft(contract))
for(const key of ['pictures','pictures','clips','clips','paired','audio'])draft.value.media[key]!.push({itemKey:crypto.randomUUID(),assetId:'',purpose:null,pairedTo:null,subjectId:null})
function insertFixtureFile(){const input=document.querySelector<HTMLInputElement>('.picker-upload input[type=file]');if(!input||input.disabled)return;const mime=input.accept.replace('*','fixture'),transfer=new DataTransfer();transfer.items.add(new File(['isolated media fixture bytes'],'隔离测试文件.'+mime.split('/')[0],{type:mime}));input.files=transfer.files;input.dispatchEvent(new Event('change',{bubbles:true}))}
createApp({render:()=>h('main',{style:'max-width:980px;margin:0 auto;padding:16px;display:grid;gap:16px;min-width:0'},[
  h('h1','混合媒体输入 · 内存隔离'),h('p','真实共用输入组件与原装配方法；上传只写内存，不校验媒体编码、不调用模型。素材名称和各次使用 ID 保留。'),
  h('label',['上传模拟回执',h('select',{value:outcome.value,disabled:busy.value,onChange:(e:any)=>outcome.value=e.target.value},['success','reject','unknown'].map(value=>h('option',{value},value)))]),
  h('button',{class:'button ghost',disabled:busy.value,onClick:insertFixtureFile},'向已展开上传区放入隔离文件'),
  h(ExecutionRequestInputEditor,{contract,modelValue:draft.value,disabled:busy.value,'onUpdate:modelValue':v=>draft.value=v,onBusy:v=>busy.value=v,onValidity:v=>errors.value=v}),
  errors.value.length?h('p',{role:'alert'},errors.value.join('；')):null,
  h('button',{class:'button primary',disabled:busy.value||!!errors.value.length,onClick:()=>{try{result.value=JSON.stringify(buildExecutionRequestFixture(contract,draft.value),null,2)}catch(error){result.value=String(error)}}},'核对本次输入（不提交任务）'),
  h('pre',{style:'white-space:pre-wrap;overflow-wrap:anywhere'},result.value),
  h('output',{style:'position:fixed;bottom:0;left:0;right:0;pointer-events:none;background:#18312b;padding:5px;font-size:12px;z-index:9000'},`内存上传 ${writes.value} · 目录读取 ${reads.value} · 网络守卫 ${blocked.value}`),
])}).mount('#app')
