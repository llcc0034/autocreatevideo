# 2026-08-27 动态 Ref2VA 双时钟短剧三线实测

> 状态：进行中。本文按真实操作顺序持续记录，不把中间成功、失败或推断改写成最终结论。

## 测试目标与统一口径

- 视频只使用 MiniMax H3 动态参考生视频 `Ref2VA` 双时钟工作流，不回退到其他视频模式。
- H3 输出为 352×608（约 0.2 MP）、24fps、4 steps；镜头时长按剧本与冻结帧策略换算。
- Bible 参考图使用 z-image-turbo，人物、场景、道具分别生成、审核并冻结唯一主锚点。
- 动态参考视频是否必选完全由所选 Workflow 的冻结条件合同决定；`minimum_items=0` 时允许留空，不强塞无关素材。
- 三条线均包含对白、声景与非画内音乐信息，并核对最终 H3 六段提示词。

| 测试线 | 项目 / Run | 当前状态 |
| --- | --- | --- |
| 纯人工 | Project `fe107d33-12ae-4ab2-a468-620b3c0bde24` / Run `f7200811-831a-4e2b-808a-23d5546345fb` | 已完成真实 H3 生成、人工视听审核、选片、组装与交付；Run 成功，ReleaseBundle `b720e57e-adb9-4d16-9ed5-1b4f9979d597` 已封存。 |
| 半自动（旧阻断样本） | Project `f907ebfe-3e78-45c6-8be0-4e5c4bbfe0f9` / Run `3c37a1d6-b357-492a-a71d-ca48076d1811` | AI 草稿、人工修改审核、有限重试转人工、Bible 生成/重生成、逐镜绑定与 H3 v7 提示词编译已完成。真实 H3 Task `c2be11cb-fd75-4cb5-9403-d66bb6584acc` 实际冻结为约 5 秒、124 帧，在采样节点因显存不足明确失败；Run 当前阻塞于 `media_generation`，未记为成功。基于最新合同的新半自动 1 秒 Run 仍待实测。 |
| 全自动（旧阻断样本） | Project `19ab6dd7-e2bc-4667-a6bc-d1e05e79a1eb` / Run `ffb05cef-028a-4507-a92c-e68d489cc275` | 已从空项目真实启动，AI 剧本、Bible 与全剧影视规则均自动完成；在 storyboard 因旧 Run 冻结 5000–15000ms、而剧本为 1000ms 被 `script_duration_policy_mismatch` 阻断。该历史 Run 不重写、不重试；基于最新合同的新全自动 1 秒 Run 仍待实测。 |

## 电脑重启后的当前检查点

- 迁移 `0082_comfyui_pre_submit_resource_release` 已把 ComfyUI 切换工作流前的资源释放收敛为 Deployment `runtime_config.pre_submit_resource_release` 版本化配置，字段为 `schema_version / enabled / unload_models / free_memory`。当前 H3 与 z-image-turbo Deployment 均配置为在新任务 `POST /prompt` 前先 `POST /free`，同时卸载模型和回收空闲显存；仅允许 `TASK_MAX_CONCURRENT=1`，释放被拒绝或结果无法确认时明确失败且不提交生成请求，恢复、轮询和产物收集路径不会重复释放。
- z-image-turbo Workflow v4 的最新真实 Smoke `75238c9b-8450-4ef7-ae4a-d046dd0c4152` 已成功；其不可变结果中 `pre_submit_resource_release.status=succeeded`、`http_status=200`，证明配置化 `/free` 已在真实提交边界执行。
- H3 Workflow v5 的第一次最新 Smoke `81385ef1-3eba-469d-bb57-31b3b5273833` 未绑定任何图片或视频，远端在 `MiniMaxH3AudioConditioningT8` 明确报错 `REF2VA requires at least one reference media input`。这说明“动态参考视频可选”不等于“全部参考媒体都可为空”：Ref2VA 至少需要一项图片或视频参考，但不强制必须是视频。
- 随后使用人物、场景、道具三张 Bible 图片且不绑定参考视频的 H3 Smoke `a4356d74-2570-4c87-9473-1a6b2b314e9d` 已成功；其 `/free` 审计同样为 HTTP 200。实际 MP4 为 352×608、22 帧、24fps、0.917 秒、H.264 + AAC、111246 bytes，SHA-256 `EA7D632917ED16C6B3715C75943901603E44D8641FC766FBB7E231E3B8708C97`。该结果验证了 0.2 MP、4 steps、双时钟 H3 在“三张 Bible 图、零参考视频”条件下可以真实执行。
- 当前受管文本目标上的 `script.generate` Smoke `c014e49c-75b0-4ca3-8d8d-0d48a13b9aca`、`script.structure` Smoke `60d97ab0-b59e-4361-bdca-ef7b35b66b8c`、`visual_rules.generate` Smoke `345b90ee-69f2-4469-a719-9869fcf4bed6` 均已成功。
- `prompt.normalize` 最新 Smoke `500cdda8-e6ca-416e-9b83-57c6bccb188d` 仍因 `protected_token_mismatch` 未通过字段生成合同。服务器安全哨兵和可信任务投影已投入复测，但当前真实 Provider 返回仍未满足受保护标记还原要求，因此不能写成已解决。归一化仍按 Profile 配置有限重试，次数耗尽后转人工确认并保留原始 Attempt。
- 当前 Workflow 级真实 Smoke 已证明 z-image-turbo 和含三图 H3 的执行边界可用；最新合同下的新半自动与全自动 1 秒完整 Run 仍均为“待执行”，不能用 Smoke 或旧历史 Run 替代 E2E 成功结论。

