export type AssetType = 'character' | 'scene' | 'prop' | 'image' | 'video' | 'audio'
export type Ref2VAReferenceCategory = 'character' | 'scene' | 'prop'
export type H3Mode = 'T2VA' | 'I2VA' | 'FL2VA' | 'L2VA' | 'Ref2VA'
export type ProjectStatus = 'draft' | 'writing' | 'producing' | 'paused' | 'completed' | 'archived'
export type WorkflowStatus = 'queued' | 'running' | 'paused' | 'completed' | 'failed'

export interface StylePreset {
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
  metadata?: Record<string, unknown>
  hasEmbeddedAudio?: boolean
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
  referenceCategory: Ref2VAReferenceCategory
  entityId: string
}

export type BibleImageEntityType = 'character' | 'scene' | 'prop'
export type BibleImageGenerationPurpose = 'bible_image_candidate'
export type BibleImageTaskStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancel_requested' | 'cancelled' | 'incomplete'

export interface BibleImagePromptConfig {
  schemaVersion: 1
  stylePrompt: string
  styleNegativePrompt: string
  compositionRules: Record<BibleImageEntityType, string>
  negativeRules: Record<BibleImageEntityType, string>
  qualitySuffix: string
}

export interface BibleImagePromptInput {
  key: string
  label: string
  value: string
  role: 'positive' | 'negative'
  source: string
  editable: boolean
}

export interface BibleImagePromptInputs {
  schemaVersion: 1
  positive: BibleImagePromptInput[]
  negative: BibleImagePromptInput[]
  finalPromptText: string
  negativePrompt: string
}

export interface BibleImageAssetSummary {
  id: string
  kind: string
  name: string
  checksum: string
  previewUrl: string
}

export interface BibleImageQualityIssue {
  code: string
  severity: string
  path: string
  message: string
}

export interface BibleImageCandidateQuality {
  policyId?: string
  status?: string
  advisoryOnly?: boolean
  wouldBlockEnqueue?: boolean
  issues: BibleImageQualityIssue[]
  warnings: string[]
  raw: Record<string, unknown>
}

export interface BibleImageLatestCandidate {
  promptArtifactId: string
  generatedWorkflowId: string | null
  generationTaskId: string | null
  taskStatus: BibleImageTaskStatus
  createdAt: string
  idempotencyKey: string
  requestFingerprint: string
  sourceFingerprint: string
  sourceCurrent: boolean
  quality: BibleImageCandidateQuality
  promptInputs: BibleImagePromptInputs
  outputMedia: TaskOutputMedia[]
  promotedAssets: BibleImageAssetSummary[]
  reviewRequired: boolean
  persistedAsAsset: boolean
}

export interface BibleImageCandidateProjection {
  entityType: BibleImageEntityType
  entityId: string
  entityName: string
  sourceFingerprint: string
  currentPromptInputs: BibleImagePromptInputs
  anchorAsset: BibleImageAssetSummary | null
  referenceAssets: BibleImageAssetSummary[]
  latestCandidate: BibleImageLatestCandidate | null
  candidateHistory: BibleImageLatestCandidate[]
}

export interface BibleImageCandidatesResult {
  projectId: string
  promptConfig: BibleImagePromptConfig
  candidates: BibleImageCandidateProjection[]
  counts: {
    characters: number
    scenes: number
    props: number
    total: number
    withCandidate: number
  }
}

export interface BibleImageFrozenTarget {
  schemaVersion: 1
  referenceCategory: BibleImageEntityType
  entityId: string
  entityName: string
  sourceFingerprint: string
}

export type DeferredFrameSlot = 'first_frame' | 'last_frame'
export type DeferredGenerationStatus =
  | 'preparing_prompt'
  | 'waiting_upstream'
  | 'extracting'
  | 'materializing'
  | 'materialized'
  | 'failed'
  | 'stale'
  | 'cancelled'

export interface DeferredGenerationRequest {
  id: string
  projectId: string
  sourceTaskId: string
  sourceOutputRole: 'primary_video'
  targetShotId: string
  frameSlot: DeferredFrameSlot
  targetTemplateId: string
  targetTemplateHash: string
  idempotencyKey: string
  requestFingerprint: string
  targetInputFingerprint: string
  status: DeferredGenerationStatus
  resolvedSourceOutputIndex: number | null
  sourceArtifactSha256: string | null
  sourceManifestSha256: string | null
  derivedAssetId: string | null
  materializedTaskId: string | null
  materializedPromptArtifactId: string | null
  materializedWorkflowId: string | null
  priority: number
  attempts: number
  maxAttempts: number
  nextAttemptAt: string | null
  extendedGenerationConfirmed: boolean
  errorCode: string
  errorMessage: string
  startedAt: string | null
  finishedAt: string | null
  createdAt: string
  updatedAt: string
  provisionalPromptText?: string
  provisionalNegativePrompt?: string
  provisionalPromptSha256?: string
}

