# 2026-08-26 动态 Ref2VA 双时钟短剧补充实测

## 结论

- 2026-08-25 的纯人工、半自动、全自动三条真实链路均已完成；原始过程、ID、媒体哈希和失败样本继续以[历史报告](./e2e-short-drama-ref2va-dual-clock-test-2026-08-25.md)为准，不改写历史事实。
- 本轮针对旧全自动样本“只有 1 个镜头、没有 Bible 参考图、不能证明跨镜一致性”的缺口，从一个 0 集、0 分镜、Bible 全空的新项目重新执行了完整全自动链路。
- 新 Run `7c06c10b-4dd8-4a19-ac78-7921371bcde7` 的 17/17 个阶段全部 `succeeded`。3 张 z-image-turbo Bible 图、2 条 H3 动态 Ref2VA 视频、1 条 2 秒分集母版和 1 个 sealed ReleaseBundle 均为真实产物；签名与在线文件完整性验证通过。
- 两个镜头共享同一人物、场景、道具锚点，抽帧显示服装/发型、站台结构和发光怀表一致。镜头 2 的“举表、暖光照脸、远处人影”符合叙事；镜头 1 把“拾起”表现成逐步放下，属于动作方向质量偏差，不影响技术链路通过，但不能记为叙事完全通过。
- 视频生产全程只使用 H3 `video.reference_to_video / Ref2VA` 双时钟工作流，没有回退到 T2VA、I2VA、FL2VA 或其他视频工作流。z-image-turbo 只负责 Bible 图片，不是视频回退路径。

## 三条线总体口径

| 测试线 | 实际运行 | 当前结论 |
| --- | --- | --- |
| 纯人工 | Project `4b09384e-77ec-4a43-b367-058a8cd94278` / Run `636fbba8-d6c2-405a-a510-265509765705` | 2026-08-25 实跑成功；人工审核分镜、动态参考和选片。 |
| 半自动 | Project `36353f8f-13ea-46c6-900c-e94559780eed` / Run `d8aac26b-ff8d-4c8f-ab18-bc9705dd6891` | 2026-08-25 实跑成功；AI 给规范化草稿，人工逐项审核并实质改写 camera。 |
| 全自动（旧闭包） | Project `7940b1a3-d1b0-4605-9b9b-8d31a9a94705` / Run `473d43a4-73e6-412a-a161-2233312b194d` | 媒体链路成功，但仅 1 镜、无 Bible 图片；只算历史 smoke，不算一致性验收。 |
| 全自动（本轮闭包） | Project `bb441f90-679f-4981-acb7-cf6f2672b476` / Run `7c06c10b-4dd8-4a19-ac78-7921371bcde7` | 17/17 阶段成功；2 镜共享 3 类实体锚点；一致性与交付闭环通过。 |

本轮没有再次产生纯人工和半自动媒体费用；这两条线引用前一日已经完成并独立探测过的真实产物。本轮新增的真实算力验收集中在补齐后的全自动一致性闭包。

## 本轮纯人工复测进行中 / 失败证据

本节记录 2026-08-26 晚间新增的纯人工复测，不改写上表 2026-08-25 已完成三条线及本轮全自动闭包的历史结论。新增复测 Project `e7cb2d1a-dbd1-4e87-b223-a14e1049c5a7`、Run `2b10618a-ea18-4540-b16d-e6840c0a24e2` 已完成人工 Bible 候选生成、重新生成、选图、Bible 审核、分镜编辑和提示词编译，但 5 秒视频阶段没有生成可验收产物；因此该新增纯人工复测仍未完成，也不能据此宣称当前三条线已重新跑完。

### 本次冻结输入