## 已完成的真实操作与证据

### 最新 Workflow 与文本能力复测

- z-image-turbo Smoke `75238c9b-8450-4ef7-ae4a-d046dd0c4152` 和三图 H3 Smoke `a4356d74-2570-4c87-9473-1a6b2b314e9d` 均在迁移 0082 后执行，两个结果都持久化了 `/free` HTTP 200 审计；这不是只做接口连通检查，而是先释放资源、再提交并等待真实媒体产物成功。
- H3 空引用 Smoke `81385ef1-3eba-469d-bb57-31b3b5273833` 的失败事实保留，用于界定合同：`reference_videos.minimum_items=0` 只表示视频槽可以为空，人物、场景、道具 Bible 图片可以单独满足 Ref2VA 的“至少一项参考媒体”要求。
- H3 成功 Smoke 使用 352×608、22 帧、24fps、4 steps 与三张图片参考，没有参考视频；产物为 0.917 秒且同时含 H.264 视频流和 AAC 音频流。文件大小与 SHA-256 已在当前检查点列出。
- 四项文本 Smoke 当前为三项成功、一项失败：剧本生成、剧本结构化、全剧影视规则生成通过；提示词字段生成仍在 `protected_token_mismatch` 阻断。该结果只证明各自操作的当前事实，不以三项成功掩盖第四项失败。

### 纯人工线

- 新 Run `f7200811-831a-4e2b-808a-23d5546345fb` 使用人工输入剧本、人工填写并复核 22 个导演字段，对白为 `这不是火烧的。`。
- 人物、场景、道具 Bible 图片均由 z-image-turbo 以 352×608、4 steps 真实生成并人工审核。人物首版缺少左领徽章，因此通过正式页面使用冻结 Bible 模型重生成；最终冻结：
  - 人物 Asset `71a33550-9825-48b2-a5b0-05d7c1e9bb8a`，SHA-256 `94d7685a9cdd800360ad1b53fd8fdee756c2097e52ec74c7db7a5eff5e58a7f9`
  - 场景 Asset `1f5f8115-5c0e-41d5-bf4d-924d6a3ac5c5`，SHA-256 `9637cff44966b6085916705f37be5a852daffb425470b845108432b918e68a54`
  - 道具 Asset `f26a2eca-681a-4bbd-8779-290d4bb20392`，SHA-256 `829f66fd1059175a55490f225e8634ec42fd331a9dcae3a304b14ee7cc3b75c9`
