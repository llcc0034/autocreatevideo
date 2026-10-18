const withCode = (label: string, value: string): string => `${label}（${value}）`

const lookup = (value: unknown, labels: Record<string, string>, fallbackPrefix: string): string => {
  const code = String(value ?? '').trim()
  if (!code) return '未提供'
  return withCode(labels[code] || fallbackPrefix, code)
}

const driverLabels: Record<string, string> = {
  local_qwen: '本地 Qwen 兼容接口',
  deepseek_api: 'DeepSeek 应用接口',
  comfyui: 'ComfyUI 工作流执行器',
  openai_compatible: '兼容 OpenAI 的接口',
  async_rest: '受限异步接口',
}

const mediaKindLabels: Record<string, string> = {
  text: '文本', image: '图片', video: '视频', audio: '音频', multimodal: '多模态',
}

const artifactRoleLabels: Record<string, string> = {
  model: '模型文件',
  checkpoint: '模型检查点',
  diffusion_model: '扩散模型',
  text_encoder: '文本编码器',
  vae: '变分自编码器',
  lora: '低秩适配器',
  tokenizer: '分词器',
  episode_master: '分集成片母版',
  primary_generation_output: '主生成产物',
  bible_reference_candidate: '设定参考图候选',
  other: '其他文件',
}

const operationLabels: Record<string, string> = {
  translate: '翻译',
  series_concept: '连续剧构思',
  shot_detail: '分镜细化',
  image_generation: '图片生成',
  video_generation: '视频生成',
  health: '健康检查',
  object_info: '节点目录读取',
  'script.generate': '剧本生成',
  'script.structure': '剧本结构化',
  'visual_rules.generate': '全剧影视规则生成',
  'prompt.normalize': '提示词规范化',
  'prompt.fields.generate': '提示词字段生成',
  'prompt.fields.translate': '提示词字段翻译',
  'prompt.draft': '提示词起草',
  'text.structured': '通用结构化文本',
  'image.text_to_image': '文生图',
  'video.text_to_video': '文生视频',
  'video.reference_to_video': '参考生视频',
  'video.image_to_video': '图片生视频',
  'video.first_last_frame_to_video': '首尾帧生视频',
  'audio.text_to_audio': '文生音频',
  'image.generate': '图片生成',
  'video.generate': '视频生成',
  'audio.generate': '音频生成',
  'artifact.assemble': '产物组装',
  'delivery.release': '交付发布',
}

const stageLabels: Record<string, string> = {
  script_input: '剧本输入',
  script_generate: 'AI 剧本生成',
  script_structure: '剧本结构化',
  script_review: '剧本审核',
  bible_materialize: '设定集生成',
  bible_review: '设定集审核',
  visual_rules_generate: '全剧影视规则生成',
  visual_rules_review: '全剧影视规则审核',
  bible_image_generation: '设定参考图生成',
  bible_image_review: '设定参考图审核',
  storyboard: '分镜生成',
  storyboard_review: '分镜审核',
  media_generation: '媒体生成',
  artifact_selection: '产物选片',
  assembly: '分集组装',
  delivery: '交付封存',
}

const modeLabels: Record<string, string> = {
  confirmed_revision: '使用已确认剧本版本',
  manual: '人工操作',
  ai: 'AI 执行',
  review: '审核门禁',
  deterministic: '确定性处理',
  existing_pipeline: '正式生产流水线',
  fixed: '使用单一实例',
  ordered_fallback: '失败后按顺序切换实例',
  manual_choice: '运行时人工选择',
  T2VA: '纯文生视频',
  I2VA: '首帧生视频',
  L2VA: '尾帧生视频',
  FL2VA: '首尾帧生视频',
  Ref2VA: '参考生视频',
}

const valueTypeLabels: Record<string, string> = {
  text: '短文本', long_text: '长文本', string_list: '文本列表', enum: '枚举',
  boolean: '布尔值', integer: '整数', number: '数值', string: '文本', object: '对象', array: '数组',
}

const scopeLabels: Record<string, string> = {
  system: '系统级', project: '项目级', run: '单次运行级', model: '模型默认', workflow: '工作流默认',
}

const freshnessLabels: Record<string, string> = {
  fresh: '证据有效', stale: '证据已过期', passed: '已通过', succeeded: '成功', not_run: '未执行', not_applicable: '不适用',
  active: '启用', inactive: '停用', disabled: '禁用', ready: '就绪', queued: '排队中', submitting: '提交中', submitted: '已提交', running: '运行中',
  cancel_requested: '取消中', cancelled: '已取消', uncertain: '待消歧', failed: '失败', error: '异常', available: '可用', missing: '缺失', blocked: '已阻断',
}

