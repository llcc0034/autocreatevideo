export type AssetType = 'character' | 'scene' | 'prop' | 'image' | 'video' | 'audio'
export type Ref2VAReferenceCategory = 'character' | 'scene' | 'prop'
export type H3Mode = 'T2VA' | 'I2VA' | 'FL2VA' | 'L2VA' | 'Ref2VA'
export type ProjectStatus = 'draft' | 'writing' | 'producing' | 'paused' | 'completed' | 'archived'
export type WorkflowStatus = 'queued' | 'running' | 'paused' | 'completed' | 'failed'
export type ProductionResolutionProfile = 'project' | '0.1' | '0.2' | '0.3' | '0.4' | '0.5' | '0.6' | '0.7' | '0.8' | '0.9' | '0.98' | '1.0' | '1.2' | '1.5' | '1.8' | '2.0'

export interface WorkflowUserParameterDefinition {
  label: string
  type: 'integer' | 'number'
  default: number
  min?: number
  max?: number
  step?: number
  help?: string
}

export interface ProductionWorkflowTemplate {
  id: string
  name: string
  slug: string
  version: number
  outputKind: 'video' | 'image'
  isSystem: boolean
  isActive: boolean
  h3Modes: H3Mode[]
  isDefault: boolean
  maturity: 'stable' | 'experimental'
  userParameters: Record<string, WorkflowUserParameterDefinition>
}

export interface ProductionWorkflowSelection {
  outputKind: 'video'
  h3Mode: H3Mode
  templateId: string
  parameters: Record<string, number>
}

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
  assetCounts: { character: number; scene: number; prop: number }
  promptPrefix: string
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

export type BibleDraftLanguage = 'zh' | 'en'
export type BibleDraftEntityType = 'character' | 'scene' | 'prop'

export interface BibleAssetDraftSource {
  id: string
  name: string
  kind: string
  checksum: string
  description?: string
  promptHint?: string
  updatedAt?: string
  metadataSha256?: string
  metadataFieldsUsed?: string[]
}

export interface BibleAssetDraftResult {
  evidenceBasis: 'asset_metadata'
  pixelVisionUsed: false
  referenceMode: 'single_reference'
  sourceMetadataSha256?: string
  outputLanguage: BibleDraftLanguage
  entityType: BibleDraftEntityType
  draft: {
    name: string
    description: string
    visualPrompt: string
  }
  sourceAssets: BibleAssetDraftSource[]
  warnings: string[]
  runtime?: Record<string, unknown>
}

export interface VisualRulesDraftData {
  negativePrompt: string
  colorScript: string
  continuitySummary: string
}

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

export interface VisualRulesDraftRuntime {
  provider: string
  model: string
  loadedOnDemand: boolean
  unloadedAfterRequest: boolean
  reviewRequired: boolean
  persisted: false
  elapsedMs: number
  repairAttempted: boolean
  repairSucceeded: boolean
}

export interface VisualRulesDraftResult {
  projectId: string
  contextFingerprint: string
  contextCurrent: boolean
  candidateLanguage: 'en'
  reviewRequired: true
  persisted: false
  draft: VisualRulesDraftData
  fieldModes: {
    negativePrompt: 'deterministic_guard'
    colorScript: 'structured_design'
    continuitySummary: 'deterministic_guard'
  }
  colorDesign: VisualRulesColorDesign
  sourceSummary: {
    projectFields: string[]
    productionSettingFields: string[]
    styleIncluded: boolean
    characterCount: number
    sceneCount: number
    propCount: number
    episodeSummaryCount: number
    episodeScriptsIncluded: false
    shotsIncluded: false
  }
  generation: {
    attempt: number
    directionNoteProvided: boolean
    variationNoteProvided: boolean
    generationFingerprint: string
  }
  warnings: string[]
  runtime: VisualRulesDraftRuntime
}

export interface VisualRulesDraftApplyResult {
  projectId: string
  appliedContextFingerprint: string
  currentContextFingerprint: string
  updatedAt: string
  applied: VisualRulesDraftData
}

export interface GeneratedImageAssetTarget {
  referenceCategory: Ref2VAReferenceCategory
  entityId: string
}

