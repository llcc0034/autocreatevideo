import { api, unwrap } from './api'
import { adaptTaskPage, adaptTaskDetail, adaptTaskOptions, type TaskFilters, type TaskKind } from './taskCenter'
export const taskCenterApi = {
  async list(filters: TaskFilters) { return adaptTaskPage((await api.get('/task-center/', {params: filters})).data) },
  async detail(kind: TaskKind, id: string, query:Record<string,string|number> = {}) {
    return adaptTaskDetail(unwrap((await api.get('/task-center/' + kind + '/' + encodeURIComponent(id) + '/', {params:query})).data))
  },
  async options(params: Record<string, string | number> = {}) { return adaptTaskOptions(unwrap((await api.get('/task-center/options/',{params})).data)) },
}
