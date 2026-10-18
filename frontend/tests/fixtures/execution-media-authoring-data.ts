import { unusedLanguageForm } from './executionLanguage.ts'
export const mediaId = (n:number) => `30000000-0000-0000-0000-${String(n).padStart(12,'0')}`
export const mediaAssets:any[] = [
  ['image','橙色外套 · 原名 🟠','png'],['image','绿色校服 · 原名 🟢','png'],
  ['video','走廊视频 A','mp4'],['video','走廊视频 B','mp4'],['audio','脚步音轨（可配套或独立）','wav'],
].map(([kind,name,ext],i)=>({id:mediaId(i+1),name,type:kind,isActive:true,mimeType:`${kind}/${ext==='wav'?'wav':ext}`,checksum:String(i+1).repeat(64),metadata:{},hasEmbeddedAudio:kind==='video'}))
const input=(key:string,kind:string,parent:string|null=null)=>({key,kind,position:0,required:false,min_items:0,max_items:3,semantic_contract:{schema_version:1,allowed_purposes:[],allowed_subject_kinds:['external'],allowed_scope_kinds:['request'],paired_with_input_key:parent,allow_repeated_asset:true},targets:[{target_kind:'workflow_slot',target_key:key,is_primary:true}]})
export const mediaContractRaw:any={
  version_id:mediaId(20),revision:1,frozen:true,sha256:'a'.repeat(64),context_fields:[],
  input_forms:Object.fromEntries([['pictures','参考图片'],['clips','参考视频'],['paired','配套音轨'],['audio','独立音轨']].map(([key,label])=>[key,{label,description:'仅用于本次调用；不改写素材库属性。',suggestions:[],schemas:[]}])),
  subject_categories:[{value:'character',label:'人物'}],language_form:unusedLanguageForm(),
  definition:{version_id:mediaId(20),configuration:{deployment_id:mediaId(21),operation:'video.reference_to_video',mode:'',workflow_version_id:mediaId(22),prompt_mode:'none',prompt_profile_version_id:null,
    inputs:[{...input('pictures','image'),media_source_policy:{schema_version:1,allowed_sources:['asset_library','asset_upload'],default_source:'asset_library',allow_override:true,order:'provided'}},input('clips','video'),input('paired','audio','clips'),input('audio','audio')],parameters:[],auxiliaries:[]},
    interface:{output:{contract:{primary:{kind:'video'}}},parameters:{},input_purposes:{},prompt:{mode:'none',channels:{}}}},
}
export const mediaSources:any={schema_version:1,configuration_owner:'recipe_instance_or_profile',description:'内存隔离目录',orders:[{value:'provided',label:'提供顺序'}],sources:[
  {value:'asset_library',label:'素材库已有素材',kinds:['image','video','audio'],available:true,reader:'asset',requires:[],description:'正式素材'},
  {value:'asset_upload',label:'本地上传',kinds:['image','video','audio'],available:true,reader:'asset',requires:[],description:'入库再采用'},
]}
