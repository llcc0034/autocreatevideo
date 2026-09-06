import { adaptResolutionOptions } from './workflowResolution'
import { adaptWorkflowConfigurationLinks } from './workflowConfigurationLinks'
import type {
  AcceptanceSmokeAction, AcceptanceSmokeDeploymentAttempt, AcceptanceSmokeDeploymentRef,
  AcceptanceSmokeDeploymentSummary,
  AcceptanceSmokeDeploymentRow, AcceptanceSmokeDeploymentSmokeSummary,
  AcceptanceSmokeEvidenceSummary, AcceptanceSmokePage, AcceptanceSmokeRow,
  AcceptanceSmokeSnapshot, AcceptanceSmokeValidationSummary, AcceptanceSmokeWorkflowAttempt,
  AcceptanceSmokeWorkflowRef, AcceptanceSmokeWorkflowRow, AcceptanceSmokeWorkflowSummary,
  ArtifactPreview, ConfigDefinition, ConfigDefinitionOptions, ConfigResourceOption, ConfigRevision, ConfigValue, GeneratedArtifact,
  GenerationAttempt,
  HostCapabilitySnapshot, LegacyWorkflowTemplate, ModelArtifact, ModelDeployment,
  ComfySavedWorkflowBatchResult, ComfySavedWorkflowPreview, ComfySavedWorkflowScan,
  WorkflowSmokeAssetCandidate, WorkflowSmokeFixtureSchema,
  ModelDeploymentSmokeAttempt,
  ModelProfileRegistryOptions, ModelProfileVersion, NodeMetadataItem, NodeMetadataResponse, ProductionRecipe, ProductionRecipeEditorOptions, ProductionRecipeMediaRouteOptions,
  PromptFieldDefinition, PromptFieldValueType, PromptProfileBinding, PromptProfileFieldRule,
  PromptProfileOptions, PromptProfilePreview, PromptProfileVersion,
  ProductionRun, ProviderProfile, RecommendationCandidate, RecommendationDecision,
  RecommendationSnapshot, ResolvedConfiguration,
  RuntimeHost, RuntimeHostProbeAttempt, RuntimeModelAvailability, RuntimeReadiness, RuntimeReadinessCapability,
  PromptNormalizationSmokeResolution, ProductionReadiness, ScriptRevision, ScriptSource, StageRun, WorkflowModelRequirement,
  WorkflowSemanticBinding, WorkflowTemplateVersion, JsonObject,
  WorkflowTemplateCanvasExport,
  WorkflowProjector, WorkflowProjectorOptions, WorkflowProjectorRecommendation, WorkflowProjectorVersion,
  ProductionRecipeInteractionMode,
  ProductionRecipeScriptClassification,
  ProductionPromptProfileRequirement,
  PlatformLifecycleAuditEvent, SecretAuditEvent, SecretBinding, WorkflowSmokeAttempt,
  WorkflowSmokeAttemptResolution,
} from './platformTypes'
import { adaptConditioningModeCapability, adaptConditioningModes } from './conditioningCapabilities'

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
const hasOwn = (value: Raw, key: string) => Object.prototype.hasOwnProperty.call(value, key)
const adaptFormCatalogOption = (value: unknown) => { const raw = platformRecord(value); return {
  value: string(raw.value), label: string(raw.label),
} }
const requiredArray = (raw: Raw, key: string, label: string): unknown[] => {
  if (!hasOwn(raw, key) || !Array.isArray(raw[key])) throw new Error(`${label} 缺少服务端目录字段 ${key}`)
  return raw[key] as unknown[]
}
const requiredStringArray = (raw: Raw, key: string, label: string): string[] => requiredArray(raw, key, label).map(string)
const requiredCatalog = (raw: Raw, key: string, label: string, allowEmpty = false) => {
  const rows = requiredArray(raw, key, label).map(adaptFormCatalogOption)
  const values = new Set(rows.map((item) => item.value))
  if ((!allowEmpty && !rows.length) || values.size !== rows.length || rows.some((item) => !item.value.trim() || !item.label.trim())) {
    throw new Error(`${label} 的 ${key} 目录为空、重复或缺少标签`)
  }
  return rows
}
const requiredObject = (raw: Raw, key: string, label: string): JsonObject => {
  const value = raw[key]
  if (!hasOwn(raw, key) || !value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${label} 缺少服务端对象字段 ${key}`)
  return value as JsonObject
}
const acceptanceRecord = (value: unknown, label: string): Raw => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${label} 必须是服务端对象`)
  return value as Raw
}
const acceptanceString = (raw: Raw, key: string, label: string, allowBlank = false): string => {
  if (!hasOwn(raw, key) || typeof raw[key] !== 'string' || (!allowBlank && !(raw[key] as string).trim())) {
    throw new Error(`${label} 缺少有效服务端字段 ${key}`)
  }
  return raw[key] as string
}
const acceptanceNullableString = (raw: Raw, key: string, label: string, allowBlank = false): string | null => {
  if (!hasOwn(raw, key)) throw new Error(`${label} 缺少服务端字段 ${key}`)
  if (raw[key] === null) return null
  return acceptanceString(raw, key, label, allowBlank)
}
const acceptanceBoolean = (raw: Raw, key: string, label: string): boolean => {
  if (!hasOwn(raw, key) || typeof raw[key] !== 'boolean') throw new Error(`${label} 缺少布尔服务端字段 ${key}`)
  return raw[key] as boolean
}
const acceptanceInteger = (raw: Raw, key: string, label: string, minimum = 0): number => {
  const value = raw[key]
  if (!hasOwn(raw, key) || !Number.isSafeInteger(value) || Number(value) < minimum) {
    throw new Error(`${label} 缺少有效整数服务端字段 ${key}`)
  }
  return Number(value)
}
const acceptanceArray = (raw: Raw, key: string, label: string): unknown[] => {
  if (!hasOwn(raw, key) || !Array.isArray(raw[key])) throw new Error(`${label} 缺少数组服务端字段 ${key}`)
  return raw[key] as unknown[]
}
const acceptanceValue = (raw: Raw, key: string, label: string): unknown => {
  if (!hasOwn(raw, key)) throw new Error(`${label} 缺少服务端字段 ${key}`)
  return raw[key]
}
const entity = (raw: Raw) => ({ id: string(raw.id), createdAt: string(raw.created_at), updatedAt: string(raw.updated_at) })
const parameterAlignment = (raw: Raw, type: string, field: string) => {
  if (!hasOwn(raw, 'alignment_modulus') || !hasOwn(raw, 'alignment_remainder')) {
    throw new Error(`${field} 缺少整数对齐合同字段`)
  }
  const rawModulus = raw.alignment_modulus
  const rawRemainder = raw.alignment_remainder
  if (rawModulus == null && rawRemainder == null) {
    return { alignmentModulus: null, alignmentRemainder: null }
  }
  if (
    type !== 'integer'
    || !Number.isSafeInteger(rawModulus) || Number(rawModulus) < 1
    || !Number.isSafeInteger(rawRemainder) || Number(rawRemainder) < 0
    || Number(rawRemainder) >= Number(rawModulus)
  ) {
    throw new Error(`${field} 的整数对齐合同无效`)
  }
  return {
    alignmentModulus: Number(rawModulus),
    alignmentRemainder: Number(rawRemainder),
  }
}

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
    return row.key === 'script_normalize' && ['ai', 'manual'].includes(string(row.mode))
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
    if (row.key === 'script_normalize') return !['ai', 'manual'].includes(string(row.mode))
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

export const productionRecipeInteractionMode = (definition: JsonObject): ProductionRecipeInteractionMode => {
  const schemaVersion = number(definition.schema_version, -1)
  const interactionMode = string(definition.interaction_mode)
  if (schemaVersion === 2 && interactionMode === 'unattended') return 'unattended'
  if (schemaVersion === 1 && !interactionMode) return 'assisted'
  return 'invalid'
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

export const adaptModelProfileRegistryOptions = (value: unknown): ModelProfileRegistryOptions => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('模型选项目录响应必须是对象')
  const raw = value as Raw
  const requiredArray = (input: unknown, field: string): unknown[] => {
    if (!Array.isArray(input)) throw new Error(`模型选项目录缺少 ${field} 数组`)
    return input
  }
  const requiredString = (input: unknown, field: string): string => {
    if (typeof input !== 'string' || !input.trim()) throw new Error(`模型选项目录 ${field} 无效`)
    return input.trim()
  }
  const requiredStrings = (input: unknown, field: string): string[] => requiredArray(input, field).map((item, index) => (
    requiredString(item, `${field}[${index}]`)
  ))
  const choices = (input: unknown, field: string) => requiredArray(input, field).map((item, index) => {
    const row = platformRecord(item)
    return {
      value: requiredString(row.value, `${field}[${index}].value`),
      label: requiredString(row.label, `${field}[${index}].label`),
    }
  })
  const schemaVersion = raw.specification_schema_version
  if (!Number.isInteger(schemaVersion) || Number(schemaVersion) < 1) throw new Error('模型选项目录 specification_schema_version 无效')
  const providerDrivers = requiredArray(raw.provider_drivers, 'provider_drivers').map((item, index) => {
    const row = platformRecord(item)
    return {
      value: requiredString(row.value, `provider_drivers[${index}].value`),
      label: requiredString(row.label, `provider_drivers[${index}].label`),
      modalities: requiredStrings(row.modalities, `provider_drivers[${index}].modalities`),
      operations: requiredStrings(row.operations, `provider_drivers[${index}].operations`),
    }
  })
  if (!providerDrivers.length) throw new Error('模型选项目录没有已注册 Provider 驱动')
  const modalities = choices(raw.modalities, 'modalities')
  const artifactRoles = choices(raw.artifact_roles, 'artifact_roles')
  const capabilities = requiredArray(raw.capabilities, 'capabilities').map((item, index) => {
    const row = platformRecord(item)
    if (typeof row.legacy !== 'boolean') throw new Error(`模型选项目录 capabilities[${index}].legacy 无效`)
    return {
      value: requiredString(row.value, `capabilities[${index}].value`),
      label: requiredString(row.label, `capabilities[${index}].label`),
      modalities: requiredStrings(row.modalities, `capabilities[${index}].modalities`),
      driverTypes: requiredStrings(row.driver_types, `capabilities[${index}].driver_types`),
      legacy: row.legacy,
    }
  })
  if (!modalities.length || !artifactRoles.length || !capabilities.length) throw new Error('模型选项目录缺少必需选项')
  const conditioning = platformRecord(raw.conditioning_contract)
  if (conditioning.schema_version !== 1 || !Number.isSafeInteger(conditioning.maximum_items) || Number(conditioning.maximum_items) < 1 || !Number.isSafeInteger(conditioning.maximum_modes) || Number(conditioning.maximum_modes) < 1 || !conditioning.mode_template || typeof conditioning.mode_template !== 'object' || Array.isArray(conditioning.mode_template)) throw new Error('模型素材合同选项目录不完整，请重新读取服务端目录')
  const inputKinds = choices(conditioning.input_kinds, 'conditioning_contract.input_kinds')
  const inputSlots = choices(conditioning.input_slots, 'conditioning_contract.input_slots')
  if (!inputKinds.length || !inputSlots.length) throw new Error('模型素材合同缺少模式或素材槽选项')
  return {
    conditioningContract: {
      schemaVersion: Number(conditioning.schema_version), adapterKey: requiredString(conditioning.adapter_key, 'conditioning_contract.adapter_key'),
      inputKinds, inputSlots,
      maximumItems: Number(conditioning.maximum_items), maximumModes: Number(conditioning.maximum_modes),
      identifierPattern: requiredString(conditioning.identifier_pattern, 'conditioning_contract.identifier_pattern'), modeTemplate: object(conditioning.mode_template),
    },
    specificationSchemaVersion: Number(schemaVersion),
    providerDrivers,
    modalities,
    artifactRoles,
    capabilities,
    modelFamilies: requiredStrings(raw.model_families, 'model_families'),
  }
}

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
  isDefault: boolean(raw.is_default), status: string(raw.status) as ModelDeployment['status'],
} }

const adaptAcceptanceSmokeDeploymentRef = (value: unknown): AcceptanceSmokeDeploymentRef => {
  const label = '验收 Deployment'
  const raw = acceptanceRecord(value, label)
  return {
    id: acceptanceString(raw, 'id', label),
    key: acceptanceString(raw, 'key', label),
    name: acceptanceString(raw, 'name', label),
    status: acceptanceString(raw, 'status', label),
    profileVersionId: acceptanceString(raw, 'profile_version_id', label),
    providerId: acceptanceString(raw, 'provider_id', label),
    providerDriver: acceptanceString(raw, 'provider_driver', label),
    runtimeHostId: acceptanceNullableString(raw, 'runtime_host_id', label),
  }
}

const adaptAcceptanceSmokeDeploymentReadiness = (
  value: unknown,
): AcceptanceSmokeDeploymentSummary => {
  const label = '验收 Deployment 状态'
  const raw = acceptanceRecord(value, label)
  const profileContractState = acceptanceString(
    raw,
    'profile_contract_state',
    label,
  )
  const targetState = acceptanceString(raw, 'target_state', label)
  if (profileContractState !== 'current' && profileContractState !== 'invalid') {
    throw new Error(`${label} profile_contract_state 无效：${profileContractState}`)
  }
  if (targetState !== 'current' && targetState !== 'invalid') {
    throw new Error(`${label} target_state 无效：${targetState}`)
  }
  return {
    ...adaptAcceptanceSmokeDeploymentRef(raw),
    currentTargetSha256: acceptanceString(
      raw,
      'current_target_sha256',
      label,
      true,
    ),
    profileContractState,
    profileContractReasonCode: acceptanceString(
      raw,
      'profile_contract_reason_code',
      label,
    ),
    targetState,
    targetReasonCode: acceptanceString(raw, 'target_reason_code', label),
  }
}