export interface BibleItem {
  id: string
  slug?: string
  stableSpeakerId?: string
  name: string
  description: string
  visualPrompt: string
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
  dialogue: string
  soundscape: string
  music: string
}

export interface H3ReferenceEntityBinding {
  entityId: string
  assetId: string
}

export type H3ReferenceVideoPurpose = 'motion_camera_reference' | 'video_editing' | 'video_continuation'
export type H3ReferenceAudioPurpose = 'audio_reference' | 'audio_reuse'

export interface H3ReferenceVideoBinding {
  assetId: string
  purpose: H3ReferenceVideoPurpose
  useEmbeddedAudio: boolean
  audioAssetId?: string
  audioPurpose?: H3ReferenceAudioPurpose
}

export interface H3ReferenceAudioBinding {
  assetId: string
  purpose: H3ReferenceAudioPurpose
}

/**
 * Typed material bindings persisted with a shot. Asset IDs always come from
 * the platform material library; the UI never accepts a local filesystem path.
 */
export interface H3ReferenceBindings {
  firstFrameAssetId?: string
  lastFrameAssetId?: string
  firstFrameReferenceAssetId?: string
  characters: H3ReferenceEntityBinding[]
  scene?: H3ReferenceEntityBinding
  props: H3ReferenceEntityBinding[]
  videos: H3ReferenceVideoBinding[]
  audios: H3ReferenceAudioBinding[]
}

