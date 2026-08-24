import { api, fetchAllPages, unwrap } from './api'
import {
  adaptConfigDefinition, adaptConfigResourceOption, adaptConfigRevision, adaptGeneratedArtifact, adaptGenerationAttempt,
  adaptHostSnapshot, adaptLegacyWorkflowTemplate, adaptModelArtifact, adaptModelDeployment,
  adaptModelDeploymentSmokeAttempt, adaptModelProfileVersion, adaptNodeMetadata, adaptProductionRecipe, adaptProductionRecipeMediaRouteOptions, adaptProductionRun,
  adaptPromptFieldDefinition, adaptPromptProfileBinding, adaptPromptProfileFieldRule, adaptPromptProfileOptions,
  adaptPromptProfilePreview, adaptPromptProfileVersion,
  adaptProviderProfile, adaptRecommendation, adaptResolvedConfiguration, adaptRuntimeHost, adaptRuntimeHostProbeAttempt,
  adaptRuntimeReadiness, adaptProductionReadiness, adaptScriptRevision, adaptScriptSource, adaptWorkflowSmokeAttempt, adaptWorkflowTemplateVersion,
  adaptWorkflowTemplateCanvasExport,
  adaptPromptNormalizationSmokeResolution,
  adaptPlatformLifecycleAuditEvent, adaptSecretAuditEvent, adaptSecretBinding, platformRecord,
} from './platformAdapters'
import type {
  ArtifactPreview, ConfigDefinition, ConfigResourceOption, ConfigRevision, GeneratedArtifact, GenerationAttempt, HostCapabilitySnapshot,
  JsonObject, LegacyWorkflowTemplate, ModelArtifact, ModelDeployment,
  ModelDeploymentSmokeAttempt, ModelProfileVersion,
  NodeMetadataCatalog, NodeMetadataResponse, ProductionRecipe, ProductionRecipeMediaRouteOptions, ProductionRun, ProductionRunCreateInput,
  PromptFieldDefinition, PromptProfileBinding, PromptProfileFieldRule, PromptProfileOptions, PromptProfilePreview, PromptProfileVersion,
  ProductionReadiness, ProviderProfile, RecommendationSnapshot, ResolvedConfiguration, RuntimeHost, RuntimeHostProbeAttempt,
  RuntimeReadiness, ScriptRevision, ScriptSource, WorkflowTemplateCanvasExport, WorkflowTemplateVersion,
  RecommendationDecision,
  PlatformLifecycleAuditEvent, PromptNormalizationSmokeResolution, ResolveUncertainAttemptInput, SecretAuditEvent, SecretBinding,
  SmokeResolutionOutcome, WorkflowSmokeAttempt, WorkflowVersionImportInput,
} from './platformTypes'
import { buildProductionRunRequestPayload } from './productionRunPayload'
import { loadCompleteNodeMetadata } from './nodeMetadata'

const body = <T>(payload: unknown): T => unwrap<T>(payload)
const auditOptions = (reasonCode = '') => reasonCode
  ? { headers: { 'X-Audit-Reason-Code': reasonCode } }
  : undefined

