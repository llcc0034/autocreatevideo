export type SettingsSectionId = 'executions' | 'models' | 'workflows' | 'fields' | 'prompts' | 'routing' | 'defaults' | 'security' | 'runtime' | 'governance'
export type SettingsPrimaryNavigationId = SettingsSectionId | 'acceptance-smoke'

export type SettingsSectionCatalogItem = {
  id: SettingsSectionId
  label: string
  englishLabel: string
  hint: string
  settingsRoute: string
  guideAnchor: string
  writeRequiresSuperuser?: boolean
}

export type SettingsPrimaryNavigationItem = {
  id: SettingsPrimaryNavigationId
  label: string
  englishLabel: string
  hint: string
  settingsRoute: string
  kind: 'section' | 'workspace'
}

/**
 * 系统设置分区的唯一展示目录。
 * Settings、图文指南和相关测试都复用这里的 ID、双语名称和深链。
 */
export const settingsSectionCatalog: SettingsSectionCatalogItem[] = [
  {
    id: 'executions',
    label: '执行实例',
    englishLabel: 'Execution Instances',
    hint: '输入准备、模型能力调用、输出与精确资源版本',
    settingsRoute: '/settings?section=executions',
    guideAnchor: 'settings-executions',
  },
  {
    id: 'models',
    label: '模型',
    englishLabel: 'Models',
    hint: '模型、能力版本、文件证明、模型部署与服务连接',
    settingsRoute: '/settings?section=models',
    guideAnchor: 'settings-models',
  },
  {
    id: 'workflows',
    label: '工作流',
    englishLabel: 'Workflows',
    hint: '可复用实现：工作流、Projector 与导入节点事实',
    settingsRoute: '/settings?section=workflows',
    guideAnchor: 'settings-workflows',
  },
  {
    id: 'fields',
    label: '字段维护',
    englishLabel: 'Fields',
    hint: '通用字段身份、定义版本、内容来源与引用影响',
    settingsRoute: '/settings?section=fields',
    guideAnchor: 'settings-fields',
  },
  {
    id: 'prompts',
    label: '提示词策略',
    englishLabel: 'Prompt Profiles',
    hint: '字段选用、提示词策略版本与精确执行实例关联',
    settingsRoute: '/settings?section=prompts',
    guideAnchor: 'settings-prompts',
  },
  {
    id: 'routing',
    label: '生产配方',
    englishLabel: 'Production Recipes',
    hint: '配方步骤、执行实例、输入来源与生产就绪概览',
    settingsRoute: '/settings?section=routing',
    guideAnchor: 'settings-routing',
  },
  {
    id: 'defaults',
    label: '默认值与限制',
    englishLabel: 'Defaults & Limits',
    hint: '生效配置（Effective Configuration）与修订版本（Revision）',
    settingsRoute: '/settings?section=defaults',
    guideAnchor: 'settings-defaults',
  },
  {
    id: 'security',
    label: '安全边界',
    englishLabel: 'Security Boundaries',
    hint: '绝对资源上限、失败关闭（fail closed）与高风险变更',
    settingsRoute: '/settings?section=security',
    guideAnchor: 'settings-security',
    writeRequiresSuperuser: true,
  },
  {
    id: 'runtime',
    label: '运行主机',
    englishLabel: 'Runtime Hosts',
    hint: '主机总体情况、探测（Probe）、快照与推荐',
    settingsRoute: '/settings?section=runtime',
    guideAnchor: 'settings-runtime',
  },
  {
    id: 'governance',
    label: '凭据与审计',
    englishLabel: 'Credentials & Audit',
    hint: '受管凭据（SecretBinding）、操作审计与业务历史清理',
    settingsRoute: '/settings?section=governance',
    guideAnchor: 'settings-governance',
  },
]

const settingsSectionNavigationItems = settingsSectionCatalog.map((item) => ({
  id: item.id,
  label: item.label,
  englishLabel: item.englishLabel,
  hint: item.hint,
  settingsRoute: item.settingsRoute,
  kind: 'section' as const,
}))

/**
 * 系统设置页及其独立工作台共用的一级导航。
 * 验收与烟测是跨模型、工作流和运行主机的工作台，不属于可编辑配置分区，
 * 因此不进入 settingsSectionCatalog 或图文指南的配置分区计数。
 */
export const settingsPrimaryNavigation: SettingsPrimaryNavigationItem[] = [
  ...settingsSectionNavigationItems,
  {
    id: 'acceptance-smoke',
    label: '能力关联与验收',
    englishLabel: 'Acceptance & Smoke',
    hint: '跨配置关联、缺口、Live Validate、Smoke 与发布门禁',
    settingsRoute: '/settings/acceptance-smoke',
    kind: 'workspace',
  },
]

/** Display hierarchy only. Resource ownership and invocation stay in server contracts. */
export const settingsNavigationGroups: { id: string; label: string; items: SettingsPrimaryNavigationId[] }[] = [
  { id: 'production', label: '生产编排与调用', items: ['routing', 'executions'] },
  { id: 'resources', label: '公共资源 · 定义与实现', items: ['fields', 'prompts', 'models', 'workflows'] },
  { id: 'operations', label: '验证与运行管理', items: ['acceptance-smoke', 'runtime', 'defaults', 'security', 'governance'] },
]

/** Backend resource links retain exact identities; only their UI destination changes. */
export function settingsSectionForQuery(query: Record<string, unknown>): SettingsSectionId {
  if (query.execution_version) return 'executions'
  const section = String(query.section || 'executions')
  return settingsSectionCatalog.some(item => item.id === section) ? section as SettingsSectionId : 'executions'
}

export const settingsSectionById = (id: SettingsSectionId) => settingsSectionCatalog.find((item) => item.id === id)!
