// Real library/components with synthetic media and in-memory APIs. No business network access.
import { createApp, h } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createMemoryHistory, RouterView, useRoute } from 'vue-router'
import AssetLibraryView from '../../src/views/AssetLibraryView.vue'
import { api } from '../../src/services/api'
import { repository } from '../../src/services/repository'
import { useAuthStore } from '../../src/stores/auth'
import { assetReferenceApi, type AssetReferenceSeries, type AssetReferenceVersion } from '../../src/services/assetReferenceApi'
import type { Asset } from '../../src/types'
import '../../src/styles.css'
import '../../src/platform.css'

api.defaults.adapter = async config => { throw Error('隔离页面禁止真实API：' + config.url) }
const image = (color:string, label:string) => 'data:image/svg+xml,' + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360"><rect width="640" height="360" fill="${color}"/><text x="30" y="180" fill="white" font-size="32">${label}</text></svg>`)
const wav = new ArrayBuffer(44 + 16000); const view = new DataView(wav)
function tag(at:number, value:string) { [...value].forEach((c,i)=>view.setUint8(at+i,c.charCodeAt(0))) }
tag(0,'RIFF');view.setUint32(4,16036,true);tag(8,'WAVE');tag(12,'fmt ');view.setUint32(16,16,true);view.setUint16(20,1,true);view.setUint16(22,1,true);view.setUint32(24,8000,true);view.setUint32(28,16000,true);view.setUint16(32,2,true);view.setUint16(34,16,true);tag(36,'data');view.setUint32(40,16000,true)
const audio = URL.createObjectURL(new Blob([wav],{type:'audio/wav'}))
const canvas = document.createElement('canvas');canvas.width=640;canvas.height=360
const context=canvas.getContext('2d')!;context.fillStyle='#304c49';context.fillRect(0,0,640,360);context.fillStyle='white';context.font='30px sans-serif';context.fillText('SYNTHETIC VIDEO FIXTURE',24,180)
const stream=canvas.captureStream(12), recorder=new MediaRecorder(stream,{mimeType:'video/webm'}),chunks:Blob[]=[]
recorder.ondataavailable=e=>chunks.push(e.data)
const videoReady=new Promise<string>(resolve=>{recorder.onstop=()=>{stream.getTracks().forEach(t=>t.stop());resolve(URL.createObjectURL(new Blob(chunks,{type:'video/webm'})))}})
recorder.start();setTimeout(()=>recorder.stop(),1100);const video=await videoReady
const media = [
  ['p-old','主图旧版','image',image('#324d62','PRIMARY v1')],['p-new','主图新版','image',image('#384c78','PRIMARY v2')],
  ['uniform-old','校服旧版','image',image('#666046','UNIFORM v1')],['uniform-new','校服新版','image',image('#806236','UNIFORM v2')],
  ['coat','外套视频','video',video],['solo-video','独立视频','video',video],['voice','独立音频','audio',audio],['solo','独立图片','image',image('#665479','INDEPENDENT IMAGE')],
].map(([id,name,type,url])=>({id,name,type,mimeType:type==='image'?'image/svg+xml':type==='audio'?'audio/wav':'video/webm',mediaUrl:url,thumbnail:type==='image'?url:'',description:'隔离合成媒体；非真实生成',promptHint:'',sourceInfo:'仅用于M9-04页面测试',tags:[],styleId:'',usedBy:0,isActive:true,createdAt:'2026-09-29',updatedAt:'2026-09-29',checksum:'a'.repeat(64)} as Asset))
const series:AssetReferenceSeries[]=[
  {id:'primary',name:'演示人物·主图',role:'primary',owner_id:1,primary_series_id:null,bible_variant_id:null,version_count:2,variant_count:2},
  {id:'uniform',name:'演示人物·校服',role:'variant',owner_id:1,primary_series_id:'primary',bible_variant_id:'setting',version_count:2,variant_count:0},
  {id:'coat',name:'演示人物·外套',role:'variant',owner_id:1,primary_series_id:'primary',bible_variant_id:null,version_count:1,variant_count:0},
]
const versions:AssetReferenceVersion[]=[['base1','primary','p-old',1],['base2','primary','p-new',2],['u1','uniform','uniform-old',1],['u2','uniform','uniform-new',2],['coat1','coat','coat',1]].map(([id,sid,aid,n])=>{
  const asset=media.find(item=>item.id===aid)!
  return {id:String(id),series_id:String(sid),version:Number(n),source:id==='u2'?'generated':'existing',asset:{id:asset.id,name:asset.name,kind:asset.type,mime_type:asset.mimeType!,is_active:true},setting_revision_id:null,generated_from_id:id==='u2'?'base1':null,generation_job:id==='u2'?{id:'fixture-job',status:'succeeded'}:null,generated_artifact:null}
})
assetReferenceApi.roles=async()=>[{value:'primary',display_name:'主图'},{value:'variant',display_name:'变体'}]
function page<T>(rows:T[],number:number,size:number){return {count:rows.length,next:number*size<rows.length?'fixture:next':null,previous:number>1?'fixture:previous':null,results:rows.slice((number-1)*size,number*size)}}
assetReferenceApi.listSeries=async q=>{
  if(q.search==='失败')throw Error('隔离查询失败，请修改名称重试')
  return page(series.filter(s=>(!q.search||s.name.includes(q.search))&&(!q.role||s.role===q.role)&&(!q.primary_series_id||s.primary_series_id===q.primary_series_id)&&(q.bible_linked===undefined||!!s.bible_variant_id===q.bible_linked)&&(!q.media_type||versions.some(v=>v.series_id===s.id&&v.asset.mime_type.startsWith(q.media_type+'/')))),q.page,2)
}
assetReferenceApi.getSeries=async id=>({...series.find(s=>s.id===id)!})
assetReferenceApi.getVersion=async id=>versions.find(v=>v.id===id)!
assetReferenceApi.listVersions=async q=>page(versions.filter(v=>v.series_id===q.series_id).sort((a,b)=>b.version-a.version),q.page,1)
const bibleOptions=[{id:'setting',name:'校服设定',reference_name:'学生・制服',project_id:'project',project_title:'隔离项目',character_name:'演示人物',scene_name:null,prop_name:null},
  {id:'second-setting',name:'另一套设定',reference_name:'Uniform B',project_id:'project',project_title:'隔离项目',character_name:'演示人物',scene_name:null,prop_name:null}]
