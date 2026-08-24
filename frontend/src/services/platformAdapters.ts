import type {
  ArtifactPreview, ConfigDefinition, ConfigResourceOption, ConfigRevision, ConfigValue, GeneratedArtifact,
  GenerationAttempt,
  HostCapabilitySnapshot, LegacyWorkflowTemplate, ModelArtifact, ModelDeployment,
  ModelDeploymentSmokeAttempt,
  ModelProfileVersion, NodeMetadataItem, NodeMetadataResponse, ProductionRecipe, ProductionRecipeMediaRouteOptions,
  PromptFieldDefinition, PromptFieldValueType, PromptProfileBinding, PromptProfileFieldRule,
  PromptProfileOptions, PromptProfilePreview, PromptProfileVersion,
  ProductionRun, ProviderProfile, RecommendationCandidate, RecommendationDecision,
  RecommendationSnapshot, ResolvedConfiguration,
  RuntimeHost, RuntimeHostProbeAttempt, RuntimeModelAvailability, RuntimeReadiness, RuntimeReadinessCapability,
  PromptNormalizationSmokeResolution, ProductionReadiness, ScriptRevision, ScriptSource, StageRun, WorkflowModelRequirement,
  WorkflowSemanticBinding, WorkflowTemplateVersion, JsonObject,
  WorkflowTemplateCanvasExport,
  ProductionRecipeScriptClassification,
  PlatformLifecycleAuditEvent, SecretAuditEvent, SecretBinding, WorkflowSmokeAttempt,
} from './platformTypes'

type Raw = Record<string, unknown>

export const platformRecord = (value: unknown): Raw => (
  value && typeof value === 'object' && !Array.isArray(value) ? value as Raw : {}
)
const string = (value: unknown): string => value === null || value === undefined ? '' : String(value)
const number = (value: unknown, fallback = 0): number => Number.isFinite(Number(value)) ? Number(value) : fallback
const nullableNumber = (value: unknown): number | null => value === null || value === undefined || value === '' ? null : Number.isFinite(Number(value)) ? Number(value) : null
const boolean = (value: unknown): boolean => value === true
const object = (value: unknown): JsonObject => platformRecord(value)
const strings = (value: unknown): string[] => Array.isArray(value) ? value.map(string) : []
const entity = (raw: Raw) => ({ id: string(raw.id), createdAt: string(raw.created_at), updatedAt: string(raw.updated_at) })

export const platformRows = (payload: unknown): unknown[] => {
  const root = platformRecord(payload)
  const value = root.data ?? payload
  if (Array.isArray(value)) return value
  const page = platformRecord(value)
  return Array.isArray(page.results) ? page.results : []
}

export const productionRecipeScriptMode = (definition: JsonObject): ProductionRecipeScriptClassification => {
  const stages = Array.isArray(definition.stages) ? definition.stages : []
  if (!stages.length) return 'invalid'
  const stageKeys = stages.map((value) => string(platformRecord(value).key)).filter(Boolean)
  if (stageKeys.length !== stages.length || new Set(stageKeys).size !== stageKeys.length) return 'invalid'
  const aiStages = stages.filter((value) => {
    const row = platformRecord(value)
    return row.key === 'script_generate' && row.mode === 'ai'
  })
  const confirmedStages = stages.filter((value) => {
    const row = platformRecord(value)
    return row.key === 'script_input' && row.mode === 'confirmed_revision'
  })
  const downstreamStages = stages.filter((value) => {
    const row = platformRecord(value)
    return row.key === 'script_input' && row.mode === 'manual'
  })
  const scriptReviewStages = stages.filter((value) => {
    const row = platformRecord(value)
    return row.key === 'script_review' && row.mode === 'review'
  })
  const scriptStructureStages = stages.filter((value) => {
    const row = platformRecord(value)
    return row.key === 'script_structure' && row.mode === 'ai'
  })
  const normalizeStages = stages.filter((value) => {
    const row = platformRecord(value)
    return row.key === 'script_normalize' && row.mode === 'ai'
  })
  const normalizeReviewStages = stages.filter((value) => {
    const row = platformRecord(value)
    return row.key === 'script_normalize_review' && row.mode === 'review'
  })
  const scriptStageInvalid = stages.some((value) => {
    const row = platformRecord(value)
    if (row.key === 'script_generate') return row.mode !== 'ai'
    if (row.key === 'script_input') return !['confirmed_revision', 'manual'].includes(string(row.mode))
    if (row.key === 'script_structure') return row.mode !== 'ai'
    if (row.key === 'script_review') return row.mode !== 'review'
    if (row.key === 'script_normalize') return row.mode !== 'ai'
    if (row.key === 'script_normalize_review') return row.mode !== 'review'
    return typeof row.key === 'string' && row.key.startsWith('script_')
  })
  if (
    scriptStageInvalid
    || aiStages.length + confirmedStages.length + downstreamStages.length !== 1
    || (aiStages.length === 1 && scriptReviewStages.length !== 1)
    || scriptStructureStages.length > 1
    || (scriptStructureStages.length === 1 && (
      confirmedStages.length !== 1 || scriptReviewStages.length !== 1
    ))
    || normalizeStages.length !== normalizeReviewStages.length
    || normalizeStages.length > 1
  ) return 'invalid'
  if (aiStages.length === 1) return 'ai'
  if (confirmedStages.length === 1) return 'confirmed_revision'
  return downstreamStages.length === 1 ? 'downstream' : 'invalid'
}