const adaptAcceptanceSmokeWorkflowRef = (value: unknown): AcceptanceSmokeWorkflowRef => {
  const label = '验收 Workflow'
  const raw = acceptanceRecord(value, label)
  return {
    id: acceptanceString(raw, 'id', label),
    templateId: acceptanceString(raw, 'template_id', label),
    templateName: acceptanceString(raw, 'template_name', label),
    templateSlug: acceptanceString(raw, 'template_slug', label),
    version: acceptanceInteger(raw, 'version', label, 1),
    status: acceptanceString(raw, 'status', label),
    isCurrent: acceptanceBoolean(raw, 'is_current', label),
    outputKind: acceptanceString(raw, 'output_kind', label),
    modelProfileVersionId: acceptanceNullableString(raw, 'model_profile_version_id', label),
    providerId: acceptanceNullableString(raw, 'provider_id', label),
  }
}

const adaptAcceptanceSmokeAction = (value: unknown): AcceptanceSmokeAction => {
  const label = '验收动作'
  const raw = acceptanceRecord(value, label)
  const method = acceptanceString(raw, 'method', label)
  if (method !== 'GET' && method !== 'POST') throw new Error(`验收动作 method 无效：${method}`)
  return {
    kind: acceptanceString(raw, 'kind', label),
    method,
    url: acceptanceString(raw, 'url', label),
    uiUrl: acceptanceString(raw, 'ui_url', label),
    enabled: acceptanceBoolean(raw, 'enabled', label),
    reasonCode: acceptanceString(raw, 'reason_code', label),
    requiresFixture: acceptanceBoolean(raw, 'requires_fixture', label),
    requiresConfirmation: acceptanceBoolean(raw, 'requires_confirmation', label),
    mayCallExternalService: acceptanceBoolean(raw, 'may_call_external_service', label),
  }
}

const adaptAcceptanceSmokeWorkflowAttempt = (value: unknown): AcceptanceSmokeWorkflowAttempt | null => {
  if (value === null || value === undefined) return null
  const label = 'Workflow Smoke attempt'
  const raw = acceptanceRecord(value, label)
  return {
    id: acceptanceString(raw, 'id', label),
    deploymentId: acceptanceString(raw, 'deployment_id', label),
    status: acceptanceString(raw, 'status', label),
    targetSha256: acceptanceString(raw, 'target_sha256', label, true),
    graphSha256: acceptanceString(raw, 'graph_sha256', label, true),
    errorCode: acceptanceString(raw, 'error_code', label, true),
    resolutionRequired: acceptanceBoolean(raw, 'resolution_required', label),
    createdAt: acceptanceString(raw, 'created_at', label),
    startedAt: acceptanceNullableString(raw, 'started_at', label),
    finishedAt: acceptanceNullableString(raw, 'finished_at', label),
  }
}

const adaptAcceptanceSmokeDeploymentAttempt = (value: unknown): AcceptanceSmokeDeploymentAttempt | null => {
  if (value === null || value === undefined) return null
  const label = 'Deployment Smoke attempt'
  const raw = acceptanceRecord(value, label)
  return {
    promptProfile: raw.prompt_profile == null ? null : acceptanceRecord(raw.prompt_profile, '烟测策略'),
    id: acceptanceString(raw, 'id', label),
    operation: acceptanceString(raw, 'operation', label),
    status: acceptanceString(raw, 'status', label),
    requestSha256: acceptanceString(raw, 'request_sha256', label, true),
    targetSha256: acceptanceString(raw, 'target_sha256', label, true),
    errorCode: acceptanceString(raw, 'error_code', label, true),
    resolutionRequired: acceptanceBoolean(raw, 'resolution_required', label),
    createdAt: acceptanceString(raw, 'created_at', label),
    submissionStartedAt: acceptanceNullableString(raw, 'submission_started_at', label),
    finishedAt: acceptanceNullableString(raw, 'finished_at', label),
  }
}

const adaptAcceptanceSmokeEvidenceSummary = (value: unknown): AcceptanceSmokeEvidenceSummary => {
  const label = '验收证据摘要'
  const raw = acceptanceRecord(value, label)
  return {
    storedState: acceptanceString(raw, 'stored_state', label),
    state: acceptanceString(raw, 'state', label),
    fresh: acceptanceBoolean(raw, 'fresh', label),
    reasonCode: acceptanceString(raw, 'reason_code', label, true),
    message: acceptanceString(raw, 'message', label, true),
    evidenceSha256: acceptanceString(raw, 'evidence_sha256', label, true),
  }
}

const adaptAcceptanceSmokeWorkflowSummary = (value: unknown): AcceptanceSmokeWorkflowSummary => {
  const raw = acceptanceRecord(value, 'Workflow Smoke 摘要')
  return {
    ...adaptAcceptanceSmokeEvidenceSummary(raw),
    evidenceDeploymentId: acceptanceNullableString(
      raw,
      'evidence_deployment_id',
      'Workflow Smoke 摘要',
    ),
    latestAttempt: adaptAcceptanceSmokeWorkflowAttempt(
      acceptanceValue(raw, 'latest_attempt', 'Workflow Smoke 摘要'),
    ),
  }
}

const adaptAcceptanceSmokeValidationSummary = (value: unknown): AcceptanceSmokeValidationSummary => {
  const label = 'Live Validate 摘要'
  const raw = acceptanceRecord(value, label)
  return {
    ...adaptAcceptanceSmokeEvidenceSummary(raw),
    storedTargetSha256: acceptanceString(raw, 'stored_target_sha256', label, true),
    currentTargetSha256: acceptanceString(raw, 'current_target_sha256', label, true),
    validatedDeploymentId: acceptanceNullableString(raw, 'validated_deployment_id', label),
  }
}

const adaptAcceptanceSmokeSnapshot = (value: unknown): AcceptanceSmokeSnapshot | null => {
  if (value === null || value === undefined) return null
  const label = 'Deployment 验收快照'
  const raw = acceptanceRecord(value, label)
  const operations = acceptanceArray(raw, 'operations', label).map((operation) => {
    if (typeof operation !== 'string' || !operation.trim()) throw new Error(`${label} operations 含无效值`)
    return operation
  })
  return {
    id: acceptanceString(raw, 'id', label),
    probeKind: acceptanceString(raw, 'probe_kind', label),
    promptProfile: raw.prompt_profile == null ? null : acceptanceRecord(raw.prompt_profile, '烟测策略'),
    status: acceptanceString(raw, 'status', label),
    operations,
    targetSha256: acceptanceString(raw, 'target_sha256', label, true),
    targetMatches: acceptanceBoolean(raw, 'target_matches', label),
    fresh: acceptanceBoolean(raw, 'fresh', label),
    capturedAt: acceptanceString(raw, 'captured_at', label),
    expiresAt: acceptanceString(raw, 'expires_at', label),
  }
}

const adaptAcceptanceSmokeDeploymentSummary = (value: unknown): AcceptanceSmokeDeploymentSmokeSummary => {
  const label = 'Deployment Smoke 摘要'
  const raw = acceptanceRecord(value, label)
  const operations = acceptanceArray(raw, 'operations', label)
  return {
    applicable: acceptanceBoolean(raw, 'applicable', label),
    reasonCode: acceptanceString(raw, 'reason_code', label),
    operations: operations.map((value) => {
      const operationLabel = 'Deployment Smoke operation'
      const operation = acceptanceRecord(value, operationLabel)
      return {
        operation: acceptanceString(operation, 'operation', operationLabel),
        promptProfiles: acceptanceArray(operation, 'prompt_profiles', operationLabel).map(value => {
          const item = acceptanceRecord(value, '策略验收')
          return {
            promptProfile: acceptanceRecord(item.prompt_profile, '烟测策略'),
            evidenceState: acceptanceString(item, 'evidence_state', '策略验收'),
            latestSnapshot: adaptAcceptanceSmokeSnapshot(item.latest_snapshot),
            freshPassedSnapshot: adaptAcceptanceSmokeSnapshot(item.fresh_passed_snapshot),
          }
        }),
        applicable: acceptanceBoolean(operation, 'applicable', operationLabel),
        evidenceState: acceptanceString(operation, 'evidence_state', operationLabel),
        latestSnapshot: adaptAcceptanceSmokeSnapshot(
          acceptanceValue(operation, 'latest_snapshot', operationLabel),
        ),
        freshPassedSnapshot: adaptAcceptanceSmokeSnapshot(
          acceptanceValue(operation, 'fresh_passed_snapshot', operationLabel),
        ),
        latestAttempt: adaptAcceptanceSmokeDeploymentAttempt(
          acceptanceValue(operation, 'latest_attempt', operationLabel),
        ),
        action: adaptAcceptanceSmokeAction(
          acceptanceValue(operation, 'action', operationLabel),
        ),
      }
    }),
  }
}

export const adaptAcceptanceSmokeRow = (value: unknown): AcceptanceSmokeRow => {
  const rowLabel = '验收与烟测记录'
  const raw = acceptanceRecord(value, rowLabel)
  const subject = acceptanceString(raw, 'subject', rowLabel)
  if (subject !== 'workflow' && subject !== 'deployment') {
    throw new Error(`验收与烟测响应包含未知 subject：${subject}`)
  }
  const actions = acceptanceArray(raw, 'actions', rowLabel).map(adaptAcceptanceSmokeAction)
  if (subject === 'workflow') {
    const associationLabel = 'Workflow 关联证据'
    const association = acceptanceRecord(
      acceptanceValue(raw, 'association', rowLabel),
      associationLabel,
    )
    const deploymentValue = acceptanceValue(raw, 'deployment', rowLabel)
    const deployment = deploymentValue === null
      ? null
      : adaptAcceptanceSmokeDeploymentRef(deploymentValue)
    return {
      subject,
      workflow: adaptAcceptanceSmokeWorkflowRef(
        acceptanceValue(raw, 'workflow', rowLabel),
      ),
      deployment,
      association: {
        level: acceptanceString(association, 'level', associationLabel),
        deploymentId: acceptanceNullableString(association, 'deployment_id', associationLabel),
        profileMatches: acceptanceBoolean(association, 'profile_matches', associationLabel),
        validatedDeploymentMatches: acceptanceBoolean(association, 'validated_deployment_matches', associationLabel),
        hasSmokeAttempt: acceptanceBoolean(association, 'has_smoke_attempt', associationLabel),
      },
      validation: adaptAcceptanceSmokeValidationSummary(
        acceptanceValue(raw, 'validation', rowLabel),
      ),
      smoke: adaptAcceptanceSmokeWorkflowSummary(
        acceptanceValue(raw, 'smoke', rowLabel),
      ),
      actions,
    } satisfies AcceptanceSmokeWorkflowRow
  }
  if (subject === 'deployment') {
    const summaryLabel = 'Workflow 反向关联摘要'
    const workflowSummary = acceptanceRecord(
      acceptanceValue(raw, 'workflow_association_summary', rowLabel),
      summaryLabel,
    )
    return {
      subject,
      deployment: adaptAcceptanceSmokeDeploymentReadiness(
        acceptanceValue(raw, 'deployment', rowLabel),
      ),
      health: adaptAcceptanceSmokeSnapshot(
        acceptanceValue(raw, 'health', rowLabel),
      ),
      deploymentSmoke: adaptAcceptanceSmokeDeploymentSummary(
        acceptanceValue(raw, 'deployment_smoke', rowLabel),
      ),
      workflowAssociationSummary: {
        profileMatchCount: acceptanceInteger(workflowSummary, 'profile_match_count', summaryLabel),
        compatibleOnlyCount: acceptanceInteger(workflowSummary, 'compatible_only_count', summaryLabel),
        validatedCount: acceptanceInteger(workflowSummary, 'validated_count', summaryLabel),
        smokeHistoryCount: acceptanceInteger(workflowSummary, 'smoke_history_count', summaryLabel),
        freshValidationCount: acceptanceInteger(workflowSummary, 'fresh_validation_count', summaryLabel),
        driftedValidationCount: acceptanceInteger(workflowSummary, 'drifted_validation_count', summaryLabel),
        freshSmokeCount: acceptanceInteger(workflowSummary, 'fresh_smoke_count', summaryLabel),
        unresolvedSmokeCount: acceptanceInteger(workflowSummary, 'unresolved_smoke_count', summaryLabel),
      },
      actions,
    } satisfies AcceptanceSmokeDeploymentRow
  }
  throw new Error(`验收与烟测响应包含未知 subject：${subject}`)
}

export const adaptAcceptanceSmokePage = (value: unknown): AcceptanceSmokePage => {
  const label = '验收与烟测分页响应'
  const raw = acceptanceRecord(value, label)
  return {
    count: acceptanceInteger(raw, 'count', label),
    next: acceptanceNullableString(raw, 'next', label),
    previous: acceptanceNullableString(raw, 'previous', label),
    results: acceptanceArray(raw, 'results', label).map(adaptAcceptanceSmokeRow),
  }
}

export const adaptModelArtifact = (value: unknown): ModelArtifact => { const raw = platformRecord(value); return {
  ...entity(raw), profileVersion: string(raw.profile_version), role: string(raw.role), logicalName: string(raw.logical_name),
  filename: string(raw.filename), storageUri: string(raw.storage_uri), sha256: string(raw.sha256),
  inventoryFolder: string(raw.inventory_folder), inventoryNodeType: string(raw.inventory_node_type),
  inventoryInputName: string(raw.inventory_input_name),
  sizeBytes: nullableNumber(raw.size_bytes), status: string(raw.status), verifiedSnapshot: string(raw.verified_snapshot),
  verifiedAt: string(raw.verified_at), verifiedBy: string(raw.verified_by),
} }

export const adaptPromptFieldDefinition = (value: unknown): PromptFieldDefinition => { const raw = platformRecord(value); return {
  ...entity(raw), key: string(raw.key), label: string(raw.label), category: string(raw.category),
  description: string(raw.description), impact: string(raw.impact), valueType: string(raw.value_type) as PromptFieldValueType,
  defaultValue: raw.default_value, previewExample: raw.preview_example, valueOptions: strings(raw.value_options), isRequired: boolean(raw.is_required),
  isActive: boolean(raw.is_active), displayOrder: number(raw.display_order),
} }

