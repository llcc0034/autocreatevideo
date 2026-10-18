import { copy, preview, previewReceipt, receipt, scan, source } from './workflow-import-data.ts'

// Two authorities deliberately expose the same path and bytes. Neither path,
// name nor hash alone may route a write or settle another source's request.
export const batchRows = () => ['a1', 'b1', 'a2'].map((key) => {
  const authority = key[0]
  const deployment = `batch-${authority}`
  return source({
    sourceKey: key, sourceDeploymentId: deployment,
    sourceScopeSha256: authority === 'a' ? 'a'.repeat(64) : 'b'.repeat(64),
    sourcePath: key === 'a2' ? 'workflows/second.json' : 'workflows/shared.json',
    sourceSha256: key === 'a2' ? '7'.repeat(64) : '1'.repeat(64),
    candidateSourceId: `candidate-${key}`,
    matchedTargetDeploymentId: deployment,
    modelProfileCandidates: source().modelProfileCandidates.map(row => ({ ...row, targetDeploymentId: deployment })),
  })
})
export function batchScan() {
  const items = batchRows()
  return { ...scan(items), status: 'partial', sourceCount: 3, succeededSourceCount: 2, failedSourceCount: 1,
    sources: ['a', 'b', 'c'].map((key) => ({ ...scan([]).sources[0], sourceDeploymentId: `batch-${key}`,
      sourceScopeSha256: key.repeat(64), sourceLabel: `来源 ${key.toUpperCase()}`,
      status: key === 'c' ? 'failed' : 'succeeded', itemCount: key === 'a' ? 2 : key === 'b' ? 1 : 0,
      errorCode: key === 'c' ? 'source_unavailable' : '', errorMessage: key === 'c' ? '隔离来源不可读取；不代表其他来源失败' : '',
    })),
  }
}
export function batchPreview(deploymentId: string, input: any) {
  const row = batchRows().find(row => row.sourceDeploymentId === deploymentId && row.sourcePath === input.sourcePath)
  if (!row) throw Error('Unknown fixture source')
  return previewReceipt(deploymentId, input, row.candidateSourceId, { ...copy(preview), contractDraftId: `contract-${row.sourceKey}` })
}
export function batchReceipt(deploymentId: string, inputs: any[], mode = 'success') {
  const result = receipt(deploymentId, inputs)
  for (const item of result.items) {
    const row = batchRows().find(row => row.sourceDeploymentId === deploymentId && row.sourcePath === item.sourcePath)!
    item.sourceId = `saved-${row.sourceKey}`
    item.workflowVersionId = `version-${row.sourceKey}`
    if (mode === 'item-failure' && row.sourceKey === 'a2') {
      item.result = 'failed'; item.code = 'contract_rejected'; item.detail = '隔离合同已明确拒绝；未保存'; item.workflowVersionId = ''
    }
  }
  result.createdCount = result.items.filter(row => row.result === 'created').length
  result.failedCount = result.items.length - result.createdCount
  result.status = result.failedCount ? 'failed' : 'complete'
  return result
}
