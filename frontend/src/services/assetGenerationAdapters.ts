import type { Asset, AssetGenerationJob, AssetGenerationOption } from '@/types'
import { adaptExecutionPreparation } from './executionPreparation'

const fail = (key: string): never => { throw new Error(`素材生成响应不完整或不一致：${key}`) }
const record = (v: unknown): Record<string, unknown> => v !== null && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : fail('object')
const string = (r: Record<string, unknown>, k: string): string => typeof r[k] === 'string' ? r[k] as string : fail(k)
const nullable = (r: Record<string, unknown>, k: string): string | null => r[k] === null ? null : string(r,k)
const integer = (r: Record<string, unknown>, k: string, min = 1): number => typeof r[k] === 'number' && Number.isSafeInteger(r[k]) && (r[k] as number) >= min ? r[k] as number : fail(k)
const boolean = (r: Record<string, unknown>, k: string): boolean => typeof r[k] === 'boolean' ? r[k] as boolean : fail(k)
const choice = <T extends string>(r: Record<string, unknown>, k: string, values: readonly T[]): T => values.includes(r[k] as T) ? r[k] as T : fail(k)
const digest = (r: Record<string, unknown>, k: string): string => /^[a-f0-9]{64}$/.test(string(r,k)) ? string(r,k) : fail(k)
const jobStatuses = ['preparing','queued','submitting','submitted','running','cancel_requested','succeeded','failed','cancelled','uncertain'] as const

export const adaptAssetGenerationOption = (value: unknown): AssetGenerationOption => {
  const r = record(value)
  return { versionId: string(r,'execution_instance_version_id'), key: string(r,'key'), name: string(r,'name'),
    version: integer(r,'version'), operation: string(r,'operation'), mode: string(r,'mode'),
    promptMode: choice(r,'prompt_mode',['profile','manual','none']), definitionSha256: digest(r,'definition_sha256') }
}
export const adaptAssetGenerationOptions = (value: unknown) => {
  const r = record(value)
  if (!Array.isArray(r.results)) fail('results')
  return { count: integer(r,'count',0), next: nullable(r,'next'), previous: nullable(r,'previous'),
    results: (r.results as unknown[]).map(adaptAssetGenerationOption) }
}

export const adaptAssetGenerationJob = (value: unknown, adaptAsset: (v: unknown) => Asset, styleId: (v: string) => string): AssetGenerationJob => {
  const r = record(value)
  const preparation = adaptExecutionPreparation(r.preparation), id = string(r,'id'), version = string(r,'execution_instance_version')
  if (preparation.assetJob !== id || preparation.executionInstanceVersion !== version || preparation.purpose !== 'production') fail('preparation')
  if (r.output_asset !== null && (typeof r.output_asset !== 'object' || Array.isArray(r.output_asset))) fail('output_asset')
  const nullableText = (key: string) => nullable(r,key) ?? ''
  if (!Array.isArray(r.retries)) fail('retries')
  const retries = (r.retries as unknown[]).map(value => {
    const retry = record(value)
    return { id: string(retry,'id'), status: choice(retry,'status',jobStatuses) }
  })
  if (retries.some(retry => retry.id === id) || new Set(retries.map(retry => retry.id)).size !== retries.length) fail('retries')
  return {
    id, retryOf: nullable(r,'retry_of'), retries, executionInstanceVersionId: version, executionInstanceName: string(r,'execution_instance_name'),
    executionVersion: integer(r,'execution_version'), preparation,
    jobType: choice(r,'job_type',['asset','prompt','field']), mediaType: choice(r,'media_type',['image','video','audio','script','text']),
    imageCategory: choice(r,'image_category',['','character','scene','prop','reference']),
    name: string(r,'name'), description: string(r,'description'), styleId: styleId(nullableText('style')),
    executionKind: choice(r,'execution_kind',['deployment','comfy_workflow']),
    modelDeploymentId: string(r,'model_deployment'), modelDeploymentName: string(r,'model_deployment_name'),
    workflowVersionId: nullableText('workflow_template_version'), workflowName: nullableText('workflow_name'),
    workflowVersion: r.workflow_version === null ? null : integer(r,'workflow_version'),
    outputAsset: r.output_asset === null ? null : adaptAsset(r.output_asset),
    status: choice(r,'status',jobStatuses),
    operation: string(r,'operation'), isExternal: boolean(r,'is_external'), allowExternalCalls: boolean(r,'allow_external_calls'),
    budgetLimit: nullable(r,'budget_limit'), estimatedCost: nullable(r,'estimated_cost'), actualCost: nullable(r,'actual_cost'),
    costCurrency: string(r,'cost_currency'), resultText: string(r,'result_text'), remoteStatus: string(r,'remote_status'),
    providerRequestIdSha256: string(r,'provider_request_id_sha256'), pollCount: integer(r,'poll_count',0),
    errorCode: string(r,'error_code'), errorMessage: string(r,'error_message'), ignoreLateResult: boolean(r,'ignore_late_result'),
    submissionStartedAt: nullableText('submission_started_at'), cancelRequestedAt: nullableText('cancel_requested_at'),
    cancelSubmittedAt: nullableText('cancel_submitted_at'), createdAt: string(r,'created_at'), updatedAt: string(r,'updated_at'),
    submittedAt: nullableText('submitted_at'), finishedAt: nullableText('finished_at'),
  }
}
