export type JsonObject = Record<string, unknown>

export interface PlatformEntity {
  id: string
  createdAt: string
  updatedAt: string
}

export interface ProviderProfile extends PlatformEntity {
  key: string
  name: string
  driverType: string
  modalities: string
  operations: string
  credentialConfigured: boolean
  driverConfig: JsonObject
  driverConfigSchemaVersion: number
  driverConfigSizeBytes: number
  driverConfigSha256: string
  capabilities: JsonObject
  isSystem: boolean
  isActive: boolean
}

export interface ModelProfileVersion extends PlatformEntity {
  profileKey: string
  version: number
  provider: string
  providerKey: string
  modelIdentifier: string
  modality: string
  specification: JsonObject
  status: string
  publishedAt: string
  publishedBy: string
  specificationSchemaVersion: number
  specificationSizeBytes: number
  specificationSha256: string
}

export interface ModelDeploymentSmokeSummary {
  id: string | null
  status: string
  operations: string[]
  capturedAt: string
  expiresAt: string
  fresh: boolean
  unresolvedSubmission: ModelDeploymentUnresolvedSmokeSubmission | null
}

export interface ModelDeploymentUnresolvedSmokeSubmission {
  id: string
  status: string
  operation: string
  requestSha256: string
  targetSha256: string
  providerRequestIdSha256: string
  submissionStartedAt: string
}

export interface ModelDeployment extends PlatformEntity {
  key: string
  name: string
  profileVersion: string
  providerProfileId: string
  providerDriver: string
  runtimeHost: string
  credentialConfigured: boolean
  latestSmoke: ModelDeploymentSmokeSummary | null
  runtimeConfig: JsonObject
  runtimeConfigSchemaVersion: number
  runtimeConfigSizeBytes: number
  runtimeConfigSha256: string
  priority: number
  isDefault: boolean
  status: string
}

export interface ModelArtifact extends PlatformEntity {
  profileVersion: string
  role: string
  logicalName: string
  filename: string
  storageUri: string
  sha256: string
  sizeBytes: number | null
  status: string
  verifiedSnapshot: string
  verifiedAt: string
  verifiedBy: string
}

export type PromptFieldValueType = 'text' | 'long_text' | 'string_list' | 'boolean' | 'number' | 'enum'

export interface PromptFieldDefinition extends PlatformEntity {
  key: string
  label: string
  category: string
  description: string
  impact: string
  valueType: PromptFieldValueType
  defaultValue: unknown
  valueOptions: string[]
  isRequired: boolean
  isActive: boolean
  displayOrder: number
}

export interface PromptProfileVersion extends PlatformEntity {
  profileKey: string
  version: number
  name: string
  description: string
  modelFamily: string
  operation: string
  mode: string
  officialReference: string
  status: string
  reviewState: string
  contractSchemaVersion: number
  contractSizeBytes: number
  contractSha256: string
  fieldRuleCount: number
  publishedAt: string
  publishedBy: string
}

export interface PromptProfileFieldRule extends PlatformEntity {
  profileVersion: string
  fieldDefinition: string
  fieldKey: string
  fieldLabel: string
  fieldValueType: PromptFieldValueType | string
  sourceSelector: string
  channel: string
  position: number
  labelStyle: string
  separator: string
  required: boolean
  omitIfEmpty: boolean
  maxChars: number
}

export interface PromptProfileBinding extends PlatformEntity {
  key: string
  name: string
  operation: string
  modelFamily: string
  mode: string
  modelProfileVersion: string
  modelIdentifier: string
  promptProfileVersion: string
  promptProfileKey: string
  promptProfileContractSha256: string
  priority: number
  isActive: boolean
}

export interface PromptProfileChoiceOption { value: string; label: string }
export interface PromptProfileModelOption {
  id: string
  profileKey: string
  version: number
  modelIdentifier: string
  modality: string
}
export interface PromptProfileOptions {
  operations: string[]
  modelFamilies: string[]
  modes: string[]
  sourceSelectors: PromptProfileChoiceOption[]
  channels: PromptProfileChoiceOption[]
  labelStyles: PromptProfileChoiceOption[]
  separators: PromptProfileChoiceOption[]
  modelProfileVersions: PromptProfileModelOption[]
}
export interface PromptProfilePreview {
  schemaVersion: number
  promptProfileVersionId: string
  profileKey: string
  version: number
  contractSha256: string
  channels: Record<string, string>
  structuredFields: JsonObject
  trace: JsonObject[]
}