export const platformApi = {
  async getProductionReadiness(): Promise<ProductionReadiness> { return adaptProductionReadiness(body((await api.get('/production-readiness/')).data)) },
  async listProviders(): Promise<ProviderProfile[]> { return (await fetchAllPages('/provider-profiles/', { page_size: 500 })).map(adaptProviderProfile) },
  async createProvider(payload: JsonObject, reasonCode = ''): Promise<ProviderProfile> { return adaptProviderProfile(body((await api.post('/provider-profiles/', payload, auditOptions(reasonCode))).data)) },
  async updateProvider(id: string, payload: JsonObject, reasonCode = ''): Promise<ProviderProfile> { return adaptProviderProfile(body((await api.patch(`/provider-profiles/${id}/`, payload, auditOptions(reasonCode))).data)) },
  async deleteProvider(id: string, reasonCode = ''): Promise<void> { await api.delete(`/provider-profiles/${id}/`, auditOptions(reasonCode)) },
  async listModelProfiles(): Promise<ModelProfileVersion[]> { return (await fetchAllPages('/model-profile-versions/', { page_size: 500 })).map(adaptModelProfileVersion) },
  async createModelProfile(payload: JsonObject, reasonCode = ''): Promise<ModelProfileVersion> { return adaptModelProfileVersion(body((await api.post('/model-profile-versions/', payload, auditOptions(reasonCode))).data)) },
  async updateModelProfile(id: string, payload: JsonObject, reasonCode = ''): Promise<ModelProfileVersion> { return adaptModelProfileVersion(body((await api.patch(`/model-profile-versions/${id}/`, payload, auditOptions(reasonCode))).data)) },
  async deleteModelProfile(id: string, reasonCode = ''): Promise<void> { await api.delete(`/model-profile-versions/${id}/`, auditOptions(reasonCode)) },
  async publishModelProfile(id: string, reasonCode = ''): Promise<ModelProfileVersion> { return adaptModelProfileVersion(body((await api.post(`/model-profile-versions/${id}/publish/`, {}, auditOptions(reasonCode))).data)) },
  async retireModelProfile(id: string, reasonCode = ''): Promise<ModelProfileVersion> { return adaptModelProfileVersion(body((await api.post(`/model-profile-versions/${id}/retire/`, {}, auditOptions(reasonCode))).data)) },
  async listDeployments(): Promise<ModelDeployment[]> { return (await fetchAllPages('/model-deployments/', { page_size: 500 })).map(adaptModelDeployment) },
  async createDeployment(payload: JsonObject, reasonCode = ''): Promise<ModelDeployment> { return adaptModelDeployment(body((await api.post('/model-deployments/', payload, auditOptions(reasonCode))).data)) },
  async updateDeployment(id: string, payload: JsonObject, reasonCode = ''): Promise<ModelDeployment> { return adaptModelDeployment(body((await api.patch(`/model-deployments/${id}/`, payload, auditOptions(reasonCode))).data)) },
  async deleteDeployment(id: string, reasonCode = ''): Promise<void> { await api.delete(`/model-deployments/${id}/`, auditOptions(reasonCode)) },
  async probeDeployment(id: string): Promise<JsonObject> { return platformRecord(body((await api.post(`/model-deployments/${id}/probe/`, {})).data)) },
  async smokeTestDeployment(id: string, operation: string): Promise<ModelDeploymentSmokeAttempt> {
    const response = await api.post(`/model-deployments/${id}/smoke-test/`, { operation })
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
  async deleteModelArtifact(id: string, reasonCode = ''): Promise<void> { await api.delete(`/model-artifacts/${id}/`, auditOptions(reasonCode)) },
  async verifyModelArtifact(id: string, hostSnapshotId: string): Promise<ModelArtifact> { return adaptModelArtifact(body((await api.post(`/model-artifacts/${id}/verify/`, { host_snapshot_id: hostSnapshotId })).data)) },

  async listPromptFieldDefinitions(): Promise<PromptFieldDefinition[]> { return (await fetchAllPages('/prompt-field-definitions/', { page_size: 500 })).map(adaptPromptFieldDefinition) },
  async createPromptFieldDefinition(payload: JsonObject, reasonCode = ''): Promise<PromptFieldDefinition> { return adaptPromptFieldDefinition(body((await api.post('/prompt-field-definitions/', payload, auditOptions(reasonCode))).data)) },
  async updatePromptFieldDefinition(id: string, payload: JsonObject, reasonCode = ''): Promise<PromptFieldDefinition> { return adaptPromptFieldDefinition(body((await api.patch(`/prompt-field-definitions/${id}/`, payload, auditOptions(reasonCode))).data)) },
  async deletePromptFieldDefinition(id: string, reasonCode = ''): Promise<void> { await api.delete(`/prompt-field-definitions/${id}/`, auditOptions(reasonCode)) },
  async listPromptProfileVersions(): Promise<PromptProfileVersion[]> { return (await fetchAllPages('/prompt-profile-versions/', { page_size: 500 })).map(adaptPromptProfileVersion) },
  async createPromptProfileVersion(payload: JsonObject, reasonCode = ''): Promise<PromptProfileVersion> { return adaptPromptProfileVersion(body((await api.post('/prompt-profile-versions/', payload, auditOptions(reasonCode))).data)) },
  async updatePromptProfileVersion(id: string, payload: JsonObject, reasonCode = ''): Promise<PromptProfileVersion> { return adaptPromptProfileVersion(body((await api.patch(`/prompt-profile-versions/${id}/`, payload, auditOptions(reasonCode))).data)) },
  async deletePromptProfileVersion(id: string, reasonCode = ''): Promise<void> { await api.delete(`/prompt-profile-versions/${id}/`, auditOptions(reasonCode)) },
  async publishPromptProfileVersion(id: string, reasonCode = ''): Promise<PromptProfileVersion> { return adaptPromptProfileVersion(body((await api.post(`/prompt-profile-versions/${id}/publish/`, {}, auditOptions(reasonCode))).data)) },
  async retirePromptProfileVersion(id: string, reasonCode = ''): Promise<PromptProfileVersion> { return adaptPromptProfileVersion(body((await api.post(`/prompt-profile-versions/${id}/retire/`, {}, auditOptions(reasonCode))).data)) },
  async previewPromptProfileVersion(id: string, sources: JsonObject): Promise<PromptProfilePreview> { return adaptPromptProfilePreview(body((await api.post(`/prompt-profile-versions/${id}/preview/`, { sources })).data)) },
  async getPromptProfileOptions(): Promise<PromptProfileOptions> { return adaptPromptProfileOptions(body((await api.get('/prompt-profile-versions/options/')).data)) },
  async listPromptProfileFieldRules(): Promise<PromptProfileFieldRule[]> { return (await fetchAllPages('/prompt-profile-field-rules/', { page_size: 1000 })).map(adaptPromptProfileFieldRule) },
  async createPromptProfileFieldRule(payload: JsonObject, reasonCode = ''): Promise<PromptProfileFieldRule> { return adaptPromptProfileFieldRule(body((await api.post('/prompt-profile-field-rules/', payload, auditOptions(reasonCode))).data)) },
  async updatePromptProfileFieldRule(id: string, payload: JsonObject, reasonCode = ''): Promise<PromptProfileFieldRule> { return adaptPromptProfileFieldRule(body((await api.patch(`/prompt-profile-field-rules/${id}/`, payload, auditOptions(reasonCode))).data)) },
  async deletePromptProfileFieldRule(id: string, reasonCode = ''): Promise<void> { await api.delete(`/prompt-profile-field-rules/${id}/`, auditOptions(reasonCode)) },
  async listPromptProfileBindings(): Promise<PromptProfileBinding[]> { return (await fetchAllPages('/prompt-profile-bindings/', { page_size: 500 })).map(adaptPromptProfileBinding) },
  async createPromptProfileBinding(payload: JsonObject, reasonCode = ''): Promise<PromptProfileBinding> { return adaptPromptProfileBinding(body((await api.post('/prompt-profile-bindings/', payload, auditOptions(reasonCode))).data)) },
  async updatePromptProfileBinding(id: string, payload: JsonObject, reasonCode = ''): Promise<PromptProfileBinding> { return adaptPromptProfileBinding(body((await api.patch(`/prompt-profile-bindings/${id}/`, payload, auditOptions(reasonCode))).data)) },
  async deletePromptProfileBinding(id: string, reasonCode = ''): Promise<void> { await api.delete(`/prompt-profile-bindings/${id}/`, auditOptions(reasonCode)) },

  async listLegacyWorkflowTemplates(): Promise<LegacyWorkflowTemplate[]> { return (await fetchAllPages('/workflow-templates/', { page_size: 500 })).map(adaptLegacyWorkflowTemplate) },
  async listWorkflowVersions(): Promise<WorkflowTemplateVersion[]> { return (await fetchAllPages('/workflow-template-versions/', { page_size: 500 })).map(adaptWorkflowTemplateVersion) },
  async getWorkflowVersion(id: string): Promise<WorkflowTemplateVersion> { return adaptWorkflowTemplateVersion(body((await api.get(`/workflow-template-versions/${id}/`)).data)) },
  async getWorkflowTemplateCanvasExport(id: string, deploymentId = ''): Promise<WorkflowTemplateCanvasExport> {
    return adaptWorkflowTemplateCanvasExport(body((await api.get(
      `/workflow-template-versions/${id}/comfyui-workflow/`,
      { params: deploymentId ? { deployment_id: deploymentId } : {} },
    )).data))
  },
  async importWorkflowVersion(input: WorkflowVersionImportInput): Promise<WorkflowTemplateVersion> {
    return adaptWorkflowTemplateVersion(body((await api.post('/workflow-template-versions/', {
      template: input.template, graph: input.graph, input_contract: input.inputContract ?? {},
      semantic_bindings: (input.semanticBindings ?? []).map((item) => ({ semantic_key: item.semanticKey, node_id: item.nodeId, input_name: item.inputName, value_type: item.valueType, is_required: item.isRequired })),
      model_requirements: (input.modelRequirements ?? []).map((item) => ({ role: item.role, alias: item.alias, artifact_id: item.artifactId || null, expected_filename: item.expectedFilename, expected_sha256: item.expectedSha256, is_required: item.isRequired })),
      provider: input.provider || null, model_profile_version: input.modelProfileVersion || null,
    })).data))
  },
  async validateWorkflow(id: string, deploymentId: string): Promise<{ version: WorkflowTemplateVersion; contractReport: JsonObject }> {
    const raw = platformRecord(body((await api.post(`/workflow-template-versions/${id}/validate/`, { deployment_id: deploymentId })).data))
    return { version: adaptWorkflowTemplateVersion(raw.version), contractReport: platformRecord(raw.contract_report) }
  },
  async createWorkflowSmokeAttempt(id: string, deploymentId: string): Promise<WorkflowSmokeAttempt> {
    return adaptWorkflowSmokeAttempt(body((await api.post(`/workflow-template-versions/${id}/smoke-test/`, { deployment_id: deploymentId })).data))
  },
  async getWorkflowSmokeAttempt(id: string): Promise<WorkflowSmokeAttempt> { return adaptWorkflowSmokeAttempt(body((await api.get(`/workflow-smoke-attempts/${id}/`)).data)) },
  async listWorkflowSmokeAttempts(params: JsonObject = {}): Promise<WorkflowSmokeAttempt[]> { return (await fetchAllPages('/workflow-smoke-attempts/', { ...params, page_size: 50 })).map(adaptWorkflowSmokeAttempt) },
  async cancelWorkflowSmokeAttempt(id: string): Promise<WorkflowSmokeAttempt> { return adaptWorkflowSmokeAttempt(body((await api.post(`/workflow-smoke-attempts/${id}/cancel/`, {})).data)) },
  async publishWorkflow(id: string, reasonCode = ''): Promise<WorkflowTemplateVersion> { return adaptWorkflowTemplateVersion(body((await api.post(`/workflow-template-versions/${id}/publish/`, {}, auditOptions(reasonCode))).data)) },
  async deleteWorkflow(id: string, reasonCode = ''): Promise<void> { await api.delete(`/workflow-template-versions/${id}/`, auditOptions(reasonCode)) },
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
  async listConfigResourceOptions(id: string): Promise<ConfigResourceOption[]> {
    return (body<unknown[]>((await api.get(`/config-definitions/${id}/resource-options/`)).data) || []).map(adaptConfigResourceOption)
  },
  async createConfigDefinition(payload: JsonObject, reasonCode = ''): Promise<ConfigDefinition> { return adaptConfigDefinition(body((await api.post('/config-definitions/', payload, auditOptions(reasonCode))).data)) },
  async updateConfigDefinition(id: string, payload: JsonObject, reasonCode = ''): Promise<ConfigDefinition> { return adaptConfigDefinition(body((await api.patch(`/config-definitions/${id}/`, payload, auditOptions(reasonCode))).data)) },
  async deleteConfigDefinition(id: string, reasonCode = ''): Promise<void> { await api.delete(`/config-definitions/${id}/`, auditOptions(reasonCode)) },
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
  async getScriptRevision(id: string): Promise<ScriptRevision> { return adaptScriptRevision(body((await api.get(`/script-revisions/${id}/`)).data)) },
  async createScriptRevision(sourceId: string, content: string): Promise<ScriptRevision> { return adaptScriptRevision(body((await api.post('/script-revisions/', { source_id: sourceId, content })).data)) },
  async confirmScriptRevision(id: string): Promise<ScriptRevision> { return adaptScriptRevision(body((await api.post(`/script-revisions/${id}/confirm/`, {})).data)) },

  async listRecipes(status?: string): Promise<ProductionRecipe[]> { return (await fetchAllPages('/production-recipes/', { status, page_size: 500 })).map(adaptProductionRecipe) },
  async getRecipeMediaRouteOptions(id: string): Promise<ProductionRecipeMediaRouteOptions> { return adaptProductionRecipeMediaRouteOptions(body((await api.get(`/production-recipes/${id}/media-route-options/`)).data)) },
  async createRecipe(payload: JsonObject, reasonCode = ''): Promise<ProductionRecipe> { return adaptProductionRecipe(body((await api.post('/production-recipes/', payload, auditOptions(reasonCode))).data)) },
  async updateRecipe(id: string, payload: JsonObject, reasonCode = ''): Promise<ProductionRecipe> { return adaptProductionRecipe(body((await api.patch(`/production-recipes/${id}/`, payload, auditOptions(reasonCode))).data)) },
  async deleteRecipe(id: string, reasonCode = ''): Promise<void> { await api.delete(`/production-recipes/${id}/`, auditOptions(reasonCode)) },
  async publishRecipe(id: string, reasonCode = ''): Promise<ProductionRecipe> { return adaptProductionRecipe(body((await api.post(`/production-recipes/${id}/publish/`, {}, auditOptions(reasonCode))).data)) },
  async retireRecipe(id: string, reasonCode = ''): Promise<ProductionRecipe> { return adaptProductionRecipe(body((await api.post(`/production-recipes/${id}/retire/`, {}, auditOptions(reasonCode))).data)) },
  async listProductionRuns(projectId?: string): Promise<ProductionRun[]> { return (await fetchAllPages('/production-runs/', { project: projectId, page_size: 200 })).map(adaptProductionRun) },
  async getProductionRun(id: string): Promise<ProductionRun> { return adaptProductionRun(body((await api.get(`/production-runs/${id}/`)).data)) },
  async createProductionRun(input: ProductionRunCreateInput): Promise<ProductionRun> {
    return adaptProductionRun(body((await api.post('/production-runs/', buildProductionRunRequestPayload(input))).data))
  },
  async approveProductionRun(id: string, input: { stageKey: string; decision: string; comment?: string; candidateOverrides?: Record<string, string>; expectedSourceSnapshotSha256?: string; adoptGeneratedMetadata?: boolean }): Promise<ProductionRun> {
    return adaptProductionRun(body((await api.post(`/production-runs/${id}/approve/`, {
      stage_key: input.stageKey,
      decision: input.decision,
      comment: input.comment || '',
      ...(input.candidateOverrides ? { candidate_overrides: input.candidateOverrides } : {}),
      ...(input.expectedSourceSnapshotSha256 ? { expected_source_snapshot_sha256: input.expectedSourceSnapshotSha256 } : {}),
      ...(input.adoptGeneratedMetadata !== undefined ? { adopt_generated_metadata: input.adoptGeneratedMetadata } : {}),
    })).data))
  },
  async continueProductionRun(id: string): Promise<ProductionRun> { return adaptProductionRun(body((await api.post(`/production-runs/${id}/continue/`, {})).data)) },
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