export const adaptProviderProfile = (value: unknown): ProviderProfile => { const raw = platformRecord(value); return {
  ...entity(raw), key: string(raw.key), name: string(raw.name), driverType: string(raw.driver_type),
  modalities: string(raw.modalities), operations: string(raw.operations), credentialConfigured: boolean(raw.credential_configured),
  driverConfig: object(raw.driver_config), driverConfigSchemaVersion: number(raw.driver_config_schema_version),
  driverConfigSizeBytes: number(raw.driver_config_size_bytes), driverConfigSha256: string(raw.driver_config_sha256),
  capabilities: object(raw.capabilities), isSystem: boolean(raw.is_system), isActive: boolean(raw.is_active),
} }

export const adaptModelProfileVersion = (value: unknown): ModelProfileVersion => { const raw = platformRecord(value); return {
  ...entity(raw), profileKey: string(raw.profile_key), version: number(raw.version), provider: string(raw.provider),
  providerKey: string(raw.provider_key), modelIdentifier: string(raw.model_identifier), modality: string(raw.modality),
  specification: object(raw.specification), specificationSchemaVersion: number(raw.specification_schema_version),
  specificationSizeBytes: number(raw.specification_size_bytes), specificationSha256: string(raw.specification_sha256),
  status: string(raw.status), publishedAt: string(raw.published_at), publishedBy: string(raw.published_by),
} }

export const adaptModelDeployment = (value: unknown): ModelDeployment => { const raw = platformRecord(value); const smoke = raw.latest_smoke ? platformRecord(raw.latest_smoke) : null; const unresolved = smoke?.unresolved_submission ? platformRecord(smoke.unresolved_submission) : null; return {
  ...entity(raw), key: string(raw.key), name: string(raw.name), profileVersion: string(raw.profile_version),
  providerProfileId: string(raw.provider_profile_id), providerDriver: string(raw.provider_driver), runtimeHost: string(raw.runtime_host),
  credentialConfigured: boolean(raw.credential_configured), latestSmoke: smoke ? {
    id: smoke.id === null || smoke.id === undefined ? null : string(smoke.id), status: string(smoke.status), operations: strings(smoke.operations),
    capturedAt: string(smoke.captured_at), expiresAt: string(smoke.expires_at), fresh: boolean(smoke.fresh),
    unresolvedSubmission: unresolved ? {
      id: string(unresolved.id), status: string(unresolved.status), operation: string(unresolved.operation),
      requestSha256: string(unresolved.request_sha256), targetSha256: string(unresolved.target_sha256),
      providerRequestIdSha256: string(unresolved.provider_request_id_sha256),
      submissionStartedAt: string(unresolved.submission_started_at),
    } : null,
  } : null, runtimeConfig: object(raw.runtime_config), runtimeConfigSchemaVersion: number(raw.runtime_config_schema_version),
  runtimeConfigSizeBytes: number(raw.runtime_config_size_bytes), runtimeConfigSha256: string(raw.runtime_config_sha256), priority: number(raw.priority),
  isDefault: boolean(raw.is_default), status: string(raw.status),
} }

export const adaptModelArtifact = (value: unknown): ModelArtifact => { const raw = platformRecord(value); return {
  ...entity(raw), profileVersion: string(raw.profile_version), role: string(raw.role), logicalName: string(raw.logical_name),
  filename: string(raw.filename), storageUri: string(raw.storage_uri), sha256: string(raw.sha256),
  sizeBytes: nullableNumber(raw.size_bytes), status: string(raw.status), verifiedSnapshot: string(raw.verified_snapshot),
  verifiedAt: string(raw.verified_at), verifiedBy: string(raw.verified_by),
} }

export const adaptPromptFieldDefinition = (value: unknown): PromptFieldDefinition => { const raw = platformRecord(value); return {
  ...entity(raw), key: string(raw.key), label: string(raw.label), category: string(raw.category),
  description: string(raw.description), impact: string(raw.impact), valueType: string(raw.value_type) as PromptFieldValueType,
  defaultValue: raw.default_value, valueOptions: strings(raw.value_options), isRequired: boolean(raw.is_required),
  isActive: boolean(raw.is_active), displayOrder: number(raw.display_order),
} }

export const adaptPromptProfileVersion = (value: unknown): PromptProfileVersion => { const raw = platformRecord(value); return {
  ...entity(raw), profileKey: string(raw.profile_key), version: number(raw.version), name: string(raw.name),
  description: string(raw.description), modelFamily: string(raw.model_family), operation: string(raw.operation),
  mode: string(raw.mode), officialReference: string(raw.official_reference), status: string(raw.status),
  reviewState: string(raw.review_state), contractSchemaVersion: number(raw.contract_schema_version),
  contractSizeBytes: number(raw.contract_size_bytes), contractSha256: string(raw.contract_sha256),
  fieldRuleCount: number(raw.field_rule_count), publishedAt: string(raw.published_at), publishedBy: string(raw.published_by),
} }

export const adaptPromptProfileFieldRule = (value: unknown): PromptProfileFieldRule => { const raw = platformRecord(value); return {
  ...entity(raw), profileVersion: string(raw.profile_version), fieldDefinition: string(raw.field_definition),
  fieldKey: string(raw.field_key), fieldLabel: string(raw.field_label), fieldValueType: string(raw.field_value_type),
  sourceSelector: string(raw.source_selector), channel: string(raw.channel), position: number(raw.position),
  labelStyle: string(raw.label_style), separator: string(raw.separator), required: boolean(raw.required),
  omitIfEmpty: boolean(raw.omit_if_empty), maxChars: number(raw.max_chars),
} }