const adaptPromptProfileChannelLimits = (value: unknown) => Object.fromEntries(
  Object.entries(platformRecord(value)).map(([channel, candidate]) => {
    const row = platformRecord(candidate)
    return [channel, {
      maxChars: number(row.max_chars),
      warnAtChars: nullableNumber(row.warn_at_chars),
    }]
  }),
)

export const adaptPromptProfileVersion = (value: unknown): PromptProfileVersion => { const raw = platformRecord(value); return {
  ...entity(raw), profileKey: string(raw.profile_key), version: number(raw.version), name: string(raw.name),
  description: string(raw.description), modelFamily: string(raw.model_family), operation: string(raw.operation),
  mode: string(raw.mode), sourceProjectionKey: string(raw.source_projection_key),
  sourceProjectionConfig: object(raw.source_projection_config),
  officialReference: string(raw.official_reference), channelLimits: adaptPromptProfileChannelLimits(raw.channel_limits), status: string(raw.status),
  reviewState: string(raw.review_state), contractSchemaVersion: number(raw.contract_schema_version),
  contractSizeBytes: number(raw.contract_size_bytes), contractSha256: string(raw.contract_sha256),
  fieldRuleCount: number(raw.field_rule_count), publishedAt: string(raw.published_at), publishedBy: string(raw.published_by),
} }

export const adaptPromptProfileFieldRule = (value: unknown): PromptProfileFieldRule => { const raw = platformRecord(value); return {
  ...entity(raw), profileVersion: string(raw.profile_version), fieldDefinition: string(raw.field_definition),
  fieldKey: string(raw.field_key), fieldLabel: string(raw.field_label), fieldValueType: string(raw.field_value_type),
  sourceSelector: string(raw.source_selector), projectionPrimitive: string(raw.projection_primitive), channel: string(raw.channel), position: number(raw.position),
  labelStyle: string(raw.label_style), separator: string(raw.separator), required: boolean(raw.required),
  omitIfEmpty: boolean(raw.omit_if_empty), maxChars: number(raw.max_chars),
  generationMode: string(raw.generation_mode), generationSourceSelector: string(raw.generation_source_selector),
  generationContextSelectors: strings(raw.generation_context_selectors),
  generationInstruction: string(raw.generation_instruction), generationOutputSemantics: string(raw.generation_output_semantics),
  generationRequired: boolean(raw.generation_required), languagePolicy: string(raw.language_policy),
  lengthUnit: string(raw.length_unit), generationMin: nullableNumber(raw.generation_min),
  generationMax: nullableNumber(raw.generation_max), generationValidatorKeys: strings(raw.generation_validator_keys),
} }

export const adaptPromptProfileBinding = (value: unknown): PromptProfileBinding => { const raw = platformRecord(value); return {
  ...entity(raw), key: string(raw.key), name: string(raw.name), operation: string(raw.operation),
  modelFamily: string(raw.model_family), mode: string(raw.mode), modelProfileVersion: string(raw.model_profile_version),
  modelIdentifier: string(raw.model_identifier), promptProfileVersion: string(raw.prompt_profile_version),
  promptProfileName: string(raw.prompt_profile_name), promptProfileVersionNumber: number(raw.prompt_profile_version_number),
  promptProfileStatus: string(raw.prompt_profile_status),
  promptProfileKey: string(raw.prompt_profile_key), promptProfileContractSha256: string(raw.prompt_profile_contract_sha256),
  priority: number(raw.priority), isActive: boolean(raw.is_active),
} }

export const adaptPromptProfileOptions = (value: unknown): PromptProfileOptions => { const raw = platformRecord(value); const choice = (item: unknown) => { const row = platformRecord(item); return { value: string(row.value), label: string(row.label) } }; const contextChoice = (item: unknown) => { const row = platformRecord(item); return { ...choice(row), description: string(row.description) } }; const generationSourceChoice = (item: unknown) => { const row = platformRecord(item); return {
  ...choice(row), scopeType: string(row.scope_type), group: string(row.group), groupLabel: string(row.group_label),
  description: string(row.description), availability: string(row.availability), scope: string(row.scope),
  dialogueExposure: string(row.dialogue_exposure), crossShotExposure: string(row.cross_shot_exposure), cardinality: string(row.cardinality),
  status: string(row.status), generationModes: strings(row.generation_modes),
} }; const primitiveChoice = (item: unknown) => { const row = platformRecord(item); return { ...contextChoice(row), engineKey: string(row.engine_key), contributionSourceSelectors: strings(row.contribution_source_selectors), renderingTemplateKeys: requiredStringArray(row, 'rendering_template_keys', '原语模板对应目录（请更新平台服务并重新读取）') } }; return {
  operations: requiredStringArray(raw, 'operations', 'PromptProfile options'),
  modelFamilies: requiredStringArray(raw, 'model_families', 'PromptProfile options'),
  modes: requiredStringArray(raw, 'modes', 'PromptProfile options'),
  fieldValueTypes: requiredArray(raw, 'field_value_types', 'PromptProfile options').map(choice),
  fieldCategories: requiredArray(raw, 'field_categories', 'PromptProfile options').map(choice),
  customEntryFields: requiredStringArray(raw, 'custom_entry_fields', 'PromptProfile options'),
  fieldSystemDefaults: requiredObject(raw, 'field_system_defaults', 'PromptProfile options'),
  ruleSystemDefaults: requiredObject(raw, 'rule_system_defaults', 'PromptProfile options'),
  sourceSelectors: requiredArray(raw, 'source_selectors', 'PromptProfile options').map(choice),
  channels: requiredArray(raw, 'channels', 'PromptProfile options').map(choice),
  labelStyles: requiredArray(raw, 'label_styles', 'PromptProfile options').map(choice),
  separators: requiredArray(raw, 'separators', 'PromptProfile options').map(choice),
  generationModes: requiredArray(raw, 'generation_modes', 'PromptProfile options').map(choice),
  generationSourceSelectors: requiredArray(raw, 'generation_source_selectors', 'PromptProfile options').map(generationSourceChoice),
  generationContextSelectors: requiredArray(raw, 'generation_context_selectors', 'PromptProfile options').map(contextChoice),
  generationOutputSemantics: requiredArray(raw, 'generation_output_semantics', 'PromptProfile options').map(choice),
  sourceProjectionKeys: requiredArray(raw, 'source_projection_keys', 'PromptProfile options').map(choice),
  sourceProjectionPrimitives: requiredArray(raw, 'source_projection_primitives', 'PromptProfile options').map(primitiveChoice),
  referenceRenderingFields: requiredArray(raw, 'reference_rendering_fields', 'PromptProfile options').map((item) => {
    const row = platformRecord(item)
    const key = acceptanceString(row, 'key', '参考渲染字段')
    const label = acceptanceString(row, 'label', '参考渲染字段')
    const placeholders = requiredArray(row, 'placeholders', '参考渲染字段')
    if (placeholders.some((token) => typeof token !== 'string' || !/^[a-z][a-z0-9_]*$/.test(token)) || new Set(placeholders).size !== placeholders.length) {
      throw new Error('参考渲染字段占位符目录无效或重复')
    }
    const help = requiredObject(row, 'placeholder_help', '参考渲染帮助（请确认后端已更新并重新读取）')
    if (Object.keys(help).length !== placeholders.length || placeholders.some(token => !Object.prototype.hasOwnProperty.call(help, token as string))) {
      throw new Error('占位符帮助目录不完整，请更新平台服务后重新读取')
    }
    const placeholderHelp = Object.fromEntries((placeholders as string[]).map(token => {
      const detail = platformRecord(help[token])
      if (typeof detail.example !== 'string') throw new Error('占位符示例数据缺失')
      return [token, { label: acceptanceString(detail, 'label', '占位符帮助'), description: acceptanceString(detail, 'description', '占位符帮助'), example: detail.example }]
    }))
    const example = requiredObject(row, 'example', '参考渲染示例')
    if (typeof example.result !== 'string') throw new Error('参考渲染示例结果缺失')
    return { key, label, placeholders: placeholders as string[], placeholderHelp,
      description: acceptanceString(row, 'description', '参考渲染帮助'),
      assembly: acceptanceString(row, 'assembly', '参考渲染帮助'),
      requirement: acceptanceString(row, 'requirement', '参考渲染帮助'),
      example: {template: acceptanceString(example, 'template', '参考渲染示例'), result: example.result},
    }
  }),
  languagePolicies: requiredArray(raw, 'language_policies', 'PromptProfile options').map(choice),
  lengthUnits: requiredArray(raw, 'length_units', 'PromptProfile options').map(choice),
  generationValidators: requiredArray(raw, 'generation_validators', 'PromptProfile options').map(choice),
  modelProfileVersions: requiredArray(raw, 'model_profile_versions', 'PromptProfile options').map((item) => { const row = platformRecord(item); return {
    id: string(row.id), profileKey: string(row.profile_key), version: number(row.version),
    modelIdentifier: string(row.model_identifier), modality: string(row.modality),
  } }),
} }