export interface BibleItem {
  id: string
  slug?: string
  stableSpeakerId?: string
  name: string
  description: string
  visualPrompt: string
  referenceAssetIds: string[]
  locked: boolean
  seed?: number
  metadata?: Record<string, unknown>
  ageAppearance?: string
  faceIdentity?: string
  wardrobe?: string
  voiceProfile?: string
  personality?: string
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
  multiShot: boolean
  audioEnabled: boolean
  shots: Shot[]
  createdAt: string
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

export type PromptProviderId = 'local_qwen' | 'deepseek_api'

export interface PromptProviderCapability {
  id: PromptProviderId
  label: string
  enabled: boolean
  configured: boolean
  ready: boolean
  /** Transient queue state; unlike `ready: false`, this does not disable submission. */
  temporarilyBusy?: boolean
  model: string
  transport: 'local' | 'remote'
  dataEgressNotice?: string
  notice?: string
  modelDownloaded?: boolean
  modelVerified?: boolean
  device?: string
  cudaRequested?: boolean
  gpuReady?: boolean
  gpuLayers?: number
  runtimeProbeErrorCode?: string
  loadedOnDemand?: boolean
  unloadedAfterRequest?: boolean
  reviewRequired?: boolean
  translationReviewReused?: boolean
  minShots?: number
  defaultShots?: number
  recommendedMaxShots?: number
  batchShots?: number
  maxDialoguePerShot?: number
  maxSourceChars?: number
  recommendedTitleZhChars?: number
  recommendedTitleEnChars?: number
  recommendedNarrativeZhChars?: number
  recommendedNarrativeEnChars?: number
  recommendedCameraZhChars?: number
  recommendedCameraEnChars?: number
  recommendedActionZhChars?: number
  recommendedActionEnChars?: number
  recommendedContinuityZhChars?: number
  recommendedContinuityEnChars?: number
  recommendedSoundscapeZhChars?: number
  recommendedSoundscapeEnChars?: number
  recommendedMusicZhChars?: number
  recommendedMusicEnChars?: number
  maxCameraMovements?: number
  maxActionSequenceMarkers?: number
  minScriptActionCoveragePercent?: number
  minShotDurationMs?: number
  maxShotDurationMs?: number
  baselineMaxNewTokens?: number
  maxNewTokens?: number
  configuredMaxNewTokens?: number
  contextSize?: number
  runtimeTimeoutSeconds?: number
  processTimeoutSeconds?: number
}

export interface TranslationCapability {
  enabled: boolean
  configured: boolean
  provider: string
  model: string
  sourceLanguage: string
  targetLanguage: string
  message?: string
  modelDownloaded?: boolean
  modelVerified?: boolean
  modelSizeBytes?: number
  modelVerifiedAt?: string
  device?: string
  loadedOnDemand?: boolean
  unloadedAfterRequest?: boolean
  reviewRequired?: boolean
  officialQwenMtCheckpoint?: boolean
  installCommand?: string
  estimatedDownloadGb?: number
  defaultProvider?: PromptProviderId
  providers?: PromptProviderCapability[]
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
  origin: 'local_qwen' | 'existing' | 'source_english' | 'none'
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
  origin: 'local_qwen' | 'existing' | 'source_english' | 'none'
  retryable: boolean
  errorCode?: string
  message?: string
}

export interface CanonicalGlossaryEnsureResult {
  projectId: string
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
    loadedOnDemand: boolean
    unloadedAfterRequest: boolean
  }
}

export type CreativeDraftStatus = 'draft' | 'confirmed' | 'applied' | 'superseded' | 'failed'
export type CreativeDraftCreativity = 'conservative' | 'balanced' | 'bold'
export type CreativeDraftLanguageMode = 'chinese_only' | 'bilingual_confirmed'
export type CreativeDraftPlacement = 'append' | 'replace'

export interface CreativeGenerationCapability {
  supported: boolean
  configured: boolean
  provider: string
  model: string
  modelDownloaded: boolean
  modelVerified: boolean
  device: string
  minShots?: number
  batchShots?: number
  maxSourceChars?: number
  minShotDurationMs?: number
  maxShotDurationMs?: number
  baselineMaxNewTokens?: number
  maxNewTokens?: number
  configuredMaxNewTokens?: number
  contextSize?: number
  loadedOnDemand: boolean
  unloadedAfterRequest: boolean
  reviewRequired: boolean
  translationReviewReused: boolean
  notice?: string
  defaultProvider?: PromptProviderId
  providers?: PromptProviderCapability[]
  shotHighDetailPolicies?: Partial<Record<PromptProviderId, ShotHighDetailPolicy>>
  shotHighDetailSharedResourceBusy?: boolean
}

export interface ShotHighDetailPolicy {
  provider?: PromptProviderId
  recommendedZhChars?: number
  recommendedEnChars?: number
  transportZhMaxChars?: number
  transportEnMaxChars?: number
  recommendedMaxShots?: number
  maxAttempts?: number
  [key: string]: unknown
}

export interface CreativeDraftEpisodeCopy {
  titleZh: string
  titleEn: string
  synopsisZh: string
  synopsisEn: string
}

