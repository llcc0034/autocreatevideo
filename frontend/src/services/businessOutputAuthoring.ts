import type {BusinessOutputVersion} from './businessOutputs'
import {outputFieldSources} from './outputFieldSources'
import {sameJson} from './structureAuthoring'
export function outputEditableContent(row:BusinessOutputVersion){
  return {name:row.name,description:row.description,parser_key:row.parser_key,data_structure_version:row.data_structure_version,quality_advisories:row.quality_advisories||{},
    ...(row.parser_key==='visual_rules.v2'?{allowed_values:row.parser_contract?.allowed_values||{},field_sources:outputFieldSources(row.parser_contract),response_keys:row.parser_contract?.response_keys||{}}:{}),
    ...(row.parser_key==='script_semantics.v1'?{semantic_mapping:row.parser_contract?.mapping||{}}:{})}
}
export function outputContentMatches(row:BusinessOutputVersion,attempt:Record<string,unknown>){
  const actual=outputEditableContent(row) as Record<string,unknown>
  return Object.entries(attempt).every(([key,value])=>sameJson(value,actual[key]))
}
// DRF trims these author labels; never normalize structured values or the form itself.
export function outputWriteMatches(row:BusinessOutputVersion,attempt:Record<string,unknown>){
  return outputContentMatches(row,{...attempt,...Object.fromEntries(['name','description'].filter(key=>typeof attempt[key]==='string').map(key=>[key,(attempt[key] as string).trim()]))})
}
