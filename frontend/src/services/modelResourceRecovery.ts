import { ref } from 'vue'
import { platformApi } from './platformApi'
import { apiErrorSummary } from './apiErrorPresentation'
import type { ModelArtifact, ModelDeployment, ModelProfileVersion, ProviderProfile } from './platformTypes'
export type ModelResourceKind = 'model' | 'provider' | 'deployment' | 'artifact'
export type ModelResourceRow = ModelArtifact | ModelDeployment | ModelProfileVersion | ProviderProfile
export interface ModelWriteIntent { resource: ModelResourceKind; id: string; key: string; sourceId: string; profileId: string }
export function modelResourceMatches(row: ModelResourceRow | undefined, intent: ModelWriteIntent) {
  if(!row?.id || intent.id && row.id!==intent.id || intent.sourceId && row.id===intent.sourceId) return false
  if(intent.resource==='model') return 'profileKey' in row && row.profileKey===intent.key
  if(intent.resource==='artifact') return 'logicalName' in row && row.profileVersion===intent.profileId && row.logicalName===intent.key
  return (intent.resource==='provider'?'driverConfig' in row:'runtimeConfig' in row) && 'key' in row && row.key===intent.key
}
export function modelResourceLabel(row: ModelResourceRow) {
  return 'profileKey' in row?`${row.profileKey} · v${row.version} · ${row.status}`:'logicalName' in row?row.logicalName:row.name || row.key
}
export function modelResourceEditable(row: ModelResourceRow) {
  return 'profileKey' in row?row.status==='draft':'runtimeConfig' in row?row.status!=='retired':true
}
export function useModelResourceRecovery(isCurrentScope:()=>string) {
  const intent=ref<ModelWriteIntent|null>(null), busy=ref(false), error=ref(''), checked=ref(false), records=ref<ModelResourceRow[]>([])
  let serial=0
  const invalidate=()=>{serial++;busy.value=false;error.value='';checked.value=false;records.value=[]}
  const reset=()=>{serial++;intent.value=null;busy.value=false;error.value='';checked.value=false;records.value=[]}
  const mark=(value:ModelWriteIntent)=>{reset();intent.value={...value}}
  async function read(candidateId?:string):Promise<ModelResourceRow|null> {
    if(!intent.value || busy.value) return null
    const target=intent.value,scope=isCurrentScope(),generation=++serial
    const current=()=>target===intent.value&&generation===serial&&scope===isCurrentScope()
    busy.value=true;error.value=''
    try {
      const rows=target.resource==='model'?await platformApi.listModelProfiles():target.resource==='deployment'?await platformApi.listDeployments():target.resource==='provider'?await platformApi.listProviders():await platformApi.listModelArtifacts()
      if(!current()) return null
      records.value=rows.filter(row=>modelResourceMatches(row,target));checked.value=true
      if(!candidateId) return null
      const row=records.value.find(row=>row.id===candidateId)
      if(!row || !modelResourceEditable(row)) throw Error('该精确记录已缺失、身份变化或不可编辑，未载入；请重新核对。')
      return row
    } catch(cause) {if(current())error.value=apiErrorSummary(cause,'只读核对失败');return null}
    finally {if(current())busy.value=false}
  }
  return {intent,busy,error,checked,records,mark,reset,read,invalidate}
}
