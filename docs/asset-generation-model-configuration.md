# 素材生成方案配置与验证

## 文档职责与当前状态

本文是风格素材库独立素材生成能力的正式配置与操作手册，适用于图片、视频、音频、剧本以及“文本模型生成提示词”。它说明系统为什么不展示生成方案、如何通过正式页面登记 Provider、模型版本、Deployment 和 Workflow、如何建立真实 probe/smoke 证据，以及如何在素材库验证生成、下载和短剧复用入口。

本文只描述当前代码主路径：

- 系统设置入口：`/settings`，仅 staff 可进入；
- 风格素材库入口：`/assets`，登录用户可以管理自己有权限的素材、创建本地入库或模型生成任务；任务状态与操作统一在“生产工作流” `/workflows?scope=asset` 查看；
- 配置事实：Deployment 方案使用 `ProviderProfile → ModelProfileVersion → ModelDeployment → ModelDeploymentCapabilitySnapshot`；ComfyUI 方案使用已导入、Live validate、真实 workflow smoke 并显式发布的 `WorkflowTemplateVersion` 及其精确 `validated_deployment`；
- 任务事实：提示词候选和剧本由 text-generation worker 执行，异步 API 图片/视频/音频由 async-media worker 执行，ComfyUI 图片/视频/音频由 generation worker 在共享 GPU 互斥门内执行；
- API 字段、枚举和状态码仍以根目录 `openapi.yaml` 为唯一机器契约；
- 本文中的域名、模型名、路径和费用均为结构示例，不代表已经接入某个真实供应商。

当前实现不会自动下载模型、自动创建凭据、自动发布模型或工作流，也不会把配置存在、历史 smoke 通过或前端下拉出现等同于真实生成成功。

## 1. 为什么素材库显示“没有可用方案”

素材库不会列出全部目录，而是通过 `GET /api/v1/asset-generation-jobs/options/` 针对任务类型和素材类型实时筛选安全生成方案。媒体素材有两类正式方案：受限异步 API Deployment 和已发布 ComfyUI Workflow；提示词候选与剧本只使用文本 Deployment。

Deployment 方案只有同时满足以下条件才会出现：

```text
Provider active
  + ModelProfileVersion published
  + Deployment active
  + 精确 capability / driver 合同匹配
  + 当前 Provider、模型、Deployment、凭据 target hash 未漂移
  + 对应 operation 有未过期且 execution_submitted=true 的 passed smoke
  + base_url 可判定为本地或外部地址
= 素材库可选 Deployment 方案
```

ComfyUI Workflow 方案只有同时满足以下条件才会出现：

```text
WorkflowTemplateVersion current + published
  + output kind 与 image/video/audio 精确匹配
  + Live validate 绑定当前 active ComfyUI Deployment
  + 版本 smoke_test_state=passed
  + smoke evidence 精确绑定 succeeded attempt、fixture、compiled graph 和 execution target
  + 当前用户有权执行模板
  + 主输出是该媒体类型受支持的持久 Save 节点
  + 可完整投影 workflow_input_schema
  + validated deployment 是 loopback，且服务可访问同一 COMFYUI_OUTPUT_DIR
= 素材库可选 ComfyUI 工作流
```

各下拉框使用的精确 operation 如下：

| 页面用途 | 任务类型 | 素材类型 | 必需 operation | 额外门禁 |
| --- | --- | --- | --- | --- |
| 文本模型生成提示词 | `prompt` | 任意类型 | `script.generate` | text/multimodal 模型，完整 `series_concept` 策略，受控 chat transport |
| 生成剧本素材 | `asset` | `script` | `script.generate` | 与提示词任务使用同一文本模型合同 |
| 生成图片素材 | `asset` | `image` | Deployment 为 `image.text_to_image`；Workflow 使用版本声明 operation | 可选 fresh deployment smoke 的 `async_rest` Deployment，或 output kind=image 且已完成 Live validate → real workflow smoke → publish 的 ComfyUI Workflow |
| 生成视频素材 | `asset` | `video` | Deployment 为 `video.text_to_video`；Workflow 使用版本声明 operation，例如 `video.reference_to_video` | 可选对应 `async_rest` Deployment 或 output kind=video 的合格 ComfyUI Workflow；参考输入不能降级为纯文本任务 |
| 生成音频素材 | `asset` | `audio` | Deployment 为 `audio.text_to_audio`；Workflow 使用版本声明 operation | 可选对应 `async_rest` Deployment 或 output kind=audio 的合格 ComfyUI Workflow |

特别注意：

- “提示词文本模型”当前检查 `script.generate`，只有 `prompt.normalize` smoke 不能让它出现。
- Deployment probe、Deployment operation smoke、Workflow Live validate 和 Workflow real smoke 是不同证据，不能互相替代。异步 API 方案读取 Deployment smoke；ComfyUI 方案读取 Workflow validation/smoke 证据。
- `options.routes[]` 以 `target_kind=deployment|comfy_workflow` 区分两类方案。创建任务时必须二选一提交 `model_deployment_id` 或 `workflow_template_version_id`；客户端不能自行拼配 Workflow 与 Deployment。
- ComfyUI 只用于 `job_type=asset` 的图片、视频和音频。提示词候选与剧本仍走 `script.generate` 文本 Deployment。
- 选项接口会排除所有不合格方案。空列表表示当前没有满足全部条件的候选，不表示前端应自动选一个较旧或不兼容目标。

### 1.1 空目录的判定顺序

空目录必须按方案类型分别定位，不能从 Provider 或历史任务存在推断可用：

1. 提示词或剧本为空：核对文本模型 `script.generate` 能力、受控策略、Deployment 生命周期和 fresh operation smoke。
2. 异步 API 媒体方案为空：核对 `async_rest` driver、精确媒体 operation、封闭请求/响应合同、Deployment target hash 和 fresh operation smoke。
3. ComfyUI 媒体方案为空：核对版本是否 current/published、Live validate 的精确 Deployment、真实 workflow smoke attempt/evidence、output kind、持久 Save 主输出、模板执行权限，以及 `workflow_input_schema` 是否能从当前版本的参数、固定槽和 conditioning 合同完整投影。存在 required 输入本身不是排除条件。
4. 方案曾出现后消失：重新读取 API，检查生命周期、凭据、graph、execution contract、validation evidence、fixture/compiled graph 或 target 是否漂移；不得使用浏览器旧缓存。

## 2. 权限、页面与服务前提

### 2.1 角色分工

