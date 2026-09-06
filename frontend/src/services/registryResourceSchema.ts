import type {
  ConfigDefinition,
  ConfigDefinitionOptions,
  JsonObject,
  ModelDeployment,
  ModelProfileRegistryOptions,
  ModelProfileVersion,
  ProviderProfile,
  ProductionRecipe,
  ProductionRecipeEditorOptions,
  RuntimeHost,
  WorkflowTemplateVersion,
} from './platformTypes'
import { driverLabel, freshnessLabel, stageLabel, valueTypeLabel } from './presentationLabels'
import { nodeIdleOverrideErrors, type NodeIdleLimits } from './modelNodeIdleOverrides'

export type RegistryResource = 'provider' | 'model' | 'deployment' | 'artifact' | 'recipe' | 'config-definition'

export interface RegistryOption {
  value: string
  label: string
  disabled?: boolean
  hint?: string
}

export interface RegistryFieldSchema {
  path: string
  label: string
  kind: 'text' | 'textarea' | 'number' | 'boolean' | 'select' | 'catalog' | 'multiselect' | 'readonly' | 'tags' | 'mapping' | 'json' | 'node-idle-overrides'
  description: string
  impact: string
  required?: boolean
  defaultValue?: unknown
  placeholder?: string
  min?: number
  max?: number
  step?: number
  pattern?: string
  options?: RegistryOption[]
  multipleValue?: 'array' | 'csv'
  inputPurpose?: 'business' | 'catalog' | 'system'
  allowCustom?: boolean
  submit?: boolean
  advanced?: boolean
  visibleWhen?: (payload: JsonObject) => boolean
}

// Registry payloads are JSON contracts, but Vue hands this service reactive
// proxies. `structuredClone()` rejects proxies in real browsers, so normalize
// through JSON before producing the immutable editor copy.
const cloneJson = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T

export interface RegistryEditorDependencies {
  providers: ProviderProfile[]
  modelProfiles: ModelProfileVersion[]
  deployments: ModelDeployment[]
  runtimeHosts: RuntimeHost[]
  workflowVersions: WorkflowTemplateVersion[]
  recipes?: ProductionRecipe[]
  configDefinitions?: ConfigDefinition[]
  recipeEditorOptions?: ProductionRecipeEditorOptions | null
  configDefinitionOptions?: ConfigDefinitionOptions | null
  modelProfileOptions?: ModelProfileRegistryOptions | null
  executionMonitoringLimits?: NodeIdleLimits | null
  lockedPaths?: string[]
}

export interface RegistryValidation {
  errors: string[]
  warnings: string[]
}

const CHAT_REQUEST_RESERVED_FIELDS = new Set([
  'input', 'model', 'messages', 'prompt', 'max_completion_tokens',
  'max_new_tokens', 'max_output_tokens', 'max_tokens', 'stream', 'seed',
  'timeout', 'timeout_seconds', 'chat_request_options', 'chat_completion_options',
])
const CHAT_REQUEST_TRANSPORT_FIELD_MARKERS = ['endpoint', 'timeout'] as const
const CHAT_REQUEST_TRANSPORT_FIELDS = new Set([
  'api_base', 'base_url', 'chat_path', 'endpoint', 'endpoint_url',
  'headers', 'host', 'url',
])
const CHAT_REQUEST_OPTION_KEY = /^[A-Za-z][A-Za-z0-9_.-]{0,79}$/
const FORBIDDEN_SECRET_SINGLE_TOKENS = new Set([
  'accesskey', 'accesstoken', 'apikey', 'apisecret', 'authtoken',
  'authorization', 'bearer', 'bearertoken', 'clientsecret', 'credential',
  'credentials', 'idtoken', 'password', 'privatekey', 'refreshtoken',
  'secret', 'secretkey', 'secretvalue', 'token',
])
const FORBIDDEN_SECRET_TOKEN_PAIRS = new Set([
  'access:key', 'access:token', 'api:key', 'api:secret', 'auth:token',
  'bearer:token', 'client:secret', 'credential:ref', 'id:token',
  'private:key', 'refresh:token', 'secret:key', 'secret:value',
])

const modelHasPromptNormalization = (payload: JsonObject): boolean => {
  const capabilities = getPath(payload, 'specification.capabilities')
  return Array.isArray(capabilities) && capabilities.some((value) => value === 'prompt.normalize')
}

const modelHasVisualRules = (payload: JsonObject): boolean => {
  const capabilities = getPath(payload, 'specification.capabilities')
  return Array.isArray(capabilities) && capabilities.some((value) => value === 'visual_rules.generate')
}

const requestFieldLabels: Record<string, string> = {
  model: '模型（model）',
  prompt: '提示词（prompt）',
  negative_prompt: '负面提示词（negative_prompt）',
  width: '宽度（width）',
  height: '高度（height）',
  duration_seconds: '时长秒数（duration_seconds）',
  fps: '帧率（fps）',
  seed: '随机种子（seed）',
  audio: '音频（audio）',
}

