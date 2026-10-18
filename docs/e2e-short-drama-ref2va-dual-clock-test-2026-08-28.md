# 2026-08-28 动态 Ref2VA 双时钟短剧三线实际操作测试

> 文档类别：历史证据（2026-09-21统一标注）。正文中的版本、任务、通过/失败及“进行中/下一步”仅表示当时状态，不是当前操作指令或新的执行授权。当前未完成工作以[统一台账](optimization/README.md)为准，实际操作按[当前手册](production-operator-manual.md)。不按旧记录重放任务，不把旧验收外推到重建配置。

> 文档职责：记录 2026-08-27 至 2026-08-28 通过正式项目工作台执行的代表性实测，并区分“技术链路成功”“人工接管后成功”“真正无人值守成功”和“视觉质量验收”。历史问题与旧 Run 不在本文中改写，详见 [2026-08-27 报告](e2e-short-drama-ref2va-dual-clock-test-2026-08-27.md)。

> 2026-08-30 后续边界：本文仍是历史证据，不是新一轮验收结果。v22/v23 的动态参考视频数为 0，不能证明本轮“两个镜头都实际提交生产动态视频”的三线验收。新一轮纯人工、半自动、无人值守验证改由操作员按应用内“生产手册”第 15 步依次手动执行；在用户回填三个 Project/Run 和动态参考 Asset ID 前，不新增通过结论。

## 结论

- 纯人工与半自动两条线均完成真实 z-image-turbo Bible 生图、H3 Ref2VA 双时钟视频生成、媒体探测、人工选片、逐集装配和 sealed ReleaseBundle，技术链路通过。
- v22 是本轮第一条真正零人工完成的全自动样本；v23 是修复“仅声音角色视觉过滤”和“静态初态投影”后的完整真实复验。v23 的 Project `6b1d3df0-6f37-4539-9dfc-f8055206c971` / Run `b4dc0fd9-5ee8-456c-9d2f-c91a599f4407` 从新空项目生成 1 集 2 镜、4 张有效 Bible 图、2 条 H3 视频、8 秒逐集母版和 sealed ReleaseBundle。17/17 阶段全部成功且各 Stage `attempt_count=1`；7 个审批全部由 `recipe_policy` 作出，人工审批为 0；生产任务使用 44/60，实际文本费用 0.190000 USD。因此“从空项目到交付”的零人工技术链路再次通过，两个修复项也已通过新 Run 复验。
- 早期全自动 Run `168642e3-f16d-4b99-b1e9-8f7035dfea8c` 仍保留为“规范化失败后人工接管”的真实记录；v16–v21 的无意义英译英、重复终态校验、大快照上限、smoke TTL、文本失败审计/有限重试和原子任务预算轨迹也全部保留，没有用 v22 覆盖或改写历史失败。
- 三条线的视频阶段都只使用已发布的 MiniMax H3 动态 Ref2VA 双时钟工作流 v5，没有回退 T2VA、I2VA、FL2VA 或其他视频工作流。v23 两个镜头均绑定 4 张 Bible 图片、动态参考视频数均为 0，符合 Workflow `reference_videos.minimum_items=0` 的可变输入合同。
- v23 两个镜头的实际 H3 提示词均包含 `subject_definitions → summary → retention_analysis → detailed_description → overall_soundscape → non_diegetic_music` 六段；中文对白分别为画外 S2 的 `别碰它！它会醒过来。`（0.8–3.6 秒）和 S1 的 `我知道，我不碰。`（0.5–3.3 秒），均保留在 `<d>[Chinese] ...</d>` 标签内。两条视频均有 AAC 音轨；当前没有用自动转写证明成片语音逐字正确。
- v23 已验证：仅声音/画外角色仍保留在 `Shot.characters` 和对白事实中，但没有 Bible 图、没有视觉绑定，也没有进入 H3 的 `<Picture>/<Subject>`；种荚静态 PromptProfile v3 只投影初始暗态，后续发光动作迁入 `request.action` 审计，z-image 参考图肉眼不发光，第一镜画面呈现先暗后亮。
- v23 技术与上述两个配置化修复通过，不等于跨镜视觉质量完全通过：六角银色徽章参考被错误贴到种荚表面，人物胸前徽章偏圆；第二镜人物面部大多被裁切，无法严格判断跨镜脸一致。模型贡献的自然语言还出现非阻断的下一镜对白串入、发言人归属漂移，但服务端权威对白标签、speaker 与时间窗正确。后续应由可配置语义审核与人工确认处理，不把这类质量建议升级为技术硬阻断。

## 统一测试口径

| 项目 | 冻结事实 |
| --- | --- |
| 视频 Workflow | `MiniMax H3 动态 Ref2VA 双时钟 4-step` v5，WorkflowTemplateVersion `e2bf310f-0c59-4733-b7ff-4e2af810f975`，published |
| H3 图规格 | 352×608，共 214,016 像素，约 0.214 MP |
| H3 采样 | 4 steps；`shift_video=12`、`shift_audio=3`；24fps |
| 1 秒测试镜头 | 合法冻结为 22 帧、917ms；逐集装配补齐为 24 帧、1.000 秒 |
| v22/v23 4 秒镜头 | 每镜请求 4.000 秒，按 H3 帧公式实际生成 90 帧、3.750 秒；两镜逐集装配补齐为 192 帧、8.000 秒 |
| Bible 生图 | z-image-turbo Workflow v4，WorkflowTemplateVersion `457c26db-0a20-482d-a702-3d2063782408`；352×608、4 steps |
| Ref2VA 输入合同 | 至少有一项参考媒体；动态参考视频槽可以是 0，人物/场景/道具 Bible 图片可以独立满足 Ref2VA 参考输入 |
| 对白 | 原始语言保留；H3 详细描述使用 `<d>[Language] ...</d>`，人物引用同时保留 `<Subject N>` 与 `(Sx)` |
| 结果核验 | 数据库阶段/审批/Attempt、实际生成文件 SHA-256、FFprobe、首中尾抽帧、AssemblyJob、ReleaseBundle |

MiniMax 官方依据为 [H3 Prompt Writing Skill](https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/h3-prompt-writing/SKILL.md) 与 [Full-Reference Mode Guide](https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/h3-prompt-writing/references/ref-en.txt)。纯人工、半自动和早期全自动样本使用 1 秒，是固定 0.2 MP / 4 steps 条件下的快速端到端测试偏离；v22、v23 已把请求时长提高到官方建议下限 4 秒并完成两镜测试，但仍不能替代更长镜头和正式内容质量验收。

## 三条线结果总览