| 角色 | 可以执行 | 不能执行 |
| --- | --- | --- |
| 超级管理员 | 创建、轮换和启停 SecretBinding；为 Provider/Deployment 选择受管凭据；执行 staff 配置操作 | 读取已保存的秘密正文 |
| 平台管理员（staff） | 管理 Provider、模型版本、Deployment、Workflow；执行 probe/smoke；处理不确定状态 | 创建或替换秘密绑定 |
| 普通登录用户 | 在素材库创建自己的生成任务，在生产工作流查看状态、取消或重试自己的可操作任务，下载有权限的素材 | 进入系统设置或修改平台目录；处理管理员消歧 |

外部 API 需要凭据时，超级管理员先完成 SecretBinding 和引用绑定，平台管理员再执行 probe/smoke。不要把 API Key、Token 或签名 URL 写入 Provider JSON、模型 specification、生成参数、普通文档或审计说明。

### 2.2 服务前提

日常启动和重启只使用：

```powershell
scripts/restart.ps1
```

该入口会启动 API、前端和 generation、text-generation、async-media、assembly、platform-maintenance worker。若 smoke 或生成任务长时间保持 `queued`，先检查对应 worker 是否运行及其日志；不要重复点击创建付费请求。

仓库没有独立的本地停止脚本，不得虚构 `stop.ps1` 或用临时 worker 命令作为业务日常入口。

## 3. 配置提示词与剧本文本模型

已有文本 Deployment 若只是 `script.generate` smoke 过期，最短恢复路径是重新建立该精确 operation 的真实 smoke：

1. 使用平台管理员账号进入“系统设置”。
2. 保持在“模型”分区，找到目标文本 Deployment。
3. 点击“烟测”。
4. 在 operation 中明确选择 `script.generate`。
5. 阅读真实调用和费用提示后，点击“确认并执行真实烟测”。
6. 页面先显示持久任务 `queued`，随后由 text-generation worker 领取并执行。
7. 等待任务终态为 `succeeded`，再点击“重新读取”核对新的 smoke 到期时间和证据。
8. 返回“风格素材库”，进入任意素材类型的“新增素材 → 模型生成”。

预期结果：

- “提示词文本模型”出现该文本 Deployment；
- 剧本素材的“素材生成方案”也可出现该 Deployment；
- 图片、视频或音频的“素材生成方案”独立判定；合格的异步 API Deployment 或 ComfyUI Workflow 均可出现。

真实 smoke 会向外部文本 Provider 提交低成本请求并可能计费。本文更新没有执行该操作，不能把上面的预期结果写成本次已通过事实。

### 3.1 新建文本模型时的规格要求

如果不是刷新现有 Deployment，而是登记新的提示词/剧本模型，必须使用实现受控 chat completion 的文本 Provider，并在模型完整规格中声明精确能力和 `series_concept`：

```json
{
  "schema_version": 1,
  "capabilities": ["script.generate"],
  "model_family": "example-text-family",
  "series_concept": {
    "minimum_shot_duration_ms": 1000,
    "maximum_shot_duration_ms": 10000,
    "max_output_tokens": 4096,
    "timeout_seconds": 180
  }
}
```

字段要求：

| 字段 | 要求 | 作用 |
| --- | --- | --- |
| `schema_version` | 必须为 `1` | 选择当前文本模型合同 |
| `capabilities` | 必须在此数组精确声明 `script.generate` | 旧 `operations`/`protocols` 别名不会兼容 |
| `model_family` | 提示词策略族 | 用于 PromptProfile 匹配；它不是模型记录跨版本的 `profile_key` |
| `minimum_shot_duration_ms` | 正整数 | 最小镜头时长门禁 |
| `maximum_shot_duration_ms` | 正整数且不小于最小值 | 最大镜头时长门禁 |
| `max_output_tokens` | 正整数 | 文本候选输出上限 |
| `timeout_seconds` | 0.5–3600 的数值 | 本次文本执行超时 |

如同一文本模型还用于正式短剧生产，可另外声明并分别配置 `script.structure`、`prompt.normalize`。这两个 operation 必须各自执行真实 smoke，但它们不能替代素材库所需的 `script.generate` smoke。

## 4. 接入图片、视频或音频 async REST Provider

受限异步 REST 是媒体素材生成的一类正式方案；另一类是第 10 节的 ComfyUI Workflow。接入异步 API 前必须从真实供应商取得并确认以下外部契约：

- API 根地址和健康检查路径；
- 创建任务、轮询、取消和收集产物路径；
- 提交响应中的 request ID 字段；
- 轮询响应中的状态字段及成功、失败、取消终态；
- 产物数组、URL、类型、ID 和可选 SHA-256 字段；
- 实际费用及币种字段；
- API 主机和所有可能的产物下载主机；
- 标准模型参数到远端单层字段的映射；
- 可确定的提交前计价方式。

供应商只提供同步响应、嵌套字段表达式、无法受限下载的任意 URL、无法幂等提交或无法识别终态时，不能用猜测配置成 `async_rest`。

### 4.1 配置全局主机白名单

在目标环境的 `backend/.env` 中，把媒体 API 主机和产物下载主机加入 `AI_PROVIDER_ALLOWED_HOSTS`。只填写规范化主机名，不填写 scheme、端口、路径、凭据或通配符：

```env
AI_PROVIDER_ALLOWED_HOSTS=localhost,127.0.0.1,::1,media-api.example.test,media-cdn.example.test
```

`example.test` 仅为文档占位符，实际配置必须替换为已审查的真实主机。环境变量修改后使用 `scripts/restart.ps1` 重启；Settings 页面不能在运行期替换这道部署级 SSRF 白名单。

### 4.2 创建受管凭据

需要 Bearer Token 的 Provider 由超级管理员执行：

1. 系统设置 →“凭据与审计”。
2. 点击“新增受管凭据”。
3. 填写唯一 key、显示名称、用途 `provider_credential`、秘密正文和可审计说明。
4. 提交后核对状态为 configured。

秘密正文只在创建或轮换请求中写入一次，后续页面不会回显。若 Provider 是受控本地且不需要鉴权，可以不绑定凭据；不能为消除“未配置”提示而保存假的 Token。

### 4.3 新建 Provider

进入系统设置 →“模型”，点击“管理服务连接”，展开后再点“新增服务连接”。常用字段如下：