- 逐镜冻结三张 Bible 图，并显式选择同故事动态参考视频 Asset `c74b0d68-d5dc-4b37-a88f-c573fec7de56`，用途为 `motion_camera_reference`；未绑定无关视频或音频。
- PromptArtifact `a52f72bb-6d24-43b9-9808-1e916b1c2938` 使用 PromptProfile v7 `ad2accb9-0bbc-48e1-9e38-be253289452a`：正向提示词 5915 字，低于 6200 警戒值和 7000 上限；六段齐全；`<Picture 1..3>`、`<Subject 1..3>`、`<Video 1>` 映射完整；原中文对白以 `<d>[Chinese] 这不是火烧的。</d>` 保留。
- H3 GenerationTask `f2def9d8-75d8-4e00-b7e6-117d341446ae` 已被 ComfyUI 接受并真实成功：352×608、22 帧、24fps、4 steps、Ref2VA、双时钟节点 `MiniMaxH3DualClockSamplerT8`；精确 LoRA 提交值保留 Windows 反斜杠枚举。
- 生成视频 Artifact `736ed797-6d25-4590-bfac-87974244bdbf` 为 104017 bytes，SHA-256 `ced82bb1bed46f0fa782645a93153ab0b2698f7bae315fc628dccccd60a08389`。FFprobe 实测 H.264、352×608、22 帧、24fps、0.917 秒，并含 AAC 双声道 32kHz 音轨；音频统计为 mean -14.6dB、max -0.4dB，确认不是静音轨。
- 已抽取首、中、尾帧到 `docs/evidence/e2e-short-drama-2026-08-27/pure-manual/`。人工复核确认人物面部、发型、风衣和徽章，场景窗户与货架，以及红色印章道具在镜头内保持连续；对白时可见口型变化。当前验证确认了音轨存在且非静音，未把无法自动转写的对白内容伪装成已核验。
- AssemblyJob `46852766-a7f3-43f3-ac30-9f48bbde50de` 已成功；成片 Artifact `933bfa2d-e62d-41a2-a287-be38d1ab6e20` 为 413379 bytes，SHA-256 `29116f9c2a4b62949bd6a3a833b3a5572dc95aabf840601e1f200466ae136303`，FFprobe 实测 768×1344、24 帧、24fps、1 秒并含 AAC 双声道 48kHz 音轨。
- ReleaseBundle `b720e57e-adb9-4d16-9ed5-1b4f9979d597` 已以 `sealed` 状态封存，manifest SHA-256 `69ef66717b5deded9f62048476226e33205e71b98690939fd3cbdcb57221a347`；Run 于 2026-08-27 05:47:29Z 成功结束，13/13 阶段完成。

### 半自动线

- 新建空 Project `f907ebfe-3e78-45c6-8be0-4e5c4bbfe0f9`，以 Run `3c37a1d6-b357-492a-a71d-ca48076d1811` 从正式生产入口执行半自动 Recipe v4；冻结任务预算为 30，允许外部调用，H3 PromptProfile v7 与 z-image Bible Profile 均在创建时显式选择。
- 人工输入一镜 5 秒故事事实：28 岁女设备工程师周岚、雨夜写字楼电梯、左腕银色维修表、右手磨损黑色门禁卡、红色数字从 03 变为 02；对白为 `倒数开始了。`，同时提供电梯钢缆低鸣、远处雨声与低频脉冲音乐约束。
- DeepSeek 生成剧本结构、Bible 候选与全剧影视规则草稿；人工在 AI 草稿上逐项修改并确认：去重负面词，将色板约束为冷白应急灯稳定、红色仅用于倒数数字，并明确左腕维修表、右手门禁卡与 03→02 的连续性。
- AI 结构化阶段收到的原始意图是 1 秒，但旧 Run 创建时先冻结了平台默认 5000–15000ms，结构化合同只能输出 5 秒。人工审核继续了这个历史样本，但它不能代表 1 秒验收成功；同一根因还使旧全自动 Run 的 1000ms 剧本在 storyboard 被明确阻断。
- `script.normalize` 连续三次返回缺少响应正文；系统按配置执行首次加两次有限重试，耗尽后转人工，没有直接失败或无限重试。三次 Attempt：
  - `b3128567-b4c2-4f9d-a076-f14c50f77c35`
  - `9deee3eb-90e7-40e6-bbaf-0648aa97dbaa`
  - `533024ef-04a9-4a67-a1c6-139a4a92fc62`
- 人工完成 21 个导演字段，并把规范英文名称修正为 `Zhou Lan`、`Rainy-Night Office Elevator`、`Worn Black Access Card` 后通过 canonical 名称门禁；中文业务名称仍保留，不用展示字段替代模型用规范名。
- z-image-turbo 人物、场景、道具三类真实任务均成功，道具因首版不满足审核要求又重生成一次：
  - 人物 Task `fbef597d-b0fc-4d5c-88e4-919750f03f61`
  - 场景 Task `e7f2ce62-1d73-47e4-b20a-cb126521608d`
  - 道具首版 Task `23cb1627-d135-4a5a-a8bf-704c0af78f2b`
  - 道具重生成 Task `751f44b3-e268-4b7c-8600-a6acd61566ab`
