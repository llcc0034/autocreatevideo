import type { ConditioningModeCapability } from './conditioningCapabilities'
import type { RetryContext } from './productionRetryPreview'

export type JsonObject = Record<string, unknown>

export type RegistryDeletionResourceType =
  | 'provider_profile'
  | 'model_profile_version'
  | 'model_deployment'
  | 'model_execution_instance'
  | 'model_execution_instance_version'
  | 'model_artifact'
  | 'workflow_template_version'
  | 'production_recipe_version'
  | 'config_definition'
  | 'field_definition'
  | 'prompt_profile_version'
  | 'prompt_profile_field_rule'

export type RegistryDeletionDisposition =
  | 'selectable_delete'
  | 'automatic_delete'
  | 'blocking_reference'
  | 'preserved_history'

export interface RegistryDeletionTarget {
  resourceType: RegistryDeletionResourceType
  objectId: string
  label: string
  status: string
  stateSha256: string
}

export interface RegistryDeletionDependency {
  dependencyKey: string
  resourceType: string
  objectId: string
  label: string
  status: string
  relationship: string
  relationshipLabel: string
  disposition: RegistryDeletionDisposition
  reasonCode: string
  reason: string
  referencePaths: string[]
  stateSha256: string
}

export interface RegistryDeletionImpact {
  schemaVersion: 1
  requestedAction: 'delete'
  target: RegistryDeletionTarget
  targetPolicy: { allowed: boolean; reasonCode: string; reason: string }
  complete: boolean
  canExecute: boolean
  dependencies: RegistryDeletionDependency[]
  summary: {
    selectableCount: number
    automaticCount: number
    blockingCount: number
    preservedHistoryCount: number
  }
  confirmationPhrase: string
  advisories: string[]
  impactSha256: string
}

export interface RegistryDeletionExecuteInput {
  expectedImpactSha256: string
  selectedDependencyKeys: string[]
  confirmationPhrase: string
  reasonCode: string
}

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

export interface ModelProfileRegistryChoice {
  value: string
  label: string
}

export interface FormCatalogOption {
  value: string
  label: string
  disabled?: boolean
  hint?: string
}

export interface ModelProfileProviderDriverOption extends ModelProfileRegistryChoice {
  modalities: string[]
  operations: string[]
}

export interface ModelProfileCapabilityOption extends ModelProfileRegistryChoice {
  modalities: string[]
  driverTypes: string[]
  legacy: boolean
}

export interface AsyncMediaEditorOptions {
  schemaVersion: 1
  maximumStringLength: number
  description: string
  fields: { key: string; label: string; types: string[]; sourceDescription: string }[]
  operations: { key: string; outputKind: string }[]
}

export interface ModelProfileRegistryOptions {
  asyncMedia: AsyncMediaEditorOptions
  scriptContracts: ScriptContractEditorOptions
  visualRulesOutput: VisualRulesOutputOptions
  executionMonitoring: { maxSeconds: number; maxOverrides: number }
  mediaConstraints: MediaConstraintsEditorOptions
  textExecution: TextExecutionEditorOptions
  chatRequestOptions: ChatRequestOptionsEditorOptions
  conditioningContract: ConditioningContractEditorOptions
  specificationSchemaVersion: number
  providerDrivers: ModelProfileProviderDriverOption[]
  modalities: ModelProfileRegistryChoice[]
  artifactRoles: ModelProfileRegistryChoice[]
  capabilities: ModelProfileCapabilityOption[]
  modelFamilies: string[]
}

export interface ScriptContractPolicyOption {
  key: string
  operation: string
  label: string
  numberFields: {key:string;label:string;unit:string;integer:boolean;minimum:number;maximum:number|null;exclusiveMinimum:boolean}[]
}
export interface ScriptContractEditorOptions {
  schemaVersion: number
  description: string
  policies: ScriptContractPolicyOption[]
}

export interface VisualRulesOutputOptions {
  schemaVersion: number
  operation: string
  responseSchemaVersion: number
  responseExample: JsonObject
  numberFields: {key:string;label:string;unit:string;minimum:number;maximum:number}[]
  numberPairs: [string,string][]
  clausesMaxBytes: number
  colorChoices: Record<string,(string|boolean)[]>
  outputs: {sourceSelector:string;field:string;label:string;responseField:string;sourceFormat?:string;transformation:string}[]
}

export interface MediaConstraintsEditorOptions {
  schemaVersion: number
  resolutionFields: {key: string; label: string; minimum: number; maximum: number; unit: string}[]
  resolutionPairs: [string, string][]
  fps: {minimum: number; maximum: number; maximumItems: number}
  durationMs: {minimum: number; maximum: number}
}

export interface ChatRequestOptionsEditorOptions {
  schemaVersion: number
  temperatureMinimum: number
  responseFormatTypes: ModelProfileRegistryChoice[]
  assemblyDescription: string
  boundaryDescription: string
}

export interface TextExecutionEditorOptions {
  schemaVersion: number
  integerConstraints: { valueFields: ModelProfileRegistryChoice[]; seedPolicies: ModelProfileRegistryChoice[] }
  operations: ModelProfileRegistryChoice[]
  parameters: (ModelProfileRegistryChoice & {
    description: string; required: boolean; requireMaximum: boolean; template: JsonObject
  })[]
}

export interface ConditioningContractEditorOptions {
  schemaVersion: number
  adapterKey: string
  inputKinds: ModelProfileRegistryChoice[]
  inputSlots: ModelProfileRegistryChoice[]
  maximumItems: number
  maximumModes: number
  identifierPattern: string
  modeTemplate: JsonObject
  dynamicReferenceTopology: ReferenceTopologyEditorOptions
}

