import type { WorkflowConfigurationLinks } from './platformTypes'
import type { RouteLocationRaw } from 'vue-router'

export const workflowModelSettingsRoute = (workflowId: string, modelId: string, deploymentId = '') => ({
  path: '/settings', query: {
    section: 'models', model_profile_version: modelId, focus: 'deployments', workflow_context: workflowId,
    ...(deploymentId ? { deployment: deploymentId } : {}),
  },
})
export const workflowExecutionSettingsRoute = (workflowId: string, versionId: string) => ({
  path: '/settings', query: {section: 'executions', execution_version: versionId, workflow_context: workflowId},
})
export const workflowExecutionsRoute = (workflowId: string) => ({
  path: '/settings/acceptance-smoke', query: {view: 'relations', kind: 'execution', source_kind: 'workflow', source_id: workflowId, relation: 'executions'},
})
export const workflowProjectorSettingsRoute = (workflowId: string, versionId: string) => ({
  path: '/settings', query: { section: 'workflows', projector_version: versionId, workflow_context: workflowId },
})

export interface WorkflowRelationItem {
  key: 'model' | 'execution' | 'projector'
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
    key: 'model', label: '校验部署', name: '尚未绑定校验部署', status: '待配置', tone: 'warning',
    description: '进入工作流详情，选择模型部署并完成在线校验。', to: detailRoute,
  }
  if (deployment) {
    if (deployment.status === 'active') {
      Object.assign(model, {
        name: deployment.name, status: '已启用', tone: 'linked',
        description: '查看此工作流的校验部署；部署启用不代表工作流或执行实例组合验收通过。',
        to: workflowModelSettingsRoute(workflowId, deployment.modelProfileVersionId, deployment.id),
      })
    } else {
      Object.assign(model, { name: '部署未启用', status: '未启用', tone: 'muted',
        description: '在更多关联中查看已记录的部署及状态。', to: undefined })
    }
    if (deployment.modelProfileVersionId !== links.model?.id) {
      Object.assign(model, { status: '版本不一致', tone: 'danger',
        description: '校验部署与工作流绑定的模型版本不一致，请查看关联详情。', to: undefined })
    }
  }
  const executions = links.executionInstances
  const first = executions.preview[0]
  const execution: WorkflowRelationItem = {
    key: 'execution', label: '执行实例', name: executions.total ? '暂无启用的发布版本' : '尚无执行实例引用',
    status: executions.total ? '未启用' : '未配置', tone: 'muted',
    description: '查看引用此工作流的精确执行实例；部署、策略和参数在执行实例中固定。',
    to: workflowExecutionsRoute(workflowId),
  }
  if (first) {
    Object.assign(execution, {
      name: `${first.name} v${first.version}`, status: executions.activeTotal > 1 ? `${executions.activeTotal} 个启用版本` : '已启用', tone: 'linked',
      description: '查看已发布执行实例的精确组合；启用不代表组合烟测通过。',
      to: executions.activeTotal === 1 ? workflowExecutionSettingsRoute(workflowId, first.id) : workflowExecutionsRoute(workflowId),
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
  return [model, execution, projector]
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
  const executions = record(raw.execution_instances), projector = record(raw.projector)
  if (!Array.isArray(executions.preview) || executions.preview.length > 3 || executions.preview.length > integer(executions.active_total) || integer(executions.active_total) > integer(executions.total)) fail()
  if (integer(executions.active_total) > 0 && !(executions.preview as unknown[]).length) fail()
  const result: WorkflowConfigurationLinks = {
    model: model ? { id: text(model.id), key: text(model.key), version: integer(model.version), status: text(model.status), instanceCount: integer(model.instance_count) } : null,
    validatedDeployment: deployment ? { id: text(deployment.id), name: text(deployment.name), status: text(deployment.status), modelProfileVersionId: text(deployment.model_profile_version_id) } : null,
    executionInstances: {
      total: integer(executions.total), activeTotal: integer(executions.active_total),
      preview: (executions.preview as unknown[]).map((value) => { const row = record(value); return {
        id: text(row.id), name: text(row.name), version: integer(row.version), status: text(row.status),
        operation: text(row.operation), mode: text(row.mode), promptMode: choice(row.prompt_mode, ['profile', 'manual', 'none']),
      } }),
    },
    projector: {
      state: choice(projector.state, ['linked', 'not_recorded', 'invalid']), versionId: projector.version_id === null ? null : text(projector.version_id),
      name: text(projector.name), key: text(projector.key), version: projector.version === null ? null : integer(projector.version),
      status: text(projector.status), isCurrent: boolean(projector.is_current), isActive: boolean(projector.is_active), definitionSha256: text(projector.definition_sha256),
    },
  }
  if (result.projector.state === 'linked' && (!result.projector.versionId || !result.projector.version || !result.projector.key || !result.projector.definitionSha256)) fail()
  if (result.executionInstances.preview.some(row => row.status !== 'published')) fail()
  if (new Set(result.executionInstances.preview.map(row => row.id)).size !== result.executionInstances.preview.length) fail()
  return result
}