| 页面字段 | 图片示例 | 必填/约束 | 影响 |
| --- | --- | --- | --- |
| 唯一 Key | `media-image-primary` | 小写稳定 key | 被模型、Deployment 和审计引用 |
| 显示名称 | `Media Image Primary` | 必填 | 仅用于识别 |
| 驱动类型 | `受限异步 REST` | 必须为 `async_rest` | 决定 submit/poll/collect 协议 |
| 支持模态 | `image` | 当前为声明字段，按注册驱动真实能力填写 | 用于展示与人工审核，不是执行能力门禁 |
| 受控操作 | `image.text_to_image` | 当前为声明字段，需与驱动和模型 capability 保持一致 | 用于展示与人工审核；烟测候选来自模型版本 capability |
| 启用 Provider | 开启 | 必须启用后才能进入新部署和任务路由 | 停用会保留历史但阻断新调用 |
| 受管凭据 | 选择已配置 SecretBinding | 外部需鉴权时选择 | 只保存引用，不保存明文 |
| 服务根地址 | `https://media-api.example.test` | 无凭据、query、fragment；主机已全局允许 | 所有远端调用根地址 |
| 请求超时 | `60` 秒 | 必填；服务端允许 0.5–600 | 单次 health/submit/poll/cancel/collect 网络等待上限 |
| 健康检查路径 | `/health` | 相对路径 | Deployment probe |
| 提交路径 | `/jobs` | 相对路径 | 创建异步任务 |
| 查询路径 | `/jobs/{request_id}` | 仅一个占位符 | poll 远端状态 |
| 取消路径 | `/jobs/{request_id}/cancel` | 仅一个占位符 | 请求远端取消 |
| 收集路径 | `/jobs/{request_id}` | 仅一个占位符 | 获取最终 manifest |
| 请求 ID 字段 | `id` | 单层 JSON 字段名 | 冻结远端任务身份 |
| 状态字段 | `status` | 单层 JSON 字段名 | 判断远端状态 |
| 成功终态 | `succeeded` | 1–32 个且与其他终态不重叠 | 进入 collect |
| 失败终态 | `failed` | 同上 | 形成明确失败 |
| 取消终态 | `cancelled` | 同上 | 形成明确取消 |
| 产物列表字段 | `artifacts` | 单层 JSON 字段名 | 读取产物数组 |
| 产物 URL 字段 | `url` | 单层 JSON 字段名 | 受控下载 |
| 产物类型字段 | `kind` | 单层 JSON 字段名 | 应返回 image/video/audio |
| 产物 ID 字段 | `id` | 单层 JSON 字段名 | 远端身份审计 |
| 产物 SHA-256 字段 | `sha256` | 单层 JSON 字段名 | 若供应商提供则核对完整性 |
| 实际费用字段 | `actual_cost` | 单层 JSON 字段名 | 费用事实 |
| 费用币种字段 | `currency` | 单层 JSON 字段名 | 与实际费用配对 |
| 产物主机白名单 | `media-cdn.example.test` | 小写主机且是全局白名单子集 | 阻止任意 URL 下载 |
| 请求字段映射 | `model→model, prompt→prompt` | model、prompt 必填；目标名唯一 | 把标准语义映射到远端请求 |
| 幂等请求头 | `Idempotency-Key` | 合法 HTTP header 名 | 防止不确定提交后重复扣费 |
| 轮询间隔 | `2` 秒 | 必填；0.25–300 | 外部压力和刷新延迟 |
| 最长轮询时间 | `3600` 秒 | 必填；10–604800 的整数 | 生产任务创建时冻结的总等待硬边界 |
| 进度字段 | `progress` | 可选单层 JSON 字段名；无可信进度时留空 | 只记录远端真实进度 |
| 进度满值 | `100` | 配置进度字段时必须为有限正数 | 把远端数值归一化为比例，不改变终态 |
| 能力快照有效期 | `3600` 秒 | 60–604800 | smoke 多久后必须刷新 |
| 估算币种 | `USD` | 3–12 位大写标识 | 预算门禁 |
| 单次固定费用 | `0.010000` | 非负数；与币种同时填写 | 提交前可确定成本 |

> Provider 驱动、“支持模态 / 声明操作”都从 `GET /api/v1/model-profile-versions/options/` 构造受控选择；目录不可用时新增与编辑失败关闭，不回退任意文本。这些字段仍是说明、展示与人工审核用的运营标记；实际可执行性以注册驱动 capabilities、已发布模型版本 `specification.capabilities`、Deployment/Workflow 合同和新鲜真实 Smoke 证据共同证明。

`timeout_seconds`、`poll_interval_seconds` 与 `max_poll_seconds` 是同一组有限运行预算，必须显式保存。生产任务会冻结 `max_poll_seconds`，后续 worker 不按新配置改写既有任务。真实 Smoke 另受平台 60 秒总轮询、2 秒最大轮询间隔的安全包络限制；这只约束验证作业，不会把生产预算静默改成 60 秒。

页面类型化字段等价的结构示例如下。不要原样使用示例域名、状态或费用；必须按真实供应商合同逐项核对：

```json
{
  "base_url": "https://media-api.example.test",
  "health_path": "/health",
  "submit_path": "/jobs",
  "poll_path": "/jobs/{request_id}",
  "cancel_path": "/jobs/{request_id}/cancel",
  "collect_path": "/jobs/{request_id}",
  "timeout_seconds": 60,
  "request_id_field": "id",
  "status_field": "status",
  "terminal_success": ["succeeded"],
  "terminal_failure": ["failed"],
  "terminal_cancelled": ["cancelled"],
  "artifacts_field": "artifacts",
  "artifact_url_field": "url",
  "artifact_kind_field": "kind",
  "artifact_id_field": "id",
  "artifact_sha256_field": "sha256",
  "actual_cost_field": "actual_cost",
  "currency_field": "currency",
  "artifact_allowed_hosts": ["media-cdn.example.test"],
  "request_field_map": {
    "model": "model",
    "prompt": "prompt",
    "negative_prompt": "negative_prompt",
    "width": "width",
    "height": "height",
    "duration_seconds": "duration_seconds",
    "fps": "fps",
    "seed": "seed"
  },
  "idempotency_header": "Idempotency-Key",
  "poll_interval_seconds": 2,
  "max_poll_seconds": 3600,
  "progress_field": "progress",
  "progress_max": 100,
  "capability_ttl_seconds": 3600,
  "pricing": {
    "currency": "USD",
    "fixed_per_request": "0.010000"
  }
}
```

按上述字段配置时，远端响应至少要能投影为以下结构：