export interface CreativeDraftDialogueLine {
  speakerId: string
  /** H3 authored dialogue remains Chinese and is never machine-translated. */
  textZh: string
  deliveryZh: string
  deliveryEn: string
  voiceover: boolean
}

export interface CreativeDraftShot {
  clientKey: string
  order: number
  titleZh: string
  titleEn: string
  narrativeZh: string
  narrativeEn: string
  cameraZh: string
  cameraEn: string
  actionZh: string
  actionEn: string
  continuityZh: string
  continuityEn: string
  soundscapeZh: string
  soundscapeEn: string
  musicZh: string
  musicEn: string
  durationMs: number
  mediaType: 'video'
  h3Mode: H3Mode
  includeAudio: boolean
  sceneId?: string
  characterIds: string[]
  propIds: string[]
  dialogue: CreativeDraftDialogueLine[]
}

export interface CreativeDraftData {
  schemaVersion: 1
  episode: CreativeDraftEpisodeCopy
  shots: CreativeDraftShot[]
}

export interface CreativeDraftValidation {
  /** Legacy values are retained so historical drafts remain readable. */
  status: 'passed' | 'failed' | 'pending' | 'ready' | 'needs_review'
  errors: string[]
  warnings: string[]
}

export type CreativeDraftReviewSeverity = 'warning' | 'risk' | 'requires_edit'
export type CreativeDraftReviewField =
  | 'titleZh' | 'titleEn'
  | 'narrativeZh' | 'narrativeEn'
  | 'actionZh' | 'actionEn'
  | 'cameraZh' | 'cameraEn'
  | 'continuityZh' | 'continuityEn'
  | 'soundscapeZh' | 'soundscapeEn'
  | 'musicZh' | 'musicEn'
  | 'durationMs' | 'h3Mode' | 'includeAudio'
  | 'sceneId' | 'characterIds' | 'propIds'
  | 'dialogueSpeakerId' | 'dialogueTextZh' | 'dialogueDeliveryZh' | 'dialogueDeliveryEn'

/** Safe, structured human-review guidance. It never carries model prose or exceptions. */
export interface CreativeDraftReviewIssue {
  code: string
  severity: CreativeDraftReviewSeverity
  path: string
  shotIndex?: number
  field?: CreativeDraftReviewField
  language?: 'zh' | 'en' | 'pair'
  suggestionZh?: string
}

export interface CreativeDraftReviewSummary {
  issueCount: number
  riskCount: number
  requiresEditCount: number
  status: 'ready' | 'needs_review'
}

export interface CreativeDraft {
  id: string
  projectId: string
  episodeId: string
  status: CreativeDraftStatus
  revision: number
  generationNo: number
  parentDraftId?: string
  contextHash: string
  contextCurrent: boolean
  creativity: CreativeDraftCreativity
  variationNote: string
  draftData: CreativeDraftData
  validation: CreativeDraftValidation
  reviewIssues: CreativeDraftReviewIssue[]
  reviewSummary: CreativeDraftReviewSummary
  warnings: string[]
  errorMessage?: string
  provider?: string
  model?: string
  runtimeMetadata?: Record<string, unknown>
  confirmedAt?: string
  appliedAt?: string
  createdAt?: string
  updatedAt?: string
  canEdit: boolean
  canRegenerate: boolean
  canConfirm: boolean
  canApply: boolean
}

export interface CreativeDraftApplyResult {
  draft: CreativeDraft
  episodeId: string
  appliedShotIds: string[]
  translationsWritten: number
}

/**
 * A single-shot, high-detail candidate.  It deliberately lives outside the
 * episode creative draft: it is authored against one persisted Shot and does
 * not alter its compact narrative/action/camera fields.
 */
export type ShotDetailDraftStatus = 'draft' | 'confirmed' | 'applied' | 'superseded' | 'failed'
export type ShotDetailDraftLanguageMode = 'chinese_only' | 'bilingual_confirmed'

export interface ShotDetailDraftData {
  schemaVersion: 1
  detailZh: string
  detailEn: string
}

export interface ShotDetailDraftValidation {
  /** Legacy values remain readable for already persisted versions. */
  status: 'passed' | 'failed' | 'pending' | 'ready' | 'needs_review'
  errors: string[]
  warnings: string[]
}

/**
 * Safe review guidance for a one-shot detail candidate.  It carries only a
 * finite issue code, a field location and an operator-facing suggestion; it
 * must never expose raw model prose, parser offsets or runtime details.
 */
export type ShotDetailDraftReviewSeverity = 'warning' | 'risk' | 'requires_edit'
export type ShotDetailDraftReviewField = 'detailZh' | 'detailEn'

export interface ShotDetailDraftReviewIssue {
  code: string
  severity: ShotDetailDraftReviewSeverity
  path: string
  field?: ShotDetailDraftReviewField
  language?: 'zh' | 'en' | 'pair'
  suggestionZh?: string
}

