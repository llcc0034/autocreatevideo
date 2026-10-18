import { api } from './api'
import { isSavedShotId, readShotSourceFields } from './shotSourceFields'

export const shotSourceFieldsApi = {
  async read(shotId: string) {
    if (!isSavedShotId(shotId)) throw Error('请先保存镜头，再读取原结构化来源。')
    return readShotSourceFields((await api.get(`/shots/${shotId}/source-fields/`)).data, shotId)
  },
}
