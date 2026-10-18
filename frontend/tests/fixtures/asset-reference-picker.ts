// Disposable in-memory data; all unhandled network API calls are rejected.
import {createApp,h,reactive} from 'vue'
import MediaSourceSelectionEditor from '../../src/components/MediaSourceSelectionEditor.vue'
import StructureMediaValue from '../../src/components/StructureMediaValue.vue'
import {api} from '../../src/services/api'
import {repository} from '../../src/services/repository'
import {assetReferenceApi,type AssetReferenceSeries,type AssetReferenceVersion} from '../../src/services/assetReferenceApi'
import type {InputSourceCatalog,MediaKind,MediaSourcePolicy,MediaSourceSelection} from '../../src/services/inputSourcePolicies'
import type {Asset} from '../../src/types'
import '../../src/styles.css'
import '../../src/platform.css'

api.defaults.adapter=async config=>{throw Error('隔离夹具禁止真实API：'+config.url)}
const uuid=(n:number)=>`10000000-0000-4000-8000-${String(n).padStart(12,'0')}`
const image=(label:string)=>'data:image/svg+xml,'+encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="480" height="240"><rect width="480" height="240" fill="#304658"/><text x="24" y="130" font-size="28" fill="white">${label}</text></svg>`)
const media:Asset[]=[['主图','image'],['制服旧版','image'],['制服新版','image'],['便装','image'],['独立视频','video'],['独立音频','audio'],['独立图片','image']].map(([name,kind],i)=>({id:uuid(i+1),name:name!,type:kind as Asset['type'],mimeType:kind+'/fixture',checksum:'a'.repeat(64),isActive:true,styleId:'',description:'隔离数据，非真实生成',tags:[],mediaUrl:kind==='image'?image(name!):''}))
const series:AssetReferenceSeries[]=[{id:'primary',name:'演示人物·主图',role:'primary',owner_id:1,primary_series_id:null,bible_variant_id:null,version_count:1,variant_count:2},
  {id:'uniform',name:'演示人物·制服',role:'variant',owner_id:1,primary_series_id:'primary',bible_variant_id:null,version_count:2,variant_count:0},
  {id:'casual',name:'演示人物·便装',role:'variant',owner_id:1,primary_series_id:'primary',bible_variant_id:null,version_count:1,variant_count:0}]
const versions:AssetReferenceVersion[]=[['p','primary',0,1],['u1','uniform',1,1],['u2','uniform',2,2],['c','casual',3,1]].map(([id,sid,i,n])=>{
  const a=media[Number(i)]!;return {id:String(id),series_id:String(sid),version:Number(n),source:'existing',generated_from_id:null,setting_revision_id:null,asset:{id:a.id,name:a.name,kind:'reference',mime_type:a.mimeType!,is_active:true}}
})
const audit=reactive({uploads:0,versionReads:0,invalidRequests:0})
const faults=reactive({version:false})
const page=<T>(rows:T[],n:number,size:number)=>({count:rows.length,next:n*size<rows.length?'fixture:next':null,previous:n>1?'fixture:previous':null,results:rows.slice((n-1)*size,n*size)})
assetReferenceApi.roles=async()=>[{value:'primary',display_name:'主图'},{value:'variant',display_name:'变体'}]
assetReferenceApi.listSeries=async q=>{
  if(q.search==='失败')throw Error('隔离查询失败，修改名称后重试')
  return page(series.filter(s=>s.name.includes(q.search)&&(!q.role||s.role===q.role)&&(!q.primary_series_id||s.primary_series_id===q.primary_series_id)&&(q.bible_linked===undefined||!!s.bible_variant_id===q.bible_linked)&&(!q.media_type||versions.some(v=>v.series_id===s.id&&v.asset.mime_type.startsWith(q.media_type+'/')))),q.page,2)
}
assetReferenceApi.getSeries=async id=>series.find(s=>s.id===id)!
assetReferenceApi.listVersions=async q=>page(versions.filter(v=>v.series_id===q.series_id).sort((a,b)=>b.version-a.version),q.page,1)
assetReferenceApi.getVersion=async id=>{audit.versionReads++;if(faults.version)throw Error('隔离夹具：此版本已被解除，未自动选择其他版本');return versions.find(v=>v.id===id)!}
repository.getAsset=async id=>{const a=media.find(v=>v.id===id);if(!a)throw Error('未找到原素材');return a}
repository.getAssetPage=async(n,search='')=>page(media.filter(a=>a.name.includes(search)),n,3)
repository.createAsset=async input=>{audit.uploads++;const a:Asset={...media[0]!,id:uuid(100+audit.uploads),name:input.name,type:input.mediaType as Asset['type'],mimeType:input.mediaType+'/fixture',mediaUrl:URL.createObjectURL(input.file)};media.push(a);return a}
const reject=async()=>{audit.invalidRequests++;throw Error('选材不得写系列、采用或调用模型')}
assetReferenceApi.attach=reject;assetReferenceApi.createSeries=reject;assetReferenceApi.updateSeries=reject;assetReferenceApi.remove=reject
const catalog:InputSourceCatalog={schema_version:1,configuration_owner:'fixture',description:'隔离来源',orders:[{value:'provided',label:'提供顺序'}],sources:['asset_library','asset_upload'].map(value=>({value,label:value==='asset_library'?'已有素材':'本地上传',kinds:['image','video','audio'],reader:'asset',available:true,requires:[],description:'Fixture'}))}
const policies=(kind:MediaKind):MediaSourcePolicy=>({schema_version:1,media_kind:kind,allowed_sources:['asset_library','asset_upload'],default_source:'asset_library',allow_override:true,min_items:0,max_items:2,allowed_purposes:['自定义用途'],order:'provided'})
const choices=reactive<Record<MediaKind,MediaSourceSelection>>({image:{source:'asset_library',items:[{item_key:'shot-a',purpose:'自定义用途',asset_id:''},{item_key:'shot-b',purpose:'自定义用途',asset_id:''}]},video:{source:'asset_library',items:[{item_key:'video',purpose:'自定义用途',asset_id:''}]},audio:{source:'asset_library',items:[{item_key:'audio',purpose:'自定义用途',asset_id:''}]}})
const busy=reactive({image:false,video:false,audio:false}),literal=reactive({value:{} as unknown})
createApp({render:()=>h('main',{style:{maxWidth:'1040px',margin:'20px auto',padding:'16px'}},[
  h('h1','M9-06 媒体字段选材 · 隔离夹具'),h('p','真实M1组件与内存数据；不连接业务库、不启动模型。主图和版本关系只读，所有选择可刷新恢复。'),
  h('label',[h('input',{type:'checkbox',checked:faults.version,onChange:(e:Event)=>faults.version=(e.target as HTMLInputElement).checked}),'模拟版本解除错误（仅夹具）']),
  ...(['image','video','audio'] as const).map(kind=>h('section',{'aria-label':kind+' 字段',style:{borderBottom:'1px solid #666',padding:'18px 0'}},[
    h('h2',kind+' 输入'),h(MediaSourceSelectionEditor,{modelValue:choices[kind],policy:policies(kind),catalog,mode:'request',supportedSources:['asset_library','asset_upload'],disabled:busy[kind],onBusy:(v:boolean)=>busy[kind]=v,'onUpdate:modelValue':(value:MediaSourceSelection)=>choices[kind]=value})])),
  h(StructureMediaValue,{modelValue:literal.value,schemas:[{'x-media-types':['image'],'x-media-usage':'JSON字段用途'}],label:'结构媒体字段',selectable:true,'onUpdate:modelValue':(value:unknown)=>literal.value=value}),
  h('pre',{'aria-label':'隔离选择结果',style:{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}},JSON.stringify({choices,literal,audit,originalVersions:versions.map(v=>({id:v.id,asset_id:v.asset.id}))},null,2)),
])}).mount('#app')