- 三类候选均完成预览、完整性审核、选择与唯一主锚点冻结；逐镜只绑定人物、场景、道具三张图片。Workflow 冻结合同允许 0 条参考视频，因此与本剧无关的视频保持未选择，没有被自动塞入 `<Video 1>`。
- 道具重生成图仍在一张静态卡片上同时显示 `03` 与 `02`，说明“初始静态锚点”和“镜头内状态变化”尚未在 Bible 生图约束中拆开；当前将其记录为明确质量问题，不能把重生成动作本身当作已解决。
- PromptArtifact `ba394350-f429-4ebc-9940-2cc0df5cb6dd`：
  - 使用 PromptProfile v7 `ad2accb9-0bbc-48e1-9e38-be253289452a`，正向提示词 5472 字，小于 6200 警戒值和 7000 上限。
  - `subject_definitions / summary / retention_analysis / detailed_description / overall_soundscape / non_diegetic_music` 六段齐全。
  - `<Picture 1..3>` 与 `<Subject 1..3>` 一一对应。
  - 原中文对白以 `<d>[Chinese] 倒数开始了。</d>` 保留。
  - 未引用 `<Video 1>`，与冻结 `reference_videos.minimum_items=0` 一致。
- 真实 H3 Task `c2be11cb-fd75-4cb5-9403-d66bb6584acc` 已提交，Comfy prompt ID 为 `f626050b-d91c-421b-849a-b7a55f23677e`。数据库复核时该任务实际为 124 帧、24fps（约 5.167 秒），已在节点 10 `SamplerCustomAdvanced` 明确失败：`torch.OutOfMemoryError`，8GB device 当时已分配 4.70GiB、追加请求 1.14GiB、CUDA free 0；Run 当前阻塞于 `media_generation`，尚未进入选片、组装或封存。该旧任务同时存在“错误冻结为约 5 秒”与“切换模型前未释放上一工作流显存”两个因素，不能把它作为最新 22 帧目标合同的容量结论。

## 已发现问题、根因与处理