- 视频输出仍为 352×608（约 0.2 MP）、24fps、124 帧 / 5167ms、4 steps、双时钟 `shift_video=12 / shift_audio=3`、原生音频。
- 三张 Bible 图分别为人物 Asset `accdf6d9-1382-4284-9d69-f64674310f57`、场景 Asset `7420ec17-c9fb-441f-bf30-bfdff71d5589`、道具 Asset `119049da-bba9-4b96-a7be-b4fb6bef716f`。
- 动态参考视频为 Asset `c74b0d68-d5dc-4b37-a88f-c573fec7de56`，352×608、24fps、52 帧 / 2167ms；冻结用途为 `motion_camera_reference`，不提交其音轨。
- 两个镜头均包含对白或画外音、声景与非画内音乐描述。镜头 1 GenerationTask `fcb6d097-d1cc-4ccb-b063-e82817939ab0`，镜头 2 GenerationTask `9db85988-a2fd-4b1f-ba84-2bb077e66cd9`。

### 失败时间线

| 样本 | 冻结条件与过程 | 终态证据 |
| --- | --- | --- |
| 当前镜头 1 | Comfy prompt `8492b89c-3054-4f4b-8291-2045210a5581` 于 21:07:34 进入节点 10 `SamplerCustomAdvanced`；节点在模型初始化阶段没有 step 级进度回调 | 平台在 21:39:00 达到 30 分钟 idle deadline 后两次发送 interrupt，ComfyUI 于 21:39:46 记录 `Processing interrupted`，总耗时 32 分 11 秒，无输出。精确远端 prompt 当时仍在运行，故这是 idle 判定误杀，不是模型主动失败证据。 |
| 当前镜头 2 | 第一条被中断后，同一 ComfyUI 进程于 21:40:02 立即领取第二条；21:41:19 完成 MiniMaxH3 模型准备并停留在 sampler 0% | `.runtime/logs/20260826-150952-comfyui.err.log` 在约 22:26:53 写入 `Fatal Python error: Aborted`。栈顶位于 `comfy_kitchen/backends/eager/quantization.py:1222` 的 `_op_int8_linear`，并经过 KJ `minimax_block_lowmem_forward` 与 H3 双时钟 sampler；进程退出且无输出。 |
| 既往 124 帧完整输入 1 | Task `b6914765-1a7e-48db-a769-59571fca91b0`，3 图 + 动态视频 + 对白 | 节点 10 明确 `torch.OutOfMemoryError`：已分配 6.07 GiB，再申请 255.94 MiB，CUDA free 为 0。 |
| 既往 124 帧完整输入 2 | Task `8c4aa0ae-80eb-4fed-965b-1ff87054c95e`，3 图 + 动态视频 + 对白 | 节点 10 明确 `torch.OutOfMemoryError`：已分配 5.83 GiB，再申请 255.94 MiB，CUDA free 为 0。 |

四个 124 帧“3 张 Bible 图 + 动态视频 + 对白/原生音频”样本目前为 **0/4 通过**：两次标准 OOM、一次 idle 误杀、一次 native abort。当前任务已把动态参考从 124 帧缩短到 52 帧，并把提示词从既往约 961/982 词降到约 815/823 词，仍未得到成功产物；不能把继续缩短参考视频或压缩少量提示词描述成已验证解决方案。

### 22 帧对照证据

- Task `dcb5b1f9-5b53-4706-aee1-11ddc08d2bbb` 与 `0f560c0a-7aca-4275-826f-03d6d80cf43c` 使用相同 Workflow v3、352×608、24fps、22 帧、4 steps、双时钟、3 图 + 动态视频，分别在约 431 秒和 444 秒完成；FFprobe 均确认 22 帧 / 917ms 且存在音轨。
- 上述两个 22 帧样本的提示词只有约 116/124 词且没有对白；另有两个“22 帧 + 3 图 + 长提示词/对白但无动态视频”样本成功。当前尚无“22 帧 + 3 图 + 动态视频 + 完整六段提示词 + 对白”全部同时成立的成功证据。
- 历史 22 帧成功样本使用的源参考视频为 1056×608、243 帧；节点按输出长度截取到 22 帧。当前 352×608、52 帧参考视频更小，因此不能把本次失败归因于当前参考视频文件比历史样本更大。