export const adaptPromptProfileBinding = (value: unknown): PromptProfileBinding => { const raw = platformRecord(value); return {
  ...entity(raw), key: string(raw.key), name: string(raw.name), operation: string(raw.operation),
  modelFamily: string(raw.model_family), mode: string(raw.mode), modelProfileVersion: string(raw.model_profile_version),
  modelIdentifier: string(raw.model_identifier), promptProfileVersion: string(raw.prompt_profile_version),
  promptProfileKey: string(raw.prompt_profile_key), promptProfileContractSha256: string(raw.prompt_profile_contract_sha256),
  priority: number(raw.priority), isActive: boolean(raw.is_active),
} }

export const adaptPromptProfileOptions = (value: unknown): PromptProfileOptions => { const raw = platformRecord(value); const choice = (item: unknown) => { const row = platformRecord(item); return { value: string(row.value), label: string(row.label) } }; return {
  operations: strings(raw.operations), modelFamilies: strings(raw.model_families), modes: strings(raw.modes),
  sourceSelectors: Array.isArray(raw.source_selectors) ? raw.source_selectors.map(choice) : [],
  channels: Array.isArray(raw.channels) ? raw.channels.map(choice) : [],
  labelStyles: Array.isArray(raw.label_styles) ? raw.label_styles.map(choice) : [],
  separators: Array.isArray(raw.separators) ? raw.separators.map(choice) : [],
  modelProfileVersions: Array.isArray(raw.model_profile_versions) ? raw.model_profile_versions.map((item) => { const row = platformRecord(item); return {
    id: string(row.id), profileKey: string(row.profile_key), version: number(row.version),
    modelIdentifier: string(row.model_identifier), modality: string(row.modality),
  } }) : [],
} }

export const adaptPromptProfilePreview = (value: unknown): PromptProfilePreview => { const raw = platformRecord(value); const rawChannels = platformRecord(raw.channels); return {
  schemaVersion: number(raw.schema_version), promptProfileVersionId: string(raw.prompt_profile_version_id),
  profileKey: string(raw.profile_key), version: number(raw.version), contractSha256: string(raw.contract_sha256),
  channels: Object.fromEntries(Object.entries(rawChannels).map(([key, item]) => [key, string(item)])),
  structuredFields: object(raw.structured_fields), trace: Array.isArray(raw.trace) ? raw.trace.map(object) : [],
} }

export const adaptLegacyWorkflowTemplate = (value: unknown): LegacyWorkflowTemplate => { const raw = platformRecord(value); return {
  ...entity(raw), name: string(raw.name), slug: string(raw.slug), version: number(raw.version), provider: string(raw.provider),
  model: string(raw.model), outputKind: string(raw.output_kind), description: string(raw.description),
  isActive: boolean(raw.is_active), isSystem: boolean(raw.is_system),
} }

const adaptSemanticBinding = (value: unknown): WorkflowSemanticBinding => { const raw = platformRecord(value); return {
  id: string(raw.id), semanticKey: string(raw.semantic_key), nodeId: string(raw.node_id), inputName: string(raw.input_name),
  valueType: string(raw.value_type), isRequired: boolean(raw.is_required),
} }
const adaptModelRequirement = (value: unknown): WorkflowModelRequirement => { const raw = platformRecord(value); return {
  id: string(raw.id), role: string(raw.role), alias: string(raw.alias), artifactId: string(raw.artifact),
  expectedFilename: string(raw.expected_filename), expectedSha256: string(raw.expected_sha256), isRequired: boolean(raw.is_required),
} }
export const adaptWorkflowTemplateVersion = (value: unknown): WorkflowTemplateVersion => { const raw = platformRecord(value); const result: WorkflowTemplateVersion = {
  ...entity(raw), template: string(raw.template), templateName: string(raw.template_name), templateSlug: string(raw.template_slug),
  outputKind: string(raw.output_kind), version: number(raw.version), provider: string(raw.provider),
  modelProfileVersion: string(raw.model_profile_version), status: string(raw.status), validationState: string(raw.validation_state),
  validationMessage: string(raw.validation_message), smokeTestState: string(raw.smoke_test_state),
  smokeTestMessage: string(raw.smoke_test_message), isCurrent: boolean(raw.is_current), graphSha256: string(raw.graph_sha256),
  graphSizeBytes: number(raw.graph_size_bytes), inputContractSha256: string(raw.input_contract_sha256),
  inputContractSizeBytes: number(raw.input_contract_size_bytes), compilerId: string(raw.compiler_id), compilerVersion: number(raw.compiler_version),
  semanticBindingsSha256: string(raw.semantic_bindings_sha256), modelRequirementsSha256: string(raw.model_requirements_sha256),
  executionContractSha256: string(raw.execution_contract_sha256), validatedDeployment: string(raw.validated_deployment),
  validationEvidenceSha256: string(raw.validation_evidence_sha256), smokeTestEvidenceSha256: string(raw.smoke_test_evidence_sha256),
  semanticBindingCount: number(raw.semantic_binding_count), modelRequirementCount: number(raw.model_requirement_count),
  publishedAt: string(raw.published_at), publishedBy: string(raw.published_by),
}; if ('graph' in raw) result.graph = object(raw.graph); if ('input_contract' in raw) result.inputContract = object(raw.input_contract)
  if (Array.isArray(raw.semantic_bindings)) result.semanticBindings = raw.semantic_bindings.map(adaptSemanticBinding)
  if (Array.isArray(raw.model_requirements)) result.modelRequirements = raw.model_requirements.map(adaptModelRequirement)
  if ('validation_evidence' in raw) result.validationEvidence = object(raw.validation_evidence)
  if ('smoke_test_evidence' in raw) result.smokeTestEvidence = object(raw.smoke_test_evidence)
  return result
}

