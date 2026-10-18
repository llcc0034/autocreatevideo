import type { ExecutionPreparationSummary } from '@/services/executionPreparation'

export type AssetType = 'character' | 'scene' | 'prop' | 'image' | 'video' | 'audio' | 'script'
export type ReferenceEntityCategory = 'character' | 'scene' | 'prop'
export type ConditioningMode = string
export type ProjectStatus = 'draft' | 'writing' | 'producing' | 'paused' | 'completed' | 'archived'
export type WorkflowStatus = 'queued' | 'running' | 'paused' | 'completed' | 'failed' | 'cancel_requested' | 'cancelled'

export interface StylePreset {
  isActive: boolean
  id: string
  backendId?: string
  slug: string
  name: string
  englishName: string
  category: string
  description: string
  accent: string
  gradient: string
  tags: string[]
  cover?: string
  /** Backend-annotated total; null means the API did not provide a trustworthy count. */
  assetCount: number | null
  promptPrefix: string
  negativePrompt: string
}

export interface Asset {
  id: string
  styleId: string
  type: AssetType
  name: string
  description: string
  /** Saved visual anchor used by metadata-driven Bible drafting. */
  promptHint?: string
  /** Server-authored human-readable source summary; lineage remains server-side. */
  sourceInfo?: string
  /** Server-issued content digest used to reject stale draft requests. */
  checksum?: string
  /** Null identifies a read-only global library item. */
  ownerId?: string | null
  updatedAt?: string
  metadataSha256?: string
  isActive?: boolean
  thumbnail?: string
  tags: string[]
  seed?: number
  backendKind?: string
  mimeType?: string
  mediaUrl?: string
  downloadUrl?: string
  metadata?: Record<string, unknown>
  hasEmbeddedAudio?: boolean
}

export type AssetGenerationJobType = 'prompt' | 'asset'
export type AssetGenerationMediaType = 'image' | 'video' | 'audio' | 'script'
export type AssetGenerationStatus = 'preparing' | 'queued' | 'submitting' | 'submitted' | 'running' | 'cancel_requested' | 'succeeded' | 'failed' | 'cancelled' | 'uncertain'
export type AssetGenerationResolutionOutcome = 'confirmed_not_submitted' | 'confirmed_remote_failed' | 'confirmed_remote_cancelled' | 'confirmed_remote_succeeded'

export type AssetGenerationExecutionKind = 'deployment' | 'comfy_workflow'

export interface AssetGenerationOption {
  versionId: string
  key: string
  name: string
  version: number
  operation: string
  mode: string
  promptMode: 'profile' | 'manual' | 'none'
  definitionSha256: string
}

export interface AssetGenerationJob {
  id: string
  retryOf: string | null
  retries: Array<{ id: string; status: AssetGenerationStatus }>
  jobType: AssetGenerationJobType | 'field'
  mediaType: AssetGenerationMediaType | 'text'
  imageCategory: '' | 'character' | 'scene' | 'prop' | 'reference'
  name: string
  description: string
  executionInstanceVersionId: string
  executionInstanceName: string
  executionVersion: number
  preparation: ExecutionPreparationSummary
  styleId: string
  executionKind: AssetGenerationExecutionKind
  modelDeploymentId: string
  modelDeploymentName: string
  workflowVersionId: string
  workflowName: string
  workflowVersion: number | null
  outputAsset: Asset | null
  status: AssetGenerationStatus
  operation: string
  isExternal: boolean
  allowExternalCalls: boolean
  budgetLimit: string | null
  estimatedCost: string | null
  actualCost: string | null
  costCurrency: string
  resultText: string
  remoteStatus: string
  providerRequestIdSha256: string
  pollCount: number
  errorCode: string
  errorMessage: string
  ignoreLateResult: boolean
  submissionStartedAt: string
  cancelRequestedAt: string
  cancelSubmittedAt: string
  createdAt: string
  updatedAt: string
  submittedAt: string
  finishedAt: string
}

export type BibleDraftEntityType = 'character' | 'scene' | 'prop'

export type VisualRulesColor = 'black'|'charcoal'|'gray'|'slate'|'ivory'|'white'|'brown'|'sepia'|'navy'|'blue'|'cyan'|'teal'|'green'|'amber'|'gold'|'copper'|'orange'|'red'|'crimson'|'magenta'|'violet'
export type VisualRulesPaletteRelation = 'monochromatic'|'analogous'|'complementary'|'neutral'|'cinematic'
export type VisualRulesLighting = 'soft_diffuse'|'hard_directional'|'low_key'|'high_key'|'practical'|'volumetric'|'rim_accent'|'balanced_ambient'
export type VisualRulesContrast = 'low'|'restrained'|'medium'|'high'
export type VisualRulesSaturation = 'desaturated'|'muted'|'natural'|'selective'|'saturated'
export type VisualRulesExposure = 'dark'|'balanced'|'bright'
export type VisualRulesProgression = 'stable'|'cool_to_warm'|'warm_to_cool'|'dark_to_bright'|'bright_to_dark'|'muted_to_saturated'|'saturated_to_muted'