export const adaptPromptProfilePreview = (value: unknown): PromptProfilePreview => { const raw = platformRecord(value); const rawChannels = platformRecord(raw.channels); const rawAudit = platformRecord(raw.channel_audit); return {
  schemaVersion: number(raw.schema_version), promptProfileVersionId: string(raw.prompt_profile_version_id),
  profileKey: string(raw.profile_key), version: number(raw.version), contractSha256: string(raw.contract_sha256),
  channels: Object.fromEntries(Object.entries(rawChannels).map(([key, item]) => [key, string(item)])),
  channelLimits: adaptPromptProfileChannelLimits(raw.channel_limits),
  channelAudit: Object.fromEntries(Object.entries(rawAudit).map(([key, item]) => [key, object(item)])),
  warnings: strings(raw.warnings),
  structuredFields: object(raw.structured_fields), trace: Array.isArray(raw.trace) ? raw.trace.map(object) : [],
  sourceProjection: object(raw.source_projection),
  ephemeral: boolean(raw.ephemeral), previewScope: string(raw.preview_scope),
  baseContractSha256: string(raw.base_contract_sha256),
  executedStages: Object.fromEntries(Object.entries(platformRecord(raw.executed_stages)).map(([key, item]) => [key, boolean(item)])),
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
export const adaptWorkflowSmokeFixtureSchema = (value: unknown): WorkflowSmokeFixtureSchema => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('工作流输入 Schema 必须是对象')
  const raw = value as Raw
  if (raw.schema_version !== 2) throw new Error('工作流输入 Schema 版本不受支持')
  if (typeof raw.available !== 'boolean') throw new Error('工作流输入 Schema 缺少 available')
  const scalar = (candidate: unknown, field: string): string | number | boolean => {
    if (typeof candidate === 'string' || typeof candidate === 'boolean') return candidate
    if (typeof candidate === 'number' && Number.isFinite(candidate)) return candidate
    throw new Error(`工作流输入 Schema 的 ${field} 不是标量`)
  }
  const finite = (candidate: unknown, field: string): number | null => {
    if (candidate === null || candidate === undefined) return null
    if (typeof candidate !== 'number' || !Number.isFinite(candidate)) throw new Error(`工作流输入 Schema 的 ${field} 不是有限数字`)
    return candidate
  }
  const cardinality = (candidate: unknown, field: string): number => {
    if (!Number.isSafeInteger(candidate) || Number(candidate) < 0) throw new Error(`工作流输入 Schema 的 ${field} 不是非负整数`)
    return Number(candidate)
  }
  if (!Array.isArray(raw.parameters) || !Array.isArray(raw.asset_slots)) {
    throw new Error('工作流输入 Schema 缺少 parameters 或 asset_slots')
  }
  const adaptDynamic = (candidate: unknown, field: string) => {
    if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) {
      throw new Error(`工作流输入 Schema 的 ${field} 必须是对象`)
    }
    const dynamicRaw = candidate as Raw
    const picturesRaw = platformRecord(dynamicRaw.pictures)
    const conditioningMode = adaptConditioningModeCapability(dynamicRaw.conditioning_mode)
    const topology = conditioningMode.dynamicReferenceTopology
    if (conditioningMode.inputKind !== 'classified_references' || !topology) {
      throw new Error(`${field} 必须声明完整的 classified_references conditioning mode`)
    }
    const mode = string(dynamicRaw.mode)
    if (!mode || mode !== conditioningMode.key) throw new Error(`${field}.mode 与 conditioning mode 不一致`)
    const categories = Array.isArray(picturesRaw.categories) ? picturesRaw.categories.map(string) : []
    const allowedCategories = new Set(['character', 'scene', 'prop', 'continuity_frame'])
    if (!categories.length || categories.some((category) => !allowedCategories.has(category))) {
      throw new Error(`${field}.pictures.categories 无效`)
    }
    return {
      mode,
      conditioningMode,
      pictures: {
        minItems: cardinality(picturesRaw.min_items, `${field}.pictures.min_items`),
        maxItems: cardinality(picturesRaw.max_items, `${field}.pictures.max_items`),
        categories: categories as Array<'character' | 'scene' | 'prop' | 'continuity_frame'>,
      },
      videos: {
        minItems: topology.videos.minimumItems, maxItems: topology.videos.maximumItems,
        purposes: topology.videos.purposes.map((item) => item.key),
        defaultPurposeKey: topology.videos.defaultPurposeKey || '',
        supportsEmbeddedAudio: topology.pairingRules.some((item) => item.allowEmbeddedSource),
        supportsExternalAudio: topology.pairingRules.some((item) => item.allowExternalTarget),
      },
      audios: {
        minItems: topology.audios.minimumItems, maxItems: topology.audios.maximumItems,
        purposes: topology.audios.purposes.map((item) => item.key),
        defaultPurposeKey: topology.audios.defaultPurposeKey || '',
      },
      maximumPhysicalAssets: cardinality(dynamicRaw.maximum_physical_assets, `${field}.maximum_physical_assets`),
    }
  }
  if (!raw.dynamic_reference_modes || typeof raw.dynamic_reference_modes !== 'object' || Array.isArray(raw.dynamic_reference_modes)) {
    throw new Error('工作流输入 Schema 缺少 dynamic_reference_modes')
  }
  const dynamicReferenceModes = Object.fromEntries(Object.entries(raw.dynamic_reference_modes as Raw).map(([key, item]) => {
    const adapted = adaptDynamic(item, `dynamic_reference_modes.${key}`)
    if (adapted.mode !== key) throw new Error(`dynamic_reference_modes.${key} 的 key 不一致`)
    return [key, adapted]
  }))
  const dynamicReferences = raw.dynamic_references == null
    ? null : adaptDynamic(raw.dynamic_references, 'dynamic_references')
  if (dynamicReferences && !dynamicReferenceModes[dynamicReferences.mode]) {
    throw new Error('dynamic_references 未包含在 dynamic_reference_modes 中')
  }
  const groupKeys = new Set<string>()
  const groupSlots = new Set<string>()
  if (raw.asset_groups !== undefined && !Array.isArray(raw.asset_groups)) throw new Error('动态素材组必须为列表')
  const assetGroups = (raw.asset_groups as unknown[] || []).map((value) => {
    const row = platformRecord(value)
    const key = string(row.key)
    const mediaKind = string(row.media_kind)
    const minItems = cardinality(row.min_items, `${key}.min_items`)
    const maxItems = cardinality(row.max_items, `${key}.max_items`)
    const semanticKeys = Array.isArray(row.semantic_keys) ? row.semantic_keys.map(string) : []
    if (!key || groupKeys.has(key) || !['image', 'video', 'audio'].includes(mediaKind)
      || !['ordered_ports', 'reduce'].includes(string(row.wiring))
      || maxItems < 1 || minItems > maxItems || semanticKeys.length !== maxItems) throw new Error('动态素材组合同无效')
    groupKeys.add(key)
    semanticKeys.forEach((semanticKey, index) => {
      const matches = (raw.asset_slots as unknown[]).map(platformRecord).filter((slot) => slot.semantic_key === semanticKey)
      if (!semanticKey || groupSlots.has(semanticKey) || matches.length !== 1
        || matches[0]!.media_kind !== mediaKind || matches[0]!.required !== (index < minItems)) throw new Error('动态素材组与素材槽合同不一致')
      groupSlots.add(semanticKey)
    })
    return { key, mediaKind: mediaKind as 'image' | 'video' | 'audio', minItems, maxItems, semanticKeys, wiring: row.wiring as 'ordered_ports' | 'reduce' }
  })
  return {
    schemaVersion: 2,
    available: raw.available,
    reasonCode: string(raw.reason_code),
    parameters: raw.parameters.map((value, index) => {
      const row = platformRecord(value)
      const type = string(row.type)
      if (!string(row.semantic_key) || !['string', 'integer', 'number', 'boolean', 'enum'].includes(type)) {
        throw new Error(`工作流输入参数 ${index + 1} 的 semantic/type 无效`)
      }
      if (typeof row.required !== 'boolean') throw new Error(`工作流输入参数 ${index + 1} 缺少 required`)
      const alignment = parameterAlignment(row, type, `parameters[${index}]`)
      return {
        semanticKey: string(row.semantic_key), type, required: row.required,
        min: finite(row.min, `parameters[${index}].min`), max: finite(row.max, `parameters[${index}].max`),
        multipleOf: finite(row.multiple_of, `parameters[${index}].multiple_of`),
        ...alignment,
        minLength: finite(row.min_length, `parameters[${index}].min_length`),
        maxLength: finite(row.max_length, `parameters[${index}].max_length`),
        options: Array.isArray(row.options)
          ? row.options.map((item, optionIndex) => scalar(item, `parameters[${index}].options[${optionIndex}]`))
          : (() => { throw new Error(`工作流输入参数 ${index + 1} 的 options 必须是数组`) })(),
        selectionGroup: string(row.selection_group),
        recommendedValue: row.recommended_value == null
          ? null : scalar(row.recommended_value, `parameters[${index}].recommended_value`),
      }
    }),
    assetSlots: raw.asset_slots.map((value, index) => {
      const row = platformRecord(value)
      const mediaKind = string(row.media_kind)
      if (!string(row.semantic_key) || !['image', 'video', 'audio'].includes(mediaKind)) {
        throw new Error(`工作流素材槽 ${index + 1} 的 semantic/media kind 无效`)
      }
      if (typeof row.required !== 'boolean') throw new Error(`工作流素材槽 ${index + 1} 缺少 required`)
      return {
        semanticKey: string(row.semantic_key), inputSlot: cardinality(row.input_slot, `asset_slots[${index}].input_slot`),
        mediaKind: mediaKind as 'image' | 'video' | 'audio', category: string(row.category),
        minItems: cardinality(row.min_items, `asset_slots[${index}].min_items`),
        maxItems: cardinality(row.max_items, `asset_slots[${index}].max_items`), required: row.required,
      }
    }),
    dynamicReferences,
    dynamicReferenceModes,
    assetGroups,
  }
}
export const adaptWorkflowTemplateVersion = (value: unknown, options: { reportInvalidSmokeFixtureForAudit?: boolean } = {}): WorkflowTemplateVersion => { const raw = platformRecord(value); const result: WorkflowTemplateVersion = {
  ...entity(raw), template: string(raw.template), templateName: string(raw.template_name), templateSlug: string(raw.template_slug),
  outputKind: string(raw.output_kind), version: number(raw.version), provider: string(raw.provider),
  modelProfileVersion: string(raw.model_profile_version), status: string(raw.status) as WorkflowTemplateVersion['status'],
  validationState: string(raw.validation_state) as WorkflowTemplateVersion['validationState'],
  validationMessage: string(raw.validation_message),
  smokeTestState: string(raw.smoke_test_state) as WorkflowTemplateVersion['smokeTestState'],
  smokeTestMessage: string(raw.smoke_test_message), isCurrent: boolean(raw.is_current), graphSha256: string(raw.graph_sha256),
  graphSizeBytes: number(raw.graph_size_bytes), inputContractSha256: string(raw.input_contract_sha256),
  inputContractSizeBytes: number(raw.input_contract_size_bytes), compilerId: string(raw.compiler_id), compilerVersion: number(raw.compiler_version),
  semanticBindingsSha256: string(raw.semantic_bindings_sha256), modelRequirementsSha256: string(raw.model_requirements_sha256),
  executionContractSha256: string(raw.execution_contract_sha256), validatedDeployment: string(raw.validated_deployment),
  validationEvidenceSha256: string(raw.validation_evidence_sha256), smokeTestEvidenceSha256: string(raw.smoke_test_evidence_sha256),
  semanticBindingCount: number(raw.semantic_binding_count), modelRequirementCount: number(raw.model_requirement_count),
  publishedAt: string(raw.published_at), publishedBy: string(raw.published_by),
}; if ('graph' in raw) result.graph = object(raw.graph); if ('input_contract' in raw) result.inputContract = object(raw.input_contract)
  if ('configuration_links' in raw) result.configurationLinks = adaptWorkflowConfigurationLinks(raw.configuration_links)
  if (Array.isArray(raw.semantic_bindings)) result.semanticBindings = raw.semantic_bindings.map(adaptSemanticBinding)
  if (Array.isArray(raw.model_requirements)) result.modelRequirements = raw.model_requirements.map(adaptModelRequirement)
  if ('validation_evidence' in raw) result.validationEvidence = object(raw.validation_evidence)
  if ('smoke_test_evidence' in raw) result.smokeTestEvidence = object(raw.smoke_test_evidence)
  if ('smoke_fixture_schema' in raw) {
    try { result.smokeFixtureSchema = adaptWorkflowSmokeFixtureSchema(raw.smoke_fixture_schema) }
    catch (cause) {
      if (!options.reportInvalidSmokeFixtureForAudit) throw cause
      result.smokeFixtureSchemaError = cause instanceof Error ? cause.message : '工作流输入 Schema 无法解析'
    }
  }
  return result
}

export const adaptWorkflowTemplateVersionForAudit = (value: unknown): WorkflowTemplateVersion => (
  adaptWorkflowTemplateVersion(value, { reportInvalidSmokeFixtureForAudit: true })
)

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

export const adaptWorkflowProjector = (value: unknown): WorkflowProjector => {
  const raw = platformRecord(value)
  const result = {
    id: string(raw.id),
    key: string(raw.key),
    name: string(raw.name),
    description: string(raw.description),
    isActive: acceptanceBoolean(raw, 'is_active', 'workflow projector'),
    createdAt: string(raw.created_at),
    updatedAt: string(raw.updated_at),
  }
  if (!result.id || !result.key || !result.name) {
    throw new Error('workflow projector returned an incomplete family identity')
  }
  return result
}

export const adaptWorkflowProjectorVersion = (value: unknown): WorkflowProjectorVersion => {
  const raw = platformRecord(value)
  const status = string(raw.status)
  const outputKind = string(raw.output_kind)
  const definition = object(raw.definition)
  const result: WorkflowProjectorVersion = {
    id: string(raw.id), projector: string(raw.projector), projectorKey: string(raw.projector_key),
    version: number(raw.version), engineKey: string(raw.engine_key), engineVersion: number(raw.engine_version),
    providerDriverType: string(raw.provider_driver_type), operation: string(raw.operation),
    outputKind: outputKind as WorkflowProjectorVersion['outputKind'], definition,
    definitionSchemaVersion: number(raw.definition_schema_version),
    definitionSizeBytes: number(raw.definition_size_bytes), definitionSha256: string(raw.definition_sha256),
    status: status as WorkflowProjectorVersion['status'], isCurrent: acceptanceBoolean(raw, 'is_current', 'workflow projector version'),
    publishedAt: string(raw.published_at), publishedBy: string(raw.published_by),
    createdAt: string(raw.created_at), updatedAt: string(raw.updated_at),
  }
  if (
    !result.id || !result.projector || !result.projectorKey || result.version < 1
    || !result.engineKey || result.engineVersion < 1 || result.providerDriverType !== 'comfyui'
    || result.operation !== 'workflow.import' || !['', 'image', 'video', 'audio', 'text'].includes(outputKind)
    || !['draft', 'published', 'retired'].includes(status) || !Object.keys(definition).length
    || result.definitionSchemaVersion < 1 || result.definitionSizeBytes < 2
    || !SAVED_WORKFLOW_SHA256.test(result.definitionSha256)
    || (result.isCurrent && status !== 'published')
  ) {
    throw new Error('workflow projector version returned an invalid immutable contract')
  }
  return result
}

export const adaptWorkflowProjectorRecommendation = (value: unknown): WorkflowProjectorRecommendation => {
  const raw = platformRecord(value)
  const evidenceRaw = requiredObject(raw, 'source_evidence', 'workflow projector recommendation')
  const catalogRaw = requiredObject(raw, 'catalog', 'workflow projector recommendation')
  const inputsRaw = requiredObject(catalogRaw, 'inputs_by_node_type', 'workflow projector recommendation catalog')
  const outputKind = string(raw.output_kind)
  const nodeTypes = requiredArray(catalogRaw, 'node_types', 'workflow projector recommendation catalog').map((value) => {
    const row = platformRecord(value)
    return {
      value: string(row.value),
      count: number(row.count),
      primaryOutput: acceptanceBoolean(row, 'primary_output', 'workflow projector recommendation node type'),
    }
  })
  const inputsByNodeType = Object.fromEntries(Object.entries(inputsRaw).map(([nodeType, values]) => {
    if (!Array.isArray(values) || values.some((item) => typeof item !== 'string' || !item)) {
      throw new Error('workflow projector recommendation input catalog is invalid')
    }
    return [nodeType, values.map(String)]
  }))
  const observedEdges = requiredArray(catalogRaw, 'observed_edges', 'workflow projector recommendation catalog').map((value) => {
    const row = platformRecord(value)
    return {
      fromNodeType: string(row.from_node_type),
      outputIndex: number(row.output_index),
      toNodeType: string(row.to_node_type),
      inputName: string(row.input_name),
      recommended: acceptanceBoolean(row, 'recommended', 'workflow projector recommendation edge'),
    }
  })
  const parameterTargets = requiredArray(catalogRaw, 'parameter_targets', 'workflow projector recommendation catalog').map((value) => {
    const row = platformRecord(value)
    return {
      semanticKey: string(row.semantic_key),
      nodeType: string(row.node_type),
      inputName: string(row.input_name),
      valueType: string(row.value_type),
    }
  })
  const warnings = requiredArray(raw, 'warnings', 'workflow projector recommendation').map(string)
  const result: WorkflowProjectorRecommendation = {
    schemaVersion: number(raw.schema_version),
    persistence: string(raw.persistence) as WorkflowProjectorRecommendation['persistence'],
    requiresHumanConfirmation: acceptanceBoolean(
      raw, 'requires_human_confirmation', 'workflow projector recommendation',
    ) as true,
    engineKey: string(raw.engine_key),
    engineVersion: number(raw.engine_version),
    providerDriverType: string(raw.provider_driver_type),
    operation: string(raw.operation),
    outputKind: outputKind as WorkflowProjectorRecommendation['outputKind'],
    definition: object(raw.definition),
    sourceEvidence: {
      candidateSourceId: string(evidenceRaw.candidate_source_id),
      sourceScopeSchemaVersion: number(evidenceRaw.source_scope_schema_version),
      sourceScopeSha256: string(evidenceRaw.source_scope_sha256),
      sourceSha256: string(evidenceRaw.source_sha256),
      sourcePathSha256: string(evidenceRaw.source_path_sha256),
      objectInfoSchemaSha256: string(evidenceRaw.object_info_schema_sha256),
      convertedGraphSha256: string(evidenceRaw.converted_graph_sha256),
      primaryCanvasNodeId: string(evidenceRaw.primary_canvas_node_id),
      primaryApiNodeId: string(evidenceRaw.primary_api_node_id),
      primaryNodeType: string(evidenceRaw.primary_node_type),
    },
    catalog: { nodeTypes, inputsByNodeType, observedEdges, parameterTargets },
    warnings,
    recommendationSha256: string(raw.recommendation_sha256),
  }
  const hashes = [
    result.sourceEvidence.sourceScopeSha256,
    result.sourceEvidence.sourceSha256,
    result.sourceEvidence.sourcePathSha256,
    result.sourceEvidence.objectInfoSchemaSha256,
    result.sourceEvidence.convertedGraphSha256,
    result.recommendationSha256,
  ]
  if (
    result.schemaVersion !== 1 || result.persistence !== 'unsaved_draft'
    || result.requiresHumanConfirmation !== true || !['generic.fixed', 'generic.references'].includes(result.engineKey)
    || result.engineVersion !== 1 || result.providerDriverType !== 'comfyui'
    || result.operation !== 'workflow.import'
    || !['image', 'video', 'audio', 'text'].includes(outputKind)
    || !Object.keys(result.definition).length || !result.sourceEvidence.candidateSourceId
    || result.sourceEvidence.sourceScopeSchemaVersion < 1
    || !result.sourceEvidence.primaryCanvasNodeId || !result.sourceEvidence.primaryApiNodeId
    || !result.sourceEvidence.primaryNodeType || hashes.some((hash) => !SAVED_WORKFLOW_SHA256.test(hash))
    || !nodeTypes.length || nodeTypes.some((row) => !row.value || row.count < 1)
    || new Set(nodeTypes.map((row) => row.value)).size !== nodeTypes.length
    || Object.keys(inputsByNodeType).some((nodeType) => !nodeTypes.some((row) => row.value === nodeType))
    || observedEdges.some((row) => !row.fromNodeType || row.outputIndex < 0 || !row.toNodeType || !row.inputName)
    || parameterTargets.some((row) => !row.semanticKey || !row.nodeType || !row.inputName)
    || warnings.some((warning) => !warning)
  ) throw new Error('workflow projector recommendation returned an invalid evidence contract')
  return result
}