| 测试线 | 项目 / Run | 自动化边界 | 最终分类 |
| --- | --- | --- | --- |
| 纯人工 | Project `fe107d33-12ae-4ab2-a468-620b3c0bde24` / Run `f7200811-831a-4e2b-808a-23d5546345fb` | 人工确认剧本、Bible、22 个导演字段、Bible 候选、逐镜绑定与成片 | 13/13 阶段成功；纯人工链路通过 |
| 半自动 | Project `cdf3134e-c09c-4e55-97a8-3be1b0114bfe` / Run `07a595da-2b9f-40e3-a81c-cccb3337435f` | AI 生成结构、全剧影视规则和提示词字段草稿；人工审核/修改、重生人物图、筛选素材并选片 | 18/18 阶段成功；半自动链路通过 |
| 全自动目标 | Project `820e4f54-ab20-4c43-8787-98ee1eb75906` / Run `168642e3-f16d-4b99-b1e9-8f7035dfea8c` | 从空项目由策略生成剧本、Bible、影视规则、参考图和成片；规范化合同失败后人工确认一次 | 17/17 阶段最终成功；人工接管后通过，真正无人值守未通过 |
| 全自动零人工复测 v16–v21 | 详见“全自动线后续零人工复测” | 每轮都从新空项目开始，不沿用失败 Run；修复明确阻断后继续下一轮 | 历史轨迹止于 v21 视频原子入队前，因显式任务硬预算 `39/40` 阻断；后续 v22、v23 已另行完成 |
| 全自动零人工 v22 | Project `da4eaef3-e299-4570-9238-130376b39bd4` / Run `dec14c59-49d7-4e3b-8c92-5c6ea5ba598c` | 从新空项目生成 1 集 2 镜、Bible、规则、两条 H3 视频、8 秒母版与交付；7 个审批均由策略完成 | 17/17 阶段成功；零人工技术链路通过；其两个配置缺口后来由 v23 复验修复 |
| 全自动零人工 v23 | Project `6b1d3df0-6f37-4539-9dfc-f8055206c971` / Run `b4dc0fd9-5ee8-456c-9d2f-c91a599f4407` | 从新空项目复验仅声音角色过滤、静态初态投影、双对白、两条 H3 视频、8 秒母版与交付；7 个审批均由策略完成 | 17/17 阶段成功；零人工技术链路与两个配置化修复通过，跨实体引用归属等质量项继续优化 |

## 1. 纯人工线

### 操作与审核

- 使用 Recipe `e2e-ref2va-manual-dual-clock` v5，ID `2760b1f1-e53d-47cd-9ca7-afe04eccf82e`。
- 人工输入并确认单集单镜 1 秒剧本，填写并核对 22 个导演字段；对白为 `这不是火烧的。`。
- z-image-turbo 分别生成角色、场景、道具参考图。人物首版缺少左领徽章，人工通过正式页面使用同一冻结模型重生成并选择第 2 版；最终锚点：
  - 人物 Asset `71a33550-9825-48b2-a5b0-05d7c1e9bb8a`，SHA-256 `94d7685a9cdd800360ad1b53fd8fdee756c2097e52ec74c7db7a5eff5e58a7f9`
  - 场景 Asset `1f5f8115-5c0e-41d5-bf4d-924d6a3ac5c5`，SHA-256 `9637cff44966b6085916705f37be5a852daffb425470b845108432b918e68a54`
  - 道具 Asset `f26a2eca-681a-4bbd-8779-290d4bb20392`，SHA-256 `829f66fd1059175a55490f225e8634ec42fd331a9dcae3a304b14ee7cc3b75c9`
- 逐镜人工绑定上述三张 Bible 图，并显式选择同故事动态参考视频 Asset `c74b0d68-d5dc-4b37-a88f-c573fec7de56`，用途为 `motion_camera_reference`；没有使用无关视频。
- PromptArtifact `a52f72bb-6d24-43b9-9808-1e916b1c2938` 使用当时冻结的 H3 PromptProfile v7；六段齐全，中文对白以 `<d>[Chinese] 这不是火烧的。</d>` 保留。该历史 Run 不重写成 v15。

### 真实媒体结果

- GenerationTask `f2def9d8-75d8-4e00-b7e6-117d341446ae`，Comfy prompt ID `8a9169b3-b8a8-4562-800a-3085fa7f09ed`。
- 生成视频 Artifact `736ed797-6d25-4590-bfac-87974244bdbf`：104,017 bytes，SHA-256 `ced82bb1bed46f0fa782645a93153ab0b2698f7bae315fc628dccccd60a08389`；FFprobe 为 H.264 352×608、24fps、22 帧、0.917 秒，并含 AAC 32kHz 双声道。
- AssemblyJob `46852766-a7f3-43f3-ac30-9f48bbde50de` 成功；逐集母版 Artifact `933bfa2d-e62d-41a2-a287-be38d1ab6e20`：413,379 bytes，SHA-256 `29116f9c2a4b62949bd6a3a833b3a5572dc95aabf840601e1f200466ae136303`；768×1344、24fps、24 帧、1 秒、AAC 48kHz 双声道。
- ReleaseBundle `b720e57e-adb9-4d16-9ed5-1b4f9979d597` 为 `sealed`，manifest SHA-256 `69ef66717b5deded9f62048476226e33205e71b98690939fd3cbdcb57221a347`。

结论：人工从业务输入、Bible 选图、逐镜引用到成片审核均有正式页面入口，媒体与交付链路通过。

## 2. 半自动线

### 操作与审核

- 使用 Recipe `e2e-ref2va-assisted-dual-clock` v4，ID `232a6ca5-b138-4f6f-ba80-1ea2a21f34ff`；外部预算 1.0000 USD，实际费用 0.110000 USD，生产任务计数 16/30。
- AI 完成剧本结构、Bible 文本候选、全剧影视规则和 21 个提示词影响字段草稿；人工逐阶段核对并修改。最终对白为 `这不是火烧的。`，时间窗 0.15–0.85 秒。
- 人物 Bible 图使用同一 z-image-turbo 模型重生成后选择 v20001；场景、道具选择 v10001。素材库搜索与筛选真实执行：一张历史“黑色门禁卡”图因包含人形阴影和红色 `43:00` 字样、与当前剧本不符而被人工拒绝，没有为了凑输入而绑定。
- 逐镜只冻结人物、场景、道具三张已批准 Bible 图。Workflow 的动态参考视频范围是 0–3，因此本镜头留空，没有塞入与剧本无关的视频。
- PromptArtifact `7522423d-e96a-48af-a5e2-bd2515bd7685` 使用 H3 PromptProfile v14，正向提示词 4632 字、详细描述审核值 354 英文词；六段、三组 `<Picture N>/<Subject N>` 和中文对白均完整。

### 真实媒体结果

- GenerationAttempt `649ed5eb-9b38-4f1a-ace3-18cadf2016c0`；GenerationTask `f0769605-1c39-4c6c-b066-4b3c777fb4f8`；Comfy prompt ID `795769b9-ba5f-497c-9548-47f174505551`。
- 实际提交到工作流 `$.6.inputs.prompt` 的文本与 PromptArtifact 完全一致，长度 4632，SHA-256 `07cc3464fb0c94fb6686ede874574eba12e5888d46f15851fcb1fb3179381991`。
- 视频 Artifact `446de950-d0a6-40bf-a8bd-055b8bf14783`：96,966 bytes，SHA-256 `9e4b37e8bf7e31bf92340640308c3bf1d23e82ac2f6fb4c851170ea7ba724460`；FFprobe 为 H.264 352×608、24fps、22 帧、0.917 秒，并含 AAC 32kHz 双声道。
- 首、中、尾帧显示同一短发女性、深色法医外套、旧电梯与黑色门禁卡；中段卡片边缘短暂暖光，末段恢复。人物、场景、道具在单镜内连续；口型有变化。
- AssemblyJob `2f617c75-f6ec-4735-8bcf-074c0549b23a` 成功；母版 Artifact `9a9c4042-8680-4242-879f-8ff5d410619d`：399,850 bytes，SHA-256 `1aa2f1aaaf94b1139b8cbd4610b336bb7998e9941316e751c07d642465083a31`；768×1344、24 帧、1 秒、AAC 48kHz 双声道。
- ReleaseBundle `0341ccdf-5fc1-4b13-bf13-590cfc92a4f8` 为 `sealed`，manifest SHA-256 `a60050bd2abfff074cb63553d2450de26e212ee630d4c8ab3e283477f4ef38fd`。