export interface VisualRulesColorDesign {
  baseColors: VisualRulesColor[]
  accentColor: VisualRulesColor|'none'
  paletteRelation: VisualRulesPaletteRelation
  lighting: VisualRulesLighting
  contrast: VisualRulesContrast
  saturation: VisualRulesSaturation
  exposure: VisualRulesExposure
  progression: VisualRulesProgression
  preserveNaturalSkinExposure: boolean
}


export interface GeneratedImageAssetTarget {
  referenceCategory: ReferenceEntityCategory
  entityId: string
}

export type BibleImageEntityType = 'character' | 'scene' | 'prop'

export interface BibleImageAssetSummary {
  id: string
  kind: string
  name: string
  checksum: string
  previewUrl: string
}

export interface BibleImageLatestCandidate {
  artifactId: string
  productionRunId: string
  stageRunId: string
  generationAttemptId: string
  generationTaskId: string | null
  preparationId: string
  executionInstanceVersionId: string
  executionInstanceName: string
  requestSha256: string
  workflowVersionId: string | null
  modelProfileVersionId: string
  modelIdentifier: string
  promptProfileVersionId: string | null
  targetSnapshotSha256: string
  sourceCurrent: boolean
  integrityStatus: 'valid' | 'invalid'
  integrityErrorCode: string
  variantGroup: string
  variantIndex: number
  status: 'available' | 'selected' | 'rejected' | 'invalid' | 'legacy_missing'
  bindingRole: 'anchor' | 'reference' | 'unbound'
  materializedAsset: BibleImageAssetSummary | null
  previewUrl: string
  downloadUrl: string
  sha256: string
  sizeBytes: number
  mimeType: string
  createdAt: string
}

export interface BibleImageCandidateProjection {
  entityType: BibleImageEntityType
  entityId: string
  entityName: string
  anchorAsset: BibleImageAssetSummary | null
  referenceAssets: BibleImageAssetSummary[]
  latestCandidate: BibleImageLatestCandidate | null
  candidateHistory: BibleImageLatestCandidate[]
}

export interface BibleImageCandidatesResult {
  projectId: string
  candidates: BibleImageCandidateProjection[]
  counts: {
    characters: number
    scenes: number
    props: number
    total: number
    withCandidate: number
  }
}


export interface BibleItem {
  id: string
  slug?: string
  stableSpeakerId?: string
  name: string
  description: string
  /** Canonical identity anchor. It is never inferred from referenceAssetIds. */
  anchorAssetId?: string | null
  /** Supplemental, human-managed reference images; excludes the anchor role. */
  referenceAssetIds: string[]
  locked: boolean
  seed?: number
  metadata?: Record<string, unknown>
  ageAppearance?: string
  faceIdentity?: string
  wardrobe?: string
  voiceProfile?: string
  personality?: string
  negativeConstraints?: string
  location?: string
  timeOfDay?: string
  lighting?: string
  colorPalette?: string
  ambientSound?: string
  material?: string
  distinctiveMarks?: string
  continuityState?: string
}

export interface ContinuityBible {
  characters: BibleItem[]
  scenes: BibleItem[]
  props: BibleItem[]
  negativePrompt: string
  colorScript: string
}

export interface ShotAudio {
  dialogue: import('../services/dialogue').DialogueLinePayload[]
}

export interface Shot {
  /** Explicit episode-relative interval; null pair means duration-only. */
  episodeStartMs?: number | null
  episodeEndMs?: number | null
  id: string
  order: number
  title: string
  mediaType: 'video' | 'image'
  duration: number
  mode: ConditioningMode
  characterRelations: import('../services/shotCharacters').ShotCharacterRelation[]
  sceneId?: string
  propIds: string[]
  /** Continuity intent only; exact materials are chosen through per-run uses. */
  requiresFirstFrameContinuity: boolean
  requiresLastFrameContinuity: boolean
  metadata?: Record<string, unknown>
  audioEnabled: boolean
  audio: ShotAudio
  status: 'draft' | 'ready' | 'rendering' | 'done'
}

export interface Episode {
  id: string
  number: number
  title: string
  synopsis: string
  /** Episode-specific source script used by local Qwen creative authoring. */
  script?: string
  status: 'draft' | 'ready' | 'rendering' | 'completed'
  audioEnabled: boolean
  shots: Shot[]
  promptSettings?: Record<string, unknown>
  shotVersions?: EpisodeShotVersion[]
  activeShotVersionKey?: string
  createdAt: string
}