const driverConfigFields: RegistryFieldSchema[] = [
  {
    path: 'driver_config.runtime_source', label: '运行来源', kind: 'select',
    description: '声明驱动从哪里取得运行时服务；不会从旧环境变量静默回退。',
    impact: '影响文本模型是否具备可执行端点。',
    required: true,
    options: [
      { value: 'deployment', label: '由具体部署填写 / Deployment-owned' },
      { value: 'openai_compatible_endpoint', label: '受控兼容 OpenAI 的端点（OpenAI-compatible endpoint）' },
    ],
    visibleWhen: (payload) => ['local_qwen', 'deepseek_api', 'comfyui'].includes(String(payload.driver_type || '')),
  },
  {
    path: 'driver_config.base_url', label: '服务根地址', kind: 'text', required: true,
    description: '仅填写无账号、无密码、无查询参数（query）或片段（fragment）的 HTTP(S) 根地址；ComfyUI 工作流执行器可留空并在具体模型部署（Deployment）覆盖。',
    impact: '缺失或不在白名单会阻断探测、烟测和生产任务。', placeholder: 'http://127.0.0.1:8188',
  },
  {
    path: 'driver_config.chat_path', label: '聊天接口路径（Chat API path）', kind: 'text',
    description: '兼容 OpenAI 的对话补全接口（OpenAI-compatible chat completion）相对路径。', impact: '影响文本生成与提示词扩写请求地址。',
    visibleWhen: (payload) => ['local_qwen', 'deepseek_api', 'openai_compatible'].includes(String(payload.driver_type || '')),
  },
  {
    path: 'driver_config.timeout_seconds', label: '请求超时', kind: 'number',
    description: '单次网络调用的最大等待秒数。', impact: '过短会导致正常任务失败；过长会延迟故障反馈。',
    step: 0.5,
  },
  {
    path: 'driver_config.pre_submit_resource_release.schema_version', label: '提交前资源释放合同版本', kind: 'number', min: 1, max: 1, step: 1,
    description: 'ComfyUI 进程级资源释放合同版本；只有完整填写本组四项时才生效，当前仅支持 1。', impact: '该配置随 Provider/Deployment 进入版本摘要，变化会使旧工作流校验与烟测证据失效。',
    visibleWhen: (payload) => payload.driver_type === 'comfyui',
  },
  {
    path: 'driver_config.pre_submit_resource_release.enabled', label: '新提交前释放 ComfyUI 资源', kind: 'boolean',
    description: '仅对尚未持久化 submission token 的新任务，在 POST /prompt 前调用 ComfyUI /free；恢复、轮询和收集不会调用。', impact: '启用后要求媒体任务并发上限为 1；释放未确认会明确失败且不提交生成请求。',
    visibleWhen: (payload) => payload.driver_type === 'comfyui',
  },
  {
    path: 'driver_config.pre_submit_resource_release.unload_models', label: '卸载已加载模型', kind: 'boolean',
    description: '映射 ComfyUI /free 的 unload_models；用于不同模型工作流交接前释放已加载权重。', impact: '会增加下一次模型加载时间，但可减少上一工作流模型残留占用。',
    visibleWhen: (payload) => payload.driver_type === 'comfyui',
  },
  {
    path: 'driver_config.pre_submit_resource_release.free_memory', label: '回收空闲显存', kind: 'boolean',
    description: '映射 ComfyUI /free 的 free_memory；与卸载模型可独立组合。', impact: '至少选择一项释放动作；停用策略时两项必须同时关闭。',
    visibleWhen: (payload) => payload.driver_type === 'comfyui',
  },
  {
    path: 'driver_config.health_path', label: '健康检查路径', kind: 'text', required: true,
    description: '异步接口（REST）健康检查的相对路径。', impact: '影响部署探测（probe）是否能建立可用性证据。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.submit_path', label: '提交路径', kind: 'text', required: true,
    description: '异步接口（REST）创建任务的相对路径。', impact: '影响真实图片/视频请求的提交位置。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.poll_path', label: '查询路径', kind: 'text', required: true,
    description: '异步任务状态查询路径，只允许一个请求编号占位符（{request_id}）。', impact: '影响后台执行器（worker）读取远端终态。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.cancel_path', label: '取消路径', kind: 'text', required: true,
    description: '异步任务取消路径，只允许一个请求编号占位符（{request_id}）。', impact: '影响安全取消能力。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.collect_path', label: '产物收集路径', kind: 'text', required: true,
    description: '异步任务完成后读取产物清单（manifest）的路径。', impact: '影响图片或视频能否进入本地资产事实。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.poll_interval_seconds', label: '轮询间隔', kind: 'number', required: true,
    description: '异步任务或 ComfyUI 素材任务两次查询之间的秒数。', impact: '当前安全范围由服务端配置校验；前端不复制上下限。', step: 0.25,
    visibleWhen: (payload) => ['async_rest', 'comfyui'].includes(String(payload.driver_type || '')),
  },
  {
    path: 'driver_config.max_poll_seconds', label: '最长轮询时间', kind: 'number', required: true,
    description: '异步任务或 ComfyUI 素材任务允许轮询的总秒数。', impact: '到期后任务会显式失败或进入待消歧状态；当前安全范围由服务端配置校验。', step: 1,
    visibleWhen: (payload) => ['async_rest', 'comfyui'].includes(String(payload.driver_type || '')),
  },
  {
    path: 'driver_config.terminal_success', label: '成功终态', kind: 'tags', required: true,
    description: '远端返回这些状态之一时视为成功；逗号分隔。', impact: '配置错误会把真实远端状态误判并阻断产物收集。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.terminal_failure', label: '失败终态', kind: 'tags', required: true,
    description: '远端返回这些状态之一时视为失败；逗号分隔。', impact: '影响失败闭环与重试判断。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.terminal_cancelled', label: '取消终态', kind: 'tags', required: true,
    description: '远端返回这些状态之一时视为已取消；逗号分隔。', impact: '影响取消请求的最终确认。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.request_id_field', label: '请求编号字段（Request ID field）', kind: 'text', required: true,
    description: '远程提交响应中承载任务编号（ID）的单层 JSON 字段名。', impact: '字段错误会导致已提交任务无法持久化远程编号（ID）。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.status_field', label: '状态字段', kind: 'text', required: true,
    description: '远程查询响应中承载任务状态的单层 JSON 字段名。', impact: '影响轮询、终态识别与失败闭环。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.artifacts_field', label: '产物列表字段', kind: 'text', required: true,
    description: '收集响应中产物数组的字段名。', impact: '字段错误会使已完成任务无法形成本地媒体事实。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.artifact_url_field', label: '产物地址字段（Artifact URL field）', kind: 'text', required: true,
    description: '单个产物中的受限下载地址（URL）字段名。', impact: '只会下载到受控主机白名单。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.artifact_kind_field', label: '产物类型字段', kind: 'text', required: true,
    description: '单个产物中的图片（image）、视频（video）或音频（audio）类型字段名。', impact: '影响媒体探测与后续装配。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.artifact_id_field', label: '产物编号字段（Artifact ID field）', kind: 'text', required: true,
    description: '单个产物中可复核远端产物身份的单层字段名。', impact: '进入产物清单（manifest），便于追踪而不暴露下载凭据。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.artifact_sha256_field', label: '产物 SHA-256 字段', kind: 'text', required: true,
    description: '单个产物中远端提供的内容摘要字段名。', impact: '若远端提供摘要，本地物化时会用于完整性核对。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.actual_cost_field', label: '实际费用字段', kind: 'text', required: true,
    description: '远端响应中本次任务实际费用的单层字段名。', impact: '影响成本事实记录；不会据此静默估算缺失费用。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.currency_field', label: '费用币种字段', kind: 'text', required: true,
    description: '远端响应中费用币种的单层字段名。', impact: '与实际费用共同形成可审计成本事实。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.artifact_allowed_hosts', label: '产物主机白名单', kind: 'tags', required: true,
    description: '只允许从这些已同时进入服务端全局白名单的小写主机名下载产物。', impact: '缺失会阻断异步接口（async REST）服务提供方（Provider）保存；不会为方便而放开服务端请求伪造（SSRF）安全边界。', placeholder: 'media.example.com, cdn.example.com',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.request_field_map', label: '请求字段映射', kind: 'mapping', required: true,
    description: '将平台标准语义映射到远程接口（API）的单层字段名；模型（model）和提示词（prompt）必须配置。', impact: '该合同决定真实生成请求如何装配，错误会阻断保存或烟测。',
    options: Object.entries(requestFieldLabels).map(([value, label]) => ({ value, label })),
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.idempotency_header', label: '幂等请求头', kind: 'text', required: true,
    description: '提交请求中传递幂等键的 HTTP 请求头（header）名称。', impact: '影响不确定提交后的安全恢复与去重。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.progress_field', label: '进度字段', kind: 'text',
    description: '可选的远端进度数值单层字段名；留空表示远端没有可信进度。', impact: '配置后后台执行器（worker）才会记录远端进度，不会用轮询次数伪造。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.progress_max', label: '进度满值', kind: 'number', required: true,
    description: '远端进度达到该值时表示 100%；仅在配置进度字段时使用。', impact: '错误满值会造成进度比例失真，但不改变真实终态。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest' && Boolean(getPath(payload, 'driver_config.progress_field')),
  },
  {
    path: 'driver_config.capability_ttl_seconds', label: '能力快照有效期', kind: 'number', required: true, step: 1,
    description: '远端能力（capability）探测事实允许用于路由的秒数。', impact: '过期后生产会明确阻断；当前安全范围由服务端配置校验，前端不复制上下限。',
  },
  {
    path: 'driver_config.pricing.currency', label: '估算币种', kind: 'text',
    description: '国际标准化组织（ISO）风格的大写币种/计价单位（3–12 位），例如美元（USD）、人民币（CNY）。', impact: '与单次固定费用共同用于提交前成本估算；缺失时明确显示未知（unknown）。', placeholder: 'USD',
  },
  {
    path: 'driver_config.pricing.fixed_per_request', label: '单次固定费用', kind: 'number', min: 0,
    description: '每次服务提供方（Provider）请求的非负固定估算金额。', impact: '影响预算门禁与成本预估，不替代远端实际费用事实。', placeholder: '0',
  },
  {
    path: 'driver_config', label: '驱动扩展合同', kind: 'json', advanced: true,
    description: '仅用于当前驱动已支持但上方没有独立控件的未来扩展字段；常用地址、字段映射、进度与计价均应优先使用类型化控件。',
    impact: '服务端会拒绝未知字段、敏感字段和不受控地址；这里不会保存密钥。',
  },
]

const deploymentRuntimeFields: RegistryFieldSchema[] = driverConfigFields.map((field) => ({
  ...field,
  path: field.path.replace(/^driver_config/, 'runtime_config'),
  // A deployment runtime_config is an overlay on Provider driver_config.
  // Empty values therefore mean inherit, never "missing required provider config".
  required: false,
  defaultValue: undefined,
  label: field.advanced ? '部署覆盖合同' : field.label,
  description: field.advanced
    ? '仅填写该部署需要覆盖 Provider 的非敏感参数；空对象表示完全继承 Provider。'
    : field.description,
}))

export const getPath = (payload: JsonObject, path: string): unknown => {
  let current: unknown = payload
  for (const segment of path.split('.')) {
    if (!current || typeof current !== 'object' || Array.isArray(current)) return undefined
    current = (current as JsonObject)[segment]
  }
  return current
}

export const setPath = (payload: JsonObject, path: string, value: unknown): JsonObject => {
  const result = cloneJson(payload)
  const segments = path.split('.')
  let current = result
  segments.forEach((segment, index) => {
    if (index === segments.length - 1) {
      if (value === undefined || value === '') delete current[segment]
      else current[segment] = value
      return
    }
    const next = current[segment]
    if (!next || typeof next !== 'object' || Array.isArray(next)) current[segment] = {}
    current = current[segment] as JsonObject
  })
  return result
}

const csvValues = (value: unknown): string[] => String(value || '').split(',').map((item) => item.trim()).filter(Boolean)
const arrayValues = (value: unknown): string[] => Array.isArray(value) ? value.map(String).filter(Boolean) : []
const choices = (items: readonly { value: string; label: string }[] = []): RegistryOption[] => items.map((item) => ({
  value: item.value,
  label: item.label,
}))
const withCurrentOptions = (options: RegistryOption[], current: readonly string[]): RegistryOption[] => {
  const known = new Set(options.map((item) => item.value))
  return [
    ...options,
    ...current.filter((value) => value && !known.has(value)).map((value) => ({
      value,
      label: `历史已登记值 · ${value}`,
      hint: '仅用于保留或移除已有值；不会注册新的执行能力。',
    })),
  ]
}

