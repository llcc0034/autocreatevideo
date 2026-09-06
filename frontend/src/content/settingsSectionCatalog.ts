export type SettingsSectionId = 'models' | 'workflows' | 'prompts' | 'routing' | 'defaults' | 'security' | 'runtime' | 'governance'
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
    id: 'models',
    label: '模型',
    englishLabel: 'Models',
    hint: '模型、能力版本、文件证明、运行实例与服务连接',
    settingsRoute: '/settings?section=models',
    guideAnchor: 'settings-models',
  },
  {
    id: 'workflows',
    label: '工作流',
    englishLabel: 'Workflows',
    hint: 'ComfyUI 模板、校验与真实烟测（Smoke Test）',
    settingsRoute: '/settings?section=workflows',
    guideAnchor: 'settings-workflows',
  },
  {
    id: 'prompts',
    label: '提示词策略',
    englishLabel: 'Prompt Profiles',
    hint: '字段装配策略（PromptProfile）与绑定规则（Binding）',
    settingsRoute: '/settings?section=prompts',
    guideAnchor: 'settings-prompts',
  },
  {
    id: 'routing',
    label: '配方与路由',
    englishLabel: 'Recipes & Routing',
    hint: '生产就绪概览、配方版本、阶段和路由',
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
    hint: '受管凭据（SecretBinding）与不可变审计记录',
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
  ...settingsSectionNavigationItems.slice(0, 2),
  {
    id: 'acceptance-smoke',
    label: '能力关联与验收',
    englishLabel: 'Acceptance & Smoke',
    hint: '跨配置关联、缺口、Live Validate、Smoke 与发布门禁',
    settingsRoute: '/settings/acceptance-smoke',
    kind: 'workspace',
  },
  ...settingsSectionNavigationItems.slice(2),
]

export const settingsSectionById = (id: SettingsSectionId) => settingsSectionCatalog.find((item) => item.id === id)!
