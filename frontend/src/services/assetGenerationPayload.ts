import type { AssetGenerationJob, AssetGenerationJobType, AssetGenerationMediaType, BibleDraftEntityType } from '@/types'
import type { ExecutionSmokeRequest } from './executionPreparation'
import type { AssetAppearanceTarget } from './assetAppearanceTarget'

export function assetPromptContinuationTarget(job: Pick<AssetGenerationJob, 'id' | 'jobType' | 'mediaType' | 'status' | 'resultText'>) {
  // Standalone text candidates are reviewed by their source page, not a text Asset category.
  if (job.jobType !== 'prompt' || job.mediaType === 'text' || job.status !== 'succeeded' || !job.resultText) return null
  return {name:'assets',query:{type:job.mediaType,promptJob:job.id}}
}

export interface AssetGenerationJobCreateInput {
  jobType: AssetGenerationJobType
  mediaType: AssetGenerationMediaType
  executionInstanceVersionId: string
  expectedDefinitionSha256: string
  fixture: ExecutionSmokeRequest['fixture']
  styleId?: string
  imageCategory?: BibleDraftEntityType | 'reference'
  name: string
  description: string
  allowExternalCalls: boolean
  idempotencyKey: string
  appearanceTarget?: AssetAppearanceTarget
}

// One explicit instance version; callers cannot select a second runtime or graph.
export const buildAssetGenerationJobRequestPayload = (payload: AssetGenerationJobCreateInput, styleId: string | null) => ({
  execution_instance_version_id: payload.executionInstanceVersionId,
  expected_definition_sha256: payload.expectedDefinitionSha256,
  job_type: payload.jobType, media_type: payload.mediaType,
  inputs: payload.fixture.inputs, parameters: payload.fixture.parameters,
  manual_channels: payload.fixture.manual_channels, subject_facts: payload.fixture.subject_facts,
  source_language: payload.fixture.source_language, target_language: payload.fixture.target_language,
  field_values: [], style_id: styleId,
  ...(payload.jobType === 'asset' && payload.mediaType === 'image' ? { image_category: payload.imageCategory } : {}),
  name: payload.name, description: payload.description,
  allow_external_calls: payload.allowExternalCalls,
  ...(payload.appearanceTarget ? { appearance_target: JSON.parse(JSON.stringify(payload.appearanceTarget)) as AssetAppearanceTarget } : {}),
})