```json
{
  "submit": {"id": "remote-request-id"},
  "poll": {"status": "succeeded", "progress": 100},
  "collect": {
    "status": "succeeded",
    "artifacts": [
      {
        "id": "remote-artifact-id",
        "kind": "image",
        "url": "https://media-cdn.example.test/files/result.png",
        "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      }
    ],
    "actual_cost": "0.010000",
    "currency": "USD"
  }
}
```

字段值可以不同，但必须通过 Provider 的单层字段配置准确映射。平台不会执行任意 JSONPath、脚本转换或不受控响应猜测。

### 4.4 新建并发布媒体模型版本

点击页面主按钮“新建模型”，填写新的稳定模型版本族 key 和真实 `model_identifier`，并从服务端目录选择 Provider、主模态、能力列表与提示词策略族。首次版本固定由服务端分配为 `v1`，`specification.schema_version` 也由服务端维护，页面不接受人工版本号。模型规格发布后不可原地修改；只有当前最高版本为 published/retired 时才能从对应模型卡片点击“新建下一版本”。服务端会先校验源合同的大小与 SHA-256，再锁定模型版本族并按该族全部历史（包括 retired）原子分配下一号。若最高版本已是草稿，页面要求继续编辑该草稿；并发或旧源请求会返回冲突，不依赖浏览器计算 `max+1`。

模型版本族 key（`profile_key`）与提示词策略族（`specification.model_family`）是两个概念：前者是模型记录跨版本身份，后者用于 PromptProfile 匹配。策略族选择器以 `options.model_families` 为已登记目录；新模型可在选择器中显式选择由合法 Profile Key 派生的同名新策略族，除此之外不接受自定义文本。能力列表、主模态、策略族、Provider 驱动、Provider 模态/传输操作以及 Artifact 角色都来自 `GET /api/v1/model-profile-versions/options/`；目录读取失败时正式页面失败关闭，不回退自由文本。能力选择只是版本合同声明，仍不能替代发布、Deployment/Workflow 合同与真实 Smoke。

> 受控目录必须提供 `text`、`image`、`video`、`audio`、`multimodal` 五个正式主模态选项；音频模型必须选择 `audio`。任一选项缺失都视为服务端目录或前端合同不完整，页面应阻断新增/编辑，不得通过数据库或临时 CLI 旁路创建。

以下是图片 `image.text_to_image` 的完整结构示例：

```json
{
  "schema_version": 1,
  "capabilities": ["image.text_to_image"],
  "model_family": "example-image-family",
  "async_media": {
    "schema_version": 1,
    "operations": {
      "image.text_to_image": {
        "output_kind": "image",
        "request_schema": {
          "type": "object",
          "additionalProperties": false,
          "required": ["model", "prompt", "width", "height"],
          "properties": {
            "model": {"type": "string", "minLength": 1, "maxLength": 255},
            "prompt": {"type": "string", "minLength": 1, "maxLength": 60000},
            "negative_prompt": {"type": "string", "maxLength": 60000},
            "width": {"type": "integer", "minimum": 64, "maximum": 4096},
            "height": {"type": "integer", "minimum": 64, "maximum": 4096},
            "seed": {"type": "integer", "minimum": 0}
          }
        },
        "smoke_inputs": {
          "model": "media-model-v1",
          "prompt": "smoke test image",
          "width": 64,
          "height": 64
        }
      }
    }
  }
}
```

`smoke_inputs.model` 应与本模型版本真实 `model_identifier` 一致，并选择供应商允许的最低成本有效参数。不要填入假模型名来换取页面发布成功。

视频和音频使用同一封闭合同结构，但 operation、输出和典型参数不同：

| 类型 | operation | `output_kind` | 常见属性 | smoke 要求 |
| --- | --- | --- | --- | --- |
| 图片 | `image.text_to_image` | `image` | model、prompt、negative_prompt、width、height、seed | 低分辨率但必须返回真实可探测图片 |
| 视频 | `video.text_to_video` | `video` | 图片字段加 duration_seconds、fps | 低时长/低分辨率但必须返回真实可探测视频 |
| 音频 | `audio.text_to_audio` | `audio` | model、prompt、duration_seconds、seed | 最短允许时长但必须返回真实可探测音频 |

`request_schema` 只允许标准字段：`model`、`prompt`、`negative_prompt`、`width`、`height`、`duration_seconds`、`fps`、`seed`、`audio`。每个必填字段都必须在 `properties` 和 `smoke_inputs` 中出现；不能允许额外字段。

`prompt.maxLength` 必须由这个已发布模型版本明确声明，且必须是 1–60000 的整数。60000 是平台集中维护的绝对请求文本包络，不代表每个模型都具备该容量；真实模型更小的限制应按供应商能力填写，并随 ModelProfileVersion 冻结。前端不另设同值 `maxlength` 作为第二套能力来源。

保存草稿后，在模型卡片的草稿版本点击“核对并发布”，选择稳定审计原因代码并确认。远端模型没有本地文件时，“文件与证明”应保持明确的 N/A 状态，不能伪造 artifact。

### 4.5 创建运行实例（Deployment）

展开刚发布版本的“运行实例（Model Deployment）”，点击“新增运行实例”。模型版本会被精确预选，然后填写：

- 稳定 Deployment key 和显示名称；
- 刚发布的模型版本；
- 远端服务选择“不绑定本地主机”；本地受控服务只绑定已 probe 为 active 的 RuntimeHost；
- priority 和是否默认；
- 状态 `active`；
- 仅在该 Deployment 与 Provider 不同时填写 `runtime_config` 覆盖；空对象表示继承 Provider；
- 如需使用与 Provider 不同的受管凭据，由超级管理员在 Deployment 上选择。

运行时覆盖和凭据变化都会改变 Deployment target hash，使旧 probe/smoke 证据失效。不要为了保留旧 smoke 而建立重复 Deployment 或复制旧快照。

### 4.6 本地模型路径调整、删除与退役

模型卡片中具体能力版本的“文件与证明”记录的是受控 models 根下的相对文件名、库存目录、预期大小和 SHA-256，不是磁盘文件管理器。编辑或删除注册记录都不会移动、改名或删除真实文件。

- 如果只移动整套 ComfyUI，且 `models/<inventory_folder>/<filename>` 与文件内容保持不变，更新 `COMFYUI_OUTPUT_DIR`/受控端点后通过 `scripts/restart.ps1` 重启，再执行 RuntimeHost probe、artifact verify 和受 target 漂移影响的 smoke/Workflow 验证；无需复制 artifact。
- 如果相对位置或文件内容变化，draft artifact 可编辑，保存后旧核验会自动失效并回到 `declared`；published/retired 版本必须新建更高 ModelProfileVersion 和 artifact，不能覆盖旧证据。
- 只有从未核验、没有任何主机观测和引用的 draft artifact 声明可删除。Deployment 先停用；无证据的 disabled 空记录可删除，有历史证据的记录在替换活动配方/current Workflow 后退役保留。