const basicFields = (dependencies: RegistryEditorDependencies, payload: JsonObject): Record<RegistryResource, RegistryFieldSchema[]> => {
  const registry = dependencies.modelProfileOptions
  const selectedDriver = registry?.providerDrivers.find((item) => item.value === payload.driver_type)
  const provider = dependencies.providers.find((item) => item.id === payload.provider)
  const selectedModelDriver = provider?.driverType || ''
  const selectedModality = String(payload.modality || '')
  const selectedCapabilities = arrayValues(getPath(payload, 'specification.capabilities'))
  const capabilityOptions: RegistryOption[] = withCurrentOptions((registry?.capabilities || [])
    .filter((item) => !item.legacy || selectedCapabilities.includes(item.value))
    .filter((item) => (
      selectedCapabilities.includes(item.value)
      || ((!item.driverTypes.length || item.driverTypes.includes(selectedModelDriver))
        && (!item.modalities.length || item.modalities.includes(selectedModality)))
    ))
    .map((item) => ({
      value: item.value,
      label: item.label,
      hint: item.legacy ? '历史已登记能力；保留前请复核当前执行合同。' : undefined,
    })), selectedCapabilities)
  const currentFamily = String(getPath(payload, 'specification.model_family') || '').trim()
  const profileKey = String(payload.profile_key || '').trim()
  const familyValues = [...new Set([
    ...(registry?.modelFamilies || []),
    currentFamily,
    ...(profileKey && /^[a-z][a-z0-9_.-]{1,99}$/.test(profileKey) ? [profileKey] : []),
  ].filter(Boolean))]
  const familyOptions = familyValues.map((value) => ({
    value,
    label: (value === profileKey && !(registry?.modelFamilies || []).includes(value))
      ? `新建提示词策略族 · ${value}`
      : value,
  }))
  const recipeOptions = dependencies.recipeEditorOptions
  const configOptions = dependencies.configDefinitionOptions
  const recipeKeyLocked = dependencies.lockedPaths?.includes('recipe_key') === true
  const recipeKeyOptions = [...new Set((dependencies.recipes || []).map((item) => item.recipeKey))]
    .map((value) => ({
      value,
      label: recipeKeyLocked ? value : `${value}（已有配方族，请创建后继草稿）`,
      disabled: !recipeKeyLocked,
      hint: recipeKeyLocked ? undefined : '已有配方族不能从普通新增入口创建版本。',
    }))
  const configDomainOptions = withCurrentOptions(
    choices(configOptions?.domains),
    [String(payload.domain || '')],
  )
  const configUnitOptions = withCurrentOptions(
    choices(configOptions?.units),
    [String(payload.unit || '')],
  )
  const configModuleOptions = withCurrentOptions(
    choices(configOptions?.impactModules),
    arrayValues(payload.impact_modules),
  )
  return ({
  provider: [
    { path: 'key', label: '唯一键（Key）', kind: 'text', required: true, pattern: '^[a-z][a-z0-9_.-]{1,79}$', description: '服务提供方（Provider）的稳定机器标识，创建后不应随显示名称变化。', impact: '被模型版本和审计记录引用。', placeholder: 'deepseek-primary' },
    { path: 'name', label: '显示名称', kind: 'text', required: true, description: '运营页面展示的名称。', impact: '仅影响识别，不改变路由。' },
    { path: 'driver_type', label: '驱动类型', kind: 'select', required: true, options: choices(registry?.providerDrivers), description: '选项来自服务端已注册驱动目录。', impact: '决定允许的配置字段、探测方式与可执行操作。' },
    { path: 'modalities', label: '支持模态 / Supported modalities', kind: 'multiselect', multipleValue: 'csv', required: true, options: withCurrentOptions(choices((registry?.modalities || []).filter((item) => !selectedDriver?.modalities.length || selectedDriver.modalities.includes(item.value))), csvValues(payload.modalities)), description: '从注册驱动涉及的媒体类型中选择说明性运营标记；不使用本字段做执行授权、能力兼容性校验或候选筛选。', impact: '真实能力仍由注册驱动、已发布模型版本、模型部署/工作流合同和新鲜真实烟测共同证明。' },
    { path: 'operations', label: '声明操作 / Declared operations', kind: 'multiselect', multipleValue: 'csv', options: withCurrentOptions((selectedDriver?.operations || []).map((value) => ({ value, label: value })), csvValues(payload.operations)), description: '从该注册驱动声明的传输操作中选择说明性运营标记；不使用本字段做执行授权、能力兼容性校验或候选筛选。', impact: '真实能力仍由注册驱动、已发布模型版本、模型部署/工作流合同和新鲜真实烟测共同证明；不能输入任意操作。' },
    { path: 'is_active', label: '启用服务提供方（Provider）', kind: 'boolean', defaultValue: true, description: '是否允许新部署和生产路由使用。', impact: '停用后相关能力会明确阻断（fail closed），历史记录保留。' },
    ...driverConfigFields,
  ],
  model: [
    { path: 'profile_key', label: '模型版本族标识（Profile Key）', kind: 'text', required: true, pattern: '^[a-z][a-z0-9_.-]{1,99}$', description: '同一模型记录跨版本共享的稳定标识；与提示词策略族是两个概念。', impact: '创建后锁定，用于版本管理和生产路由引用。', placeholder: 'minimax-h3-video' },
    { path: 'version', label: '版本号', kind: 'readonly', description: '保存时由服务端按该模型版本族的全部历史自动分配。', impact: '不接受人工输入；并发创建由服务端锁定并消歧。', placeholder: '保存后自动生成' },
    { path: 'provider', label: '服务提供方（Provider）', kind: 'select', required: true, options: dependencies.providers.filter((item) => item.isActive).map((item) => ({ value: item.id, label: `${item.name} · ${driverLabel(item.driverType)}` })), description: '从已启用的服务提供方（Provider）中选择前置依赖。', impact: '决定调用协议、凭据来源和可用部署。' },
    { path: 'model_identifier', label: '实际模型标识', kind: 'text', required: true, description: '提交给模型服务的模型名或本地目录标识。', impact: '生产请求会冻结并使用此标识。', placeholder: 'deepseek-chat' },
    { path: 'modality', label: '主模态', kind: 'select', required: true, options: choices(registry?.modalities), description: '选项来自服务端模型模态枚举。', impact: '影响路由兼容性、能力候选和工作流选择。' },
    { path: 'specification.schema_version', label: '模型规格结构版本', kind: 'readonly', required: true, defaultValue: registry?.specificationSchemaVersion, description: '由服务端维护的合同结构版本。', impact: '不会要求操作员输入或猜测。' },
    { path: 'specification.capabilities', label: '能力列表', kind: 'multiselect', multipleValue: 'array', required: true, options: capabilityOptions, description: '按所选 Provider 驱动和主模态展示服务端受控业务能力；历史值只在原合同中显式保留。', impact: '选择只是合同声明，仍需发布校验及相应 Deployment/Workflow 与真实 Smoke 证据。' },
    { path: 'specification.model_family', label: '提示词策略族', kind: 'select', required: true, options: familyOptions, description: '从服务端已有策略族中选择；新模型也可显式选择以当前 Profile Key 登记同名策略族。', impact: '影响提示词策略（Prompt Profile）匹配，不会从 Provider 或模型名自动猜测。' },
    { path: 'specification.prompt_normalization.max_input_bytes', label: '标准化最大输入字节 / Max input bytes', kind: 'number', required: true, min: 1, step: 1, description: '单批实际聊天消息允许的 UTF-8 字节上限；只在能力列表包含 prompt.normalize 时显示。', impact: '最终可接受范围由当前服务端安全边界校验；前端不复制上限。', visibleWhen: modelHasPromptNormalization },
    { path: 'specification.prompt_normalization.input_byte_warning_threshold', label: '输入字节建议阈值 / Input byte advisory threshold', kind: 'number', min: 1, step: 1, description: '可选软阈值；实际聊天消息超过后记录警告，但不会拆批、重试或判定失败。', impact: '只要不大于本版本 max_input_bytes 即可；不设置隐含默认。', visibleWhen: modelHasPromptNormalization },
    { path: 'specification.prompt_normalization.max_output_tokens', label: '标准化最大输出令牌 / Max output tokens', kind: 'number', required: true, min: 1, step: 1, description: '每批模型响应的最大令牌预算。', impact: '安全绝对上限由服务端当前配置校验，该值随模型版本冻结。', visibleWhen: modelHasPromptNormalization },
    { path: 'specification.prompt_normalization.output_token_warning_threshold', label: '输出令牌建议阈值 / Output token advisory threshold', kind: 'number', min: 1, step: 1, description: '可选软阈值；实际输出超过后记录警告，但不会因此判定失败。', impact: '只要不大于本版本 max_output_tokens 即可。', visibleWhen: modelHasPromptNormalization },
    { path: 'specification.prompt_normalization.max_items_per_batch', label: '标准化每批最大字段数 / Max items per batch', kind: 'number', required: true, min: 1, step: 1, description: '每次 prompt.normalize 请求最多包含的字段条数。', impact: '当前安全上限由服务端校验，不会把成功批次随失败批次重复提交。', visibleWhen: modelHasPromptNormalization },
    { path: 'specification.prompt_normalization.timeout_seconds', label: '标准化请求超时 / Timeout seconds', kind: 'number', required: true, step: 0.5, description: '单批文本模型传输的有限正数秒数。', impact: '服务端按当前安全边界校验；不改变 ProductionRun 总体任务时限。', visibleWhen: modelHasPromptNormalization },
    { path: 'specification.prompt_normalization.chat_request_options', label: '受控请求选项 / Controlled request options', kind: 'json', required: true, advanced: true, description: '必须显式冻结的非敏感 JSON 对象；可为空，也可按当前 Provider 能力声明 temperature、response_format、thinking 等选项。', impact: '不得包含凭据、传输地址/超时，不得覆盖 model、messages、输出预算、seed 或包装字段；字节和结构上限读取当前安全配置。', visibleWhen: modelHasPromptNormalization },
    { path: 'specification.visual_rules.max_input_bytes', label: '影视规则最大输入字节 / Visual-rules max input bytes', kind: 'number', required: true, min: 1, step: 1, description: '完整已确认剧本、Bible 与规则上下文允许的 UTF-8 硬容量；只在能力列表包含 visual_rules.generate 时显示。', impact: '安全绝对上限由服务端当前配置校验；前端不复制上限。', visibleWhen: modelHasVisualRules },
    { path: 'specification.visual_rules.input_byte_warning_threshold', label: '影视规则输入建议阈值 / Input advisory threshold', kind: 'number', min: 1, step: 1, description: '可选建议值；超过后只记录告警，仍提交完整剧本。', impact: '不得大于本版本 max_input_bytes。', visibleWhen: modelHasVisualRules },
    { path: 'specification.visual_rules.max_output_tokens', label: '影视规则最大输出令牌 / Visual-rules max output tokens', kind: 'number', required: true, min: 1, step: 1, description: 'Provider 请求的真实最大完成预算，不是推荐篇幅。', impact: '安全绝对上限由服务端校验；Provider 明示 length 仍表示结果不完整。', visibleWhen: modelHasVisualRules },
    { path: 'specification.visual_rules.output_token_warning_threshold', label: '影视规则输出建议阈值 / Output advisory threshold', kind: 'number', min: 1, step: 1, description: '可选建议值；已通过严格结构校验的输出超过后只告警，不阻断审核。', impact: '不得大于本版本 max_output_tokens。', visibleWhen: modelHasVisualRules },
    { path: 'specification.visual_rules.timeout_seconds', label: '影视规则请求超时 / Visual-rules timeout', kind: 'number', required: true, step: 0.5, description: '单次 visual_rules.generate 的有限正数传输时长。', impact: '当前安全上限由服务端校验；不改变 ProductionRun 总体任务硬时限。', visibleWhen: modelHasVisualRules },
    { path: 'specification.visual_rules.quality_advisories', label: '影视规则质量建议 / Visual-rules quality advisories', kind: 'json', advanced: true, description: '可选 schema_version=1 对象。可按需声明 base_colors_min/max_items、negative_constraints_min/max_items、negative_constraint_max_chars、recommended_negative_clauses，以及 continuity_summary、negative_prompt、color_script 的 max_chars；省略任一字段就不启用该项隐含建议。', impact: '命中只写入候选 warnings 与 advisory audit；建议负面短语只在配置存在时合并且人工可移除，不截断、不重试、不阻断结构完整且安全的候选。数组类型、枚举、唯一性、可打印英文、来源回显和绝对 JSON/请求上限仍硬校验。', visibleWhen: modelHasVisualRules },
    { path: 'specification.visual_rules.chat_request_options', label: '影视规则受控请求选项 / Visual-rules request options', kind: 'json', required: true, advanced: true, description: '当前模型版本必须显式冻结的非敏感 JSON；可为空，也可配置 temperature、response_format、thinking 等已声明选项。', impact: '不得覆盖模型、消息、输出预算、种子、传输地址或超时，也不得包含凭据。', visibleWhen: modelHasVisualRules },
    { path: 'specification.execution_monitoring.schema_version', label: '执行监控合同版本 / Execution monitoring schema', kind: 'readonly', required: true, defaultValue: 1, description: '媒体模型的通用长节点监控合同版本；由系统维护，当前只支持 1。', impact: '发布后冻结，新合同版本需新建模型版本。', visibleWhen: (payload) => ['image', 'video', 'multimodal'].includes(String(payload.modality || '')) },
    { path: 'specification.execution_monitoring.default_idle_timeout_seconds', label: '默认静默时限 / Default idle timeout', kind: 'number', step: 1, description: '没有更具体节点规则时使用的正整数秒数；留空则沿用任务冻结的全局静默时限。', impact: '当前安全范围由服务端配置校验；只影响未来任务且不能延长硬时限。', visibleWhen: (payload) => ['image', 'video', 'multimodal'].includes(String(payload.modality || '')) },
    { path: 'specification.execution_monitoring.node_type_overrides', label: '特定节点等待时限（高级）', kind: 'node-idle-overrides', advanced: true, description: '通常无需设置；从节点目录选择类型，按分钟填写等待时长。', impact: '保存在同一模型合同中，仍按正整数秒冻结；不改变任务总超时。', visibleWhen: (payload) => ['image', 'video', 'multimodal'].includes(String(payload.modality || '')) },
    { path: 'specification', label: '完整模型规格', kind: 'json', advanced: true, description: '同一 specification 事实的完整 JSON 视图，用于异步媒体请求（async_media）的结构约束（Schema）、烟测输入（smoke_inputs）等扩展能力合同；上方类型化字段会直接读写这个对象。', impact: '不是第二套配置；发布后不可原地修改，服务端会校验秘密字段和媒体合同。' },
  ],
  deployment: [
    { path: 'key', label: '部署键（Deployment Key）', kind: 'text', required: true, pattern: '^[a-z][a-z0-9_.-]{1,99}$', description: '运行部署的稳定机器标识。', impact: '配方路由和审计通过该标识识别部署。', placeholder: 'deepseek-prod-01' },
    { path: 'name', label: '显示名称', kind: 'text', required: true, description: '管理员识别部署的名称。', impact: '仅影响页面显示。' },
    { path: 'profile_version', label: '已发布模型版本', kind: 'select', required: true, options: dependencies.modelProfiles.filter((item) => item.status === 'published').map((item) => ({ value: item.id, label: `${item.profileKey} v${item.version} · ${item.modelIdentifier}` })), description: '只能选择已发布且未退役的模型版本。', impact: '决定模型标识、服务提供方（Provider）及能力合同。' },
    { path: 'runtime_host', label: '运行主机', kind: 'select', options: [{ value: '', label: '远端服务 / 不绑定本地主机' }, ...dependencies.runtimeHosts.map((item) => ({ value: item.id, label: `${item.name} · ${freshnessLabel(item.status)}`, disabled: item.status !== 'active' }))], description: '本地模型和 ComfyUI 工作流执行器应选择已探测为启用（active）的主机；远端接口（API）可不绑定。', impact: '主机不可用会阻断本地生产任务。' },
    { path: 'priority', label: '路由优先级', kind: 'number', required: true, defaultValue: 100, step: 1, description: '同能力多个部署的排序依据。', impact: '数值越高越优先，但最终仍受配方冻结路由约束。' },
    { path: 'is_default', label: '默认部署', kind: 'boolean', defaultValue: false, description: '标记同模型下的默认候选。', impact: '只影响推荐与默认选择，不覆盖已发布配方。' },
    { path: 'status', label: '部署状态', kind: 'select', required: true, defaultValue: 'active', options: [{ value: 'active', label: '启用（active）' }, { value: 'disabled', label: '禁用（disabled）' }, { value: 'error', label: '异常（error）' }], description: '部署生命周期状态。', impact: '非启用（active）部署不会进入新生产路由。' },
    ...deploymentRuntimeFields,
  ],
  artifact: [
    { path: 'profile_version', label: '草稿模型版本', kind: 'select', required: true, options: dependencies.modelProfiles.filter((item) => item.status === 'draft').map((item) => ({ value: item.id, label: `${item.profileKey} v${item.version} · 草稿` })), description: '模型文件只能在模型版本发布前声明或修改。', impact: '发布后文件清单冻结；变化必须新建模型版本。' },
    { path: 'role', label: '文件角色', kind: 'select', required: true, options: choices(registry?.artifactRoles), description: '选项来自服务端模型文件角色枚举。', impact: '工作流模型要求（model requirement）依据角色和别名装配。' },
    { path: 'logical_name', label: '逻辑名称', kind: 'text', required: true, description: '同模型版本与角色下的稳定别名。', impact: '供工作流和审核页面引用。', placeholder: 'base-checkpoint' },
    { path: 'filename', label: '预期文件名', kind: 'text', required: true, description: '运行主机应观测到的文件名。', impact: '主机探测和发布前核验使用该值。' },
    { path: 'inventory_folder', label: 'ComfyUI 库存目录 / Inventory folder', kind: 'text', advanced: true, description: 'ComfyUI models 根目录下的受控目录。标准文件角色可留空并由平台解析为 checkpoints、diffusion_models、text_encoders、vae 或 loras。', impact: '仅在模型位于非标准目录时填写；自定义目录必须同时配置下面的节点类型和输入名。', placeholder: 'diffusion_models' },
    { path: 'inventory_node_type', label: '库存节点类型 / Inventory node type', kind: 'text', advanced: true, description: '自定义模型目录通过 ComfyUI object_info 枚举核验时使用的节点类型；标准目录应留空。', impact: '与库存节点输入名必须同时填写或同时留空；错误会阻断模型证明和工作流发布。', placeholder: 'CustomModelLoader' },
    { path: 'inventory_input_name', label: '库存节点输入名 / Inventory input name', kind: 'text', advanced: true, description: '上述 ComfyUI 节点中返回受控模型文件选项的输入字段名；标准目录应留空。', impact: '与库存节点类型共同定义可验证的模型库存来源，不会按路径或模型名称猜测。', placeholder: 'model_name' },
    { path: 'storage_uri', label: '受管存储标识（URI）', kind: 'text', description: '可选的受管制品库统一资源标识（URI）；不得包含账号或访问令牌（Token）。', impact: '用于运维定位来源，不作为浏览器直接下载地址。' },
    { path: 'sha256', label: 'SHA-256', kind: 'text', pattern: '^[0-9a-f]{64}$', description: '可选的 64 位小写文件摘要。', impact: '填写后主机观测必须一致才能标记可用。' },
    { path: 'size_bytes', label: '文件大小（字节）', kind: 'number', min: 0, step: 1, description: '可选的预期字节数。', impact: '与 SHA-256 一起用于模型文件核验。' },
  ],
  recipe: [
    { path: 'recipe_key', label: '新配方族标识（Recipe Key）', kind: 'catalog', inputPurpose: 'business', allowCustom: true, required: true, options: recipeKeyOptions, pattern: '^[a-z][a-z0-9_.-]{1,99}$', description: '已有配方族只能从版本卡片创建后继草稿；此处仅在建立全新生产方案时登记稳定标识。', impact: '生产运行（ProductionRun）只引用已发布的不可变版本。', placeholder: 'series-ai-production' },
    { path: 'version', label: '版本号', kind: 'readonly', inputPurpose: 'system', submit: false, description: '保存时由服务端按配方族全部历史原子分配。', impact: '不接受人工输入；已有配方请使用“创建后继草稿”。', placeholder: '保存后自动生成' },
    { path: 'name', label: '显示名称', kind: 'text', inputPurpose: 'business', required: true, description: '操作员选择生产方案时看到的名称。', impact: '应能区分人工智能起稿（AI draft）、已确认剧本等入口。' },
    { path: 'definition.schema_version', label: '结构合同版本', kind: 'readonly', inputPurpose: 'system', required: true, defaultValue: recipeOptions?.definitionSchemaVersion, description: '由服务端结构化配方目录维护；普通页面不允许修改。', impact: '未知版本会阻断发布。' },
  ],
  'config-definition': [
    { path: 'key', label: '配置键（Configuration Key）', kind: 'text', inputPurpose: 'business', required: true, pattern: '^[a-z][a-z0-9_.-]{1,159}$', description: '读取方使用的全局稳定标识；创建后由系统锁定。', impact: '配置修订版本（revision）、页面和生产任务均通过它读取同一事实。', placeholder: 'production.project_creation_policy' },
    { path: 'configuration_class', label: '配置类别（Configuration Class）', kind: 'readonly', inputPurpose: 'system', required: true, defaultValue: configOptions?.systemDefaults.configuration_class, options: choices(configOptions?.configurationClasses), description: '由当前设置分区确定，不允许在表单中重新分类。', impact: '安全边界只能由超级管理员在安全分区维护。' },
    { path: 'domain', label: '配置域', kind: 'catalog', inputPurpose: 'catalog', allowCustom: configOptions?.customEntryFields.includes('domain') === true, required: true, options: configDomainOptions, description: '优先复用服务端已有配置域；只有服务端授权时才可登记新域。', impact: '影响页面分组与运维归属。', placeholder: 'production' },
    { path: 'value_type', label: '值类型（Value Type）', kind: 'select', inputPurpose: 'catalog', required: true, options: choices(configOptions?.valueTypes), description: '保存和解析时使用的强类型。', impact: '已有配置修订版本（revision）后不可改变。' },
    { path: 'description', label: '参数说明', kind: 'textarea', required: true, description: '说明参数用途、空值语义和生效范围。', impact: '普通用户依赖该说明正确配置。' },
    { path: 'unit', label: '单位', kind: 'catalog', inputPurpose: 'catalog', allowCustom: configOptions?.customEntryFields.includes('unit') === true, options: configUnitOptions, description: '优先从服务端已有单位目录选择；无单位可留空，服务端授权时才可登记新单位。', impact: '防止数值口径混淆。' },
    { path: 'impact_modules', label: '影响模块', kind: 'multiselect', inputPurpose: 'catalog', allowCustom: configOptions?.customEntryFields.includes('impact_modules') === true, multipleValue: 'array', required: true, options: configModuleOptions, description: '从服务端模块目录多选；只有服务端授权时才可登记新模块。', impact: '用于变更审核和影响提示。', placeholder: 'production' },
    { path: 'allowed_scopes', label: '允许作用域', kind: 'multiselect', inputPurpose: 'catalog', multipleValue: 'csv', required: true, defaultValue: configOptions?.systemDefaults.allowed_scopes, options: choices(configOptions?.scopes), description: '只允许服务端登记的系统、项目和单次运行作用域。', impact: '决定是否能按项目或单次运行覆盖。' },
    { path: 'allowed_default_sources', label: '允许默认来源', kind: 'multiselect', inputPurpose: 'catalog', multipleValue: 'csv', defaultValue: configOptions?.systemDefaults.allowed_default_sources, options: choices(configOptions?.defaultSources), description: '只允许服务端登记的模型、工作流来源；密钥或需重启项必须留空。', impact: '决定模型/工作流合同能否提供默认值。' },
    { path: 'restart_required', label: '需要重启后激活', kind: 'boolean', defaultValue: configOptions?.systemDefaults.restart_required, description: '是否必须由新进程激活配置修订版本（revision）。', impact: '启用后保存不会立即改变当前进程。' },
    { path: 'is_required', label: '生产必填', kind: 'boolean', defaultValue: configOptions?.systemDefaults.is_required, description: '没有有效值时是否阻断生产运行（ProductionRun）。', impact: '必填配置缺失会明确阻断（fail closed）。' },
    { path: 'is_secret', label: '敏感配置', kind: 'boolean', defaultValue: configOptions?.systemDefaults.is_secret, description: '敏感值只允许绑定受管凭据，不保存明文初始值（initial_value）。', impact: '启用后浏览器和接口（API）永不回传秘密正文。' },
    { path: 'is_active', label: '启用定义', kind: dependencies.lockedPaths?.includes('is_active') ? 'readonly' : 'boolean', inputPurpose: dependencies.lockedPaths?.includes('is_active') ? 'system' : 'business', defaultValue: configOptions?.systemDefaults.is_active, description: '新定义由服务端固定为启用；已有定义可显式停用。', impact: '停用不会删除历史配置修订版本（revision）。' },
    { path: 'needs_review', label: '需要人工确认', kind: dependencies.lockedPaths?.includes('needs_review') ? 'readonly' : 'boolean', inputPurpose: dependencies.lockedPaths?.includes('needs_review') ? 'system' : 'business', defaultValue: configOptions?.systemDefaults.needs_review, description: '新定义由服务端固定为无需确认；已有迁移定义可显式标记。', impact: '就绪检查会持续提示。' },
    { path: 'review_reason', label: '人工确认原因', kind: 'textarea', description: '说明为何不能安全给出默认值。', impact: '帮助管理员完成后续确认。', visibleWhen: (payload) => payload.needs_review === true },
    { path: 'value_schema.minimum', label: '最小值', kind: 'number', description: '数值类型的下限。', impact: '浏览器与服务端保存都会校验。', visibleWhen: (payload) => ['integer', 'number'].includes(String(payload.value_type || '')) },
    { path: 'value_schema.maximum', label: '最大值', kind: 'number', description: '数值类型的上限。', impact: '浏览器与服务端保存都会校验。', visibleWhen: (payload) => ['integer', 'number'].includes(String(payload.value_type || '')) },
    { path: 'value_schema.pattern', label: '字符串格式', kind: 'text', description: '字符串类型的正则约束。', impact: '不匹配时阻断保存。', visibleWhen: (payload) => payload.value_type === 'string' },
    { path: 'value_schema.enum', label: '允许枚举', kind: 'tags', description: '可选值列表；逗号分隔。', impact: '设置页面会自动使用下拉选择。' },
    { path: 'value_schema', label: '完整值结构约束（Schema）', kind: 'json', advanced: true, description: '仅用于上方尚未覆盖的 JSON 结构约束（JSON Schema）。', impact: '服务端是最终校验者；非法结构约束（Schema）不会生效。' },
    { path: 'initial_value', label: '初始默认值', kind: 'boolean', description: '首次建立定义时的布尔默认值。', impact: '会进入系统配置事实并影响新运行。', visibleWhen: (payload) => payload.value_type === 'boolean' && payload.is_secret !== true },
    { path: 'initial_value', label: '初始默认值', kind: 'number', step: 1, description: '首次建立定义时的整数默认值。', impact: '会进入系统配置事实并影响新运行。', visibleWhen: (payload) => payload.value_type === 'integer' && payload.is_secret !== true },
    { path: 'initial_value', label: '初始默认值', kind: 'number', description: '首次建立定义时的数值默认值。', impact: '会进入系统配置事实并影响新运行。', visibleWhen: (payload) => payload.value_type === 'number' && payload.is_secret !== true },
    { path: 'initial_value', label: '初始默认值', kind: 'text', description: '首次建立定义时的字符串默认值。', impact: '会进入系统配置事实并影响新运行。', visibleWhen: (payload) => payload.value_type === 'string' && payload.is_secret !== true },
    { path: 'initial_value', label: '初始默认值', kind: 'json', advanced: true, description: '对象或数组类型的初始值；敏感配置必须留空。', impact: '会进入系统配置事实并影响新运行。', visibleWhen: (payload) => ['object', 'array'].includes(String(payload.value_type || '')) && payload.is_secret !== true },
  ],
})
}