结论：AI 给草稿、人工审核/实质调整、Bible 重生成与素材库选择、H3 生成和交付的半自动路径通过。

## 3. 全自动目标线

### 自动执行与人工接管边界

- 从 0 集、0 分镜、Bible 全空的新项目创建 Run，使用 Recipe `e2e-ref2va-unattended-dual-clock` v6，ID `895b69f8-81f2-4966-8eb5-27b0eaff4622`，`interaction_mode=unattended`。
- Run 于 2026-08-28 05:16:21（Asia/Shanghai）开始，05:27:55 完成；实际费用 0.110000 USD，生产任务计数 15/30。
- `script_generate` 自动生成单集单镜剧本；对白为林霁在 0.15–0.85 秒低声说 `它醒了。`。`bible_materialize` 自动建立人物、地下种子库和青绿色种荚；`visual_rules_generate` 自动生成全剧影视规则。
- `script_normalize` 冻结了 21 个批次。前 8 个批次成功，第 9 个 Attempt `9d18017c-2c83-466f-a0fb-2ba43919b3b8` 因 `prompt_normalize_contract_failed` 失败；系统记录 `review_origin=human_after_validation_retry_exhausted`、`stop_reason=safe_feedback_unavailable`，没有继续盲重试剩余批次。
- 随后的 `script_normalize_review` 虽配置 `accept_all_valid_frozen_prompt_translations_v2`，实际 ApprovalDecision 为 `actor=human`。人工确认后，后续 Bible 生图、选图、逐镜绑定、H3 生成、选片、装配和交付均由策略自动完成。

因此：该早期 Run 数据库终态为 succeeded，但按测试目标只能判定为“有限重试/安全反馈不可用后转人工，接管后通过”；它本身不是无人值守成功。后续 v22、v23 已分别用新的空项目完成零人工复测，不能反向改写本 Run 的人工接管事实。

### Bible 与逐镜引用

- z-image-turbo v4 自动完成三类 352×608、4-step 生图并由策略选择唯一候选：
  - 人物 Asset `195bd647-d0dd-4ed7-9cc2-861b80732c31`，242,916 bytes，SHA-256 `f8ea76c8b55c3167a9e7ddd73f2cdda54691f3bf719e9a0d62cf0b209a3356bc`
  - 场景 Asset `bb92578f-4f88-415b-b592-bcbec39a517e`，357,853 bytes，SHA-256 `76685329e5444bd4b68357080f0283b3be6d3981016b6f2ed952b9afc7689f1c`
  - 道具 Asset `535ce02b-0ed2-4359-86bc-f79467c019df`，201,433 bytes，SHA-256 `3b8627ee08c2d8c8d95d56e7ad60948724e0efc1c9ce7dafad4a3840fdd8a209`
- Shot `9811f4e4-587c-4f44-9624-40a5e806da91` 的 `reference_bindings` 显式绑定上述人物、场景、道具；H3 GenerationTask 的 `staged_inputs` 恰好为三张图，source/submitted SHA-256 一致，没有视频或音频参考输入。

### H3 v15 提示词与真实提交

- PromptArtifact `f961e684-a4bf-4de2-a61a-25a210702b13` 使用 PromptProfile `minimax_h3.video.ref2va` v15，ID `754fd1cd-2358-475c-a76c-5ffddb1591ba`；合同 SHA-256 `829b5b82c0fdcb392b07990fa2d33575693ad1907785e98d487998874e7b275d`。
- 六段顺序为 `subject_definitions → summary → retention_analysis → detailed_description → overall_soundscape → non_diegetic_music`。
- `retention_analysis` 实际输出包含 `<Subject N> (appears in [Shot 1]): fully_preserved - ...`。
- `detailed_description` 在 `[Shot 1]` 前有两句英文视觉风格/色彩开场；首镜头开头立即引用 `<Subject 1..3>`，随后按 0.00–0.15、0.15–0.85、0.85–1.00 秒描述动作。
- 中文对白原样保留为 `<d>[Chinese] 它醒了。</d>`。
- PromptArtifact 共 3481 字，内容 SHA-256 `787401707706aaafcc69499ad98e75b273113313d8a0d1f8cb3840c6bbc44503`；GeneratedWorkflow `3c2f9752-f172-466d-b485-ce920efa7ded` 的 `$.6.inputs.prompt` 与其完全相同。

### 真实媒体结果与画面审核

- GenerationAttempt `16ca21bf-b9a8-443e-a588-4cdd3626eb5a`；GenerationTask `3473ad25-83ae-4773-8836-2a69e493bbc0`；Comfy prompt ID `5371019b-3312-475c-8da2-55c6b2c20235`。
- 冻结执行图为 352×608、22 帧、24fps、4 steps、`shift_video=12`、`shift_audio=3`。
- 视频 Artifact `69ead4a4-bbff-4258-8fb8-1984274cd4d6`：136,627 bytes，SHA-256 `13e34e494bca886ce13b9b688e2913977a81cae6032b853b61bd4acf0123ecbd`。独立 FFprobe：H.264 352×608、24fps、22 帧、0.916667 秒；AAC 32kHz 双声道、0.917 秒。音量探测 mean -14.0dB、max -1.3dB，确认不是静音轨。
- 首、中、尾抽帧显示同一女性、同一灰绿色实验服和地下青色玻璃储藏空间；椭圆种荚由暗绿变亮青色再恢复暗绿，完成“一次脉冲”动作。人物面部、发型、服装、场景结构和道具轮廓单镜内连续。
- 画面质量偏差：胸章沿用了 Bible 图的圆/椭圆外观，没有达到文字要求的六角形；种荚参考图已有明显内发光感且成片比例接近人物躯干，弱化了“初始不发光”和实际尺寸感。当前策略没有把这些语义偏差识别为自动选片阻断。
- AssemblyJob `1b67efa1-3ed4-47ec-9da0-59bc5fe4b52b` 成功；母版 Artifact `1f1e2e57-0bb5-41c2-bd98-9b7cf132f23c`：575,382 bytes，SHA-256 `8a72e0a7f903a55dfa166f573d6c0acf7be0bc7a79101694cf14ce33864d17d1`；FFprobe 为 H.264 768×1344、24fps、24 帧、1.000 秒，并含 AAC 48kHz 双声道。
- ReleaseBundle `b907d343-c958-4e67-b583-789994122320` 为 `sealed`，manifest SHA-256 `ad1c2151178c2a887fe57cb9e5bcfc4cfaa172b1cf310d8c67a774be429dcf90`。

## 全自动线后续零人工复测（v16–v23）

下表记录的是“从新空项目创建新 Run”的连续诊断链。失败 Run 保留为真实审计证据，没有原地改写为成功，也没有降低安全合同来制造通过。

