import type {
  RegistryDeletionDependency,
  RegistryDeletionDisposition,
  RegistryDeletionImpact,
  RegistryDeletionResourceType,
} from './platformTypes'

const record = (value: unknown, context: string): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${context} 响应不是对象`)
  return value as Record<string, unknown>
}

const text = (value: unknown, context: string): string => {
  if (typeof value !== 'string') throw new Error(`${context} 缺少字符串字段`)
  return value
}

const integer = (value: unknown, context: string): number => {
  if (!Number.isInteger(value) || Number(value) < 0) throw new Error(`${context} 缺少非负整数`)
  return Number(value)
}

const boolean = (value: unknown, context: string): boolean => {
  if (typeof value !== 'boolean') throw new Error(`${context} 缺少布尔字段`)
  return value
}

const stringList = (value: unknown, context: string): string[] => {
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) throw new Error(`${context} 必须是字符串列表`)
  return [...value]
}

const dispositions = new Set<RegistryDeletionDisposition>([
  'selectable_delete',
  'automatic_delete',
  'blocking_reference',
  'preserved_history',
])

const resourceTypes = new Set<RegistryDeletionResourceType>([
  'provider_profile',
  'model_profile_version',
  'model_deployment',
  'model_artifact',
  'workflow_template_version',
  'production_recipe_version',
  'config_definition',
  'prompt_field_definition',
  'prompt_profile_version',
  'prompt_profile_field_rule',
  'prompt_profile_binding',
])

const adaptDependency = (value: unknown, index: number): RegistryDeletionDependency => {
  const row = record(value, `dependencies[${index}]`)
  const disposition = text(row.disposition, `dependencies[${index}].disposition`) as RegistryDeletionDisposition
  if (!dispositions.has(disposition)) throw new Error(`服务端返回未知依赖策略 ${disposition}，已失败关闭`)
  return {
    dependencyKey: text(row.dependency_key, `dependencies[${index}].dependency_key`),
    resourceType: text(row.resource_type, `dependencies[${index}].resource_type`),
    objectId: text(row.object_id, `dependencies[${index}].object_id`),
    label: text(row.label, `dependencies[${index}].label`),
    status: text(row.status, `dependencies[${index}].status`),
    relationship: text(row.relationship, `dependencies[${index}].relationship`),
    relationshipLabel: text(row.relationship_label, `dependencies[${index}].relationship_label`),
    disposition,
    reasonCode: text(row.reason_code, `dependencies[${index}].reason_code`),
    reason: text(row.reason, `dependencies[${index}].reason`),
    referencePaths: stringList(row.reference_paths, `dependencies[${index}].reference_paths`),
    stateSha256: text(row.state_sha256, `dependencies[${index}].state_sha256`),
  }
}

export const adaptRegistryDeletionImpact = (value: unknown): RegistryDeletionImpact => {
  const row = record(value, '删除影响预览')
  const target = record(row.target, 'target')
  const targetPolicy = record(row.target_policy, 'target_policy')
  const summary = record(row.summary, 'summary')
  const resourceType = text(target.resource_type, 'target.resource_type') as RegistryDeletionResourceType
  if (!resourceTypes.has(resourceType)) throw new Error(`服务端返回未知删除资源类型 ${resourceType}，已失败关闭`)
  if (row.schema_version !== 1 || row.requested_action !== 'delete') throw new Error('删除影响预览合同版本不受支持')
  if (!Array.isArray(row.dependencies)) throw new Error('删除影响预览缺少完整 dependencies 清单')
  return {
    schemaVersion: 1,
    requestedAction: 'delete',
    target: {
      resourceType,
      objectId: text(target.object_id, 'target.object_id'),
      label: text(target.label, 'target.label'),
      status: text(target.status, 'target.status'),
      stateSha256: text(target.state_sha256, 'target.state_sha256'),
    },
    targetPolicy: {
      allowed: boolean(targetPolicy.allowed, 'target_policy.allowed'),
      reasonCode: text(targetPolicy.reason_code, 'target_policy.reason_code'),
      reason: text(targetPolicy.reason, 'target_policy.reason'),
    },
    complete: boolean(row.complete, 'complete'),
    canExecute: boolean(row.can_execute, 'can_execute'),
    dependencies: row.dependencies.map(adaptDependency),
    summary: {
      selectableCount: integer(summary.selectable_count, 'summary.selectable_count'),
      automaticCount: integer(summary.automatic_count, 'summary.automatic_count'),
      blockingCount: integer(summary.blocking_count, 'summary.blocking_count'),
      preservedHistoryCount: integer(summary.preserved_history_count, 'summary.preserved_history_count'),
    },
    confirmationPhrase: text(row.confirmation_phrase, 'confirmation_phrase'),
    advisories: stringList(row.advisories, 'advisories'),
    impactSha256: text(row.impact_sha256, 'impact_sha256'),
  }
}