export const resourceEditorFields = (
  resource: RegistryResource,
  payload: JsonObject,
  dependencies: RegistryEditorDependencies,
): RegistryFieldSchema[] => {
  const profile = resource === 'deployment'
    ? dependencies.modelProfiles.find((item) => item.id === payload.profile_version)
    : undefined
  const provider = profile
    ? dependencies.providers.find((item) => item.id === profile.provider)
    : undefined
  const contextualPayload = provider ? {
    ...payload,
    driver_type: provider.driverType,
    driver_config: {
      ...provider.driverConfig,
      ...(payload.runtime_config && typeof payload.runtime_config === 'object' && !Array.isArray(payload.runtime_config) ? payload.runtime_config as JsonObject : {}),
    },
  } : payload
  const textEndpointFields = new Set(['base_url', 'chat_path', 'timeout_seconds', 'capability_ttl_seconds'])
  const comfyFields = new Set(['base_url', 'timeout_seconds', 'poll_interval_seconds', 'max_poll_seconds', 'capability_ttl_seconds'])
  const asyncFields = new Set([
    'base_url', 'health_path', 'submit_path', 'poll_path', 'cancel_path', 'collect_path',
    'timeout_seconds', 'poll_interval_seconds', 'max_poll_seconds', 'request_id_field',
    'status_field', 'terminal_success', 'terminal_failure', 'terminal_cancelled',
    'artifacts_field', 'actual_cost_field', 'currency_field', 'artifact_url_field',
    'artifact_kind_field', 'artifact_id_field', 'artifact_sha256_field',
    'artifact_allowed_hosts', 'request_field_map', 'idempotency_header',
    'capability_ttl_seconds',
  ])
  return basicFields(dependencies, payload)[resource]
    .filter((field) => !field.visibleWhen || field.visibleWhen(contextualPayload))
    .map((field) => {
      const driverType = String(contextualPayload.driver_type || '')
      const configPrefix = resource === 'deployment' ? 'runtime_config.' : 'driver_config.'
      if (field.path.startsWith(configPrefix)) {
        const configField = field.path.slice(configPrefix.length)
        const runtimeSource = String(getPath(contextualPayload, 'driver_config.runtime_source') || '')
        const executableField = (
          (driverType === 'async_rest' && asyncFields.has(configField))
          || (driverType === 'comfyui' && comfyFields.has(configField))
          || (driverType === 'openai_compatible' && textEndpointFields.has(configField))
          || (
            ['local_qwen', 'deepseek_api'].includes(driverType)
            && (configField === 'runtime_source' || (runtimeSource === 'openai_compatible_endpoint' && textEndpointFields.has(configField)))
          )
        )
        if (executableField) {
          if (resource === 'provider') return { ...field, required: true }
          const inherited = provider
            ? getPath({ driver_config: provider.driverConfig }, `driver_config.${configField}`)
            : undefined
          const inheritedCannotExecute = (
            configField === 'runtime_source'
            && ['local_qwen', 'deepseek_api'].includes(driverType)
            && inherited === 'deployment'
          )
          return {
            ...field,
            required: inheritedCannotExecute || inherited === undefined || inherited === null || inherited === '',
          }
        }
        if (resource === 'provider' && configField === 'runtime_source' && driverType === 'comfyui') {
          return { ...field, required: true }
        }
      }
      if (dependencies.lockedPaths?.includes(field.path)) {
        return { ...field, kind: 'readonly' as const }
      }
      return field
    })
}