| 轮次 | Project / Run | 实际停止点与根因 | 修复或处理 | 当前判定 |
| --- | --- | --- | --- | --- |
| v16 | Project `51bde886-2b1f-41e0-87ce-236ebb16e6a5` / Run `906a9ace-1d2c-436f-b936-f6ac8c829c92` | 已经符合目标语言的英文提示词仍被送入通用“英译英”模型合同；模型自由改写后不满足字段规范，形成无必要的外部调用与失败面。 | `translate_only` 且源文本已合规时改走 `server_deterministic`，只有确需模型生成/转换的内容才调用 Provider；批计划收敛为 schema v3。 | 阻断源已修复，保留失败审计。 |
| v17 | Project `e852ef16-864d-4fb4-8b97-40f16c3331ba` / Run `ea655def-8f2f-4281-b9a2-e564222f422c` | Provider 驱动已校验结果并恢复服务端哨兵，外层调用又把“已终态化结果”当原始 wire payload 二次校验，合法恢复后的 token 被误判。 | 引入进程内 `FinalizedPromptNormalizationResponse` 标记，绑定同一冻结 payload 的身份与内容指纹且只消费一次；普通 `dict` 仍执行严格原始响应校验。 | 重复终态校验已修复，不放宽原始响应合同。 |
| v18 | Project `387e16fe-e42e-4b0e-978d-0a6b7ddfb9a5` / Run `3c4c5877-609c-4771-ae4d-80627c4a4154` | 31 个规范化批次均成功，输出快照为 204,875 bytes；保存合同允许 8 MiB，但无人值守审核重新哈希时误用了默认 64 KiB 上限，导致同一合法快照在下游被阻断。 | 四处无人值守来源快照重哈希统一使用 `MAX_STAGE_SNAPSHOT_BYTES=8 MiB`；新增“31 批超过 64 KiB 仍自动审核”与“超过 8 MiB 仍硬拒绝”覆盖。 | 上限口径已统一，硬上限仍保留。 |
| v19 | Project `52fe6940-5b8c-428f-8657-9dc2695b6762` / Run `82a5aead-a5cd-4299-8fd7-887507ff6df8` | 第 25 个规范化批次提交前，`prompt.normalize` 的真实 smoke 恰好越过有效 TTL；能力证明已过期，系统按当前目标哈希和精确 operation 拒绝继续。 | 未降低或绕过 TTL 硬门；后续 Run 前通过正式页面重新执行真实 smoke。通用“到期自动刷新 smoke”仍未实现，不能把历史成功当当前能力。 | 属于正确的能力硬阻断；后续仍需改善长 Run 的烟测刷新体验。 |
| v20 | Project `d4c82b46-ccb3-42cd-a041-d08e3081db25` / Run `574ce4d5-420d-4fb8-ac49-aa8086c7add1` | `visual_rules.generate` 返回 `provider_response_invalid`；旧记录只留下 length/content_filter 的模糊线索，安全失败元数据没有完整持久化，无法据此安全重试，结合返回迹象推断更可能是输出长度耗尽。 | 仅持久化安全元数据（合同错误码、`finish_reason`、usage、响应 ID 的 SHA，不保存原始响应）；新增 `production.text_contract_retry_limit`（默认 2、范围 0–5），只对有完整审计的已知安全响应拒绝做有限修复重试，耗尽后转人工，content filter、提交不确定、烟测/配置漂移不重放；DeepSeek visual-rules Profile v16 将输入/输出建议阈值分别配置为 16,000 bytes / 32,768 tokens，仅 warning，硬容量为 65,536 bytes / 131,072 tokens，并关闭 thinking。 | 元数据、有限重试与容量合同已配置化；“疑似长度”明确标注为推断，不伪装成已证明根因。 |
| v21 | Project `01e5218b-bd46-4201-a7ea-4b856afc4066` / Run `3d4fc14e-a045-4d24-81ef-68beea7cd891` | 已自动生成 1 集 2 镜、两名角色、1 个场景、1 个道具，并用 z-image-turbo 生成对应 4 张 Bible 锚点图；进入 `media_generation` 时计数已为 `39/40`，而两个镜头需要同时新增 2 个视频任务。系统在原子入队前以 `generation_task_budget_exceeded` 阻断，没有只入队其中一个镜头。 | 这是用户冻结的 Run 任务硬预算，不是画质/字数建议值，也不是 Provider 故障；不能静默越过。后续 v22 按两镜完整任务计划提高显式 `max_generation_tasks`，同时保留原子预检。 | 本 Run 的前置文本、Bible 与绑定链已走通，但视频未提交；后续 v22 另行完成零人工全链。 |
| v22 | Project `da4eaef3-e299-4570-9238-130376b39bd4` / Run `dec14c59-49d7-4e3b-8c92-5c6ea5ba598c` | 从新空项目自动生成 1 集 2 镜、4 个 Bible 候选、两条 H3 视频、8 秒逐集母版和 ReleaseBundle；17/17 阶段均一次成功，7 个审批全为 `recipe_policy`，人工审批为 0。 | 显式任务预算提高到 60，实际使用 41；保留原子预算预检，没有降低其他安全/能力硬合同。仅声音角色误入视觉引用、道具初态提前发光及徽章/面部轻微漂移作为质量问题单独记录并进入修复。 | **零人工技术链路通过；视觉质量待修复后复验。** |
| v23 | Project `6b1d3df0-6f37-4539-9dfc-f8055206c971` / Run `b4dc0fd9-5ee8-456c-9d2f-c91a599f4407` | 从新空项目复验 v22 暴露的两个配置缺口；仅声音角色保留剧情/对白事实但退出全部视觉链，道具静态参考只保留初始暗态。完成 4 张有效 Bible 图、两条 H3 视频、8 秒母版和 ReleaseBundle；17/17 Stage 均一次成功，7 个审批全为 `recipe_policy`。 | `visual_reference_policy` 与 PromptProfile v3 静态投影已进入真实生产链。一次 `script.generate` 明确以 `finish_reason=length`、4096 completion tokens 被安全拒绝，按配置有限重试后第 2 次成功；未发生不确定重复提交，也未转人工。实际任务 44/60、费用 0.190000/2 USD。 | **零人工技术链路与两个配置化修复通过；跨实体引用归属、对白自然语言归属和严格跨镜人脸一致性继续作为质量优化。** |

### v21 Bible 与原子预算证据

- 两个角色 Bible 均已生成并冻结锚点：CharacterBible `2e580d61-3fff-4872-9519-098a1d0e5b41`、`345d5e37-fbb7-4b17-b96b-1d7113cc75ee`。
- 场景与道具 Bible 均已生成并冻结锚点：SceneBible `e0440381-b71a-46e7-a101-18b4549c2d0a`、PropBible `433b51fb-62e1-4021-ab47-42d31d1b470a`。
- Run 冻结 `max_generation_tasks=40`，阻断时 `generation_task_count=39`、`current_stage_key=media_generation`、`blocked_code=generation_task_budget_exceeded`。两镜视频任务在预算预检通过前不会部分创建，这避免了“一镜已花费、一镜未入队”的非原子状态。

### v22 零人工完整终态证据

#### Run、阶段与审批

