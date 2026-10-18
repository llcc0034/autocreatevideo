import { api } from './api'
import { isSavedShotId } from './shotSourceFields'
import { readShotFieldRevision, type ShotFieldRevisionWrite } from './shotFieldRevisions'

const path = (shotId:string) => {
  if(!isSavedShotId(shotId)) throw Error('请先保存镜头。')
  return `/shots/${shotId}/field-revisions/`
}
export const shotFieldRevisionsApi = {
  async read(shotId:string, revisionId?:string) {
    const detail=readShotFieldRevision((await api.get(path(shotId),{params:revisionId?{revision_id:revisionId}:undefined})).data,shotId)
    if(revisionId&&detail.revision?.id!==revisionId)throw Error('返回的历史修订与指定版本不一致。')
    return detail
  },
  async write(shotId:string, payload:ShotFieldRevisionWrite) {
    const result = (await api.post(path(shotId),payload)).data
    if(typeof result?.replayed!=='boolean')throw Error('保存回执不完整，请用原请求核对结果。')
    const detail=readShotFieldRevision(result.detail,shotId)
    if(!detail.revision||detail.source_sha256!==payload.source_sha256)throw Error('保存回执缺少确切修订依据，请用原请求核对结果。')
    return {detail,replayed:result.replayed}
  },
}