export const applyResourceDefaults = (
  resource: RegistryResource,
  payload: JsonObject,
  dependencies: RegistryEditorDependencies,
): JsonObject => {
  let result = cloneJson(payload)
  // Defaults are applied only when a field is truly absent; existing null/false/0
  // values keep their explicit meaning.
  for (const field of resourceEditorFields(resource, result, dependencies)) {
    if (field.defaultValue !== undefined && getPath(result, field.path) === undefined) {
      result = setPath(result, field.path, cloneJson(field.defaultValue))
    }
  }
  return result
}

export const registryInputPurpose = (field: RegistryFieldSchema): 'business' | 'catalog' | 'system' => (
  field.inputPurpose
  || (field.kind === 'readonly' ? 'system' : ['select', 'catalog', 'multiselect', 'boolean'].includes(field.kind) ? 'catalog' : 'business')
)

export const buildRegistryResourcePayload = (
  resource: RegistryResource,
  payload: JsonObject,
  dependencies: RegistryEditorDependencies,
): JsonObject => resourceEditorFields(resource, payload, dependencies)
  .filter((field) => field.submit === false)
  .reduce((result, field) => setPath(result, field.path, undefined), cloneJson(payload))

const empty = (value: unknown): boolean => value === undefined || value === null || value === ''
  || (Array.isArray(value) && value.length === 0)
  || (typeof value === 'object' && !Array.isArray(value) && Object.keys(value as object).length === 0)

