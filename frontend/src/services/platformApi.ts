import { api, fetchAllPages, unwrap } from './api'
import {
  adaptAcceptanceSmokePage,
  adaptAcceptanceSmokeRow,
  adaptConfigDefinition, adaptConfigDefinitionOptions, adaptConfigResourceOption, adaptConfigRevision, adaptGeneratedArtifact, adaptGenerationAttempt,
  adaptHostSnapshot, adaptLegacyWorkflowTemplate, adaptModelArtifact, adaptModelDeployment,
  adaptModelDeploymentSmokeAttempt, adaptModelProfileRegistryOptions, adaptModelProfileVersion, adaptNodeMetadata, adaptProductionRecipe, adaptProductionRecipeEditorOptions, adaptProductionRecipeMediaRouteOptions, adaptProductionRun,
  adaptPromptFieldDefinition, adaptPromptProfileBinding, adaptPromptProfileFieldRule, adaptPromptProfileOptions,
  adaptPromptProfilePreview, adaptPromptProfileVersion,
  adaptProviderProfile, adaptRecommendation, adaptResolvedConfiguration, adaptRuntimeHost, adaptRuntimeHostProbeAttempt,
  adaptRuntimeReadiness, adaptProductionReadiness, adaptScriptRevision, adaptScriptSource, adaptWorkflowSmokeAttempt, adaptWorkflowTemplateVersion, adaptWorkflowTemplateVersionForAudit,
  adaptWorkflowTemplateCanvasExport,
  adaptComfySavedWorkflowBatchResult, adaptComfySavedWorkflowPreview, adaptComfySavedWorkflowScan,
  adaptWorkflowProjector, adaptWorkflowProjectorOptions, adaptWorkflowProjectorRecommendation, adaptWorkflowProjectorVersion,
  adaptWorkflowSmokeAssetCandidate,
  adaptWorkflowSmokeResolution,
  adaptPromptNormalizationSmokeResolution,
  adaptPlatformLifecycleAuditEvent, adaptSecretAuditEvent, adaptSecretBinding, platformRecord,
} from './platformAdapters'
import type {
  AcceptanceSmokeDeploymentRow, AcceptanceSmokePage, AcceptanceSmokeQuery,
  ArtifactPreview, ConfigDefinition, ConfigDefinitionOptions, ConfigResourceOption, ConfigRevision, GeneratedArtifact, GenerationAttempt, HostCapabilitySnapshot,
  JsonObject, LegacyWorkflowTemplate, ModelArtifact, ModelDeployment,
  ModelDeploymentSmokeAttempt, ModelProfileRegistryOptions, ModelProfileVersion,
  NodeMetadataCatalog, NodeMetadataResponse, ProductionRecipe, ProductionRecipeEditorOptions, ProductionRecipeMediaRouteOptions, ProductionRun, ProductionRunCreateInput,
  PromptFieldDefinition, PromptProfileBinding, PromptProfileFieldRule, PromptProfileOptions, PromptProfilePreview, PromptProfilePreviewRequest, PromptProfileVersion,
  ProductionReadiness, ProviderProfile, RecommendationSnapshot, ResolvedConfiguration, RuntimeHost, RuntimeHostProbeAttempt,
  RuntimeReadiness, ScriptRevision, ScriptSource, WorkflowTemplateCanvasExport, WorkflowTemplateVersion,
  RecommendationDecision,
  PlatformLifecycleAuditEvent, PromptNormalizationSmokeResolution, ResolveUncertainAttemptInput, SecretAuditEvent, SecretBinding,
  SmokeResolutionOutcome, WorkflowSmokeAttempt, WorkflowSmokeAttemptResolution,
  WorkflowSmokeResolutionOutcome,
  WorkflowSmokeFixtureInput,
  BibleImageBindingSelectionInput, RegenerateBibleImagesInput, StoryboardShotReferenceSelectionInput,
  ComfySavedWorkflowBatchItemInput, ComfySavedWorkflowBatchResult, ComfySavedWorkflowPreview, ComfySavedWorkflowScan,
  WorkflowSmokeAssetCandidate,
  RegistryDeletionExecuteInput, RegistryDeletionImpact, RegistryDeletionResourceType,
  WorkflowProjector, WorkflowProjectorOptions, WorkflowProjectorRecommendation, WorkflowProjectorVersion,
} from './platformTypes'
import { adaptRegistryDeletionImpact } from './registryDeletion'
import { buildProductionRunRequestPayload } from './productionRunPayload'
import { loadCompleteNodeMetadata } from './nodeMetadata'
import { shotReferenceBindingsPayload } from './storyboardReferenceBindings'

const body = <T>(payload: unknown): T => unwrap<T>(payload)
const auditOptions = (reasonCode = '') => reasonCode
  ? { headers: { 'X-Audit-Reason-Code': reasonCode } }
  : undefined

export const bibleImageBindingsPayload = (items: BibleImageBindingSelectionInput[]) => items.map((item) => ({
  entity_type: item.entityType,
  entity_id: item.entityId,
  selected_artifact_ids: item.selectedArtifactIds,
  selected_library_assets: item.selectedLibraryAssets.map((asset) => ({
    asset_id: asset.assetId,
    expected_sha256: asset.expectedSha256,
  })),
  anchor_artifact_id: item.anchorArtifactId || null,
  anchor_library_asset_id: item.anchorLibraryAssetId || null,
}))

export const regenerateBibleImagesPayload = (input: RegenerateBibleImagesInput) => ({
  request_id: input.requestId,
  expected_source_snapshot_sha256: input.expectedSourceSnapshotSha256,
  entities: input.entities.map((entity) => ({
    entity_type: entity.entityType,
    entity_id: entity.entityId,
  })),
})