### 根因分级

**已确认**：本次进程丢失的直接表现是 native abort，不是普通 Python 异常；既往同类 124 帧请求已两次在同一 `INT8 linear` 路径 OOM。本机 `comfy_kitchen` eager 实现以固定约 256 MiB 的 float32 chunk 做 INT8 scaling，正好对应两次 255.94 MiB 失败申请；`MiniMaxLowVRAMAttention.head_chunks=4` 只切分 attention，不能直接消除 MLP/INT8 linear 峰值。Windows Application/System 在 abort 时间窗内没有 Application Error、WER、资源耗尽、`nvlddmkm`、Display reset 或 WHEA 事件，也没有 crash dump。

**推断**：124 帧输出 latent、参考视频 latent、三张参考图和长提示词共同把 8GB GTX 1070 推到不稳定边界；第一条在 native 运算中被 interrupt 后没有记录“卸载全部模型”，同进程立即执行第二条可能进一步放大了 offload 状态风险。这些是证据支持的高可信推断，不等于已定位到唯一底层缺陷。

**未知**：当前没有证据解释 native runtime 为什么选择 `abort()` 而不是再次抛出 OOM，也没有证据证明某个 DynamicVRAM headroom 数值能让 124 帧完整条件稳定通过；更大显存主机的精确最低门槛同样尚未实测。

### 后续验收口径

1. GTX 1070 8GB 上若继续诊断，最小增量应使用冷进程、GPU 独占、单任务、22 帧、相同 3 图和当前动态视频，再加入可在 917ms 内完成的极短对白及完整六段结构；成功也只证明 22 帧诊断条件，不能冒充 5 秒验收。
2. 任一 H3 interrupt、OOM 或 native abort 后，不在同一 ComfyUI 进程自动执行下一条，也不盲目自动重试；先核对精确 prompt 的 queue/history 并保留失败事实。
3. 5 秒 / 124 帧完整验收应迁移到显存明显大于 8GB 的独占主机，并用精确的 3 图 + 动态视频 + 对白组合重新执行 real smoke。16GB 以上只是当前保守主机建议，不是已经验证的最低门槛。
4. 平台应以模型无关的主机容量证据包覆盖真实生产参数，不能让 22 帧或少素材 smoke 自动授权 124 帧完整输入；该未落地方案记录在[优化台账 OPT-CAPACITY-001](./optimization/README.md)。

## 冻结配置

### H3 动态参考生视频

- Workflow v3：`aadbf9be-9a33-4507-a6d2-9629d5c3cfd0`，published/current。
- Deployment：`43eaaff2-49ae-45c2-8d1a-2544b7a41bf5`。
- PromptProfile v3：`527bc15f-722d-4991-9752-3f24946d26e1`。
- Binding：`0e592051-f86b-4d5c-ba47-6b6714f62043`，exact-model、priority 300。
- Profile contract SHA-256：`e36a43589f57c0fa657c7c252b3c0b0ce8a31ea91190bdcd50ec765ee2692f0e`。
- 语义门禁：正向 `shot.narrative` 必须 `required=true` 且 `omit_if_empty=false`。
- 每镜：352×608（214,016 像素，约 0.2 MP）、24fps、22 帧、H3 4 steps、双时钟 `shift_video=12 / shift_audio=3`，业务时长 1000ms、合法帧实际时长 917ms。

### z-image-turbo Bible 图片

- Workflow v2：`a3c0b172-b194-42b5-9b8d-9895957c36b8`，published/current。
- Deployment：`b8d85c53-df63-46b3-b2fd-89669f16e676`。
- PromptProfile Binding：`d5937c4f-cc6d-4254-8999-a3a836112977`。
- 每张：352×608、9 steps。这里的 9 steps 是独立的图片工作流参数；用户要求的 4 steps 用于 H3 视频阶段，二者不混用。

### 全自动配方与参考视频