| 问题 | 已确认根因 | 处理状态 |
| --- | --- | --- |
| 用户要求 1 秒，但半自动旧 Run 被固化为 5 秒；全自动旧 Run 又因 1 秒与冻结范围不一致在 storyboard 阻断 | Run 创建先采用平台默认 5000–15000ms，没有从 `episode_duration_seconds / shots_per_episode` 派生本次结构化镜头时长合同，也没有与冻结文本模型策略统一求交。第一次修复仅覆盖 AI 从零创建；重启后复核又发现“导入剧本”前端会丢弃同一组计划参数，因此半自动仍会重现旧问题 | 已收敛为通用 `structured_shot_duration_contract`：包含 `script_structure` 的 AI 创建和导入配方按集数、每集镜数、单集时长与全部冻结 route 策略求交；1 集、1 镜、1 秒冻结为 1000/1000/1000ms，冲突在模型调用前拒绝。代码与专项测试已完成，仍需用新半自动和全自动 Run 验证真实冻结事实。 |
| Bible 候选短暂显示“预览不可用，禁止盲选” | 页面先清空历史预览映射再异步加载，加载期间错误渲染成正式失败 | 已修复加载态为“正在校验预览…”，仅加载完成后仍无有效预览才禁止选择；前端测试 8/8、类型检查通过。 |
| ComfyUI 明确 HTTP 拒绝被记录为“提交结果不确定” | 任务队列把 `ComfyUIHTTPError` 与连接中断统一包装为 `SubmissionUncertain`，丢失 4xx 详情 | 已修复：明确 HTTP 响应保留状态码和详情，标记 `terminal_failed / remote failed`；传输中断仍要求人工核验。相关测试 66/66 通过。 |
| 烟测成功，但正式 H3 在 `/prompt` 被立即拒绝 | 发布与烟测使用 ComfyUI 下拉框精确 LoRA 值（目录分隔符为反斜杠）；正式语义编译用规范化 `expected_filename` 的正斜杠覆盖精确值，未满足 Combo 精确枚举 | 已通用修复：从哈希校验后的已发布 graph 冻结精确提交值，同时继续用规范化文件名和 SHA 校验模型身份；新人工 Run 已被 ComfyUI 接受并运行。 |
| 半自动旧 Run 不再创建新的媒体 Attempt | 重启后复核数据库确认该 Run 已用 10 个生成任务、冻结上限为 30，并非预算耗尽；停止重试的真实原因是它仍冻结约 5 秒、124 帧的旧时长与旧提示词合同，继续执行不能验证当前 1 秒目标 | 保留旧 Run 作为淘汰合同的审计证据；使用最新合同新建 Run 重跑，不手改或续跑历史快照。 |
| H3 提示词总长度与部分质量规则仍有代码常量 | PromptProfile 已配置字段级规则，但通道总长度 7000/6200 与一处 350–500 重复判断仍在 H3 adapter | 已移除 H3 常量并迁移到 PromptProfile v7：`positive_prompt` 上限/警戒值及 `detailed_description` 词数规则均由配置驱动；新人工 Run 已冻结 v7 并通过编译。 |
| `prompt.normalize` 历史无正文与当前受保护标记不一致 | 历史 Attempt `0fc487db` 已确认是思考模式消耗输出预算后没有返回结构化正文；迁移 0079 已通过版本化 `chat_completion_options` 关闭该模式。后续字段合同失败继续保留。最新 Smoke `500cdda8-e6ca-416e-9b83-57c6bccb188d` 已进入服务器安全哨兵和可信任务投影后的新合同，但真实返回仍触发 `protected_token_mismatch` | 尚未解决，不能因另外三项文本 Smoke 成功而宣告文本目标全部可用。配置合同已包含容量、确定性分批和有限重试；校验或审计仍失败时，重试耗尽即转人工确认，不会无限重试。下一步继续修复受保护标记还原并重新 Smoke。 |
| 半自动道具 Bible 静态图同时出现 `03` 和 `02` | 生图提示把同一镜头内的状态迁移同时交给单张静态 canonical 参考图表达 | 已修复为 PromptProfile schema v4 的版本化 `source_projection_config`：人物/道具 canonical 图按配置仅保留单一初态，跨实体场景与变化语句在生图调用前剔除并审计；`03→02` 仍保留在逐镜动作和 H3 动态提示词。z-image Bible v2 与 H3 v8 已唯一激活，专项回归全部通过；待新 Run 实测图片。 |
| AI 剧本对白没有显式时间轴，后续可能用镜头中点猜测 | 旧结构化合同只要求 `speaker/delivery/text` 三字段，未让文本模型输出对白在镜头内的 `start_ms/end_ms` | 已升级合同：有声对白必须满足 `0 <= start_ms < end_ms <= shot.duration_ms`，无对白必须显式空文本与 0/0；渲染、压缩、DeepSeek/Qwen 与队列合同已同步。待新 Run 核对对白时间和 H3 六段装配。 |
| 半自动旧 H3 在采样节点 OOM | 旧任务实际为 124 帧、24fps（约 5.167 秒），远大于本轮目标 22 帧；同时当时没有切换 z-image-turbo → H3 前的资源释放合同，节点 10 追加 1.14GiB 时 CUDA free 为 0。根因是“旧时长合同放大负载”与“缺少切模释放”的双因素，不能只归咎于 8GB GPU | 迁移 0082 已把 `/free` 做成 Deployment 配置并在新提交前 fail closed；z-image-turbo 与三图 H3 Smoke 均记录 `/free` HTTP 200，后者真实完成 22 帧媒体生成。旧 Task 与 prompt ID 保留为审计事实，不重试；仍需在新半自动、全自动 1 秒 Run 中验证生产链路。 |
| H3 空引用 Smoke 明确失败 | `reference_videos.minimum_items=0` 只允许视频槽为空；ComfyUI 的 Ref2VA 运行时仍要求至少一项 reference media。Attempt `81385ef1-3eba-469d-bb57-31b3b5273833` 在调理节点明确返回 `REF2VA requires at least one reference media input` | 不把动态参考视频改成强制项。Smoke 改用人物、场景、道具三张 Bible 图片且不绑定视频，Attempt `a4356d74-2570-4c87-9473-1a6b2b314e9d` 已真实成功；新 Run 继续按 Workflow 合同显式绑定图片。 |

## 待完成

1. 继续修复 `prompt.normalize` 最新 Smoke `500cdda8-e6ca-416e-9b83-57c6bccb188d` 的 `protected_token_mismatch`，重新执行真实 Smoke 并刷新该操作的平台可用性证据。历史失败 Attempt 与旧 Run 的冻结事实保持不变。
2. 从正式页面新建半自动 1 秒 Run：审核/修改 AI 草稿，真实生成并审核单状态 Bible 图，核对对白时间轴和 H3 六段提示词，再完成 H3、选片、组装与封存。旧 5 秒 OOM Run 不再重试。
3. 从空项目新建全自动 1 秒 Run，验证 AI 剧本、视觉规则、Bible、对白、声景、提示词、媒体、选片、组装与封存全部由策略自动推进。
4. 对两条新 Run 补齐文件大小、SHA-256、FFprobe、音轨、抽帧、一致性、ReleaseBundle 与 live verification；执行 `scripts/check.ps1`，再按授权只保留最新成功代表任务并清理旧开发样本。
