import { api, unwrap } from './api'
import { platformApi } from './platformApi'
import { adaptExecutionSmokeCreated, executionSmokePayload, type ExecutionSmokeRequest } from './executionPreparation'
import { adaptExecutionEvidence } from './executionEvidence'
import { adaptSmokeSourceContexts } from './executionSmokeSourceContext'
import { adaptExecutionParameterMappings } from './executionParameterMappings'
import {
  adaptExecutionInstance, adaptExecutionVersion, adaptExecutionVersionDetail, adaptExecutionContract,
  adaptExecutionSelectionOptions, adaptExecutionAuthoringOptions, adaptExecutionPage,
  executionSelectionMatches, executionFrozenProfileInputs, type ExecutionConfiguration, type ExecutionVersionDetail, type ExecutionVersion,
  adaptExecutionInputSuggestion,
} from './executionRegistry'

const instances = '/model-execution-instances/'
const versions = '/model-execution-instance-versions/'
const path = (base: string, id: string) => `${base}${encodeURIComponent(id)}/`
const result = (response: { data: unknown }) => unwrap(response.data)

export const executionRegistryApi = {
  async smokeSourceContexts(id: string, page = 1) {
    return adaptSmokeSourceContexts((await api.get(path(versions,id)+'smoke-source-contexts/', {params:{page,page_size:20}})).data)
  },
  async evidence(id: string, page = 1, status?: string) {
    // Keep the summary and DRF page together; unwrap would discard them.
    return adaptExecutionEvidence((await api.get(path(versions, id) + 'evidence/', { params: { page, ...(status ? { status } : {}) } })).data)
  },
  async smoke(id: string, input: ExecutionSmokeRequest, idempotencyKey: string) {
    if (!idempotencyKey.trim()) throw new Error('验收请求必须具有明确的幂等键')
    return adaptExecutionSmokeCreated(result(await api.post(path(versions, id) + 'smoke-test/', executionSmokePayload(input), { headers: { 'Idempotency-Key': idempotencyKey } })))
  },
  async instances(page: number, search = '') { return adaptExecutionPage((await api.get(instances, { params: { page, search } })).data, adaptExecutionInstance) },
  async instance(id: string) { return adaptExecutionInstance(result(await api.get(path(instances, id)))) },
  async createInstance(input: { key: string; name: string; description: string }) { return adaptExecutionInstance(result(await api.post(instances, input))) },
  async updateInstance(id: string, input: { expected_revision: number; name?: string; description?: string; is_active?: boolean }) { return adaptExecutionInstance(result(await api.patch(path(instances, id), input))) },
  async versions(page: number, filters: { instance?: string; status?: string; operation?: string; search?: string } = {}) { return adaptExecutionPage((await api.get(versions, { params: { page, ...filters } })).data, adaptExecutionVersion) },
  async version(id: string) { return adaptExecutionVersionDetail(result(await api.get(path(versions, id)))) },
  async createVersion(instanceId: string, revision: number, configuration: ExecutionConfiguration) { return adaptExecutionVersionDetail(result(await api.post(versions, { instance_id: instanceId, expected_revision: revision, ...configuration }))) },
  async updateVersion(id: string, input: Omit<Partial<ExecutionConfiguration>, 'output_version_id'> & { expected_revision: number; output_version_id?: string | null }) { return adaptExecutionVersionDetail(result(await api.patch(path(versions, id), input))) },
  async contract(id: string) { return adaptExecutionContract(result(await api.get(path(versions, id) + 'input-contract/'))) },
  async parameterMappings(id: string, sha256: string) {
    return adaptExecutionParameterMappings(result(await api.get(path(versions, id) + 'parameter-mappings/', {
      params: { expected_definition_sha256: sha256 },
    })))
  },
  async inputSuggestion(id: string, inputKey: string, suggestionKey: string, projectId?: string,
    production?: { runId: string; scope: { kind: string; id: string } }) {
    return adaptExecutionInputSuggestion(result(await api.get(path(versions, id) + 'input-suggestion/', {
      params: { input_key: inputKey, suggestion_key: suggestionKey, ...(projectId ? { project_id: projectId } : {}),
        ...(production ? { production_run_id: production.runId, entity_kind: production.scope.kind, entity_id: production.scope.id } : {}) },
    })))
  },
  async authoring(id: string) { return adaptExecutionAuthoringOptions(result(await api.get(path(versions, id) + 'authoring-options/'))) },
  async choices(deploymentId?: string, workflowId?: string) { return adaptExecutionSelectionOptions(result(await api.get(versions + 'selection-options/', { params: { ...(deploymentId ? { deployment_id: deploymentId } : {}), ...(workflowId ? { workflow_version_id: workflowId } : {}) } }))) },
  async publish(id: string, input: { expected_revision: number; expected_definition_sha256: string }) { return adaptExecutionVersionDetail(result(await api.post(path(versions, id) + 'publish/', input))) },
  async retire(id: string, input: { expected_revision: number; expected_definition_sha256: string; reason: string }) { return adaptExecutionVersionDetail(result(await api.post(path(versions, id) + 'retire/', input))) },
  async clone(id: string, input: { expected_revision: number; expected_definition_sha256: string | null }) { return adaptExecutionVersionDetail(result(await api.post(path(versions, id) + 'clone-successor/', input))) },
}

export async function loadExecutionEditorCatalogs(version: ExecutionVersionDetail | null, draft: ExecutionConfiguration) {
  const readonly = Boolean(version && version.status !== 'draft')
  const choices = readonly ? await executionRegistryApi.choices() : await executionRegistryApi.choices(draft.deployment_id || undefined, draft.workflow_version_id || undefined)
  const catalogs = readonly ? null : await Promise.all([platformApi.listPromptProfileVersions(), platformApi.listWorkflowVersions()])
  const options = version?.status === 'draft' && executionSelectionMatches(draft, version.configuration) ? await executionRegistryApi.authoring(version.id) : null
  const frozenContract = readonly && version ? await executionRegistryApi.contract(version.id) : null
  const profileInputs = frozenContract && version ? executionFrozenProfileInputs(version, frozenContract) : options?.profile_inputs
  const auxiliaryVersions: ExecutionVersion[] = []
  if (options) {
    for (const operation of new Set(options.auxiliary_purposes.map(p => p.operation))) {
      let page = 1
      for (;;) {
        const result = await executionRegistryApi.versions(page, { status: 'published', operation })
        auxiliaryVersions.push(...result.results)
        if (result.next === null) break
        page++
      }
    }
  }
  return { choices, options, profileInputs, inputForms: frozenContract?.input_forms, auxiliaryVersions, profiles: catalogs?.[0] || [], workflows: catalogs?.[1] || [] }
}