export interface ReferenceTopologyEditorOptions {
  schema_version: number
  maximum_rules: number
  maximum_duration_ms: number
  maximum_fps: number
  label_max_characters: number
  text_max_characters: number
  maximum_targets_per_source: number
  media_groups: {value: string; label: string; media_kind: string; input_slot: string}[]
  preprocessing_slots: {value: string; label: string; input_slot: string; include_target_fps: boolean;
    adapters: {value: string; label: string; description: string; target_fps: 'required' | 'forbidden' | 'not_applicable'}[]}[]
  pairing_direction: {source_media_kind: string; target_media_kind: string}
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

export type ModelDeploymentStatus = 'active' | 'disabled' | 'error' | 'retired'

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
  status: ModelDeploymentStatus
}

export type AcceptanceSmokeSubject = 'workflow' | 'deployment'

export interface AcceptanceSmokeDeploymentRef {
  id: string
  key: string
  name: string
  status: string
  profileVersionId: string
  providerId: string
  providerDriver: string
  runtimeHostId: string | null
}

export interface AcceptanceSmokeDeploymentSummary extends AcceptanceSmokeDeploymentRef {
  currentTargetSha256: string
  profileContractState: 'current' | 'invalid'
  profileContractReasonCode: string
  targetState: 'current' | 'invalid'
  targetReasonCode: string
}

export interface AcceptanceSmokeWorkflowRef {
  id: string
  templateId: string
  templateName: string
  templateSlug: string
  version: number
  status: string
  isCurrent: boolean
  outputKind: string
  modelProfileVersionId: string | null
  providerId: string | null
}

export interface AcceptanceSmokeAction {
  kind: string
  method: 'GET' | 'POST'
  url: string
  uiUrl: string
  enabled: boolean
  reasonCode: string
  requiresFixture: boolean
  requiresConfirmation: boolean
  mayCallExternalService: boolean
}

export interface AcceptanceSmokeWorkflowAttempt {
  id: string
  deploymentId: string
  status: string
  targetSha256: string
  graphSha256: string
  createdAt: string
  startedAt: string | null
  finishedAt: string | null
  errorCode: string
  resolutionRequired: boolean
}

export interface AcceptanceSmokeDeploymentAttempt {
  promptProfile: JsonObject | null
  id: string
  operation: string
  status: string
  requestSha256: string
  targetSha256: string
  errorCode: string
  resolutionRequired: boolean
  createdAt: string
  submissionStartedAt: string | null
  finishedAt: string | null
}

export interface AcceptanceSmokeEvidenceSummary {
  storedState: string
  state: string
  fresh: boolean
  reasonCode: string
  message: string
  evidenceSha256: string
}

export interface AcceptanceSmokeValidationSummary extends AcceptanceSmokeEvidenceSummary {
  storedTargetSha256: string
  currentTargetSha256: string
  validatedDeploymentId: string | null
}

export interface AcceptanceSmokeWorkflowSummary extends AcceptanceSmokeEvidenceSummary {
  evidenceDeploymentId: string | null
  latestAttempt: AcceptanceSmokeWorkflowAttempt | null
}

export interface AcceptanceSmokeAssociation {
  level: string
  deploymentId: string | null
  profileMatches: boolean
  validatedDeploymentMatches: boolean
  hasSmokeAttempt: boolean
}

export interface AcceptanceSmokeWorkflowRow {
  subject: 'workflow'
  workflow: AcceptanceSmokeWorkflowRef
  deployment: AcceptanceSmokeDeploymentRef | null
  association: AcceptanceSmokeAssociation
  validation: AcceptanceSmokeValidationSummary
  smoke: AcceptanceSmokeWorkflowSummary
  actions: AcceptanceSmokeAction[]
}

export interface AcceptanceSmokeSnapshot {
  promptProfile: JsonObject | null
  id: string
  probeKind: string
  status: string
  operations: string[]
  targetSha256: string
  targetMatches: boolean
  fresh: boolean
  capturedAt: string
  expiresAt: string
}

export interface AcceptanceSmokeDeploymentOperation {
  promptProfiles: {
    promptProfile: JsonObject
    evidenceState: string
    latestSnapshot: AcceptanceSmokeSnapshot | null
    freshPassedSnapshot: AcceptanceSmokeSnapshot | null
  }[]
  operation: string
  applicable: boolean
  evidenceState: string
  latestSnapshot: AcceptanceSmokeSnapshot | null
  freshPassedSnapshot: AcceptanceSmokeSnapshot | null
  latestAttempt: AcceptanceSmokeDeploymentAttempt | null
  action: AcceptanceSmokeAction
}

export interface AcceptanceSmokeDeploymentSmokeSummary {
  applicable: boolean
  reasonCode: string
  operations: AcceptanceSmokeDeploymentOperation[]
}

export interface AcceptanceSmokeWorkflowAssociationSummary {
  profileMatchCount: number
  compatibleOnlyCount: number
  validatedCount: number
  smokeHistoryCount: number
  freshValidationCount: number
  driftedValidationCount: number
  freshSmokeCount: number
  unresolvedSmokeCount: number
}

export interface AcceptanceSmokeDeploymentRow {
  subject: 'deployment'
  deployment: AcceptanceSmokeDeploymentSummary
  health: AcceptanceSmokeSnapshot | null
  deploymentSmoke: AcceptanceSmokeDeploymentSmokeSummary
  workflowAssociationSummary: AcceptanceSmokeWorkflowAssociationSummary
  actions: AcceptanceSmokeAction[]
}

