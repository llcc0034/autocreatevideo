// Fixed presentation fixtures; backend relationship queries are covered separately.
import { createApp, h } from 'vue'
import { createRouter, createMemoryHistory, RouterView, useRoute } from 'vue-router'
import FieldMaintenanceSettings from '../../src/components/FieldMaintenanceSettings.vue'
import RegistryRelationsPanel from '../../src/components/RegistryRelationsPanel.vue'
import { api } from '../../src/services/api'
import { platformApi } from '../../src/services/platformApi'
import { fieldDefinitionVersions } from '../../src/services/fieldDefinitionVersions'
import { dataStructures } from '../../src/services/dataStructures'
import { relationOverviewUrl, type RelationRow, type RelationKind } from '../../src/services/registryRelations'
import '../../src/styles.css'
import '../../src/platform.css'

const field={id:'field-1',key:'camera.movement',isActive:true,displayOrder:1,createdAt:'',updatedAt:''}
const version={id:'version-1',field_definition:field.id,field_key:field.key,version:1,status:'retired' as const,label:'镜头变化',category:'camera',value_type:'text' as const,unit:'',description:'固定引用版本',impact:'',value_options:[],preview_example:'向前推近'}
const structure={id:'structure-1',structure_key:'series',name:'镜头输入',version:1,status:'retired' as const,description:'隔离结构',nodes:[]}
const relations:Partial<Record<RelationKind,RelationRow>>={}
function row(kind:RelationKind,id:string,label:string,config:string):RelationRow {return{id,kind,label,status:'retired',config_url:config,overview_url:relationOverviewUrl(kind,id),acceptance_url:'',notices:['固定展示夹具：关系查询已在隔离后端测试，当前页面不访问真实API。'],capabilities:[],groups:[]}}
relations.field=row('field',field.id,field.key,'/settings?section=fields&field_definition='+field.id)
relations.field_version=row('field_version',version.id,'镜头变化 · v1','/settings?section=fields&field_definition='+field.id+'&field_version='+version.id)
relations.structure=row('structure',structure.id,'镜头输入 · v1','/settings?section=fields&structure_version='+structure.id)
for(const [from,key,label,to] of [['field','versions','定义版本','field_version'],['field','structures','引用结构','structure'],['field_version','structures','引用结构','structure'],['structure','fields','节点字段版本','field_version']] as const) {
  const source=relations[from]!
  source.groups.push({key,label,kind:to,count:1,url:'/settings/acceptance-smoke?'+new URLSearchParams({view:'relations',kind:to,source_kind:from,source_id:source.id,relation:key})})
}
api.defaults.adapter=async config=>{
  if(config.method!=='get'||config.url!=='/registry-relations/')throw Error('隔离页面禁止真实API与写入：'+config.url)
  const selected=relations[config.params.kind as RelationKind]
  const results=selected&&(!config.params.ids||config.params.ids===selected.id)?[selected]:[]
  return{config,status:200,statusText:'OK',headers:{},data:{count:results.length,results,next:null,previous:null,generated_at:'2026-09-29T03:00:00+08:00',context_label:'固定夹具 · 精确版本关联'}}
}
platformApi.fieldDefinitionPage=async()=>({count:1,results:[field],hasNext:false,hasPrevious:false})
platformApi.getFieldDefinition=async id=>{if(id!==field.id)throw Error('无此隔离字段');return field}
fieldDefinitionVersions.options=async()=>({value_types:[{value:'text',label:'文字'}],statuses:[{value:'retired',label:'停用'}]})
fieldDefinitionVersions.list=async()=>[] // The requested version is intentionally outside the list.
fieldDefinitionVersions.get=async id=>{if(id!==version.id)throw Error('无此隔离版本');return version}
dataStructures.list=async()=>({hasNext:false,hasPrevious:false,count:0,results:[]})
dataStructures.get=async id=>{if(id!==structure.id)throw Error('无此隔离结构');return structure}
dataStructures.options=async()=>({statuses:[{value:'retired',label:'停用'}],node_defaults:{parent_key:null,field_definition_version:null,property_name:'',position:0,required:false,allow_null:false,allow_blank:true,allow_empty:true,has_default:false,default_value:null,constraints:{}},constraint_schemas:{},value_schemas:{}})
const Settings={setup(){const route=useRoute();return()=>h(FieldMaintenanceSettings,{fieldDefinitionId:String(route.query.field_definition||''),fieldVersionId:String(route.query.field_version||''),structureVersionId:String(route.query.structure_version||'')})}}
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/settings',component:Settings},{path:'/settings/acceptance-smoke',component:RegistryRelationsPanel}]})
await router.push('/settings?section=fields')
createApp({render:()=>h('main',{style:{maxWidth:'1100px',margin:'20px auto',padding:'12px'}},[h('h1','字段引用与导航 · 隔离验证'),h('p','真实组件＋固定目录；所有真实API和写入均被禁用，不代表现用环境验收。'),h(RouterView)])}).use(router).mount('#app')
