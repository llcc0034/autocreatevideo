import { api } from './api'
import type { BibleVariantDetail, BibleVariantScope, BibleVariantSettingInput } from './bibleVariants'
import { readSettingSource, readVariantDetail, readVariantPage, readVariantRevision, readVariantSummary, requireVariantScope, variantCreatePayload, variantReviewPayload, variantRevisePayload } from './bibleVariants'

function detailFor(value: unknown, id: string) {
  const result = readVariantDetail(value)
  if (result.id !== id) throw new Error('返回的变体不属于所选记录，请重新读取。')
  return result
}

export const bibleVariantApi = {
  async list(scope: BibleVariantScope, page: number) {
    return readVariantPage((await api.get('/bible-variants/', { params: { ...scope, page } })).data, row => requireVariantScope(readVariantSummary(row), scope))
  },
  async detail(id: string, revision?: number) {
    const result = detailFor((await api.get(`/bible-variants/${id}/`, { params: revision === undefined ? {} : { revision } })).data, id)
    if (revision !== undefined && result.setting.revision !== revision) throw new Error('返回的设定不是所选历史修订。')
    return result
  },
  async history(id: string, page: number) {
    return readVariantPage((await api.get(`/bible-variants/${id}/revisions/`, { params: { page } })).data, readVariantRevision)
  },
  async source(detail: BibleVariantDetail) {
    return readSettingSource((await api.get(`/bible-variants/${detail.id}/source/`,
      { params: { revision: detail.setting.revision } })).data, detail)
  },
  async create(scope: BibleVariantScope, value: BibleVariantSettingInput, primary: BibleVariantDetail | null) {
    if (primary) requireVariantScope(primary, scope)
    const input = variantCreatePayload(value, primary)
    const result = readVariantDetail((await api.post('/bible-variants/', { ...scope, ...input })).data)
    requireVariantScope(result, scope)
    if (result.appearance_role !== input.appearance_role || result.setting.primary_setting_revision_id !== input.primary_setting_revision_id
      || (primary && result.primary_setting?.content_sha256 !== input.primary_setting_sha256)) throw new Error('返回的主图依赖与本次选择不符，请重新读取。')
    return result
  },
  async revise(detail: BibleVariantDetail, value: BibleVariantSettingInput) {
    return detailFor((await api.post(`/bible-variants/${detail.id}/revise/`, variantRevisePayload(detail, value))).data, detail.id)
  },
  async review(detail: BibleVariantDetail, decision: 'approve' | 'reject', comment: string) {
    return detailFor((await api.post(`/bible-variants/${detail.id}/review/`, variantReviewPayload(detail, decision, comment))).data, detail.id)
  },
}
