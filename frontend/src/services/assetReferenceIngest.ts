import { reactive } from 'vue'
import type { Asset } from '@/types'
import type { AssetReferenceSeries, AssetReferenceVersion, ReferenceSeriesCreate, ReferenceVersionCreate } from './assetReferenceApi'
import { apiErrorSummary } from './apiErrorPresentation'

export function createReferenceIngest(api: {
  createSeries(input: ReferenceSeriesCreate): Promise<AssetReferenceSeries>
  attach(input: ReferenceVersionCreate): Promise<AssetReferenceVersion>
}) {
  const state = reactive({
    target: null as AssetReferenceSeries | null, asset: null as Asset | null,
    source: 'existing' as 'upload' | 'existing', busy: false, error: '',
    result: null as AssetReferenceVersion | null, creationUnknown: false,
  })
  let generation = 0
  function selectAsset(asset: Asset, source: 'asset_library' | 'asset_upload') {
    if (state.busy) return
    state.asset = asset; state.source = source === 'asset_upload' ? 'upload' : 'existing'
    state.result = null; state.error = ''
  }
  function selectSeries(series: AssetReferenceSeries | null) {
    if (state.busy) return
    state.target = series; state.result = null; state.error = ''; state.creationUnknown = false
  }
  async function create(input: ReferenceSeriesCreate) {
    if (state.busy || state.creationUnknown) return
    const token = generation; state.busy = true; state.error = ''
    try {
      const series = await api.createSeries(input)
      if (token === generation) { state.target = series; state.result = null }
    } catch (cause) {
      if (token === generation) {
        const status = (cause as { response?: { status?: number } })?.response?.status
        state.creationUnknown = !status || status >= 500
        state.error = `${apiErrorSummary(cause)}${state.creationUnknown ? '。创建结果不确定，请先查询已有系列核对，不要重复创建。' : ''}`
      }
    } finally { if (token === generation) state.busy = false }
  }
  async function attach() {
    if (state.busy || !state.target || !state.asset) return
    const token = generation; state.busy = true; state.error = ''; state.result = null
    const input = {series_id: state.target.id, asset_id: state.asset.id, source: state.source}
    try {
      const result = await api.attach(input)
      if (token === generation) state.result = result
    } catch (cause) {
      if (token === generation) state.error = `素材已在库中，系列关联未确认：${apiErrorSummary(cause)}。保留本次素材，可重试关联，无需重新上传。`
    } finally { if (token === generation) state.busy = false }
  }
  function dispose() { generation++ }
  return { state, selectAsset, selectSeries, create, attach, dispose }
}