export interface EpisodeShotVersion {
  key: string
  label: string
  active: boolean
  mode?: string
  shotCount: number
  createdAt?: string
  shots: Shot[]
}

export interface SeriesProject {
  id: string
  title: string
  logline: string
  masterScript: string
  isFavorite: boolean
  /** Dedicated, owner-scoped reference Asset used as the series cover. */
  coverAssetId?: string
  /** Short-lived server-signed preview URL; refresh project data after it expires. */
  coverUrl?: string
  continuitySummary?: string
  translationGlossary?: Record<string, string>
  styleId: string
  status: ProjectStatus
  language: string
  aspectRatio: string
  resolution: '720p' | '1080p' | '4K'
  width?: number
  height?: number
  bible: ContinuityBible
  episodes: Episode[]
  /** List endpoints provide aggregate counts without loading every episode and shot. */
  episodeCount?: number
  completedEpisodeCount?: number
  totalDurationSeconds?: number
  /** False only for list summaries whose Bible and episodes have not been hydrated. */
  detailLoaded?: boolean
  createdAt: string
  updatedAt: string
}

export interface PromptWritingGuideFieldRule {
  fieldKey: string
  fieldLabel?: string
  sourceSelector: string
  channel: string
  position: number
  labelStyle: string
  separator: string
  maxChars: number
  generationMode?: string
  generationOutputSemantics?: string
}

export interface PromptWritingGuideProfile {
  profileKey: string
  name: string
  version: number
  sourceProjectionKey: string
  officialReference?: string
  fieldRules: PromptWritingGuideFieldRule[]
}

export type TranslationReviewStatus = 'untranslated' | 'translating' | 'pending_review' | 'confirmed' | 'failed' | 'source_changed'
export type TranslationBackendStatus = 'draft_translation' | 'confirmed' | 'needs_retranslation' | 'failed'

export interface PromptTranslationField {
  fieldKey: string
  label: string
  sourceText: string
  scopeType: 'project' | 'episode' | 'shot' | 'character' | 'scene' | 'prop'
  scopeId: string
  impact: 'prompt' | 'continuity'
  help?: string
}

export interface PromptTranslationEntry {
  id: string
  projectId?: string
  fieldKey: string
  scopeType?: PromptTranslationField['scopeType']
  scopeId?: string
  sourceText: string
  translatedText: string
  status: TranslationBackendStatus
  revision: number
  confirmedAt?: string
  updatedAt?: string
  provider?: string
  model?: string
  error?: string
  runtimeMetadata?: Record<string, unknown>
  contextCurrent?: boolean
  confirmationTarget?: 'field_review' | 'canonical_glossary'
  canonicalValue?: string | null
}


export interface WorkflowNode {
  id: string
  label: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancel_requested' | 'cancelled'
  type: 'script' | 'prompt' | 'image' | 'video' | 'audio' | 'compose' | 'export'
  progress: number
}

export interface TaskOutputMedia {
  index?: number
  kind: string
  filename: string
  previewUrl: string
  downloadUrl: string
  aiGenerated: boolean
  provenanceUrl?: string
  artifactSha256?: string
  manifestSha256?: string
  distributionStatus?: 'not_prepared' | 'processing' | 'ready' | 'failed' | string
  distributionError?: string
  publicProvenanceUrl?: string
  publicArtifactSha256?: string
  publicManifestSha256?: string
  publicLabel?: string
  publicVersion?: string
  promotedAssetId?: string
}

export interface TaskReferenceAssetSummary {
  referenceBinding: import('../services/frozenReferenceBinding').FrozenReferenceBinding
  itemKey: string
  inputKey: string
  assetId?: string
  artifactId?: string
  displayName: string
  mediaKind: 'image' | 'video' | 'audio'
  previewUrl: string
  inputLabel?: string
  purpose?: string
  pairedTo?: string
  checksum: string
  sourceSha256: string
  ports: Array<{ targetKey: string; port: Record<string, unknown> }>
  stagingStatus: 'staged' | 'not_staged' | 'not_applicable'
}

export interface TaskInputPrompt {
  taskId: string
  attemptId: string
  preparationId: string
  executionInstanceVersionId: string
  requestSha256: string
  promptMode: 'manual' | 'profile' | 'none'
  promptProfileVersionId: string | null
  channels: Array<{ key: string; semanticKey: string; text: string; characterCount: number; valueSource: string }>
  declaredReferenceLabels: string[]
}

export interface WorkflowTimelineEvent {
  event: string
  status: string
  occurredAt: string
  label: string
  details?: string
}