const reasonCodeLabels: Record<string, string> = {
  route_selection_required: '需要选择执行实例',
  routing_not_configured: '执行实例尚未配置',
  unknown_cost: '费用未知，等待授权',
  external_call_not_authorized: '外部调用未授权',
  cost_currency_missing: '缺少费用币种',
  uncertain_remote_response: '远端结果不确定，等待消歧',
  actual_cost_reconciliation_required: '需要核对实际费用',
  stage_approval_required: '阶段等待审批',
  manual_stage_confirmation_required: '人工步骤等待确认',
  existing_storyboard_requires_manual_resolution: '已有分镜，需要人工处理',
  driver_contract_missing: '缺少执行驱动合同',
  published_recipe_missing: '缺少已发布生产配方',
  workflow_fixture_contract_missing: '缺少工作流烟测输入合同',
  compatible_deployment_missing: '缺少可用的兼容模型部署',
  text_resource_smoke_passed: '文本资源基础烟测已通过，实例组合需另行验收',
  text_resource_smoke_required: '缺少文本能力的有效基础烟测，连接健康不能替代',
  resource_smoke_evidence_invalid: '基础烟测证据或目标配置不可核验',
  recipe_contract_invalid: '配方合同或精确实例引用不可核验',
  readiness_evaluation_failed: '就绪检查读取失败，请重新读取',
  execution_evidence_unverifiable: '实例资源或组合验收证据不可核验',
  execution_auxiliary_attention: '辅助实例尚有待核对项；仅在实际调用它时才需满足准入条件',
  execution_route_alternative_attention: '部分候选实例尚未就绪；实际选择时只核对选中的实例',
  execution_smoke_missing: '此精确实例组合尚无通过记录',
  execution_smoke_failed: '此精确实例组合最近一次烟测失败',
  execution_smoke_expired: '组合验收已过期，是否阻断按当前执行策略决定',
  execution_capacity_unknown: '尚无本次请求规格，容量需在实际执行准备时核对',
  execution_capacity_uncovered: '本次规格尚无对应的容量观测',
  text_generation_worker_unavailable: '文本执行服务不可用',
  async_media_worker_unavailable: '异步媒体执行服务不可用',
  generation_worker_unavailable: '工作流执行服务不可用',
  execution_snapshot_unlinked: '任务快照未关联工作流',
  model_artifact_attestation_required: '模型文件尚未完成证明',
  model_artifact_contract_required: '模型文件合同不完整',
  model_inventory_missing: '运行主机缺少声明的模型文件',
  model_profile_modality_mismatch: '目标模型类型与工作流输出不兼容',
  model_profile_not_published: '模型能力版本尚未发布',
  model_profile_selection_required: '尚未选择精确模型版本',
  required_dimension_multiple_unresolved: '画面尺寸倍数约束无法证明',
  resolution_execution_adapter_unavailable: '当前执行适配器不支持该尺寸策略',
  unsupported_dynamic_workflow_family: '工作流动态结构尚无受审合同方案',
  workflow_profile_projection_failed: '工作流与模型合同投影失败',
  workflow_projector_contract_mismatch: '工作流合同方案与图结构不兼容',
  workflow_projector_engine_unavailable: '工作流合同引擎不可用',
}

export const driverLabel = (value: unknown) => lookup(value, driverLabels, '未识别驱动')
export const mediaKindLabel = (value: unknown) => lookup(value, mediaKindLabels, '未识别媒体类型')
export const artifactRoleLabel = (value: unknown) => lookup(value, artifactRoleLabels, '未识别文件角色')
export const operationLabel = (value: unknown) => lookup(value, operationLabels, '自定义操作')
export const stageLabel = (value: unknown) => lookup(value, stageLabels, '未识别阶段')
export const modeLabel = (value: unknown) => lookup(value, modeLabels, '未识别模式')
export const valueTypeLabel = (value: unknown) => lookup(value, valueTypeLabels, '未识别值类型')
export const scopeLabel = (value: unknown) => lookup(value, scopeLabels, '自定义作用域')
export const freshnessLabel = (value: unknown) => lookup(value, freshnessLabels, '未识别证据状态')
export const reasonCodeTitle = (value: unknown): string => {
  const code = String(value ?? '').trim()
  if (!code) return '未提供'
  return reasonCodeLabels[code] || '未识别的阻断原因'
}
export const reasonCodeLabel = (value: unknown) => lookup(value, reasonCodeLabels, '未识别的阻断原因')

export const commaListLabel = (value: unknown, presenter: (item: string) => string): string => {
  const items = Array.isArray(value) ? value : String(value ?? '').split(',')
  const normalized = items.map((item) => String(item).trim()).filter(Boolean)
  return normalized.length ? normalized.map(presenter).join('、') : '未声明'
}

export const mediaKindListLabel = (value: unknown) => commaListLabel(value, mediaKindLabel)
export const operationListLabel = (value: unknown) => commaListLabel(value, operationLabel)
