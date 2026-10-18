// Isolated UI data only; the formal upload/file/relationship API is tested separately.
import { createApp, h, reactive } from 'vue'
import AssetReferenceIngest from '../../src/components/AssetReferenceIngest.vue'
import { api } from '../../src/services/api'
import { repository } from '../../src/services/repository'
import { assetReferenceApi, type AssetReferenceSeries, type AssetReferenceVersion } from '../../src/services/assetReferenceApi'
import type { Asset } from '../../src/types'
import '../../src/styles.css'
import '../../src/platform.css'

api.defaults.adapter = async config => { throw Error('隔离页面禁止真实API：' + config.url) }
const audit = reactive({creates: 0, uploads: 0, attaches: 0, versions: 0})
const catalog: AssetReferenceSeries[] = [
  {id:'primary', name:'演示角色·主图', role:'primary', owner_id:1, primary_series_id:null, bible_variant_id:null, version_count:0, variant_count:2},
  ...['校服', '外套'].map((name,index) => ({id:`variant-${index}`, name:`演示角色·${name}`, role:'variant' as const, owner_id:1, primary_series_id:'primary', bible_variant_id:null, version_count:0, variant_count:0})),
]
const media: Asset[] = [{id:'asset-1', name:'隔离测试图片（非真实产物）', type:'image', mimeType:'image/png', checksum:'a'.repeat(64), isActive:true,
  thumbnail:'data:image/svg+xml,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="240" height="160"><rect width="240" height="160" fill="#244e5e"/><text x="30" y="90" fill="white" font-size="20">UI TEST FIXTURE</text></svg>'),
  description:'仅用于界面验证', mediaUrl:'', tags:[], createdAt:'2026-09-29', usedBy:0} as Asset]
assetReferenceApi.roles = async () => [{value:'primary', display_name:'主图'}, {value:'variant', display_name:'变体'}]
assetReferenceApi.listSeries = async params => {
  const rows = catalog.filter(row => (!params.role || row.role === params.role) && row.name.includes(params.search))
  return {count:rows.length, next:params.page*2<rows.length?'fixture:next':null, previous:params.page>1?'fixture:previous':null, results:rows.slice((params.page-1)*2,params.page*2)}
}
assetReferenceApi.createSeries = async input => {
  audit.creates++
  const row: AssetReferenceSeries = {...input, id:'created-'+audit.creates, owner_id:1, bible_variant_id:null, version_count:0, variant_count:0}
  catalog.push(row)
  return row
}
repository.getAssetPage = async () => ({count:media.length, results:media, next:null, previous:null})
repository.createAsset = async input => {
  audit.uploads++
  const row = {...media[0]!, id:'uploaded-'+audit.uploads, name:input.name}
  media.push(row)
  return row
}
const versions: AssetReferenceVersion[] = []
assetReferenceApi.attach = async input => {
  audit.attaches++
  if(audit.attaches === 1) throw {response:{status:400, data:{asset_id:['隔离失败夹具：首次关联失败，原素材仍保留']}}}
  const existing = versions.find(row => row.series_id===input.series_id && row.asset.id===input.asset_id && row.source===input.source)
  if(existing) return existing
  audit.versions++
  const asset = media.find(row => row.id===input.asset_id)!
  const row: AssetReferenceVersion = {id:'version-'+audit.versions, series_id:input.series_id, version:versions.filter(row=>row.series_id===input.series_id).length+1, source:input.source,
    asset:{id:asset.id, name:asset.name, kind:'reference', mime_type:'image/png', is_active:true}, generated_from_id:null, setting_revision_id:null}
  versions.push(row)
  return row
}
createApp({render:()=>h('main',{style:{maxWidth:'1060px', margin:'20px auto', padding:'12px'}},[
  h('h1','主图／变体入库 · 隔离页面'),
  h('p','真实组件与内存夹具，不连接业务库、不调用模型。第一次关联固定失败，用于验证重试；不代表真实上传或部署验收。'),
  h(AssetReferenceIngest),
  h('p',{'aria-label':'隔离调用计数'},`创建系列 ${audit.creates} · 上传 ${audit.uploads} · 关联尝试 ${audit.attaches} · 关联记录 ${audit.versions}`),
])}).mount('#app')
