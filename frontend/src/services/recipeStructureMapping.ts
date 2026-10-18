import type { JsonObject, ProductionRecipeEditorOptions } from './platformTypes'
import type { ExecutionContract } from './executionRegistry'
import { jsonChildSchemas, jsonInputErrors, jsonPropertyOptions } from './structuredInputForm'
import { pathSchema, pathType } from './structurePaths'
import { shotFieldSchema, shotFieldSelectionIssue, type ShotFieldSchemas } from './shotFieldMapping'

export type MappingCatalog = { fields: ExecutionContract['context_fields']; outputs: ProductionRecipeEditorOptions['textOutputOptions']; itemSchema?:JsonObject; shotFields?:ShotFieldSchemas }
export const mappingOutputIdentity = (stageKey: unknown, outputKey: unknown) => JSON.stringify([stageKey,outputKey])
export const mappingSelectedOutput = (node:JsonObject, outputs:MappingCatalog['outputs']) => outputs.find(row=>row.stageKey===node.stage_key&&row.outputKey===node.output_key)
export function mappingSourceSchema(node:JsonObject,catalog:MappingCatalog): JsonObject {
  if(node.kind==='shot_field')return shotFieldSchema(node,catalog.shotFields).schema
  if(node.kind==='item_field')return catalog.itemSchema||{}
  const source=node.kind==='context_field'?catalog.fields.find(row=>row.selector_key===node.selector_key):mappingSelectedOutput(node,catalog.outputs)
  return source?.value_schema||(source?.kind==='text'?{type:'string'}:{})
}
export function mappingValueSchema(node:JsonObject,catalog:MappingCatalog){
  if(node.kind==='shot_field'){
    const selected=shotFieldSchema(node,catalog.shotFields)
    if(selected.error)return selected
  }
  if(node.kind==='context_field'&&!catalog.fields.some(row=>row.selector_key===node.selector_key)
    ||node.kind==='selected_output'&&!mappingSelectedOutput(node,catalog.outputs))return {schema:{},error:'来源已不在当前目录中，请重新选择'}
  return pathSchema(mappingSourceSchema(node,catalog),mappingHas(node,'path')?node.path:node.kind==='item_field'?undefined:[])
}

