// Test-only shared catalog for the two real author components in separate windows.
// Only this run's namespaced storage is touched; no application API or auth storage.
import {dataStructures} from '../../src/services/dataStructures'
import {fieldDefinitionVersions} from '../../src/services/fieldDefinitionVersions'
import {platformApi} from '../../src/services/platformApi'
import {scriptFixture} from './script-semantics-data'

const copy=<T,>(value:T):T=>JSON.parse(JSON.stringify(value))
export function semanticFixtureReport(saved:any,row:any,text:string){
  if(!saved||saved.revision!==row.revision||JSON.stringify(saved.parser_contract.mapping)!==JSON.stringify(scriptFixture.layouts.b.mapping)||text!==JSON.stringify(scriptFixture.layouts.b.raw,null,2))throw new Error('隔离夹具只返回原合同的明确样例，不执行修改后映射的新解析')
  return {...copy(scriptFixture.layouts.b.report),source_structure_version_id:row.data_structure_version,output_version_id:row.id,output_revision:row.revision,output_contract_sha256:row.contract_sha256}
}
export function outputMaintenanceSession(countRead:()=>void,countWrite:()=>void,deny:()=>never,initial:any[]){
  const params=new URLSearchParams(location.search)
  const session=params.get('fixture_session')||crypto.randomUUID()
  const key='autoprompt-test:output-maintenance:'+session
  const defaults={parent_key:null,field_definition_version:null,property_name:'',position:0,required:false,allow_null:false,allow_blank:true,allow_empty:true,has_default:false,default_value:null,constraints:{}}
  const fields:any[]=[],nodes:any[]=[]
  function visit(schema:any,parent:string|null=null,property='',required=false){
    const id='schema-node-'+nodes.length,type=({string:'text',integer:'integer',number:'number',boolean:'boolean',array:'array',object:'object'} as any)[schema.type]
    if(!type)throw Error('Unspecified fixture schema type')
    fields.push({id:'field-'+id,field_definition:'identity-'+id,field_key:id,version:1,status:'published',label:property||'响应对象',category:'fixture',value_type:type,unit:'',description:'隔离结构定义',impact:'',value_options:[],preview_example:null})
    nodes.push({...copy(defaults),node_key:id,parent_key:parent,property_name:property,field_definition_version:'field-'+id,required,position:nodes.length})
    for(const [name,child] of Object.entries(schema.properties||{}))visit(child,id,name,(schema.required||[]).includes(name))
    if(schema.items)visit(schema.items,id)
  }
  visit(scriptFixture.layouts.b.schema)
  const seed=copy(initial)
  seed.find(row=>row.id==='script-structure').nodes=nodes
  if(!localStorage.getItem(key))localStorage.setItem(key,JSON.stringify(seed))
  const rows=():any[]=>JSON.parse(localStorage.getItem(key)!)
  const find=(id:string)=>{const row=rows().find(row=>row.id===id);if(!row)throw{response:{status:404}};return row}
  for(const group of [fieldDefinitionVersions,platformApi])for(const name of Object.keys(group))(group as any)[name]=async()=>deny()
  platformApi.fieldDefinitionPage=async()=>{countRead();return{count:0,results:[],hasNext:false,hasPrevious:false}}
  fieldDefinitionVersions.get=async id=>{countRead();const field=fields.find(row=>row.id===id);if(!field)throw{response:{status:404}};return copy(field)}
  dataStructures.get=async id=>{countRead();return copy(find(id))}
  dataStructures.list=async(page=1,search='',status='',family)=>{countRead();const found=rows().filter(row=>row.name.includes(search)&&(!status||row.status===status)&&(!family||row.structure_key===family));return{count:found.length,results:copy(found.slice((page-1)*20,page*20)),hasNext:page*20<found.length,hasPrevious:page>1}}
  dataStructures.options=async()=>{countRead();return{node_defaults:copy(defaults),statuses:[{value:'draft',label:'草稿'},{value:'published',label:'可用'},{value:'retired',label:'停用'}],value_schemas:{},constraint_schemas:{}}}
  dataStructures.create=async(content,source)=>{
    if(source!=='script-structure')return deny()
    countWrite();const all=rows(),old=find(source),next={...copy(old),...copy(content),id:'script-successor-'+all.length,version:Math.max(...all.filter(row=>row.structure_key===old.structure_key).map(row=>row.version))+1,status:'draft'}
    all.push(next);localStorage.setItem(key,JSON.stringify(all));return copy(next)
  }
  dataStructures.transition=async(id,action)=>{
    if(action!=='publish'||!id.startsWith('script-successor-'))return deny()
    countWrite();const all=rows(),row=all.find(row=>row.id===id);if(!row)throw{response:{status:404}};row.status='published';localStorage.setItem(key,JSON.stringify(all));return copy(row)
  }
  dataStructures.schema=async id=>{countRead();const row=find(id);return{valid:true,json_schema:copy(row.structure_key==='script-structure-family'?scriptFixture.layouts.b.schema:{type:'object'}),errors:{},structure_version_id:id} as any}
  // Keep the production href intact for assertions. Rebase only transport to this
  // guarded fixture document; the destination mounts the real maintenance component.
  document.addEventListener('click',event=>{
    const anchor=(event.target as Element)?.closest('a[target="_blank"]')
    if(!anchor)return
    const target=new URL((anchor as HTMLAnchorElement).href)
    event.preventDefault()
    if(target.origin!==location.origin||target.pathname!=='/settings'||target.searchParams.get('section')!=='fields'||!rows().some(row=>row.id===target.searchParams.get('structure_version')))return deny()
    const isolated=new URL(location.pathname,location.origin)
    isolated.searchParams.set('fixture_session',session)
    isolated.searchParams.set('maintenance_route',target.pathname+target.search)
    window.open(isolated,'_blank','noopener')
  })
  return{rows,maintenanceRoute:params.get('maintenance_route')||'',key}
}