- 使用与前轮相同的无人值守 Recipe `e2e-ref2va-unattended-dual-clock` v6（`895b69f8-81f2-4966-8eb5-27b0eaff4622`），从新空项目创建 Run，而不是续跑或改写 v21。
- Run 于 2026-08-28 11:09:55（Asia/Shanghai）开始，11:36:24 完成，耗时约 26 分 29 秒；状态为 `succeeded`，17/17 阶段全部成功且 `attempt_count=1`。
- `script_review`、`bible_review`、`visual_rules_review`、`script_normalize_review`、`bible_image_review`、`storyboard_review`、`artifact_selection` 共 7 个审批全部为 `actor=recipe_policy`、`decision=approve`；没有人工审批或人工接管。
- 冻结外部预算为 2 USD，实际文本费用 0.180000 USD；生产任务计数 41/60。v21 暴露的任务预算问题通过提高新 Run 的显式上限解决，原子预算预检仍保留。

#### Bible、逐镜绑定与动态参考输入

- z-image-turbo v4 以 352×608、4 steps 自动生成 4 张 Bible 候选图：林霁、地下种子库、种荚，以及一个被剧本文本标为“仅声音/画外”的同伴角色。策略为每个实体自动选择唯一候选。
- 第一镜实际绑定 4 张图片（仅声音同伴、林霁、场景、道具），第二镜绑定 3 张图片（林霁、场景、道具）；两个镜头的动态参考视频、配对音频和独立参考音频均为 0。它验证了“可变视频输入不强制绑定无关视频”的正确路径，也暴露了仅声音角色不应生成视觉 Bible 或进入 `<Picture>/<Subject>` 的过滤缺口。
- 道具 Bible 的静态生图提示词包含 `initially dark, pulses once with glow`。模型把后续动态“发光一次”提前表现到静态参考图，导致第一镜开始阶段已经偏亮；这不是 H3 工作流失败，而是 Bible 静态初始态投影合同不够严格。

#### H3 六段提示词与对白

- 两个 PromptArtifact 分别为 `47f829a3-4685-4e02-909b-2085fb0ed64a`（5707 字，SHA-256 `c2d8acaff0cb4b67c6b2310ef790d6e6ae08e7cc8ca83ef608165d2221df7731`）和 `5c126136-6326-4f40-8cea-f399a5b7b1e6`（5169 字，SHA-256 `ec96fce211f802585eafb24de1255a2aaf5d87392a2786bb110101acca331bbd`）。
- 两个提示词都按 v15 合同包含并依次组装 `subject_definitions`、`summary`、`retention_analysis`、`detailed_description`、`overall_soundscape`、`non_diegetic_music` 六段；实际 GeneratedWorkflow 使用 `minimax_h3` compiler v3、352×608、90 帧、24fps、4 steps、`shift_video=12`、`shift_audio=3`。
- 第一镜在 1.000–3.500 秒保留中文对白 `<d>[Chinese] 别碰它，它在呼吸。</d>`；第二镜在 1.500–3.800 秒保留 `<d>[Chinese] 它记得我的名字。</d>`。对白、声景和非画面音乐段均进入实际工作流正向提示词，负面提示词为空。

#### 两条 H3 视频、8 秒母版与交付

- 镜头视频 Artifact `0deff492-6e67-4a3b-b524-4a5aa9af959b`：335,978 bytes，SHA-256 `df5dc4b74cdb8293f9fe5f43dd02d1677ec17ac7cb15aa8f7b1c1168055f5a8e`；FFprobe 记录 H.264 352×608、24fps、90 帧、3.750 秒并含音频。
- 镜头视频 Artifact `a184a921-a6ab-4574-8520-d5a78af3bec1`：323,526 bytes，SHA-256 `796a5629c98469d107d6b477b3c4de72dd1e3e762e88d7032c3b4ba61fafac6e`；FFprobe 记录 H.264 352×608、24fps、90 帧、3.750 秒并含音频。两条视频均由策略选中，生成任务均只执行 1 次。
- AssemblyJob `a2ff4a40-caab-4fb0-9fcf-15c1c2cad262` 成功；母版 Artifact `576308c6-a78e-4818-a54d-e1597582e251` 为 2,865,292 bytes，SHA-256 `5e6f26dac436caf5e1d9456614f1b569f44eb77e561bdadc008d66422dde711d`。独立媒体探测为 H.264 768×1344、24fps、192 帧、8.000 秒，并含 AAC 48kHz 双声道。
- ReleaseBundle `04c281a3-4d55-48e5-89b8-2a2efc479d59` 为 `sealed`，manifest SHA-256 `a1f3d70fc341dd4e5177534b0a6184d29e66fe53e4bb50bf8345fde7c6da2222`；selection 显式冻结两条镜头 Artifact 和上述逐集母版 Artifact。

#### 技术通过与视觉质量待复验边界

- 技术通过：从空项目开始，文本生成、Bible、生图、规则、逐镜提示词、H3 双时钟视频、自动选片、装配和 sealed 交付全部在零人工条件下闭环；动态参考视频留空没有阻断；两镜均有对白和音轨。
- 视觉质量待复验：仅声音同伴生成了无关男性形象并被第一镜引用；种荚 Bible 已发亮，使第一镜初态不够暗；两镜场景、服装、主体道具和整体色调大体连续，但徽章在圆形/银色六角形之间漂移，面部也有轻微漂移。因此 v22 证明的是“零人工技术链路通过”，不是“跨镜视觉质量验收通过”。
- v22 当时待修复的两个缺口后来均已通过 v23 新 Run 复验：可配置 `visual_reference_policy` 排除了仅声音/画外角色的视觉 Bible 与逐镜绑定；配置化静态参考投影只向 Bible 生图提供道具初始状态，把后续动作留给镜头提示词。v22 本身的历史画面与绑定事实保持不变。徽章、面部及引用归属漂移继续进入可配置语义审核与人工接管，不以无限重试或新增模型专用硬代码掩盖。

### v23 配置化修复后的完整真实复验

#### Run、阶段、有限重试与审批

- 使用同一无人值守 Recipe `e2e-ref2va-unattended-dual-clock` v6（`895b69f8-81f2-4966-8eb5-27b0eaff4622`），从新空项目 `6b1d3df0-6f37-4539-9dfc-f8055206c971` 创建 Run `b4dc0fd9-5ee8-456c-9d2f-c91a599f4407`，没有续跑或改写 v22。
- Run 于 2026-08-28 13:32:20（Asia/Shanghai）开始，14:01:19 完成，耗时约 28 分 59 秒；状态为 `succeeded`，17/17 Stage 全部成功且各 Stage `attempt_count=1`。
- 7 个审批全部为 `actor=recipe_policy`、`decision=approve`，人工审批与人工接管均为 0；生产任务计数 44/60，冻结外部预算 2 USD，实际文本费用 0.190000 USD。
- `script.generate` 第一次 Provider 响应明确记录 `finish_reason=length`、4096 completion tokens，resolution 为 `known_response_rejected / response_output_budget_exhausted`。系统只对这个“已知收到且可安全判定”的响应按 `production.text_contract_retry_limit` 做有限修复重试，第 2 次成功；没有对提交结果不确定的请求重复 POST，也没有无限重试或转人工。这里的两个 GenerationAttempt 属于同一 Stage 内的受控合同修复，不改变该 Stage `attempt_count=1` 的事实。
- 这一结果也验证了“建议值只告警、硬合同才阻断”的边界：建议输出长度可以提示和继续；但当真实响应已被 `finish_reason=length` 截断、结构不完整时，不能把残缺内容伪装成成功，必须进入有限修复重试，耗尽后再转人工。