const executionMonitoringValidationErrors = (value: unknown, limits?: NodeIdleLimits | null): string[] => {
  if (value === undefined || value === null) return []
  if (!value || typeof value !== 'object' || Array.isArray(value)) return ['执行监控合同必须是 JSON 对象']
  const contract = value as JsonObject
  const allowed = new Set(['schema_version', 'default_idle_timeout_seconds', 'node_type_overrides'])
  const unknown = Object.keys(contract).filter((key) => !allowed.has(key))
  const errors = unknown.length ? [`模型执行监控合同包含未知字段：${unknown.join('、')}`] : []
  if (contract.schema_version !== 1) errors.push('执行监控合同版本必须为 1')
  const defaultIdle = contract.default_idle_timeout_seconds
  if (defaultIdle !== undefined && defaultIdle !== null && (
    typeof defaultIdle !== 'number' || !Number.isInteger(defaultIdle) || defaultIdle <= 0
  )) errors.push('默认静默时限必须是正整数秒数；当前安全上限由服务端配置校验')
  errors.push(...nodeIdleOverrideErrors(contract.node_type_overrides, limits))
  return errors
}

const preSubmitResourceReleaseValidationErrors = (value: unknown): string[] => {
  if (value === undefined || value === null) return []
  if (!value || typeof value !== 'object' || Array.isArray(value)) return ['提交前资源释放合同必须是 JSON 对象']
  const contract = value as JsonObject
  const allowed = new Set(['schema_version', 'enabled', 'unload_models', 'free_memory'])
  const unknown = Object.keys(contract).filter((key) => !allowed.has(key))
  const missing = [...allowed].filter((key) => !Object.prototype.hasOwnProperty.call(contract, key))
  const errors: string[] = []
  if (unknown.length) errors.push(`提交前资源释放合同包含未知字段：${unknown.join('、')}`)
  if (missing.length) errors.push(`提交前资源释放合同缺少字段：${missing.join('、')}`)
  if (contract.schema_version !== 1) errors.push('提交前资源释放合同版本必须为 1')
  for (const key of ['enabled', 'unload_models', 'free_memory']) {
    if (typeof contract[key] !== 'boolean') errors.push(`提交前资源释放 ${key} 必须是布尔值`)
  }
  if (contract.enabled === true && contract.unload_models !== true && contract.free_memory !== true) {
    errors.push('启用提交前资源释放时至少选择卸载模型或回收空闲显存')
  }
  if (contract.enabled === false && (contract.unload_models === true || contract.free_memory === true)) {
    errors.push('停用提交前资源释放时两项释放动作必须同时关闭')
  }
  return errors
}

const containsForbiddenSecretField = (value: unknown): boolean => {
  if (Array.isArray(value)) return value.some(containsForbiddenSecretField)
  if (!value || typeof value !== 'object') return false
  return Object.entries(value as JsonObject).some(([key, child]) => {
    const separated = key
      .replace(/([A-Z]+)([A-Z][a-z])/g, '$1_$2')
      .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
    const tokens = separated.toLocaleLowerCase('en-US').match(/[a-z0-9]+/g) || []
    const hasForbiddenPair = tokens.some((token, index) => (
      index < tokens.length - 1 && FORBIDDEN_SECRET_TOKEN_PAIRS.has(`${token}:${tokens[index + 1]}`)
    ))
    return (tokens.length === 1 && FORBIDDEN_SECRET_SINGLE_TOKENS.has(tokens[0]))
      || hasForbiddenPair
      || containsForbiddenSecretField(child)
  })
}

const normalizedRequestField = (value: string): string => value.toLocaleLowerCase('en-US').replace(/-/g, '_')

const isReservedChatRequestField = (value: string): boolean => {
  const normalized = normalizedRequestField(value)
  const collapsed = normalized.replace(/_/g, '')
  return CHAT_REQUEST_RESERVED_FIELDS.has(normalized)
    || CHAT_REQUEST_TRANSPORT_FIELDS.has(normalized)
    || [...CHAT_REQUEST_RESERVED_FIELDS, ...CHAT_REQUEST_TRANSPORT_FIELDS]
      .some((field) => field.replace(/_/g, '') === collapsed)
    || CHAT_REQUEST_TRANSPORT_FIELD_MARKERS.some((marker) => normalized.includes(marker))
}

const chatRequestOptionErrors = (value: unknown): string[] => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return ['受控请求选项必须是 JSON 对象']
  const errors: string[] = []
  const root = value as JsonObject

  const visit = (node: unknown, path: string): void => {
    if (node === null || typeof node === 'boolean' || typeof node === 'number') {
      if (typeof node === 'number' && !Number.isFinite(node)) errors.push(`受控请求选项包含非有限数值：${path}`)
      return
    }
    if (typeof node === 'string') {
      if (/[\p{Cf}\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F]/u.test(node)) {
        errors.push(`受控请求选项不得包含不可见控制或 Unicode 格式字符：${path}`)
      }
      return
    }
    if (Array.isArray(node)) {
      node.forEach((child, index) => visit(child, `${path}[${index}]`))
      return
    }
    if (node && typeof node === 'object') {
      const entries = Object.entries(node as JsonObject)
      for (const [key, child] of entries) {
        const fieldPath = `${path}.${key || '(空)'}`
        if (!CHAT_REQUEST_OPTION_KEY.test(key)) errors.push(`受控请求选项字段名无效：${fieldPath}`)
        if (isReservedChatRequestField(key)) errors.push(`受控请求选项不得覆盖运行或传输字段：${fieldPath}`)
        visit(child, fieldPath)
      }
      return
    }
    errors.push(`受控请求选项包含不支持的数据类型：${path}`)
  }
  visit(root, 'chat_request_options')
  if (containsForbiddenSecretField(root)) errors.push('受控请求选项不得包含密码、令牌、密钥或其他凭据类字段')
  try {
    JSON.stringify(root)
  } catch {
    errors.push('受控请求选项不是可冻结的 JSON')
  }
  return errors
}

export const promptNormalizationValidationErrors = (value: unknown): string[] => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return ['prompt.normalize 容量合同必须是 JSON 对象']
  const contract = value as JsonObject
  const required = ['max_input_bytes', 'max_output_tokens', 'max_items_per_batch', 'timeout_seconds', 'chat_request_options']
  const allowed = new Set([...required, 'input_byte_warning_threshold', 'output_token_warning_threshold'])
  const errors: string[] = []
  const missing = required.filter((key) => !Object.prototype.hasOwnProperty.call(contract, key))
  const unknown = Object.keys(contract).filter((key) => !allowed.has(key))
  if (missing.length) errors.push(`prompt.normalize 容量合同缺少字段：${missing.join('、')}`)
  if (unknown.length) errors.push(`prompt.normalize 容量合同包含未知字段：${unknown.join('、')}`)
  const positiveInteger = (key: string, label: string) => {
    const candidate = contract[key]
    if (typeof candidate !== 'number' || !Number.isSafeInteger(candidate) || candidate < 1) {
      errors.push(`${label}必须是正整数`)
    }
  }
  positiveInteger('max_input_bytes', '标准化最大输入字节')
  if (Object.prototype.hasOwnProperty.call(contract, 'input_byte_warning_threshold')) {
    positiveInteger('input_byte_warning_threshold', '输入字节建议阈值')
    if (typeof contract.input_byte_warning_threshold === 'number'
      && typeof contract.max_input_bytes === 'number'
      && contract.input_byte_warning_threshold > contract.max_input_bytes) {
      errors.push('输入字节建议阈值不能大于标准化最大输入字节')
    }
  }
  positiveInteger('max_output_tokens', '标准化最大输出令牌')
  if (Object.prototype.hasOwnProperty.call(contract, 'output_token_warning_threshold')) {
    positiveInteger('output_token_warning_threshold', '输出令牌建议阈值')
    if (typeof contract.output_token_warning_threshold === 'number'
      && typeof contract.max_output_tokens === 'number'
      && contract.output_token_warning_threshold > contract.max_output_tokens) {
      errors.push('输出令牌建议阈值不能大于标准化最大输出令牌')
    }
  }
  positiveInteger('max_items_per_batch', '标准化每批最大字段数')
  const timeout = contract.timeout_seconds
  if (typeof timeout !== 'number' || !Number.isFinite(timeout) || timeout <= 0) {
    errors.push('标准化请求超时必须是有限正数')
  }
  if (Object.prototype.hasOwnProperty.call(contract, 'chat_completion_options')) {
    errors.push('chat_completion_options 已退役，请改用 chat_request_options')
  }
  if (Object.prototype.hasOwnProperty.call(contract, 'chat_request_options')) {
    errors.push(...chatRequestOptionErrors(contract.chat_request_options))
  }
  return errors
}