export const mappingObject = (v: unknown): JsonObject => v && typeof v==='object' && !Array.isArray(v) ? v as JsonObject : {}
export function mappingHasMaterialDifference(node: JsonObject): boolean {
  if(node.kind==='material_difference')return true
  if(node.kind==='object')return Object.values(mappingObject(node.fields)).some(value=>mappingHasMaterialDifference(mappingObject(value)))
  if(node.kind==='array')return Array.isArray(node.items)&&node.items.some(value=>mappingHasMaterialDifference(mappingObject(value)))
  if(node.kind==='unit_map')return mappingHasMaterialDifference(mappingObject(node.value))
  if(node.kind==='each')return ['source','item'].some(key=>mappingHasMaterialDifference(mappingObject(node[key])))
  return false
}
export const mappingHas = (value: object,key:string) => Object.prototype.hasOwnProperty.call(value,key)
export const mappingHasDefault = (schemas: JsonObject[], key: string) => jsonChildSchemas(schemas,key,false).some(rule=>mappingHas(rule,'default'))
export function initializeMapping(kind: string, schema: JsonObject, defaultSchemas: JsonObject[] = []): JsonObject {
  if (kind==='object') return {kind,fields:Object.fromEntries(jsonPropertyOptions([schema]).filter(key=>!mappingHasDefault(defaultSchemas,key)).map(key=>[key,{}]))}
  if (kind==='array') return {kind,items:[]}
  if (kind==='unit_map') return {kind,value:{}}
  if (kind==='each') return {kind,source:{},item:{}}
  if (kind==='context_field') return {kind,selector_key:'',scope:{kind:''}}
  if (kind==='shot_field') return {kind,field:{structure_version_id:'',node_key:'',field_definition_version_id:''},scope:{kind:'shot'}}
  if (kind==='selected_output') return {kind,stage_key:'',output_key:'',scope:{kind:''},selection_policy:'reviewed_selected'}
  if (kind==='item_field') return {kind}
  if (kind==='literal') return {kind,value:mappingHas(schema,'const')?schema.const:null}
  return {kind}
}
export function mappingSourceChoices(fields: ExecutionContract['context_fields'], schema: JsonObject, scopes: string[], priorStages: string[]) {
  return fields.filter(row=>scopes.includes(row.scope_kind)
    && (!row.required_stage_key || priorStages.includes(row.required_stage_key))
    && (row.revision_kind!=='storyboard_approval'||priorStages.includes('storyboard_review'))
    && (row.revision_kind!=='bible_approval'||priorStages.includes('bible_review'))
    && (row.revision_dependencies || []).every(dependency=>priorStages.includes(dependency.stage_key))
    && (!schema.type || ['object','array'].includes(String(pathType(row.value_schema||{}))) || (row.kind==='text'?'string':pathType(row.value_schema||{}) || schema.type)===schema.type
      || row.value_schema?.type==='integer'&&schema.type==='number'))
}
export function mappingErrors(node: JsonObject, schema: JsonObject={}, path='结构映射', defaultSchemas: JsonObject[] = [], catalog?:MappingCatalog): string[] {
  const kind=node.kind, errors:string[]=[]
  if (!kind) return [`${path} 尚未选择来源`]
  if (kind==='literal') return jsonInputErrors(node.value,[schema],path,defaultSchemas)
  if (Array.isArray(schema.anyOf)) {
    const branches=schema.anyOf.map(branch=>mappingErrors(node,mappingObject(branch),path,defaultSchemas,catalog))
    const {anyOf: _branches,...outer}=schema
    return [...mappingErrors(node,outer,path,defaultSchemas,catalog),...(branches.some(errors=>!errors.length)?[]:branches[0] || [])]
  }
  const produced=({object:'object',unit_map:'object',array:'array',each:'array',unit_key:'string',reference_subjects:'array',material_difference:'string'} as Record<string,string>)[String(kind)]
  if(kind==='material_difference'&&Object.keys(node).some(key=>key!=='kind'))return [`${path} 的变体差异不能另填正文或路径`]
  if(produced&&schema.type&&produced!==schema.type)return [`${path} 的结构类型与目标不匹配`]
  if (kind==='context_field' && !node.selector_key) return [`${path} 尚未选择内容字段`]
  if (kind==='selected_output' && (!node.output_key||!node.stage_key)) return [`${path} 尚未选择已采用输出`]
  if (kind==='shot_field' && shotFieldSelectionIssue(node)) return [`${path}：${shotFieldSelectionIssue(node)}`]
  if (kind==='item_field' && (!Array.isArray(node.path)||mappingHas(node,'field'))) return [`${path} 尚未选择条目字段路径；不接受旧field字段`]
  if (catalog && ['context_field','selected_output','item_field','shot_field'].includes(String(kind))) {
    const selected=mappingValueSchema(node,catalog),actual=pathType(selected.schema)
    if(selected.error)errors.push(`${path}：${selected.error}`)
    if(kind==='item_field'&&!catalog.itemSchema)errors.push(`${path} 只能在逐条映射内读取条目`)
    if(actual&&schema.type&&actual!==schema.type&&!(actual==='integer'&&schema.type==='number'))errors.push(`${path} 的来源路径类型与目标不匹配`)
    if(mappingHas(node,'if_missing')) {
      const tokens=Array.isArray(node.path)?node.path:[]
      if(!tokens.length||typeof tokens[tokens.length-1]!=='string')errors.push(`${path} 缺失值只能配置在末级对象属性`)
      errors.push(...jsonInputErrors(node.if_missing,[selected.schema,schema],`${path}.缺失值`))
    }
  }
  if (kind==='object') {
    const fields=mappingObject(node.fields), properties=mappingObject(schema.properties)
    for(const key of Array.isArray(schema.required)?schema.required:[]) if(!mappingHas(fields,String(key))&&!mappingHasDefault(defaultSchemas,String(key))) errors.push(`${path}.${key} 是必需字段`)
    for(const [key,value] of Object.entries(fields)) {
      if(schema.additionalProperties===false && !mappingHas(properties,key)) errors.push(`${path}.${key} 不在目标合同内`)
      errors.push(...mappingErrors(mappingObject(value),mappingObject(properties[key] ?? schema.additionalProperties),`${path}.${key}`,jsonChildSchemas(defaultSchemas,key,false),catalog))
    }
  } else if(kind==='array') {
    const items=Array.isArray(node.items)?node.items:[]
    if(typeof schema.minItems==='number' && items.length<schema.minItems) errors.push(`${path} 至少需要 ${schema.minItems} 项`)
    if(typeof schema.maxItems==='number' && items.length>schema.maxItems) errors.push(`${path} 最多允许 ${schema.maxItems} 项`)
    items.forEach((value,index)=>errors.push(...mappingErrors(mappingObject(value),mappingObject(schema.items),`${path}[${index}]`,jsonChildSchemas(defaultSchemas,String(index),true),catalog)))
  } else if(kind==='unit_map') errors.push(...mappingErrors(mappingObject(node.value),mappingObject(schema.additionalProperties),`${path}.当前单元`,[],catalog))
  else if(kind==='each') {
    const source=mappingObject(node.source)
    errors.push(...mappingErrors(source,{type:'array'},`${path}.列表来源`,[],catalog))
    if(!['context_field','selected_output','item_field','shot_field'].includes(String(source.kind)))errors.push(`${path} 列表来源必须为声明的来源或条目路径`)
    const selected=catalog?mappingValueSchema(source,catalog):null
    if(catalog&&(!selected||selected.error||pathType(selected.schema)!=='array'))errors.push(`${path} 来源目录未声明该列表结构`)
    const itemSchema=selected?jsonChildSchemas([selected.schema],'0',true)[0]:undefined
    errors.push(...mappingErrors(mappingObject(node.item),mappingObject(schema.items),`${path}.逐条内容`,jsonChildSchemas(defaultSchemas,'0',true),catalog?{...catalog,itemSchema}:undefined))
  }
  return errors
}

export function mappingOutputChoices(outputs: ProductionRecipeEditorOptions['textOutputOptions'], schema: JsonObject, scopes: string[], priorStages: string[]) {
  return outputs.filter(row=>scopes.includes(row.scopeKind)&&priorStages.includes(row.stageKey)
    && (!schema.type||row.kind==='json'||schema.type==='string'))
}
