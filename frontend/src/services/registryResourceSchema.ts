import type {
  JsonObject,
  ModelDeployment,
  ModelProfileVersion,
  ProviderProfile,
  RuntimeHost,
  WorkflowTemplateVersion,
} from './platformTypes'

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
  kind: 'text' | 'textarea' | 'number' | 'boolean' | 'select' | 'tags' | 'mapping' | 'json'
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
}

export interface RegistryValidation {
  errors: string[]
  warnings: string[]
}

const providerDriverOptions: RegistryOption[] = [
  { value: 'local_qwen', label: '本地 Qwen（受控 OpenAI-compatible）' },
  { value: 'deepseek_api', label: 'DeepSeek API' },
  { value: 'comfyui', label: 'ComfyUI' },
  { value: 'openai_compatible', label: 'OpenAI-compatible' },
  { value: 'async_rest', label: '受限异步 REST' },
]

const modalityOptions: RegistryOption[] = [
  { value: 'text', label: '文本' },
  { value: 'image', label: '图片' },
  { value: 'video', label: '视频' },
  { value: 'multimodal', label: '多模态' },
]

const artifactRoleOptions: RegistryOption[] = [
  ['model', '模型'], ['checkpoint', 'Checkpoint'], ['diffusion_model', 'Diffusion model'],
  ['text_encoder', 'Text encoder'], ['vae', 'VAE'], ['lora', 'LoRA'],
  ['tokenizer', 'Tokenizer'], ['other', '其他'],
].map(([value, label]) => ({ value, label }))

