// Test transport only: real authors share this session's isolated structure catalog.
// Production hrefs stay visible; no application storage, API or credentials are used.
import { dataStructures } from '../../src/services/dataStructures'
import { fieldDefinitionVersions } from '../../src/services/fieldDefinitionVersions'
import { platformApi } from '../../src/services/platformApi'

const copy = <T,>(value:T):T => JSON.parse(JSON.stringify(value))
export function authorMaintenanceSession(initial:any, schema:any, read:()=>void, write:()=>void, deny:()=>never, fieldSeed?:{identity:any;version:any}) {
  const params=new URLSearchParams(location.search)
  if(params.get('maintenance')!=='1')return null
  const session=params.get('fixture_session')||crypto.randomUUID()
  const key='autoprompt-test:author-maintenance:'+session
  const defaults={parent_key:null,field_definition_version:null,property_name:'',position:0,required:false,allow_null:false,allow_blank:true,allow_empty:true,has_default:false,default_value:null,constraints:{}}
  const fields:any[]=[],nodes:any[]=[]
  function visit(value:any,parent:string|null=null,property='',required=false) {
    const node='node-'+nodes.length,type=Array.isArray(value.type)?value.type.find((entry:string)=>entry!=='null'):value.type
    const kind=({string:'text',integer:'integer',number:'number',boolean:'boolean',object:'object',array:'array'} as any)[type]
    if(!kind)throw Error('Undeclared isolated schema type')
    fields.push({id:'maintenance-field-'+node,field_definition:'maintenance-identity-'+node,field_key:node,version:1,status:'published',label:property||'输入对象',category:'fixture',value_type:kind,unit:'',description:'隔离公共定义',impact:'',value_options:[],preview_example:null})
    nodes.push({...copy(defaults),node_key:node,parent_key:parent,property_name:property,required,position:nodes.length,field_definition_version:fields.at(-1)!.id,allow_null:Array.isArray(value.type)&&value.type.includes('null')})
    for(const [name,child] of Object.entries(value.properties||{}))visit(child,node,name,(value.required||[]).includes(name))
    if(value.items)visit(value.items,node)
  }
  visit(schema)
  const seed={...copy(initial),nodes:copy(initial.nodes??nodes)},original=JSON.stringify(seed)
  if(!localStorage.getItem(key))localStorage.setItem(key,JSON.stringify([seed]))
  const rows=():any[]=>JSON.parse(localStorage.getItem(key)!)
  const fieldKey=key+':field'
  if(fieldSeed&&!localStorage.getItem(fieldKey))localStorage.setItem(fieldKey,JSON.stringify([fieldSeed.version]))
  const fieldRows=():any[]=>fieldSeed?JSON.parse(localStorage.getItem(fieldKey)!):[]
  const find=(id:string)=>{const value=rows().find(row=>row.id===id);if(!value)throw{response:{status:404}};return value}
  const oldGet=dataStructures.get,oldSchema=dataStructures.schema,oldField=fieldDefinitionVersions.get
  dataStructures.get=async id=>{if(!rows().some(row=>row.id===id))return oldGet(id);read();return copy(find(id))}
  dataStructures.schema=async id=>{if(!rows().some(row=>row.id===id))return oldSchema(id);read();return{valid:true,json_schema:copy(schema),errors:{}}}
  dataStructures.list=async(page=1,search='',status='',family)=>{read();const found=rows().filter(row=>row.name.includes(search)&&(!status||row.status===status)&&(!family||row.structure_key===family));return{count:found.length,results:copy(found.slice((page-1)*20,page*20)),hasNext:page*20<found.length,hasPrevious:page>1}}
  dataStructures.options=async()=>{read();return{node_defaults:copy(defaults),statuses:[{value:'draft',label:'草稿'},{value:'published',label:'已发布'}],value_schemas:{},constraint_schemas:{}}}
  fieldDefinitionVersions.get=async id=>{const field=[...fields,...fieldRows()].find(row=>row.id===id);if(!field)return oldField(id);read();return copy(field)}
  fieldDefinitionVersions.catalog=async()=>{read();const all=[...fields,...fieldRows().filter(row=>row.status==='published')];return{count:all.length,results:copy(all)}}
  platformApi.fieldDefinitionPage=async()=>{read();return{count:fieldSeed?1:0,results:fieldSeed?[copy(fieldSeed.identity)]:[],hasNext:false,hasPrevious:false}}
  if(fieldSeed){
    platformApi.getFieldDefinition=async id=>{if(id!==fieldSeed.identity.id)return deny();read();return copy(fieldSeed.identity)}
    fieldDefinitionVersions.options=async()=>{read();return{value_types:[{value:fieldSeed.version.value_type,label:'文本'}],statuses:[{value:'draft',label:'草稿'},{value:'published',label:'已启用'}]}}
    fieldDefinitionVersions.list=async id=>{if(id!==fieldSeed.identity.id)return deny();read();return copy(fieldRows())}
    fieldDefinitionVersions.create=async(id,content,source)=>{
      if(id!==fieldSeed.identity.id||source!==fieldSeed.version.id)return deny()
      write();const all=fieldRows(),next={...copy(fieldSeed.version),...copy(content),id:source+'-successor-'+all.length,version:Math.max(...all.map(row=>row.version))+1,status:'draft'}
      all.push(next);localStorage.setItem(fieldKey,JSON.stringify(all));return copy(next)
    }
    fieldDefinitionVersions.transition=async(id,action)=>{
      if(action!=='publish'||!id.startsWith(fieldSeed.version.id+'-successor-'))return deny()
      const all=fieldRows(),row=all.find(value=>value.id===id);if(!row)return deny()
      write();row.status='published';localStorage.setItem(fieldKey,JSON.stringify(all));return copy(row)
    }
  }
  dataStructures.create=async(content,source)=>{
    if(source!==initial.id)return deny()
    write();const all=rows(),next={...copy(find(source)),...copy(content),id:initial.id+'-successor-'+all.length,version:Math.max(...all.map(row=>row.version))+1,status:'draft'}
    all.push(next);localStorage.setItem(key,JSON.stringify(all));return copy(next)
  }
  dataStructures.transition=async(id,action)=>{
    if(action!=='publish'||!id.startsWith(initial.id+'-successor-'))return deny()
    const all=rows(),row=all.find(value=>value.id===id);if(!row)throw{response:{status:404}}
    write();row.status='published';localStorage.setItem(key,JSON.stringify(all));return copy(row)
  }
  // A test-only same-document transport allows the destination to mount the actual
  // FieldMaintenanceSettings with the original exact route, rather than a mock UI.
  document.addEventListener('click',event=>{
    const anchor=(event.target as Element)?.closest('a[target="_blank"]') as HTMLAnchorElement|null
    if(!anchor)return
    event.preventDefault()
    const target=new URL(anchor.href)
    const exactStructure=rows().some(row=>row.id===target.searchParams.get('structure_version'))
    const exactField=fieldSeed&&target.searchParams.get('field_definition')===fieldSeed.identity.id&&fieldRows().some(row=>row.id===target.searchParams.get('field_version'))
    if(target.origin!==location.origin||target.pathname!=='/settings'||target.searchParams.get('section')!=='fields'||!(exactStructure||exactField))return deny()
    const isolated=new URL(location.href)
    isolated.searchParams.set('fixture_session',session)
    isolated.searchParams.set('maintenance_route',target.pathname+target.search)
    window.open(isolated,'_blank','noopener')
  })
  return{route:params.get('maintenance_route')||'',report:()=>({originalUnchanged:JSON.stringify(find(initial.id))===original,rows:rows(),...(fieldSeed?{fieldOriginalUnchanged:JSON.stringify(fieldRows()[0])===JSON.stringify(fieldSeed.version),fieldRows:fieldRows()}: {})})}
}