assetReferenceApi.bibleOptions=async(_id,q)=>page(bibleOptions.filter(b=>(!q.id||b.id===q.id)&&(!q.search||JSON.stringify(b).includes(q.search))),q.page,1)
assetReferenceApi.updateSeries=async(id,data)=>{
  if(data.name==='失败')throw Error('隔离保存失败：请修改名称后重试')
  const row=series.find(s=>s.id===id)!;Object.assign(row,data);return {...row}
}
assetReferenceApi.removalImpact=async(kind,id)=>{
  const target=kind==='series'?versions.filter(v=>v.series_id===id):versions.filter(v=>v.id===id)
  const children=kind==='series'?series.filter(s=>s.primary_series_id===id).length:0
  const dependents=versions.filter(v=>!target.includes(v)&&target.some(t=>t.id===v.generated_from_id)).length
  const productionUses=new URLSearchParams(location.search).has('production-use')&&target.some(t=>t.id==='base2')?2:0
  return {target_type:kind,id,name:id,version_count:target.length,asset_count:new Set(target.map(v=>v.asset.id)).size,variant_count:children,dependent_version_count:dependents,production_use_count:productionUses,can_remove:!children&&!dependents&&!productionUses}
}
assetReferenceApi.remove=async(kind,id)=>{
  if(!(await assetReferenceApi.removalImpact(kind,id)).can_remove)throw Error('仍有关系依赖')
  for(let i=versions.length-1;i>=0;i--)if(kind==='series'?versions[i].series_id===id:versions[i].id===id)versions.splice(i,1)
  if(kind==='series')series.splice(series.findIndex(s=>s.id===id),1)
  for(const s of series){s.version_count=versions.filter(v=>v.series_id===s.id).length;s.variant_count=series.filter(v=>v.primary_series_id===s.id).length}
}
repository.getAssets=async()=>media
repository.getAsset=async id=>{const asset=media.find(a=>a.id===id);if(!asset)throw Error('fixture asset missing');return asset}
for(const asset of media)asset.ownerId='1'
repository.deleteAsset=async id=>{
  if(versions.some(v=>v.asset.id===id))throw {response:{data:{error:{details:'此原素材仍有主图/变体版本关联，请先解除关系。'}}}}
  const index=media.findIndex(a=>a.id===id);if(index<0)throw Error('素材不存在');media.splice(index,1)
}
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',name:'assets',component:AssetLibraryView},{path:'/tasks',name:'workflows',component:{setup(){const route=useRoute();return()=>h('main',[h('h2','隔离任务跳转（无真实任务）'),h('pre',JSON.stringify(route.query)),h('button',{onClick:()=>router.push('/')},'返回素材库')])}}}]})
await router.push('/');await router.isReady()
const pinia=createPinia()
useAuthStore(pinia).user={id:'1',username:'fixture-owner',name:'Fixture',email:'',isStaff:false,isSuperuser:false,role:'member',mustChangePassword:false}
createApp({render:()=>h('main',{style:{maxWidth:'1180px',margin:'20px auto',padding:'12px'}},[h('p','M9-04/05 隔离测试：合成媒体与内存关系；修改和解除仅在内存，不调用真实API或模型。'),h(RouterView)])}).use(pinia).use(router).mount('#app')
