import { api } from './api'

export interface AssetReferenceSeries {
  id: string; name: string; role: 'primary' | 'variant'; owner_id: number
  primary_series_id: string | null; bible_variant_id: string | null
  version_count: number; variant_count: number
}
export interface AssetReferenceVersion {
  id: string; series_id: string; version: number; source: 'upload' | 'existing' | 'generated'
  asset: { id: string; name: string; kind: string; mime_type: string; is_active: boolean }
  generated_from_id: string | null; setting_revision_id: string | null
  generated_artifact?: { id: string; status: string; production_run_id: string | null; generation_attempt_id: string | null } | null
  generation_job?: { id: string; status: string } | null
  created_at?: string
  origin_history_cleared?: boolean
}
export interface ReferenceSeriesPage { count: number; next: string | null; previous: string | null; results: AssetReferenceSeries[] }
export interface ReferenceVersionPage { count: number; next: string | null; previous: string | null; results: AssetReferenceVersion[] }
export interface ReferenceSeriesQuery {
  page: number; search: string; role?: 'primary' | 'variant'; primary_series_id?: string
  media_type?: 'image' | 'video' | 'audio'; bible_linked?: boolean; bible_variant_id?: string; asset_id?: string
}
export interface ReferenceSeriesCreate { name: string; role: 'primary' | 'variant'; primary_series_id: string | null }
export interface ReferenceVersionCreate { series_id: string; asset_id: string; source: 'upload' | 'existing' }
export interface ReferenceSeriesPatch { name?: string; bible_variant_id?: string | null }
export interface ReferenceBibleOption {
  id: string; name: string; reference_name: string; project_id: string; project_title: string
  character_name: string | null; scene_name: string | null; prop_name: string | null
}
export interface ReferenceBiblePage { count: number; next: string | null; previous: string | null; results: ReferenceBibleOption[] }
export interface ReferenceRemovalImpact {
  target_type: 'series' | 'version'; id: string; name: string; version_count: number; asset_count: number
  variant_count: number; dependent_version_count: number; production_use_count: number; can_remove: boolean
}

export const assetReferenceApi = {
  async roles(): Promise<Array<{ value: 'primary' | 'variant'; display_name: string }>> {
    const response = await api.options('/asset-reference-series/')
    const choices = response.data?.actions?.POST?.role?.choices
    if (!Array.isArray(choices) || !choices.length) throw new Error('系列角色选项未返回，请刷新后重试')
    return choices
  },
  async listSeries(params: ReferenceSeriesQuery): Promise<ReferenceSeriesPage> {
    return (await api.get('/asset-reference-series/', { params })).data
  },
  async getSeries(id: string): Promise<AssetReferenceSeries> {
    return (await api.get(`/asset-reference-series/${id}/`)).data
  },
  async listVersions(params: { series_id: string; page: number }): Promise<ReferenceVersionPage> {
    return (await api.get('/asset-reference-versions/', { params })).data
  },
  async getVersion(id: string): Promise<AssetReferenceVersion> {
    return (await api.get(`/asset-reference-versions/${id}/`)).data
  },
  async createSeries(input: ReferenceSeriesCreate): Promise<AssetReferenceSeries> {
    return (await api.post('/asset-reference-series/', input)).data
  },
  async attach(input: ReferenceVersionCreate): Promise<AssetReferenceVersion> {
    return (await api.post('/asset-reference-versions/', input)).data
  },
  async updateSeries(id: string, input: ReferenceSeriesPatch): Promise<AssetReferenceSeries> {
    return (await api.patch(`/asset-reference-series/${id}/`, input)).data
  },
  async bibleOptions(id: string, params: {page: number; search?: string; id?: string}): Promise<ReferenceBiblePage> {
    return (await api.get(`/asset-reference-series/${id}/bible-options/`, {params})).data
  },
  async removalImpact(kind: 'series' | 'version', id: string): Promise<ReferenceRemovalImpact> {
    return (await api.get(`/asset-reference-${kind === 'series' ? 'series' : 'versions'}/${id}/removal-impact/`)).data
  },
  async remove(kind: 'series' | 'version', id: string): Promise<void> {
    await api.delete(`/asset-reference-${kind === 'series' ? 'series' : 'versions'}/${id}/`)
  },
}