- Recipe v4：`983f2cd4-37eb-4914-869e-1b7b467ca6ae`，key `e2e-ref2va-unattended-dual-clock`，schema v2 unattended，共 17 个阶段。
- 动态参考视频：Asset `67069cf0-ae08-483e-be0a-2d443e259ef9`，10.125 秒、24fps，冻结用途 `motion_camera_reference`，`use_embedded_audio=false`。
- 该视频是历史烟测素材，画面内容与本剧无关；原生 Ref2VA 实际会读取完整视频，`motion_camera_reference` 只是提示词意图，不构成内容隔离。因此该次结果只证明链路可执行，不能证明正式剧情参考正确；新生产必须改选同故事且人物、场景、道具不冲突的素材。

## 实际操作过程

1. 在正式页面创建 Bible 全空的新项目，并输入“林默在雨夜废弃车站发现发光怀表”的 1 集 2 镜故事要求。
2. 选择 Recipe v4、H3 Workflow v3、H3 PromptProfile v3 Binding 和 z-image-turbo Bible Binding；显式选择唯一动态参考视频，关闭参考视频内嵌音轨，确认外发与全自动策略。
3. 创建时服务端重新执行 Readiness。期间发现 `prompt.normalize` 缺少当前真实 smoke，先在系统设置页执行真实 `prompt.normalize` 和 `script.generate` smoke；两者成功后重新创建。
4. AI 生成并策略审核剧本；确定性物化人物“林默”、场景“雨夜废弃车站站台”、道具“发光怀表”；AI 生成并策略审核全剧影视规则。
5. 生成 2 个 Shot。镜头 2 在剧本文字中使用“怀表”简称，修复后的保守唯一匹配将其正确绑定到“发光怀表”；歧义时仍 fail closed。
6. z-image-turbo 为人物、场景、道具各生成 1 张候选图；策略分别选为唯一锚点，并物化为受管 Asset。
7. 两个 Shot 均绑定同一人物 Asset、同一场景 Asset、同一道具 Asset，以及同一条显式动态参考视频。
8. PromptProfile v3 为两个镜头分别编译不同的 `narrative:`，同时冻结相同的 `<Picture 1..3>` 与 `<Video 1>`。
9. H3 串行生成两条 4-step 双时钟 Ref2VA 视频；单候选策略自动选片，随后自动装配 2 秒分集母版并封存 ReleaseBundle。
10. 运行完成后独立执行磁盘大小/SHA-256、FFprobe、抽帧、音量存在性和 ReleaseBundle live verification 核对；ComfyUI 最终 running=0、pending=0。

运行从 2026-08-26 01:40:28 到 01:59:55，总耗时约 19 分 27 秒；8 个生成任务，实际费用 `0.03 USD`。7 个审核决定全部为 `actor=recipe_policy`、`decided_by=null`，运行创建后没有人工批准、人工选片、人工重试或取消。

## Bible 锚点与逐镜绑定

| 类型 | Bible / Asset | 文件事实 |
| --- | --- | --- |
| 人物 | 林默 `fff3cebe-f9fb-4168-b903-8a9768240199` / `c6607015-b9f8-4c35-affc-00973044cfeb` | 300,045 B；SHA-256 `66f7cebe3ee4a801672a0bddd4c4f321d6fada6624c021a051d325519aff4354` |
| 场景 | 雨夜废弃车站站台 `aa99a399-f483-4f60-91d4-486343f1b452` / `677cb804-c270-4bf8-971a-8f6d7c8e476e` | 345,875 B；SHA-256 `c8fa4ff097e6a670a44e09106d6bb29a415a88f5520e48512e75f998b5d6a6f8` |
| 道具 | 发光怀表 `0ed99f33-efcd-41bb-92ec-3a76fafe54a8` / `e9fcc180-7a4b-42f7-aaff-9377360e9d8a` | 298,326 B；SHA-256 `9716c7c541cda4eb5bf75e1bace23e24c614fd8ab3041f2785e6ea780f7a230a` |