export interface Shot {
  id: string
  order: number
  title: string
  mediaType: 'video' | 'image'
  duration: number
  mode: H3Mode
  framing: string
  camera: string
  action: string
  /**
   * Optional long-form, manually reviewed director description produced by the
   * single-shot detail draft flow.  It is the only long-form source that the
   * H3 compiler may prefer over the compact storyboard fields.
   */
  detailedDescription?: string
  continuityDirection?: string
  prompt: string
  generatedPrompt?: H3PromptPayload
  /** Ephemeral editor hint; never persisted by the shot sync payload. */
  promptRefreshReason?: 'compiler_updated' | 'translations_changed' | 'inputs_changed'
  /** Backend-authoritative eligibility for the guarded legacy recovery action. */
  legacyNegativePromptRepairEligible?: boolean
  negativePrompt: string
  characterIds: string[]
  sceneId?: string
  propIds: string[]
  referenceAssetIds: string[]
  referenceBindings?: H3ReferenceBindings
  /** Continuity intent only; concrete frame assets remain in referenceBindings. */
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
  bibleImagePromptConfig: BibleImagePromptConfig
  styleId: string
  status: ProjectStatus
  aspectRatio: '9:16' | '16:9' | '1:1'
  resolution: '720p' | '1080p' | '4K'
  width?: number
  height?: number
  defaultMode: H3Mode
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

export interface H3PromptPayload {
  /** Backend-authoritative compiler compatibility for persisted artifacts. */
  compiler_current?: boolean
  /** Immutable PromptArtifact metadata used only for preview freshness checks. */
  artifact_id?: string
  artifact_revision?: number
  artifact_created_at?: string
  model: string
  mediaType: 'video' | 'image'
  mode?: H3Mode
  effective_conditioning_mode?: H3Mode | 'TEXT_ONLY'
  prompt_template_key?: string
  reference_bindings?: Array<Record<string, unknown>>
  duration?: number
  /** Exact text persisted in PromptArtifact.prompt_text and injected into ComfyUI. */
  prompt_text: string
  integrated_multimodal_description: string
  subject_definitions?: string[]
  summary?: string
  retention_analysis?: string[]
  detailed_description?: string[]
  overall_soundscape?: string
  non_diegetic_music?: string
  quality?: {
    status: 'ready' | 'needs_attention'
    language_policy: string
    dialogue_language?: string
    prompt_character_count: number
    prompt_character_limit?: number
    prompt_word_count?: number
    prompt_word_count_is_approximate?: boolean
    detailed_description_word_count?: number
    warnings: string[]
    fields_requiring_english_rewrite: string[]
    source_fields_optimized?: string[]
    optimizer?: {
      status: 'optimized' | 'not_required' | 'disabled' | 'misconfigured' | 'failed_request' | 'failed_validation' | 'not_configured'
      enabled: boolean
      provider: string
      model: string
      transport: 'local' | 'remote'
      failure_mode: 'warn' | 'block'
      message: string
      validation_errors: string[]
    }
    production_language_gate?: {
      mode: 'block' | 'warn'
      would_block_enqueue: boolean
      message: string
    }
  }
  negative_prompt: string
  continuity: Record<string, unknown>
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

export interface PromptTranslationPromotedField {
  fieldKey: string
  value: string
  dialogueIndex: number | null
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
  /** Authoritative Shot fields atomically promoted by confirm. */
  promotedFields?: PromptTranslationPromotedField[]
}

export interface ShotTranslationPromotionResult {
  shotId: string
  promotedFields: PromptTranslationPromotedField[]
  entries: PromptTranslationEntry[]
  shotPatch: {
    metadata?: Record<string, unknown>
    updatedAt?: string
  }
}

export interface PromptNormalizationDeployment {
  modelDeploymentId: string
  deploymentKey: string
  label: string
  providerKey: string
  providerLabel: string
  providerDriver: string
  modelProfileVersionId: string
  modelIdentifier: string
  capability: 'prompt.normalize'
  state: 'ready' | 'smoke_test_required' | 'unavailable'
  ready: boolean
  reasonCode: string
  transport: 'local_endpoint' | 'remote_api'
  dataEgress: boolean
  dataEgressNotice: string
  estimatedCost?: string
  costCurrency: string
  costBasis: string
  smokeSnapshotId?: string
  smokeExpiresAt?: string
}

export interface TranslationCapability {
  schemaVersion: 2
  capabilityKey: 'prompt.normalize'
  reviewRequired: boolean
  defaultModelDeploymentId?: string
  configurationPublicSha256: string
  deployments: PromptNormalizationDeployment[]
}

export type CanonicalGlossaryScope = 'character' | 'scene' | 'prop'
export type CanonicalGlossaryEnsureStatus = 'generated' | 'reused' | 'conflict' | 'failed'
export type BibleVisualTranslationEnsureStatus = 'generated' | 'reused' | 'not_required' | 'failed' | 'conflict'

export interface BibleVisualTranslationEnsureEntry {
  scopeType: CanonicalGlossaryScope
  scopeId: string
  fieldKey: string
  sourceText: string
  translatedText: string
  translationId?: string
  translationStatus?: TranslationBackendStatus
  revision?: number
  status: BibleVisualTranslationEnsureStatus
  origin: 'model' | 'existing' | 'source_english' | 'none'
  contextCurrent: boolean
  retryable: boolean
  errorCode?: string
  message?: string
  translation?: PromptTranslationEntry
}

export interface CanonicalGlossaryEnsureEntry {
  scopeType: CanonicalGlossaryScope
  scopeId: string
  name: string
  canonicalEnglish: string
  status: CanonicalGlossaryEnsureStatus
  origin: 'model' | 'existing' | 'source_english' | 'none'
  retryable: boolean
  errorCode?: string
  message?: string
}

export interface CanonicalGlossaryEnsureResult {
  projectId: string
  updatedAt?: string
  translationGlossary: Record<string, string>
  summary: {
    requested: number
    generated: number
    reused: number
    conflict: number
    failed: number
  }
  entries: CanonicalGlossaryEnsureEntry[]
  visualSummary: {
    requested: number
    generated: number
    reused: number
    notRequired: number
    conflict: number
    failed: number
  }
  visualTranslations: BibleVisualTranslationEnsureEntry[]
  runtime?: {
    invocationAttempted: boolean
    submissionUncertain: boolean
    modelDeploymentId?: string
    provider?: string
    dataEgress: boolean
  }
}

export interface WorkflowNode {
  id: string
  label: string
  status: 'pending' | 'running' | 'completed' | 'failed'
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
  assetId?: string
  displayName: string
  mediaKind: 'image' | 'video' | 'audio'
  previewUrl: string
  category?: string
  inputLabel?: string
  entityLabel?: string
  useEmbeddedAudio?: boolean
  sourceFps?: number
  effectiveFps?: number
  effectiveDurationMs?: number
  cfrGuaranteed?: boolean
  pairedAudioDurationAligned?: boolean
}

export interface TaskInputPrompt {
  taskId: string
  promptArtifactId?: string
  promptType?: string
  model?: string
  h3Mode?: string
  effectiveConditioningMode?: string
  promptTemplateKey?: string
  contentHash?: string
  prompt: string
  negativePrompt: string
  promptCharacterCount: number
  negativePromptCharacterCount: number
  promptSource?: string
  negativePromptSource?: string
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

export interface Workflow {
  id: string
  taskId?: string
  kind?: 'video' | 'image'
  backendStatus?: string
  generatedWorkflowId?: string
  comfyPromptId?: string
  submissionToken?: string
  submissionState?: string
  backend?: string
  modelProfile?: string
  workflowVersion?: number
  seed?: number
  effectiveFrames?: number
  effectiveDurationMs?: number
  inputParameters?: Record<string, unknown>
  generationPurpose?: BibleImageGenerationPurpose
  bibleImageTarget?: BibleImageFrozenTarget
  referenceAssets?: TaskReferenceAssetSummary[]
  continuityAssets?: TaskReferenceAssetSummary[]
  promptAvailable?: boolean
  promptCharacterCount?: number
  negativePromptCharacterCount?: number
  errorMessage?: string
  outputs?: unknown[]
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
  maxAttempts?: number
  batchId?: string
  batchIndex?: number
  batchSize?: number
  batchKind?: string
  shotVersionKey?: string
  shotVersionLabel?: string
  batchSlots?: WorkflowBatchSlot[]
  batchTaskIds?: string[]
  isVirtualProductionUnit?: boolean
  virtualProductionUnitKind?: 'deferred_first_frame_continuity' | 'waiting_for_deferred_first_frame_source' | 'waiting_for_deferred_first_frame_chain'
  deferredGenerationRequestId?: string
  deferredGenerationStatus?: string
  deferredSourceTaskId?: string
  deferredUpstreamRequestId?: string
  deferredFrameSlot?: 'first_frame' | 'last_frame'
  batchWorkflows?: Workflow[]
  episodeId?: string
  episodeNumber?: number
  episodeTitle?: string
  shotId?: string
  shotIds?: string[]
  shotNumber?: number
  shotTitle?: string
}

export type WorkflowBatchSlotStatus =
  | 'materialized'
  | 'waiting_deferred'
  | 'cancelled'
  | 'cancelled_by_upstream'
  | 'not_created'

export interface WorkflowBatchSlot {
  batchId?: string
  batchKind?: string
  shotVersionKey?: string
  shotVersionLabel?: string
  batchIndex: number
  batchSize: number
  slotStatus: WorkflowBatchSlotStatus
  message?: string
  workflowMode?: string
  taskId?: string
  taskStatus?: string
  workflowId?: string
  deferredGenerationRequestId?: string
  deferredGenerationStatus?: string
  deferredUpstreamRequestId?: string
  episodeId?: string
  episodeNumber?: number
  episodeTitle?: string
  shotId?: string
  shotNumber?: number
  shotTitle?: string
  createdAt?: string
  updatedAt?: string
}

export interface GeneratedWorkflowExport {
  taskId: string
  generatedWorkflowId: string
  workflowJson: Record<string, unknown>
  injectedValues: Record<string, unknown>
  contentHash: string
  filename: string
  format?: string
  templateSlug?: string
  templateVersion?: number
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

export interface ComfyUIWorkflowSyncResult {
  taskId: string
  generatedWorkflowId: string
  synced: boolean
  workflowPath: string
  filename: string
  comfyuiUrl: string
  openReason: string
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
  sourceTaskIds?: string[]
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
  taskId: string
  outputIndex: number
}

export interface ReleaseEpisodeSelection {
  episodeId: string
  assemblyId: string
}

export interface ReleaseSelection {
  shots: ReleaseShotSelection[]
  episodes: ReleaseEpisodeSelection[]
  seriesAssemblyId?: string
}

export interface ReleaseBundle {
  id: string
  projectId: string
  version: number
  title: string
  notes: string
  status: 'draft' | 'sealed' | 'released' | 'superseded'
  selection: ReleaseSelection
  manifestSha256?: string
  verificationStatus: 'draft' | 'verified' | 'invalid'
  sealedAt?: string
  releasedAt?: string
  createdAt?: string
  updatedAt?: string
}