export const adaptWorkflowTemplateCanvasExport = (value: unknown): WorkflowTemplateCanvasExport => {
  const raw = platformRecord(value)
  const deploymentSource = string(raw.deployment_source)
  if (!['validated', 'explicit_draft'].includes(deploymentSource)) {
    throw new Error('workflow canvas export returned an invalid deployment source')
  }
  return {
    workflowVersionId: string(raw.workflow_version_id),
    format: 'comfyui_ui',
    filename: string(raw.filename),
    graphSha256: string(raw.graph_sha256),
    templateSlug: string(raw.template_slug),
    templateVersion: number(raw.template_version),
    deploymentId: string(raw.deployment_id),
    deploymentName: string(raw.deployment_name),
    deploymentSource: deploymentSource as WorkflowTemplateCanvasExport['deploymentSource'],
    workflow: object(raw.workflow),
    importReady: boolean(raw.import_ready),
    openReason: string(raw.open_reason),
    // The product intentionally has no cross-origin browser injection path.
    directOpenSupported: false,
    directOpenUrl: null,
    openMode: 'download_import',
    downloadUrl: string(raw.download_url),
  }
}

const adaptNodeMetadataItem = (value: unknown): NodeMetadataItem => { const raw = platformRecord(value); return {
  classType: string(raw.class_type), displayName: string(raw.display_name), category: string(raw.category),
  requiredInputs: strings(raw.required_inputs), optionalInputs: strings(raw.optional_inputs), outputCount: number(raw.output_count),
} }
export const adaptNodeMetadata = (value: unknown): NodeMetadataResponse => { const raw = platformRecord(value); return {
  schemaVersion: number(raw.schema_version), totalCount: number(raw.total_count), offset: number(raw.offset), limit: number(raw.limit),
  nextOffset: raw.next_offset === null || raw.next_offset === undefined ? null : number(raw.next_offset),
  catalogSha256: string(raw.catalog_sha256),
  items: Array.isArray(raw.items) ? raw.items.map(adaptNodeMetadataItem) : [],
} }

export const adaptWorkflowSmokeAttempt = (value: unknown): WorkflowSmokeAttempt => { const raw = platformRecord(value); return {
  ...entity(raw), workflowVersion: string(raw.workflow_version), deployment: string(raw.deployment), createdBy: string(raw.created_by),
  status: string(raw.status), graphSha256: string(raw.graph_sha256), executionContractSha256: string(raw.execution_contract_sha256),
  validationEvidenceSha256: string(raw.validation_evidence_sha256), targetSha256: string(raw.target_sha256), workerId: string(raw.worker_id),
  submissionStartedAt: string(raw.submission_started_at), submittedAt: string(raw.submitted_at), lastPolledAt: string(raw.last_polled_at),
  nextPollAt: string(raw.next_poll_at), pollCount: number(raw.poll_count), remoteStatus: string(raw.remote_status),
  hardDeadlineAt: string(raw.hard_deadline_at), idleDeadlineAt: string(raw.idle_deadline_at), cancelRequestedAt: string(raw.cancel_requested_at),
  finishedAt: string(raw.finished_at), errorCode: string(raw.error_code), errorMessage: string(raw.error_message),
} }

export const adaptModelDeploymentSmokeAttempt = (value: unknown): ModelDeploymentSmokeAttempt => { const raw = platformRecord(value); return {
  ...entity(raw), deployment: string(raw.deployment), operation: string(raw.operation), status: string(raw.status),
  requestSha256: string(raw.request_sha256), targetSha256: string(raw.target_sha256), estimatedCost: string(raw.estimated_cost),
  costCurrency: string(raw.cost_currency), providerRequestIdSha256: string(raw.provider_request_id_sha256), workerId: string(raw.worker_id),
  submissionStartedAt: string(raw.submission_started_at), submittedAt: string(raw.submitted_at), lastPolledAt: string(raw.last_polled_at),
  nextPollAt: string(raw.next_poll_at), pollCount: number(raw.poll_count), remoteStatus: string(raw.remote_status),
  hardDeadlineAt: string(raw.hard_deadline_at), idleDeadlineAt: string(raw.idle_deadline_at), cancelRequestedAt: string(raw.cancel_requested_at),
  errorCode: string(raw.error_code), errorMessage: string(raw.error_message), finishedAt: string(raw.finished_at),
  finalSnapshot: string(raw.final_snapshot),
} }