完整迁移顺序和故障判定见 [运维文档：模型文件路径调整与注册表清理](operations.md#模型文件路径调整与注册表清理)。

## 5. Deployment probe、operation smoke 与候选生效

### 5.1 执行 Deployment probe

对于 active `async_rest` 运行实例，展开所属模型版本的“运行实例”，在该实例“更多”中选择“探测（Probe）”：

1. 点击“探测”。
2. 平台向配置的 health path 发起受控 GET。
3. 查看页面返回的 capability snapshot，而不是只看按钮 Toast。
4. 失败时先修复域名白名单、DNS、TLS、凭据、路径或远端健康状态。

Probe 证明目标端点当时可达，但不提交生成，不证明产物合同正确，也不让素材模型自动进入下拉框。

### 5.2 执行精确 operation smoke

1. 在同一运行实例行点击“真实烟测（Smoke）”。
2. operation 必须选择当前类型的精确值，例如 `image.text_to_image`。
3. 阅读费用说明并点击“确认并执行真实烟测”。
4. API 先创建持久 `ModelDeploymentSmokeAttempt` 并返回 `202`；浏览器不直接占用一个长请求等待远端完成。
5. async-media worker 领取任务后执行一次 submit，再 poll 和 collect。
6. 等待任务终态 `succeeded`，点击“详情与证据”核对 operation、target、到期时间、请求/响应 hash 和成本事实。

一个模型声明多个 operation 时必须逐项 smoke。例如图片 passed 不能证明视频，`script.generate` passed 不能证明 `prompt.normalize`。

### 5.3 判定模型已进入素材库

Smoke 成功后：

1. 点击系统设置右上角“重新读取”。
2. 确认 Deployment 显示 fresh smoke，而不是只显示历史 `succeeded` attempt。
3. 进入 `/assets`。
4. 切换到对应图片、视频、音频或剧本类型。
5. 点击“新增素材”，选择“模型生成”。
6. “素材生成方案”中的异步 API 条目应显示 Deployment 名称、operation、外部标记和 smoke 有效期；ComfyUI 条目按第 10 节的 Workflow 证据判定，并携带该不可变版本的 `workflow_input_schema`。

若页面仍为空，按第 9 节逐项排查。不要使用浏览器缓存、数据库直接插入或本地常量绕过服务端选项目录。

### 5.4 素材库与生产工作流页面分工

- 素材库顶部的“风格、素材类型、图片子类、搜索、刷新、排序、生成任务、新增素材”组成固定操作区。滚动素材列表时该区域保持在页面内容顶部；风格过多时单行横向滚动，不再换成多层大卡片。
- 素材库只负责检索、预览、下载、本地入库和创建生成任务，不再展示第二份任务历史。
- 点击“生成任务”，或提交提示词/素材生成任务后，会进入 `/workflows?scope=asset&task=<job-id>`。生产工作流页以文字标签区分“素材任务”“短剧任务”和“独立媒体”，并保留各自的真实状态语义。
- 短剧 Task 详情仍为只读诊断，写操作回所属 ProductionRun；素材 AssetGenerationJob 可在其详情中取消、对失败或已取消任务重试，并由 staff 对 `uncertain` 做人工消歧。
- 成功提示词任务点击“采用并继续生成”后，系统以 `promptJob` 标识返回素材库并重新打开对应类型的模型生成表单；URL 不携带长提示词正文。成功素材任务可从工作流详情返回素材库查看，或直接使用新的受管下载地址下载。

## 6. 在素材库执行一次真实生成验证

只有在用户明确同意真实外部调用及可能费用后执行：

1. 进入对应素材类型的“新增素材 → 模型生成”。
2. 填写名称、基础描述、创作需求和最终提示词。
3. 如需文本模型扩写，先选择“提示词文本模型”并创建独立提示词任务；页面会跳转到生产工作流。任务成功后人工核对并点击“采用并继续生成”返回素材库，系统不会自动串联素材扣费。
4. 选择本类型“素材生成方案”，确认标签是“异步 API”还是“ComfyUI”，并核对其 Deployment、精确 operation 与 smoke 证据。参考生视频 Workflow 应显示自身声明的 `video.reference_to_video` 等 operation，不应被改写为 `video.text_to_video`。
5. 只填写方案公开的可编辑输入。异步 API 参数必须符合封闭 `request_schema`；ComfyUI 页面按 `workflow_input_schema` 动态展示全部 string/integer/number/boolean/enum 标量、每个固定图片/视频/音频槽，以及按 `CONDITIONING_MODE` 切换的动态多图、多视频、音频用途和内嵌/外部音轨配对选项。多个 conditioning mode 时必须明确选择，单一 mode 可使用版本给出的唯一值。
6. ComfyUI 创建体统一为 `workflow_inputs={schema_version:2,values,assets,references}`：`values` 保存公开标量与 `CONDITIONING_MODE`，`assets` 只保存 `{semantic_key,asset_id}`，`references` 只保存图片/视频/音频 Asset ID、用途和音轨配对意图。不得提交本地路径、checksum、上传名、ComfyUI 节点值或高级 JSON。提示词、模型、输出前缀和 `generation.batch_size=1` 由服务端注入。
7. 只要所选文本或媒体模型是外部地址，勾选允许外发，并填写不低于本次估算费用的预算上限。
8. 点击“创建生成任务”。服务端会在排队前重新读取 owner/global Asset，校验 required 输入、active/MIME/受管路径/SHA-256、数量、用途、互斥与音轨配对，并复用短剧的 semantic compiler、staging 和 conditioning/preprocessing adapter；任一不满足都会明确失败，不会降级丢弃参考素材。
9. 页面会跳转到“生产工作流”的素材任务详情；在该页观察 `queued → submitting → submitted/running → succeeded`。`uncertain` 必须保持为“待核对”，由管理员核对远端终态与账单后消歧；不要用 Toast 或浏览器等待时间判断成功。
10. 成功后打开新 Asset，核对素材类型、checksum、来源任务和预览。
11. 点击下载前让页面重新读取 Asset 详情，使用新的短期签名 `download_url` 下载受管文件。
12. 在短剧制作正式选择器中核对图片、视频或音频可被选择；剧本先导入目标项目形成 preview revision，再人工确认。

验收结果必须分别记录：

- 所选方案是 Deployment 还是 ComfyUI Workflow；
- Deployment 方案的 probe 与精确 operation smoke 是否 passed/fresh，或 Workflow 方案的 Live validate、精确真实 smoke 和 published/current 状态是否成立；
- AssetGenerationJob 是否 succeeded；
- 执行产物是否已受限下载或从受控共享目录读取、探测、校验并物化为 Asset；
- 下载是否使用新签名地址；
- 对应短剧入口是否能读取该 Asset。

若尚未进行现场验收，不得把自动化测试、已有其他 Workflow 产物或历史截图登记为本次通过。已执行的现场结果必须追加到第 11.2 节，并保留失败、重试与费用事实。

## 7. 费用、外发与安全确认

- Provider 或 Deployment 必须提供可确定 pricing；ComfyUI 本地方案也必须由 driver 在任务创建前返回确定估算。无法估算时，真实 smoke 或素材任务应被阻断。
- 外部素材任务必须逐次提交 `allow_external_calls=true` 和预算上限；平台不会从上一次任务沿用授权。
- 预算是本次上限，不是供应商账单替代品；最终以持久 actual cost 事实和 Provider 账单对账。
- `base_url` 不得包含凭据、query 或 fragment；API 和产物主机必须在部署级全局白名单中。
- `artifact_allowed_hosts` 必须是全局白名单的规范化子集。重定向或返回未允许主机时，下载必须失败。
- SecretBinding 明文只允许超级管理员 write-only 创建或轮换；浏览器、普通 API、审计和本文均不回显。
- Idempotency-Key 由持久任务冻结。网络响应丢失时先刷新同一任务，不创建“看起来一样”的第二个付费请求。
- 产物进入本地前必须经过大小、类型、媒体探测和可选远端 SHA-256 校验；远端 URL 本身不等于受管 Asset。
- ComfyUI 方案只接受 loopback Deployment 与受控共享 `COMFYUI_OUTPUT_DIR`；远程 ComfyUI 在没有正式受限下载合同时不会进入素材生成目录。

## 8. Smoke 过期、target 漂移和 uncertain 恢复

### 8.1 Smoke 过期

`capability_ttl_seconds` 默认 3600 秒，允许范围为 60–604800 秒。到期后：

- 历史 attempt 和 snapshot 保留；
- 素材选项目录立即排除该 Deployment；
- 新任务不能回退使用过期证据；
- 管理员必须重新执行对应 operation smoke。

延长 TTL 只是延长证据使用窗口，不能替代真实复验。应依据供应商稳定性、模型更新频率和费用风险设置，而不是固定设为最大值。

### 8.2 Target 漂移

以下变化会使旧 smoke 与当前目标不一致：

- Provider driver config 改变；
- Deployment runtime config 改变；
- 受管凭据绑定或 SecretVersion 改变；
- 模型 specification 或 model identifier 变更；
- Provider/Deployment 生命周期不再 active。

变更后重新 probe 和 smoke。已发布模型 specification 不可修改，应创建新模型版本和新 Deployment，不得搬运旧 snapshot。

### 8.3 Deployment smoke 为 uncertain

`uncertain` 表示平台无法确认一次真实 submit 是否到达远端，不表示“可以再试一次”：

1. 不要创建同 operation 的新 smoke。
2. 管理员核对 Provider 控制台、远端 request、终态和账单时间范围。
3. 在系统设置的 Deployment 行点击“管理员消歧”。
4. 选择“确认未提交”“确认远端已提交”或“确认远端失败”，填写至少 8 个字符的可复核说明；需要时提交 request ID 和实际费用/币种。
5. 提交只追加不可变结论，不会把旧 attempt 改成 passed。
6. 消歧完成后必须创建新的真实 smoke，只有新 attempt 成功才形成可用证据。

### 8.4 Workflow smoke 为 uncertain

Workflow smoke 与 Deployment smoke 使用不同的 attempt/action：

1. 未消歧前，同一 workflow/deployment 的任何 fixture 都禁止创建新 smoke。
2. staff 核对 ComfyUI queue/history、稳定 submission token 和终态后，通过 `WorkflowSmokeAttempt.resolve-uncertain` 追加不可变结论。
3. 人工确认成功也不会把旧 attempt 或版本标成 passed；必须创建新的真实 attempt，重新收集并验证 fixture、compiled graph、target 和输出 evidence，再显式 publish。

### 8.5 AssetGenerationJob 为 uncertain

素材任务的 `uncertain` 在“生产工作流” `/workflows?scope=asset` 的素材任务详情中处理：

- 普通用户不能直接重试；
- staff 核对 Provider request ID、终态和账单后提交 `resolve-uncertain`；
- ComfyUI 使用任务 UUID 作为冻结 submission token；maintenance/recovery 可先以该稳定 token 查询原请求，但“队列与历史均不存在”不能证明未提交，仍需 staff 证据；
- 确认远端成功的媒体任务只恢复原 request 的 collect，不重新 submit；
- 费用证据冲突会再次阻断，不会静默覆盖；
- 如果任务已经记录取消意图，晚到成功只恢复费用对账，产物不会进入素材库。

明确 `failed` 或 `cancelled` 后，用户才能创建新的重试任务。历史 ComfyUI 素材任务的请求快照 schema version 小于 3 时没有完整 `workflow_inputs` 冻结意图，服务端会明确拒绝自动 retry；应返回素材库，从当前发布 Workflow 重新选择输入并创建任务，不能猜测旧参数或引用素材。

## 9. 常见“没有可用方案”排查表

| 现象 | 核对位置 | 原因 | 正确处理 |
| --- | --- | --- | --- |
| 提示词模型为空 | 模型详情、Deployment smoke | 没有 `script.generate`、策略不完整或 smoke 过期 | 修复文本模型规格并重新执行 `script.generate` smoke |
| 剧本模型为空 | 同上 | 剧本也使用 `script.generate` | 不要用 `prompt.normalize` 证据替代 |
| ComfyUI Workflow 不出现 | Workflow 版本、validation/smoke evidence、素材 options | 版本非 current/published、output kind 不匹配、证据漂移、主输出不是受支持 Save 节点、用户无执行权限，或当前版本无法完整投影 `workflow_input_schema` | 按第 10 节从精确版本重新 validate、real smoke、publish 并修复输入合同；required 素材槽本身不是排除条件，不要补 Deployment smoke 冒充 Workflow 证据 |
| 远程 ComfyUI Workflow 不出现 | validated deployment 与输出合同 | 当前 workflow-backed AssetGenerationJob 只允许 loopback 且共享受控输出目录 | 使用同机受控 ComfyUI，或先实现并审核远程产物下载合同 |
| async REST 模型不出现 | ModelProfile 状态 | 模型仍是 draft/retired | 审核规格后显式发布新版本 |
| Deployment 不出现 | 生命周期 | Provider 或 Deployment 非 active | 修复真实状态；不要只改页面展示 |
| Smoke succeeded 仍不出现 | snapshot 详情 | 快照已过期或 target hash 漂移 | 以当前目标重新 probe/smoke |
| Provider 保存失败 | Provider 表单、环境变量 | API/CDN 主机不在全局白名单、路径或字段不合法 | 修正部署白名单并重启，再按真实合同填写 |
| Smoke 或生成一直 queued | worker 状态和日志 | text-generation、async-media 或 generation worker 未运行；ComfyUI GPU 互斥门仍被 workflow smoke/ProductionRun/其他 Comfy 素材任务占用 | 使用正式 restart 入口检查服务，等待同一持久任务；不要重复创建 |
| Smoke failed | attempt 详情、Provider 日志 | 鉴权、路径、状态字段、schema、费用或产物合同错误 | 修复根因后创建新 smoke，不篡改旧证据 |
| 素材任务提示预算错误 | Provider pricing、任务表单 | 费率未知、币种缺失或预算低于估算 | 配置确定 pricing，并明确授权足够预算 |
| 只有 Deployment smoke passed | 方案类型 | Deployment smoke 不能让 ComfyUI Workflow 方案生效 | 对 Workflow 执行 Live validate → real workflow smoke → publish |
| 只有 Workflow smoke passed | 工作流版本详情 | 尚未 published/current，或 evidence 未精确绑定当前 fixture/compiled graph/target | 修复版本门禁并显式发布；不要把历史 passed 文案当作当前 evidence |

当前选项 API 不公开每个被排除 Deployment/Workflow 的详细原因，管理员排障必须以上述服务端事实为准，不能由普通用户从空下拉猜测凭据或内部地址。

## 10. ComfyUI workflow-backed AssetGenerationJob

ComfyUI Workflow 可以作为素材库图片、视频或音频的正式生成方案。上线顺序必须完整执行：

1. 确认平台存在 active ComfyUI Provider、active RuntimeHost，以及绑定该主机的 active Deployment；扫描时无需人工选择来源 Deployment；
2. 从 Settings 扫描系统配置 `workflow.comfyui_saved_workflow_directory` 指定的 ComfyUI userdata 相对目录，不接受本地 JSON 上传或粘贴；服务端按 Provider、RuntimeHost、规范化 endpoint 与有效目录去重，每个来源作用域只扫描一次；
3. 对 `matched` 行复核服务端自动匹配结果；`ambiguous` 只从服务端候选补选，`unmatched/blocked` 按行内动作登记或证明模型、恢复实例、修复来源后重新扫描；
4. preview、模型证明和 import 必须回传 discover 的来源作用域版本/hash；作用域漂移时丢弃旧行、重新全局扫描，再导入不可变结构合同；
5. 在工作流版本验收页执行 Live validate，得到绑定精确 Deployment、graph、execution contract、模型依赖和 target hash 的 validation evidence；
6. 为本次 `WorkflowSmokeAttempt` 选择参数与可选 Asset fixture，执行真实 workflow smoke；
7. 核对 succeeded attempt、fixture hash、compiled graph hash、唯一主输出与真实媒体后显式 publish，并确保该版本为 current；
8. 返回 `/assets`，对应媒体类型的“素材生成方案”会以 `target_kind=comfy_workflow` 展示该版本。

完整操作见 [ComfyUI 已保存工作流导入、验证与发布手册](comfyui-saved-workflow-validation.md)。

此管理入口的调整不创建或修改 `AssetGenerationJob`、`ProductionRun`，也不提交 prompt。两条运行链仍在各自任务创建时按 `workflow_input_schema` 冻结 `values/assets/references` 与已经校验的 Deployment。

工作流版本验收页的 smoke fixture 与素材库的 `workflow_inputs` 使用同一动态输入编辑器和公共校验内核，因此目录导入识别出的提示词、标量参数、多个固定 LoadImage/LoadVideo/LoadAudio 节点，以及注册 projector 生成的 conditioning mode 与动态图片/视频/音频，会从版本 Schema 自动展示相应控件，不需要为每个模型再写一套表单。两条路径仍保持事实边界：smoke 冻结验收 fixture，可使用 staff 技术素材和 smoke 推荐值；素材生成只读取当前用户/全局的普通受管 Asset，不读取 smoke-only 默认值，并建立自己的 AssetGenerationJob 输入快照。

素材库中的两类媒体方案共享 `AssetGenerationJob` 状态机，但证据和执行边界不同：

| 能力 | 异步 API Deployment | ComfyUI Workflow |
| --- | --- | --- |
| options 身份 | `target_kind=deployment` + `target_id=<deployment UUID>`；创建时提交 `model_deployment_id` | `target_kind=comfy_workflow` + `target_id=<workflow version UUID>`；创建时提交 `workflow_template_version_id`，服务端解析精确 validated Deployment |
| 必需证据 | fresh Deployment operation smoke | current/published + Live validation + succeeded real Workflow smoke 的精确 attempt/fixture/compiled/target evidence |
| 参数来源 | 封闭 async `request_schema` | `workflow_input_schema` 完整投影公开标量、固定槽和按 `CONDITIONING_MODE` 切换的动态引用；提示词、模型、输出前缀和批量大小不开放覆盖 |
| 参考素材 | 依赖正式 provider upload contract | 通过 `workflow_inputs.assets/references` 提交 Asset ID、用途和音轨配对意图；服务端复用 owner/global 授权、semantic compiler、staging 与 preprocessing，不接受路径或节点值 |
| 执行 worker | async-media worker | generation worker；与 Workflow smoke、ProductionRun Comfy task 和其他 Comfy 素材任务共享数据库 GPU 互斥门 |
| 产物进入平台 | 受限 URL 下载、校验后物化 Asset | 只从共享 `COMFYUI_OUTPUT_DIR` 读取冻结输出前缀下的唯一主输出，校验后复制为 Asset |
| 支持范围 | image/video/audio | image/video/audio；剧本和提示词候选不支持 |

Workflow 主输出必须是受支持的持久节点：图片 `SaveImage`/`SaveImageAdvanced`，视频 `SaveVideo`/`VHS_VideoCombine`，音频 `SaveAudioAdvanced`。任务创建时服务端冻结 workflow version、Deployment、validation/smoke/target hash、规范化输入请求、Asset facts、编译后的 bindings、conditioning mode、preprocessing/profile hash、编译图、主输出、每任务输出前缀和以任务 UUID 表示的 submission token；执行前再次复验，不允许客户端改绑 Deployment。素材任务不需要 ProductionRecipe；它只复用 ProductionRun 已使用的公共输入验证、编译和媒体准备能力，并保持独立任务事实。

ComfyUI 成功只在队列/历史返回冻结主输出、文件位于受控目录、路径前缀匹配、媒体类型/大小/checksum/FFprobe 校验通过并物化 Asset 后成立。产物下载和短剧复用与异步 API Asset 使用同一正式入口；Workflow 本身不会为素材任务创建 Episode、Shot、ProductionRun、装配或交付。

## 11. 现场验证与记录

本文记录当前代码与操作合同，不把文档更新、单元测试、历史 workflow smoke 或目录中出现方案写成某个真实案例已经成功。现场验收必须以数据库任务、费用证据、受管 Asset 和页面复用结果为准。

### 11.1 现场验收记录

实际接入后按下表记录，不要用“已配置”替代真实结果：

| 项目 | 记录 |
| --- | --- |
| 执行方案 | `deployment` 或 `comfy_workflow`；稳定 target ID 与不可变版本 |
| 精确 operation | Deployment 记录 `script.generate` / `image.text_to_image` / `video.text_to_video` / `audio.text_to_audio`；Workflow 记录版本声明 operation，例如 `video.reference_to_video` |
| Workflow 输入合同 | `CONDITIONING_MODE`、公开标量、每个 semantic key 对应的 Asset ID/用途/配对意图，以及规范化输入、binding、conditioning 与 preprocessing hash；不记录本地路径 |
| 配置门禁 | Deployment lifecycle/probe，或 Workflow import/validate/publish/current 状态 |
| Smoke attempt | Deployment 或 Workflow attempt ID、fixture/target/compiled hash、终态、有效期（如适用）、费用 |
| AssetGenerationJob | ID、终态、估算/实际费用、是否发生人工消歧 |
| 产物 | Asset ID、媒体类型、大小、checksum、探测结果 |
| 下载 | 是否重新读取签名 URL、附件是否完整 |
| 短剧复用 | 使用页面、项目、对应选择器和确认结果 |
| 未验证项 | 环境限制、负责人和后续动作 |

只有对应方案的配置门禁、精确真实 smoke、素材任务、产物入库、下载及目标短剧入口均得到现场证据后，才能报告该图片、视频、音频或剧本案例闭环已通过。

### 11.2 2026-08-25 本机现场记录

以下记录来自正式素材库 API、ComfyUI 受控输出目录和受管下载接口；没有将浏览器 Toast、目录扫描或本地文件路径作为成功依据。

| 类型 | 方案与门禁 | 真实任务与产物 | 下载与短剧入口核对 |
| --- | --- | --- | --- |
| 图片 | ComfyUI Workflow `62f0fe23-bb6e-4ff0-b6fa-2fbd4fc06fe9`；已发布/current，成功 smoke `38f034a2-3e37-4fe9-9680-befadd787c38` | Job `eff472a1-9a23-47b6-98eb-f54abdabd037` succeeded；Asset `2dabfe46-7b73-4184-a024-0b27ec726e2f`，`scene`，PNG，101888 bytes | 新签名下载 HTTP 200，字节数和 SHA-256 一致；存在于 active `scene` 列表，供 Storyboard 首/尾帧素材选择器读取。 |
| 视频 | ComfyUI H3 Workflow `85734ebd-4393-414c-8e2f-4550b2dc1d28`；Live validate、已发布/current，成功 smoke `aa66f353-6b34-4ae7-a924-afe10d4bce64` | Job `87d5b69f-c5fa-4dcd-870b-3790d159ab63` succeeded；Asset `8871d315-6bf2-4828-843e-c0d4552b55ce`，`reference_video`，MP4，532097 bytes，5167 ms | 新签名下载 HTTP 200，字节数和 SHA-256 一致；在 2–15 秒约束内，存在于 ProductionRun/Storyboard 参考视频选择器数据源。 |
| 音频 | ComfyUI H3 Workflow `5a938723-75f8-41ad-a60b-cb104fcc2f12`；Live validate、已发布/current，成功 smoke `30ee3c85-0aa1-4fd1-9d22-b4ee00d15b73` | 首次 17 帧任务 `0d7e806d-ca7d-43f2-a726-6017c226cce6` 的远端产物不足 2 秒，被本地合同明确拒绝；51 帧重试 Job `3e5e4f7a-c7d3-43e1-a673-eb5e5ca1fab5` succeeded；Asset `8a52f3f0-f950-4a58-8409-f7da32565397`，`reference_audio`，MP3，40825 bytes，2325 ms | 新签名下载 HTTP 200，字节数和 SHA-256 一致；在 2–15 秒约束内，存在于 ProductionRun/Storyboard 参考音频选择器数据源。 |
| 剧本 | DeepSeek Deployment `a49b0101-03f9-4d60-ab22-7d5c0d44b2db`；fresh `script.generate` smoke attempt `00a44d59-80e5-4038-a930-03af71ec37a2`，snapshot `2215c9e7-2911-4334-b08e-0643be8e1541`，估算 0.010000 USD | 首次 1200-token Job `ed7ffaba-c9aa-4043-8276-5187c6d53c3e` 返回空正文并准确记录 failed；默认 4096-token 重试 Job `a8bc6ce2-8f93-40ad-91f8-6b57834fb236` succeeded，actual cost 0.010000 USD；Asset `7df17a57-e516-4af0-bd32-fa7e9d1fcd43`，`script`，Markdown，2682 bytes | 新签名下载 HTTP 200，字节数和 SHA-256 一致；存在于 ProductionRun“素材库剧本”选择器数据源，选择后只会创建 preview revision，不会覆盖已有确认剧本。 |

H3 导入的原始 `length=124` 默认值不符合该节点声明的 17 帧倍数。素材库公开的工作流参数默认值已统一归一为 `119`；这与 smoke fixture 的归一化规则一致，避免用户保持默认值时提交一个必然被 ComfyUI 拒绝的参数。浏览器端登录会话在本次验收环境不可用，因此“选择器可见性”采用其正式 `/assets/?kind=...&is_active=true` 数据源及受控选择条件核验；素材本身没有写入任意真实项目或触发短剧生产工作流。