const driverConfigFields: RegistryFieldSchema[] = [
  {
    path: 'driver_config.runtime_source', label: '运行来源', kind: 'select',
    description: '声明驱动从哪里取得运行时服务；不会从旧环境变量静默回退。',
    impact: '影响文本模型是否具备可执行端点。',
    defaultValue: 'openai_compatible_endpoint',
    options: [{ value: 'openai_compatible_endpoint', label: '受控 OpenAI-compatible 端点' }],
    visibleWhen: (payload) => ['local_qwen', 'deepseek_api'].includes(String(payload.driver_type || '')),
  },
  {
    path: 'driver_config.base_url', label: '服务根地址', kind: 'text', required: true,
    description: '仅填写无账号、无密码、无 query/fragment 的 HTTP(S) 根地址；ComfyUI 可留空并在具体 Deployment 覆盖。',
    impact: '缺失或不在白名单会阻断探测、烟测和生产任务。', placeholder: 'http://127.0.0.1:8188',
  },
  {
    path: 'driver_config.chat_path', label: 'Chat API 路径', kind: 'text',
    description: 'OpenAI-compatible chat completion 的相对路径。', impact: '影响文本生成与提示词扩写请求地址。',
    defaultValue: '/v1/chat/completions',
    visibleWhen: (payload) => ['local_qwen', 'deepseek_api', 'openai_compatible'].includes(String(payload.driver_type || '')),
  },
  {
    path: 'driver_config.timeout_seconds', label: '请求超时', kind: 'number',
    description: '单次网络调用的最大等待秒数。', impact: '过短会导致正常任务失败；过长会延迟故障反馈。',
    defaultValue: 60, min: 0.5, max: 3600, step: 0.5,
  },
  {
    path: 'driver_config.health_path', label: '健康检查路径', kind: 'text', defaultValue: '/health',
    description: '异步 REST 健康检查的相对路径。', impact: '影响部署 probe 是否能建立可用性证据。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.submit_path', label: '提交路径', kind: 'text', defaultValue: '/jobs',
    description: '异步 REST 创建任务的相对路径。', impact: '影响真实图片/视频请求的提交位置。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.poll_path', label: '查询路径', kind: 'text', defaultValue: '/jobs/{request_id}',
    description: '异步任务状态查询路径，只允许一个 {request_id} 占位。', impact: '影响 worker 读取远端终态。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.cancel_path', label: '取消路径', kind: 'text', defaultValue: '/jobs/{request_id}',
    description: '异步任务取消路径，只允许一个 {request_id} 占位。', impact: '影响安全取消能力。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.collect_path', label: '产物收集路径', kind: 'text', defaultValue: '/jobs/{request_id}',
    description: '异步任务完成后读取产物 manifest 的路径。', impact: '影响图片或视频能否进入本地资产事实。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.poll_interval_seconds', label: '轮询间隔', kind: 'number', defaultValue: 2,
    description: '异步任务两次查询之间的秒数。', impact: '影响外部接口压力和状态刷新延迟。', min: 0.25, max: 300, step: 0.25,
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.max_poll_seconds', label: '最长轮询时间', kind: 'number', defaultValue: 3600,
    description: '异步任务允许轮询的总秒数。', impact: '到期后任务会显式失败或进入待消歧状态。', min: 10, max: 604800, step: 1,
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
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
    path: 'driver_config.request_id_field', label: '请求 ID 字段', kind: 'text', defaultValue: 'id',
    description: '远程提交响应中承载任务 ID 的单层 JSON 字段名。', impact: '字段错误会导致已提交任务无法持久化远程 ID。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.status_field', label: '状态字段', kind: 'text', defaultValue: 'status',
    description: '远程查询响应中承载任务状态的单层 JSON 字段名。', impact: '影响轮询、终态识别与失败闭环。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.artifacts_field', label: '产物列表字段', kind: 'text', defaultValue: 'artifacts',
    description: '收集响应中产物数组的字段名。', impact: '字段错误会使已完成任务无法形成本地媒体事实。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.artifact_url_field', label: '产物 URL 字段', kind: 'text', defaultValue: 'url',
    description: '单个产物中的受限下载 URL 字段名。', impact: '只会下载到受控主机白名单。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.artifact_kind_field', label: '产物类型字段', kind: 'text', defaultValue: 'kind',
    description: '单个产物中的 image/video 类型字段名。', impact: '影响媒体探测与后续装配。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.artifact_id_field', label: '产物 ID 字段', kind: 'text', defaultValue: 'id',
    description: '单个产物中可复核远端产物身份的单层字段名。', impact: '进入产物 manifest，便于追踪而不暴露下载凭据。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.artifact_sha256_field', label: '产物 SHA-256 字段', kind: 'text', defaultValue: 'sha256',
    description: '单个产物中远端提供的内容摘要字段名。', impact: '若远端提供摘要，本地物化时会用于完整性核对。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.actual_cost_field', label: '实际费用字段', kind: 'text', defaultValue: 'actual_cost',
    description: '远端响应中本次任务实际费用的单层字段名。', impact: '影响成本事实记录；不会据此静默估算缺失费用。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.currency_field', label: '费用币种字段', kind: 'text', defaultValue: 'currency',
    description: '远端响应中费用币种的单层字段名。', impact: '与实际费用共同形成可审计成本事实。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.artifact_allowed_hosts', label: '产物主机白名单', kind: 'tags', required: true,
    description: '只允许从这些已同时进入服务端全局白名单的小写主机名下载产物。', impact: '缺失会阻断 async REST Provider 保存；不会为方便而放开 SSRF 边界。', placeholder: 'media.example.com, cdn.example.com',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.request_field_map', label: '请求字段映射', kind: 'mapping', required: true,
    description: '将平台标准语义映射到远程 API 的单层字段名；model 和 prompt 必须配置。', impact: '该合同决定真实生成请求如何装配，错误会阻断保存或烟测。',
    defaultValue: { model: 'model', prompt: 'prompt' },
    options: ['model', 'prompt', 'negative_prompt', 'width', 'height', 'duration_seconds', 'fps', 'seed', 'audio'].map((value) => ({ value, label: value })),
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.idempotency_header', label: '幂等请求头', kind: 'text', defaultValue: 'Idempotency-Key',
    description: '提交请求中传递幂等键的 HTTP header 名。', impact: '影响不确定提交后的安全恢复与去重。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.progress_field', label: '进度字段', kind: 'text',
    description: '可选的远端进度数值单层字段名；留空表示远端没有可信进度。', impact: '配置后 worker 才会记录远端进度，不会用轮询次数伪造。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.progress_max', label: '进度满值', kind: 'number', defaultValue: 100, min: 0.000001,
    description: '远端进度达到该值时表示 100%；仅在配置进度字段时使用。', impact: '错误满值会造成进度比例失真，但不改变真实终态。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest' && Boolean(getPath(payload, 'driver_config.progress_field')),
  },
  {
    path: 'driver_config.capability_ttl_seconds', label: '能力快照有效期', kind: 'number', defaultValue: 3600, min: 60, max: 604800, step: 1,
    description: '远端 capability 探测事实允许用于路由的秒数。', impact: '过期后生产会 fail closed，必须重新探测。',
    visibleWhen: (payload) => payload.driver_type === 'async_rest',
  },
  {
    path: 'driver_config.pricing.currency', label: '估算币种', kind: 'text',
    description: 'ISO 风格的大写币种/计价单位（3–12 位），例如 USD、CNY。', impact: '与单次固定费用共同用于提交前成本估算；缺失时明确显示 unknown。', placeholder: 'USD',
  },
  {
    path: 'driver_config.pricing.fixed_per_request', label: '单次固定费用', kind: 'number', min: 0,
    description: '每次 Provider 请求的非负固定估算金额。', impact: '影响预算门禁与成本预估，不替代远端实际费用事实。', placeholder: '0',
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
})).filter((field) => field.path !== 'runtime_config.runtime_source')

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