export const adaptConfigDefinition = (value: unknown): ConfigDefinition => { const raw = platformRecord(value); return {
  ...entity(raw), key: string(raw.key), domain: string(raw.domain), valueType: string(raw.value_type),
  description: string(raw.description), unit: string(raw.unit), impactModules: strings(raw.impact_modules),
  restartRequired: boolean(raw.restart_required), allowedScopes: string(raw.allowed_scopes),
  allowedDefaultSources: string(raw.allowed_default_sources), isRequired: boolean(raw.is_required),
  isSecret: boolean(raw.is_secret), isActive: boolean(raw.is_active), needsReview: boolean(raw.needs_review),
  reviewReason: string(raw.review_reason), valueSchema: object(raw.value_schema), initialValue: raw.initial_value,
} }

export const adaptConfigResourceOption = (value: unknown): ConfigResourceOption => { const raw = platformRecord(value); return {
  value: string(raw.value), label: string(raw.label), resource: string(raw.resource), capability: string(raw.capability),
  state: string(raw.state), reasonCode: string(raw.reason_code), isSelectable: boolean(raw.is_selectable),
} }
const adaptConfigValue = (value: unknown): ConfigValue => { const raw = platformRecord(value); return {
  id: string(raw.id), key: string(raw.key), isSecret: boolean(raw.is_secret), value: raw.value,
  credentialConfigured: boolean(raw.credential_configured), valueSha256: string(raw.value_sha256),
} }
export const adaptConfigRevision = (value: unknown): ConfigRevision => { const raw = platformRecord(value); return {
  ...entity(raw), scopeType: string(raw.scope_type), project: string(raw.project), run: string(raw.run), revision: number(raw.revision),
  status: string(raw.status), basedOnRevision: string(raw.based_on_revision), activationState: string(raw.activation_state),
  activationAvailable: boolean(raw.activation_available),
  activatedAt: string(raw.activated_at), activationError: string(raw.activation_error),
  valuesSha256: string(raw.values_sha256), changeSummary: string(raw.change_summary),
  publishedAt: string(raw.published_at), values: Array.isArray(raw.values) ? raw.values.map(adaptConfigValue) : [],
} }
export const adaptSecretBinding = (value: unknown): SecretBinding => { const raw = platformRecord(value); return {
  ...entity(raw), key: string(raw.key), label: string(raw.label), purpose: string(raw.purpose),
  isActive: boolean(raw.is_active), configured: boolean(raw.configured), currentVersion: nullableNumber(raw.current_version),
} }
export const adaptSecretAuditEvent = (value: unknown): SecretAuditEvent => { const raw = platformRecord(value); return {
  id: string(raw.id), binding: string(raw.binding), bindingKey: string(raw.binding_key), version: nullableNumber(raw.version),
  actor: string(raw.actor), actorUsername: string(raw.actor_username), action: string(raw.action), reason: string(raw.reason),
  createdAt: string(raw.created_at),
} }
export const adaptPlatformLifecycleAuditEvent = (value: unknown): PlatformLifecycleAuditEvent => { const raw = platformRecord(value); return {
  id: string(raw.id), actor: string(raw.actor), actorUsername: string(raw.actor_username), action: string(raw.action),
  objectType: string(raw.object_type), objectId: string(raw.object_id), reasonCode: string(raw.reason_code),
  beforeSha256: string(raw.before_sha256), afterSha256: string(raw.after_sha256),
  beforeSummary: object(raw.before_summary), afterSummary: object(raw.after_summary), createdAt: string(raw.created_at),
} }
export const adaptResolvedConfiguration = (value: unknown): ResolvedConfiguration => { const raw = platformRecord(value); const sources: ResolvedConfiguration['sources'] = {}
  for (const [key, sourceValue] of Object.entries(object(raw.sources))) { const source = platformRecord(sourceValue); sources[key] = { source: string(source.source || source.source_type || source.scope), id: string(source.id || source.source_id || source.revision_id), version: number(source.version || source.revision), hash: string(source.hash || source.sha256), scope: string(source.scope), revision: number(source.revision), revisionId: string(source.revision_id) } }
  return { schemaVersion: number(raw.schema_version), values: object(raw.values), sources, revisionIds: strings(raw.revision_ids), sha256: string(raw.sha256) }
}