export const visualRulesValidationErrors = (value: unknown): string[] => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return ['visual_rules.generate 容量合同必须是 JSON 对象']
  const contract = value as JsonObject
  const required = ['max_input_bytes', 'max_output_tokens', 'timeout_seconds', 'chat_request_options']
  const allowed = new Set([...required, 'input_byte_warning_threshold', 'output_token_warning_threshold', 'quality_advisories'])
  const errors: string[] = []
  const missing = required.filter((key) => !Object.prototype.hasOwnProperty.call(contract, key))
  const unknown = Object.keys(contract).filter((key) => !allowed.has(key))
  if (missing.length) errors.push(`visual_rules.generate 容量合同缺少字段：${missing.join('、')}`)
  if (unknown.length) errors.push(`visual_rules.generate 容量合同包含未知字段：${unknown.join('、')}`)
  const positiveInteger = (key: string, label: string) => {
    const candidate = contract[key]
    if (typeof candidate !== 'number' || !Number.isSafeInteger(candidate) || candidate < 1) {
      errors.push(`${label}必须是正整数`)
    }
  }
  positiveInteger('max_input_bytes', '影视规则最大输入字节')
  positiveInteger('max_output_tokens', '影视规则最大输出令牌')
  for (const [field, maximumField, label] of [
    ['input_byte_warning_threshold', 'max_input_bytes', '影视规则输入建议阈值'],
    ['output_token_warning_threshold', 'max_output_tokens', '影视规则输出建议阈值'],
  ] as const) {
    if (!Object.prototype.hasOwnProperty.call(contract, field)) continue
    positiveInteger(field, label)
    if (typeof contract[field] === 'number' && typeof contract[maximumField] === 'number'
      && contract[field] > contract[maximumField]) errors.push(`${label}不能大于对应硬容量`)
  }
  const timeout = contract.timeout_seconds
  if (typeof timeout !== 'number' || !Number.isFinite(timeout) || timeout <= 0) {
    errors.push('影视规则请求超时必须是有限正数')
  }
  if (Object.prototype.hasOwnProperty.call(contract, 'chat_completion_options')) {
    errors.push('chat_completion_options 已退役，请改用 chat_request_options')
  }
  if (Object.prototype.hasOwnProperty.call(contract, 'chat_request_options')) {
    errors.push(...chatRequestOptionErrors(contract.chat_request_options))
  }
  if (Object.prototype.hasOwnProperty.call(contract, 'quality_advisories')) {
    const raw = contract.quality_advisories
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      errors.push('影视规则质量建议必须是 schema_version=1 的 JSON 对象')
    } else {
      const advisories = raw as JsonObject
      const advisoryFields = new Set([
        'schema_version',
        'base_colors_min_items', 'base_colors_max_items',
        'negative_constraints_min_items', 'negative_constraints_max_items',
        'negative_constraint_max_chars', 'recommended_negative_clauses', 'continuity_summary_max_chars',
        'negative_prompt_max_chars', 'color_script_max_chars',
      ])
      const advisoryUnknown = Object.keys(advisories).filter((key) => !advisoryFields.has(key))
      if (advisories.schema_version !== 1) errors.push('影视规则质量建议 schema_version 只能为 1')
      if (advisoryUnknown.length) errors.push(`影视规则质量建议包含未知字段：${advisoryUnknown.join('、')}`)
      for (const field of ['base_colors_min_items', 'base_colors_max_items'] as const) {
        if (!Object.prototype.hasOwnProperty.call(advisories, field)) continue
        const candidate = advisories[field]
        if (typeof candidate !== 'number' || !Number.isInteger(candidate) || candidate < 0 || candidate > 21) {
          errors.push(`${field} 必须是 0–21 的整数`)
        }
      }
      for (const field of ['negative_constraints_min_items', 'negative_constraints_max_items'] as const) {
        if (!Object.prototype.hasOwnProperty.call(advisories, field)) continue
        const candidate = advisories[field]
        if (typeof candidate !== 'number' || !Number.isInteger(candidate) || candidate < 0 || candidate > 1024 * 1024) {
          errors.push(`${field} 必须是 0–1048576 的整数`)
        }
      }
      for (const field of ['negative_constraint_max_chars', 'continuity_summary_max_chars', 'negative_prompt_max_chars', 'color_script_max_chars'] as const) {
        if (!Object.prototype.hasOwnProperty.call(advisories, field)) continue
        const candidate = advisories[field]
        if (typeof candidate !== 'number' || !Number.isInteger(candidate) || candidate < 1 || candidate > 1024 * 1024) {
          errors.push(`${field} 必须是 1–1048576 的整数`)
        }
      }
      if (Object.prototype.hasOwnProperty.call(advisories, 'recommended_negative_clauses')) {
        const clauses = advisories.recommended_negative_clauses
        if (!Array.isArray(clauses)) {
          errors.push('recommended_negative_clauses 必须是数组')
        } else {
          const normalized: string[] = []
          for (const [index, rawClause] of clauses.entries()) {
            const clause = typeof rawClause === 'string' ? rawClause.replace(/^[ .]+|[ .]+$/g, '') : ''
            if (!clause || !/^[\x20-\x7e]+$/.test(clause) || /[,;\r\n]/.test(clause) || /<\/?[A-Za-z][^>]*>/.test(clause)) {
              errors.push(`recommended_negative_clauses[${index}] 必须是单条非空、无 HTML/控制字符/列表分隔符的可打印英文`)
            }
            normalized.push(clause.toLocaleLowerCase('en-US'))
          }
          if (new Set(normalized).size !== normalized.length) {
            errors.push('recommended_negative_clauses 不能包含重复项')
          }
        }
      }
      for (const [minimum, maximum] of [
        ['base_colors_min_items', 'base_colors_max_items'],
        ['negative_constraints_min_items', 'negative_constraints_max_items'],
      ] as const) {
        if (typeof advisories[minimum] === 'number' && typeof advisories[maximum] === 'number'
          && advisories[minimum] > advisories[maximum]) errors.push(`${minimum} 不能大于 ${maximum}`)
      }
    }
  }
  return errors
}

export const recipeBibleSourceValidationErrors = (definition: JsonObject): string[] => {
  const stages = Array.isArray(definition.stages)
    ? definition.stages.filter((value): value is JsonObject => Boolean(value && typeof value === 'object' && !Array.isArray(value)))
    : []
  const byKey = new Map(stages.map((stage) => [String(stage.key || ''), stage]))
  const hasBibleImages = byKey.has('bible_image_generation') || byKey.has('bible_image_review')
  if (!hasBibleImages) return []

  // schema v2 unattended recipes carry stronger policy/approval contracts that
  // are validated by the existing backend path. Keep that contract untouched.
  if (Number(definition.schema_version) === 2 && definition.interaction_mode === 'unattended') return []

  const materialize = byKey.get('bible_materialize')
  const review = byKey.get('bible_review')
  const deterministicSource = Boolean(
    materialize?.mode === 'deterministic'
    && review?.mode === 'review'
    && review?.approval === 'required',
  )
  const manualSource = Boolean(
    !materialize
    && review?.mode === 'manual'
    && review?.approval === 'required',
  )
  if (!deterministicSource && !manualSource) {
    return ['带 Bible 参考图的配方必须且只能选择一种文本 Bible 来源：bible_materialize/deterministic + bible_review/review(required)，或不含 bible_materialize 且使用 bible_review/manual(required)']
  }
  return []
}