const basicFields = (dependencies: RegistryEditorDependencies): Record<RegistryResource, RegistryFieldSchema[]> => ({
  provider: [
    { path: 'key', label: '唯一 Key', kind: 'text', required: true, pattern: '^[a-z][a-z0-9_.-]{1,79}$', description: 'Provider 的稳定机器标识，创建后不应随显示名称变化。', impact: '被模型版本和审计记录引用。', placeholder: 'deepseek-primary' },
    { path: 'name', label: '显示名称', kind: 'text', required: true, description: '运营页面展示的名称。', impact: '仅影响识别，不改变路由。' },
    { path: 'driver_type', label: '驱动类型', kind: 'select', required: true, options: providerDriverOptions, description: '选择服务端已经注册的受控调用协议。', impact: '决定允许的配置字段、探测方式与可执行操作。' },
    { path: 'modalities', label: '支持模态', kind: 'tags', required: true, description: '逗号分隔的 text、image、video。', impact: '用于能力审核和操作提示。', placeholder: 'text, image' },
    { path: 'operations', label: '受控操作', kind: 'tags', description: '该 Provider 对外声明的操作名。', impact: '影响能力审核；最终可执行能力仍由驱动和真实探测判定。', placeholder: 'script.generate, prompt.normalize' },
    { path: 'is_active', label: '启用 Provider', kind: 'boolean', defaultValue: true, description: '是否允许新部署和生产路由使用。', impact: '停用后相关能力 fail closed，历史记录保留。' },
    ...driverConfigFields,
  ],
  model: [
    { path: 'profile_key', label: '模型族 Key', kind: 'text', required: true, pattern: '^[a-z][a-z0-9_.-]{1,99}$', description: '同一模型族跨版本共享的稳定标识。', impact: '用于版本管理、Prompt Profile 和生产路由引用。', placeholder: 'minimax-h3-video' },
    { path: 'version', label: '版本号', kind: 'number', required: true, defaultValue: 1, min: 1, step: 1, description: '该模型族的不可变递增版本。', impact: '发布后必须新建更高版本才能修改合同。' },
    { path: 'provider', label: 'Provider', kind: 'select', required: true, options: dependencies.providers.filter((item) => item.isActive).map((item) => ({ value: item.id, label: `${item.name} · ${item.driverType}` })), description: '从已启用 Provider 中选择前置依赖。', impact: '决定调用协议、凭据来源和可用部署。' },
    { path: 'model_identifier', label: '实际模型标识', kind: 'text', required: true, description: '提交给模型服务的模型名或本地目录标识。', impact: '生产请求会冻结并使用此标识。', placeholder: 'deepseek-chat' },
    { path: 'modality', label: '主模态', kind: 'select', required: true, options: modalityOptions, description: '模型主要输出模态。', impact: '影响路由兼容性和工作流选择。' },
    { path: 'specification.schema_version', label: '规格版本', kind: 'number', required: true, defaultValue: 1, min: 1, step: 1, description: '模型能力合同的结构版本。', impact: '未知版本会被服务端拒绝。' },
    { path: 'specification.capabilities', label: '能力列表', kind: 'tags', required: true, description: '模型真实支持的受控能力，例如 script.generate、prompt.normalize、image.text_to_image。', impact: '部署烟测和生产路由只允许这里声明的能力。' },
    { path: 'specification.model_family', label: '模型族', kind: 'text', description: '用于选择对应提示词装配策略的稳定模型族名。', impact: '影响 Prompt Profile 匹配；不应填写临时展示名称。', placeholder: 'minimax-h3' },
    { path: 'specification', label: '完整模型规格', kind: 'json', advanced: true, description: '用于 async_media 请求 Schema、smoke_inputs 等扩展能力合同。', impact: '发布后不可原地修改；服务端会校验秘密字段和媒体合同。' },
  ],
  deployment: [
    { path: 'key', label: '部署 Key', kind: 'text', required: true, pattern: '^[a-z][a-z0-9_.-]{1,99}$', description: '运行部署的稳定机器标识。', impact: '配方路由和审计通过该标识识别部署。', placeholder: 'deepseek-prod-01' },
    { path: 'name', label: '显示名称', kind: 'text', required: true, description: '管理员识别部署的名称。', impact: '仅影响页面显示。' },
    { path: 'profile_version', label: '已发布模型版本', kind: 'select', required: true, options: dependencies.modelProfiles.filter((item) => item.status === 'published').map((item) => ({ value: item.id, label: `${item.profileKey} v${item.version} · ${item.modelIdentifier}` })), description: '只能选择已发布且未退役的模型版本。', impact: '决定模型标识、Provider 及能力合同。' },
    { path: 'runtime_host', label: '运行主机', kind: 'select', options: [{ value: '', label: '远端服务 / 不绑定本地主机' }, ...dependencies.runtimeHosts.map((item) => ({ value: item.id, label: `${item.name} · ${item.status}`, disabled: item.status !== 'active' }))], description: '本地模型和 ComfyUI 应选择已探测为 active 的主机；远端 API 可不绑定。', impact: '主机不可用会阻断本地生产任务。' },
    { path: 'priority', label: '路由优先级', kind: 'number', required: true, defaultValue: 100, step: 1, description: '同能力多个部署的排序依据。', impact: '数值越高越优先，但最终仍受配方冻结路由约束。' },
    { path: 'is_default', label: '默认部署', kind: 'boolean', defaultValue: false, description: '标记同模型下的默认候选。', impact: '只影响推荐与默认选择，不覆盖已发布配方。' },
    { path: 'status', label: '部署状态', kind: 'select', required: true, defaultValue: 'active', options: [{ value: 'active', label: '启用' }, { value: 'disabled', label: '禁用' }, { value: 'error', label: '异常' }], description: '部署生命周期状态。', impact: '非 active 部署不会进入新生产路由。' },
    ...deploymentRuntimeFields,
  ],
  artifact: [
    { path: 'profile_version', label: '草稿模型版本', kind: 'select', required: true, options: dependencies.modelProfiles.filter((item) => item.status === 'draft').map((item) => ({ value: item.id, label: `${item.profileKey} v${item.version} · 草稿` })), description: '模型文件只能在模型版本发布前声明或修改。', impact: '发布后文件清单冻结；变化必须新建模型版本。' },
    { path: 'role', label: '文件角色', kind: 'select', required: true, options: artifactRoleOptions, description: '该文件在工作流中的语义角色。', impact: '工作流 model requirement 依据角色和别名装配。' },
    { path: 'logical_name', label: '逻辑名称', kind: 'text', required: true, description: '同模型版本与角色下的稳定别名。', impact: '供工作流和审核页面引用。', placeholder: 'base-checkpoint' },
    { path: 'filename', label: '预期文件名', kind: 'text', required: true, description: '运行主机应观测到的文件名。', impact: '主机探测和发布前核验使用该值。' },
    { path: 'storage_uri', label: '受管存储 URI', kind: 'text', description: '可选的受管制品库 URI；不得包含账号或 Token。', impact: '用于运维定位来源，不作为浏览器直接下载地址。' },
    { path: 'sha256', label: 'SHA-256', kind: 'text', pattern: '^[0-9a-f]{64}$', description: '可选的 64 位小写文件摘要。', impact: '填写后主机观测必须一致才能标记可用。' },
    { path: 'size_bytes', label: '文件大小（字节）', kind: 'number', min: 0, step: 1, description: '可选的预期字节数。', impact: '与 SHA-256 一起用于模型文件核验。' },
  ],
  recipe: [
    { path: 'recipe_key', label: '配方 Key', kind: 'text', required: true, pattern: '^[a-z][a-z0-9_.-]{1,99}$', description: '生产链的稳定机器标识。', impact: 'ProductionRun 只引用已发布的不可变版本。', placeholder: 'series-ai-production' },
    { path: 'version', label: '版本号', kind: 'number', required: true, defaultValue: 1, min: 1, step: 1, description: '配方不可变版本号。', impact: '发布后调整阶段或路由必须新建版本。' },
    { path: 'name', label: '显示名称', kind: 'text', required: true, description: '操作员选择生产方案时看到的名称。', impact: '应能区分 AI 起稿、已确认剧本等入口。' },
    { path: 'definition.schema_version', label: '定义版本', kind: 'number', required: true, defaultValue: 1, min: 1, step: 1, description: '配方定义的结构版本。', impact: '未知版本会阻断发布。' },
    { path: 'definition', label: '完整配方定义', kind: 'json', advanced: true, description: '用于当前版本尚未提供图形控件的扩展定义；阶段与 route 应优先使用上方结构化编辑器。', impact: '服务端发布时会复验阶段顺序、部署、工作流、烟测和 delivery 闭环。' },
  ],
  'config-definition': [
    { path: 'key', label: '配置 Key', kind: 'text', required: true, pattern: '^[a-z][a-z0-9_.-]{1,159}$', description: '全局唯一、长期稳定的配置名。', impact: 'revision、页面和生产任务均通过它读取同一事实。', placeholder: 'production.default_fps' },
    { path: 'domain', label: '配置域', kind: 'text', required: true, description: '设置中心的分类名称。', impact: '影响页面分组与运维归属。', placeholder: 'production' },
    { path: 'value_type', label: '值类型', kind: 'select', required: true, defaultValue: 'string', options: ['boolean', 'integer', 'number', 'string', 'object', 'array'].map((value) => ({ value, label: value })), description: '保存和解析时的强类型。', impact: '已有 revision 后不可改变。' },
    { path: 'description', label: '参数说明', kind: 'textarea', required: true, description: '说明参数用途、空值语义和生效范围。', impact: '普通用户依赖该说明正确配置。' },
    { path: 'unit', label: '单位', kind: 'text', description: '例如 ms、seconds、pixels、USD。', impact: '防止数值口径混淆。' },
    { path: 'impact_modules', label: '影响模块', kind: 'tags', required: true, description: '读取该配置的模块列表；逗号分隔。', impact: '用于变更审核和影响提示。', placeholder: 'production, worker' },
    { path: 'allowed_scopes', label: '允许作用域', kind: 'tags', required: true, defaultValue: 'system,project,run', description: '只允许 system、project、run 的组合。', impact: '决定是否能按项目或单次运行覆盖。' },
    { path: 'allowed_default_sources', label: '允许默认来源', kind: 'tags', description: '只允许 model、workflow；密钥或需重启项必须留空。', impact: '决定模型/工作流合同能否提供默认值。' },
    { path: 'restart_required', label: '需要重启后激活', kind: 'boolean', defaultValue: false, description: '是否必须由新进程激活 revision。', impact: '启用后保存不会立即改变当前进程。' },
    { path: 'is_required', label: '生产必填', kind: 'boolean', defaultValue: false, description: '没有有效值时是否阻断 ProductionRun。', impact: '必填配置缺失会 fail closed。' },
    { path: 'is_secret', label: '敏感配置', kind: 'boolean', defaultValue: false, description: '敏感值只允许绑定受管凭据，不保存明文 initial_value。', impact: '启用后浏览器和 API 永不回传秘密正文。' },
    { path: 'is_active', label: '启用定义', kind: 'boolean', defaultValue: true, description: '是否参与当前配置解析。', impact: '停用不会删除历史 revision。' },
    { path: 'needs_review', label: '需要人工确认', kind: 'boolean', defaultValue: false, description: '标记迁移后尚未确认的初始值。', impact: '就绪检查会持续提示。' },
    { path: 'review_reason', label: '人工确认原因', kind: 'textarea', description: '说明为何不能安全给出默认值。', impact: '帮助管理员完成后续确认。', visibleWhen: (payload) => payload.needs_review === true },
    { path: 'value_schema.minimum', label: '最小值', kind: 'number', description: '数值类型的下限。', impact: '浏览器与服务端保存都会校验。', visibleWhen: (payload) => ['integer', 'number'].includes(String(payload.value_type || '')) },
    { path: 'value_schema.maximum', label: '最大值', kind: 'number', description: '数值类型的上限。', impact: '浏览器与服务端保存都会校验。', visibleWhen: (payload) => ['integer', 'number'].includes(String(payload.value_type || '')) },
    { path: 'value_schema.pattern', label: '字符串格式', kind: 'text', description: '字符串类型的正则约束。', impact: '不匹配时阻断保存。', visibleWhen: (payload) => payload.value_type === 'string' },
    { path: 'value_schema.enum', label: '允许枚举', kind: 'tags', description: '可选值列表；逗号分隔。', impact: '设置页面会自动使用下拉选择。' },
    { path: 'value_schema', label: '完整值 Schema', kind: 'json', advanced: true, description: '仅用于上方尚未覆盖的 JSON Schema 约束。', impact: '服务端是最终校验者；非法 Schema 不会生效。' },
    { path: 'initial_value', label: '初始默认值', kind: 'boolean', description: '首次建立定义时的布尔默认值。', impact: '会进入系统配置事实并影响新运行。', visibleWhen: (payload) => payload.value_type === 'boolean' && payload.is_secret !== true },
    { path: 'initial_value', label: '初始默认值', kind: 'number', step: 1, description: '首次建立定义时的整数默认值。', impact: '会进入系统配置事实并影响新运行。', visibleWhen: (payload) => payload.value_type === 'integer' && payload.is_secret !== true },
    { path: 'initial_value', label: '初始默认值', kind: 'number', description: '首次建立定义时的数值默认值。', impact: '会进入系统配置事实并影响新运行。', visibleWhen: (payload) => payload.value_type === 'number' && payload.is_secret !== true },
    { path: 'initial_value', label: '初始默认值', kind: 'text', description: '首次建立定义时的字符串默认值。', impact: '会进入系统配置事实并影响新运行。', visibleWhen: (payload) => payload.value_type === 'string' && payload.is_secret !== true },
    { path: 'initial_value', label: '初始默认值', kind: 'json', advanced: true, description: '对象或数组类型的初始值；敏感配置必须留空。', impact: '会进入系统配置事实并影响新运行。', visibleWhen: (payload) => ['object', 'array'].includes(String(payload.value_type || '')) && payload.is_secret !== true },
  ],
})

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
  return basicFields(dependencies)[resource]
    .filter((field) => !field.visibleWhen || field.visibleWhen(contextualPayload))
    .map((field) => {
      const driverType = String(contextualPayload.driver_type || '')
      const baseUrlPath = resource === 'deployment' ? 'runtime_config.base_url' : 'driver_config.base_url'
      const timeoutPath = resource === 'deployment' ? 'runtime_config.timeout_seconds' : 'driver_config.timeout_seconds'
      if (field.path === baseUrlPath) {
        if (resource === 'deployment') return { ...field, required: !String(provider?.driverConfig.base_url || '').trim() }
        if (driverType === 'comfyui') return { ...field, required: false }
      }
      if (field.path === timeoutPath) return { ...field, max: driverType === 'comfyui' ? 3600 : 600 }
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

const empty = (value: unknown): boolean => value === undefined || value === null || value === ''
  || (Array.isArray(value) && value.length === 0)
  || (typeof value === 'object' && !Array.isArray(value) && Object.keys(value as object).length === 0)

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
  if (resource === 'model' && !dependencies.providers.some((item) => item.isActive)) errors.push('请先新增并启用 Provider，再创建模型版本')
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
  }
  if (resource === 'recipe') {
    const definition = payload.definition
    const stages = definition && typeof definition === 'object' && !Array.isArray(definition)
      ? (definition as JsonObject).stages : null
    if (!Array.isArray(stages) || !stages.length) errors.push('配方必须至少包含一个结构化生产阶段')
    if (!dependencies.deployments.some((item) => item.status === 'active')) warnings.push('当前没有 active Deployment；需要路由的阶段将无法发布')
  }
  return { errors: [...new Set(errors)], warnings: [...new Set(warnings)] }
}