const WORKFLOW_PROJECTOR_EDITOR_V1_FIELDS = new Map<string, { control: string; readOnly: boolean }>([
  ['schema_version', { control: 'readonly', readOnly: true }],
  ['priority', { control: 'integer', readOnly: false }],
  ['detector.fallback', { control: 'boolean', readOnly: false }],
  ['detector.allow_other_nodes', { control: 'boolean', readOnly: false }],
  ['detector.node_counts', { control: 'node_count_map', readOnly: false }],
  ['detector.optional_node_counts', { control: 'node_range_map', readOnly: false }],
  ['detector.input_equals', { control: 'input_equals_list', readOnly: false }],
  ['output_nodes', { control: 'output_map', readOnly: false }],
  ['model_selectors', { control: 'model_selector_list', readOnly: false }],
  ['node_roles', { control: 'node_role_map', readOnly: false }],
  ['required_edges', { control: 'edge_list', readOnly: false }],
  ['parameter_policies', { control: 'parameter_policy_list', readOnly: false }],
  ['asset_slots', { control: 'asset_slot_list', readOnly: false }],
  ['adapters', { control: 'adapter_contract', readOnly: true }],
  ['projection', { control: 'projection_config', readOnly: false }],
  ['$', { control: 'advanced_json', readOnly: false }],
])
const WORKFLOW_PROJECTOR_PROJECTION_V1_FIELDS = new Map<string, Map<string, { control: string; readOnly: boolean }>>([
  ['generic_fixed', new Map([
    ['projection.config.image_resolution', { control: 'image_resolution', readOnly: false }],
  ])],
  ['generic_references', new Map([
    ['projection.config.groups', { control: 'asset_group_list', readOnly: false }],
  ])],
  ['generic_classified', new Map([
    ['projection.config', { control: 'classified_reference_config', readOnly: false }],
  ])],
])

export const adaptWorkflowProjectorOptions = (value: unknown): WorkflowProjectorOptions => {
  const raw = platformRecord(value)
  const adaptChoiceRows = (values: unknown, label: string, allowBlank = false) => {
    if (!Array.isArray(values)) throw new Error(`${label} must be an array`)
    const rows = values.map((value) => {
      const row = adaptFormCatalogOption(value)
      if ((!allowBlank && !row.value) || !row.label) {
        throw new Error(`${label} contains an invalid choice`)
      }
      return row
    })
    if (!rows.length || new Set(rows.map((row) => row.value)).size !== rows.length) {
      throw new Error(`${label} is empty or contains duplicate values`)
    }
    return rows
  }
  const choiceRows = (key: string, allowBlank = false) => adaptChoiceRows(
    requiredArray(raw, key, 'workflow projector options'),
    `workflow projector options ${key}`,
    allowBlank,
  )
  const adaptEditorField = (value: unknown, expectedFields: Map<string, { control: string; readOnly: boolean }>) => {
    const row = platformRecord(value)
    const result = {
      path: string(row.path),
      label: string(row.label),
      help: string(row.help),
      control: string(row.control),
      required: acceptanceBoolean(row, 'required', 'workflow projector editor field'),
      readOnly: acceptanceBoolean(row, 'read_only', 'workflow projector editor field'),
    }
    const expected = expectedFields.get(result.path)
    if (
      !result.path || !result.label || !result.help || !result.control
      || !expected || result.control !== expected.control
      || result.required !== true || result.readOnly !== expected.readOnly
    ) {
      throw new Error('workflow projector editor field is incomplete')
    }
    return result
  }
  const engines = requiredArray(raw, 'engines', 'workflow projector options').map((value) => {
    const row = platformRecord(value)
    const adapterContract = requiredObject(row, 'adapter_contract', 'workflow projector engine option')
    const template = requiredObject(row, 'creation_template', 'workflow projector engine option')
    const templateDefinition = requiredObject(template, 'definition', 'workflow projector creation template')
    const templateAdapters = requiredObject(templateDefinition, 'adapters', 'workflow projector creation template')
    const templateProjection = requiredObject(templateDefinition, 'projection', 'workflow projector creation template')
    const result = {
      key: string(row.key),
      version: number(row.version),
      label: string(row.label),
      description: string(row.description),
      projectionKind: string(row.projection_kind),
      adapterContract: {
        compiler: string(adapterContract.compiler),
        output: string(adapterContract.output),
        dynamic: string(adapterContract.dynamic),
      },
      lockedFields: requiredArray(row, 'locked_fields', 'workflow projector engine option').map(string),
      projectionFields: [] as WorkflowProjectorOptions['engines'][number]['projectionFields'],
      creationTemplate: {
        providerDriverType: string(template.provider_driver_type), operation: string(template.operation),
        outputKind: string(template.output_kind) as WorkflowProjectorVersion['outputKind'], definition: templateDefinition,
      },
    }
    if (result.creationTemplate.providerDriverType !== 'comfyui' || result.creationTemplate.operation !== 'workflow.import'
      || result.creationTemplate.outputKind !== '' || templateDefinition.schema_version !== raw.definition_schema_version
      || templateProjection.kind !== result.projectionKind
      || ['compiler','output','dynamic'].some(key => templateAdapters[key] !== adapterContract[key])) {
      throw new Error('workflow projector creation template disagrees with its registered engine')
    }
    const expectedProjectionFields = WORKFLOW_PROJECTOR_PROJECTION_V1_FIELDS.get(result.projectionKind)
    if (!expectedProjectionFields) throw new Error('workflow projector engine projection is unsupported by editor schema v1')
    result.projectionFields = requiredArray(
      row, 'projection_fields', 'workflow projector engine option',
    ).map((field) => adaptEditorField(field, expectedProjectionFields))
    if (
      !result.key || result.version < 1 || !result.label || !result.description || !result.projectionKind
      || !result.adapterContract.compiler || !result.adapterContract.output || !result.adapterContract.dynamic
      || new Set(result.lockedFields).size !== result.lockedFields.length
      || result.projectionFields.length !== expectedProjectionFields.size
      || new Set(result.projectionFields.map((field) => field.path)).size !== result.projectionFields.length
    ) throw new Error('workflow projector engine option is invalid')
    return result
  })
  const engineIdentities = engines.map((item) => `${item.key}\u0000${item.version}`)
  if (new Set(engineIdentities).size !== engineIdentities.length) {
    throw new Error('workflow projector engine options contain duplicate identities')
  }
  const adapterRaw = requiredObject(raw, 'adapters', 'workflow projector options')
  const adapterKinds = ['compiler', 'output', 'dynamic'] as const
  if (
    Object.keys(adapterRaw).length !== adapterKinds.length
    || Object.keys(adapterRaw).some((kind) => !adapterKinds.includes(kind as typeof adapterKinds[number]))
  ) throw new Error('workflow projector adapter catalog must contain only compiler/output/dynamic')
  const adapterChoices = (kind: typeof adapterKinds[number]) => {
    const values = adapterRaw[kind]
    if (!Array.isArray(values)) throw new Error(`workflow projector adapter catalog ${kind} is invalid`)
    const rows = values.map((value) => {
      const row = adaptFormCatalogOption(value)
      if (!row.value || !row.label) throw new Error(`workflow projector adapter ${kind} is invalid`)
      return row
    })
    if (!rows.length || new Set(rows.map((row) => row.value)).size !== rows.length) {
      throw new Error(`workflow projector adapter ${kind} is empty or contains duplicate values`)
    }
    return rows
  }
  const adapters: WorkflowProjectorOptions['adapters'] = {
    compiler: adapterChoices('compiler'),
    output: adapterChoices('output'),
    dynamic: adapterChoices('dynamic'),
  }
  for (const engine of engines) {
    for (const kind of adapterKinds) {
      if (!adapters[kind].some((row) => row.value === engine.adapterContract[kind])) {
        throw new Error(`workflow projector engine ${engine.key}/v${engine.version} references an unknown ${kind} adapter`)
      }
    }
  }
  const editorRaw = requiredObject(raw, 'definition_editor', 'workflow projector options')
  const choicesRaw = requiredObject(editorRaw, 'choices', 'workflow projector definition editor')
  const limitsRaw = requiredObject(editorRaw, 'limits', 'workflow projector definition editor')
  const sections = requiredArray(editorRaw, 'sections', 'workflow projector definition editor').map((value) => {
    const row = platformRecord(value)
    const result = {
      key: string(row.key),
      label: string(row.label),
      description: string(row.description),
      fields: requiredArray(row, 'fields', 'workflow projector editor section').map(
        (field) => adaptEditorField(field, WORKFLOW_PROJECTOR_EDITOR_V1_FIELDS),
      ),
    }
    if (!result.key || !result.label || !result.description || !result.fields.length) {
      throw new Error('workflow projector editor section is incomplete')
    }
    return result
  })
  if (!sections.length || new Set(sections.map((row) => row.key)).size !== sections.length) {
    throw new Error('workflow projector editor sections are empty or duplicated')
  }
  const editorFields = sections.flatMap((section) => section.fields)
  if (
    editorFields.length !== WORKFLOW_PROJECTOR_EDITOR_V1_FIELDS.size
    || new Set(editorFields.map((field) => field.path)).size !== editorFields.length
  ) throw new Error('workflow projector editor schema v1 field set is incomplete or duplicated')
  const editorChoice = (key: string) => adaptChoiceRows(
    choicesRaw[key], `workflow projector editor choice ${key}`,
  )
  const limits = {
    priorityMin: number(limitsRaw.priority_min),
    priorityMax: number(limitsRaw.priority_max),
    nodeCountMax: number(limitsRaw.node_count_max),
    inputEqualsMaxItems: number(limitsRaw.input_equals_max_items),
    modelSelectorsMaxItems: number(limitsRaw.model_selectors_max_items),
    requiredEdgesMaxItems: number(limitsRaw.required_edges_max_items),
    parameterPoliciesMaxItems: number(limitsRaw.parameter_policies_max_items),
    assetSlotsMaxItems: number(limitsRaw.asset_slots_max_items),
  }
  const result: WorkflowProjectorOptions = {
    imageResolution: adaptResolutionOptions(raw.image_resolution),
    definitionSchemaVersion: number(raw.definition_schema_version),
    editorSchemaVersion: number(raw.editor_schema_version),
    engines,
    providerDriverTypes: choiceRows('provider_driver_types'),
    operations: choiceRows('operations'),
    outputKinds: choiceRows('output_kinds', true),
    adapters,
    definitionEditor: {
      sections,
      choices: {
        cardinalities: editorChoice('cardinalities'),
        parameterPolicies: editorChoice('parameter_policies'),
        artifactRoles: editorChoice('artifact_roles'),
        mediaKinds: editorChoice('media_kinds'),
        outputKinds: editorChoice('output_kinds'),
        contractSources: editorChoice('contract_sources'),
        assetGroupWirings: engines.some((engine) => engine.key === 'generic.references') ? editorChoice('asset_group_wirings') : [],
        roleSelectors: editorChoice('role_selectors'),
        scalarTypes: editorChoice('scalar_types'),
      },
      limits,
    },
  }
  if (
    result.definitionSchemaVersion < 1 || result.editorSchemaVersion !== 1 || !engines.length
    || limits.priorityMin < 0 || limits.priorityMax < limits.priorityMin || limits.nodeCountMax < 1
    || Object.values(limits).some((value) => !Number.isSafeInteger(value))
  ) {
    throw new Error('workflow projector options are incomplete')
  }
  return result
}

const SAVED_WORKFLOW_SHA256 = /^[0-9a-f]{64}$/
const SAVED_WORKFLOW_OUTPUT_KINDS = new Set(['image', 'video', 'audio', 'text'])
const SAVED_WORKFLOW_MODEL_MODALITIES = new Set([
  'text', 'image', 'video', 'audio', 'multimodal',
])

