import type {
  AssetGenerationExecutionKind,
  AssetGenerationJobType,
  AssetGenerationMediaType,
  BibleDraftEntityType,
} from '@/types'
import type { WorkflowExecutionInput } from './platformTypes'

export interface AssetGenerationJobCreateInput {
  jobType: AssetGenerationJobType
  mediaType: AssetGenerationMediaType
  executionKind: AssetGenerationExecutionKind
  generationTargetId: string
  styleId?: string
  imageCategory?: BibleDraftEntityType | 'reference'
  name?: string
  description?: string
  prompt: string
  negativePrompt?: string
  parameters?: Record<string, unknown>
  workflowInputs?: WorkflowExecutionInput
  allowExternalCalls?: boolean
  budgetLimit?: string | null
  idempotencyKey: string
}

export const buildAssetGenerationJobRequestPayload = (
  payload: AssetGenerationJobCreateInput,
  resolvedStyleId: string | null,
) => ({
  job_type: payload.jobType,
  media_type: payload.mediaType,
  execution_kind: payload.executionKind,
  ...(payload.executionKind === 'deployment'
    ? { model_deployment_id: payload.generationTargetId }
    : { workflow_template_version_id: payload.generationTargetId }),
  style_id: resolvedStyleId,
  ...(payload.mediaType === 'image' && payload.imageCategory
    ? { image_category: payload.imageCategory }
    : {}),
  name: payload.name || '',
  description: payload.description || '',
  prompt: payload.prompt,
  negative_prompt: payload.negativePrompt || '',
  parameters: payload.executionKind === 'deployment' ? payload.parameters || {} : {},
  ...(payload.executionKind === 'comfy_workflow' && payload.workflowInputs ? {
    workflow_inputs: {
      schema_version: payload.workflowInputs.schemaVersion,
      values: payload.workflowInputs.values,
      assets: payload.workflowInputs.assets.map((item) => ({
        semantic_key: item.semanticKey,
        asset_id: item.assetId,
      })),
      references: {
        pictures: payload.workflowInputs.references.pictures.map((item) => ({
          asset_id: item.assetId,
          category: item.category,
        })),
        videos: payload.workflowInputs.references.videos.map((item) => ({
          asset_id: item.assetId,
          purpose: item.purpose,
          use_embedded_audio: item.useEmbeddedAudio,
          ...(item.audioAssetId ? { audio_asset_id: item.audioAssetId } : {}),
          ...(item.audioPurpose ? { audio_purpose: item.audioPurpose } : {}),
        })),
        audios: payload.workflowInputs.references.audios.map((item) => ({
          asset_id: item.assetId,
          purpose: item.purpose,
        })),
      },
    },
  } : {}),
  allow_external_calls: payload.allowExternalCalls === true,
  budget_limit: payload.budgetLimit || null,
})
