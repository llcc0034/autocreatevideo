import { createApp, h } from 'vue'
import DataStructureMappingPreview from '../../src/components/DataStructureMappingPreview.vue'
import { api } from '../../src/services/api'
import { dataStructures } from '../../src/services/dataStructures'
import '../../src/styles.css'
import '../../src/platform.css'

// Presentation fixture only. It never calls an API or evaluates real mappings.
api.defaults.adapter = async config => {throw Error('隔离页面禁止真实API：'+config.url)}
const rows = ['来源布局A','目标输入'].map((name,index)=>({id:'fixture-'+index,structure_key:'fixture',name,version:1,status:'draft' as const,description:'隔离结构',nodes:[]}))
dataStructures.list = async () => ({hasNext:false,hasPrevious:false,count:rows.length,results:rows})
dataStructures.get = async id => {const row=rows.find(row=>row.id===id);if(!row)throw Error('无此夹具');return row}
dataStructures.mappingOptions = async () => ({schema_version:1,nodes:[['path','字段路径'],['lookup','按稳定key引用'],['object','按字段组成对象'],['array','有序列表'],['literal','固定值'],['each','列表逐项映射']].map(([value,label])=>({value:value!,label:label!})),scopes:[{value:'root',label:'整个来源'},{value:'item',label:'当前列表条目'}]})
dataStructures.schema = async () => ({valid:true,json_schema:{type:'object',properties:{note:{type:'string',title:'自定义动作'},enabled:{type:'boolean',default:false}},required:['note'],additionalProperties:true},errors:{}})
dataStructures.mappingPreview = async (target,source,text) => {
  const report = {preview_only:true as const,valid:false,parsed:false,source_structure_version_id:source,target_structure_version_id:target,raw_value:null as unknown,source_value:null as unknown,value:null as unknown,defaults:[],provenance:[],warnings:[],errors:{} as Record<string,string[]>}
  if (text === '{bad') return {...report,errors:{source_text:['隔离错误回执：JSON 第1行第2列无法解析']}}
  return {...report,valid:true,parsed:true,raw_value:{note:'向门口走去'},source_value:{note:'向门口走去',enabled:false},value:{note:'向门口走去',enabled:false},provenance:[{path:'value',source_path:'source',kind:'path',source_structure_version_id:source,target_structure_version_id:target}]}
}
createApp({render:()=>h('main',{style:{maxWidth:'1100px',margin:'24px auto',padding:'16px'}},[h('h1','字段映射预览 · 隔离组件验证'),h('p','真实组件＋固定回执夹具（不验证解析服务）：输入 {bad 返回错误，其余输入返回固定展示结果。真实API已禁用。'),h(DataStructureMappingPreview)])}).mount('#app')