export interface LegacyWorkflowTemplate extends PlatformEntity {
  name: string
  slug: string
  version: number
  provider: string
  model: string
  outputKind: string
  description: string
  isActive: boolean
  isSystem: boolean
}

export interface WorkflowSemanticBinding {
  id: string
  semanticKey: string
  nodeId: string
  inputName: string
  valueType: string
  isRequired: boolean
}

export interface WorkflowModelRequirement {
  id: string
  role: string
  alias: string
  artifactId: string
  expectedFilename: string
  expectedSha256: string
  isRequired: boolean
}

export interface WorkflowTemplateVersion extends PlatformEntity {
  template: string
  templateName: string
  templateSlug: string
  outputKind: string
  version: number
  provider: string
  modelProfileVersion: string
  status: string
  validationState: string
  validationMessage: string
  smokeTestState: string
  smokeTestMessage: string
  isCurrent: boolean
  validatedDeployment: string
  graphSha256: string
  graphSizeBytes: number
  inputContractSha256: string
  inputContractSizeBytes: number
  compilerId: string
  compilerVersion: number
  semanticBindingsSha256: string
  modelRequirementsSha256: string
  executionContractSha256: string
  validationEvidenceSha256: string
  smokeTestEvidenceSha256: string
  semanticBindingCount: number
  modelRequirementCount: number
  publishedAt: string
  publishedBy: string
  graph?: JsonObject
  inputContract?: JsonObject
  semanticBindings?: WorkflowSemanticBinding[]
  modelRequirements?: WorkflowModelRequirement[]
  validationEvidence?: JsonObject
  smokeTestEvidence?: JsonObject
}

export interface WorkflowTemplateCanvasExport {
  workflowVersionId: string
  format: 'comfyui_ui'
  filename: string
  graphSha256: string
  templateSlug: string
  templateVersion: number
  deploymentId: string
  deploymentName: string
  deploymentSource: 'validated' | 'explicit_draft'
  workflow: JsonObject
  importReady: boolean
  openReason: string
  directOpenSupported: false
  directOpenUrl: null
  openMode: 'download_import'
  downloadUrl: string
}

export interface WorkflowVersionImportInput {
  template: string
  graph: JsonObject
  inputContract?: JsonObject
  semanticBindings?: Array<Omit<WorkflowSemanticBinding, 'id'>>
  modelRequirements?: Array<Omit<WorkflowModelRequirement, 'id'>>
  provider?: string | null
  modelProfileVersion?: string | null
}

export interface NodeMetadataItem {
  classType: string
  displayName: string
  category: string
  requiredInputs: string[]
  optionalInputs: string[]
  outputCount: number
}

export interface NodeMetadataResponse {
  schemaVersion: number
  totalCount: number
  offset: number
  limit: number
  nextOffset: number | null
  catalogSha256: string
  items: NodeMetadataItem[]
}

export interface NodeMetadataCatalog {
  schemaVersion: 2
  totalCount: number
  catalogSha256: string
  items: NodeMetadataItem[]
}

export interface WorkflowSmokeAttempt extends PlatformEntity {
  workflowVersion: string
  deployment: string
  createdBy: string
  status: string
  graphSha256: string
  executionContractSha256: string
  validationEvidenceSha256: string
  targetSha256: string
  workerId: string
  submissionStartedAt: string
  submittedAt: string
  lastPolledAt: string
  nextPollAt: string
  pollCount: number
  remoteStatus: string
  hardDeadlineAt: string
  idleDeadlineAt: string
  cancelRequestedAt: string
  finishedAt: string
  errorCode: string
  errorMessage: string
}