export const validateRegistryResource = (
  resource: RegistryResource,
  payload: JsonObject,
  dependencies: RegistryEditorDependencies,
): RegistryValidation => {
  const errors: string[] = []
  const warnings: string[] = []
  for (const field of resourceEditorFields(resource, payload, dependencies)) {
    if (field.advanced) continue
    const value = getPath(payload, field.path)
    if (field.required && empty(value)) errors.push(`${field.label}为必填项`)
    if ((field.kind === 'select' || field.kind === 'catalog' || field.kind === 'multiselect') && field.options) {
      const selectedValues = field.kind === 'multiselect'
        ? (field.multipleValue === 'array' ? arrayValues(value) : csvValues(value))
        : (empty(value) ? [] : [String(value)])
      for (const selectedValue of selectedValues) {
        const option = field.options.find((item) => item.value === selectedValue)
        if (!option && field.allowCustom) continue
        if (!option || option.disabled) errors.push(`${field.label}包含不可用选项 ${selectedValue}`)
      }
    }
    if (field.pattern && typeof value === 'string' && value && !new RegExp(field.pattern).test(value)) errors.push(`${field.label}格式不正确`)
    if (field.kind === 'number' && value !== undefined && value !== null && value !== '') {
      const numeric = Number(value)
      if (!Number.isFinite(numeric)) errors.push(`${field.label}必须是数字`)
      else {
        if (field.min !== undefined && numeric < field.min) errors.push(`${field.label}不能小于 ${field.min}`)
        if (field.max !== undefined && numeric > field.max) errors.push(`${field.label}不能大于 ${field.max}`)
        if (field.step === 1 && !Number.isInteger(numeric)) errors.push(`${field.label}必须是整数`)
      }
    }
  }
  if (['provider', 'model', 'artifact'].includes(resource) && !dependencies.modelProfileOptions) {
    errors.push('服务端模型选项目录读取失败；不能回退为自由文本，请重新读取后再保存')
  }
  if (resource === 'config-definition' && !dependencies.configDefinitionOptions) {
    errors.push('服务端配置定义目录读取失败；不能回退为自由文本，请重新读取后再保存')
  }
  if (resource === 'recipe' && !dependencies.recipeEditorOptions) {
    errors.push('服务端配方编辑目录读取失败；不能使用浏览器内置枚举或默认值保存')
  }
  if (resource === 'model' && !dependencies.providers.some((item) => item.isActive)) errors.push('请先新增并启用 Provider，再创建模型版本')
  if (resource === 'model') {
    const registry = dependencies.modelProfileOptions
    const provider = dependencies.providers.find((item) => item.id === payload.provider)
    const modality = String(payload.modality || '')
    const capabilities = arrayValues(getPath(payload, 'specification.capabilities'))
    for (const capability of capabilities) {
      const option = registry?.capabilities.find((item) => item.value === capability)
      if (!option) errors.push(`能力 ${capability} 不在当前服务端受控目录中`)
      else if (!option.legacy && (
        (option.driverTypes.length > 0 && !option.driverTypes.includes(provider?.driverType || ''))
        || (option.modalities.length > 0 && !option.modalities.includes(modality))
      )) errors.push(`能力 ${capability} 与当前 Provider 驱动或主模态不兼容`)
    }
    if (
      registry
      && getPath(payload, 'specification.schema_version') !== registry.specificationSchemaVersion
    ) errors.push(`模型规格结构版本必须由系统保持为 ${registry.specificationSchemaVersion}`)
    const modelFamily = String(getPath(payload, 'specification.model_family') || '')
    if (
      modelFamily
      && registry
      && !registry.modelFamilies.includes(modelFamily)
      && modelFamily !== String(payload.profile_key || '')
    ) errors.push('提示词策略族必须从服务端目录选择，或显式选择与当前 Profile Key 同名的新策略族')
    const monitoring = getPath(payload, 'specification.execution_monitoring')
    if (
      monitoring !== undefined && monitoring !== null
      && !['image', 'video', 'multimodal'].includes(String(payload.modality || ''))
    ) errors.push('执行监控合同仅适用于 image、video 或 multimodal 模型；请在完整模型规格中移除 execution_monitoring')
    else errors.push(...executionMonitoringValidationErrors(monitoring, dependencies.executionMonitoringLimits))
    if (modelHasPromptNormalization(payload)) {
      errors.push(...promptNormalizationValidationErrors(getPath(payload, 'specification.prompt_normalization')))
    }
    if (modelHasVisualRules(payload)) {
      errors.push(...visualRulesValidationErrors(getPath(payload, 'specification.visual_rules')))
    }
  }
  if (resource === 'deployment' && !dependencies.modelProfiles.some((item) => item.status === 'published')) errors.push('请先发布模型版本，再创建部署')
  if (resource === 'artifact' && !dependencies.modelProfiles.some((item) => item.status === 'draft')) errors.push('请先创建草稿模型版本，再声明模型文件')
  if (resource === 'deployment') {
    const hostId = String(payload.runtime_host || '')
    const status = String(payload.status || '')
    const host = dependencies.runtimeHosts.find((item) => item.id === hostId)
    if (host && status === 'active' && host.status !== 'active') errors.push('启用部署只能绑定已探测为 active 的运行主机')
    const profile = dependencies.modelProfiles.find((item) => item.id === payload.profile_version)
    const provider = dependencies.providers.find((item) => item.id === profile?.provider)
    const runtimeConfig = payload.runtime_config && typeof payload.runtime_config === 'object' && !Array.isArray(payload.runtime_config) ? payload.runtime_config as JsonObject : {}
    const effectiveBaseUrl = String(runtimeConfig.base_url || provider?.driverConfig.base_url || '').trim()
    if (status === 'active' && provider && !effectiveBaseUrl) errors.push('active Deployment 缺少有效服务根地址；请在 Provider 或本部署中配置')
  }
  if (resource === 'config-definition') {
    if (payload.is_secret === true && payload.initial_value !== null && payload.initial_value !== undefined) errors.push('敏感配置不得保存初始明文值')
    if (payload.needs_review === true && !String(payload.review_reason || '').trim()) errors.push('标记需要人工确认时必须填写原因')
    if (payload.restart_required === true && String(payload.allowed_default_sources || '').trim()) errors.push('需重启配置不能由模型或工作流提供默认值')
    if (payload.configuration_class === 'security_boundary') {
      if (String(payload.domain || '') !== 'security') errors.push('安全边界配置必须使用 security 配置域')
      if (String(payload.allowed_scopes || '') !== 'system') errors.push('安全边界配置只能使用 system 作用域')
      if (String(payload.allowed_default_sources || '').trim()) errors.push('安全边界配置不能接受模型或工作流默认值')
      if (payload.is_required !== true) errors.push('安全边界配置必须标记为必填')
      if (payload.is_secret === true) errors.push('安全边界配置不能承载秘密正文；凭据必须使用 SecretBinding')
      if (payload.is_active !== true) errors.push('当前安全边界合同要求定义保持启用；退役记录只读保留')
    }
  }
  if (resource === 'provider' && payload.driver_type === 'async_rest') {
    const mapping = getPath(payload, 'driver_config.request_field_map')
    if (!mapping || typeof mapping !== 'object' || Array.isArray(mapping)
      || !String((mapping as JsonObject).model || '').trim()
      || !String((mapping as JsonObject).prompt || '').trim()) {
      errors.push('请求字段映射必须同时配置 model 和 prompt')
    }
  }
  if (resource === 'provider' || resource === 'deployment') {
    const prefix = resource === 'provider' ? 'driver_config' : 'runtime_config'
    const currency = String(getPath(payload, `${prefix}.pricing.currency`) || '').trim()
    const amount = getPath(payload, `${prefix}.pricing.fixed_per_request`)
    if ((currency && (amount === undefined || amount === null || amount === '')) || (!currency && amount !== undefined && amount !== null && amount !== '')) {
      errors.push('估算币种与单次固定费用必须同时填写')
    }
    if (currency && !/^[A-Z][A-Z0-9]{2,11}$/.test(currency)) errors.push('估算币种必须为 3–12 位大写字母或数字且以字母开头')
    errors.push(...preSubmitResourceReleaseValidationErrors(
      getPath(payload, `${prefix}.pre_submit_resource_release`),
    ))
  }
  if (resource === 'recipe') {
    const registry = dependencies.recipeEditorOptions
    const definition = payload.definition
    const recipeDefinition = definition && typeof definition === 'object' && !Array.isArray(definition)
      ? definition as JsonObject : null
    const stages = recipeDefinition?.stages
    if (!Array.isArray(stages) || !stages.length) errors.push('配方必须至少包含一个结构化生产阶段')
    if (registry && recipeDefinition?.schema_version !== registry.definitionSchemaVersion) {
      errors.push(`配方结构版本必须由系统保持为 ${registry.definitionSchemaVersion}`)
    }
    if (registry && Array.isArray(stages)) {
      const approvals = new Set(registry.approvalOptions.map((item) => item.value))
      const routingModes = new Set(registry.routingModeOptions.map((item) => item.value))
      for (const [index, rawStage] of stages.entries()) {
        const stage = rawStage && typeof rawStage === 'object' && !Array.isArray(rawStage) ? rawStage as JsonObject : {}
        if (!approvals.has(String(stage.approval || ''))) errors.push(`阶段 ${index + 1} 的审核门禁不在服务端目录中`)
        if (recipeStageRequiresRouting(stage, registry)) {
          const routing = stage.routing && typeof stage.routing === 'object' && !Array.isArray(stage.routing) ? stage.routing as JsonObject : {}
          if (!routingModes.has(String(routing.mode || ''))) errors.push(`阶段 ${index + 1} 的路由策略不在服务端目录中`)
        }
      }
    }
    if (recipeDefinition) errors.push(...recipeBibleSourceValidationErrors(recipeDefinition))
    if (!dependencies.deployments.some((item) => item.status === 'active')) warnings.push('当前没有 active Deployment；需要路由的阶段将无法发布')
  }
  return { errors: [...new Set(errors)], warnings: [...new Set(warnings)] }
}

export const standardRecipeStages = (
  scriptMode: 'ai' | 'confirmed',
  options?: ProductionRecipeEditorOptions | null,
): JsonObject[] => cloneJson(
  options?.presets.find((item) => item.value === (scriptMode === 'ai' ? 'ai_draft' : 'confirmed_script'))?.stages || [],
)

export const recipeStageRequiresRouting = (
  stage: JsonObject,
  options?: ProductionRecipeEditorOptions | null,
): boolean => (
  options?.stageOptions.find((item) => item.value === String(stage.key || ''))?.requiresRouting === true
  && !(String(stage.key || '') === 'script_normalize' && String(stage.mode || '') === 'manual')
)

export const recipeStageOptions = (
  options?: ProductionRecipeEditorOptions | null,
): RegistryOption[] => (options?.stageOptions || []).map((item) => ({
  value: item.value,
  label: stageLabel(item.value),
}))

export const recipeStageModes = (
  options?: ProductionRecipeEditorOptions | null,
): Record<string, string[]> => Object.fromEntries(
  (options?.stageOptions || []).map((item) => [item.value, item.modes]),
)
