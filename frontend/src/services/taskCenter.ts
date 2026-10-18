import { adaptPreparationOutcome, type PreparationOutcome } from './preparationOutcome.ts'
import { adaptFrozenReferenceBinding } from './frozenReferenceBinding.ts'
export type TaskKind = 'preparation' | 'generation_task' | 'assembly'
export type TaskCategory = 'production' | 'asset' | 'smoke'
export interface TaskChoice { value: string; label: string }
export interface TaskRow {
  kind: TaskKind; id: string; category: TaskCategory; title: string; status: string; operation: string
  project_id: string | null; project_title: string | null; production_run_id: string | null
  execution_instance_version_id: string | null; execution_instance_name: string | null; execution_version: number | null
  preparation_id: string | null; source_id: string | null; source_kind: string
  created_at: string; updated_at: string; finished_at: string | null
  estimated_cost: string | null; actual_cost: string | null; cost_currency: string; anomaly: boolean
}
export interface TaskPage {
  count: number; next: string | null; previous: string | null; results: TaskRow[]
  counts: Record<'all' | TaskCategory, number>; read_at: string
}
export interface TaskCall {
  retry_of_id: string | null
  kind: string; id: string; role: string; status: string; operation: string; preparation_id: string | null
  created_at: string; finished_at: string | null; error_code: string
}
export interface TaskPromptTrace {
  state:'recorded'|'unavailable'; message:string; profile_id:string|null; profile_sha256:string|null
  fields:{field_key:string;source_selector:string;channel:string;defaulted:boolean;reviewed:boolean;projection_primitive:string;
    sources:{input_key:string;kind:string|null;location:string|null;source_id:string|null;value_sha256:string|null;edited:boolean|null}[]}[]
  channels:{channel:string;characters:number;state:'matched'|'mismatch'|'unavailable'|'omitted';destination:string;message:string}[]
}
export interface TaskSubmissionEvidence {
  state:'recorded'|'unavailable'|'inconsistent';started_at:string|null;registered_at:string|null
  identifier_state:'recorded'|'digest_only'|'not_recorded'|'unavailable'|'inconsistent';message:string;notes:string[]
}
export interface TaskDetail extends TaskRow {
  outcome: PreparationOutcome | null
  submission:TaskSubmissionEvidence
  actions: {cancel_smoke: boolean; cancel_asset: boolean}
  purpose: string | null; preparation_status: string | null; request_frozen_at: string | null; request_sha256: string | null
  input: { state: string; submitted: unknown; resolved: unknown
    parameters: { key: string; value: unknown; source: string }[]
    prompts: { key: string; role: string; text: string; source: string }[]; prompt_trace:TaskPromptTrace|null; field_batch_trace:TaskFieldBatchTrace|null; media_trace:TaskMediaTrace|null; api_mapping:TaskApiMapping|null; native_input_trace:TaskNativeInputTrace|null; system_message_trace:TaskSystemMessageTrace|null; notes: string[] }
  output: { state: string; result: unknown; text: string; media: unknown[]; notes: string[] }
  validation: { status: string; warnings: string[]; error_code: string; error_message: string }
  calls: TaskCall[]; call_count: number; calls_next: string | null; calls_previous: string | null
  output_count: number; output_next: string | null; output_previous: string | null
  deferred_fields: {field: string; sha256: string; size_bytes: number; total_characters: number; encoding: string}[]
  content?: {field: string; text: string; offset: number; next_offset: number | null; total_characters: number; sha256: string}
  links: { generation_task_id: string | null; asset_job_id: string | null }
}
export interface TaskFilters {
  unit_key?: string
  category?: string; status?: string; project_id?: string; production_run_id?: string; stage_run_id?: string; execution_instance_version_id?: string; operation?: string
  created_after?: string; created_before?: string; anomaly?: string; source_id?: string; cursor?: string; page_size?: number
}

/** Measure the already-authorized display text, never a reconstructed request.
 * Totals include every row; equality is exact, not semantic deduplication.
 */
