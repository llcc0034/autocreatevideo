import { reactive } from 'vue'
import type { Asset } from '@/types'
import type { AssetReferenceSeries, AssetReferenceVersion, ReferenceSeriesPage, ReferenceVersionPage, ReferenceSeriesQuery } from './assetReferenceApi'
import { apiErrorSummary } from './apiErrorPresentation'

type BrowserApi = {
  listSeries(query: ReferenceSeriesQuery): Promise<ReferenceSeriesPage>
  getSeries(id: string): Promise<AssetReferenceSeries>
  listVersions(query: { series_id: string; page: number }): Promise<ReferenceVersionPage>
  getVersion(id: string): Promise<AssetReferenceVersion>
  getAsset(id: string): Promise<Asset>
}
const emptyPage = <T>() => ({count: 0, next: null as string | null, previous: null as string | null, results: [] as T[]})

// Reads only. Each independent request has an epoch so switching cannot show another series' media.
export function createReferenceBrowser(api: BrowserApi) {
  const state = reactive({
    query: {page: 1, search: ''} as ReferenceSeriesQuery,
    catalog: emptyPage<AssetReferenceSeries>(), catalogLoading: false, catalogError: '',
    selected: null as AssetReferenceSeries | null, primary: null as AssetReferenceSeries | null,
    variants: emptyPage<AssetReferenceSeries>(), variantPage: 1, relationsLoading: false, relationsError: '',
    versions: emptyPage<AssetReferenceVersion>(), versionPage: 1, versionsLoading: false, versionsError: '',
    version: null as AssetReferenceVersion | null, asset: null as Asset | null, previewLoading: false, previewError: '',
    detailLoading: false, detailError: '',
  })
  let listEpoch = 0, relationEpoch = 0, versionsEpoch = 0, previewEpoch = 0, detailEpoch = 0
  function clearDetail() {
    relationEpoch++; versionsEpoch++; previewEpoch++; detailEpoch++
    state.selected = null; state.primary = null; state.version = null; state.asset = null
    state.variants = emptyPage(); state.versions = emptyPage(); state.variantPage = 1; state.versionPage = 1
    state.relationsError = ''; state.versionsError = ''; state.previewError = ''; state.detailError = ''
    state.relationsLoading = false; state.versionsLoading = false; state.previewLoading = false; state.detailLoading = false
  }
  async function search(query = state.query) {
    const epoch = ++listEpoch; state.query = {...query}; state.catalogLoading = true; state.catalogError = ''
    state.catalog = emptyPage(); clearDetail()
    try { const rows = await api.listSeries({...query}); if(epoch === listEpoch) state.catalog = rows }
    catch(error) { if(epoch === listEpoch) state.catalogError = apiErrorSummary(error) }
    finally { if(epoch === listEpoch) state.catalogLoading = false }
  }
  async function relations(page = 1) {
    const selected = state.selected; if(!selected) return
    const epoch = ++relationEpoch; state.relationsLoading = true; state.relationsError = ''; state.variantPage = page
    state.primary = null; state.variants = emptyPage()
    try {
      const primary = selected.primary_series_id ? await api.getSeries(selected.primary_series_id) : selected
      if(epoch !== relationEpoch) return
      state.primary = primary
      const variants = await api.listSeries({page, search: '', primary_series_id: primary.id})
      if(epoch === relationEpoch) state.variants = variants
    } catch(error) { if(epoch === relationEpoch) state.relationsError = apiErrorSummary(error) }
    finally { if(epoch === relationEpoch) state.relationsLoading = false }
  }
  async function preview(version: AssetReferenceVersion) {
    if(version.series_id !== state.selected?.id) return
    const epoch = ++previewEpoch; state.version = version; state.asset = null; state.previewLoading = true; state.previewError = ''
    try {
      const asset = await api.getAsset(version.asset.id)
      if(epoch !== previewEpoch) return
      if(asset.id !== version.asset.id) throw Error('服务端返回的素材与所选版本不一致')
      state.asset = asset
    } catch(error) { if(epoch === previewEpoch) state.previewError = apiErrorSummary(error) }
    finally { if(epoch === previewEpoch) state.previewLoading = false }
  }
  async function versions(page = 1, exact?: AssetReferenceVersion) {
    const selected = state.selected; if(!selected) return
    const epoch = ++versionsEpoch; previewEpoch++; state.asset = null; state.version = null; state.previewLoading = false; state.previewError = ''
    state.versionPage = page; state.versions = emptyPage(); state.versionsLoading = true; state.versionsError = ''
    try {
      const rows = await api.listVersions({series_id: selected.id, page})
      if(epoch !== versionsEpoch) return
      state.versions = rows
      const chosen = exact || rows.results[0]
      if(chosen) await preview(chosen)
    } catch(error) { if(epoch === versionsEpoch) state.versionsError = apiErrorSummary(error) }
    finally { if(epoch === versionsEpoch) state.versionsLoading = false }
  }
  async function select(series: AssetReferenceSeries, exact?: AssetReferenceVersion) {
    clearDetail(); state.selected = series
    await Promise.all([relations(), versions(1, exact)])
  }
  async function openVersion(id: string) {
    clearDetail(); const epoch = detailEpoch; state.detailLoading = true
    try {
      const version = await api.getVersion(id)
      if(epoch !== detailEpoch) return
      const series = await api.getSeries(version.series_id)
      if(epoch !== detailEpoch) return
      await select(series, version)
    } catch(error) { if(epoch === detailEpoch) state.detailError = apiErrorSummary(error) }
    finally { if(epoch === detailEpoch) state.detailLoading = false }
  }
  function mediaFailed(assetId: string) {
    if(state.asset?.id === assetId) state.previewError = '此版本的媒体暂时无法预览，请重新读取；不会换成其他版本。'
  }
  function dispose() { listEpoch++; clearDetail() }
  return {state, search, select, relations, versions, preview, openVersion, mediaFailed, dispose}
}