export const adaptRuntimeHost = (value: unknown): RuntimeHost => { const raw = platformRecord(value); return {
  ...entity(raw), key: string(raw.key), name: string(raw.name), fingerprintSha256: string(raw.fingerprint_sha256),
  status: string(raw.status), lastSeenAt: string(raw.last_seen_at),
} }
export const adaptRuntimeHostProbeAttempt = (value: unknown): RuntimeHostProbeAttempt => { const raw = platformRecord(value); return {
  ...entity(raw), host: string(raw.host), createdBy: string(raw.created_by), trigger: string(raw.trigger), status: string(raw.status),
  workerId: string(raw.worker_id), claimedAt: string(raw.claimed_at), heartbeatAt: string(raw.heartbeat_at),
  startedAt: string(raw.started_at), progressMarker: string(raw.progress_marker), cancelRequestedAt: string(raw.cancel_requested_at),
  finishedAt: string(raw.finished_at), errorCode: string(raw.error_code), errorMessage: string(raw.error_message),
  finalSnapshot: string(raw.final_snapshot),
} }
const adaptAvailability = (value: unknown): RuntimeModelAvailability => { const raw = platformRecord(value); return {
  id: string(raw.id), deployment: string(raw.deployment), artifact: string(raw.artifact), availabilityKey: string(raw.availability_key),
  status: string(raw.status), reason: string(raw.reason), observedSizeBytes: nullableNumber(raw.observed_size_bytes),
  observedSha256: string(raw.observed_sha256), observationSource: string(raw.observation_source),
} }
export const adaptHostSnapshot = (value: unknown): HostCapabilitySnapshot => { const raw = platformRecord(value); return {
  ...entity(raw), host: string(raw.host), revision: number(raw.revision), probeStatus: string(raw.probe_status),
  cpuArchitecture: string(raw.cpu_architecture), cpuName: string(raw.cpu_name), logicalCpuCount: number(raw.logical_cpu_count),
  memoryTotalBytes: number(raw.memory_total_bytes), diskTotalBytes: number(raw.disk_total_bytes), diskFreeBytes: number(raw.disk_free_bytes),
  gpuCount: number(raw.gpu_count), ffmpegAvailable: boolean(raw.ffmpeg_available), ffprobeAvailable: boolean(raw.ffprobe_available),
  comfyuiVersion: string(raw.comfyui_version), details: object(raw.details), capturedAt: string(raw.captured_at), expiresAt: string(raw.expires_at),
  modelAvailability: Array.isArray(raw.model_availability) ? raw.model_availability.map(adaptAvailability) : [],
} }
export const adaptRuntimeReadiness = (value: unknown): RuntimeReadiness => { const raw = platformRecord(value); const capabilities: Record<string, RuntimeReadinessCapability> = {}
  for (const [key, itemValue] of Object.entries(object(raw.capabilities))) { const item = platformRecord(itemValue); capabilities[key] = { state: string(item.state), reason: string(item.reason), deploymentCount: number(item.deployment_count) } }
  return { schemaVersion: number(raw.schema_version), snapshotId: string(raw.snapshot_id), capturedAt: string(raw.captured_at), capabilities }
}
export const adaptProductionReadiness = (value: unknown): ProductionReadiness => { const raw = platformRecord(value); return {
  schemaVersion: number(raw.schema_version), status: string(raw.status), reasonCodes: strings(raw.reason_codes),
  recipes: (Array.isArray(raw.recipes) ? raw.recipes : []).map((recipeValue) => {
    const recipe = platformRecord(recipeValue)
    const capabilities: ProductionReadiness['recipes'][number]['capabilities'] = {}
    for (const [key, capabilityValue] of Object.entries(object(recipe.capabilities))) {
      const capability = platformRecord(capabilityValue)
      capabilities[key] = { state: string(capability.state), required: boolean(capability.required), reasonCodes: strings(capability.reason_codes) }
    }
    return { id: string(recipe.id), recipeKey: string(recipe.recipe_key), version: number(recipe.version), state: string(recipe.state), reasonCodes: strings(recipe.reason_codes), capabilities }
  }),
} }
const adaptRecommendationCandidate = (value: unknown): RecommendationCandidate => { const raw = platformRecord(value); return {
  ...entity(raw), capabilityKey: string(raw.capability_key), deployment: string(raw.deployment), deploymentKey: string(raw.deployment_key),
  workflowVersion: string(raw.workflow_version), status: string(raw.status), rank: nullableNumber(raw.rank), reasonCode: string(raw.reason_code),
  suggestedValues: object(raw.suggested_values), suggestedValuesSha256: string(raw.suggested_values_sha256),
} }
const adaptRecommendationDecision = (value: unknown): RecommendationDecision => { const raw = platformRecord(value); return {
  ...entity(raw), recommendationSnapshot: string(raw.recommendation_snapshot), candidate: string(raw.candidate), decision: string(raw.decision),
  scopeType: string(raw.scope_type), project: string(raw.project), configRevision: string(raw.config_revision), recipeVersion: string(raw.recipe_version),
  overrideValues: object(raw.override_values), reason: string(raw.reason),
} }
export const adaptRecommendation = (value: unknown): RecommendationSnapshot => { const raw = platformRecord(value); return {
  ...entity(raw), hostSnapshot: string(raw.host_snapshot), project: string(raw.project), configRevision: string(raw.config_revision),
  inputSha256: string(raw.input_sha256), status: string(raw.status), reason: string(raw.reason), recommendation: object(raw.recommendation),
  candidates: Array.isArray(raw.candidates) ? raw.candidates.map(adaptRecommendationCandidate) : [],
  decisions: Array.isArray(raw.decisions) ? raw.decisions.map(adaptRecommendationDecision) : [],
} }

export const adaptScriptSource = (value: unknown): ScriptSource => { const raw = platformRecord(value); return {
  ...entity(raw), project: string(raw.project), sourceType: string(raw.source_type), sourceFormat: string(raw.source_format),
  status: string(raw.status), originalName: string(raw.original_name), mimeType: string(raw.mime_type), fileSizeBytes: nullableNumber(raw.file_size_bytes),
  fileSha256: string(raw.file_sha256), validationMessage: string(raw.validation_message), latestRevisionId: string(raw.latest_revision_id),
} }
export const adaptScriptRevision = (value: unknown): ScriptRevision => { const raw = platformRecord(value); const result: ScriptRevision = {
  ...entity(raw), source: string(raw.source), sourceStatus: string(raw.source_status), revision: number(raw.revision),
  contentSizeBytes: number(raw.content_size_bytes), contentSha256: string(raw.content_sha256), status: string(raw.status), confirmedAt: string(raw.confirmed_at),
}; if ('content' in raw) result.content = string(raw.content); return result }

