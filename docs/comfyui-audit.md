# ComfyUI / MiniMax H3 兼容性与验证

本文定义 AutoPrompt Studio 连接 ComfyUI 时的能力要求、验证方法和验收边界。配置示例见 [`config/comfyui-h3.audit.example.json`](../config/comfyui-h3.audit.example.json)，生产验收清单见 [`production-readiness.md`](./production-readiness.md)。

## 连接边界

- 本地开发默认连接 `http://127.0.0.1:8188`。非回环地址必须显式设置 `COMFYUI_ALLOW_NON_LOOPBACK=true`，并由网络层限制来源。
- 浏览器不直接访问 ComfyUI。Django 负责素材授权、工作流物化、能力预检、入队和产物归属校验。
- 平台不会在本地工作流失败时静默切换到 Comfy Partner API。需要外部服务的路径必须由用户显式选择，并单独配置凭据和网络授权。
- ComfyUI 的 `is_active` 数据库状态只表示模板可选；节点、端口、模型枚举和实际 GPU 生成必须在目标环境验证。

## 支持矩阵

| 路径 | 输入模式 | 默认状态 | 可调参数 | GPU 验收要求 |
| --- | --- | --- | --- | --- |
| 官方 H3 Base | T2VA | 默认 | 分辨率档位；采样固定 20 steps | 目标环境低成本烟测 |
| 官方 H3 First/Last Frame | T2VA、I2VA、L2VA、FL2VA | 默认 | 分辨率档位；采样固定 20 steps | 0/1/2 张图矩阵烟测 |
| 官方 H3 Ref2VA | Ref2VA | 默认 | 分辨率档位；采样固定 20 steps | 图片、视频、配对音轨和独立音频矩阵烟测 |
| T8 Dual-Clock 4-Step | Ref2VA | 实验、显式选择 | `STEPS` 1–20，默认 4；`LORA_STRENGTH` 0–2.2，默认 1.0 | 使用兼容 non-pruned Ref2VA 后验收 |
| Turbo LoRA | Ref2VA | 实验、显式选择 | `STEPS` 1–20，默认 10；`LORA_STRENGTH` 0–2.2，默认 1.0 | 使用兼容 non-pruned Ref2VA 后验收 |
| SDXL Base 1.0 | 文生图 | 默认 | 独立图片分辨率档位 | checkpoint 与 PNG 烟测 |

两个实验工作流的源配方连接 Turbo LoRA，而目标环境若使用 pruned Ref2VA INT8 基模，可能出现 AdaLN 适配器形状不匹配、LoRA 不完整应用或运行失败。节点和模型可见、静态图构建成功、参数契约通过均不等同于 GPU 端到端验收。

## 必需能力

官方 H3 路径至少需要以下节点类型：

- 模型加载：`UNETLoader`、`CLIPLoader`、`VAELoader`。
- H3：`MiniMaxH3ImageToVideo`、`MiniMaxH3ReferenceToVideo`。
- 采样：`RandomNoise`、`BasicGuider`、`KSamplerSelect`、`BasicScheduler`、`SamplerCustomAdvanced`。
- 解码与输出：`VAEDecode`、`VAEDecodeAudio`、`CreateVideo`、`SaveVideo`。
- 动态参考：`LoadImage`、`LoadVideo`、`GetVideoComponents`、`LoadAudio`；只在实际绑定对应输入时创建。

实验路径还需要：

- T8 Dual-Clock：`LoraLoaderBypassModelOnly`、`MiniMaxH3AudioConditioningT8`、`MiniMaxH3DualClockSamplerT8`、`MiniMaxH3AVDecodeT8`。
- Turbo LoRA：`LoraLoaderModelOnly` 和模板声明的 Turbo LoRA 文件。

SDXL 图片路径固定使用 `CheckpointLoaderSimple`、两个 `CLIPTextEncode`、`EmptyLatentImage`、`KSampler`、`VAEDecode` 和 `SaveImage`，checkpoint 固定为 `sd_xl_base_1.0.safetensors`。

配置示例列出模板期望的 H3 diffusion model、Qwen3-VL text encoder、video/audio VAE 和 LoRA 文件名。文件是否存在以目标 ComfyUI 的实时模型枚举为准，不以仓库文档或本地目录推断。

## 入队前契约预检

生成 worker 在创建提交令牌或调用 `/prompt` 前读取 `/object_info`，并验证：

