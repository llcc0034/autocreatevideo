import type { WorkflowConfigurationLinks } from './platformTypes'
import type { RouteLocationRaw } from 'vue-router'
import { modelPromptSettingsRoute } from './modelPromptBindings'

export const workflowModelSettingsRoute = (workflowId: string, modelId: string, deploymentId = '') => ({
  path: '/settings', query: {
    section: 'models', model_profile_version: modelId, focus: 'deployments', workflow_context: workflowId,
    ...(deploymentId ? { deployment: deploymentId } : {}),
  },
})
export const workflowPromptSettingsRoute = (workflowId: string, modelId: string, profileId = '', includeInactive = false) => {
  const route = modelPromptSettingsRoute(modelId, profileId)
  return { ...route, query: { ...route.query, workflow_context: workflowId, ...(includeInactive ? { include_inactive: '1' } : {}) } }
}
export const workflowProjectorSettingsRoute = (workflowId: string, versionId: string) => ({
  path: '/settings', query: { section: 'workflows', projector_version: versionId, workflow_context: workflowId },
})

export interface WorkflowRelationItem {
  key: 'model' | 'prompt' | 'projector'
  label: string
  name: string
  status: string
  tone: 'linked' | 'warning' | 'danger' | 'muted'
  description: string
  to?: RouteLocationRaw
}

/** Configuration facts only: these indicators never stand in for Validate/Smoke. */
export const workflowRelationItems = (workflowId: string, links: WorkflowConfigurationLinks): WorkflowRelationItem[] => {
  const detailRoute = `/settings/workflows/${workflowId}`
  const deployment = links.validatedDeployment
  const model: WorkflowRelationItem = {
    key: 'model', label: '模型实例', name: '尚未绑定校验实例', status: '待配置', tone: 'warning',
    description: '进入工作流详情，选择实例并完成在线校验。', to: detailRoute,
  }
  if (deployment) {
    if (deployment.status === 'active') {
      Object.assign(model, {
        name: deployment.name, status: '已启用', tone: 'linked',
        description: '查看此工作流的校验实例；实例启用不代表工作流验收通过。',
        to: workflowModelSettingsRoute(workflowId, deployment.modelProfileVersionId, deployment.id),
      })
    } else {
      Object.assign(model, { name: '实例未启用', status: '未启用', tone: 'muted',
        description: '在更多关联中查看已记录的实例及状态。', to: undefined })
    }
    if (deployment.modelProfileVersionId !== links.model?.id) {
      Object.assign(model, { status: '版本不一致', tone: 'danger',
        description: '校验实例与工作流绑定的模型版本不一致，请查看关联详情。', to: undefined })
    }
  }
  const prompts = links.promptBindings
  const first = prompts.preview[0]
  const prompt: WorkflowRelationItem = {
    key: 'prompt', label: '提示词', name: prompts.total ? '暂无启用绑定' : '尚未配置模型提示词绑定',
    status: prompts.total ? '未启用' : '未配置', tone: 'muted',
    description: '查看适用于此模型的策略；是否必需取决于操作与模式。',
    to: links.model ? workflowPromptSettingsRoute(workflowId, links.model.id, '', true) : undefined,
  }
  if (first && links.model) {
    Object.assign(prompt, {
      name: `${first.name} v${first.version}`, status: prompts.activeTotal > 1 ? `${prompts.activeTotal} 条启用` : '已启用', tone: 'linked',
      description: '查看适用于此模型的提示词策略；具体任务使用哪条，请查看任务详情。',
      to: prompts.activeTotal === 1 ? workflowPromptSettingsRoute(workflowId, links.model.id, first.profileId) : undefined,
    })
  }
  if (prompts.preview.some(binding => binding.status !== 'published')) {
    Object.assign(prompt, { status: '待发布', tone: 'warning', description: '部分启用绑定的策略尚未发布，请在更多关联中核对。', to: undefined })
  }
  if (prompts.state !== 'available') {
    Object.assign(prompt, {
      status: prompts.state === 'model_unbound' ? '待绑定模型' : '目录不完整', tone: 'warning',
      description: prompts.state === 'model_unbound' ? '尚未绑定模型，无法查询策略。' : '模型族缺失，策略目录不完整，请查看关联详情。',
      to: undefined,
    })
  }
  const reference = links.projector
  const projector: WorkflowRelationItem = {
    key: 'projector', label: 'Projector', name: '未记录 Projector', status: '待关联', tone: 'warning',
    description: '缺少 Projector 引用，不能执行；请从候选重新生成工作流合同。', to: detailRoute,
  }
  if (reference.state === 'linked') {
    if (reference.isActive && reference.status === 'published') {
      Object.assign(projector, {
        name: `${reference.name} v${reference.version}`, status: '已引用', tone: 'linked',
        description: '查看工作流冻结的精确版本，不会替换为当前版本。',
        to: workflowProjectorSettingsRoute(workflowId, reference.versionId!),
      })
    } else {
      Object.assign(projector, { name: '查看冻结版本', status: '历史引用', tone: 'muted',
        description: '方案族已停用或版本已退役；冻结引用仍保留，不等同于工作流失效。', to: undefined })
    }
  } else if (reference.state === 'invalid') {
    Object.assign(projector, { name: '查看合同证据', status: '引用异常', tone: 'danger',
      description: '冻结引用缺失或不一致，不能执行；请在工作流详情查看合同证据。' })
  }
  return [model, prompt, projector]
}