export const adaptProductionRecipe = (value: unknown): ProductionRecipe => { const raw = platformRecord(value); return {
  ...entity(raw), recipeKey: string(raw.recipe_key), version: number(raw.version), name: string(raw.name), definition: object(raw.definition),
  status: string(raw.status), publishedAt: string(raw.published_at), publishedBy: string(raw.published_by),
  definitionSchemaVersion: number(raw.definition_schema_version), definitionSizeBytes: number(raw.definition_size_bytes),
  definitionSha256: string(raw.definition_sha256),
} }
export const adaptProductionRecipeMediaRouteOptions = (value: unknown): ProductionRecipeMediaRouteOptions => {
  const raw = platformRecord(value)
  return {
    schemaVersion: 1,
    recipeVersionId: string(raw.recipe_version_id),
    recipeDefinitionSha256: string(raw.recipe_definition_sha256),
    routes: (Array.isArray(raw.routes) ? raw.routes : []).map((routeValue) => {
      const route = platformRecord(routeValue)
      return {
        routeIndex: number(route.route_index),
        workflowVersionId: string(route.workflow_version_id),
        workflowLabel: string(route.workflow_label),
        modelDeploymentId: string(route.model_deployment_id),
        deploymentLabel: string(route.deployment_label),
        modelIdentifier: string(route.model_identifier),
        driverType: string(route.driver_type),
        outputKinds: strings(route.output_kinds),
        h3Modes: strings(route.h3_modes),
        mediaOperations: strings(route.media_operations),
        isSelectable: boolean(route.is_selectable),
        reasonCodes: strings(route.reason_codes),
        reasons: strings(route.reasons),
      }
    }),
  }
}
const adaptStageRun = (value: unknown): StageRun => { const raw = platformRecord(value); return {
  ...entity(raw), stageKey: string(raw.stage_key), ordinal: number(raw.ordinal), mode: string(raw.mode), status: string(raw.status),
  approvalRequired: boolean(raw.approval_required), attemptCount: number(raw.attempt_count), inputReference: string(raw.input_reference),
  inputSnapshot: object(raw.input_snapshot), inputSnapshotSha256: string(raw.input_snapshot_sha256),
  outputReference: string(raw.output_reference), outputSnapshot: object(raw.output_snapshot), outputSnapshotSha256: string(raw.output_snapshot_sha256),
  reasonCode: string(raw.reason_code), reason: string(raw.reason),
  routing: object(raw.routing), routingSha256: string(raw.routing_sha256), routingDecision: object(raw.routing_decision),
  routingDecisionSha256: string(raw.routing_decision_sha256), selectedRouteIndex: nullableNumber(raw.selected_route_index),
  approvedEstimatedCost: string(raw.approved_estimated_cost), approvedCostCurrency: string(raw.approved_cost_currency),
  selectionConfirmedAt: string(raw.selection_confirmed_at),
  startedAt: string(raw.started_at), finishedAt: string(raw.finished_at), approvalDecisions: strings(raw.approval_decisions),
} }
export const adaptGeneratedArtifact = (value: unknown): GeneratedArtifact => { const raw = platformRecord(value); const result: GeneratedArtifact = {
  ...entity(raw), manifestKey: string(raw.manifest_key), productionRun: string(raw.production_run), stageRun: string(raw.stage_run),
  generationAttempt: string(raw.generation_attempt), generationTask: string(raw.generation_task), asset: string(raw.asset), kind: string(raw.kind),
  role: string(raw.role), variantGroup: string(raw.variant_group), variantIndex: number(raw.variant_index), status: string(raw.status),
  storageBackend: string(raw.storage_backend), objectKey: string(raw.object_key), mimeType: string(raw.mime_type), sizeBytes: nullableNumber(raw.size_bytes),
  sha256: string(raw.sha256), manifestMetadataSha256: string(raw.manifest_metadata_sha256),
  previewUrl: string(raw.preview_url), downloadUrl: string(raw.download_url), comparisonParameters: object(raw.comparison_parameters),
}; if ('manifest_metadata' in raw) result.manifestMetadata = object(raw.manifest_metadata); return result }
export const adaptProductionRun = (value: unknown): ProductionRun => { const raw = platformRecord(value); const result: ProductionRun = {
  ...entity(raw), project: string(raw.project), projectTitle: string(raw.project_title), scriptRevision: string(raw.script_revision),
  recipeVersion: string(raw.recipe_version), idempotencyKey: string(raw.idempotency_key), requestSha256: string(raw.request_sha256),
  status: string(raw.status), currentStageKey: string(raw.current_stage_key), blockedCode: string(raw.blocked_code), blockedReason: string(raw.blocked_reason),
  allowExternalCalls: boolean(raw.allow_external_calls), budgetLimit: string(raw.budget_limit), costCurrency: string(raw.cost_currency),
  estimatedCostTotal: string(raw.estimated_cost_total), committedCostTotal: string(raw.committed_cost_total),
  actualCostTotal: string(raw.actual_cost_total), releaseBundleId: string(raw.release_bundle_id), maxGenerationTasks: number(raw.max_generation_tasks),
  generationTaskCount: number(raw.generation_task_count), configSnapshotSha256: string(raw.config_snapshot_sha256), inputSnapshotSha256: string(raw.input_snapshot_sha256),
  startedAt: string(raw.started_at), finishedAt: string(raw.finished_at),
}; if ('config_revision' in raw) result.configRevision = string(raw.config_revision); if ('config_snapshot' in raw) result.configSnapshot = object(raw.config_snapshot)
  if ('input_snapshot' in raw) result.inputSnapshot = object(raw.input_snapshot); if (Array.isArray(raw.stages)) result.stages = raw.stages.map(adaptStageRun)
  if (Array.isArray(raw.artifacts)) result.artifacts = raw.artifacts.map(adaptGeneratedArtifact); return result }