1. 节点类型和输入端口存在；必需、自增长和可选端口与模板一致。
2. 节点输出槽位、连接类型、图环路和固定加载器符合服务端模板。
3. 数值参数满足类型、范围和步长；模型、checkpoint 与 LoRA 值出现在实时枚举中。
4. 只接受服务端审核模板和受控参数，不执行用户提交的任意工作流 JSON。
5. 通过后的契约哈希写入任务 `capability_snapshot.comfyui_contract_preflight`。

已证明不兼容的契约会在 GPU 提交前失败。ComfyUI 不可达时，任务按重试上限安全回队；具有不确定提交状态的任务不会自动二次调用 `/prompt`。

可用的只读检查：

- `GET /system_stats`：服务连通性。
- `GET /features`：实例能力。
- `GET /object_info` 或 `/object_info/{node_class}`：节点、端口与枚举。
- `GET /models/{folder}`：模型目录枚举。
- `GET /api/v1/workflow-templates/production-options/`：平台提供的无路径选择器元数据。
- `GET /api/v1/workflow-templates/{id}/validate-comfyui/`：单模板可用性；不会提交生成。

## 输入与输出约束

- H3 视频为 24 FPS，时长 5–15 秒，帧数对齐 `17k+5`。
- Ref2VA 支持 0–9 张图片、0–3 个视频、每个视频可选一条配对音轨，以及 0–3 条独立音频。
- 参考视频进入任务目录后统一转为精确 24/1 CFR H.264，并核验帧数；音频按最终帧时长补齐或裁剪。
- 上传与暂存文件按租户、MIME、扩展名、大小、媒体流和解析后路径重新验证。
- 输出只能来自当前任务对应的 Prompt，并通过短期签名媒体 URL 提供；原始 ComfyUI 输出目录不作为公开下载入口。
- 仓库素材库是 Bible 文本连续性和溯源锚点。只有用户显式绑定到 H3 输入槽位的媒体才会作为像素或音频参考暂存到 ComfyUI。

## GPU 烟测

在没有运行中任务且目标模型已加载条件满足时，按以下顺序执行：

1. 对每份模板运行 `validate-comfyui`，保存节点、端口与模型检查结果。
2. 使用固定 seed、5 秒、低分辨率档位提交一项任务；确认只创建声明输入对应的动态节点。
3. 核对任务中的模板 ID、实际 steps、LoRA 强度、分辨率、帧数、FPS、Prompt ID 和契约哈希。
4. 验证输出可解码、时长和帧数正确；启用音频时确认 AAC 音轨和音画同步。
5. 记录目标设备、ComfyUI 与插件版本、模型文件哈希、耗时、峰值显存/内存和输出 SHA-256。
6. 再验证取消、worker 中断恢复、明确失败后的审计式重试，以及缺节点/缺模型时未调用 `/prompt`。

实验工作流只有在兼容 non-pruned Ref2VA 模型上完成上述步骤后，才能在对应环境标记为已验收。正式分辨率和批量吞吐需要独立压力与画质抽检，不能由低分辨率单次烟测推断。

## 安全要求

- ComfyUI 默认只监听回环地址，不启用通配 CORS。
- 工作流节点受服务端 allowlist 限制；Partner/API/network、下载上传、模型保存和文本保存类节点始终拒绝。
- `COMFYUI_EXTRA_ALLOWED_NODE_TYPES` 只允许加入经人工验证、无外部网络和无持久副作用的本地节点，危险类别不能覆盖放行。
- API key 只保存在后端环境变量，不写入模板、任务配置、日志或导出工作流。
- 日志不记录原始提示词、令牌或本机绝对路径；对外选择器仅返回归一化文件名。
- 自动中断和自动释放显存保持关闭。需要重启 ComfyUI 时先读取 `/queue`，确认无运行或待处理任务。

## 故障判定

- `/system_stats` 失败：先处理 ComfyUI 进程、端口、CUDA OOM 或网络边界，再检查模板。
- 核心节点正常但 LoRA 枚举失败：官方模板按自身能力判定；仅实验模板不可用。
- 参数被拒绝：只提交模板 `input_schema` 声明的字段；官方模板不接受 `STEPS` 覆盖。
- 静态预检通过但 GPU 失败：保存错误、版本、模型哈希和任务配置，保持实验状态，不以重复提交掩盖兼容性问题。

## 第三方许可证

MiniMax H3、SDXL、ComfyUI、插件、模型权重和 FFmpeg 分别适用各自许可证。分发对应材料时保留仓库 `licenses/` 和 `NOTICE`，并核对所用模型与插件的具体条款。