/** Missing or malformed relationships must not masquerade as an empty catalog. */
export const adaptWorkflowConfigurationLinks = (value: unknown): WorkflowConfigurationLinks => {
  const fail = (): never => { throw new Error('工作流关联配置响应不完整，请刷新或检查服务版本。') }
  const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : fail()
  const text = (value: unknown): string => typeof value === 'string' ? value : fail()
  const integer = (value: unknown): number => Number.isInteger(value) && Number(value) >= 0 ? Number(value) : fail()
  const boolean = (value: unknown): boolean => typeof value === 'boolean' ? value : fail()
  const choice = <T extends string>(value: unknown, values: T[]): T => values.includes(value as T) ? value as T : fail()
  const raw = record(value), model = raw.model === null ? null : record(raw.model)
  const deployment = raw.validated_deployment === null ? null : record(raw.validated_deployment)
  const prompts = record(raw.prompt_bindings), projector = record(raw.projector)
  if (!Array.isArray(prompts.preview) || prompts.preview.length > 3 || prompts.preview.length > integer(prompts.active_total) || integer(prompts.active_total) > integer(prompts.total)) fail()
  if (integer(prompts.active_total) > 0 && !(prompts.preview as unknown[]).length) fail()
  const result: WorkflowConfigurationLinks = {
    model: model ? { id: text(model.id), key: text(model.key), version: integer(model.version), status: text(model.status), instanceCount: integer(model.instance_count) } : null,
    validatedDeployment: deployment ? { id: text(deployment.id), name: text(deployment.name), status: text(deployment.status), modelProfileVersionId: text(deployment.model_profile_version_id) } : null,
    promptBindings: {
      state: choice(prompts.state, ['available', 'model_unbound', 'family_missing']), total: integer(prompts.total), activeTotal: integer(prompts.active_total),
      preview: (prompts.preview as unknown[]).map((value) => { const row = record(value); return {
        bindingId: text(row.binding_id), profileId: text(row.profile_id), name: text(row.name), version: integer(row.version), status: text(row.status),
        scope: choice(row.scope, ['exact_model', 'model_family']), operation: text(row.operation), mode: text(row.mode), isActive: boolean(row.is_active),
      } }),
    },
    projector: {
      state: choice(projector.state, ['linked', 'not_recorded', 'invalid']), versionId: projector.version_id === null ? null : text(projector.version_id),
      name: text(projector.name), key: text(projector.key), version: projector.version === null ? null : integer(projector.version),
      status: text(projector.status), isCurrent: boolean(projector.is_current), isActive: boolean(projector.is_active), definitionSha256: text(projector.definition_sha256),
    },
  }
  if (result.projector.state === 'linked' && (!result.projector.versionId || !result.projector.version || !result.projector.key || !result.projector.definitionSha256)) fail()
  if (!result.model && (result.promptBindings.preview.length || result.promptBindings.total)) fail()
  if (result.promptBindings.preview.some(binding => !binding.isActive)) fail()
  return result
}