export interface ModelDeploymentSmokeAttempt extends PlatformEntity {
  deployment: string
  operation: string
  status: string
  requestSha256: string
  targetSha256: string
  estimatedCost: string
  costCurrency: string
  providerRequestIdSha256: string
  workerId: string
  submissionStartedAt: string
  submittedAt: string
  lastPolledAt: string
  nextPollAt: string
  pollCount: number
  remoteStatus: string
  hardDeadlineAt: string
  idleDeadlineAt: string
  cancelRequestedAt: string
  errorCode: string
  errorMessage: string
  finishedAt: string
  finalSnapshot: string
}

export interface ConfigDefinition extends PlatformEntity {
  key: string
  domain: string
  valueType: string
  description: string
  unit: string
  impactModules: string[]
  restartRequired: boolean
  allowedScopes: string
  allowedDefaultSources: string
  isRequired: boolean
  isSecret: boolean
  isActive: boolean
  needsReview: boolean
  reviewReason: string
  valueSchema: JsonObject
  initialValue: unknown
}

export interface ConfigResourceOption {
  value: string
  label: string
  resource: string
  capability: string
  state: string
  reasonCode: string
  isSelectable: boolean
}

export interface ConfigValue {
  id: string
  key: string
  isSecret: boolean
  value: unknown
  credentialConfigured: boolean
  valueSha256: string
}

export interface ConfigRevision extends PlatformEntity {
  scopeType: string
  project: string
  run: string
  revision: number
  status: string
  basedOnRevision: string
  activationState: string
  activationAvailable: boolean
  activatedAt: string
  activationError: string
  valuesSha256: string
  changeSummary: string
  publishedAt: string
  values: ConfigValue[]
}

export interface SecretBinding extends PlatformEntity {
  key: string
  label: string
  purpose: string
  isActive: boolean
  configured: boolean
  currentVersion: number | null
}

export interface SecretAuditEvent {
  id: string
  binding: string
  bindingKey: string
  version: number | null
  actor: string
  actorUsername: string
  action: string
  reason: string
  createdAt: string
}

export interface PlatformLifecycleAuditEvent {
  id: string
  actor: string
  actorUsername: string
  action: string
  objectType: string
  objectId: string
  reasonCode: string
  beforeSha256: string
  afterSha256: string
  beforeSummary: JsonObject
  afterSummary: JsonObject
  createdAt: string
}

export interface ResolvedConfiguration {
  schemaVersion: number
  values: JsonObject
  sources: Record<string, { source: string; id: string; version: number; hash: string; scope: string; revision: number; revisionId: string }>
  revisionIds: string[]
  sha256: string
}

export interface RuntimeHost extends PlatformEntity {
  key: string
  name: string
  fingerprintSha256: string
  status: string
  lastSeenAt: string
}

export interface RuntimeHostProbeAttempt extends PlatformEntity {
  host: string
  createdBy: string
  trigger: string
  status: string
  workerId: string
  claimedAt: string
  heartbeatAt: string
  startedAt: string
  progressMarker: string
  cancelRequestedAt: string
  finishedAt: string
  errorCode: string
  errorMessage: string
  finalSnapshot: string
}

export interface RuntimeModelAvailability {
  id: string
  deployment: string
  artifact: string
  availabilityKey: string
  status: string
  reason: string
  observedSizeBytes: number | null
  observedSha256: string
  observationSource: string
}

export interface HostCapabilitySnapshot extends PlatformEntity {
  host: string
  revision: number
  probeStatus: string
  cpuArchitecture: string
  cpuName: string
  logicalCpuCount: number
  memoryTotalBytes: number
  diskTotalBytes: number
  diskFreeBytes: number
  gpuCount: number
  ffmpegAvailable: boolean
  ffprobeAvailable: boolean
  comfyuiVersion: string
  details: JsonObject
  capturedAt: string
  expiresAt: string
  modelAvailability: RuntimeModelAvailability[]
}

export interface RuntimeReadinessCapability {
  state: string
  reason: string
  deploymentCount: number
}

export interface RuntimeReadiness {
  schemaVersion: number
  snapshotId: string
  capturedAt: string
  capabilities: Record<string, RuntimeReadinessCapability>
}

