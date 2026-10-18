/** Read the server's frozen correspondence, never name or select materials. */
export interface FrozenReferenceBinding {
  position: number
  original_name: string
  reference_name: string | null
  name_disambiguated: boolean | null
  input_slot: number | null
  shot_reference: null | {
    use_id: string; use_key: string; position: number; purpose: string; source_mode: string
    source: {kind:'asset';asset_id:string} | {kind:'reference_version';reference_version_id:string;series_id:string;version:number;acquisition_source:string}
    setting: null | {appearance_id:string;setting_revision_id:string;revision:number;content_sha256:string;entity_kind:string;entity_id:string;appearance_role:string;reference_name:string}
  }
}
export function adaptFrozenReferenceBinding(value: unknown): FrozenReferenceBinding {
  const fail = (): never => { throw Error('原请求名称与版本对应记录缺失或无效') }
  const object = (v:unknown):Record<string,any> => !v || typeof v!=='object' || Array.isArray(v) ? fail() : v
  const integer = (v:unknown,min=0) => { if(typeof v!=='number'||!Number.isInteger(v)||v<min)fail() }
  const strings = (row:Record<string,any>,keys:string[]) => {for(const key of keys)if(typeof row[key]!=='string')fail()}
  const r=object(value)
  integer(r.position);strings(r,['original_name'])
  if(r.reference_name===null){if(r.name_disambiguated!==null||r.input_slot!==null)fail()}
  else {strings(r,['reference_name']);if(typeof r.name_disambiguated!=='boolean')fail();integer(r.input_slot,1)}
  if(r.shot_reference!==null){
    const s=object(r.shot_reference),source=object(s.source)
    strings(s,['use_id','use_key','purpose','source_mode']);integer(s.position)
    if(!['existing','upload','generated'].includes(s.source_mode))fail()
    if(source.kind==='asset')strings(source,['asset_id'])
    else if(source.kind==='reference_version'){strings(source,['reference_version_id','series_id','acquisition_source']);integer(source.version,1)}
    else fail()
    if(s.setting!==null){const setting=object(s.setting);integer(setting.revision,1)
      strings(setting,['appearance_id','setting_revision_id','content_sha256','entity_kind','entity_id','appearance_role','reference_name'])}
  }
  return r as FrozenReferenceBinding
}