export const platformApi = {
  async getAcceptanceSmokePage(input: AcceptanceSmokeQuery): Promise<AcceptanceSmokePage> {
    const response = await api.get('/acceptance-smoke/', { params: {
      subject: input.subject,
      page: input.page,
      page_size: input.pageSize,
      ...(input.search?.trim() ? { search: input.search.trim() } : {}),
      ...(input.deploymentId?.trim() ? { deployment_id: input.deploymentId.trim() } : {}),
      ...(input.workflowId?.trim() ? { workflow_id: input.workflowId.trim() } : {}),
    } })
    return adaptAcceptanceSmokePage(response.data)
  },
  async listAcceptanceSmokeDeploymentRows(): Promise<AcceptanceSmokeDeploymentRow[]> {
    const rows = (await fetchAllPages<unknown>('/acceptance-smoke/', {
      subject: 'deployment',
      page_size: 200,
    })).map(adaptAcceptanceSmokeRow)
    const deploymentIds = new Set<string>()
    return rows.map((row) => {
      if (row.subject !== 'deployment') throw new Error('模型实例验收响应包含非 Deployment 记录')
      if (deploymentIds.has(row.deployment.id)) throw new Error(`模型实例验收响应包含重复 Deployment：${row.deployment.id}`)
      deploymentIds.add(row.deployment.id)
      return row
    })
  },
  async getProductionReadiness(): Promise<ProductionReadiness> { return adaptProductionReadiness(body((await api.get('/production-readiness/')).data)) },
  async previewRegistryDeletion(resourceType: RegistryDeletionResourceType, objectId: string): Promise<RegistryDeletionImpact> {
    return adaptRegistryDeletionImpact(body((await api.post('/registry-deletion-preview/', {
      resource_type: resourceType,
      object_id: objectId,
    })).data))
  },
  async executeRegistryDeletion(resourceType: RegistryDeletionResourceType, objectId: string, input: RegistryDeletionExecuteInput): Promise<void> {
    await api.post('/registry-deletion-execute/', {
        resource_type: resourceType,
        object_id: objectId,
        expected_impact_sha256: input.expectedImpactSha256,
        selected_dependency_keys: input.selectedDependencyKeys,
        confirmation_phrase: input.confirmationPhrase,
      }, auditOptions(input.reasonCode))
  },
  async listProviders(): Promise<ProviderProfile[]> { return (await fetchAllPages('/provider-profiles/', { page_size: 500 })).map(adaptProviderProfile) },
  async createProvider(payload: JsonObject, reasonCode = ''): Promise<ProviderProfile> { return adaptProviderProfile(body((await api.post('/provider-profiles/', payload, auditOptions(reasonCode))).data)) },
  async updateProvider(id: string, payload: JsonObject, reasonCode = ''): Promise<ProviderProfile> { return adaptProviderProfile(body((await api.patch(`/provider-profiles/${id}/`, payload, auditOptions(reasonCode))).data)) },
  async listModelProfiles(): Promise<ModelProfileVersion[]> { return (await fetchAllPages('/model-profile-versions/', { page_size: 500 })).map(adaptModelProfileVersion) },
  async getModelProfileRegistryOptions(): Promise<ModelProfileRegistryOptions> { return adaptModelProfileRegistryOptions(body((await api.get('/model-profile-versions/options/')).data)) },
  async createModelProfile(payload: JsonObject, reasonCode = ''): Promise<ModelProfileVersion> { return adaptModelProfileVersion(body((await api.post('/model-profile-versions/', payload, auditOptions(reasonCode))).data)) },
  async createNextModelProfile(sourceId: string, payload: JsonObject, reasonCode = ''): Promise<ModelProfileVersion> { return adaptModelProfileVersion(body((await api.post(`/model-profile-versions/${sourceId}/draft-next/`, payload, auditOptions(reasonCode))).data)) },
  async updateModelProfile(id: string, payload: JsonObject, reasonCode = ''): Promise<ModelProfileVersion> { return adaptModelProfileVersion(body((await api.patch(`/model-profile-versions/${id}/`, payload, auditOptions(reasonCode))).data)) },
  async publishModelProfile(id: string, reasonCode = ''): Promise<ModelProfileVersion> { return adaptModelProfileVersion(body((await api.post(`/model-profile-versions/${id}/publish/`, {}, auditOptions(reasonCode))).data)) },
  async retireModelProfile(id: string, reasonCode = ''): Promise<ModelProfileVersion> { return adaptModelProfileVersion(body((await api.post(`/model-profile-versions/${id}/retire/`, {}, auditOptions(reasonCode))).data)) },
  async listDeployments(): Promise<ModelDeployment[]> { return (await fetchAllPages('/model-deployments/', { page_size: 500 })).map(adaptModelDeployment) },
  async createDeployment(payload: JsonObject, reasonCode = ''): Promise<ModelDeployment> { return adaptModelDeployment(body((await api.post('/model-deployments/', payload, auditOptions(reasonCode))).data)) },
  async updateDeployment(id: string, payload: JsonObject, reasonCode = ''): Promise<ModelDeployment> { return adaptModelDeployment(body((await api.patch(`/model-deployments/${id}/`, payload, auditOptions(reasonCode))).data)) },
  async retireDeployment(id: string, reasonCode = ''): Promise<ModelDeployment> { return adaptModelDeployment(body((await api.post(`/model-deployments/${id}/retire/`, {}, auditOptions(reasonCode))).data)) },
  async probeDeployment(id: string): Promise<JsonObject> { return platformRecord(body((await api.post(`/model-deployments/${id}/probe/`, {})).data)) },
  async smokeTestDeployment(id: string, operation: string, promptProfileBindingId?: string): Promise<ModelDeploymentSmokeAttempt> {
    const response = await api.post(`/model-deployments/${id}/smoke-test/`, { operation, ...(promptProfileBindingId ? { prompt_profile_binding_id: promptProfileBindingId } : {}) })
    if (response.status !== 202) throw new Error(`部署烟测必须返回持久任务，实际 HTTP ${response.status}`)
    return adaptModelDeploymentSmokeAttempt(body(response.data))
  },
  async getDeploymentSmokeAttempt(id: string): Promise<ModelDeploymentSmokeAttempt> { return adaptModelDeploymentSmokeAttempt(body((await api.get(`/model-deployment-smoke-attempts/${id}/`)).data)) },
  async listDeploymentSmokeAttempts(params: JsonObject = {}): Promise<ModelDeploymentSmokeAttempt[]> { return (await fetchAllPages('/model-deployment-smoke-attempts/', { ...params, page_size: 100 })).map(adaptModelDeploymentSmokeAttempt) },
  async cancelDeploymentSmokeAttempt(id: string): Promise<ModelDeploymentSmokeAttempt> { return adaptModelDeploymentSmokeAttempt(body((await api.post(`/model-deployment-smoke-attempts/${id}/cancel/`, {})).data)) },
  async listDeploymentEvidence(id: string): Promise<JsonObject[]> { return (await fetchAllPages(`/model-deployments/${id}/evidence/`, { page_size: 500 })).map(platformRecord) },
  async listModelArtifacts(): Promise<ModelArtifact[]> { return (await fetchAllPages('/model-artifacts/', { page_size: 500 })).map(adaptModelArtifact) },
  async createModelArtifact(payload: JsonObject, reasonCode = ''): Promise<ModelArtifact> { return adaptModelArtifact(body((await api.post('/model-artifacts/', payload, auditOptions(reasonCode))).data)) },
  async updateModelArtifact(id: string, payload: JsonObject, reasonCode = ''): Promise<ModelArtifact> { return adaptModelArtifact(body((await api.patch(`/model-artifacts/${id}/`, payload, auditOptions(reasonCode))).data)) },
  async verifyModelArtifact(id: string, hostSnapshotId: string): Promise<ModelArtifact> { return adaptModelArtifact(body((await api.post(`/model-artifacts/${id}/verify/`, { host_snapshot_id: hostSnapshotId })).data)) },

  async listPromptFieldDefinitions(): Promise<PromptFieldDefinition[]> { return (await fetchAllPages('/prompt-field-definitions/', { page_size: 500 })).map(adaptPromptFieldDefinition) },
  async createPromptFieldDefinition(payload: JsonObject, reasonCode = ''): Promise<PromptFieldDefinition> { return adaptPromptFieldDefinition(body((await api.post('/prompt-field-definitions/', payload, auditOptions(reasonCode))).data)) },
  async updatePromptFieldDefinition(id: string, payload: JsonObject, reasonCode = ''): Promise<PromptFieldDefinition> { return adaptPromptFieldDefinition(body((await api.patch(`/prompt-field-definitions/${id}/`, payload, auditOptions(reasonCode))).data)) },
  async listPromptProfileVersions(): Promise<PromptProfileVersion[]> { return (await fetchAllPages('/prompt-profile-versions/', { page_size: 500 })).map(adaptPromptProfileVersion) },
  async createPromptProfileVersion(payload: JsonObject, reasonCode = ''): Promise<PromptProfileVersion> { return adaptPromptProfileVersion(body((await api.post('/prompt-profile-versions/', payload, auditOptions(reasonCode))).data)) },
  async updatePromptProfileVersion(id: string, payload: JsonObject, reasonCode = ''): Promise<PromptProfileVersion> { return adaptPromptProfileVersion(body((await api.patch(`/prompt-profile-versions/${id}/`, payload, auditOptions(reasonCode))).data)) },
  async clonePromptProfileSuccessor(id: string, expectedSourceContractSha256: string, name: string): Promise<PromptProfileVersion> { return adaptPromptProfileVersion(body((await api.post(`/prompt-profile-versions/${id}/clone-successor/`, { expected_source_contract_sha256: expectedSourceContractSha256, name })).data)) },
  async publishPromptProfileVersion(id: string, reasonCode = ''): Promise<PromptProfileVersion> { return adaptPromptProfileVersion(body((await api.post(`/prompt-profile-versions/${id}/publish/`, {}, auditOptions(reasonCode))).data)) },
  async retirePromptProfileVersion(id: string, reasonCode = ''): Promise<PromptProfileVersion> { return adaptPromptProfileVersion(body((await api.post(`/prompt-profile-versions/${id}/retire/`, {}, auditOptions(reasonCode))).data)) },
  async previewPromptProfileVersion(id: string, request: PromptProfilePreviewRequest): Promise<PromptProfilePreview> { return adaptPromptProfilePreview(body((await api.post(`/prompt-profile-versions/${id}/preview/`, {
    sources: request.sources,
    ...(request.expectedBaseContractSha256 ? { expected_base_contract_sha256: request.expectedBaseContractSha256 } : {}),
    ...(request.candidate ? { candidate: request.candidate } : {}),
  })).data)) },
  async getPromptProfileOptions(): Promise<PromptProfileOptions> { return adaptPromptProfileOptions(body((await api.get('/prompt-profile-versions/options/')).data)) },
  async listPromptProfileFieldRules(): Promise<PromptProfileFieldRule[]> { return (await fetchAllPages('/prompt-profile-field-rules/', { page_size: 1000 })).map(adaptPromptProfileFieldRule) },
  async createPromptProfileFieldRule(payload: JsonObject, reasonCode = ''): Promise<PromptProfileFieldRule> { return adaptPromptProfileFieldRule(body((await api.post('/prompt-profile-field-rules/', payload, auditOptions(reasonCode))).data)) },
  async updatePromptProfileFieldRule(id: string, payload: JsonObject, reasonCode = ''): Promise<PromptProfileFieldRule> { return adaptPromptProfileFieldRule(body((await api.patch(`/prompt-profile-field-rules/${id}/`, payload, auditOptions(reasonCode))).data)) },
  async listPromptProfileBindings(params: { model_family?: string; model_profile_version?: string } = {}): Promise<PromptProfileBinding[]> { return (await fetchAllPages('/prompt-profile-bindings/', { ...params, page_size: 50 })).map(adaptPromptProfileBinding) },
  async getModelProfile(id: string): Promise<ModelProfileVersion> { return adaptModelProfileVersion(body((await api.get(`/model-profile-versions/${id}/`)).data)) },
  async createPromptProfileBinding(payload: JsonObject, reasonCode = ''): Promise<PromptProfileBinding> { return adaptPromptProfileBinding(body((await api.post('/prompt-profile-bindings/', payload, auditOptions(reasonCode))).data)) },
  async updatePromptProfileBinding(id: string, payload: JsonObject, reasonCode = ''): Promise<PromptProfileBinding> { return adaptPromptProfileBinding(body((await api.patch(`/prompt-profile-bindings/${id}/`, payload, auditOptions(reasonCode))).data)) },

  async listLegacyWorkflowTemplates(): Promise<LegacyWorkflowTemplate[]> { return (await fetchAllPages('/workflow-templates/', { page_size: 500 })).map(adaptLegacyWorkflowTemplate) },
  async listWorkflowVersions(options: { modelProfileVersion?: string } = {}): Promise<WorkflowTemplateVersion[]> {
    const modelProfileVersion = options.modelProfileVersion?.trim().toLocaleLowerCase('en-US')
    const versions = (await fetchAllPages('/workflow-template-versions/', {
      ...(modelProfileVersion === undefined ? {} : { model_profile_version: modelProfileVersion }),
      page_size: 50,
    })).map((item) => adaptWorkflowTemplateVersion(item))
    if (modelProfileVersion !== undefined && versions.some((item) => item.modelProfileVersion.toLocaleLowerCase('en-US') !== modelProfileVersion)) {
      throw new Error(`工作流列表返回了不属于精确模型版本 ${modelProfileVersion} 的记录`)
    }
    return versions
  },
  async getWorkflowVersion(id: string): Promise<WorkflowTemplateVersion> { return adaptWorkflowTemplateVersion(body((await api.get(`/workflow-template-versions/${id}/`)).data)) },
  async getWorkflowVersionForAudit(id: string): Promise<WorkflowTemplateVersion> { return adaptWorkflowTemplateVersionForAudit(body((await api.get(`/workflow-template-versions/${id}/`)).data)) },
  async getWorkflowTemplateCanvasExport(id: string, deploymentId = ''): Promise<WorkflowTemplateCanvasExport> {
    return adaptWorkflowTemplateCanvasExport(body((await api.get(
      `/workflow-template-versions/${id}/comfyui-workflow/`,
      { params: deploymentId ? { deployment_id: deploymentId } : {} },
    )).data))
  },
  async listWorkflowProjectors(): Promise<WorkflowProjector[]> {
    return (await fetchAllPages('/workflow-projectors/', { page_size: 500 })).map(adaptWorkflowProjector)
  },
  async createWorkflowProjector(payload: JsonObject, reasonCode = ''): Promise<WorkflowProjector> {
    return adaptWorkflowProjector(body((await api.post('/workflow-projectors/', payload, auditOptions(reasonCode))).data))
  },
  async updateWorkflowProjector(id: string, payload: JsonObject, reasonCode = ''): Promise<WorkflowProjector> {
    return adaptWorkflowProjector(body((await api.patch(`/workflow-projectors/${id}/`, payload, auditOptions(reasonCode))).data))
  },
  async listWorkflowProjectorVersions(): Promise<WorkflowProjectorVersion[]> {
    return (await fetchAllPages('/workflow-projector-versions/', { page_size: 500 })).map(adaptWorkflowProjectorVersion)
  },
  async getWorkflowProjectorVersion(id: string): Promise<WorkflowProjectorVersion> {
    return adaptWorkflowProjectorVersion(body((await api.get(`/workflow-projector-versions/${id}/`)).data))
  },
  async getWorkflowProjectorOptions(): Promise<WorkflowProjectorOptions> {
    return adaptWorkflowProjectorOptions(body((await api.get('/workflow-projector-versions/options/')).data))
  },
  async recommendWorkflowProjector(payload: JsonObject): Promise<WorkflowProjectorRecommendation> {
    return adaptWorkflowProjectorRecommendation(body((await api.post('/workflow-projector-versions/recommendation/', payload)).data))
  },
  async createWorkflowProjectorVersion(payload: JsonObject, reasonCode = ''): Promise<WorkflowProjectorVersion> {
    return adaptWorkflowProjectorVersion(body((await api.post('/workflow-projector-versions/', payload, auditOptions(reasonCode))).data))
  },
  async createNextWorkflowProjectorVersion(id: string, payload: JsonObject = {}, reasonCode = ''): Promise<WorkflowProjectorVersion> {
    return adaptWorkflowProjectorVersion(body((await api.post(`/workflow-projector-versions/${id}/draft-next/`, payload, auditOptions(reasonCode))).data))
  },
  async updateWorkflowProjectorVersion(id: string, payload: JsonObject, reasonCode = ''): Promise<WorkflowProjectorVersion> {
    return adaptWorkflowProjectorVersion(body((await api.patch(`/workflow-projector-versions/${id}/`, payload, auditOptions(reasonCode))).data))
  },
  async publishWorkflowProjectorVersion(id: string, reasonCode = ''): Promise<WorkflowProjectorVersion> {
    return adaptWorkflowProjectorVersion(body((await api.post(`/workflow-projector-versions/${id}/publish/`, {}, auditOptions(reasonCode))).data))
  },
  async discardWorkflowProjectorVersion(id: string, reasonCode = ''): Promise<void> {
    await api.post(`/workflow-projector-versions/${id}/discard/`, {}, auditOptions(reasonCode))
  },
  async retireWorkflowProjectorVersion(id: string, reasonCode = ''): Promise<WorkflowProjectorVersion> {
    return adaptWorkflowProjectorVersion(body((await api.post(`/workflow-projector-versions/${id}/retire/`, {}, auditOptions(reasonCode))).data))
  },
  async scanComfySavedWorkflows(): Promise<ComfySavedWorkflowScan> {
    return adaptComfySavedWorkflowScan(body((await api.get(
      '/comfy-workflow-sources/discover/',
    )).data))
  },
  async listWorkflowSmokeAssetCandidates(): Promise<WorkflowSmokeAssetCandidate[]> {
    return (await fetchAllPages(
      '/assets/workflow-smoke-candidates/',
      { page_size: 100 },
    )).map(adaptWorkflowSmokeAssetCandidate)
  },
  async previewComfySavedWorkflow(deploymentId: string, input: {
    sourcePath: string
    expectedSha256: string
    expectedSourceScopeSchemaVersion: number
    expectedSourceScopeSha256: string
    primaryOutputNodeIds?: string[]
    modelProfileVersion?: string | null
  }): Promise<ComfySavedWorkflowPreview> {
    const preview = adaptComfySavedWorkflowPreview(body((await api.post(
      `/model-deployments/${deploymentId}/saved-workflows/preview/`,
      {
        source_path: input.sourcePath,
        expected_sha256: input.expectedSha256,
        expected_source_scope_schema_version: input.expectedSourceScopeSchemaVersion,
        expected_source_scope_sha256: input.expectedSourceScopeSha256,
        primary_output_node_ids: input.primaryOutputNodeIds || [],
        model_profile_version: input.modelProfileVersion || null,
      },
    )).data))
    if ((input.primaryOutputNodeIds?.length || 0) > 0 && !preview.contractDraftId) {
      throw new Error('服务端未返回持久化平台执行合同草稿，不能继续提升工作流版本')
    }
    return preview
  },
  async attestComfySavedWorkflowModelArtifacts(deploymentId: string, input: {
    sourcePath: string
    expectedSha256: string
    expectedSourceScopeSchemaVersion: number
    expectedSourceScopeSha256: string
    primaryOutputNodeIds: string[]
    expectedObjectInfoSchemaSha256: string
    expectedConvertedGraphSha256: string
    modelProfileVersion: string
  }): Promise<RuntimeHostProbeAttempt> {
    const response = await api.post(
      `/model-deployments/${deploymentId}/saved-workflows/attest-model-artifacts/`,
      {
        source_path: input.sourcePath,
        expected_sha256: input.expectedSha256,
        expected_source_scope_schema_version: input.expectedSourceScopeSchemaVersion,
        expected_source_scope_sha256: input.expectedSourceScopeSha256,
        primary_output_node_ids: input.primaryOutputNodeIds,
        expected_object_info_schema_sha256: input.expectedObjectInfoSchemaSha256,
        expected_converted_graph_sha256: input.expectedConvertedGraphSha256,
        model_profile_version: input.modelProfileVersion,
      },
    )
    if (response.status !== 202) throw new Error(`模型文件证明必须返回持久任务，实际 HTTP ${response.status}`)
    return adaptRuntimeHostProbeAttempt(body(response.data))
  },
  async batchImportComfySavedWorkflows(
    deploymentId: string,
    items: ComfySavedWorkflowBatchItemInput[],
  ): Promise<ComfySavedWorkflowBatchResult> {
    return adaptComfySavedWorkflowBatchResult(body((await api.post(
      `/model-deployments/${deploymentId}/saved-workflows/batch-import/`,
      { items: items.map((item) => ({
        source_path: item.sourcePath,
        expected_sha256: item.expectedSha256,
        expected_source_scope_schema_version: item.expectedSourceScopeSchemaVersion,
        expected_source_scope_sha256: item.expectedSourceScopeSha256,
        import_kind: item.importKind,
        contract_draft: item.contractDraft || null,
        primary_output_node_ids: item.primaryOutputNodeIds || [],
        expected_object_info_schema_sha256: item.expectedObjectInfoSchemaSha256 || '',
        expected_converted_graph_sha256: item.expectedConvertedGraphSha256 || '',
        expected_parameter_schema_sha256: item.expectedParameterSchemaSha256 || '',
        expected_structural_contract_sha256: item.expectedStructuralContractSha256 || '',
        template: item.template || null,
        new_template: item.newTemplate ? {
          name: item.newTemplate.name,
          slug: item.newTemplate.slug,
          output_kind: item.newTemplate.outputKind,
          description: item.newTemplate.description || '',
        } : null,
        model_profile_version: item.modelProfileVersion || null,
      })) },
    )).data))
  },
  async validateWorkflow(id: string, deploymentId: string): Promise<{ version: WorkflowTemplateVersion; contractReport: JsonObject }> {
    const raw = platformRecord(body((await api.post(`/workflow-template-versions/${id}/validate/`, { deployment_id: deploymentId })).data))
    return { version: adaptWorkflowTemplateVersion(raw.version), contractReport: platformRecord(raw.contract_report) }
  },
  async createWorkflowSmokeAttempt(id: string, deploymentId: string, fixture: WorkflowSmokeFixtureInput): Promise<WorkflowSmokeAttempt> {
    return adaptWorkflowSmokeAttempt(body((await api.post(`/workflow-template-versions/${id}/smoke-test/`, {
      deployment_id: deploymentId,
      fixture: {
        schema_version: fixture.schemaVersion,
        values: fixture.values,
        assets: fixture.assets.map((item) => ({ semantic_key: item.semanticKey, asset_id: item.assetId })),
        references: {
          pictures: fixture.references.pictures.map((item) => ({
            asset_id: item.assetId,
            category: item.category,
          })),
          videos: fixture.references.videos.map((item) => ({
            asset_id: item.assetId,
            purpose: item.purpose,
            use_embedded_audio: item.useEmbeddedAudio,
            ...(item.audioAssetId ? { audio_asset_id: item.audioAssetId } : {}),
            ...(item.audioPurpose ? { audio_purpose: item.audioPurpose } : {}),
          })),
          audios: fixture.references.audios.map((item) => ({
            asset_id: item.assetId,
            purpose: item.purpose,
          })),
        },
      },
    })).data))
  },
  async getWorkflowSmokeAttempt(id: string): Promise<WorkflowSmokeAttempt> { return adaptWorkflowSmokeAttempt(body((await api.get(`/workflow-smoke-attempts/${id}/`)).data)) },
  async listWorkflowSmokeAttempts(params: JsonObject = {}): Promise<WorkflowSmokeAttempt[]> { return (await fetchAllPages('/workflow-smoke-attempts/', { ...params, page_size: 50 })).map(adaptWorkflowSmokeAttempt) },
  async cancelWorkflowSmokeAttempt(id: string): Promise<WorkflowSmokeAttempt> { return adaptWorkflowSmokeAttempt(body((await api.post(`/workflow-smoke-attempts/${id}/cancel/`, {})).data)) },
  async resolveWorkflowSmokeAttempt(id: string, input: { outcome: WorkflowSmokeResolutionOutcome; note: string; providerRequestId?: string }): Promise<WorkflowSmokeAttemptResolution> {
    return adaptWorkflowSmokeResolution(body((await api.post(`/workflow-smoke-attempts/${id}/resolve-uncertain/`, {
      outcome: input.outcome,
      note: input.note,
      provider_request_id: input.providerRequestId || '',
    })).data))
  },
  async publishWorkflow(id: string, reasonCode = ''): Promise<WorkflowTemplateVersion> { return adaptWorkflowTemplateVersion(body((await api.post(`/workflow-template-versions/${id}/publish/`, {}, auditOptions(reasonCode))).data)) },
  async retireWorkflow(id: string, reasonCode = ''): Promise<WorkflowTemplateVersion> { return adaptWorkflowTemplateVersion(body((await api.post(`/workflow-template-versions/${id}/retire/`, {}, auditOptions(reasonCode))).data)) },
  async getNodeMetadataPage(deploymentId: string, search = '', offset = 0, limit = 200): Promise<NodeMetadataResponse> {
    return adaptNodeMetadata(body((await api.get('/runtime-hosts/node-metadata/', { params: { deployment_id: deploymentId, search, offset, limit } })).data))
  },
  async getAllNodeMetadata(deploymentId: string, search = ''): Promise<NodeMetadataCatalog> {
    return loadCompleteNodeMetadata(
      (offset, limit) => this.getNodeMetadataPage(deploymentId, search, offset, limit),
      200,
    )
  },

  async listConfigDefinitions(): Promise<ConfigDefinition[]> { return (await fetchAllPages('/config-definitions/', { page_size: 500 })).map(adaptConfigDefinition) },
  async getConfigDefinitionOptions(): Promise<ConfigDefinitionOptions> { return adaptConfigDefinitionOptions(body((await api.get('/config-definitions/options/')).data)) },
  async listConfigResourceOptions(id: string): Promise<ConfigResourceOption[]> {
    return (body<unknown[]>((await api.get(`/config-definitions/${id}/resource-options/`)).data) || []).map(adaptConfigResourceOption)
  },
  async createConfigDefinition(payload: JsonObject, reasonCode = ''): Promise<ConfigDefinition> { return adaptConfigDefinition(body((await api.post('/config-definitions/', payload, auditOptions(reasonCode))).data)) },
  async updateConfigDefinition(id: string, payload: JsonObject, reasonCode = ''): Promise<ConfigDefinition> { return adaptConfigDefinition(body((await api.patch(`/config-definitions/${id}/`, payload, auditOptions(reasonCode))).data)) },
  async listConfigRevisions(params: JsonObject = {}): Promise<ConfigRevision[]> { return (await fetchAllPages('/config-revisions/', { ...params, page_size: 500 })).map(adaptConfigRevision) },
  async createConfigRevision(input: { scopeType: string; project?: string | null; run?: string | null; values: JsonObject; unsetKeys?: string[]; changeSummary?: string; publish?: boolean; reasonCode?: string }): Promise<ConfigRevision> {
    return adaptConfigRevision(body((await api.post('/config-revisions/', { scope_type: input.scopeType, project: input.project || null, run: input.run || null, values: input.values, unset_keys: input.unsetKeys || [], change_summary: input.changeSummary || '', publish: input.publish === true }, auditOptions(input.reasonCode))).data))
  },
  async publishConfigRevision(id: string, reasonCode = ''): Promise<ConfigRevision> { return adaptConfigRevision(body((await api.post(`/config-revisions/${id}/publish/`, {}, auditOptions(reasonCode))).data)) },
  async activateConfigRevision(id: string, reasonCode = ''): Promise<ConfigRevision> { return adaptConfigRevision(body((await api.post(`/config-revisions/${id}/activate/`, {}, auditOptions(reasonCode))).data)) },
  async listSecretBindings(): Promise<SecretBinding[]> { return (await fetchAllPages('/secret-bindings/', { page_size: 500 })).map(adaptSecretBinding) },
  async createSecretBinding(input: { key: string; label: string; purpose: string; secretValue: string; reason?: string }): Promise<SecretBinding> {
    return adaptSecretBinding(body((await api.post('/secret-bindings/', { key: input.key, label: input.label, purpose: input.purpose, secret_value: input.secretValue, reason: input.reason || '' })).data))
  },
  async rotateSecretBinding(id: string, secretValue: string, reason: string): Promise<SecretBinding> {
    return adaptSecretBinding(body((await api.post(`/secret-bindings/${id}/rotate/`, { secret_value: secretValue, reason })).data))
  },
  async setSecretBindingStatus(id: string, isActive: boolean, reason: string): Promise<SecretBinding> {
    return adaptSecretBinding(body((await api.post(`/secret-bindings/${id}/status/`, { is_active: isActive, reason })).data))
  },
  async listSecretAuditEvents(): Promise<SecretAuditEvent[]> { return (await fetchAllPages('/secret-audit-events/', { page_size: 500 })).map(adaptSecretAuditEvent) },
  async listPlatformLifecycleAuditEvents(params: JsonObject = {}): Promise<PlatformLifecycleAuditEvent[]> { return (await fetchAllPages('/platform-lifecycle-audit-events/', { ...params, page_size: 500 })).map(adaptPlatformLifecycleAuditEvent) },
  async resolveConfiguration(params: { projectId?: string; runId?: string } = {}): Promise<ResolvedConfiguration> {
    return adaptResolvedConfiguration(body((await api.get('/config-revisions/resolved/', { params: { project_id: params.projectId, run_id: params.runId } })).data))
  },
  async getProjectConfiguration(projectId: string): Promise<ResolvedConfiguration> {
    return adaptResolvedConfiguration(body((await api.get(`/projects/${projectId}/config/`)).data))
  },
  async publishProjectConfiguration(projectId: string, values: JsonObject, changeSummary = ''): Promise<ResolvedConfiguration> {
    const raw = platformRecord(body((await api.put(`/projects/${projectId}/config/`, { values, change_summary: changeSummary })).data))
    return adaptResolvedConfiguration(raw.effective)
  },
  async patchProjectConfiguration(projectId: string, input: { values?: JsonObject; unsetKeys?: string[]; changeSummary?: string }): Promise<ResolvedConfiguration> {
    const raw = platformRecord(body((await api.patch(`/projects/${projectId}/config/`, { values: input.values || {}, unset_keys: input.unsetKeys || [], change_summary: input.changeSummary || '' })).data))
    return adaptResolvedConfiguration(raw.effective)
  },

  async listRuntimeHosts(): Promise<RuntimeHost[]> { return (await fetchAllPages('/runtime-hosts/', { page_size: 500 })).map(adaptRuntimeHost) },
  async getLatestRuntimeSnapshot(hostId: string): Promise<HostCapabilitySnapshot> { return adaptHostSnapshot(body((await api.get(`/runtime-hosts/${hostId}/latest-snapshot/`)).data)) },
  async setRuntimeHostStatus(hostId: string, isActive: boolean, reasonCode = ''): Promise<RuntimeHost> { return adaptRuntimeHost(body((await api.post(`/runtime-hosts/${hostId}/status/`, { is_active: isActive }, auditOptions(reasonCode))).data)) },
  async createRuntimeHostProbe(hostId: string): Promise<RuntimeHostProbeAttempt> { return adaptRuntimeHostProbeAttempt(body((await api.post(`/runtime-hosts/${hostId}/probe/`, {})).data)) },
  async listRuntimeHostProbeAttempts(params: JsonObject = {}): Promise<RuntimeHostProbeAttempt[]> { return (await fetchAllPages('/runtime-host-probe-attempts/', { ...params, page_size: 100 })).map(adaptRuntimeHostProbeAttempt) },
  async getRuntimeHostProbeAttempt(id: string): Promise<RuntimeHostProbeAttempt> { return adaptRuntimeHostProbeAttempt(body((await api.get(`/runtime-host-probe-attempts/${id}/`)).data)) },
  async cancelRuntimeHostProbeAttempt(id: string): Promise<RuntimeHostProbeAttempt> { return adaptRuntimeHostProbeAttempt(body((await api.post(`/runtime-host-probe-attempts/${id}/cancel/`, {})).data)) },
  async runtimeReadiness(hostId: string, snapshotId?: string): Promise<RuntimeReadiness> { return adaptRuntimeReadiness(body((await api.get(`/runtime-hosts/${hostId}/readiness/`, { params: { snapshot_id: snapshotId || undefined } })).data)) },
  async recommendRuntime(hostId: string, input: { snapshotId?: string; projectId?: string } = {}): Promise<RecommendationSnapshot> {
    return adaptRecommendation(body((await api.post(`/runtime-hosts/${hostId}/recommend/`, { snapshot_id: input.snapshotId, project_id: input.projectId })).data))
  },
  async getRecommendation(id: string): Promise<RecommendationSnapshot> { return adaptRecommendation(body((await api.get(`/recommendations/${id}/`)).data)) },
  async decideRecommendation(id: string, input: { candidateId?: string | null; decision: 'accept' | 'override' | 'reject'; scopeType: 'system' | 'project'; projectId?: string | null; overrideValues?: JsonObject; reason?: string }): Promise<RecommendationDecision> {
    const raw = platformRecord(body((await api.post(`/recommendations/${id}/decide/`, { candidate_id: input.candidateId || null, decision: input.decision, scope_type: input.scopeType, project_id: input.projectId || null, override_values: input.overrideValues || {}, reason: input.reason || '' })).data))
    return {
      id: String(raw.id || ''), createdAt: String(raw.created_at || ''), updatedAt: String(raw.updated_at || ''),
      recommendationSnapshot: String(raw.recommendation_snapshot || ''), candidate: String(raw.candidate || ''), decision: String(raw.decision || ''),
      scopeType: String(raw.scope_type || ''), project: String(raw.project || ''), configRevision: String(raw.config_revision || ''), recipeVersion: String(raw.recipe_version || ''),
      overrideValues: platformRecord(raw.override_values), reason: String(raw.reason || ''),
    }
  },

  async listScriptSources(projectId: string): Promise<ScriptSource[]> { return (await fetchAllPages('/script-sources/', { project: projectId, page_size: 200 })).map(adaptScriptSource) },
  async createPastedScript(projectId: string, content: string, sourceFormat: string): Promise<{ source: ScriptSource; revision: ScriptRevision }> {
    const raw = platformRecord(body((await api.post('/script-sources/', { project_id: projectId, content, source_format: sourceFormat })).data))
    return { source: adaptScriptSource(raw.source), revision: adaptScriptRevision(raw.revision) }
  },
  async uploadScript(projectId: string, file: File): Promise<{ source: ScriptSource; revision: ScriptRevision }> {
    const form = new FormData(); form.append('project_id', projectId); form.append('file', file)
    const raw = platformRecord(body((await api.post('/script-sources/', form, { headers: { 'Content-Type': 'multipart/form-data' } })).data))
    return { source: adaptScriptSource(raw.source), revision: adaptScriptRevision(raw.revision) }
  },
  async importScriptAsset(projectId: string, assetId: string): Promise<{ source: ScriptSource; revision: ScriptRevision }> {
    const raw = platformRecord(body((await api.post('/script-sources/', { project_id: projectId, asset_id: assetId })).data))
    return { source: adaptScriptSource(raw.source), revision: adaptScriptRevision(raw.revision) }
  },
  async getScriptRevision(id: string): Promise<ScriptRevision> { return adaptScriptRevision(body((await api.get(`/script-revisions/${id}/`)).data)) },
  async createScriptRevision(sourceId: string, content: string): Promise<ScriptRevision> { return adaptScriptRevision(body((await api.post('/script-revisions/', { source_id: sourceId, content })).data)) },
  async confirmScriptRevision(id: string): Promise<ScriptRevision> { return adaptScriptRevision(body((await api.post(`/script-revisions/${id}/confirm/`, {})).data)) },

  async listRecipes(status?: string): Promise<ProductionRecipe[]> { return (await fetchAllPages('/production-recipes/', { status, page_size: 500 })).map(adaptProductionRecipe) },
  async getRecipeEditorOptions(): Promise<ProductionRecipeEditorOptions> { return adaptProductionRecipeEditorOptions(body((await api.get('/production-recipes/options/')).data)) },
  async getRecipeMediaRouteOptions(id: string): Promise<ProductionRecipeMediaRouteOptions> { return adaptProductionRecipeMediaRouteOptions(body((await api.get(`/production-recipes/${id}/media-route-options/`)).data)) },
  async createRecipe(payload: JsonObject, reasonCode = ''): Promise<ProductionRecipe> { return adaptProductionRecipe(body((await api.post('/production-recipes/', payload, auditOptions(reasonCode))).data)) },
  async cloneRecipeSuccessor(id: string, expectedDefinitionSha256: string, reasonCode = ''): Promise<ProductionRecipe> { return adaptProductionRecipe(body((await api.post(`/production-recipes/${id}/clone-successor/`, { expected_definition_sha256: expectedDefinitionSha256 }, auditOptions(reasonCode))).data)) },
  async updateRecipe(id: string, payload: JsonObject, reasonCode = ''): Promise<ProductionRecipe> { return adaptProductionRecipe(body((await api.patch(`/production-recipes/${id}/`, payload, auditOptions(reasonCode))).data)) },
  async publishRecipe(id: string, reasonCode = ''): Promise<ProductionRecipe> { return adaptProductionRecipe(body((await api.post(`/production-recipes/${id}/publish/`, {}, auditOptions(reasonCode))).data)) },
  async retireRecipe(id: string, reasonCode = ''): Promise<ProductionRecipe> { return adaptProductionRecipe(body((await api.post(`/production-recipes/${id}/retire/`, {}, auditOptions(reasonCode))).data)) },
  async listProductionRuns(projectId?: string): Promise<ProductionRun[]> { return (await fetchAllPages('/production-runs/', { project: projectId, page_size: 200 })).map(adaptProductionRun) },
  async getProductionRun(id: string): Promise<ProductionRun> { return adaptProductionRun(body((await api.get(`/production-runs/${id}/`)).data)) },
  async createProductionRun(input: ProductionRunCreateInput): Promise<ProductionRun> {
    return adaptProductionRun(body((await api.post('/production-runs/', buildProductionRunRequestPayload(input))).data))
  },
  async approveProductionRun(id: string, input: { stageKey: string; decision: string; comment?: string; candidateOverrides?: Record<string, string>; expectedSourceSnapshotSha256?: string; adoptGeneratedMetadata?: boolean; bibleImageBindings?: BibleImageBindingSelectionInput[]; shotReferenceBindings?: StoryboardShotReferenceSelectionInput[] }): Promise<ProductionRun> {
    return adaptProductionRun(body((await api.post(`/production-runs/${id}/approve/`, {
      stage_key: input.stageKey,
      decision: input.decision,
      comment: input.comment || '',
      ...(input.candidateOverrides ? { candidate_overrides: input.candidateOverrides } : {}),
      ...(input.expectedSourceSnapshotSha256 ? { expected_source_snapshot_sha256: input.expectedSourceSnapshotSha256 } : {}),
      ...(input.adoptGeneratedMetadata !== undefined ? { adopt_generated_metadata: input.adoptGeneratedMetadata } : {}),
      ...(input.bibleImageBindings ? { bible_image_bindings: bibleImageBindingsPayload(input.bibleImageBindings) } : {}),
      ...(input.shotReferenceBindings ? { shot_reference_bindings: input.shotReferenceBindings.map((item) => ({ shot_id: item.shotId, reference_bindings: shotReferenceBindingsPayload(item.referenceBindings) })) } : {}),
    })).data))
  },
  async continueProductionRun(id: string): Promise<ProductionRun> { return adaptProductionRun(body((await api.post(`/production-runs/${id}/continue/`, {})).data)) },
  async regenerateBibleImages(id: string, input: RegenerateBibleImagesInput): Promise<ProductionRun> {
    return adaptProductionRun(body((await api.post(`/production-runs/${id}/regenerate-bible-images/`, regenerateBibleImagesPayload(input))).data))
  },
  async retryProductionRun(id: string): Promise<ProductionRun> { return adaptProductionRun(body((await api.post(`/production-runs/${id}/retry/`, {})).data)) },
  async cancelProductionRun(id: string): Promise<ProductionRun> { return adaptProductionRun(body((await api.post(`/production-runs/${id}/cancel/`, {})).data)) },
  async listGenerationAttempts(params: JsonObject = {}): Promise<GenerationAttempt[]> { return (await fetchAllPages('/generation-attempts/', { ...params, page_size: 200 })).map(adaptGenerationAttempt) },
  async resolveProductionRunUncertain(id: string, input: ResolveUncertainAttemptInput): Promise<ProductionRun> {
    return adaptProductionRun(body((await api.post(`/production-runs/${id}/resolve-uncertain/`, {
      attempt_id: input.attemptId,
      outcome: input.outcome,
      note: input.note,
      provider_request_id: input.providerRequestId || '',
      actual_cost: input.actualCost === undefined || input.actualCost === '' ? null : input.actualCost,
      cost_currency: input.costCurrency || '',
    })).data))
  },
  async resolveDeploymentSmokeUncertain(id: string, input: { smokeAttemptId: string; outcome: SmokeResolutionOutcome; note: string; providerRequestId?: string; actualCost?: string | null; costCurrency?: string }): Promise<PromptNormalizationSmokeResolution> {
    return adaptPromptNormalizationSmokeResolution(body((await api.post(`/model-deployments/${id}/resolve-smoke-uncertain/`, {
      smoke_attempt_id: input.smokeAttemptId,
      outcome: input.outcome,
      note: input.note,
      provider_request_id: input.providerRequestId || '',
      actual_cost: input.actualCost === undefined || input.actualCost === '' ? null : input.actualCost,
      cost_currency: input.costCurrency || '',
    })).data))
  },
  async selectProductionRoute(id: string, input: { stageKey: string; routeIndex: number }): Promise<ProductionRun> {
    return adaptProductionRun(body((await api.post(`/production-runs/${id}/select-route/`, { stage_key: input.stageKey, route_index: input.routeIndex })).data))
  },
  async authorizeProductionCost(id: string, input: { stageKey: string; allowExternalCalls?: boolean | null; budgetLimit?: string | null; approvedEstimatedCost?: string | null; currency?: string; comment?: string }): Promise<ProductionRun> {
    return adaptProductionRun(body((await api.post(`/production-runs/${id}/authorize-cost/`, {
      stage_key: input.stageKey, allow_external_calls: input.allowExternalCalls ?? null,
      budget_limit: input.budgetLimit ?? null, approved_estimated_cost: input.approvedEstimatedCost ?? null,
      currency: input.currency || '', comment: input.comment || '',
    })).data))
  },
  async selectArtifact(runId: string, artifactId: string): Promise<GeneratedArtifact> { return adaptGeneratedArtifact(body((await api.post(`/production-runs/${runId}/select-artifact/`, { artifact_id: artifactId })).data)) },
  async listArtifacts(params: JsonObject = {}): Promise<GeneratedArtifact[]> { return (await fetchAllPages('/generated-artifacts/', { ...params, page_size: 500 })).map(adaptGeneratedArtifact) },
  async getArtifact(id: string): Promise<GeneratedArtifact> { return adaptGeneratedArtifact(body((await api.get(`/generated-artifacts/${id}/`)).data)) },
  async getArtifactPreview(artifact: GeneratedArtifact): Promise<ArtifactPreview | null> {
    const detail = artifact.manifestMetadata ? artifact : await this.getArtifact(artifact.id)
    const taskId = String(detail.manifestMetadata?.task_id || detail.generationTask || '')
    const rawOutputIndex = Number(detail.manifestMetadata?.output_index)
    const outputIndex = Number.isInteger(rawOutputIndex) && rawOutputIndex >= 0 ? rawOutputIndex : 0
    if (!detail.previewUrl) return null
    return {
      taskId,
      outputIndex,
      kind: detail.kind,
      filename: detail.objectKey.split('/').pop() || detail.objectKey,
      previewUrl: detail.previewUrl,
      downloadUrl: detail.downloadUrl,
      parameters: detail.comparisonParameters,
      backendStatus: detail.status,
    }
  },
}