const adaptSavedWorkflowProfileCandidate = (value: unknown) => {
  const raw = platformRecord(value)
  const version = number(raw.version)
  const status = string(raw.status)
  const modality = string(raw.modality)
  const modelProfileVersionId = string(raw.model_profile_version_id)
  const profileKey = string(raw.profile_key)
  const targetDeploymentId = string(raw.target_deployment_id)
  const eligible = acceptanceBoolean(raw, 'eligible', 'saved workflow model candidate')
  const liveValidateReady = acceptanceBoolean(raw, 'live_validate_ready', 'saved workflow model candidate')
  const eligibleOutputNodeIds = strings(raw.eligible_output_node_ids)
  const rejectionCodes = strings(raw.rejection_codes)
  const liveValidateRejectionCodes = strings(raw.live_validate_rejection_codes)
  if (
    !Number.isSafeInteger(version)
    || version < 1
    || !modelProfileVersionId
    || !profileKey
    || !['draft', 'published', 'retired'].includes(status)
    || !SAVED_WORKFLOW_MODEL_MODALITIES.has(modality)
    || eligibleOutputNodeIds.some((id) => !/^(0|[1-9][0-9]{0,17})$/.test(id))
    || new Set(eligibleOutputNodeIds).size !== eligibleOutputNodeIds.length
    || (eligible && (rejectionCodes.length > 0 || eligibleOutputNodeIds.length === 0))
    || (!eligible && eligibleOutputNodeIds.length > 0)
    || (liveValidateReady && (!eligible || !targetDeploymentId || liveValidateRejectionCodes.length > 0))
  ) {
    throw new Error('saved workflow discovery returned an invalid model candidate')
  }
  return {
    modelProfileVersionId,
    profileKey,
    version,
    status: status as 'draft' | 'published' | 'retired',
    modality: modality as 'text' | 'image' | 'video' | 'audio' | 'multimodal',
    targetDeploymentId,
    eligible,
    liveValidateReady,
    eligibleOutputNodeIds,
    rejectionCodes,
    liveValidateRejectionCodes,
  }
}

const savedWorkflowCount = (value: unknown, field: string): number => {
  if (!Number.isSafeInteger(value) || Number(value) < 0) {
    throw new Error(`saved workflow discovery returned an invalid ${field}`)
  }
  return Number(value)
}

const adaptSavedWorkflowSource = (value: unknown) => {
  const raw = platformRecord(value)
  const status = string(raw.status)
  if (!['succeeded', 'failed'].includes(status)) {
    throw new Error('saved workflow discovery returned an invalid source status')
  }
  const sourceScopeSchemaVersion = savedWorkflowCount(raw.source_scope_schema_version, 'source scope schema version')
  const sourceScopeSha256 = string(raw.source_scope_sha256)
  const sourceDeploymentId = string(raw.source_deployment_id)
  const providerId = string(raw.provider_id)
  const runtimeHostId = string(raw.runtime_host_id)
  const deploymentCount = savedWorkflowCount(raw.deployment_count, 'deployment count')
  if (
    sourceScopeSchemaVersion < 1
    || deploymentCount < 1
    || !sourceDeploymentId
    || !providerId
    || !runtimeHostId
    || (status === 'succeeded' && !SAVED_WORKFLOW_SHA256.test(sourceScopeSha256))
    || (status === 'failed' && sourceScopeSha256 && !SAVED_WORKFLOW_SHA256.test(sourceScopeSha256))
  ) {
    throw new Error('saved workflow discovery omitted source identity')
  }
  return {
    sourceScopeSchemaVersion,
    sourceScopeSha256,
    sourceDeploymentId,
    providerId,
    runtimeHostId,
    sourceLabel: string(raw.source_label),
    deploymentCount,
    status: status as 'succeeded' | 'failed',
    itemCount: savedWorkflowCount(raw.item_count, 'source item count'),
    errorCode: string(raw.error_code),
    errorMessage: string(raw.error_message),
  }
}

const adaptSavedWorkflowOutputCandidate = (value: unknown) => {
  const raw = platformRecord(value)
  const canvasNodeId = string(raw.canvas_node_id)
  const nodeType = string(raw.node_type)
  const suggestedOutputKind = string(raw.suggested_output_kind)
  if (!canvasNodeId || !nodeType || (suggestedOutputKind !== '' && !SAVED_WORKFLOW_OUTPUT_KINDS.has(suggestedOutputKind))) {
    throw new Error('saved workflow discovery returned an invalid output candidate')
  }
  return {
    canvasNodeId,
    nodeType,
    suggestedOutputKind: suggestedOutputKind as '' | 'image' | 'video' | 'audio' | 'text',
  }
}

const adaptSavedWorkflowDiscoveryItem = (value: unknown) => {
  const raw = platformRecord(value)
  const matchStatus = string(raw.match_status)
  const sourceKey = string(raw.source_key)
  const sourceDeploymentId = string(raw.source_deployment_id)
  const sourceScopeSchemaVersion = savedWorkflowCount(
    raw.source_scope_schema_version,
    'item source scope schema version',
  )
  const sourceScopeSha256 = string(raw.source_scope_sha256)
  const sourceProviderId = string(raw.source_provider_id)
  const sourceRuntimeHostId = string(raw.source_runtime_host_id)
  const sourcePath = string(raw.source_path)
  const sourceSha256 = string(raw.source_sha256)
  const sizeBytes = savedWorkflowCount(raw.size_bytes, 'item size')
  const suggestedImportKind = string(raw.suggested_import_kind)
  const itemErrorCode = string(raw.error_code)
  const candidateManaged = acceptanceBoolean(raw, 'candidate_managed', 'saved workflow discovery item')
  const candidateSourceId = string(raw.candidate_source_id)
  const analysisStatus = string(raw.analysis_status)
  const admissionTier = string(raw.admission_tier)
  const actionability = string(raw.actionability)
  if (!['matched', 'ambiguous', 'unmatched', 'blocked'].includes(matchStatus)) {
    throw new Error('saved workflow discovery returned an invalid match status')
  }
  if (
    sourceScopeSchemaVersion < 1
    || !SAVED_WORKFLOW_SHA256.test(sourceScopeSha256)
    || !sourceKey
    || !sourceDeploymentId
    || !sourceProviderId
    || !sourceRuntimeHostId
    || !sourcePath
    || !['complete', 'blocked', 'failed'].includes(analysisStatus)
    || !['candidate', 'generic_fixed', 'projected_dynamic'].includes(admissionTier)
    || !['candidate_only', 'ready_for_executable_import'].includes(actionability)
    || (candidateManaged && !candidateSourceId)
    || (!candidateManaged && candidateSourceId.length > 0)
    || (
      itemErrorCode
        ? (
          matchStatus !== 'blocked'
          || Boolean(sourceSha256 && !SAVED_WORKFLOW_SHA256.test(sourceSha256))
          || Boolean(suggestedImportKind && !['template_version', 'execution_snapshot'].includes(suggestedImportKind))
        )
        : (
          !SAVED_WORKFLOW_SHA256.test(sourceSha256)
          || sizeBytes < 1
          || !['template_version', 'execution_snapshot'].includes(suggestedImportKind)
        )
    )
  ) {
    throw new Error('saved workflow discovery omitted authoritative source identity')
  }
  const suggested = platformRecord(raw.suggested_template)
  const matchActions = Array.isArray(raw.match_actions)
    ? raw.match_actions.map((value) => ({ code: string(platformRecord(value).code) }))
    : []
  const recoveryActions = Array.isArray(raw.recovery_actions)
    ? raw.recovery_actions.map((value) => {
      const row = platformRecord(value)
      return { code: string(row.code), label: string(row.label) || undefined }
    })
    : []
  const outputCandidates = Array.isArray(raw.output_candidates)
    ? raw.output_candidates.map(adaptSavedWorkflowOutputCandidate)
    : []
  const modelProfileCandidates = Array.isArray(raw.model_profile_candidates)
    ? raw.model_profile_candidates.map(adaptSavedWorkflowProfileCandidate)
    : []
  const outputCandidateIds = new Set(outputCandidates.map((candidate) => candidate.canvasNodeId))
  if (
    outputCandidateIds.size !== outputCandidates.length
    || modelProfileCandidates.some((candidate) => (
      candidate.eligibleOutputNodeIds.some((nodeId) => !outputCandidateIds.has(nodeId))
    ))
  ) {
    throw new Error('saved workflow discovery returned inconsistent output candidates')
  }
  const matchedOutputKind = string(raw.matched_output_kind)
  const suggestedOutputKind = string(suggested.output_kind)
  if (
    matchActions.some((action) => !action.code)
    || recoveryActions.some((action) => !action.code)
    || (matchedOutputKind && !SAVED_WORKFLOW_OUTPUT_KINDS.has(matchedOutputKind))
    || (Object.keys(suggested).length && !SAVED_WORKFLOW_OUTPUT_KINDS.has(suggestedOutputKind))
  ) {
    throw new Error('saved workflow discovery returned invalid recovery or output metadata')
  }
  const adapted = {
    sourceScopeSchemaVersion,
    sourceScopeSha256,
    sourceKey,
    sourceDeploymentId,
    sourceProviderId,
    sourceRuntimeHostId,
    sourcePath,
    sourceSha256,
    sizeBytes,
    sourceWorkflowId: string(raw.source_workflow_id),
    suggestedImportKind: suggestedImportKind as 'template_version' | 'execution_snapshot' | '',
    candidateManaged,
    candidateSourceId,
    imported: boolean(raw.imported),
    sourceId: string(raw.source_id),
    workflowVersionId: string(raw.workflow_version_id),
    templateId: string(raw.template_id),
    contractUpgradeRequired: boolean(raw.contract_upgrade_required),
    errorCode: itemErrorCode,
    errorMessage: string(raw.error_message),
    analysisStatus: analysisStatus as 'complete' | 'blocked' | 'failed',
    analysisCode: string(raw.analysis_code),
    analysisMessage: string(raw.analysis_message),
    admissionTier: admissionTier as 'candidate' | 'generic_fixed' | 'projected_dynamic',
    actionability: actionability as 'candidate_only' | 'ready_for_executable_import',
    recoveryActions,
    matchStatus: matchStatus as 'matched' | 'ambiguous' | 'unmatched' | 'blocked',
    matchReasonCodes: strings(raw.match_reason_codes),
    matchActions,
    outputCandidates,
    modelProfileCandidates,
    matchedModelProfileVersionId: string(raw.matched_model_profile_version_id),
    matchedTargetDeploymentId: string(raw.matched_target_deployment_id),
    matchedPrimaryOutputNodeId: string(raw.matched_primary_output_node_id),
    matchedOutputKind: matchedOutputKind as 'image' | 'video' | 'audio' | 'text' | '',
    suggestedTemplate: Object.keys(suggested).length ? {
      useExisting: boolean(suggested.use_existing),
      templateId: string(suggested.template_id),
      name: string(suggested.name),
      slug: string(suggested.slug),
      outputKind: suggestedOutputKind as 'image' | 'video' | 'audio' | 'text',
    } : null,
    preview: raw.preview && typeof raw.preview === 'object'
      ? adaptComfySavedWorkflowPreview(raw.preview)
      : null,
  }
  if (
    (matchStatus === 'matched' && matchActions.length > 0)
    || (
      matchStatus === 'matched'
      && suggestedImportKind === 'template_version'
      && (
        !adapted.matchedModelProfileVersionId
        || !adapted.matchedOutputKind
      )
    )
    || (
      matchStatus === 'matched'
      && adapted.matchedPrimaryOutputNodeId
      && !modelProfileCandidates.some((candidate) => (
        candidate.modelProfileVersionId === adapted.matchedModelProfileVersionId
        && candidate.eligibleOutputNodeIds.includes(adapted.matchedPrimaryOutputNodeId)
        && (
          !candidate.liveValidateReady
          || candidate.targetDeploymentId === adapted.matchedTargetDeploymentId
        )
      ))
    )
  ) {
    throw new Error('saved workflow discovery returned an invalid matched result')
  }
  return adapted
}

