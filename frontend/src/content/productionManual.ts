export type ManualInput = {
  label: string
  example: string
  note: string
}

export type ManualStep = {
  id: string
  number: number
  title: string
  summary: string
  location: string
  route?: string
  inputs: ManualInput[]
  actions: string[]
  success: string[]
  next: string
  cautions?: string[]
  keywords: string[]
}

export type ManualIncident = {
  id: string
  symptom: string
  likelyCause: string
  actions: string[]
  confirmation: string
  keywords: string[]
}

/**
 * 页面内生产手册的唯一内容源。
 *
 * 修改生产入口、状态机、配置激活、执行器或恢复方式时，必须同步更新本文件，
 * 并运行 npm run test:manual、npm run typecheck 与 npm run build。
 */
export const productionManual = {
  schemaVersion: 2,
  version: '2026.08.24.2',
  updatedAt: '2026-08-24',
  title: '从内容审核到逐集交付：生产操作手册',
  subtitle: '以 ProductionRun 为唯一新生产入口，覆盖语义工作流、异步执行、受管产物和逐集交付',
  defaults: {
    videoResolution: '以已激活配置与 ProductionRun 快照为准',
  },
  steps: [
    {
      id: 'create-project',
      number: 1,
      title: '选择空项目、导入剧本或 AI 创建',
      summary: '新建页提供三个受审入口；导入与 AI 均进入 ProductionRun，手工模式只建立空项目。',
      location: '左侧“连续剧项目”',
      route: '/projects',
      inputs: [
        { label: '创建方式', example: '空项目 / 导入剧本 / AI 描述创建', note: '有完整剧本必须走服务端提取、预览、确认；不能保存为不可运行的旁路母稿。' },
        { label: '自动生产', example: '已发布配方、整集统一工作流、云端授权与预算', note: '只自动推进非人工阶段；遇到 required review 会明确暂停。' },
      ],
      actions: ['点击“新建连续剧”并选择一种创建方式。', '导入模式粘贴或上传 TXT/Markdown/Fountain/DOCX，核对服务端不可变预览后确认；非规范正文由已发布 script.structure 策略生成待审核结构化 revision。', 'AI 模式填写需求、集数、镜数、时长、预算和外部调用授权。', '带媒体配方选择支持项目默认视频模式的整集统一 route；已有 Shot 的运行页可改为逐镜头选择。'],
      success: ['项目、ScriptSource/Revision（如有）和 ProductionRun（如选择自动生产）均有稳定 ID。', '取消或后续失败会保留已创建事实并显示明确错误，不会伪装完整成功。'],
      next: '在 ProductionRun 审核脚本、Bible、分镜和提示词标准化候选。',
      cautions: ['AI 和结构化模型只生成 preview；采用标题/梗概也需显式勾选。', '空项目不会自动生成脚本或媒体。'],
      keywords: ['项目', '新建连续剧', '导入剧本', 'script.structure', 'AI 创建', 'ProductionRun'],
    },
    {
      id: 'prepare-content',
      number: 2,
      title: '准备脚本、Bible 与分镜',
      summary: '把人物、场景、道具、对白和连续性保存为可审核的内容事实。',
      location: '连续剧工作室 → 项目总览 / 一致性 Bible / 剧集与分镜',
      inputs: [
        { label: '脚本', example: '按集、场、镜头保存的完整正文', note: '对白与人物关系以原文为准。' },
        { label: 'Bible', example: '人物身份、场景结构、道具形态', note: '记录不允许跨镜头漂移的事实。' },
        { label: '分镜', example: '构图、动作、运镜、声音、持续时间', note: '只保存当前确认内容，不在此页提交生产。' },
      ],
      actions: ['保存脚本母稿与剧集。', '建立人物、场景、道具 Bible 并完成必要翻译审核。', '核对分镜对白、引用、连续性和媒体类型。'],
      success: ['脚本、Bible 与镜头均能从服务端重新读取。', '待审核和来源已变化状态没有被静默忽略。'],
      next: '由平台操作员完成 provider、deployment、workflow 和配置准备。',
      cautions: ['普通字段翻译不是 AI script 正式生产阶段，也不能绕过 ProductionRun 审核。'],
      keywords: ['脚本', 'Bible', '分镜', '翻译审核', '连续性'],
    },
    {
      id: 'configure-providers',
      number: 3,
      title: '配置 Provider 与模型部署',
      summary: '登记能力、受管凭据绑定与目标部署；平台不会自动下载模型。',
      location: '系统设置 → Provider / 模型',
      route: '/settings',
      inputs: [
        { label: '受管凭据', example: 'credential_id 与“已配置”状态', note: '不要在页面、日志或文档中粘贴原始密钥。' },
        { label: 'Deployment', example: 'provider、model version、runtime host', note: 'catalog 条目不等于已安装或已授权。' },
      ],
      actions: ['创建或审阅 provider profile。', '从数据库受管凭据中选择 credential_id，并在目标环境验证授权。', '按草稿模型版本/文件声明 → 发布 → 主机启用并探测为 active → deployment → 再探测主机 → 文件核验 → deployment probe/smoke 的顺序登记；文本部署分别执行 script.generate、script.structure 与 prompt.normalize 真实烟测。', '每次 smoke 接受 HTTP 202 的持久任务并等待页面轮询终态；刷新页面后从任务 collection 恢复。queued 可取消，submitting 后不确定时先核对 Provider 控制台与账单，再追加管理员消歧证据，禁止重复点击重放付费请求。'],
      success: ['API 不回显 credential 原值或 smoke 冻结请求正文。', '目标 deployment 的三项文本 smoke 与媒体 smoke 都显示持久 succeeded 任务及当前 hash 证据，可追踪到 host。'],
      next: '审阅并发布 PromptProfile 装配策略与绑定。',
      cautions: ['推荐和 catalog 不会自动下载模型、创建虚假 deployment 或确认许可。'],
      keywords: ['provider', 'credential_id', '受管凭据', '模型部署', 'probe', 'smoke'],
    },
    {
      id: 'configure-prompt-profiles',
      number: 4,
      title: '配置并发布 PromptProfile 装配策略',
      summary: '每个实际模型、操作和模式都必须解析到唯一已发布策略；缺失或同优先级冲突会阻断生产。',
      location: '系统设置 → 提示词策略',
      route: '/settings?section=prompts',
      inputs: [
        { label: '字段目录', example: '主题、环境、灯光、动作、镜头、风格、对白语调、音乐、负面约束', note: '字段说明同时展示含义和对模型输入、连续性或执行合同的影响。' },
        { label: 'Profile 与规则', example: '模型族 / 精确模型 / operation / mode / channel / selector / 顺序', note: '官方有说明时记录官方链接与版本；无官方说明时使用需人工审核的通用策略。' },
        { label: 'Binding', example: 'DeepSeek script.generate、SDXL image.generate、MiniMax H3 T2VA', note: '生产按精确模型、族、模式和优先级解析唯一 binding，不静默使用旧提示词。' },
      ],
      actions: ['审阅 seed 为 needs_review 的草稿策略，不把 seed 当成已发布能力。', '在确定性 preview 中核对各 channel、字段来源、顺序、默认值和省略规则。', '发布 Profile 后创建 active binding；对 script.generate、script.structure、prompt.normalize、image.generate 和每个 video mode 分别检查。'],
      success: ['每条生产 route 都解析到唯一 active binding 和不可变 Profile 版本/hash。', '关闭或移除的字段不会从旧 builder 旁路进入最终提示词。'],
      next: '审阅并发布语义 Workflow。',
      cautions: ['多个策略可以共存，但同一实际 route 的最高优先级只能有一个。', '系统/JSON 输出合同和引用安全门禁不可被用户字段覆盖。'],
      keywords: ['PromptProfile', '字段目录', '装配策略', 'binding', '模型族', 'operation', 'mode', 'preview'],
    },
    {
      id: 'validate-workflows',
      number: 5,
      title: '校验并发布语义 Workflow',
      summary: '按 input/output semantics 绑定节点；不依赖固定 Comfy 节点 ID。',
      location: '生产工作流 / 工作流版本编辑',
      route: '/settings?section=workflows',
      inputs: [
        { label: 'Semantic bindings', example: 'prompt、尺寸、帧率、输出', note: '每个必需语义必须唯一、可解析。' },
        { label: 'Smoke evidence', example: '真实 executor submission 与结果', note: '仅构造请求或 mock response 不算真实 smoke。' },
      ],
      actions: ['打开 needs_review 的 workflow version。', '用字段化表单审阅 graph、input contract、semantic bindings 和 model requirements；依赖项必须从已发布资源下拉选择。', '依次执行 validate、异步真实 smoke-test 和显式 publish。', '需要在 ComfyUI 查看时使用“在 ComfyUI 中打开（下载导入）”；系统下载安全 UI JSON，不会伪造可直接打开的 URL。'],
      success: ['版本不再是未审阅的 inactive draft。', 'validation 与 real smoke evidence 均属于当前图版本。'],
      next: '发布配置并处理 boot activation。',
      cautions: ['bootstrap catalog 初始就是 needs_review；setup、demo seed 和 restart 都不会覆盖它。', '下载导入使用该版本已验证的精确 deployment；draft 必须显式选择兼容 deployment。'],
      keywords: ['semantic workflow', 'needs_review', 'validate', 'real smoke', 'publish', 'ComfyUI', 'download_import'],
    },
    {
      id: 'activate-configuration',
      number: 6,
      title: '解析五层配置并激活新 Boot',
      summary: '确认 model default、workflow default、system、project 与 run override 的职责边界。',
      location: '系统设置 → 配置 / 运行主机',
      route: '/settings',
      inputs: [
        { label: 'Config revision', example: '全局或项目 revision', note: 'ProductionRun 创建时再固化不可变 snapshot。' },
        { label: 'Boot activation', example: 'pending restart-required revision', note: '新 boot ID 本身不等于已激活。' },
      ],
      actions: ['使用字段化参数编辑器填写值；模型部署等依赖必须从服务端核验的下拉选择，禁止粘贴 UUID。', '发布经过校验的 config revision。', '对 restart-required revision 启动新 boot。', '在新 boot 上显式激活，并读取 resolved config 复核。'],
      success: ['resolved config 显示预期来源层。', 'pending revision 有明确 activation 审计，旧 run 快照不被回写。'],
      next: '探测主机并人工审阅推荐草案。',
      cautions: ['不要在 restart 脚本中静默激活，也不要把 pending 显示成 active。'],
      keywords: ['五层配置', 'model default', 'workflow default', 'system revision', 'project revision', 'run override', 'boot activation'],
    },
    {
      id: 'probe-host',
      number: 7,
      title: '探测 RuntimeHost 并审阅推荐',
      summary: 'probe 记录当前能力；recommend 只生成可审核草案。',
      location: '系统设置 → 运行主机',
      route: '/settings',
      inputs: [
        { label: 'Host', example: '目标机器与 executor endpoint', note: '探测结果是带时间的快照。' },
        { label: 'Recommendation', example: 'recipe/config draft', note: '接受后仍需分别发布和激活。' },
      ],
      actions: ['对目标 RuntimeHost 执行 probe。', '读取 readiness，确认 deployment 与 workflow 兼容。', '需要时生成 recommend；人工审阅后再提交 decide。', 'decide 由服务端复验 host snapshot、catalog/config hash 与候选当前兼容性；提示 stale 时重新 probe/recommend。'],
      success: ['能力快照有当前时间与真实错误。', '陈旧推荐被服务端拒绝。', '接受推荐没有自动下载模型、发布 workflow 或激活配置。'],
      next: '进入项目的一键生产页面创建 ProductionRun。',
      cautions: ['当前没有跨节点或多 GPU scheduler；worker 并发只是进程局部设置。'],
      keywords: ['RuntimeHost', 'probe', 'readiness', 'recommend', '草案', '多 GPU'],
    },
    {
      id: 'create-production-run',
      number: 8,
      title: '创建 ProductionRun',
      summary: '所有新生产都从这里创建；不从 Tasks、Assemblies 或 Releases 发起。',
      location: '项目 → “一键生产”',
      inputs: [
        { label: '配方', example: '已发布 ProductionRecipe', note: '发布门禁已验证唯一 content 入口与完整 content → storyboard → prompt → media → selection → assembly → delivery 闭包。' },
        { label: '运行边界', example: '选集、任务上限、外部调用与预算', note: '留空项由服务端 effective config 解析并固化。' },
        { label: 'Workflow selection', example: '整集统一，或每个镜头独立 route', note: '只从所选已发布配方的兼容 route 中选择，服务端按镜头输出/模式复验并冻结。' },
      ],
      actions: ['打开目标项目的“一键生产”。', '选择已发布配方和所需输入；确认外部调用与预算。', '对已有 Shot 选择“整集统一”或“逐镜头”，可将同一 route 应用到整集后再逐项调整。', '点击“创建 ProductionRun”，等待服务端返回当前 stage/gate。'],
      success: ['出现唯一 run ID、状态、当前阶段和配置/配方快照。', '刷新详情仍能读取同一 run，没有旁路任务。'],
      next: 'AI script 模式先审核脚本和 Bible；其他模式处理当前 gate。',
      cautions: ['不要调用已删除的旧生成入口，也不要直接创建 Task 或 Assembly。'],
      keywords: ['ProductionRun', '一键生产', '配方', '预算', '快照'],
    },
    {
      id: 'review-script-bible',
      number: 9,
      title: '审核 AI 脚本与确定性 Bible',
      summary: 'AI script 正式链路是“脚本生成 → 人工审核 → 确定性 Bible → 人工审核”。',
      location: 'ProductionRun 详情 → 审批证据',
      inputs: [
        { label: '脚本审核', example: '正文、revision、内容 hash、警告', note: '模型只生成 preview；批准后才确认。' },
        { label: 'Bible 审核', example: '候选集合、来源 hash、解析诊断', note: 'Bible 由确认脚本确定性 materialize，不是第二次自由生成。' },
      ],
      actions: ['逐段核对完整脚本、hash 和警告。', '选择“批准并继续”或“拒绝并终止”，填写必要说明。', '进入 Bible gate 后核对候选、来源指纹和诊断，再提交审批。'],
      success: ['脚本 revision 与 run 审核快照一致。', 'Bible 候选来源于本次确认脚本，两个审核决定均有审计。'],
      next: '处理 route、cost 和候选选择 gate。',
      cautions: ['脚本或 Bible 来源发生漂移时停止审批，不从当前表反推或补写审核证据。'],
      keywords: ['AI script', '脚本审核', 'deterministic Bible', 'Bible 审核', 'hash'],
    },
    {
      id: 'handle-run-gates',
      number: 10,
      title: '处理路由、成本与候选 Gate',
      summary: '只对本次运行冻结的候选作选择，所有决定记录在 ProductionRun。',
      location: 'ProductionRun 详情 → 当前阶段',
      inputs: [
        { label: 'Route', example: 'provider、deployment、workflow version', note: '选择兼容且已验证的冻结候选。' },
        { label: 'Cost', example: '币种、估算、授权上限', note: '币种不一致或预算不足时保持 blocked。' },
        { label: 'Artifact candidate', example: '当前 lineage 的 managed artifact', note: '远端 URL 未归档前不可选择。' },
      ],
      actions: ['路由 gate 点击“确认所选路由”。', '审批/成本 gate 核对证据后提交决定。', '候选 gate 仅选择已验证、属于本 run lineage 的 managed artifact。'],
      success: ['每个选择都记录实际 provider、deployment、workflow、成本或 artifact ID。', '不兼容、未授权或未归档的候选仍保持 blocked。'],
      next: '继续运行并观察异步执行、归档和装配。',
      cautions: ['async I2V/reference image 没有 provider upload contract 时必须 blocked，不能降级为纯文本任务。'],
      keywords: ['route', 'cost', 'artifact selection', 'provider_upload_contract_missing', 'blocked'],
    },
    {
      id: 'monitor-production-run',
      number: 11,
      title: '从 ProductionRun 监控和恢复',
      summary: '运行级 continue、retry、cancel 与 uncertain recovery 是正式控制面；当前 async REST 正式支持 T2I/T2V。',
      location: 'ProductionRun 详情 → 命令栏 / 阶段时间线',
      inputs: [
        { label: '运行状态', example: 'awaiting_approval、blocked、failed、uncertain、completed', note: '显示真实状态，不用默认值替代。' },
      ],
      actions: ['普通自动阶段由 worker 在持久事实提交后幂等推进；required review、成本或选择 gate 会明确暂停。', 'T2I/T2V 路由应观察真实 submit、poll 和 managed artifact 归档。', 'blocked/failed 时先修复原因，再点击“重试当前阶段”。', '远端提交不确定时由管理员核验 provider 证据，再执行 resolve-uncertain；不要普通重试。', '需要止损时点击“取消运行”。'],
      success: ['状态、stage、attempt 和错误码在重新读取后保持一致。', '重试没有重复扣费或生成未归档的孤儿结果。'],
      next: '用诊断投影核对任务、产物、装配和交付。',
      cautions: ['仓库没有真实外部 provider/GPU 完整 E2E 证据；目标环境必须单独留证。'],
      keywords: ['continue', 'retry', 'cancel', 'uncertain', 'resolve-uncertain', 'async REST', 'T2I', 'T2V', 'E2E'],
    },
    {
      id: 'inspect-diagnostics',
      number: 12,
      title: '只读检查 Tasks、Assemblies 与 Releases',
      summary: '这些页面是 ProductionRun 的诊断投影，不是第二生产入口。',
      location: '生产工作流 → 诊断列表',
      route: '/workflows',
      inputs: [
        { label: 'Lineage', example: 'run、stage、task、attempt、artifact、assembly', note: '用 ID 与时间线定位根因。' },
        { label: '参数差异', example: 'GeneratedArtifact comparison_parameters', note: '归档时从冻结事实白名单生成；不使用 manifest 的身份、对象键、输出索引或 hash。' },
      ],
      actions: ['读取 task/attempt 状态和输入输出摘要。', '读取 Deferred request 历史与所属 run。', '候选参数比较只读取 comparison_parameters。', '读取 assembly source items 与 episode master。', '读取 ReleaseBundle 并执行 verify。', '任何创建、重试、取消或有序 fallback 都回到所属 ProductionRun 操作。'],
      success: ['诊断资源可追溯到 ProductionRun。', '页面没有通用的新建、组装或发布写按钮。'],
      next: '验证逐集母版和 ReleaseBundle。',
      cautions: ['Tasks 与 Deferred requests 均为完全只读的历史事实，不存在任务级写例外。'],
      keywords: ['只读诊断', 'Tasks', 'Assemblies', 'Releases', 'Deferred history'],
    },
    {
      id: 'verify-delivery',
      number: 13,
      title: '验证逐集母版与 ReleaseBundle',
      summary: '每集一个 episode master；发布包聚合逐集母版，不生成整季单条长视频。',
      location: 'ProductionRun 详情 / ReleaseBundle 诊断',
      inputs: [
        { label: 'Episode master', example: '每个目标 episode 的受管母版', note: '必须包含完整 source items 与 provenance。' },
        { label: 'ReleaseBundle', example: '逐集 master 清单与验证结果', note: '缺集或无效媒体会阻断完成。' },
      ],
      actions: ['逐集播放并核对时长、声音、画面和 source items。', '核对 managed artifact 与 attempt/provenance。', '执行 ReleaseBundle verify，确认目标集全部存在。'],
      success: ['每集有独立、可验证的 master。', 'ReleaseBundle verify 通过，内容与 ProductionRun 范围一致。'],
      next: '归档运行证据，并按备份恢复手册保护数据库与受管媒体。',
      cautions: ['不要把不存在的整季长视频当作成功标志。'],
      keywords: ['episode master', 'ReleaseBundle', 'managed artifact', 'provenance', '逐集'],
    },
  ] as ManualStep[],
  incidents: [
    {
      id: 'migration-pending',
      symptom: '启动或 API 提示数据库结构未就绪',
      likelyCause: '目标数据库有待应用 migration。',
      actions: ['停止新的生产提交。', '备份数据库。', '运行 migrate --plan、migrate、migrate --check。'],
      confirmation: 'migrate --check 成功，健康检查和目标页面正常。',
      keywords: ['migration', 'migrate --check', '数据库'],
    },
    {
      id: 'workflow-needs-review',
      symptom: '没有可选 workflow，或版本显示 needs_review',
      likelyCause: 'bootstrap catalog 尚未完成语义绑定、校验、真实 smoke 和发布。',
      actions: ['打开工作流版本编辑。', '完成 semantic bindings。', '依次 validate、real smoke、publish。'],
      confirmation: '当前图版本已有 validation/smoke evidence 且处于 published。',
      keywords: ['needs_review', 'semantic', 'smoke'],
    },
    {
      id: 'deployment-unavailable',
      symptom: '路由没有可用模型部署',
      likelyCause: 'provider 未授权、deployment/host 未 probe 或 workflow 不兼容。',
      actions: ['核对 credential configured state。', '重新 probe host 与 deployment。', '修复兼容性后重新读取 run。'],
      confirmation: 'ProductionRun 返回可审计的兼容 route 候选。',
      keywords: ['deployment', 'probe', 'route'],
    },
    {
      id: 'upload-contract-missing',
      symptom: 'I2V 或参考图任务被 provider_upload_contract_missing 阻断',
      likelyCause: 'async REST provider 没有已实现和验证的上传/引用契约。',
      actions: ['保持 run blocked。', '确认目标 provider 的上传与引用格式。', '完成代码、契约和真实 E2E 后再重试 run。'],
      confirmation: 'provider-specific upload contract 已验证，参考输入被真实提交且归档。',
      keywords: ['I2V', 'reference image', 'provider_upload_contract_missing'],
    },
    {
      id: 'provider-authorization',
      symptom: '外部 provider 返回未授权、配额不足或 endpoint 错误',
      likelyCause: '受管凭据绑定、账号权限、配额或 endpoint 配置不正确。',
      actions: ['不要回显密钥。', '由超级管理员在系统设置中核对受管凭据状态。', '修复后重新执行 probe/smoke，再从 run 重试。'],
      confirmation: '真实授权和 smoke 成功，错误记录仍可审计。',
      keywords: ['provider', 'credential_id', '受管凭据', 'quota'],
    },
    {
      id: 'cost-blocked',
      symptom: 'ProductionRun 因成本或币种停在 blocked',
      likelyCause: '估算币种、预算上限或外部调用授权不满足。',
      actions: ['核对冻结 cost evidence。', '修正配置或提交明确授权。', '在 ProductionRun 继续，不创建旁路任务。'],
      confirmation: '授权记录与路由成本一致，run 可继续。',
      keywords: ['cost', 'currency', 'budget'],
    },
    {
      id: 'script-review-drift',
      symptom: 'AI 脚本审核证据与 ScriptRevision 不一致',
      likelyCause: 'revision、正文 hash 或 run snapshot 发生漂移。',
      actions: ['拒绝当前审批。', '重新读取服务端完整 revision。', '创建新的合规运行或阶段，不拼接旧证据。'],
      confirmation: '审核的 revision、正文 hash 与 run snapshot 完全一致。',
      keywords: ['script_review', 'hash', 'drift'],
    },
    {
      id: 'bible-review-conflict',
      symptom: 'Bible 候选来源或内容 hash 不匹配',
      likelyCause: '候选不是由本次已确认脚本的冻结快照确定性生成。',
      actions: ['停止审批。', '核对 script revision 与 source fingerprint。', '从 ProductionRun 正式阶段重新 materialize。'],
      confirmation: 'Bible 候选、脚本和阶段快照 hash 一致。',
      keywords: ['Bible', 'materialize', 'source fingerprint'],
    },
    {
      id: 'remote-uncertain',
      symptom: '远端提交结果标记 uncertain',
      likelyCause: '提交超时或连接中断，无法确定 provider 是否已扣费/生成。',
      actions: ['禁止普通重试。', '由管理员核对 provider request 证据。', '通过 ProductionRun resolve-uncertain 记录结论。'],
      confirmation: '结论有审计，且不会重复提交或丢失已存在结果。',
      keywords: ['uncertain', 'resolve-uncertain', '重复扣费'],
    },
    {
      id: 'artifact-archive-failed',
      symptom: '远端生成完成，但 managed artifact 不可用',
      likelyCause: '地址、大小、媒体类型、下载或存储校验失败。',
      actions: ['核对 attempt 和远端结果。', '检查归档错误与存储空间。', '修复根因后从 ProductionRun 重试当前阶段。'],
      confirmation: 'artifact 已写入受管存储并通过媒体/provenance 校验。',
      keywords: ['managed artifact', 'archive', 'media validation'],
    },
    {
      id: 'episode-master-incomplete',
      symptom: '某集 master 无法生成或 source items 不完整',
      likelyCause: '目标镜头 artifact 缺失、未选择或媒体校验失败。',
      actions: ['从 assembly 诊断读取 source items。', '回到 ProductionRun 修复缺失阶段或候选。', '重新验证该集 master。'],
      confirmation: '该集 master 可播放且 source items/provenance 完整。',
      keywords: ['episode master', 'source items', 'assembly'],
    },
    {
      id: 'release-bundle-invalid',
      symptom: 'ReleaseBundle verify 失败',
      likelyCause: '缺少目标 episode master、范围不一致或母版无效。',
      actions: ['核对 ProductionRun 目标集范围。', '逐集修复 master。', '再次执行 bundle verify。'],
      confirmation: '发布包包含全部逐集母版并验证通过。',
      keywords: ['ReleaseBundle', 'verify', '逐集'],
    },
    {
      id: 'config-pending-activation',
      symptom: '重启后 resolved config 仍不是新值',
      likelyCause: 'restart-required revision 仅处于 pending，尚未对新 boot 显式激活。',
      actions: ['确认当前 boot identity。', '调用正式 activation。', '重新读取 resolved config 与审计。'],
      confirmation: 'revision 在当前 boot 显示 active，新 run snapshot 固化新值。',
      keywords: ['pending', 'boot activation', 'resolved config'],
    },
    {
      id: 'recommendation-stale',
      symptom: '提交推荐决定时返回 recommendation_stale',
      likelyCause: 'host snapshot 已过期/失败，或 catalog、配置、deployment、workflow、smoke 与候选兼容性已经漂移。',
      actions: ['不要由前端强行沿用旧候选。', '重新 probe 目标 host。', '重新生成 recommendation 并提交新的 decide。'],
      confirmation: '服务端复验当前快照、目录、配置和候选兼容性后接受新决定。',
      keywords: ['recommendation_stale', 'probe', 'catalog hash', 'config hash'],
    },
    {
      id: 'recommendation-not-applied',
      symptom: '接受主机推荐后生产能力没有自动变化',
      likelyCause: 'recommendation 按设计只创建草案。',
      actions: ['审阅产生的 recipe/config draft。', '分别验证、发布和激活。', '重新 probe/readiness 后创建新 run。'],
      confirmation: '变化来自显式发布/激活，模型未被自动下载。',
      keywords: ['recommendation', 'draft', 'activate'],
    },
    {
      id: 'no-real-e2e-evidence',
      symptom: '自动化测试通过，但无法证明目标 GPU/provider 可生产',
      likelyCause: '测试使用 mock 或未覆盖真实授权、submit/poll/archive/assembly。',
      actions: ['在目标环境执行 provider 授权和 deployment probe。', '运行 workflow real smoke。', '完成 T2I/T2V、artifact、episode master、ReleaseBundle 与恢复演练。'],
      confirmation: '每个阶段都有目标环境的真实证据和关联 ID。',
      keywords: ['真实 E2E', 'GPU', 'provider', 'mock'],
    },
  ] as ManualIncident[],
}

export const searchableManualText = (step: ManualStep) => [
  step.title,
  step.summary,
  step.location,
  ...step.inputs.flatMap((input) => [input.label, input.example, input.note]),
  ...step.actions,
  ...step.success,
  step.next,
  ...(step.cautions ?? []),
  ...step.keywords,
].join(' ').toLocaleLowerCase('zh-CN')

export const searchableIncidentText = (incident: ManualIncident) => [
  incident.symptom,
  incident.likelyCause,
  ...incident.actions,
  incident.confirmation,
  ...incident.keywords,
].join(' ').toLocaleLowerCase('zh-CN')