export interface ShotDetailDraftReviewSummary {
  issueCount: number
  riskCount: number
  requiresEditCount: number
  status: 'ready' | 'needs_review'
}

export interface ShotDetailDraft {
  id: string
  projectId: string
  shotId: string
  status: ShotDetailDraftStatus
  revision: number
  generationNo: number
  parentDraftId?: string
  contextHash: string
  contextCurrent: boolean
  variationNote: string
  draftData: ShotDetailDraftData
  validation: ShotDetailDraftValidation
  reviewIssues: ShotDetailDraftReviewIssue[]
  reviewSummary: ShotDetailDraftReviewSummary
  warnings: string[]
  errorMessage?: string
  provider?: string
  model?: string
  runtimeMetadata?: Record<string, unknown>
  createdAt?: string
  updatedAt?: string
  confirmedAt?: string
  appliedAt?: string
  canEdit: boolean
  canRegenerate: boolean
  canConfirm: boolean
  canApply: boolean
}

/** Minimal safe shot patch returned by a high-detail apply transaction. */
export interface ShotDetailDraftAppliedShot {
  id: string
  detailedDescription: string
  metadata?: Record<string, unknown>
}

export interface ShotDetailDraftApplyResult {
  draft: ShotDetailDraft
  appliedShot: ShotDetailDraftAppliedShot
  translationWritten: boolean
}

export type ShotDetailDraftBatchGenerationStatus =
  | 'queued' | 'running' | 'completed' | 'completed_with_errors' | 'failed' | 'cancelled'
export type ShotDetailDraftBatchApplyStatus = 'not_started' | 'complete'
export type ShotDetailDraftBatchItemStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'stale' | 'skipped'

export interface ShotDetailDraftBatch {
  id: string
  projectId: string
  episodeId: string
  requestId: string
  provider: PromptProviderId
  variationNote: string
  generationStatus: ShotDetailDraftBatchGenerationStatus
  applyStatus: ShotDetailDraftBatchApplyStatus
  revision: number
  totalCount: number
  pendingCount: number
  succeededCount: number
  failedCount: number
  staleCount: number
  skippedCount: number
  appliedCount: number
  policySnapshot: Record<string, unknown>
  createdAt?: string
  updatedAt?: string
  startedAt?: string
  finishedAt?: string
}

export interface ShotDetailDraftBatchBeforeSnapshot {
  targetShot: Record<string, unknown>
}

export interface ShotDetailDraftBatchItem {
  id: string
  batchId: string
  shotId: string
  ordinal: number
  beforeSnapshot: ShotDetailDraftBatchBeforeSnapshot
  draft?: ShotDetailDraft
  status: ShotDetailDraftBatchItemStatus
  attempts: number
  maxAttempts: number
  errorCode?: string
  errorMessage?: string
  createdAt?: string
  updatedAt?: string
}

export interface ShotDetailDraftBatchDetail {
  batch: ShotDetailDraftBatch
  items: ShotDetailDraftBatchItem[]
  appliedShots: ShotDetailDraftAppliedShot[]
  translationWritten: boolean
  pagination: { count: number; next?: string; previous?: string }
}

export interface ShotDetailDraftBatchApplyResult {
  batch: ShotDetailDraftBatch
  items: ShotDetailDraftBatchItem[]
  appliedShots: ShotDetailDraftAppliedShot[]
  translationWritten: boolean
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
  publishUrl?: string
  promotedAssetId?: string
  promoteToAssetUrl?: string
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
  retryAllowed?: boolean
  retryRequiresReconciliation?: boolean
  retryBlockReason?: string
  cancelAllowed?: boolean
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
  batchTaskIds?: string[]
  episodeId?: string
  episodeNumber?: number
  episodeTitle?: string
  shotId?: string
  shotIds?: string[]
  shotNumber?: number
  shotTitle?: string
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

export interface ComfySettings {
  host: string
  installPath: string
  outputPath: string
  workflowPath: string
  concurrency: number
  autoStart: boolean
  apiKey: string
}

export interface ScriptBreakdownOptions {
  episodeCountHint: number
  defaultShotDuration: number
  multiShot: boolean
  includeAudio: boolean
  defaultFirstFrameContinuity: boolean
  defaultLastFrameContinuity: boolean
  replaceExisting: boolean
  discardExistingProduction: boolean
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

export interface H3PromptOptimizerCapability {
  enabled: boolean
  configured: boolean
  transport: 'local' | 'remote' | 'unconfigured'
  model: string
  failureMode: 'warn' | 'block'
  languageGate: 'warn' | 'block'
}