export interface ProductionReadinessCapability {
  state: 'not_required' | 'blocked' | 'ready' | string
  required: boolean
  reasonCodes: string[]
}

export interface ProductionReadinessRecipe {
  id: string
  recipeKey: string
  version: number
  state: 'blocked' | 'ready' | string
  reasonCodes: string[]
  capabilities: Record<string, ProductionReadinessCapability>
}

export interface ProductionReadiness {
  schemaVersion: number
  status: 'unconfigured' | 'blocked' | 'ready' | string
  reasonCodes: string[]
  recipes: ProductionReadinessRecipe[]
}

export interface RecommendationSnapshot extends PlatformEntity {
  hostSnapshot: string
  project: string
  configRevision: string
  inputSha256: string
  status: string
  reason: string
  recommendation: JsonObject
  candidates: RecommendationCandidate[]
  decisions: RecommendationDecision[]
}

export interface RecommendationCandidate extends PlatformEntity {
  capabilityKey: string
  deployment: string
  deploymentKey: string
  workflowVersion: string
  status: 'recommended' | 'compatible' | 'smoke_test_required' | 'unavailable' | string
  rank: number | null
  reasonCode: string
  suggestedValues: JsonObject
  suggestedValuesSha256: string
}

export interface RecommendationDecision extends PlatformEntity {
  recommendationSnapshot: string
  candidate: string
  decision: 'accept' | 'override' | 'reject' | string
  scopeType: string
  project: string
  configRevision: string
  recipeVersion: string
  overrideValues: JsonObject
  reason: string
}

export interface ScriptSource extends PlatformEntity {
  project: string
  sourceType: string
  sourceFormat: string
  status: string
  originalName: string
  mimeType: string
  fileSizeBytes: number | null
  fileSha256: string
  validationMessage: string
  latestRevisionId: string
}

export interface ScriptRevision extends PlatformEntity {
  source: string
  sourceStatus: string
  revision: number
  contentSizeBytes: number
  contentSha256: string
  status: string
  confirmedAt: string
  content?: string
}

export interface ProductionRecipe extends PlatformEntity {
  recipeKey: string
  version: number
  name: string
  definition: JsonObject
  status: string
  publishedAt: string
  publishedBy: string
  definitionSchemaVersion: number
  definitionSizeBytes: number
  definitionSha256: string
}

export interface ProductionRecipeMediaRouteOption {
  routeIndex: number
  workflowVersionId: string
  workflowLabel: string
  modelDeploymentId: string
  deploymentLabel: string
  modelIdentifier: string
  driverType: string
  outputKinds: string[]
  h3Modes: string[]
  mediaOperations: string[]
  isSelectable: boolean
  reasonCodes: string[]
  reasons: string[]
}

export interface ProductionRecipeMediaRouteOptions {
  schemaVersion: 1
  recipeVersionId: string
  recipeDefinitionSha256: string
  routes: ProductionRecipeMediaRouteOption[]
}

export interface StageRun extends PlatformEntity {
  stageKey: string
  ordinal: number
  mode: string
  status: string
  approvalRequired: boolean
  attemptCount: number
  inputReference: string
  inputSnapshot: JsonObject
  inputSnapshotSha256: string
  outputReference: string
  outputSnapshot: JsonObject
  outputSnapshotSha256: string
  reasonCode: string
  reason: string
  routing: JsonObject
  routingSha256: string
  routingDecision: JsonObject
  routingDecisionSha256: string
  selectedRouteIndex: number | null
  approvedEstimatedCost: string
  approvedCostCurrency: string
  selectionConfirmedAt: string
  startedAt: string
  finishedAt: string
  approvalDecisions: string[]
}

export type ProductionRecipeScriptMode = 'ai' | 'confirmed_revision' | 'downstream'
export type ProductionRecipeScriptClassification = ProductionRecipeScriptMode | 'invalid'

export interface GeneratedArtifact extends PlatformEntity {
  manifestKey: string
  productionRun: string
  stageRun: string
  generationAttempt: string
  generationTask: string
  asset: string
  kind: string
  role: string
  variantGroup: string
  variantIndex: number
  status: string
  storageBackend: string
  objectKey: string
  mimeType: string
  sizeBytes: number | null
  sha256: string
  manifestMetadataSha256: string
  previewUrl: string
  downloadUrl: string
  comparisonParameters: JsonObject
  manifestMetadata?: JsonObject
}