export type AcceptanceSmokeRow = AcceptanceSmokeWorkflowRow | AcceptanceSmokeDeploymentRow

export interface AcceptanceSmokePage {
  count: number
  next: string | null
  previous: string | null
  results: AcceptanceSmokeRow[]
}

export interface AcceptanceSmokeQuery {
  subject: AcceptanceSmokeSubject
  page: number
  pageSize: number
  search?: string
  deploymentId?: string
  workflowId?: string
}

export interface ModelArtifact extends PlatformEntity {
  profileVersion: string
  role: string
  logicalName: string
  filename: string
  storageUri: string
  inventoryFolder: string
  inventoryNodeType: string
  inventoryInputName: string
  sha256: string
  sizeBytes: number | null
  status: string
  verifiedSnapshot: string
  verifiedAt: string
  verifiedBy: string
}

export type PromptFieldValueType = import('./fieldDefinitionVersions').FieldDefinitionVersionSummary['value_type']
export interface PromptProfileChannelLimit {
  maxChars: number
  warnAtChars: number | null
}

export interface FieldDefinition extends PlatformEntity {
  key: string
  isActive: boolean
  displayOrder: number
}

export interface PromptProfileVersion extends PlatformEntity {
  inputStructureVersion: string | null
  profileKey: string
  version: number
  name: string
  description: string
  modelFamily: string
  operation: string
  mode: string
  sourceProjectionKey: string
  sourceProjectionConfig: JsonObject
  officialReference: string
  channelLimits: Record<string, PromptProfileChannelLimit>
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
  sourcePath: (string | number)[] | null
  fieldDefinitionVersion: string | null
  sourceVersion?: string | null
  sourceDefinition?: JsonObject | null
  profileVersion: string
  fieldDefinition: string
  fieldKey: string
  fieldLabel: string
  fieldValueType: PromptFieldValueType | string
  fixedValue: unknown
  hasDefault: boolean
  defaultValue: unknown
  sourceSelector: string
  projectionPrimitive: string
  channel: string
  position: number
  labelStyle: string
  separator: string
  required: boolean
  omitIfEmpty: boolean
  maxChars: number
  generationMode: string
  generationSourceSelector: string
  generationContextSelectors: string[]
  generationInstruction: string
  generationOutputSemantics: string
  generationRequired: boolean
  languagePolicy: string
  lengthUnit: string
  generationMin: number | null
  generationMax: number | null
  generationValidatorKeys: string[]
}