#### Bible、仅声音角色过滤与静态初态投影

- z-image-turbo v4 以 352×608、4 steps 自动生成并选择 4 张有效 Bible 锚点图：
  - 林霁 Asset `cd700308-72c1-4d34-bd03-0e3d7d5346be`：232,836 bytes，SHA-256 `15047b54a857561c4804b012a1bd743f7dd158e5825f159f750b9a40d54f3c15`
  - 极夜地下种子库 Asset `779c30d1-4281-46f1-a3eb-165563945392`：410,709 bytes，SHA-256 `7125b4ce199e36886a90a8695070064f66b3535acbcc073fe53635525ccce1d2`
  - 六角银色徽章 Asset `7b1f0f36-3225-4e8c-9144-d287574e6ac7`：267,104 bytes，SHA-256 `dbb04aee0822b26d033e7cfe07e7afc83f9869e33aaed4479c31d679cb4613d0`
  - 青绿色种荚 Asset `d48d8c10-6c7e-42ec-877d-d528c79f751f`：211,942 bytes，SHA-256 `aa9128345794f4f49ebd648674987ae348091619d9fd67f29dfe17828f1b36e0`
- 画外同伴 CharacterBible `f8f147e2-cce6-4fb9-8929-85fdbb1c2221` 的描述与 `visual_identity_en` 明确为“永不出镜/voice only”。它继续保留在两个 Shot 的 `characters` 与对白事实中，但 `visual_reference_policy` 自动判定不具备视觉资格：没有生成 Bible 图、没有进入 Storyboard 视觉 ID、没有逐镜视觉绑定，也没有进入 H3 的 `<Picture>/<Subject>`。两镜实际均为 4 张图片、0 条动态参考视频。
- 青绿色种荚静态 PromptArtifact `859b5d29-60d7-4dee-b0ef-c0871810710f` 使用 PromptProfile v3。静态正向提示词只保留 `Seed Pod Initially Dark` 与单体 canonical prop 描述；“随后脉冲发亮”的动态子句被确定性迁移到 `request.action` 审计，而不是交给 z-image 静态生图。实际参考图肉眼为暗棕黑色、没有发光，证明 v22 的“静态图提前表现后续动作”缺口已修复。

#### H3 六段合同、视觉绑定与对白

- 第一镜 PromptArtifact `cf27d7ae-29d3-49b0-b200-95413456ba11` 为 5885 字，内容 SHA-256 `d9bc417ca03e20ec795b6f395138aa1c6f9b7093086e783e96664c2e554c0863`；第二镜 PromptArtifact `bcd86404-73ea-49f5-abe2-9099c6a18803` 为 5682 字，内容 SHA-256 `9e6ddb4059fedf904a4537a190ef6ef4576e20220a7d43155a9291afc6106feb`。两者均使用 `minimax_h3.video.ref2va` PromptProfile v15，合同 SHA-256 `829b5b82c0fdcb392b07990fa2d33575693ad1907785e98d487998874e7b275d`。
- 两个实际提交提示词均依次包含 `subject_definitions → summary → retention_analysis → detailed_description → overall_soundscape → non_diegetic_music` 六段，并分别包含 4 组 `<Picture>/<Subject>`：林霁、场景、徽章和种荚。画外同伴不在 Subject 定义中，动态参考视频也未被强制填入。
- 第一镜权威对白为画外 S2 的 `<d>[Chinese] 别碰它！它会醒过来。</d>`，时间窗 0.8–3.6 秒；第二镜权威对白为 S1 的 `<d>[Chinese] 我知道，我不碰。</d>`，时间窗 0.5–3.3 秒。两句对白、speaker 和时间窗均进入服务端最终合同。
- 文本模型贡献仍有两处非阻断语义漂移：第一镜自然语言 prose 提到了下一镜 S1 的回复；第二镜一处 prose 把 `calm low` 归给画外同伴。服务端最终权威 dialogue tag、speaker 与时间窗没有被改写，因此没有升级为媒体技术硬阻断；后续应以可配置语义审核发现归属冲突，并在超过有限重试次数后转人工确认。

#### 两条 H3 视频、8 秒母版与交付

- 第一镜视频 Artifact `b68bb4bd-3a74-43f7-89f3-a37401121293`：714,436 bytes，SHA-256 `872e7d5bc983eb4b1114ffc383b7527fd1553c489395ba8d25d8b73a508c5bb2`；第二镜视频 Artifact `476c9ba6-8413-4405-b1fa-c2e04d205aad`：309,473 bytes，SHA-256 `ba7a7d1e47ef73d26845daa0105a77d057361f44507a4f4b0785190ddfec8bdc`。
- 两条视频均经独立 FFprobe 验证为 H.264 352×608、24fps、90 帧、3.750 秒，并含 AAC 32kHz 双声道；冻结工作图均为 4 steps，动态参考视频数为 0。音量探测分别为 mean/max `-14.6/-0.1 dB` 与 `-16.9/-0.3 dB`，证明存在非静音音轨，但不代表对白逐字 ASR 已验收。
- 逐集母版 Artifact `f24d9fc0-4463-4e46-bc8e-1fc834ec213d`：4,613,887 bytes，SHA-256 `6da5ffb99b2f112a0e693161d1b664864ce5c4633d1501f4ec9386f7aea938f4`；独立探测为 H.264 768×1344、24fps、192 帧、8.000 秒，AAC 48kHz 双声道，音量 mean/max `-15.9/-0.4 dB`。
- ReleaseBundle `afd817d7-d8a5-44bd-b803-961bc6fa18a3` 为 `sealed`，manifest SHA-256 `2da9c671a2fd0f520d7ea04288a0164f3a73e940ce275f478daddfe49eba91fd`。

#### 首中尾抽帧的视觉结论

- 第一镜同一女性、种子库场景和种荚主体连续；中段种荚仍可见暗态，末段变为青绿色明亮发光，实际表现为“先暗后亮”。画面没有出现额外的画外同伴，验证声音角色过滤没有损伤对白事实。
- 第二镜开头是人物手中的暗态种荚，中后段发光；画面同样没有出现额外角色。人物胸前仍有徽章，但面部大多被裁切，无法用本镜严格证明跨镜人脸一致。
- 仍需优化的画质/归属问题：六角银色徽章参考中的视觉特征被错误贴到种荚表面，人物胸前徽章偏圆。该问题不是 Workflow、媒体文件或能力合同失败，而是跨实体 reference ownership 和几何语义一致性偏差，应由可配置语义审核、自动重试上限和人工确认闭环处理。

## 安全基线与能力合同修复摘要