export const adaptComfySavedWorkflowScan = (value: unknown): ComfySavedWorkflowScan => {
  const raw = platformRecord(value)
  const effectiveLimits = platformRecord(raw.effective_limits)
  const positiveInteger = (value: unknown): number | null => (
    Number.isSafeInteger(value) && Number(value) > 0 ? Number(value) : null
  )
  const positiveNumber = (value: unknown): number | null => (
    typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null
  )
  const effectiveScanMaxItems = positiveInteger(effectiveLimits.scan_max_items)
  const effectiveScanMaxSeconds = positiveNumber(effectiveLimits.scan_max_seconds)
  const effectiveRequestMaxBytes = positiveInteger(effectiveLimits.request_max_bytes)
  const rawBatchMaximum = effectiveLimits.batch_max_items
  const effectiveBatchMaxItems = positiveInteger(rawBatchMaximum)
  const effectiveBatchMaxSeconds = positiveNumber(effectiveLimits.batch_max_seconds)
  const savedWorkflowDirectory = string(effectiveLimits.saved_workflow_directory)
  const status = string(raw.status)
  if (!['complete', 'partial', 'failed'].includes(status)) {
    throw new Error('saved workflow discovery returned an invalid status')
  }
  if (!Array.isArray(raw.sources) || !Array.isArray(raw.items)) {
    throw new Error('saved workflow discovery omitted source or item arrays')
  }
  const sources = raw.sources.map(adaptSavedWorkflowSource)
  const items = raw.items
    ? raw.items.map(adaptSavedWorkflowDiscoveryItem)
    : []
  if (new Set(items.map((item) => item.sourceKey)).size !== items.length) {
    throw new Error('saved workflow discovery returned duplicate source keys')
  }
  const schemaVersion = savedWorkflowCount(raw.schema_version, 'schema version')
  const sourceCount = savedWorkflowCount(raw.source_count, 'source count')
  const succeededSourceCount = savedWorkflowCount(raw.succeeded_source_count, 'succeeded source count')
  const failedSourceCount = savedWorkflowCount(raw.failed_source_count, 'failed source count')
  const matchedCount = savedWorkflowCount(raw.matched_count, 'matched count')
  const reviewCount = savedWorkflowCount(raw.review_count, 'review count')
  const sourceIdentity = (source: typeof sources[number]) => (
    `${source.sourceScopeSchemaVersion}:${source.sourceScopeSha256}:${source.sourceDeploymentId}`
  )
  const itemSourceIdentity = (item: typeof items[number]) => (
    `${item.sourceScopeSchemaVersion}:${item.sourceScopeSha256}:${item.sourceDeploymentId}`
  )
  const uniqueSources = new Set(sources.map(sourceIdentity))
  const uniqueSourceDeployments = new Set(sources.map((source) => source.sourceDeploymentId))
  const succeededSources = sources.filter((source) => source.status === 'succeeded')
  const failedSources = sources.filter((source) => source.status === 'failed')
  const succeededSourceKeys = new Set(succeededSources.map(sourceIdentity))
  const sourcesByIdentity = new Map(sources.map((source) => [sourceIdentity(source), source]))
  if (
    schemaVersion < 1
    || sources.length !== sourceCount
    || uniqueSources.size !== sources.length
    || uniqueSourceDeployments.size !== sources.length
    || succeededSources.length !== succeededSourceCount
    || failedSources.length !== failedSourceCount
    || succeededSourceCount + failedSourceCount !== sourceCount
    || items.filter((item) => item.matchStatus === 'matched').length !== matchedCount
    || items.filter((item) => item.matchStatus !== 'matched').length !== reviewCount
    || items.some((item) => {
      const identity = itemSourceIdentity(item)
      const source = sourcesByIdentity.get(identity)
      return (
        !succeededSourceKeys.has(identity)
        || !source
        || item.sourceProviderId !== source.providerId
        || item.sourceRuntimeHostId !== source.runtimeHostId
      )
    })
    || succeededSources.some((source) => (
      items.filter((item) => itemSourceIdentity(item) === sourceIdentity(source)).length
      !== source.itemCount
    ))
    || failedSources.some((source) => source.itemCount !== 0)
    || (status === 'complete' && failedSourceCount !== 0)
    || (status === 'partial' && (succeededSourceCount === 0 || failedSourceCount === 0))
    || (status === 'failed' && succeededSourceCount !== 0)
  ) {
    throw new Error('saved workflow discovery returned inconsistent counts or source ownership')
  }
  return {
    schemaVersion,
    status: status as 'complete' | 'partial' | 'failed',
    sourceCount,
    succeededSourceCount,
    failedSourceCount,
    matchedCount,
    reviewCount,
    effectiveScanMaxItems,
    effectiveScanMaxSeconds,
    effectiveRequestMaxBytes,
    effectiveBatchMaxItems,
    effectiveBatchMaxSeconds,
    savedWorkflowDirectory,
    sources,
    items,
  }
}

export const adaptComfySavedWorkflowPreview = (value: unknown): ComfySavedWorkflowPreview => {
  const raw = platformRecord(value)
  const scalar = (value: unknown): string | number | boolean => (
    typeof value === 'string' || typeof value === 'boolean'
      ? value
      : typeof value === 'number' && Number.isFinite(value) ? value : ''
  )
  const admissionTier = string(raw.admission_tier)
  const actionability = string(raw.actionability)
  const contractDraftId = string(raw.contract_draft_id)
  const contractDraftRevision = savedWorkflowCount(
    raw.contract_draft_revision,
    'contract draft revision',
  )
  if (
    !['candidate', 'generic_fixed', 'projected_dynamic'].includes(admissionTier)
    || !['select_primary_output', 'candidate_only', 'ready_for_executable_import'].includes(actionability)
    || Boolean(contractDraftId) !== (contractDraftRevision > 0)
  ) {
    throw new Error('saved workflow preview returned invalid admission or contract draft identity')
  }
  return {
    schemaVersion: number(raw.schema_version), sourcePath: string(raw.source_path), sourceSha256: string(raw.source_sha256),
    sizeBytes: number(raw.size_bytes), sourceWorkflowId: string(raw.source_workflow_id),
    primaryOutputNodeIds: strings(raw.primary_output_node_ids),
    primaryApiNodeIds: strings(raw.primary_api_node_ids),
    outputCandidates: Array.isArray(raw.output_candidates)
      ? raw.output_candidates.map(adaptSavedWorkflowOutputCandidate)
      : [],
    modelDependencies: Array.isArray(raw.model_dependencies) ? raw.model_dependencies.map((value) => { const item = platformRecord(value); return {
      nodeId: string(item.node_id), nodeType: string(item.node_type), inputName: string(item.input_name), filename: string(item.filename),
      role: string(item.role), suggestedAlias: string(item.suggested_alias), inventoryFolder: string(item.inventory_folder),
      inventoryNodeType: string(item.inventory_node_type), inventoryInputName: string(item.inventory_input_name),
      availableInObjectInfo: boolean(item.available_in_object_info),
      artifactMatches: Array.isArray(item.artifact_matches) ? item.artifact_matches.map((value) => { const match = platformRecord(value); return {
        id: string(match.id), status: string(match.status), sha256: string(match.sha256), sizeBytes: nullableNumber(match.size_bytes),
        verifiedSnapshot: string(match.verified_snapshot),
      } }) : [],
    } }) : [],
    assetDependencies: Array.isArray(raw.asset_dependencies) ? raw.asset_dependencies.map((value) => { const item = platformRecord(value); return {
      nodeId: string(item.node_id), nodeType: string(item.node_type), inputName: string(item.input_name),
      mediaKind: string(item.media_kind) as 'image' | 'video' | 'audio',
      valueType: string(item.value_type) as 'asset_image' | 'asset_video' | 'asset_audio',
      suggestedSemanticKey: string(item.suggested_semantic_key), inputSlot: number(item.input_slot),
      assetCategory: string(item.asset_category) as 'image' | 'video' | 'audio', isRequired: boolean(item.is_required),
      minItems: number(item.min_items), maxItems: number(item.max_items),
    } }) : [],
    conversionReport: object(raw.conversion_report),
    objectInfoSchemaSha256: string(raw.object_info_schema_sha256),
    convertedGraphSha256: string(raw.converted_graph_sha256),
    configurableParameters: Array.isArray(raw.configurable_parameters)
      ? raw.configurable_parameters.map((value) => {
        const item = platformRecord(value); const target = platformRecord(item.target)
        const type = string(item.type) as 'integer' | 'number' | 'enum'
        const alignment = parameterAlignment(
          item,
          type,
          `configurable_parameters.${string(item.semantic_key) || '?'}`,
        )
        return {
          semanticKey: string(item.semantic_key), sourceValue: scalar(item.source_value),
          recommendedSafeValue: scalar(item.recommended_safe_value),
          defaultValue: item.default === null || item.default === undefined ? null : scalar(item.default),
          min: nullableNumber(item.min), max: nullableNumber(item.max),
          schemaMin: nullableNumber(item.schema_min), schemaMax: nullableNumber(item.schema_max),
          multipleOf: nullableNumber(item.multiple_of),
          ...alignment,
          options: Array.isArray(item.options) ? item.options.map(scalar) : [],
          type,
          selectionGroup: string(item.selection_group),
          target: {
            nodeId: string(target.node_id), nodeType: string(target.node_type),
            inputName: string(target.input_name),
          },
        }
      }) : [],
    parameterSchemaSha256: string(raw.parameter_schema_sha256),
    structuralContractSha256: string(raw.structural_contract_sha256),
    suggestedInputContract: object(raw.suggested_input_contract),
    suggestedSemanticBindings: Array.isArray(raw.suggested_semantic_bindings) ? raw.suggested_semantic_bindings.map(object) : [],
    suggestedModelRequirements: Array.isArray(raw.suggested_model_requirements) ? raw.suggested_model_requirements.map(object) : [],
    blockers: Array.isArray(raw.blockers) ? raw.blockers.map((value) => { const item = platformRecord(value); return {
      code: string(item.code), detail: string(item.detail),
    } }) : [],
    liveValidateRequirements: Array.isArray(raw.live_validate_requirements)
      ? raw.live_validate_requirements.map((value) => {
        const item = platformRecord(value)
        return { code: string(item.code), detail: string(item.detail), phase: string(item.phase) || undefined }
      })
      : [],
    admissionTier: admissionTier as ComfySavedWorkflowPreview['admissionTier'],
    actionability: actionability as ComfySavedWorkflowPreview['actionability'],
    recoveryActions: Array.isArray(raw.recovery_actions)
      ? raw.recovery_actions.map((value) => {
        const item = platformRecord(value)
        return { code: string(item.code), label: string(item.label) }
      })
      : [],
    contractDraftId,
    contractDraftRevision,
  }
}

export const adaptWorkflowSmokeAssetCandidate = (
  value: unknown,
): WorkflowSmokeAssetCandidate => {
  const raw = platformRecord(value)
  return {
    id: string(raw.id),
    name: string(raw.name),
    backendKind: string(raw.kind),
    mimeType: string(raw.mime_type),
    checksum: string(raw.checksum),
    durationMs: nullableNumber(raw.duration_ms),
    sourceFps: nullableNumber(raw.source_fps),
    hasEmbeddedAudio: boolean(raw.has_audio),
    smokeEligible: boolean(raw.smoke_eligible),
    smokeIneligibleReason: string(raw.smoke_ineligible_reason),
  }
}