export type WorkflowProgressSource = 'none' | 'submission' | 'comfyui' | 'terminal'
export type WorkflowProgressTelemetryState = 'unknown' | 'connected' | 'receiving' | 'unavailable'
export type WorkflowInputContractStatus = 'available' | 'unavailable'
export type WorkflowInputContractCode = '' | 'frozen_contract_invalid'

export interface Workflow {
  execution: TaskExecutionIdentity | null
  id: string
  taskId?: string
  kind?: 'video' | 'image' | 'audio'
  backendStatus?: string
  comfyPromptId?: string
  submissionToken?: string
  submissionState?: string
  inputParameters?: Record<string, unknown>
  inputContractStatus?: WorkflowInputContractStatus
  inputContractCode?: WorkflowInputContractCode
  referenceAssets: TaskReferenceAssetSummary[]
  contextMediaUses: TaskReferenceAssetSummary[]
  errorMessage?: string
  outputMedia?: TaskOutputMedia[]
  queuedAt?: string
  startedAt?: string
  finishedAt?: string
  submissionPreparedAt?: string
  submissionSubmittedAt?: string
  durationMs?: number
  queueWaitMs?: number
  totalDurationMs?: number
  timeline?: WorkflowTimelineEvent[]
  lastHeartbeatAt?: string
  timeoutAt?: string
  isStalled?: boolean
  stallReason?: string
  projectId: string
  projectTitle: string
  status: WorkflowStatus
  /** Legacy platform-stage progress. Do not present it as ComfyUI sampler progress. */
  progress: number
  /** Describes whether a user-visible percentage is a submission marker, ComfyUI sample, or terminal value. */
  progressSource?: WorkflowProgressSource
  /** Raw value/max percentage for the current ComfyUI node; not overall task completion. */
  comfyProgress?: number
  progressNodeId?: string
  progressUpdatedAt?: string
  progressTelemetryState?: WorkflowProgressTelemetryState
  progressTelemetryError?: string
  remoteState?: string
  progressIndeterminate?: boolean
  progressNote?: string
  currentStep: string
  nodes: WorkflowNode[]
  createdAt: string
  eta?: string
  attempts?: number
  episodeId?: string
  episodeNumber?: number
  episodeTitle?: string
}

export interface TaskExecutionIdentity {
  productionRunId: string
  stageRunId: string
  stageKey: string
  stageOrdinal: number
  unitKey: string
  attemptId: string
  attemptNumber: number
  preparationId: string
  executionInstanceVersionId: string
  operation: string
}

export interface TaskWorkflowExport {
  taskId: string
  attemptId: string
  preparationId: string
  executionInstanceVersionId: string
  workflowVersionId: string
  requestSha256: string
  definitionSha256: string
  graphState: 'prepared' | 'staged'
  missingReceiptCount: number
  workflowJson: Record<string, unknown>
  contentHash: string
  filename: string
  format?: string
  importReady?: boolean
  comfyPromptId?: string
  comfyuiUrl?: string
  directOpenUrl?: string
  safeToOpen: boolean
  directOpenSupported?: boolean
  openMode?: string
  downloadUrl?: string
  availableFormats?: Array<'comfyui_api' | 'comfyui_ui'>
  apiDownloadUrl?: string
  uiDownloadUrl?: string
  containsStagedPlaceholders?: boolean
  stagedPlaceholderCount?: number
  openReason?: string
}

export interface AssemblyResult {
  id: string
  status: 'planned' | 'queued' | 'running' | 'succeeded' | 'failed'
  scope: 'episode' | 'series'
  projectId: string
  episodeId?: string
  outputUrl?: string
  downloadUrl?: string
  aiGenerated?: boolean
  provenanceUrl?: string
  sourceMediaCount?: number
  createdAt?: string
  queuedAt?: string
  startedAt?: string
  finishedAt?: string
  attempts?: number
  maxAttempts?: number
  errorMessage?: string
  message: string
}

export interface ReleaseShotSelection {
  shotId: string
  artifactId: string
}

export interface ReleaseEpisodeSelection {
  episodeId: string
  artifactId: string
}

export interface ReleaseSelection {
  shots: ReleaseShotSelection[]
  episodes: ReleaseEpisodeSelection[]
}

export interface ReleaseBundle {
  id: string
  projectId: string
  productionRunId: string
  version: number
  title: string
  notes: string
  status: 'draft' | 'sealed' | 'released' | 'superseded'
  selection: ReleaseSelection
  assemblySources?: { episodeId: string; artifactId: string; assemblyId: string }[]
  manifestSha256?: string
  verificationStatus: 'draft' | 'verified' | 'invalid'
  sealedAt?: string
  releasedAt?: string
  createdAt?: string
  updatedAt?: string
}