Shot `bf9332cb-30fc-491c-b18a-08e766b0fde8` 与 `ea2b5d1d-9866-46c6-86e4-d14f7d1be6fd` 的 character、scene、prop、video 绑定 ID 完全相同，且 ReleaseBundle 中两条视频的 `source_media` 继续冻结相同的源/提交 SHA-256。

## 媒体与交付证据

| 产物 | ID / 路径 | 探测事实 |
| --- | --- | --- |
| 镜头 1 视频 | Artifact `d40b4930-fc80-4aac-87a4-4936d5fa0aee`；`backend/media/generated-artifacts/7/bb441f90-679f-4981-acb7-cf6f2672b476/dcb5b1f9-5b53-4706-aee1-11ddc08d2bbb/0/variant-1_00001_.mp4` | 127,845 B；SHA-256 `5b893669d9500dadd9c1e108aa112c5c3c3951a593b5730b9963c53db0cc04f4`；H.264 352×608 / 24fps / 22 帧 / 0.917 秒；AAC 32kHz stereo。 |
| 镜头 2 视频 | Artifact `aa9655d5-282d-4683-82ff-deb1ad4ade3b`；`backend/media/generated-artifacts/7/bb441f90-679f-4981-acb7-cf6f2672b476/0f560c0a-7aca-4275-826f-03d6d80cf43c/0/variant-1_00001_.mp4` | 96,624 B；SHA-256 `54275727285c38820cf92b63c4a1f7928fff9226045690ca97f8c36641ce91c6`；H.264 352×608 / 24fps / 22 帧 / 0.917 秒；AAC 32kHz stereo。 |
| 分集母版 | Artifact `fbf571a4-3055-4e54-9c58-6112db11d9e2`；`backend/media/assemblies/7/bb441f90-679f-4981-acb7-cf6f2672b476/episode-0001-7ecfe894-03b1-47c6-a0c8-5508807de3cf.mp4` | 941,302 B；SHA-256 `9ee0d650adcf7b3627ab42e07a1b7fb3a0b38ffc5d798575743eab192aa586c9`；H.264 768×1344 / 24fps / 48 帧 / 2.000 秒；AAC 48kHz stereo。 |

- AssemblyJob：`7ecfe894-03b1-47c6-a0c8-5508807de3cf`，`succeeded`、`dry_run=false`、`has_audio=true`。
- ReleaseBundle：`c5a6ae29-f3a7-4d13-b402-c2945323db5a`，`sealed`。
- Manifest SHA-256：`d5fd346f6752b2c8258a2d048a60ee174303430f7a0273ed9f85139d1d26bc99`。
- Live verification：`signature_verified=true`、`live_verified=true`。
- 音轨不是静音文件：镜头 1 / 镜头 2 / 母版平均音量分别约为 -30.3 / -24.3 / -26.8 dB。

## 抽帧质量复核

- 人物：两个镜头均为短黑发、深色系带风衣的同一女性形象；没有出现参考视频里的室内人物或房间语义。
- 场景：雨夜旧站台、暖色雨棚灯、蓝黑轨道和湿地反光在两镜保持一致。
- 道具：两镜均为圆形暖光怀表，外形、尺寸和光色连续。
- 镜头 1：人物从站立逐步弯腰，并把手中怀表降低到湿地；与“从地面拾起”方向相反，记为部分符合。
- 镜头 2：人物把怀表举到眼前，暖光照脸，远处出现人影；叙事和构图均符合。
- 跨镜一致性：通过；逐镜动作语义：1/2 完全通过，1/2 部分通过。

抽帧审计文件位于 `.runtime/e2e/20260826-full-auto-rerun/`，包括两个镜头和 2 秒母版的 contact sheet。它们是本地验收证据，不是正式发布资产。

## 本轮发现、处理与剩余优化