export interface PromptProfileChoiceOption { value: string; label: string }
export interface PromptSourceOption extends PromptProfileChoiceOption {
  groupLabel: string
  description: string
  recordLocation: string
  executionUsage: string
}
export interface PromptProfileContextSelectorOption extends PromptProfileChoiceOption { description: string }
export interface PromptProfileGenerationSourceOption extends PromptProfileChoiceOption {
  scopeType: string
  group: string
  groupLabel: string
  description: string
  availability: string
  scope: string
  dialogueExposure: string
  crossShotExposure: string
  cardinality: string
  status: string
  generationModes: string[]
}
export interface PromptProfileProjectionPrimitiveOption extends PromptProfileContextSelectorOption {
  engineKey: string
  renderingTemplateKeys: string[]
  contributionSourceSelectors: string[]
}
export interface PromptProfileModelOption {
  id: string
  profileKey: string
  version: number
  modelIdentifier: string
  modality: string
}
export interface ReferenceRenderingOption {
  key: string
  label: string
  placeholders: string[]
  description: string
  assembly: string
  requirement: string
  placeholderHelp: Record<string, { label: string; description: string; example: string }>
  example: { template: string; result: string }
}
export interface PromptProfileOptions {
  operations: string[]
  modelFamilies: string[]
  modes: string[]
  customEntryFields: string[]
  ruleSystemDefaults: JsonObject
  sourceSelectors: PromptSourceOption[]
  channels: PromptProfileChoiceOption[]
  labelStyles: PromptProfileChoiceOption[]
  separators: PromptProfileChoiceOption[]
  generationModes: PromptProfileChoiceOption[]
  generationLeafOperations: string[]
  generationSourceSelectors: PromptProfileGenerationSourceOption[]
  generationContextSelectors: PromptProfileContextSelectorOption[]
  generationOutputSemantics: PromptProfileChoiceOption[]
  sourceProjectionKeys: PromptProfileChoiceOption[]
  sourceProjectionPrimitives: PromptProfileProjectionPrimitiveOption[]
  referenceRenderingFields: ReferenceRenderingOption[]
  referenceRenderingTextSlots: PromptProfileChoiceOption[]
  languagePolicies: PromptProfileChoiceOption[]
  lengthUnits: PromptProfileChoiceOption[]
  generationValidators: PromptProfileChoiceOption[]
  modelProfileVersions: PromptProfileModelOption[]
}
export interface PromptProfilePreview {
  schemaVersion: number
  promptProfileVersionId: string
  profileKey: string
  version: number
  contractSha256: string
  channels: Record<string, string>
  channelLimits: Record<string, PromptProfileChannelLimit>
  channelAudit: Record<string, JsonObject>
  warnings: string[]
  structuredFields: JsonObject
  trace: JsonObject[]
  sourceProjection: JsonObject
  ephemeral: boolean
  previewScope: string
  baseContractSha256: string
  executedStages: Record<string, boolean>
}
export interface PromptProfilePreviewRequest {
  sources: JsonObject
  expectedBaseContractSha256?: string
  candidate?: JsonObject
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

export interface WorkflowInputParameter {
  semanticKey: string
  type: string
  required: boolean
  min: number | null
  max: number | null
  multipleOf: number | null
  alignmentModulus: number | null
  alignmentRemainder: number | null
  minLength?: number | null
  maxLength?: number | null
  options: ComfyWorkflowParameterScalar[]
  selectionGroup: string
  recommendedValue: ComfyWorkflowParameterScalar | null
}

export interface WorkflowInputAssetSlot {
  semanticKey: string
  inputSlot: number
  mediaKind: 'image' | 'video' | 'audio'
  category: string
  minItems: number
  maxItems: number
  required: boolean
}

export interface WorkflowDynamicReferencesSchema {
  mode: string
  conditioningMode: ConditioningModeCapability
  pictures: {
    minItems: number
    maxItems: number
    categories: Array<'character' | 'scene' | 'prop' | 'continuity_frame'>
  }
  videos: {
    minItems: number
    maxItems: number
    purposes: string[]
    defaultPurposeKey: string
    supportsEmbeddedAudio: boolean
    supportsExternalAudio: boolean
  }
  audios: {
    minItems: number
    maxItems: number
    purposes: string[]
    defaultPurposeKey: string
  }
  maximumPhysicalAssets: number
}

export interface WorkflowAssetGroup {
  key: string
  mediaKind: 'image' | 'video' | 'audio'
  minItems: number
  maxItems: number
  semanticKeys: string[]
  wiring: 'ordered_ports' | 'reduce'
}

export interface WorkflowInputSchema {
  schemaVersion: 2
  available: boolean
  reasonCode: string
  parameters: WorkflowInputParameter[]
  assetSlots: WorkflowInputAssetSlot[]
  assetGroups?: WorkflowAssetGroup[]
  dynamicReferences: WorkflowDynamicReferencesSchema | null
  dynamicReferenceModes: Record<string, WorkflowDynamicReferencesSchema>
}

export type WorkflowTemplateVersionStatus = 'draft' | 'validated' | 'published' | 'retired'
export type WorkflowTemplateVersionValidationState = 'not_run' | 'passed' | 'failed'

export interface WorkflowTemplateVersion extends PlatformEntity {
  configurationLinks?: WorkflowConfigurationLinks
  template: string
  templateName: string
  templateSlug: string
  outputKind: string
  version: number
  provider: string
  modelProfileVersion: string
  status: WorkflowTemplateVersionStatus
  validationState: WorkflowTemplateVersionValidationState
  validationMessage: string
  smokeTestState: WorkflowTemplateVersionValidationState
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
  workflowInputSchema?: WorkflowInputSchema
  workflowInputSchemaError?: string
}

export interface WorkflowConfigurationLinks {
  model: { id: string; key: string; version: number; status: string; instanceCount: number } | null
  validatedDeployment: { id: string; name: string; status: string; modelProfileVersionId: string } | null
  executionInstances: {
    total: number
    activeTotal: number
    preview: {
      id: string; name: string; version: number; status: string
      operation: string; mode: string; promptMode: 'profile' | 'manual' | 'none'
    }[]
  }
  projector: {
    state: 'linked' | 'not_recorded' | 'invalid'; versionId: string | null; name: string; key: string
    version: number | null; status: string; isCurrent: boolean; isActive: boolean; definitionSha256: string
  }
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

export interface WorkflowProjector {
  id: string
  key: string
  name: string
  description: string
  isActive: boolean
  createdAt: string
  updatedAt: string
}

export interface WorkflowProjectorVersion {
  id: string
  projector: string
  projectorKey: string
  version: number
  engineKey: string
  engineVersion: number
  providerDriverType: string
  operation: string
  outputKind: 'image' | 'video' | 'audio' | 'text' | ''
  definition: JsonObject
  definitionSchemaVersion: number
  definitionSizeBytes: number
  definitionSha256: string
  status: 'draft' | 'published' | 'retired'
  isCurrent: boolean
  publishedAt: string
  publishedBy: string
  createdAt: string
  updatedAt: string
}

export interface WorkflowProjectorChoiceOption {
  value: string
  label: string
}

export interface WorkflowProjectorEditorField {
  path: string
  label: string
  help: string
  control: string
  required: boolean
  readOnly: boolean
}

export interface WorkflowProjectorEngineOption {
  key: string
  version: number
  label: string
  description: string
  projectionKind: string
  adapterContract: {
    compiler: string
    output: string
    dynamic: string
  }
  lockedFields: string[]
  projectionFields: WorkflowProjectorEditorField[]
  creationTemplate: {
    providerDriverType: string
    operation: string
    outputKind: WorkflowProjectorVersion['outputKind']
    definition: JsonObject
  }
}

export interface WorkflowResolutionSize { width: number; height: number }
export interface WorkflowResolutionOptions {
  nativeAdapter: string
  configuredAdapter: string
  algorithm: string
  modes: WorkflowProjectorChoiceOption[]
  nativeSizes: WorkflowResolutionSize[]
  presets: Array<WorkflowResolutionSize & { label: string }>
  limits: { minDimension: number; maxDimension: number; multiple: number; maxPixels: number; maxCandidates: number }
}

export interface WorkflowProjectorOptions {
  imageResolution: WorkflowResolutionOptions
  definitionSchemaVersion: number
  editorSchemaVersion: number
  engines: WorkflowProjectorEngineOption[]
  providerDriverTypes: WorkflowProjectorChoiceOption[]
  operations: WorkflowProjectorChoiceOption[]
  outputKinds: WorkflowProjectorChoiceOption[]
  adapters: {
    compiler: WorkflowProjectorChoiceOption[]
    output: WorkflowProjectorChoiceOption[]
    dynamic: WorkflowProjectorChoiceOption[]
  }
  definitionEditor: {
    sections: Array<{
      key: string
      label: string
      description: string
      fields: WorkflowProjectorEditorField[]
    }>
    choices: {
      executionCapabilities: WorkflowProjectorChoiceOption[]
      cardinalities: WorkflowProjectorChoiceOption[]
      parameterPolicies: WorkflowProjectorChoiceOption[]
      artifactRoles: WorkflowProjectorChoiceOption[]
      mediaKinds: WorkflowProjectorChoiceOption[]
      outputKinds: WorkflowProjectorChoiceOption[]
      contractSources: WorkflowProjectorChoiceOption[]
      assetGroupWirings?: WorkflowProjectorChoiceOption[]
      modelQuantityInputs: Array<WorkflowProjectorChoiceOption & { mediaKind: 'image' | 'video' | 'audio' }>
      roleSelectors: WorkflowProjectorChoiceOption[]
      scalarTypes: WorkflowProjectorChoiceOption[]
    }
    limits: {
      priorityMin: number
      priorityMax: number
      nodeCountMax: number
      inputEqualsMaxItems: number
      modelSelectorsMaxItems: number
      requiredEdgesMaxItems: number
      parameterPoliciesMaxItems: number
      assetSlotsMaxItems: number
    }
  }
}

export interface WorkflowProjectorRecommendation {
  schemaVersion: number
  persistence: 'unsaved_draft'
  requiresHumanConfirmation: true
  engineKey: string
  engineVersion: number
  providerDriverType: string
  operation: string
  outputKind: 'image' | 'video' | 'audio' | 'text'
  definition: JsonObject
  sourceEvidence: {
    candidateSourceId: string
    sourceScopeSchemaVersion: number
    sourceScopeSha256: string
    sourceSha256: string
    sourcePathSha256: string
    objectInfoSchemaSha256: string
    convertedGraphSha256: string
    primaryCanvasNodeId: string
    primaryApiNodeId: string
    primaryNodeType: string
  }
  catalog: {
    nodeTypes: Array<{
      value: string
      count: number
      primaryOutput: boolean
    }>
    inputsByNodeType: Record<string, string[]>
    observedEdges: Array<{
      fromNodeType: string
      outputIndex: number
      toNodeType: string
      inputName: string
      recommended: boolean
    }>
    parameterTargets: Array<{
      semanticKey: string
      nodeType: string
      inputName: string
      valueType: string
    }>
  }
  warnings: string[]
  recommendationSha256: string
}

export interface ComfySavedWorkflowItem {
  sourceScopeSchemaVersion: number
  sourceScopeSha256: string
  sourceKey: string
  sourceDeploymentId: string
  sourceProviderId: string
  sourceRuntimeHostId: string
  sourcePath: string
  sourceSha256: string
  sizeBytes: number
  sourceWorkflowId: string
  suggestedImportKind: 'template_version' | 'execution_snapshot' | ''
  candidateManaged: boolean
  candidateSourceId: string
  imported: boolean
  sourceId: string
  workflowVersionId: string
  templateId: string
  contractUpgradeRequired: boolean
  errorCode: string
  errorMessage: string
  analysisStatus: 'complete' | 'blocked' | 'failed'
  analysisCode: string
  analysisMessage: string
  admissionTier: 'candidate' | 'generic_fixed' | 'projected_dynamic'
  actionability: 'candidate_only' | 'ready_for_executable_import'
  recoveryActions: Array<{ code: string; label?: string }>
  matchStatus: 'matched' | 'ambiguous' | 'unmatched' | 'blocked'
  matchReasonCodes: string[]
  matchActions: Array<{ code: string }>
  outputCandidates: ComfyWorkflowOutputCandidate[]
  modelProfileCandidates: Array<{
    modelProfileVersionId: string
    profileKey: string
    version: number
    status: 'draft' | 'published' | 'retired'
    modality: 'text' | 'image' | 'video' | 'audio' | 'multimodal'
    targetDeploymentId: string
    eligible: boolean
    liveValidateReady: boolean
    eligibleOutputNodeIds: string[]
    rejectionCodes: string[]
    liveValidateRejectionCodes: string[]
  }>
  matchedModelProfileVersionId: string
  matchedTargetDeploymentId: string
  matchedPrimaryOutputNodeId: string
  matchedOutputKind: 'image' | 'video' | 'audio' | 'text' | ''
  suggestedTemplate: {
    useExisting: boolean
    templateId: string
    name: string
    slug: string
    outputKind: 'image' | 'video' | 'audio' | 'text'
  } | null
  preview: ComfySavedWorkflowPreview | null
}

export interface ComfySavedWorkflowSourceResult {
  sourceScopeSchemaVersion: number
  sourceScopeSha256: string
  sourceDeploymentId: string
  providerId: string
  runtimeHostId: string
  sourceLabel: string
  deploymentCount: number
  status: 'succeeded' | 'failed'
  itemCount: number
  errorCode: string
  errorMessage: string
}

export interface ComfySavedWorkflowScan {
  schemaVersion: number
  status: 'complete' | 'partial' | 'failed'
  sourceCount: number
  succeededSourceCount: number
  failedSourceCount: number
  matchedCount: number
  reviewCount: number
  effectiveScanMaxItems: number | null
  effectiveScanMaxSeconds: number | null
  effectiveRequestMaxBytes: number | null
  effectiveBatchMaxItems: number | null
  effectiveBatchMaxSeconds: number | null
  savedWorkflowDirectory: string
  sources: ComfySavedWorkflowSourceResult[]
  items: ComfySavedWorkflowItem[]
}

export interface ComfyWorkflowOutputCandidate {
  canvasNodeId: string
  nodeType: string
  suggestedOutputKind: '' | 'image' | 'video' | 'audio' | 'text'
}

export interface ComfyWorkflowModelDependency {
  nodeId: string
  nodeType: string
  inputName: string
  filename: string
  role: string
  suggestedAlias: string
  inventoryFolder: string
  inventoryNodeType: string
  inventoryInputName: string
  availableInObjectInfo: boolean
  artifactMatches: Array<{ id: string; status: string; sha256: string; sizeBytes: number | null; verifiedSnapshot: string }>
}

export interface ComfyWorkflowAssetDependency {
  nodeId: string
  nodeType: string
  inputName: string
  mediaKind: 'image' | 'video' | 'audio'
  valueType: 'asset_image' | 'asset_video' | 'asset_audio'
  suggestedSemanticKey: string
  inputSlot: number
  assetCategory: 'image' | 'video' | 'audio'
  isRequired: boolean
  minItems: number
  maxItems: number
}

export type ComfyWorkflowParameterScalar = string | number | boolean

export interface ComfyWorkflowConfigurableParameter {
  semanticKey: string
  sourceValue: ComfyWorkflowParameterScalar
  recommendedSafeValue: ComfyWorkflowParameterScalar
  defaultValue: ComfyWorkflowParameterScalar | null
  min: number | null
  max: number | null
  schemaMin: number | null
  schemaMax: number | null
  multipleOf: number | null
  alignmentModulus: number | null
  alignmentRemainder: number | null
  options: ComfyWorkflowParameterScalar[]
  type: 'integer' | 'number' | 'enum'
  selectionGroup: string
  target: { nodeId: string; nodeType: string; inputName: string }
}

export interface ComfySavedWorkflowPreview {
  requestReceipt?: {
    requestKey: string
    candidateSourceId: string
    request: { deploymentId: string; sourcePath: string; expectedSha256: string; expectedSourceScopeSchemaVersion: number;
      expectedSourceScopeSha256: string; primaryOutputNodeIds: string[]; modelProfileVersion: string | null }
  }
  schemaVersion: number
  sourcePath: string
  sourceSha256: string
  sizeBytes: number
  sourceWorkflowId: string
  primaryOutputNodeIds: string[]
  primaryApiNodeIds: string[]
  outputCandidates: ComfyWorkflowOutputCandidate[]
  modelDependencies: ComfyWorkflowModelDependency[]
  assetDependencies: ComfyWorkflowAssetDependency[]
  conversionReport: JsonObject
  objectInfoSchemaSha256: string
  convertedGraphSha256: string
  configurableParameters: ComfyWorkflowConfigurableParameter[]
  parameterSchemaSha256: string
  structuralContractSha256: string
  suggestedInputContract: JsonObject
  suggestedSemanticBindings: JsonObject[]
  suggestedModelRequirements: JsonObject[]
  blockers: Array<{ code: string; detail: string }>
  liveValidateRequirements: Array<{ code: string; detail: string; phase?: string }>
  admissionTier: 'candidate' | 'generic_fixed' | 'projected_dynamic'
  actionability: 'select_primary_output' | 'candidate_only' | 'ready_for_executable_import'
  recoveryActions: Array<{ code: string; label: string }>
  contractDraftId: string
  contractDraftRevision: number
}

export interface WorkflowSmokeAssetCandidate {
  id: string
  name: string
  backendKind: string
  mimeType: string
  checksum: string
  durationMs: number | null
  sourceFps: number | null
  hasEmbeddedAudio: boolean
  smokeEligible: boolean
  smokeIneligibleReason: string
}

export interface ComfySavedWorkflowBatchItemInput {
  sourcePath: string
  expectedSha256: string
  expectedSourceScopeSchemaVersion: number
  expectedSourceScopeSha256: string
  importKind: 'candidate' | 'template_version' | 'execution_snapshot' | 'ignored'
  primaryOutputNodeIds?: string[]
  expectedObjectInfoSchemaSha256?: string
  expectedConvertedGraphSha256?: string
  expectedParameterSchemaSha256?: string
  expectedStructuralContractSha256?: string
  contractDraft?: string | null
  successorOf?: string | null
  template?: string | null
  newTemplate?: { name: string; slug: string; outputKind: 'image' | 'video' | 'audio' | 'text'; description?: string } | null
  modelProfileVersion?: string | null
}

export interface ComfySavedWorkflowBatchResult {
  schemaVersion: number
  deploymentId: string
  status: 'complete' | 'partial' | 'failed'
  createdCount: number
  reusedCount: number
  failedCount: number
  items: Array<{
    sourceDeploymentId: string
    sourcePath: string
    sourceSha256: string
    sourceId: string
    importKind: 'candidate' | 'template_version' | 'execution_snapshot' | 'ignored'
    result: 'created' | 'reused' | 'failed'
    code: string
    detail: string
    workflowVersionId: string
    structuralContractSha256: string
  }>
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

export interface ExecutionSmokeIdentity {
  preparationId: string | null
  allowExternalCalls: boolean
  budgetLimit: string | null
  actualCost: string | null
  scope: 'resource' | 'instance'
  executionInstanceVersion: string | null
  evidenceExpiresAt: string
  resultSchemaVersion: number
  resultSizeBytes: number
  resultSha256: string
}

export interface WorkflowSmokeAttempt extends PlatformEntity, ExecutionSmokeIdentity {
  estimatedCost: string
  costCurrency: string
  workflowVersion: string
  deployment: string
  createdBy: string
  status: string
  graphSha256: string
  executionContractSha256: string
  validationEvidenceSha256: string
  targetSha256: string
  fixtureValuesSchemaVersion: number
  fixtureValuesSizeBytes: number
  fixtureValuesSha256: string
  fixtureSha256: string
  assetBindings: WorkflowSmokeAssetBinding[]
  resolutions: WorkflowSmokeAttemptResolution[]
  resolutionRequired: boolean
  workerId: string
  submissionStartedAt: string
  submittedAt: string
  lastPolledAt: string
  nextPollAt: string
  pollCount: number
  remoteStatus: string
  deadlineStartedAt: string
  hardDeadlineAt: string
  idleDeadlineAt: string
  lastMeaningfulProgressAt: string
  cancelRequestedAt: string
  cancelSubmittedAt: string
  finishedAt: string
  errorCode: string
  errorMessage: string
  result: JsonObject
}

export type WorkflowSmokeResolutionOutcome =
  | 'confirmed_not_submitted'
  | 'confirmed_remote_failed'
  | 'confirmed_remote_succeeded'

export interface WorkflowSmokeAttemptResolution extends PlatformEntity {
  workflowSmokeAttemptId: string
  revision: number
  outcome: WorkflowSmokeResolutionOutcome
  providerRequestIdSha256: string
  evidence: JsonObject
  evidenceSchemaVersion: number
  evidenceSizeBytes: number
  evidenceSha256: string
  note: string
  decidedBy: string
}

export interface WorkflowSmokeAssetBinding {
  semanticKey: string
  inputSlot: number
  asset: string
  mediaKind: string
  category: string
  checksum: string
  mimeType: string
  sizeBytes: number
  createdAt: string
}

export interface ModelDeploymentSmokeAttempt extends PlatformEntity, ExecutionSmokeIdentity {
  result: JsonObject
  promptProfile: JsonObject | null
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
  /**
   * Only the two server-contract values are actionable. Missing or future values
   * stay in the explicit unknown bucket so the settings UI fails closed.
   */
  configurationClass: 'standard' | 'security_boundary' | 'unknown'
  configurationClassRaw: string
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
  hasValues: boolean
  mutableFields: string[]
}

export interface ConfigDefinitionOptions {
  configurationClasses: FormCatalogOption[]
  valueTypes: FormCatalogOption[]
  scopes: FormCatalogOption[]
  defaultSources: FormCatalogOption[]
  domains: FormCatalogOption[]
  units: FormCatalogOption[]
  impactModules: FormCatalogOption[]
  customEntryFields: string[]
  systemDefaults: JsonObject
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
  changesSecurityBoundaries: boolean | null
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
  probeKind: 'capability' | 'draft_model_artifact_attestation' | string
  requestSnapshot: JsonObject
  requestSnapshotSchemaVersion: number
  requestSnapshotSizeBytes: number
  requestSnapshotSha256: string
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
  scope: 'configuration_only'
  requestChecked: false
  state: 'not_required' | 'blocked' | 'ready' | string
  required: boolean
  reasonCodes: string[]
  warningCodes: string[]
  instanceCount: number | null
  viableInstanceCount: number | null
}

export interface ProductionReadinessValidationError {
  phase: 'definition' | 'requirements' | 'pipeline'
  fieldPath: string
  message: string
  code: string | null
  stageIndex: number | null
  stageKey: string | null
  stageKind: string | null
  routingKey: string | null
  routeIndex: number | null
}

export interface ProductionReadinessRecipe {
  id: string
  recipeKey: string
  version: number
  state: 'blocked' | 'ready' | string
  reasonCodes: string[]
  capabilities: Record<string, ProductionReadinessCapability>
  validationErrors: ProductionReadinessValidationError[]
}

export interface ProductionReadiness {
  schemaVersion: number
  requestChecked: false
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
  overrideValues: JsonObject
  reason: string
}

export interface ScriptSource extends PlatformEntity {
  project: string
  sourceAsset: string
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

export interface ProductionRecipeStageOption {
  value: string
  label: string
  repeatable: boolean
  candidateEvaluatorOptions: { value: string | null; label: string }[]
  modes: string[]
  requiresRouting: boolean
  routingModes: string[]
  decisionPolicy: string | null
}

export interface ProductionRecipePresetOption extends FormCatalogOption {
  stages: JsonObject[]
}

export interface ProductionRecipeEditorOptions {
  assemblyOutputSchema?: JsonObject
  assemblyTimingSchema?: JsonObject
  assemblyMediaSchema?: JsonObject
  deliveryMetadataSchema?: JsonObject
  shotReferenceSourceOptions: {value: string; label: string; upstreamStageKey: string | null; selectionOptions: FormCatalogOption[]}[]
  materialSourceOptions: FormCatalogOption[]
  materialSelectionOptions: FormCatalogOption[]
  definitionSchemaVersion: number
  effectiveLimits: { maxRoutingRoutes: number; maxVariants: number; maxGenerationTasks: number }
  stageOptions: ProductionRecipeStageOption[]
  approvalOptions: FormCatalogOption[]
  interactionModeOptions: FormCatalogOption[]
  fieldReviewPolicyOptions: JsonObject[]
  sourceKindOptions: FormCatalogOption[]
  structureNodeOptions: FormCatalogOption[]
  textOutputOptions: { outputKey: string; stageKey: string; kind: string; scopeKind: string; label: string; value_schema?:JsonObject; outputVersionId?:string; outputContractSha256?:string; structureVersionId?:string }[]
  referenceSourceOptions: { value: string; label: string; kind: string; scopeKind: string; stageKey: string; paired: boolean }[]
  parameterPolicyOptions: FormCatalogOption[]
  parameterSourceOptions: { selectorKey: string; label: string; scopeKind: string; type: string; unit: string; sourceKind: string; parameterKey: string | null; stageKey: string | null; description: string }[]
  scopeOptions: FormCatalogOption[]
  routingModeOptions: FormCatalogOption[]
  presets: ProductionRecipePresetOption[]
}

export interface ApprovalDecisionFact {
  id: string
  revision: number
  decision: string
  comment: string
  outputSnapshotSha256: string
  actor: 'human' | 'recipe_policy'
  decisionPolicy: string
  decisionPolicySha256: string
  decidedBy: string
  createdAt: string
}

export interface StageRun extends PlatformEntity {
  inputAmendmentPolicy?: string | null
  stageKey: string
  ordinal: number
  mode: string
  status: string
  approvalRequired: boolean
  decisionPolicy: string
  decisionPolicySha256: string
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
  selectedVariantRouteIndex?: number | null
  approvedEstimatedCost: string
  approvedCostCurrency: string
  selectionConfirmedAt: string
  startedAt: string
  finishedAt: string
  approvalDecisions: string[]
  approvalDecisionFacts: ApprovalDecisionFact[]
  artifactSelectionDecisions: Array<{
    id: string; variantGroup: string; revision: number; artifact: string
    selectionMode: string; selectedBy: string | null; createdAt: string
  }>
}

export type ProductionRecipeScriptMode = 'ai' | 'confirmed_revision' | 'downstream'
export type ProductionRecipeScriptClassification = ProductionRecipeScriptMode | 'invalid'
export type ProductionRecipeInteractionMode = 'assisted' | 'unattended' | 'invalid'

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
  textCandidate?: JsonObject | null
  manualMediaCandidate?: JsonObject | null
}

export interface ProductionRun extends PlatformEntity {
  project: string
  projectTitle: string
  scriptRevision: string
  recipeVersion: string
  planSource: 'recipe' | 'manual'
  executionPlan?: ProductionRunPlanEnvelope
  retryContext?: RetryContext | null
  idempotencyKey: string
  requestSha256: string
  status: string
  currentStageKey: string
  blockedCode: string
  blockedReason: string
  allowExternalCalls: boolean
  costCurrency: string
  estimatedCostTotal: string
  committedCostTotal: string
  actualCostTotal: string
  releaseBundleId: string
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

export interface ProductionRunPlanEnvelope {
  value: JsonObject
  schemaVersion: number
  sizeBytes: number
  sha256: string
}

export interface ProductionManualPlanInput {
  stageCatalogKey: string
  expectedStageCatalogSha256: string
  scope: { kind: 'project' } | { kind: 'episodes'; episodeIds: string[] }
  delivery: { kind: 'episode_masters' }
  initialContent: { kind: 'script_revision'; id: string; expectedSha256: string } | null
}

export interface ProductionManualOptions {
  projectId: string
  stageCatalog: ProductionRunPlanEnvelope
  scopeKinds: string[]
  deliveryKinds: string[]
  initialContentKinds: string[]
  facts: Record<string, number>
}

export interface ProductionRunCreateCommon {
  projectId: string
  idempotencyKey: string
  allowExternalCalls?: boolean | null
  configOverrides?: JsonObject
}

export interface ProductionManualRunCreateInput extends ProductionRunCreateCommon {
  planSource: 'manual'
  manualPlan: ProductionManualPlanInput
}

export interface RecipeProjectSourceConfirmation {
  executionInstanceVersionId: string
  inputKey: string
  reference: string
}

export interface ProductionRecipeRunCreateInput extends ProductionRunCreateCommon {
  planSource: 'recipe'
  scriptMode: ProductionRecipeScriptMode
  scriptRevisionId?: string | null
  recipeVersionId: string
  idea?: string
  styleHint?: string
  episodeCount?: number
  shotsPerEpisode?: number | null
  episodeDurationSeconds?: number | null
  structuredImportPlan?: boolean
  episodeIds?: string[]
  unattendedConfirmed?: boolean
  projectSourceConfirmations?: RecipeProjectSourceConfirmation[]
}

export type ProductionRunCreateInput = ProductionRecipeRunCreateInput | ProductionManualRunCreateInput

export interface BibleImageBindingSelectionInput {
  entityType: 'character' | 'scene' | 'prop'
  entityId: string
  selectedArtifactIds: string[]
  selectedLibraryAssets: Array<{
    assetId: string
    expectedSha256: string
  }>
  anchorArtifactId?: string
  anchorLibraryAssetId?: string
}

export interface RegenerateBibleImagesInput {
  requestId: string
  expectedSourceSnapshotSha256: string
  entities: Array<{
    entityType: 'character' | 'scene' | 'prop'
    entityId: string
  }>
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
