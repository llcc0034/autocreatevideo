import type {
  ComfySavedWorkflowBatchItemInput,
  ComfySavedWorkflowBatchResult,
} from '@/services/platformTypes'

export const chunkSavedWorkflowBatch = (
  items: ComfySavedWorkflowBatchItemInput[],
  effectiveMaximumItems: number | null,
): ComfySavedWorkflowBatchItemInput[][] => {
  if (!items.length) return []
  if (!Number.isSafeInteger(effectiveMaximumItems) || Number(effectiveMaximumItems) < 1) {
    throw new Error('服务端未返回有效的工作流批量上限，导入已失败关闭')
  }
  const batchSize = Number(effectiveMaximumItems)
  const chunks: ComfySavedWorkflowBatchItemInput[][] = []
  for (let offset = 0; offset < items.length; offset += batchSize) {
    chunks.push(items.slice(offset, offset + batchSize))
  }
  return chunks
}

export interface SavedWorkflowDeploymentInput {
  sourceDeploymentId: string
  item: ComfySavedWorkflowBatchItemInput
}

export interface SavedWorkflowBatchAggregate {
  schemaVersion: 1
  deploymentIds: string[]
  status: 'complete' | 'partial' | 'failed'
  createdCount: number
  reusedCount: number
  failedCount: number
  items: ComfySavedWorkflowBatchResult['items']
}

export const groupSavedWorkflowBatches = (
  rows: SavedWorkflowDeploymentInput[],
  effectiveMaximumItems: number | null,
): Array<{ deploymentId: string; items: ComfySavedWorkflowBatchItemInput[] }> => {
  const grouped = new Map<string, ComfySavedWorkflowBatchItemInput[]>()
  for (const row of rows) {
    if (!row.sourceDeploymentId) throw new Error('工作流缺少权威来源 deployment')
    const items = grouped.get(row.sourceDeploymentId) || []
    items.push(row.item)
    grouped.set(row.sourceDeploymentId, items)
  }
  return [...grouped.entries()].flatMap(([deploymentId, items]) => (
    chunkSavedWorkflowBatch(items, effectiveMaximumItems).map((batch) => ({
      deploymentId,
      items: batch,
    }))
  ))
}

const savedWorkflowBatchIdentity = (sourcePath: string, sourceSha256: string, importKind: string) => (
  `${sourcePath}\u0000${sourceSha256}\u0000${importKind}`
)

export const assertSavedWorkflowBatchResultMatchesRequest = (
  deploymentId: string,
  requestItems: ComfySavedWorkflowBatchItemInput[],
  result: ComfySavedWorkflowBatchResult,
): void => {
  if (result.deploymentId !== deploymentId) {
    throw new Error('工作流批量结果与请求来源 deployment 不一致')
  }
  const requested = requestItems.map((item) => (
    savedWorkflowBatchIdentity(item.sourcePath, item.expectedSha256, item.importKind)
  ))
  const returned = result.items.map((item) => (
    savedWorkflowBatchIdentity(item.sourcePath, item.sourceSha256, item.importKind)
  ))
  const requestedSet = new Set(requested)
  const returnedSet = new Set(returned)
  if (
    requestedSet.size !== requested.length
    || returnedSet.size !== returned.length
    || requested.length !== returned.length
    || returned.some((identity) => !requestedSet.has(identity))
  ) {
    throw new Error('工作流批量结果与请求项目集合不一致')
  }
}

export const mergeSavedWorkflowBatchResults = (
  previous: SavedWorkflowBatchAggregate | null,
  next: ComfySavedWorkflowBatchResult,
): SavedWorkflowBatchAggregate => {
  if (!next.deploymentId || next.items.some((item) => item.sourceDeploymentId !== next.deploymentId)) {
    throw new Error('工作流批量结果与权威来源 deployment 不一致')
  }
  const items = [...(previous?.items || []), ...next.items]
  const createdCount = items.filter((item) => item.result === 'created').length
  const reusedCount = items.filter((item) => item.result === 'reused').length
  const failedCount = items.filter((item) => item.result === 'failed').length
  const succeededCount = createdCount + reusedCount
  return {
    schemaVersion: 1,
    deploymentIds: [...new Set([...(previous?.deploymentIds || []), next.deploymentId])],
    status: failedCount === 0 ? 'complete' : succeededCount === 0 ? 'failed' : 'partial',
    createdCount,
    reusedCount,
    failedCount,
    items,
  }
}