| 问题 | 根因 / 证据 | 处理与结果 |
| --- | --- | --- |
| AI 新建入口先创建空项目，再因全自动缺少动态视频/确认而 400 | onboarding 没有 schema v2 unattended 的显式 reference video 选择和确认字段 | 已修复：创建项目前先校验，要求唯一受管视频和全自动确认，固定 `useEmbeddedAudio=false`，不自动猜选素材。 |
| Run 创建被 `prompt_normalize_smoke_required` 阻断 | 配方冻结的 DeepSeek Deployment 缺少当前 durable/fresh smoke | 通过设置页真实执行 `prompt.normalize` 与 `script.generate` smoke 后恢复；未绕过门禁。 |
| 生成 seed 可能超出 SQLite/跨端安全整数范围 | 原随机上界过大 | 已收敛到 `1..9007199254740991` 并补回归。 |
| 镜头 2 的“怀表”简称未绑定“发光怀表” | 分镜拆解只做完整字符串匹配 | 已加入保守唯一核心名匹配；唯一时绑定，歧义时 fail closed；本 Run 两镜道具绑定相同。 |
| 视频提示词没有逐镜叙事 | 旧 Profile 只依赖 subject/action 等可空字段 | 已发布 Profile v3，并在发布、兼容 Binding 与 Readiness 三处强制 `shot.narrative` 正向 required/non-omit。 |
| API 返回字符串数组详情时页面只显示通用错误 | 错误展示层只接受对象详情 | 已安全支持 `details: string[]`，同时过滤 HTML、trace、对象和敏感内容。 |
| Run 详情把正式产物角色显示为“未识别” | 双语文件角色映射缺少 `episode_master` 与 `primary_generation_output` | 已映射为“分集成片母版”和“主生成产物”，保留括号机器值。 |
| 与剧本无关的动态参考视频出现在模型输入 | 本轮明确使用历史 smoke Asset，只验证动作/运镜输入链路 | 没有观察到室内人物/房间语义泄漏；但正式生产必须使用逐镜匹配素材，已在新建页和指南明确提示。 |
| 镜头 1 动作方向反转 | 1 秒/22 帧短时长下，H3 对“拾起”时序理解不稳定 | 不篡改结果；记录为质量优化。建议使用与“拾起”动作匹配的动态视频、提高动作方向描述强度，并在半自动/人工线审核后重生成。 |
| 三张 z-image 图片的发布分发状态为 failed | `Task model cannot be resolved to a managed distribution profile` | 本地受管 Asset、Bible 审核、下游 Ref2VA 和 ReleaseBundle 均成功，不阻断生产；需要补 z-image 的受管 distribution profile 后再验证对外分发。 |
| 英文 canonical 名称较机械 | 当前安全派生优先保证唯一、短且不残留 provisional token | 不影响执行与一致性；记录为文案质量优化，后续可用项目 glossary 提供更自然的受控英文名。 |

待优化项已同步到 `docs/optimization/README.md`；明确阻断项已经修复，历史失败/不完整 Run 保持审计事实，不删除、不伪装成功。

## 验证范围与边界

- 已验证：真实 Provider smoke、真实 z-image-turbo 3 图、真实 H3 2 视频、磁盘大小/SHA、FFprobe、抽帧、音轨存在性、Assembly、sealed ReleaseBundle、签名/live verification、ComfyUI 队列清空、正式页面 17/17 阶段终态。
- 已验证：页面/API/数据库冻结的 PromptProfile v3、两个不同 narrative、相同 3 图 + 1 视频绑定，以及 7 项 `recipe_policy` 审核事实。
- 历史人工与半自动线没有在本轮重复消耗媒体算力；其真实结果引用 2026-08-25 报告。
- 本轮开发数据库仍为 SQLite；PostgreSQL 专属 `select_for_update` 并发锁测试不在本地 SQLite 执行。
- “没有明显语义泄漏”来自本轮抽帧观察，不等于任意无关参考视频都安全。正式生产仍应按镜头选择匹配的动作/运镜素材。