export const standardRecipeStages = (scriptMode: 'ai' | 'confirmed'): JsonObject[] => {
  const script = scriptMode === 'ai'
    ? [{ key: 'script_generate', mode: 'ai', approval: 'auto', routing: { mode: 'fixed', routes: [], variant_count: 1, evaluator: null } }]
    : [{ key: 'script_input', mode: 'confirmed_revision', approval: 'auto' }]
  return [
    ...script,
    { key: 'script_review', mode: 'review', approval: 'required' },
    { key: 'bible_materialize', mode: 'deterministic', approval: 'auto' },
    { key: 'bible_review', mode: 'review', approval: 'required' },
    { key: 'storyboard', mode: 'deterministic', approval: 'auto' },
    { key: 'storyboard_review', mode: 'review', approval: 'required' },
    { key: 'script_normalize', mode: 'ai', approval: 'auto', routing: { mode: 'fixed', routes: [], variant_count: 1, evaluator: null } },
    { key: 'script_normalize_review', mode: 'review', approval: 'required' },
    { key: 'prompt_compile', mode: 'existing_pipeline', approval: 'auto' },
    { key: 'media_generation', mode: 'existing_pipeline', approval: 'auto', routing: { mode: 'fixed', routes: [], variant_count: 1, evaluator: null } },
    { key: 'artifact_selection', mode: 'review', approval: 'required' },
    { key: 'assembly', mode: 'existing_pipeline', approval: 'auto' },
    { key: 'delivery', mode: 'existing_pipeline', approval: 'auto' },
  ]
}

export const routedRecipeStageKeys = new Set(['script_generate', 'script_structure', 'script_normalize', 'media_generation'])

export const recipeStageOptions: RegistryOption[] = [
  'script_input', 'script_generate', 'script_structure', 'script_review', 'bible_materialize', 'bible_review',
  'storyboard', 'storyboard_review', 'script_normalize', 'script_normalize_review',
  'prompt_compile', 'media_generation', 'artifact_selection', 'assembly', 'delivery',
].map((value) => ({ value, label: value }))

export const recipeStageModes: Record<string, string[]> = {
  script_input: ['confirmed_revision', 'manual'],
  script_generate: ['ai'],
  script_structure: ['ai'],
  script_review: ['review'],
  bible_materialize: ['deterministic'],
  bible_review: ['review', 'manual'],
  storyboard: ['deterministic', 'manual'],
  storyboard_review: ['review', 'manual'],
  script_normalize: ['ai'],
  script_normalize_review: ['review'],
  prompt_compile: ['existing_pipeline'],
  media_generation: ['existing_pipeline'],
  artifact_selection: ['review'],
  assembly: ['existing_pipeline'],
  delivery: ['existing_pipeline'],
}