| 范围 | 修复后的当前合同 | 验证状态 |
| --- | --- | --- |
| Provider 外连 SSRF / DNS 重绑定 | 一个请求只进行一次经过私网/保留地址过滤的 DNS 解析，并把允许地址集冻结到 `_ResolvedProviderEndpoint`；实际 TCP 连接固定到该解析结果，HTTPS 仍使用原始主机名做 SNI 与证书校验，连接后 peer 必须属于冻结集合；Provider 传输不读取环境代理。这样消除了“校验时解析到公网、连接时重解析到内网”的 TOCTOU 窗口。 | 定向 5/5、平台基础配置/Provider 11/11 通过，并覆盖真实本地 HTTP transport。 |
| Workflow readiness / current | “已发布”不再等同“可生产”。当前 readiness 同时校验 operation/workflow mode、graph/input/execution hash、当前 Deployment/Provider/Artifact target、精确 operation 的未过期真实 smoke，以及 fixture、materialized、compiled 和输出媒体证据；缺少显式 mode 的旧 Workflow 经迁移 `0095_retire_workflows_without_explicit_mode` 退役，保留审计但不能再选作生产 current。 | 相关扩展测试 119/119、readiness 19/19、新增定向 6/6、迁移测试 2/2 通过；当前发布主路径仅保留动态 Ref2VA v5 与 z-image-turbo v4。 |
| 文本合同失败与有限重试 | 建议输入/输出阈值只产生 warning；真实响应一旦明确以 `length` 截断、结构无效，则不能作为成功结果。只有已收到并具有完整安全审计的确定性拒绝可以按配置上限修复重试；提交不确定、content filter、smoke/配置漂移不能自动重放，重试耗尽后转人工。 | v23 `script.generate` 以一次 `response_output_budget_exhausted` 加一次成功实证该边界；没有不确定重复提交或人工接管。 |
| PromptProfile / H3 能力合同 | 最终段落、字段顺序、标签、分隔符、生成模式、输出语义、逐字段可见上下文和校验规则由冻结的版本化 PromptProfile 字段规则声明；生产校验不再写死 H3 六个字段名或“前三段/后三段”的所有权。代码只保留已注册投影算法、受类型约束的 selector/validator/compiler 安全拓扑。 | v23 两条实际工作流输入均按历史 v15 合同组装六段，合同 SHA-256 一致；迁移 `0099` 随后发布 schema-v5 H3 v16，把三个模型生成字段显式限制在项目风格、当前镜头核心、当前镜头对白元数据和当前镜头关联 Bible。Provider 请求改用逐任务独立上下文子树，相关 162 项后端回归通过；v16 尚待刷新真实文本 Smoke 后做下一次外部复验。 |

上述网络、能力 current、确定性装配与失败审计边界均属于安全/能力合同，不能按“建议值只告警”的口径放宽。可告警继续的仅是描述长度、输入/输出建议阈值、画质偏好等质量建议；凭据、目标哈希、能力 smoke、网络地址、显式费用/任务预算、截断后的结构完整性和原子性属于必须阻断或转入有限修复/人工确认的边界。

## H3 官方提示词差异与 v15 修正

| 核对项 | v14 半自动实际提示词 | v15 早期全自动实际提示词 | v15 v23 实际提示词 | 当前结论 |
| --- | --- | --- | --- | --- |
| 六段顺序 | 正确 | 正确 | 两镜均正确 | 通过 |
| 正文语言 | 英文；中文对白保留 | 英文；中文对白保留 | 英文；两句中文对白保留 | 通过 |
| retention 镜头出现位置 | 缺少 `(appears in [Shot 1])` | 已包含 | 已包含 | v15 已修正并持续生效 |
| retention 分隔符 | 使用 Unicode 长破折号 | 使用官方示例的 ` - ` | 使用冻结 v15 分隔规则 | v15 已修正并持续生效 |
| 首镜头前风格开场 | 没有，直接进入 `[Shot 1]` | 两句英文风格/色彩开场后进入 `[Shot 1]` | 由 v15 字段规则投影 | v15 已修正并持续生效 |
| Subject 首次出现 | 统一在镜头描述末尾补充 | 在 `[Shot 1]` 开头引用并说明角色 | 两镜各 4 组 `<Picture>/<Subject>`；不含画外角色 | v15 与视觉资格策略均生效 |
| 对白格式 | `<d>[Chinese] 这不是火烧的。</d>` | `<d>[Chinese] 它醒了。</d>` | `<d>[Chinese] 别碰它！它会醒过来。</d>`；`<d>[Chinese] 我知道，我不碰。</d>` | 通过；自然语言 prose 的发言归属仍需语义审核 |
| 详细描述建议长度 | 审核值 354 词，ready | 模型贡献 104 词，warning | 六段总长分别 5885/5682 字；继续记录建议性质量 warning | 建议值不阻断；由画面质量复核决定是否扩写 |
| 请求时长 | 1 秒 | 1 秒 | 两镜均请求 4 秒，实际各 3.750 秒 | v23 达到官方建议下限 |

v15 不是在 H3 主逻辑里增加新的模型名分支，而是通过版本化 PromptProfile 配置 `source_projection_config` 声明：

- `retention_analysis.include_subject_shot_usage=true`
- `retention_analysis.assignment_separator=" - "`
- `detailed_description.style_opening_placement="before_first_shot"`
- `detailed_description.style_opening_max_sentences=2`
- `detailed_description.subject_reference_placement="first_clear_appearance"`

当前 v15 Profile 把前三段配置为服务器确定性投影，把后三段配置为文本模型受约束贡献，最终由确定性装配器按 Profile 顺序、标签和分隔符组装。这里的“前三/后三”来自冻结 Profile 字段属性，不是生产校验器里的 H3 专用字段名或位置分支；新增模型可以发布不同字段合同。对白、`<Picture N>/<Subject N>` 映射与服务端身份事实仍不交给模型自由改写。v23 两个实际提示词再次验证了相同 v15 合同，并分别绑定 4 组图片 Subject、0 条动态参考视频。

v23 完成后实施的迁移 `0099_prompt_profile_generation_context_scope` 没有改写上述历史证据，而是从 v15 克隆发布 v16 并切换 active Binding。v16 的 `detailed_description`、`overall_soundscape`、`non_diegetic_music` 分别冻结 `generation_context_selectors=[project.style,target_shot.core,target_shot.dialogue_metadata,target_shot.linked_bible]`；对白元数据不包含正文，正文仍由服务器确定性装配，相邻镜头和全剧范围也不会隐式进入 Provider。完整冻结上下文只留在服务器审计，实际模型请求为每个任务生成独立 `generation_contexts/N`、JSON Pointer 与 SHA-256。合同、迁移、双镜隔离、对白正文排除、显式选择、指针/哈希和篡改拒绝已通过自动化验证；由于 Profile hash 变化会使旧 `prompt.normalize` Smoke 正确失效，本报告不把这些结构测试表述为 v16 的真实 Provider/成片复验。

## 问题、处理与剩余风险

