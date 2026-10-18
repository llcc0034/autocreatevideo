import {createApp,h,ref} from 'vue'
import {createPinia} from 'pinia'
import {createRouter,createMemoryHistory,RouterView} from 'vue-router'
import AssetLibraryView from '../../src/views/AssetLibraryView.vue'
import {useAuthStore} from '../../src/stores/auth'
import {repository} from '../../src/services/repository'
import {assetReferenceApi} from '../../src/services/assetReferenceApi'
import {api} from '../../src/services/api'
import '../../src/styles.css'
const writes=ref(0),reads=ref(0),blocked=ref(0),copy=(v:any)=>JSON.parse(JSON.stringify(v))
const deny=()=>{blocked.value++;throw Error('隔离素材验证：禁止未声明网络与写入')}
const nativeFetch=window.fetch.bind(window),allowedMedia=new Set<string>()
window.fetch=async(input,options)=>allowedMedia.has(String(input))?nativeFetch(input,options):deny();XMLHttpRequest.prototype.open=function(){deny()};navigator.sendBeacon=()=>deny();api.interceptors.request.use(()=>deny());api.defaults.adapter=async()=>deny()
for(const client of [repository,assetReferenceApi])for(const key of Object.keys(client))(client as any)[key]=async()=>deny()
const png='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII='
const base:any={id:'fixture-asset',type:'image',backendKind:'reference',name:'隔离绿图 · 原名 🟢',description:'仅供前端隔离验证的素材说明',promptHint:'不翻译原始事实',sourceInfo:'隔离来源；业务历史已清理',styleId:'',ownerId:'905',tags:[],checksum:'a'.repeat(64),metadataSha256:'b'.repeat(64),metadata:{cleared_history_origin:true},isActive:true,mimeType:'image/png',updatedAt:'2026-10-09T14:10:00Z',thumbnail:'data:image/png;base64,'+png,mediaUrl:'data:image/png;base64,'+png}
const sampleScript='# 隔离剧本\n\n第一镜：林岚站在窗边。\n林岚：原名与原事实保持 🟢\n\n这份文本仅用于前端交互验证。'
const scriptUrl='data:text/markdown;charset=utf-8,'+encodeURIComponent(sampleScript)
const wav=new Uint8Array(4044),wave=new DataView(wav.buffer)
for(const [offset,value] of [[0,'RIFF'],[8,'WAVE'],[12,'fmt '],[36,'data']] as const)for(let i=0;i<value.length;i++)wav[offset+i]=value.charCodeAt(i)
wave.setUint32(4,wav.length-8,true);wave.setUint32(16,16,true);wave.setUint16(20,1,true);wave.setUint16(22,1,true);wave.setUint32(24,8000,true);wave.setUint32(28,16000,true);wave.setUint16(32,2,true);wave.setUint16(34,16,true);wave.setUint32(40,4000,true)
const audioUrl=URL.createObjectURL(new Blob([wav],{type:'audio/wav'})),videoUrl='/tests/fixtures/artifact-media-recovery.mp4'
allowedMedia.add(scriptUrl);allowedMedia.add(videoUrl);allowedMedia.add(audioUrl)
const samples:any={video:{mime:'video/mp4',extension:'mp4',url:videoUrl,kind:'reference_video',metadata:{duration_ms:1000,source_fps:24,has_audio:false}},audio:{mime:'audio/wav',extension:'wav',url:audioUrl,kind:'reference_audio',metadata:{duration_ms:250,has_audio:true,size_bytes:wav.length}},script:{mime:'text/markdown',extension:'md',url:scriptUrl,kind:'script',metadata:{size_bytes:new TextEncoder().encode(sampleScript).length,script_source_format:'md',script_normalization:'utf8_markdown_v1'}}}
const rows=[copy(base),...Object.entries(samples).map(([type,entry]:any)=>({...copy(base),id:'fixture-'+type,type,backendKind:entry.kind,mimeType:entry.mime,name:'隔离'+({video:'视频',audio:'音频',script:'剧本'} as any)[type]+' · 原名 🟢',thumbnail:'',mediaUrl:entry.url,metadata:{...entry.metadata,cleared_history_origin:true}}))]
repository.getAssets=async()=>{reads.value++;return copy(rows)}
repository.getAsset=async id=>{reads.value++;const row=rows.find(r=>r.id===id);if(!row)throw{response:{status:404}};return copy(row)}
repository.getAssetPage=async(page,search)=>{reads.value++;const list=rows.filter(r=>r.name.includes(search||''));return{count:list.length,next:null,previous:null,results:copy(list)}}
const save=async(payload:any,id?:string)=>{writes.value++;await new Promise(r=>setTimeout(r,200));if(payload.name.startsWith('拒绝'))throw{response:{status:400,data:{error:{fields:{description:['隔离校验：请调整描述']}}}}};let row=rows.find(r=>r.id===id);if(row)Object.assign(row,{name:payload.name.trim(),description:payload.description.trim(),promptHint:payload.promptHint.trim(),styleId:payload.styleId||''});else{const sample=samples[payload.mediaType];const url=URL.createObjectURL(payload.file);allowedMedia.add(url);row={...copy(base),id:'fixture-upload-'+rows.length,type:payload.mediaType,backendKind:sample?.kind||'reference',mimeType:payload.file.type,thumbnail:payload.mediaType==='image'?url:'',mediaUrl:url,name:payload.name.trim(),description:payload.description.trim(),promptHint:payload.promptHint.trim(),sourceInfo:'隔离上传来源（内存夹具；未调用服务端探测）',metadata:{...sample?.metadata,size_bytes:payload.file.size},styleId:''};rows.push(row)}if(payload.name.startsWith('超时'))throw{response:{status:504}};return copy(row)}
repository.createAsset=payload=>save(payload)
repository.updateAssetMetadata=(id,payload)=>save(payload,id)
assetReferenceApi.listSeries=async query=>{reads.value++;if(query.asset_id!==base.id)return{count:0,next:null,previous:null,results:[]};return{count:1,next:null,previous:null,results:[{id:'fixture-series',name:'原主图系列 · 隔离',role:'primary',owner_id:905,primary_series_id:null,bible_variant_id:null,version_count:1,variant_count:0}]}}
function selectFixture(){const input=document.querySelector<HTMLInputElement>('input[type=file]');if(!input||input.disabled)return;const transfer=new DataTransfer();transfer.items.add(new File([Uint8Array.from(atob(png),c=>c.charCodeAt(0))],'隔离图.png',{type:'image/png'}));input.files=transfer.files;input.dispatchEvent(new Event('change',{bubbles:true}))}
async function selectMedia(type:string){const input=document.querySelector<HTMLInputElement>('input[type=file]');if(!input||input.disabled)return;const sample=samples[type],transfer=new DataTransfer();const body=type==='audio'?wav:type==='script'?sampleScript:await(await window.fetch(videoUrl)).arrayBuffer();transfer.items.add(new File([body],'隔离文件.'+sample.extension,{type:sample.mime}));input.files=transfer.files;input.dispatchEvent(new Event('change',{bubbles:true}))}
const Screen={render:()=>h('main',{style:'padding:16px'},[h('h1','素材作者 · 内存隔离'),h('p','名称以拒绝／超时开头模拟错误。只使用内存数据，不连接现用后台。'),h('nav',{style:'position:fixed;top:0;right:0;z-index:9000;display:flex;flex-wrap:wrap;max-width:100%'},[h('button',{class:'button ghost',onClick:selectFixture},'填入隔离 PNG'),...Object.keys(samples).map(type=>h('button',{class:'button ghost',onClick:()=>selectMedia(type)},'填入隔离 '+type))]),h(AssetLibraryView),h('output',{style:'position:fixed;bottom:0;left:0;right:0;z-index:9000;pointer-events:none;background:#18312b;padding:5px;font-size:12px'},'隔离内存 · 写入 '+writes.value+' · 读取 '+reads.value+' · 网络守卫 '+blocked.value)])}
const pinia=createPinia();useAuthStore(pinia).user={id:905,username:'fixture',isStaff:true,isSuperuser:true} as any
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',redirect:'/assets'},{path:'/assets',name:'assets',component:Screen},{path:'/workflows',name:'workflows',component:{render:()=>h('p','任务入口：隔离不执行')}}]})
createApp({render:()=>h(RouterView)}).use(pinia).use(router).mount('#app')