export const adaptComfySavedWorkflowBatchResult = (value: unknown): ComfySavedWorkflowBatchResult => {
  const raw = platformRecord(value)
  const deploymentId = string(raw.deployment_id)
  const status = string(raw.status)
  if (!deploymentId || !['complete', 'partial', 'failed'].includes(status) || !Array.isArray(raw.items)) {
    throw new Error('saved workflow batch returned an invalid envelope')
  }
  const items = raw.items.map((value) => { const item = platformRecord(value); const result = string(item.result); const importKind = string(item.import_kind); if (!['created', 'reused', 'failed'].includes(result) || !['candidate', 'template_version', 'execution_snapshot', 'ignored'].includes(importKind) || !string(item.source_path) || !SAVED_WORKFLOW_SHA256.test(string(item.source_sha256))) throw new Error('saved workflow batch returned an invalid item'); return {
      sourceDeploymentId: deploymentId,
      sourcePath: string(item.source_path), sourceSha256: string(item.source_sha256), sourceId: string(item.source_id),
      importKind: importKind as ComfySavedWorkflowBatchResult['items'][number]['importKind'], result: result as 'created' | 'reused' | 'failed',
      code: string(item.code), detail: string(item.detail), workflowVersionId: string(item.workflow_version_id),
      structuralContractSha256: string(item.structural_contract_sha256),
    } })
  const createdCount = savedWorkflowCount(raw.created_count, 'created count')
  const reusedCount = savedWorkflowCount(raw.reused_count, 'reused count')
  const failedCount = savedWorkflowCount(raw.failed_count, 'failed count')
  const actualCreated = items.filter((item) => item.result === 'created').length
  const actualReused = items.filter((item) => item.result === 'reused').length
  const actualFailed = items.filter((item) => item.result === 'failed').length
  if (createdCount !== actualCreated || reusedCount !== actualReused || failedCount !== actualFailed || (status === 'complete' && failedCount !== 0) || (status === 'partial' && (failedCount === 0 || createdCount + reusedCount === 0)) || (status === 'failed' && createdCount + reusedCount !== 0)) {
    throw new Error('saved workflow batch returned inconsistent counts')
  }
  return {
    schemaVersion: savedWorkflowCount(raw.schema_version, 'batch schema version'), deploymentId,
    status: status as ComfySavedWorkflowBatchResult['status'], createdCount,
    reusedCount, failedCount,
    items,
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

export const adaptWorkflowSmokeResolution = (value: unknown): WorkflowSmokeAttemptResolution => { const raw = platformRecord(value); return {
  ...entity(raw), workflowSmokeAttemptId: string(raw.workflow_smoke_attempt_id), revision: number(raw.revision),
  outcome: string(raw.outcome) as WorkflowSmokeAttemptResolution['outcome'], providerRequestIdSha256: string(raw.provider_request_id_sha256),
  evidence: object(raw.evidence), evidenceSchemaVersion: number(raw.evidence_schema_version), evidenceSizeBytes: number(raw.evidence_size_bytes),
  evidenceSha256: string(raw.evidence_sha256), note: string(raw.note), decidedBy: string(raw.decided_by),
} }

export const adaptWorkflowSmokeAttempt = (value: unknown): WorkflowSmokeAttempt => { const raw = platformRecord(value); return {
  ...entity(raw), workflowVersion: string(raw.workflow_version), deployment: string(raw.deployment), createdBy: string(raw.created_by),
  status: string(raw.status), graphSha256: string(raw.graph_sha256), executionContractSha256: string(raw.execution_contract_sha256),
  validationEvidenceSha256: string(raw.validation_evidence_sha256), targetSha256: string(raw.target_sha256),
  fixtureValuesSchemaVersion: number(raw.fixture_values_schema_version), fixtureValuesSizeBytes: number(raw.fixture_values_size_bytes),
  fixtureValuesSha256: string(raw.fixture_values_sha256), fixtureSha256: string(raw.fixture_sha256),
  assetBindings: Array.isArray(raw.asset_bindings) ? raw.asset_bindings.map((value) => { const row = platformRecord(value); return {
    semanticKey: string(row.semantic_key), inputSlot: number(row.input_slot), asset: string(row.asset),
    mediaKind: string(row.media_kind), category: string(row.category), checksum: string(row.checksum),
    mimeType: string(row.mime_type), sizeBytes: number(row.size_bytes), createdAt: string(row.created_at),
  } }) : [],
  resolutions: Array.isArray(raw.resolutions) ? raw.resolutions.map(adaptWorkflowSmokeResolution) : [],
  resolutionRequired: boolean(raw.resolution_required), workerId: string(raw.worker_id),
  submissionStartedAt: string(raw.submission_started_at), submittedAt: string(raw.submitted_at), lastPolledAt: string(raw.last_polled_at),
  nextPollAt: string(raw.next_poll_at), pollCount: number(raw.poll_count), remoteStatus: string(raw.remote_status),
  deadlineStartedAt: string(raw.deadline_started_at), hardDeadlineAt: string(raw.hard_deadline_at), idleDeadlineAt: string(raw.idle_deadline_at),
  lastMeaningfulProgressAt: string(raw.last_meaningful_progress_at), cancelRequestedAt: string(raw.cancel_requested_at),
  cancelSubmittedAt: string(raw.cancel_submitted_at),
  finishedAt: string(raw.finished_at), errorCode: string(raw.error_code), errorMessage: string(raw.error_message),
  result: object(raw.result),
} }

export const adaptModelDeploymentSmokeAttempt = (value: unknown): ModelDeploymentSmokeAttempt => { const raw = platformRecord(value); return {
  promptProfile: raw.prompt_profile == null ? null : acceptanceRecord(raw.prompt_profile, '烟测策略'),
  ...entity(raw), deployment: string(raw.deployment), operation: string(raw.operation), status: string(raw.status),
  requestSha256: string(raw.request_sha256), targetSha256: string(raw.target_sha256), estimatedCost: string(raw.estimated_cost),
  costCurrency: string(raw.cost_currency), providerRequestIdSha256: string(raw.provider_request_id_sha256), workerId: string(raw.worker_id),
  submissionStartedAt: string(raw.submission_started_at), submittedAt: string(raw.submitted_at), lastPolledAt: string(raw.last_polled_at),
  nextPollAt: string(raw.next_poll_at), pollCount: number(raw.poll_count), remoteStatus: string(raw.remote_status),
  hardDeadlineAt: string(raw.hard_deadline_at), idleDeadlineAt: string(raw.idle_deadline_at), cancelRequestedAt: string(raw.cancel_requested_at),
  errorCode: string(raw.error_code), errorMessage: string(raw.error_message), finishedAt: string(raw.finished_at),
  finalSnapshot: string(raw.final_snapshot),
} }

export const adaptConfigDefinition = (value: unknown): ConfigDefinition => { const raw = platformRecord(value); if (!hasOwn(raw, 'has_values') || !Array.isArray(raw.mutable_fields)) throw new Error('ConfigDefinition 响应缺少 has_values 或 mutable_fields 编辑合同'); const configurationClassRaw = string(raw.configuration_class); return {
  ...entity(raw), key: string(raw.key), domain: string(raw.domain),
  configurationClass: configurationClassRaw === 'standard' || configurationClassRaw === 'security_boundary' ? configurationClassRaw : 'unknown',
  configurationClassRaw, valueType: string(raw.value_type),
  description: string(raw.description), unit: string(raw.unit), impactModules: strings(raw.impact_modules),
  restartRequired: boolean(raw.restart_required), allowedScopes: string(raw.allowed_scopes),
  allowedDefaultSources: string(raw.allowed_default_sources), isRequired: boolean(raw.is_required),
  isSecret: boolean(raw.is_secret), isActive: boolean(raw.is_active), needsReview: boolean(raw.needs_review),
  reviewReason: string(raw.review_reason), valueSchema: object(raw.value_schema), initialValue: raw.initial_value,
  hasValues: boolean(raw.has_values), mutableFields: strings(raw.mutable_fields),
} }

export const adaptConfigDefinitionOptions = (value: unknown): ConfigDefinitionOptions => { const raw = platformRecord(value); const systemDefaults = requiredObject(raw, 'system_defaults', 'ConfigDefinition options'); for (const key of ['configuration_class', 'allowed_scopes', 'allowed_default_sources', 'restart_required', 'is_required', 'is_secret', 'is_active', 'needs_review']) {
  if (!hasOwn(systemDefaults, key)) throw new Error(`ConfigDefinition options 的 system_defaults 缺少 ${key}`)
} return {
  configurationClasses: requiredCatalog(raw, 'configuration_classes', 'ConfigDefinition options'),
  valueTypes: requiredCatalog(raw, 'value_types', 'ConfigDefinition options'),
  scopes: requiredCatalog(raw, 'scopes', 'ConfigDefinition options'),
  defaultSources: requiredCatalog(raw, 'default_sources', 'ConfigDefinition options'),
  domains: requiredCatalog(raw, 'domains', 'ConfigDefinition options', true),
  units: requiredCatalog(raw, 'units', 'ConfigDefinition options', true),
  impactModules: requiredCatalog(raw, 'impact_modules', 'ConfigDefinition options', true),
  customEntryFields: requiredStringArray(raw, 'custom_entry_fields', 'ConfigDefinition options'),
  systemDefaults,
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
  changesSecurityBoundaries: typeof raw.changes_security_boundaries === 'boolean' ? raw.changes_security_boundaries : null,
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
  ...entity(raw), host: string(raw.host), createdBy: string(raw.created_by), trigger: string(raw.trigger),
  probeKind: string(raw.probe_kind), requestSnapshot: object(raw.request_snapshot),
  requestSnapshotSchemaVersion: number(raw.request_snapshot_schema_version),
  requestSnapshotSizeBytes: number(raw.request_snapshot_size_bytes), requestSnapshotSha256: string(raw.request_snapshot_sha256),
  status: string(raw.status),
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
  ...entity(raw), project: string(raw.project), sourceAsset: string(raw.source_asset), sourceType: string(raw.source_type), sourceFormat: string(raw.source_format),
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

export const adaptProductionRecipeEditorOptions = (value: unknown): ProductionRecipeEditorOptions => { const raw = platformRecord(value); const result = {
  definitionSchemaVersion: number(raw.definition_schema_version),
  stageOptions: requiredArray(raw, 'stage_options', 'ProductionRecipe options').map((item) => { const row = platformRecord(item); return {
    value: string(row.value), modes: strings(row.modes), requiresRouting: boolean(row.requires_routing),
  } }),
  approvalOptions: requiredCatalog(raw, 'approval_options', 'ProductionRecipe options'),
  routingModeOptions: requiredCatalog(raw, 'routing_mode_options', 'ProductionRecipe options'),
  presets: requiredArray(raw, 'presets', 'ProductionRecipe options').map((item) => { const row = platformRecord(item); return {
    ...adaptFormCatalogOption(row),
    stages: Array.isArray(row.stages) ? row.stages.map(object) : [],
  } }),
}; const stageValues = new Set(result.stageOptions.map((item) => item.value)); if (
  !Number.isSafeInteger(result.definitionSchemaVersion) || result.definitionSchemaVersion < 1
  || !result.stageOptions.length || stageValues.size !== result.stageOptions.length
  || result.stageOptions.some((item) => !item.value || !item.modes.length || item.modes.some((mode) => !mode))
  || !result.presets.length || result.presets.some((item) => !item.value || !item.label || !item.stages.length)
) throw new Error('ProductionRecipe options 的结构版本、阶段或预设目录不完整'); return result }
const adaptProductionPromptProfileRequirement = (value: unknown): ProductionPromptProfileRequirement => {
  const raw = platformRecord(value)
  return {
    operation: string(raw.operation),
    mode: string(raw.mode),
    choices: (Array.isArray(raw.choices) ? raw.choices : []).map((choiceValue) => {
      const choice = platformRecord(choiceValue)
      return {
        bindingId: string(choice.binding_id),
        bindingKey: string(choice.binding_key),
        bindingName: string(choice.binding_name),
        promptProfileVersionId: string(choice.prompt_profile_version_id),
        profileKey: string(choice.profile_key),
        profileVersion: number(choice.profile_version),
        profileName: string(choice.profile_name),
        contractSha256: string(choice.contract_sha256),
        priority: number(choice.priority),
        bindingScope: string(choice.binding_scope),
      }
    }),
  }
}

export const adaptProductionRecipeMediaRouteOptions = (value: unknown): ProductionRecipeMediaRouteOptions => {
  const raw = platformRecord(value)
  if (number(raw.schema_version) !== 2) throw new Error('不支持的媒体 route options 版本')
  return {
    schemaVersion: 2,
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
        conditioningModes: adaptConditioningModes(route.conditioning_modes),
        conditioningContractSha256: string(route.conditioning_contract_sha256),
        mediaOperations: strings(route.media_operations),
        isSelectable: boolean(route.is_selectable),
        reasonCodes: strings(route.reason_codes),
        reasons: strings(route.reasons),
        promptProfileRequirements: (
          Array.isArray(route.prompt_profile_requirements)
            ? route.prompt_profile_requirements
            : []
        ).map(adaptProductionPromptProfileRequirement),
      }
    }),
    bibleImageGeneration: raw.bible_image_generation === null || raw.bible_image_generation === undefined
      ? null
      : adaptProductionPromptProfileRequirement(raw.bible_image_generation),
  }
}
const adaptStageRun = (value: unknown): StageRun => { const raw = platformRecord(value); return {
  ...entity(raw), stageKey: string(raw.stage_key), ordinal: number(raw.ordinal), mode: string(raw.mode), status: string(raw.status),
  approvalRequired: boolean(raw.approval_required), decisionPolicy: string(raw.decision_policy),
  decisionPolicySha256: string(raw.decision_policy_sha256),
  attemptCount: number(raw.attempt_count), inputReference: string(raw.input_reference),
  inputSnapshot: object(raw.input_snapshot), inputSnapshotSha256: string(raw.input_snapshot_sha256),
  outputReference: string(raw.output_reference), outputSnapshot: object(raw.output_snapshot), outputSnapshotSha256: string(raw.output_snapshot_sha256),
  reasonCode: string(raw.reason_code), reason: string(raw.reason),
  routing: object(raw.routing), routingSha256: string(raw.routing_sha256), routingDecision: object(raw.routing_decision),
  routingDecisionSha256: string(raw.routing_decision_sha256), selectedRouteIndex: nullableNumber(raw.selected_route_index),
  approvedEstimatedCost: string(raw.approved_estimated_cost), approvedCostCurrency: string(raw.approved_cost_currency),
  selectionConfirmedAt: string(raw.selection_confirmed_at),
  startedAt: string(raw.started_at), finishedAt: string(raw.finished_at), approvalDecisions: strings(raw.approval_decisions),
  approvalDecisionFacts: (Array.isArray(raw.approval_decision_facts) ? raw.approval_decision_facts : []).map((value) => {
    const fact = platformRecord(value)
    return {
      id: string(fact.id), revision: number(fact.revision), decision: string(fact.decision), comment: string(fact.comment),
      outputSnapshotSha256: string(fact.output_snapshot_sha256), actor: string(fact.actor) as 'human' | 'recipe_policy',
      decisionPolicy: string(fact.decision_policy), decisionPolicySha256: string(fact.decision_policy_sha256),
      decidedBy: string(fact.decided_by), createdAt: string(fact.created_at),
    }
  }),
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
  repairOfAttemptId: string(raw.repair_of_attempt_id), retryFeedbackSha256: string(raw.retry_feedback_sha256),
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
    active: { label: '启用', tone: 'info' }, ready: { label: '就绪', tone: 'success' }, configured: { label: '已配置', tone: 'info' }, published: { label: '已发布', tone: 'info' }, validated: { label: '已校验', tone: 'success' }, confirmed: { label: '已确认', tone: 'success' }, preview: { label: '预览', tone: 'info' }, imported: { label: '已导入', tone: 'info' }, unconfigured: { label: '未配置', tone: 'warning' },
    succeeded: { label: '成功', tone: 'success' }, passed: { label: '通过', tone: 'success' }, fresh: { label: '证据有效', tone: 'success' }, selected: { label: '已选', tone: 'success' }, available: { label: '可用', tone: 'info' }, current: { label: '当前版本', tone: 'success' }, sealed: { label: '已封存', tone: 'success' }, released: { label: '已发行', tone: 'success' },
    running: { label: '运行中', tone: 'info' }, queued: { label: '排队中', tone: 'info' }, submitting: { label: '提交中', tone: 'info' }, submitted: { label: '已提交', tone: 'info' }, cancel_requested: { label: '取消中', tone: 'warning' }, uncertain: { label: '待消歧', tone: 'danger' }, draft: { label: '草稿', tone: 'muted' }, declared: { label: '已声明', tone: 'muted' }, inactive: { label: '停用', tone: 'muted' }, disabled: { label: '禁用', tone: 'muted' }, retired: { label: '已退役', tone: 'muted' }, not_run: { label: '未执行', tone: 'muted' }, not_applicable: { label: '不适用', tone: 'muted' }, unavailable: { label: '不可用', tone: 'warning' },
    pending: { label: '等待', tone: 'muted' }, pending_review: { label: '待审核', tone: 'warning' }, pending_activation: { label: '待激活', tone: 'warning' }, awaiting_approval: { label: '待审批', tone: 'warning' }, needs_review: { label: '需要人工确认', tone: 'warning' }, upgrade_required: { label: '需要升级合同', tone: 'warning' }, unprepared: { label: '尚未准备', tone: 'warning' }, stale: { label: '已过期', tone: 'warning' }, drifted: { label: '证据已漂移', tone: 'danger' }, blocked: { label: '阻断', tone: 'danger' }, missing: { label: '缺失', tone: 'danger' },
    failed: { label: '失败', tone: 'danger' }, error: { label: '异常', tone: 'danger' }, cancelled: { label: '已取消', tone: 'muted' }, invalid: { label: '无效', tone: 'danger' }, ignored: { label: '已忽略', tone: 'muted' }, reused: { label: '已复用', tone: 'info' }, created: { label: '已新建', tone: 'success' },
  }
  return known[normalized] ?? { label: status ? '未知状态' : '未返回状态', tone: 'unknown' }
}