export interface ProductionRun extends PlatformEntity {
  project: string
  projectTitle: string
  scriptRevision: string
  recipeVersion: string
  idempotencyKey: string
  requestSha256: string
  status: string
  currentStageKey: string
  blockedCode: string
  blockedReason: string
  allowExternalCalls: boolean
  budgetLimit: string
  costCurrency: string
  estimatedCostTotal: string
  committedCostTotal: string
  actualCostTotal: string
  releaseBundleId: string
  maxGenerationTasks: number
  generationTaskCount: number
  configSnapshotSha256: string
  inputSnapshotSha256: string
  startedAt: string
  finishedAt: string
  configRevision?: string
  configSnapshot?: JsonObject
  inputSnapshot?: JsonObject
  stages?: StageRun[]
  artifacts?: GeneratedArtifact[]
}

export interface ProductionRunCreateInput {
  projectId: string
  scriptMode: ProductionRecipeScriptMode
  scriptRevisionId?: string | null
  recipeVersionId: string
  idempotencyKey: string
  allowExternalCalls?: boolean | null
  budgetLimit?: string | null
  maxGenerationTasks?: number | null
  configOverrides?: JsonObject
  idea?: string
  styleHint?: string
  episodeCount?: number
  shotsPerEpisode?: number | null
  episodeDurationSeconds?: number | null
  episodeIds?: string[]
  workflowSelection?: ProductionWorkflowSelectionInput | null
}

export interface ProductionWorkflowShotRouteInput {
  shotId: string
  routeIndex: number
}

export type ProductionWorkflowSelectionInput =
  | {
      schemaVersion: 1
      mode: 'uniform'
      routeIndex: number
      shotRoutes?: never
    }
  | {
      schemaVersion: 1
      mode: 'per_shot'
      routeIndex?: never
      shotRoutes: ProductionWorkflowShotRouteInput[]
    }

export interface GenerationAttempt extends PlatformEntity {
  stageRun: string
  unitKey: string
  attemptNumber: number
  modelDeployment: string
  generationTask: string
  status: string
  routeIndex: number | null
  operation: string
  isExternal: boolean
  providerRequestIdSha256: string
  providerRequestIdSuffix: string
  requestSha256: string
  requestSnapshotSchemaVersion: number
  requestSnapshotSizeBytes: number
  requestSnapshotSha256: string
  resultSha256: string
  estimatedCost: string
  committedCost: string
  actualCost: string
  costCurrency: string
  errorCode: string
  remoteStatus: string
  submissionStartedAt: string
  submittedAt: string
  lastPolledAt: string
  pollCount: number
  hardDeadlineAt: string
  idleDeadlineAt: string
  startedAt: string
  finishedAt: string
}

export type UncertainResolutionOutcome =
  | 'confirmed_not_submitted'
  | 'confirmed_remote_submitted'
  | 'confirmed_remote_failed'
  | 'actual_cost_confirmed'

export interface ResolveUncertainAttemptInput {
  attemptId: string
  outcome: UncertainResolutionOutcome
  note: string
  providerRequestId?: string
  actualCost?: string | null
  costCurrency?: string
}

export type SmokeResolutionOutcome =
  | 'confirmed_not_submitted'
  | 'confirmed_remote_submitted'
  | 'confirmed_remote_failed'

export interface PromptNormalizationSmokeResolution {
  id: string
  smokeAttemptId: string
  smokeAttemptStatus: string
  revision: number
  outcome: SmokeResolutionOutcome
  requestSha256: string
  targetSha256: string
  providerRequestIdSha256: string
  evidenceSha256: string
  note: string
  decidedBy: string
  createdAt: string
}

export interface ArtifactPreview {
  taskId: string
  outputIndex: number
  kind: string
  filename: string
  previewUrl: string
  downloadUrl: string
  parameters: JsonObject
  backendStatus: string
}
