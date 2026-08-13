# 生产技术验收

本清单用于在目标环境验证部署、运行和恢复能力。

## 上线前验证

- 使用真实域名和 TLS 反向代理；完成 [生产部署安全](./production-deployment.md) 中的 production profile、回环发布、代理头覆盖、Secure Cookie 和 HSTS 验收。
- 更换 Django、PostgreSQL 和任何外部服务密钥；关闭并删除演示账号，限制管理后台来源地址。
- 使用 PostgreSQL，配置数据库、媒体目录和 ComfyUI 输出目录备份；先用 `scripts/backup.ps1` 和 `scripts/restore.ps1 -DryRun` 校验，再在隔离验收环境实际演练一次带 `-ConfirmRestore` 的恢复。操作、安全边界与验收项见 [备份、校验与恢复](./backup-restore.md)。
- 保持同一 GPU 只有一个 worker；用目标模型 profile 完成 5 秒低分辨率烟测，再逐级验证 15 秒与 768×1344。记录耗时、峰值显存、峰值内存、失败恢复和输出哈希。
- 对三类 H3 输入模式完成验收：文生视频无参考；首尾帧模式分别验证 0 图、仅首帧、仅尾帧、首尾双帧；Ref2VA 至少验证零参考、单图片、纯独立音频、视频内嵌音频、视频外部配对音频和混合参考。检查固定模型加载器始终存在、仅为实际输入创建动态素材节点、标签顺序、人物身份、对白、AAC 音轨、镜头末帧及 FFmpeg 单集/整剧装配。
- Ref2VA 执行工作流矩阵包括官方默认、T8 Dual-Clock 4-Step 和 Turbo LoRA。每个变体均验证默认值、上下边界、拒绝越界与错误类型、任务详情中的实际 steps/LoRA 强度、重试复用物化工作流，以及缺节点/缺模型时在 GPU 提交前失败。T8 Dual-Clock 4-Step 与 Turbo LoRA 保持实验状态；使用兼容 non-pruned Ref2VA 后，执行固定 seed、低分辨率、5 秒的端到端 GPU smoke，并记录耗时、峰值显存、音画同步、画质和输出哈希。
- 对本地 SDXL Base 1.0 图片链路完成验收：核对 `sd_xl_base_1.0.safetensors` 的文件大小与 SHA256、实时 checkpoint 枚举、7 节点 API 图、30 steps/CFG 6.5/`dpmpp_2m`/`karras` 和 PNG 预览下载。目标环境至少完成一次 768×1344（0.98 MP）烟测，并覆盖批量吞吐、长期稳定性和人工画质抽检。0.1–0.3 MP 只作为低成本链路烟测，不作为正式图片默认质量档。
- 校验素材库文件哈希、母版哈希、裁切关系、来源声明和生成工具声明，避免清单与文件静默漂移。
- 运行 `scripts/check.ps1` 和 `scripts/security_check.ps1`，保存测试、生产部署静态检查、OpenAPI、素材哈希、依赖审计和 ComfyUI capability probe 结果。

## 第三方许可文件

仓库随附 `licenses/minimax-h3/LICENSE-2026-08-09.txt` 和根目录 `NOTICE`。分发对应 MiniMax H3 材料时应保留这两个文件。其他模型、组件和二进制也分别适用其自身许可证。

## 建议的运行监控

- API：5xx、P95 延迟、登录/刷新失败、数据库连接与磁盘剩余空间。
- Worker：队列深度、单任务耗时、重试次数、失联 prompt、GPU/RAM 峰值。
- 产物：缺失音轨、0 字节文件、时长偏差、装配失败、人工质检退回率。
- 安全：异常登录、越权 403、被拒绝的路径/SSRF 输入、上传失败和管理操作审计。

## 验收边界

- 自动验证范围包括代码、数据库迁移、API 契约、前端构建、素材完整性和执行验证时的 ComfyUI 节点/模型可见性。
- 官方 H3 与 SDXL 是否可用，以目标环境保存的烟测记录为准。T8 Dual-Clock 4-Step 与 Turbo LoRA 具备代码、契约及节点/模型可见性验证，尚无 GPU 端到端生成结论；H3/SDXL 的批量吞吐、画质一致性和长期稳定性需按上面的烟测要求验收。
- 来源清单覆盖任务、模型/模板、Prompt 哈希、生成时间、输入媒体哈希和最终产物哈希，并对错误租户令牌、签名篡改、sidecar 篡改和媒体字节篡改实行拒绝服务；清单不包含完整提示词或本地路径。
- Docker 配置可做语法检查；若验收机未安装 Docker，仍需在有 Docker 的环境执行一次完整镜像构建和容器健康检查。