| 问题 | 证据与根因 | 处理 / 当前状态 |
| --- | --- | --- |
| Bible 审核页大量缩略图触发 429 | 签名媒体请求曾与匿名普通 API 共用限流桶；素材库默认同时展开大量历史图片 | 已把签名媒体拆为独立 `access.throttle_signed_media_rate=600/minute`；素材库默认折叠、按实体搜索、每页 12 张并延迟加载。浏览器复测筛选“黑色门禁卡”只加载 2 张且无新 429。 |
| 半自动候选库可能出现与剧本无关素材 | 历史素材只按类型进入候选，不代表语义匹配 | 保留人工预览与拒绝；本轮历史门禁卡因人形阴影和 `43:00` 文本被拒绝，没有绑定。后续可增加语义相似度排序，但不能替代人工确认。 |
| H3 v14 与官方引用布局差异明显 | retention、风格开场、Subject 首次出现位置与官方 full-reference guide 不一致 | 已发布配置化 v15，并由全自动样本实际提交文本验证。 |
| 详细描述 104 词低于 350–500 建议 | 1 秒单镜输入信息量较小；模型贡献未达到建议区间 | 当前按 `length_advisory` 记录 warning 并继续，符合“建议值不阻断主流程”；由人工根据画面质量决定是否优化策略。 |
| 早期全自动规范化转人工 | 第 9 批共享合同校验失败且 `safe_feedback_unavailable`，系统没有足够安全反馈继续模型修复 | 该 Run 正确停止自动重试并转人工，因此只能记为人工接管后完成；后续 v16–v20 修复相关链路，v22、v23 已用新空项目完成零人工技术复测。历史审批事实不改写。 |
| v16–v20 连续零人工阻断 | 先后来自无意义英译英、终态结果重复校验、大快照上限不一致、smoke TTL 和 visual-rules 响应审计/容量合同 | 除 smoke 到期自动刷新体验外均已按上表修复；smoke TTL 继续作为硬门，由后续 Run 前正式刷新。失败 Run 作为审计保留。 |
| v21 两镜视频未入队 | 39 个既有生产任务加 2 个待建视频任务会超过冻结上限 40 | 原子预检正确阻断且未产生半批任务。v22 在新 Run 中显式提高任务预算到 60，并以 41/60 完成；历史 v21 不修改，硬预算也没有降级为 warning。 |
| 仅声音角色误入 Bible 与第一镜引用 | v22 的同伴只承担画外对白，但自动建立了人物 Bible、生成无关男性形象，并成为第一镜 `<Picture 1>/<Subject 1>`；画面中没有明显出现该男性，仍造成无效生图、占位和身份污染风险 | **已由 v23 完整 Run 复验修复。** 可配置 `visual_reference_policy` 让画外角色继续保留在 Shot、对白和声景事实中，但不生成 Bible 图、不进入视觉绑定或 H3 `<Picture>/<Subject>`；两条成片也没有出现额外角色。 |
| 全自动 Bible 静态初态与后续动作混入 | v22 道具提示词同时包含“初始不发光”和“之后发光一次”，z-image-turbo 在静态参考图中提前表现了发光 | **已由 v23 完整 Run 复验修复。** PromptProfile v3 只向 Bible 生图提供初始暗态，把动态子句迁入 `request.action` 审计；实际 z-image 种荚图肉眼不发光，第一镜抽帧表现为中段暗、末段亮。 |
| 跨实体参考归属与几何一致性偏差 | v23 徽章参考的视觉特征被贴到种荚表面，人物胸前徽章仍偏圆；第二镜人脸大多裁切，无法严格判断跨镜脸一致 | 已记录到[优化台账 `OPT-RELATION-001`](optimization/README.md)，目标是模型无关的 Bible 实体所有权/附着关系合同。当前固定佩戴物优先并入人物锚点；缺关系、重复锚点和特征迁移风险只告警并进入人工审核，不把技术成功改判为失败。 |
| 文本模型 prose 的对白归属漂移 | v23 第一镜自然语言提到下一镜 S1 回复，第二镜一处把 `calm low` 归给画外同伴；根因是当时 v15 的模型任务仍可引用全量冻结生成上下文，但服务端权威 dialogue tag、speaker 与时间窗均正确 | **结构性输入隔离已由迁移 0099 / H3 v16 实现并通过自动化验证。** 每个字段只读取配置允许的当前镜头事实；对白 metadata 不含正文，相邻/全剧必须显式选择。v23 历史成片仍按非阻断质量偏差保留；v16 需刷新真实文本 Smoke 后再做一次 Provider/成片复验，不能用结构测试反向改写 v23。 |
| `script.generate` 首次输出被截断 | v23 第一次真实响应明确为 `finish_reason=length`、4096 completion tokens，输出结构不完整 | 完整记录为 `known_response_rejected / response_output_budget_exhausted`，只对此类已知安全拒绝执行有限修复重试，第 2 次成功；未对不确定请求重放，未无限重试。建议阈值仍只 warning，但截断后的残缺结构不能伪装成功。 |
| 1 秒镜头曾显示“约 5 秒动作描述过密”提示 | 质量提示曾对 `duration_ms <= 5500` 使用固定“约 5 秒”文案，实际本镜为 1000ms | 已改为按冻结的 `duration_ms` 显示真实时长（如 `1 秒`、`1.25 秒`），并补充整数秒与小数秒测试；该项继续只告警、不阻断。 |
| 对白内容未做逐字听写验收 | Prompt、时间窗、音轨、非静音和口型均有证据，但没有可靠 ASR 对成片逐字转写 | 当前只判定“对白链路与音轨存在”通过；正式内容验收仍需人工试听或独立 ASR 对账。 |

## 最终验收判定

- **纯人工：通过。** 正式页面与正式任务链完整，人工审核边界清楚。
- **半自动：通过。** AI 草稿、人工修改/确认、Bible 重生成、素材库拒绝与后续真实媒体交付均完成。
- **全自动技术链路：v22 首次通过，v23 再次通过。** v23 新空项目在 0 次人工审批下完成 17/17 Stage、2 条真实 H3 视频、8 秒逐集母版和 sealed ReleaseBundle；44/60 的显式任务预算与 0.190000/2 USD 的外部预算均未越过。早期人工接管样本和 v16–v21 失败轨迹继续保留为审计事实。
- **仅声音角色视觉过滤：v23 通过。** 画外同伴保留对白与剧情事实，但没有 Bible 图、视觉绑定、H3 Picture/Subject 或成片人物形象。
- **Bible 静态初态投影：v23 通过。** 种荚参考图保持暗态，动态动作进入镜头阶段；第一镜首中尾抽帧实际呈现先暗后亮。
- **H3 v15 官方布局：通过 v22、v23 实际提交验证。** 修正项确实进入实际 ComfyUI 输入，不只是迁移或单元测试状态；最终字段合同由版本化 PromptProfile 配置，不在生产校验器中写死 H3 六段。当前 active Binding 已由迁移 0099 切到 schema-v5 H3 v16；逐字段上下文隔离已通过结构、迁移和回归测试，但尚未刷新真实文本 Smoke 或生成新的外部成片，因此单独标记为“实现通过、真实 Provider 复验待执行”。
- **受控文本重试边界：v23 通过。** 明确的长度截断被拒绝并在配置上限内修复成功；没有不确定重复提交、无限重试或隐藏人工接管。
- **跨镜视觉质量：部分通过，仍需质量优化。** 场景、服装、主体种荚、暗到亮动作和无额外角色均有连续性；但跨实体 reference ownership 仍把徽章特征贴到种荚，人物徽章偏圆，第二镜裁切使人脸一致性无法严格判断，模型 prose 还存在非权威的对白归属漂移。技术成功与两个修复项通过不能扩大表述为严格视觉质量全部通过。

后续无需再重复验证已经由 v23 证明的“仅声音角色过滤”和“静态初态投影”基础路径；下一轮应先刷新受 H3 v16 合同 hash 影响的真实文本 Smoke，再集中复验逐镜上下文隔离、可配置的跨实体 reference ownership 与几何标记一致性。正式内容验收仍需人工试听或独立 ASR 对账，且不得把 v23 的技术成功或 v16 的结构测试扩大表述为跨镜视觉质量全部通过。
