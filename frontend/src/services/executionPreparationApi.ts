import { api, unwrap } from './api'
import { replacementOptions, replacementPreview, replacementResult } from './configurationReplacement'
import { adaptBulkFieldPreview, adaptBulkFieldResult, type BulkFieldReceipt } from './executionFieldBulkReview'
import {
  adaptExecutionField, adaptExecutionFieldActionResult, adaptExecutionFieldDetail, adaptExecutionPreparationDetail, adaptStagePreparationPage,
  executionFieldConfirmPayload, executionFieldCreatePayload, executionFieldReviewPayload,
  executionPreparationContinuePayload, adaptExecutionSmokeReuseFixture,
  adaptPreparationGroupPage, preparationUnitKey,
  type ExecutionFieldConfirm, type ExecutionFieldCreate, type ExecutionFieldReview,
} from './executionPreparation'

export const executionPreparationApi = {
  async retryUncertainMedia(id: string, revision: number, recovery: {taskId:string;attemptId:string;taskUpdatedAt:string;requestSha256:string}) {
    return adaptExecutionPreparationDetail(unwrap((await api.post(`/execution-preparations/${encodeURIComponent(id)}/retry-uncertain-media/`, {
      expected_preparation_revision: revision, task_id: recovery.taskId, attempt_id: recovery.attemptId,
      task_updated_at: recovery.taskUpdatedAt, request_sha256: recovery.requestSha256, acknowledged: true,
    })).data))
  },
  async recoverMediaArchive(id: string, revision: number, recovery: {taskId:string;attemptId:string;taskUpdatedAt:string;receiptSha256:string}) {
    return adaptExecutionPreparationDetail(unwrap((await api.post(`/execution-preparations/${encodeURIComponent(id)}/recover-media-archive/`, {
      expected_preparation_revision: revision, task_id: recovery.taskId, attempt_id: recovery.attemptId,
      task_updated_at: recovery.taskUpdatedAt, receipt_sha256: recovery.receiptSha256,
    })).data))
  },
  async configurationSuccessors(id: string) {
    return replacementOptions(unwrap((await api.get(`/execution-preparations/${encodeURIComponent(id)}/configuration-successors/`)).data), id)
  },
  async previewConfigurationReplacement(id: string, versionId: string, revision: number) {
    return replacementPreview(unwrap((await api.post(`/execution-preparations/${encodeURIComponent(id)}/configuration-replacement-preview/`,
      { version_id: versionId, expected_preparation_revision: revision })).data), id, versionId, revision)
  },
  async replaceConfiguration(id: string, versionId: string, revision: number, previewSha: string) {
    return replacementResult(unwrap((await api.post(`/execution-preparations/${encodeURIComponent(id)}/configuration-replacement/`,
      { version_id: versionId, expected_preparation_revision: revision, preview_sha256: previewSha })).data), id, versionId)
  },
  async bulkFieldPreview(ids: string[]) {
    return adaptBulkFieldPreview(unwrap((await api.post('/execution-preparations/bulk-field-preview/', { preparation_ids: ids })).data), ids)
  },
  async bulkFieldConfirm(items: BulkFieldReceipt[]) {
    return adaptBulkFieldResult(unwrap((await api.post('/execution-preparations/bulk-field-confirm/', { items })).data), items)
  },
  async completeTextResponse(id: string, expectedRevision: number, reviewId: string, originalSha256: string, text: string) {
    return adaptExecutionPreparationDetail(unwrap((await api.post(`/execution-preparations/${encodeURIComponent(id)}/complete-text-response/`,
      { expected_preparation_revision: expectedRevision, review_id: reviewId, original_sha256: originalSha256, text })).data))
  },
  async confirmResources(id: string, expectedRevision: number, fingerprint: string) {
    return adaptExecutionPreparationDetail(unwrap((await api.post(`/execution-preparations/${encodeURIComponent(id)}/confirm-resources/`,
      { expected_preparation_revision: expectedRevision, fingerprint })).data))
  },
  async groups(stageId: string, page: number, kind: 'primary' | 'auxiliary') {
    const raw = (await api.get('/execution-preparations/groups/', { params: { stage_run_id: stageId, page, kind } })).data
    return adaptPreparationGroupPage(raw, stageId, kind)
  },
  async forStage(stageId: string, page: number, kind?: 'primary' | 'auxiliary', unitKey?: string) {
    const raw = (await api.get('/execution-preparations/', { params: { stage_run_id: stageId, page, ...(kind ? { kind } : {}), ...(unitKey ? { unit_key: unitKey } : {}) } })).data
    const result = adaptStagePreparationPage(raw, stageId)
    if (kind && result.results.some(row => row.presentation.kind !== kind)) throw new Error('执行准备分类与请求不一致')
    if (unitKey && result.results.some(row => preparationUnitKey(row) !== unitKey)) throw new Error('历史记录不属于当前制作对象')
    return result
  },
  async retryFieldBatch(id: string, expectedRevision: number, fieldBatchKey: string, attemptNumber: number, acknowledgedUncertainTaskId?: string) {
    return adaptExecutionPreparationDetail(unwrap((await api.post(`/execution-preparations/${encodeURIComponent(id)}/retry-field-batch/`,
      { expected_preparation_revision: expectedRevision, field_batch_key: fieldBatchKey, attempt_number: attemptNumber,
        ...(acknowledgedUncertainTaskId ? { acknowledged_uncertain_task_id: acknowledgedUncertainTaskId } : {}) })).data))
  },
  async smokeFixture(id: string, expectedRevision: number) {
    return adaptExecutionSmokeReuseFixture(unwrap((await api.get(`/execution-preparations/${encodeURIComponent(id)}/smoke-fixture/`,
      { params: executionPreparationContinuePayload(expectedRevision) })).data))
  },
  async get(id: string) {
    return adaptExecutionPreparationDetail(unwrap((await api.get(`/execution-preparations/${encodeURIComponent(id)}/`)).data))
  },
  async continue(id: string, expectedRevision: number) {
    return adaptExecutionPreparationDetail(unwrap((await api.post(`/execution-preparations/${encodeURIComponent(id)}/continue/`,
      executionPreparationContinuePayload(expectedRevision))).data))
  },
  async cancel(id: string, expectedRevision: number) {
    return adaptExecutionPreparationDetail(unwrap((await api.post(`/execution-preparations/${encodeURIComponent(id)}/cancel/`,
      executionPreparationContinuePayload(expectedRevision))).data))
  },
  async fields(preparationId: string, page: number) {
    // unwrap intentionally extracts results for item lists; keep the whole
    // DRF page here so count and page links remain authoritative.
    const raw = (await api.get('/execution-field-values/', { params: { preparation_id: preparationId, page } })).data
    if (!raw || !Number.isSafeInteger(raw.count) || raw.count < 0 || !Array.isArray(raw.results)
      || !(raw.next === null || typeof raw.next === 'string') || !(raw.previous === null || typeof raw.previous === 'string')) {
      throw new Error('字段修订分页响应不完整')
    }
    return { count: raw.count, next: raw.next, previous: raw.previous, results: raw.results.map(adaptExecutionField) }
  },
  async field(id: string) {
    return adaptExecutionFieldDetail(unwrap((await api.get(`/execution-field-values/${encodeURIComponent(id)}/`)).data))
  },
  async createField(input: ExecutionFieldCreate) {
    return adaptExecutionFieldActionResult(unwrap((await api.post('/execution-field-values/', executionFieldCreatePayload(input))).data))
  },
  async confirmField(id: string, input: ExecutionFieldConfirm) {
    return adaptExecutionFieldActionResult(unwrap((await api.post(`/execution-field-values/${encodeURIComponent(id)}/confirm/`, executionFieldConfirmPayload(input))).data))
  },
  async rejectField(id: string, input: ExecutionFieldReview) {
    return adaptExecutionFieldActionResult(unwrap((await api.post(`/execution-field-values/${encodeURIComponent(id)}/reject/`, executionFieldReviewPayload(input))).data))
  },
}