export const adaptGenerationAttempt = (value: unknown): GenerationAttempt => { const raw = platformRecord(value); return {
  ...entity(raw), stageRun: string(raw.stage_run), unitKey: string(raw.unit_key), attemptNumber: number(raw.attempt_number),
  modelDeployment: string(raw.model_deployment), generationTask: string(raw.generation_task), status: string(raw.status),
  routeIndex: nullableNumber(raw.route_index), operation: string(raw.operation), isExternal: boolean(raw.is_external),
  providerRequestIdSha256: string(raw.provider_request_id_sha256), providerRequestIdSuffix: string(raw.provider_request_id_suffix),
  requestSha256: string(raw.request_sha256), requestSnapshotSchemaVersion: number(raw.request_snapshot_schema_version),
  requestSnapshotSizeBytes: number(raw.request_snapshot_size_bytes), requestSnapshotSha256: string(raw.request_snapshot_sha256),
  resultSha256: string(raw.result_sha256), estimatedCost: string(raw.estimated_cost), committedCost: string(raw.committed_cost),
  actualCost: string(raw.actual_cost), costCurrency: string(raw.cost_currency), errorCode: string(raw.error_code),
  remoteStatus: string(raw.remote_status), submissionStartedAt: string(raw.submission_started_at), submittedAt: string(raw.submitted_at),
  lastPolledAt: string(raw.last_polled_at), pollCount: number(raw.poll_count), hardDeadlineAt: string(raw.hard_deadline_at),
  idleDeadlineAt: string(raw.idle_deadline_at), startedAt: string(raw.started_at), finishedAt: string(raw.finished_at),
} }

export const adaptPromptNormalizationSmokeResolution = (value: unknown): PromptNormalizationSmokeResolution => { const raw = platformRecord(value); return {
  id: string(raw.id), smokeAttemptId: string(raw.smoke_attempt_id), smokeAttemptStatus: string(raw.smoke_attempt_status),
  revision: number(raw.revision), outcome: string(raw.outcome) as PromptNormalizationSmokeResolution['outcome'],
  requestSha256: string(raw.request_sha256), targetSha256: string(raw.target_sha256),
  providerRequestIdSha256: string(raw.provider_request_id_sha256), evidenceSha256: string(raw.evidence_sha256),
  note: string(raw.note), decidedBy: string(raw.decided_by), createdAt: string(raw.created_at),
} }

export const adaptArtifactPreview = (value: unknown, taskId: string, outputIndex: number): ArtifactPreview | null => {
  const raw = platformRecord(value); const media = Array.isArray(raw.output_media) ? raw.output_media.map(platformRecord) : []
  const selected = media.find((item, index) => number(item.index, index) === outputIndex)
  if (!selected) return null
  return { taskId, outputIndex, kind: string(selected.kind), filename: string(selected.filename), previewUrl: string(selected.preview_url), downloadUrl: string(selected.download_url), parameters: object(raw.input_parameters), backendStatus: string(raw.status) }
}

export const presentPlatformStatus = (status: string): { label: string; tone: string } => {
  const normalized = status.trim().toLowerCase()
  const known: Record<string, { label: string; tone: string }> = {
    active: { label: '启用', tone: 'info' }, ready: { label: '就绪', tone: 'success' }, configured: { label: '已配置', tone: 'info' }, published: { label: '已发布', tone: 'info' }, unconfigured: { label: '未配置', tone: 'warning' },
    succeeded: { label: '成功', tone: 'success' }, passed: { label: '通过', tone: 'success' }, selected: { label: '已选', tone: 'success' }, available: { label: '可用', tone: 'info' },
    running: { label: '运行中', tone: 'info' }, queued: { label: '排队中', tone: 'info' }, submitting: { label: '提交中', tone: 'info' }, submitted: { label: '已提交', tone: 'info' }, cancel_requested: { label: '取消中', tone: 'warning' }, uncertain: { label: '待消歧', tone: 'danger' }, draft: { label: '草稿', tone: 'muted' }, declared: { label: '已声明', tone: 'muted' }, inactive: { label: '停用', tone: 'muted' }, disabled: { label: '禁用', tone: 'muted' }, retired: { label: '已退役', tone: 'muted' }, not_run: { label: '未执行', tone: 'muted' }, unavailable: { label: '不可用', tone: 'warning' },
    pending: { label: '等待', tone: 'muted' }, pending_activation: { label: '待激活', tone: 'warning' }, awaiting_approval: { label: '待审批', tone: 'warning' }, blocked: { label: '阻断', tone: 'danger' }, missing: { label: '缺失', tone: 'danger' },
    failed: { label: '失败', tone: 'danger' }, error: { label: '异常', tone: 'danger' }, cancelled: { label: '已取消', tone: 'muted' }, invalid: { label: '无效', tone: 'danger' },
  }
  return known[normalized] ?? { label: status || '未返回', tone: 'unknown' }
}