export function summarizeTaskPrompts(prompts: TaskDetail['input']['prompts']) {
  const encoder = new TextEncoder()
  const identical = new Map<string, number[]>()
  let characters = 0, utf8Bytes = 0
  const rows = prompts.map((prompt, index) => {
    let count = 0
    for (const _character of prompt.text) count += 1
    const bytes = encoder.encode(prompt.text).byteLength
    characters += count
    utf8Bytes += bytes
    // Empty channels are explicit facts, not evidence of repeated content.
    if (prompt.text !== '') {
      const indexes = identical.get(prompt.text)
      if (indexes) indexes.push(index)
      else identical.set(prompt.text, [index])
    }
    return { characters: count, utf8Bytes: bytes }
  })
  return { rows, characters, utf8Bytes, identicalGroups: [...identical.values()].filter(indexes => indexes.length > 1) }
}
const fail = (key: string): never => { throw new Error('任务中心响应不完整：' + key) }
const object = (v: unknown): Record<string, any> => v !== null && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, any> : fail('对象')
const text = (v: unknown, key: string): string => typeof v === 'string' ? v : fail(key)
const nullable = (v: unknown, key: string): string | null => v === null ? null : text(v, key)
const array = (v: unknown, key: string): any[] => Array.isArray(v) ? v : fail(key)
const number = (v: unknown, key: string): number => Number.isSafeInteger(v) && Number(v) >= 0 ? Number(v) : fail(key)
const strings = (v: unknown, key: string) => array(v, key).map(x => text(x, key))
export interface TaskApiMapping {
  state:'verified'|'unavailable';code:string;message:string;deployment_id:string;configuration_sha256:string
  rows:{key:string;field:string|null;state:'included'|'omitted'|'unmapped'}[]
}
export interface TaskNativeInputTrace {
  state:'matched'|'mismatch'|'partial'|'unavailable';message:string;unmapped_count:number
  rows:{label:string;source:string;destination:string;state:'matched'|'mismatch'|'unavailable'}[]
}
export interface TaskSystemMessageTrace {
  state:'matched'|'mismatch'|'unavailable';message:string
  rows:{label:string;source:string;start:number;end:number;state:'matched'|'mismatch'}[]
}
function adaptSystemMessageTrace(value:unknown):TaskSystemMessageTrace|null {
  if(value===null)return null
  const r=object(value)
  if(!['matched','mismatch','unavailable'].includes(r.state))fail('system_message_trace.state')
  const rows=array(r.rows,'system_message_trace.rows')
  if(rows.length>11)fail('system_message_trace.rows')
  let previousEnd=0
  return {state:r.state,message:text(r.message,'system_message_trace.message'),rows:rows.map(value=>{
    const row=object(value),start=number(row.start,'start'),end=number(row.end,'end')
    if(start!==previousEnd||end<=start||!['matched','mismatch'].includes(row.state))fail('system_message_trace.row')
    previousEnd=end
    return {label:text(row.label,'label'),source:text(row.source,'source'),start,end,state:row.state}
  })}
}
function adaptNativeInputTrace(value:unknown):TaskNativeInputTrace|null {
  if(value===null)return null
  const r=object(value)
  if(!['matched','mismatch','partial','unavailable'].includes(r.state))fail('native_input_trace.state')
  return {state:r.state,message:text(r.message,'native_input_trace.message'),unmapped_count:number(r.unmapped_count,'unmapped_count'),
    rows:array(r.rows,'native_input_trace.rows').map(value=>{const row=object(value)
      if(!['matched','mismatch','unavailable'].includes(row.state))fail('native_input_trace.row.state')
      return {label:text(row.label,'label'),source:text(row.source,'source'),destination:text(row.destination,'destination'),state:row.state}
    })}
}
export function adaptApiMapping(v:unknown):TaskApiMapping|null {
  if(v===null)return null
  const r=object(v)
  if(!['verified','unavailable'].includes(r.state))fail('api_mapping.state')
  return {state:r.state,code:text(r.code,'api_mapping.code'),message:text(r.message,'api_mapping.message'),
    deployment_id:text(r.deployment_id,'deployment_id'),configuration_sha256:text(r.configuration_sha256,'configuration_sha256'),
    rows:array(r.rows,'api_mapping.rows').map(value=>{const row=object(value)
      if(!['included','omitted','unmapped'].includes(row.state))fail('api_mapping.row.state')
      return {key:text(row.key,'api_mapping.key'),field:nullable(row.field,'api_mapping.field'),state:row.state}
    })}
}
export interface TaskMediaProcessingSummary {
  state:'not_applicable'|'declared'|'recorded'|'partial'|'unverifiable';label:string;description:string;configuration_source:string;shared_with:string|null;message:string
  byte_identity:'not_applicable'|'unavailable'|'same'|'changed'
  declared:TaskMediaProcessingRow[];observed:TaskMediaProcessingRow[]
}
export interface TaskMediaProcessingRow {key:string;label:string;unit:string;source:string;state:'recorded'|'missing'|'unavailable'|'invalid';value:string|number|boolean|null}
function adaptMediaProcessing(value:unknown):TaskMediaProcessingSummary {
  const r=object(value)
  if(!['not_applicable','declared','recorded','partial','unverifiable'].includes(r.state)||!['not_applicable','unavailable','same','changed'].includes(r.byte_identity))fail('processing_summary state')
  const rows=(v:unknown)=>array(v,'processing_summary rows').map(value=>{const row=object(value)
    if(!['recorded','missing','unavailable','invalid'].includes(row.state))fail('processing_summary row state')
    const v=row.value
    if(row.state!=='recorded'?v!==null:!(typeof v==='string'||typeof v==='boolean'||(typeof v==='number'&&Number.isFinite(v)&&v>=0)))fail('processing_summary row value')
    return {key:text(row.key,'key'),label:text(row.label,'label'),unit:text(row.unit,'unit'),source:text(row.source,'source'),state:row.state,value:v}
  })
  const declared=rows(r.declared),observed=rows(r.observed)
  if((r.state==='not_applicable'&&(declared.length||observed.length||r.byte_identity!=='not_applicable'))
     ||(r.state==='declared'&&(observed.length||r.byte_identity!=='unavailable')))fail('processing_summary evidence')
  return {state:r.state,label:text(r.label,'label'),description:text(r.description,'description'),configuration_source:text(r.configuration_source,'configuration_source'),
    shared_with:nullable(r.shared_with,'shared_with'),message:text(r.message,'message'),byte_identity:r.byte_identity,declared,observed}
}
export interface TaskMediaTrace {
  state:'recorded'; workflow_version_id:string; request_sha256:string; root_id:string|null; message:string; media_profile:unknown
  scalar_wiring:{state:'matched'|'mismatch'|'partial'|'unavailable';contract_sha256:string;message:string;
    rows:{semantic_key:string;value_type:'string'|'enum'|'integer'|'number'|'boolean';node_id:string;input_name:string;class_type:string;state:'matched'|'mismatch'|'omitted'|'unavailable';message:string}[]}
  items:{reference_binding:import('./frozenReferenceBinding').FrozenReferenceBinding;item_key:string;input_key:string;kind:string;display_name:string;asset_id:string|null;artifact_id:string|null;
    origin:unknown;purpose:string|null;paired_to:string|null;source_facts_sha256:string;source_bytes_sha256:string;input_label:string|null;
    mode:'workflow_input'|'embedded_audio'|'context_only';ports:unknown[];graph_inputs:{node_id:string;class_type:string;input_path:(string|number)[]}[];
    port_routes:{target_key:string;destination:{node_id:string;input_name:string}|null;state:'connected'|'disconnected'|'unavailable';entry_node_id:string|null;edge_indexes:number[];message:string}[];
    processing_summary:TaskMediaProcessingSummary;processing:unknown;adapter:string|null;receipt_state:'recorded'|'missing'|'not_applicable';receipt:unknown;processing_metadata:unknown}[]
  nodes:{node_id:string;class_type:string;origin:'original'|'added'|'replaced'|'unavailable';removed_inputs:string[];
    inputs:{input_name:string;change:'unchanged'|'changed'|'added'|'unavailable';kind:'literal'|'connection'|'media'|'redacted';value:unknown}[]}[]
  edges:{from_node:string;output_index:number;to_node:string;input_name:string}[]
}
function adaptMediaTrace(value:unknown):TaskMediaTrace|null {
  if(value===null)return null
  const r=object(value)
  if(r.state!=='recorded')fail('media_trace.state')
  if(!('media_profile' in r))fail('media_profile')
  const wiring=object(r.scalar_wiring)
  if(!['matched','mismatch','partial','unavailable'].includes(wiring.state))fail('scalar_wiring.state')
  const edges=array(r.edges,'edges').map(value=>{const e=object(value);return{from_node:text(e.from_node,'from_node'),output_index:number(e.output_index,'output_index'),to_node:text(e.to_node,'to_node'),input_name:text(e.input_name,'input_name')}})
  return {state:r.state,workflow_version_id:text(r.workflow_version_id,'workflow_version_id'),request_sha256:text(r.request_sha256,'request_sha256'),
    root_id:nullable(r.root_id,'root_id'),message:text(r.message,'message'),media_profile:r.media_profile,
    scalar_wiring:{state:wiring.state,contract_sha256:text(wiring.contract_sha256,'scalar_wiring.contract_sha256'),message:text(wiring.message,'scalar_wiring.message'),
      rows:array(wiring.rows,'scalar_wiring.rows').map(value=>{const row=object(value)
        if(!['matched','mismatch','omitted','unavailable'].includes(row.state)||!['string','enum','integer','number','boolean'].includes(row.value_type))fail('scalar_wiring row')
        return {semantic_key:text(row.semantic_key,'semantic_key'),value_type:row.value_type,node_id:text(row.node_id,'node_id'),input_name:text(row.input_name,'input_name'),
          class_type:text(row.class_type,'class_type'),state:row.state,message:text(row.message,'scalar_wiring.row.message')}
      })},
    items:array(r.items,'media_trace.items').map(value=>{const i=object(value)
      if(!['workflow_input','embedded_audio','context_only'].includes(i.mode)||!['recorded','missing','not_applicable'].includes(i.receipt_state))fail('media_trace use state')
      for(const key of ['origin','processing','receipt','processing_metadata'])if(!(key in i))fail(key)
      return {item_key:text(i.item_key,'item_key'),input_key:text(i.input_key,'input_key'),kind:text(i.kind,'kind'),display_name:text(i.display_name,'display_name'),
        reference_binding:adaptFrozenReferenceBinding(i.reference_binding),
        asset_id:nullable(i.asset_id,'asset_id'),artifact_id:nullable(i.artifact_id,'artifact_id'),origin:i.origin,purpose:nullable(i.purpose,'purpose'),paired_to:nullable(i.paired_to,'paired_to'),
        source_facts_sha256:text(i.source_facts_sha256,'source_facts_sha256'),source_bytes_sha256:text(i.source_bytes_sha256,'source_bytes_sha256'),input_label:nullable(i.input_label,'input_label'),
        mode:i.mode,ports:array(i.ports,'ports'),graph_inputs:array(i.graph_inputs,'graph_inputs').map(value=>{const p=object(value)
          return {node_id:text(p.node_id,'node_id'),class_type:text(p.class_type,'class_type'),input_path:array(p.input_path,'input_path').map(v=>typeof v==='string'?v:number(v,'input_path'))}
        }),port_routes:array(i.port_routes,'port_routes').map(value=>{const route=object(value)
          if(!['connected','disconnected','unavailable'].includes(route.state))fail('port_routes.state')
          const d=route.destination===null?null:object(route.destination)
          const destination=d===null?null:{node_id:text(d.node_id,'destination.node_id'),input_name:text(d.input_name,'destination.input_name')}
          const indexes=array(route.edge_indexes,'edge_indexes').map(v=>{const n=number(v,'edge_index');if(!Number.isInteger(n)||n>=edges.length)fail('edge_index');return n})
          const entry=nullable(route.entry_node_id,'entry_node_id')
          if(new Set(indexes).size!==indexes.length||(route.state==='unavailable')!==(destination===null)
             ||(route.state==='connected')!==(entry!==null)||(route.state!=='connected'&&indexes.length))fail('port_routes evidence')
          if(indexes.length){
            const path=indexes.map(index=>edges[index]!)
            if(path[0]!.from_node!==entry||path[path.length-1]!.to_node!==destination?.node_id||path[path.length-1]!.input_name!==destination?.input_name
               ||path.some((edge,index)=>index>0&&path[index-1]!.to_node!==edge.from_node))fail('port_routes continuity')
          }
          return{target_key:text(route.target_key,'target_key'),destination,state:route.state,entry_node_id:entry,edge_indexes:indexes,message:text(route.message,'port_routes.message')}
        }),processing_summary:adaptMediaProcessing(i.processing_summary),processing:i.processing,adapter:nullable(i.adapter,'adapter'),receipt_state:i.receipt_state,receipt:i.receipt,processing_metadata:i.processing_metadata}
    }),nodes:array(r.nodes,'nodes').map(value=>{const n=object(value)
      if(!['original','added','replaced','unavailable'].includes(n.origin))fail('media_node.origin')
      const removed=array(n.removed_inputs,'removed_inputs').map(value=>text(value,'removed_input'))
      const inputs=array(n.inputs,'node.inputs').map(value=>{const row=object(value)
        if(!['unchanged','changed','added','unavailable'].includes(row.change)||!['literal','connection','media','redacted'].includes(row.kind)||!('value' in row))fail('media_node.input')
        if((row.kind==='media'||row.kind==='redacted')&&row.value!==null)fail('media_node.protected_value')
        if(row.kind==='connection'){
          const link=array(row.value,'media_node.link')
          if(link.length!==2||typeof link[0]!=='string'||!link[0]||typeof link[1]!=='number'||!Number.isInteger(link[1])||link[1]<0)fail('media_node.link')
        }
        if((n.origin==='added'&&row.change!=='added')||(['replaced','unavailable'].includes(n.origin)&&row.change!=='unavailable')||(n.origin==='original'&&row.change==='unavailable'))fail('media_node.baseline')
        return{input_name:text(row.input_name,'input_name'),change:row.change,kind:row.kind,value:row.value}
      })
      if((n.origin!=='original'&&removed.length)||new Set(removed).size!==removed.length||new Set(inputs.map(row=>row.input_name)).size!==inputs.length||inputs.some(row=>removed.includes(row.input_name)))fail('media_node.inputs')
      return{node_id:text(n.node_id,'node_id'),class_type:text(n.class_type,'class_type'),origin:n.origin,removed_inputs:removed,inputs}
    }),
    edges}
}
export interface TaskFieldBatchTrace {
  state:'matched'|'mismatch'|'unavailable'; message:string; profile_id:string|null; profile_sha256:string|null; batch_sha256:string|null; destination:string
  items:{request_id:string;unit_key:string;field_key:string;source_selector:string;generation_source_selector:string;channel:string;
    generation_mode:string;generation_instruction:string;language_policy:string;generation_output_semantics:string;item_path:string;
    source_characters:number;source_language:string;target_language:string;context_selectors:string[];
    protected_count:number;logical_input_count:number;subject_fact_count:number}[]
}
function adaptFieldBatchTrace(value:unknown):TaskFieldBatchTrace|null {
  if(value===null)return null
  const r=object(value)
  if(!['matched','mismatch','unavailable'].includes(r.state))fail('field_batch_trace.state')
  return {state:r.state,message:text(r.message,'field_batch_trace.message'),profile_id:nullable(r.profile_id,'profile_id'),
    profile_sha256:nullable(r.profile_sha256,'profile_sha256'),batch_sha256:nullable(r.batch_sha256,'batch_sha256'),destination:text(r.destination,'destination'),
    items:array(r.items,'field_batch_trace.items').map(value=>{const i=object(value)
      if(!['model_generated','translate_only'].includes(i.generation_mode))fail('generation_mode')
      return {request_id:text(i.request_id,'request_id'),unit_key:text(i.unit_key,'unit_key'),field_key:text(i.field_key,'field_key'),
        source_selector:text(i.source_selector,'source_selector'),generation_source_selector:text(i.generation_source_selector,'generation_source_selector'),
        channel:text(i.channel,'channel'),generation_mode:text(i.generation_mode,'generation_mode'),generation_instruction:text(i.generation_instruction,'generation_instruction'),
        language_policy:text(i.language_policy,'language_policy'),generation_output_semantics:text(i.generation_output_semantics,'generation_output_semantics'),
        item_path:text(i.item_path,'item_path'),source_characters:number(i.source_characters,'source_characters'),source_language:text(i.source_language,'source_language'),
        target_language:text(i.target_language,'target_language'),context_selectors:strings(i.context_selectors,'context_selectors'),
        protected_count:number(i.protected_count,'protected_count'),logical_input_count:number(i.logical_input_count,'logical_input_count'),subject_fact_count:number(i.subject_fact_count,'subject_fact_count')}
    })}
}
function adaptPromptTrace(value:unknown):TaskPromptTrace|null {
  if(value===null)return null
  const r=object(value)
  if(!['recorded','unavailable'].includes(r.state))fail('prompt_trace.state')
  return {state:r.state,message:text(r.message,'prompt_trace.message'),profile_id:nullable(r.profile_id,'profile_id'),profile_sha256:nullable(r.profile_sha256,'profile_sha256'),
    fields:array(r.fields,'prompt_trace.fields').map(value=>{const f=object(value)
      if(typeof f.defaulted!=='boolean'||typeof f.reviewed!=='boolean')fail('prompt_trace.field flags')
      return {field_key:text(f.field_key,'field_key'),source_selector:text(f.source_selector,'source_selector'),channel:text(f.channel,'channel'),defaulted:f.defaulted,reviewed:f.reviewed,
        projection_primitive:text(f.projection_primitive,'projection_primitive'),sources:array(f.sources,'sources').map(value=>{const s=object(value)
          if(s.edited!==null&&typeof s.edited!=='boolean')fail('source.edited')
          return {input_key:text(s.input_key,'input_key'),kind:nullable(s.kind,'kind'),location:nullable(s.location,'location'),source_id:nullable(s.source_id,'source_id'),value_sha256:nullable(s.value_sha256,'value_sha256'),edited:s.edited}
        })}
    }),channels:array(r.channels,'prompt_trace.channels').map(value=>{const c=object(value)
      if(!['matched','mismatch','unavailable','omitted'].includes(c.state))fail('prompt_trace.channel.state')
      return {channel:text(c.channel,'channel'),characters:number(c.characters,'characters'),state:c.state,destination:text(c.destination,'destination'),message:text(c.message,'message')}
    })}
}
function adaptContent(value: unknown): NonNullable<TaskDetail['content']> {
  const c=object(value), result={field:text(c.field,'content.field'),text:text(c.text,'content.text'),
    offset:number(c.offset,'content.offset'),next_offset:c.next_offset===null?null:number(c.next_offset,'content.next_offset'),
    total_characters:number(c.total_characters,'content.total_characters'),sha256:text(c.sha256,'content.sha256')}
  if(!/^[a-f0-9]{64}$/.test(result.sha256)||result.offset+Array.from(result.text).length>result.total_characters)fail('content bounds')
  if(result.next_offset!==null&&result.next_offset!==result.offset+Array.from(result.text).length)fail('content next_offset')
  return result
}
export function adaptTaskRow(value: unknown): TaskRow {
  const r = object(value)
  if (!['preparation','generation_task','assembly'].includes(r.kind)) fail('任务来源')
  if (!['production','asset','smoke'].includes(r.category)) fail('任务类别')
  if (typeof r.anomaly !== 'boolean') fail('关联状态')
  const result = { ...r } as TaskRow
  for (const key of ['id','title','status','operation','source_kind','created_at','updated_at','cost_currency']) text(r[key], key)
  for (const key of ['source_id','project_id','project_title','production_run_id','execution_instance_version_id','execution_instance_name','preparation_id','finished_at','estimated_cost','actual_cost']) nullable(r[key], key)
  if (r.execution_version !== null) number(r.execution_version, 'execution_version')
  return result
}
export function adaptTaskPage(value: unknown): TaskPage {
  const r = object(value), counts = object(r.counts)
  for (const key of ['all','production','asset','smoke']) number(counts[key], key)
  return { count: number(r.count, 'count'), next: nullable(r.next,'next'), previous: nullable(r.previous,'previous'),
    results: array(r.results,'results').map(adaptTaskRow), counts: counts as TaskPage['counts'], read_at: text(r.read_at,'read_at') }
}
function adaptSubmission(value:unknown):TaskSubmissionEvidence {
  const r=object(value)
  if(!['recorded','unavailable','inconsistent'].includes(r.state)||!['recorded','digest_only','not_recorded','unavailable','inconsistent'].includes(r.identifier_state))fail('submission.state')
  const date=(value:unknown,key:string)=>{const v=nullable(value,key);if(v!==null&&(!/^\d{4}-\d\d-\d\dT/.test(v)||!Number.isFinite(Date.parse(v))))fail(key);return v}
  const started_at=date(r.started_at,'submission.started_at'),registered_at=date(r.registered_at,'submission.registered_at')
  if(r.state==='unavailable'&&(started_at!==null||registered_at!==null||r.identifier_state!=='unavailable'))fail('submission.unavailable')
  if(r.state==='recorded'&&(r.identifier_state==='inconsistent'||r.identifier_state==='unavailable'||(started_at&&registered_at&&Date.parse(registered_at)<Date.parse(started_at))))fail('submission.consistency')
  return {state:r.state,started_at,registered_at,identifier_state:r.identifier_state,message:text(r.message,'submission.message'),notes:strings(r.notes,'submission.notes')}
}
export function adaptTaskDetail(value: unknown): TaskDetail {
  const r = object(value), row = adaptTaskRow(r), input = object(r.input), output = object(r.output), validation = object(r.validation), links = object(r.links)
  if (!('submitted' in input) || !('resolved' in input) || !('result' in output)) fail('输入输出事实')
  const actions=object(r.actions)
  if(typeof actions.cancel_smoke!=='boolean')fail('actions.cancel_smoke')
  if(typeof actions.cancel_asset!=='boolean')fail('actions.cancel_asset')
  return { ...row, outcome:r.outcome===null?null:adaptPreparationOutcome(r.outcome), submission:adaptSubmission(r.submission), actions:{cancel_smoke:actions.cancel_smoke,cancel_asset:actions.cancel_asset}, purpose: nullable(r.purpose,'purpose'), preparation_status: nullable(r.preparation_status,'preparation_status'),
    request_frozen_at: nullable(r.request_frozen_at,'request_frozen_at'), request_sha256: nullable(r.request_sha256,'request_sha256'),
    input: { state: text(input.state,'input.state'), submitted: input.submitted, resolved: input.resolved, prompt_trace:adaptPromptTrace(input.prompt_trace),field_batch_trace:adaptFieldBatchTrace(input.field_batch_trace),media_trace:adaptMediaTrace(input.media_trace),api_mapping:adaptApiMapping(input.api_mapping),native_input_trace:adaptNativeInputTrace(input.native_input_trace),system_message_trace:adaptSystemMessageTrace(input.system_message_trace),
      parameters: array(input.parameters,'parameters').map(value => { const p=object(value); if(!('value' in p))fail('parameter.value'); return {key:text(p.key,'key'),value:p.value,source:text(p.source,'source')} }),
      prompts: array(input.prompts,'prompts').map(value=>{const p=object(value);return {key:text(p.key,'key'),role:text(p.role,'role'),text:text(p.text,'prompt'),source:text(p.source,'source')}}), notes: strings(input.notes,'input.notes') },
    output: { state:text(output.state,'output.state'), result:output.result, text:text(output.text,'output.text'),media:array(output.media,'media'),notes:strings(output.notes,'output.notes') },
    validation: { status:text(validation.status,'validation.status'),warnings:strings(validation.warnings,'warnings'),error_code:text(validation.error_code,'error_code'),error_message:text(validation.error_message,'error_message') },
    calls:array(r.calls,'calls').map(value=>{const c=object(value);for(const k of ['kind','id','role','status','operation','created_at','error_code'])text(c[k],k);return {...c,preparation_id:nullable(c.preparation_id,'call.preparation_id'),finished_at:nullable(c.finished_at,'call.finished_at')} as TaskCall}),
    call_count:number(r.call_count,'call_count'),calls_next:nullable(r.calls_next,'calls_next'),calls_previous:nullable(r.calls_previous,'calls_previous'),
    output_count:number(r.output_count,'output_count'),output_next:nullable(r.output_next,'output_next'),output_previous:nullable(r.output_previous,'output_previous'),
    deferred_fields:array(r.deferred_fields,'deferred_fields').map(v=>{const f=object(v);return{field:text(f.field,'field'),size_bytes:number(f.size_bytes,'size_bytes'),total_characters:number(f.total_characters,'total_characters'),encoding:text(f.encoding,'encoding'),sha256:text(f.sha256,'deferred.sha256')}}),
    ...(r.content?{content:adaptContent(r.content)} : {}),
    links:{generation_task_id:nullable(links.generation_task_id,'generation_task_id'),asset_job_id:nullable(links.asset_job_id,'asset_job_id')} }
}
export const taskTime = (value: string | null) => value === null ? '尚无记录' : new Date(value).toLocaleString()
export const taskValue = (value: unknown) => value === undefined ? '字段缺失' : value === null ? '空值（null）' : value === '' ? '空文本' : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)
export interface TaskParameterGroup {
  key: string
  values: { value: unknown; sources: string[] }[]
}
function sameRecordedValue(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if (a === null || b === null || typeof a !== 'object' || typeof b !== 'object') return false
  if (Array.isArray(a) || Array.isArray(b)) {
    return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((value, i) => sameRecordedValue(value, b[i]))
  }
  const left = a as Record<string, unknown>, right = b as Record<string, unknown>
  return Object.keys(left).length === Object.keys(right).length && Object.keys(left).every(key =>
    Object.prototype.hasOwnProperty.call(right, key) && sameRecordedValue(left[key], right[key]))
}
/** Read-only grouping of saved facts, not parameter resolution or alias inference. */
export function groupTaskParameters(parameters: TaskDetail['input']['parameters']): TaskParameterGroup[] {
  const groups = new Map<string, TaskParameterGroup>()
  for (const parameter of parameters) {
    // Two nodes of the same class can legitimately have different widget values.
    // Retain their exact graph identities rather than grouping by display name.
    const identity = JSON.stringify([parameter.key, parameter.source.startsWith('graph.') ? parameter.source : null])
    let group = groups.get(identity)
    if (!group) { group = { key: parameter.key, values: [] }; groups.set(identity, group) }
    const same = group.values.find(item => sameRecordedValue(item.value, parameter.value))
    if (same) same.sources.push(parameter.source)
    else group.values.push({ value: parameter.value, sources: [parameter.source] })
  }
  return [...groups.values()]
}
export function taskCenterTarget(input: {category?: TaskCategory; versionId?: string; sourceId?: string; kind?: TaskKind; id?: string; attemptId?: string; projectId?: string; runId?: string; stageId?: string}) {
  return {name:'workflows',query:{...(input.category?{category:input.category}:{}),...(input.versionId?{execution_instance_version_id:input.versionId}:{}),
    ...(input.sourceId?{source_id:input.sourceId}:{}),...(input.kind&&input.id?{kind:input.kind,id:input.id}:{}),
    ...(input.kind==='preparation'&&input.id&&input.attemptId?{attempt_id:input.attemptId}:{}),...(input.projectId?{project_id:input.projectId}:{}),
    ...(input.runId?{production_run_id:input.runId}:{}),...(input.stageId?{stage_run_id:input.stageId}:{})}}
}

export function pageCursor(url: string | null): string | undefined {
  return url ? new URL(url, 'http://local').searchParams.get('cursor') || undefined : undefined
}
export function adaptTaskOptions(value: unknown) {
  const r=object(value), result:Record<string,TaskChoice[]> = {}
  for(const key of ['categories','statuses','operations','projects','execution_versions']) result[key]=array(r[key],key).map(v=>{const p=object(v);return{value:text(p.value,'value'),label:text(p.label,'label')}})
  const pagination:Record<string,{count:number;next:string|null;previous:string|null}> = {}
  for(const [key,value] of Object.entries(object(r.pagination))){const p=object(value);pagination[key]={count:number(p.count,'count'),next:nullable(p.next,'next'),previous:nullable(p.previous,'previous')}}
  return {categories:result.categories!,statuses:result.statuses!,operations:result.operations!,projects:result.projects!,execution_versions:result.execution_versions!,pagination}
}
