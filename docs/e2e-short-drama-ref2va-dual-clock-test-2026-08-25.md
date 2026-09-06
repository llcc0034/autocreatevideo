# 2026-08-25 动态 Ref2VA 双时钟短剧实际操作测试

## 结论与一致性复核

- 先前两个 Codex 任务均已正常完成，无需续跑：`01a0294a-3798-7ee2-9d3c-c8091e5deab2`、`01a0344e-6664-7421-b075-727d5c62c75e`。
- 纯人工、半自动和全自动三条线均已通过当时的真实前端与正式 API/worker 链路达到 `succeeded`，均生成逐镜视频、1 秒逐集母版和 sealed ReleaseBundle。这证明了动态 Ref2VA 的提交、生成、探测、归档、装配和交付链路，不等同于多镜头实体一致性验收。
- 全自动最终 Run 从 0 集、0 分镜的新项目开始，按当时冻结的 13 阶段旧配方完成，创建后没有人工审批、人工重试或人工选片；五项审批全部是 `actor=recipe_policy`、`decided_by=null`。该事实作为历史审计保留，但旧闭包不再满足当前 Ref2VA 要求。
- 测试全程只使用动态 `video.reference_to_video` / H3 `Ref2VA`，没有回退 T2VA、FL2VA、静态图片或其他媒体工作流。
- 发现并修复了帧数上取整导致 OOM、主机探测刷新空窗、unattended UI 识别、临时 Shot UUID、无人值守标准化 canonical 物化与嵌套 ValidationError 等明确阻断。
- 另遇到一次 DeepSeek 返回非唯一 JSON 的真实供应商随机合约失败；严格解析门禁保持不变，随后从另一个真正空项目重跑成功。
- 复核确认全自动 Project `7940b1a3-d1b0-4605-9b9b-8d31a9a94705` / Run `473d43a4-73e6-412a-a161-2233312b194d` 只有 1 个 Shot，没有 `bible_image_generation`、`bible_image_review`、Bible 图片候选或实体锚点；Shot 只关联场景，未关联人物和道具，参考绑定只有一条用途为 `motion_camera_reference` 的动态视频。其成功不能证明人物、场景、道具在多个镜头间一致。
- 当时记录为“非阻断质量问题”的人物/道具缺失，按当前一致性合同应视为 Ref2VA 生产阻断：动态视频不能替代实体参考图，且未进入 Shot 关系的 Bible 实体不能被视为已经建立一致性。

## 当前一致性验收口径

本轮没有因额度受限再次触发真实 Provider、ComfyUI 或媒体任务。下一次真实验收必须新建符合当前闭包的配方与 Run，并至少包含 2 个共享同一人物、场景或道具的 Shot；单镜头只能验证媒体链路，不能验证跨镜头一致性。

完整链路为：`已确认脚本 → 文本 Bible → 模型生成或素材库选择实体参考图 → 人工/策略审核并确定唯一锚点 → 每个 Shot 绑定已批准实体锚点 + 动态参考视频 → prompt_compile 硬门禁 → Ref2VA 生成 → 跨镜抽帧比对`。验收至少核对：

- 新配方包含 `bible_image_generation → bible_image_review → storyboard_review → prompt_compile`，并配置图片 Deployment/Workflow route 与 Bible PromptProfile binding。
- 每个 Ref2VA Shot 至少绑定一张本 Run 已批准的实体图；每个被镜头使用的人物、场景、道具都有已批准参考图。全自动 AI 新剧本须从 Bible 全空的新项目开始，并要求随后物化的全部 Bible 实体至少被一个 Shot 使用。
- 两个及以上 Shot 对共享实体绑定同一已批准 anchor Asset，绑定 ID、文件 SHA-256 与审核快照一致；同时保留动态参考视频作为动作/镜头条件。
- `prompt_compile` 对缺少图片阶段、实体绑定不完整或引用未批准 Asset 的运行明确阻断，不回退为仅动态视频生成。
- 成片以共享人物五官/服装、场景结构/材质、道具外形/比例的跨镜抽帧比对为质量证据，并另行核对动作和运镜是否符合动态参考。

## 测试边界与冻结配置

- 工作流：`MiniMax H3 动态 Ref2VA 双时钟 4-step` v3，ID `aadbf9be-9a33-4507-a6d2-9629d5c3cfd0`，published/current。
- 媒体 Deployment：`43eaaff2-49ae-45c2-8d1a-2544b7a41bf5`。
- 输出规格：352×608（214,016 像素，约 0.2MP）、24fps、4 steps、22 frames、917ms。22 帧是离 1 秒最近且满足 H3 `5+17n` 合同的合法值。
- 工作流真实节点：`MiniMaxH3AudioConditioningT8` + `MiniMaxH3DualClockSamplerT8`；采样器 `shift_video=12.0`、`shift_audio=3.0`。
- 动态参考视频：Asset `67069cf0-ae08-483e-be0a-2d443e259ef9`，SHA-256 `ac1b86ed042b16655d492913a91c8abe9ed27efcb839abff3291fb01b6ddcecb`，10.125 秒 / 24fps / 243 帧；冻结用途 `motion_camera_reference`，不采用内嵌音轨。
- PromptProfile：`minimax_h3.video.ref2va` v2，ID `215a6c75-9db1-4868-be3b-c9b511721066`，合同 SHA-256 `3f3834d26ce85f81643e719e75aea90aff12b3b548da83ce97c1c2aaf43a28d8`。
- PromptProfile binding：`minimax-h3-ref2va-dynamic-v2`，ID `761aef21-4e95-4855-a9d0-ca47b5b01b8d`，exact-model，priority 200。
- 当时最终全自动 Recipe：ID `3cb7e942-a980-4262-910e-23d1f1498f23`，key `e2e-ref2va-unattended-dual-clock`，v3，definition SHA-256 `3619ac46361cda4ae31d1e46bf81f53d67ba980383b209c118cbd62668f8bd48`。失效的 v1/v2 已通过设置页退役，历史运行仍保留不可变引用；v3 缺少当前强制的 Bible 图片闭包，不得作为新 Ref2VA 一致性配方复用，必须创建并发布下一版本。
- 最终主机快照：`4d40ce0e-6c11-445e-93dc-a6134a449f2e`，正式持久探测于 14:25:14 成功。
- 最终提交前的 DeepSeek 真实烟测：`script.generate` 任务前缀 `aa76ef41` 于 14:16:10 成功；`prompt.normalize` 任务前缀 `5c56cc11` 于 14:31:07 成功。

## 三条线结果总览

| 测试线 | 项目 / Run | 人工介入边界 | 结果 | 费用 |
| --- | --- | --- | --- | --- |
| 纯人工 | Project `4b09384e-77ec-4a43-b367-058a8cd94278` / Run `636fbba8-d6c2-405a-a510-265509765705` | 人工批准分镜、动态参考绑定、成片选片 | `succeeded` | 0 USD |
| 半自动 | Project `36353f8f-13ea-46c6-900c-e94559780eed` / Run `d8aac26b-ff8d-4c8f-ab18-bc9705dd6891` | AI 给 6 项标准化草稿；人工逐项审核并实质改写 camera，再批准参考与选片 | `succeeded` | 0.01 USD |
| 全自动 | Project `7940b1a3-d1b0-4605-9b9b-8d31a9a94705` / Run `473d43a4-73e6-412a-a161-2233312b194d` | 创建前只填写需求、预算、动态视频与无人值守确认；创建后 0 次人工动作 | `succeeded` | 0.02 USD |

## 1. 纯人工操作

- Episode `ac33bb28-b0e6-4d8f-bc6f-0c7760b5e80b`；Shot `dc75427f-04f7-4999-806d-612a41283e90`；ScriptRevision `fc15cd4f-1c13-4a71-b752-aa004c388c5e`。
- 页面操作：从 confirmed ScriptRevision 创建 Run；人工批准 storyboard；核对唯一动态视频、用途与音轨策略后批准 storyboard_review；预览视频并复核 FFprobe/SHA 后批准唯一候选；assembly/delivery 自动完成。
- Media attempt `443dcbd5-f6f6-4b15-ba94-de63ebef1f12`；GenerationTask `ac55fe9d-d071-4fe2-9a18-3d75448d47c9`；Comfy request `35983881-2bbe-4230-9092-ef49e0030d41`。
- 生成视频 Artifact `2114f7bb-973b-4687-b604-75fcf3d37d86`：105,479 bytes，SHA-256 `3f587d0a8e151a8bfbe8c06e56dd09cf01c46c63faace03ad811b25f8060a7e1`；H.264 352×608 / 24fps / 22 帧 / 0.917 秒，AAC 32kHz stereo。
- Episode master Artifact `641eb4fd-ffd8-46ea-8723-e466ec7129a4`：455,241 bytes，SHA-256 `649a722592f83424d1d11a0d265fa8a1234f08d6c1b6aa66c48c82caf0091d94`；H.264 768×1344 / 24fps / 24 帧 / 1 秒，AAC 48kHz stereo。
- ReleaseBundle `cc8a478b-ce9c-4d9b-9194-a28e1a52c463`，`sealed`，manifest SHA-256 `86004de97c46dbb05dfb7720a61a3a2c11e22323b954b591f269189511cc853a`。

## 2. 半自动操作

- Episode `5b44e04c-0da5-45e5-9013-4ec40d0183d4`；Shot `c5177734-dc0d-4985-ba0f-c501e895f98d`；ScriptRevision `2e34f294-cc35-48cd-b478-8b2bf98f2fa3`。
- `prompt.normalize` attempt `f5d4dd33-1bbb-494f-9fc1-3fed0e83a96e` 成功生成 6 项候选。
- 人工逐项核对，并把 camera 实质改为：`Medium close-up at eye level; a slow right-to-left lateral move ends on the courier holding a vintage cassette recorder and looking up off-screen.`
- 动态参考与不采用参考内嵌音轨由人工确认；视频候选经预览、FFprobe 与 SHA 核对后人工选中。
- Media attempt `d22f4046-4966-4543-88fe-04b025fde5a8`；GenerationTask `49077238-ccb6-4bbb-bbee-95874ae5eefa`；Comfy request `e280365e-f5f5-4ae8-81f4-ae1ff291bcd8`。
- 生成视频 Artifact `8aca9594-b05e-4917-803a-85a0a1b39692`：138,540 bytes，SHA-256 `5060b022d6df8c5defaec975e73ed0fada2215ab8ee2069245f1ce4c71f1dd95`；H.264 352×608 / 24fps / 22 帧 / 0.917 秒，AAC。中间帧显示人物手持复古录音机并抬头，与人工 camera 草稿一致。
- Episode master Artifact `8d4c02fe-b563-462b-a6f0-47851d69aa4b`：593,179 bytes，SHA-256 `1e39f5dccbde1d40379261b7765aab4e323bd9c8a8a7b16bda16750a46a77cfd`；H.264 768×1344 / 24fps / 24 帧 / 1 秒，AAC 48kHz stereo。
- ReleaseBundle `4fec83a6-8083-4c82-a1ce-56a6cb556ce3`，`sealed`，manifest SHA-256 `b26edea0f8c50604693d4c8a3054cdc711d5d1f3a3cf375dc93b159bbe9d7e11`。

## 3. 全自动操作

### 旧闭包正式成功 Run（历史事实）

- 新项目 `7940b1a3-d1b0-4605-9b9b-8d31a9a94705` 创建后核验为 0 集、0 分镜，默认模式 `Ref2VA`。
- 表单：1 集、1 镜、1 秒；外部预算 1.0000 USD；Recipe v3；工作流 v3；动态参考 Asset `67069cf0-ae08-483e-be0a-2d443e259ef9`；PromptProfile binding `761aef21-4e95-4855-a9d0-ca47b5b01b8d`；勾选无人值守确认。
- Run `473d43a4-73e6-412a-a161-2233312b194d`，创建于 14:31:58，完成于 14:38:07，总耗时 368.95 秒，实际外部费用 0.02 USD。
- ScriptRevision `d1c42c7d-0b56-485d-9cee-26de477b62d0`；Episode `f164324a-6d8a-4a6f-83aa-eeed58249678`；Shot `daf36130-b970-4d36-a1dd-ba36ab26796a`。
- `script_generate` attempt `5b18818a-56aa-42f2-acae-42e62eb160f5`、`prompt.normalize` attempt `b828cf70-adb9-4e7c-a38c-a5fedc6562a9` 均成功。
- v2 标准化策略物化 10 项候选，`confirmed_by=null`；canonical：`S1 → Young Courier Raincoat Vigilant`、`scene:scene-e2a2ea182b16 → Old Apartment Entrance Iron`、`prop:prop-b884495d1035 → Vintage Portable Cassette Recorder`。双时钟叙事保留 3 行与 2 个换行：`[0s–1s]`、`[0s–0.5s]`、`[0.5s–1s]`。
- 当时旧闭包的五个 review 决策：`approve_single_frozen_script_v1`、`approve_frozen_bible_v1`、`accept_all_valid_frozen_prompt_translations_v2`、`bind_single_dynamic_reference_video_v1`、`accept_single_artifact_per_group_v1`；全部 `approve`、`actor=recipe_policy`、`decided_by=null`。当前 Ref2VA 基础闭包还必须包含 `bible_image_review` 的 `select_single_bible_candidate_per_entity_v1`，不能用这五项历史决定代替。
- Media attempt `6832d4cd-8920-45b4-8b5c-d2062428f6c7`；GenerationTask `1b3912a6-9b0d-48a9-826b-e513305db1ba`；Comfy request `cf6549c6-1b10-453c-ba40-42ee480c2768`；真实媒体执行耗时 328.76 秒。
- 编译语义确认 `width=352`、`height=608`、`frames=22`、`fps=24`、`steps=4`、Ref2VA、`<Video 1>`，参考素材 checksum 与 source/submitted checksum 均冻结；执行图确认为双时钟 sampler。
- 生成视频 Artifact `a627bd74-1d9d-4595-9f25-3b13977e71b0`：
  - 文件：`backend/media/generated-artifacts/7/7940b1a3-d1b0-4605-9b9b-8d31a9a94705/1b3912a6-9b0d-48a9-826b-e513305db1ba/0/variant-1_00001_.mp4`
  - 152,033 bytes；SHA-256 `ee4dba59b8ebface866b780206864da8b19f38c8f693f4d530add0e188be31e6`。
  - 独立 FFprobe：H.264 352×608 / 24fps / 22 帧 / 0.916667 秒；AAC 32kHz stereo / 0.917 秒。
- Episode master Artifact `784675f2-f4c9-4c59-9173-72d88313fa35`：
  - 文件：`backend/media/assemblies/7/7940b1a3-d1b0-4605-9b9b-8d31a9a94705/episode-0001-accf18e0-4941-4e3d-9da6-6b87e32ffdec.mp4`
  - 661,107 bytes；SHA-256 `39fc1204f91419391a6c7fb0bce32f67a16ad2893e56974642cfe9172b9b474d`。
  - 独立 FFprobe：H.264 768×1344 / 24fps / 24 帧 / 1 秒；AAC 48kHz stereo / 1 秒。
- ReleaseBundle `2f56c127-094a-4076-9371-28e6a9cee229`，`sealed`，manifest SHA-256 `c8de3ecc82ee2a1af181c836ea580659174e9673639700a606176bf74509028d`。
- 0.45 秒抽帧显示雨夜旧公寓铁门、湿地与昏黄顶灯，符合 environment；未出现快递员和录音机。由于只有一个 Shot 且没有 Bible 图片/实体绑定，该结果只证明单镜环境与媒体链路，不构成跨镜一致性证据。

### 全自动失败样本与处置

1. Run `f9b5667d-3a0f-43dd-bcd3-59c1b95145ad`：旧 v1 Recipe 缺少 `script_normalize` / review，语言门禁在 `prompt_compile` 正确阻断；已取消，实际费用 0.01 USD。
2. Run `45f26469-d2d1-4c55-80da-e03fd835fea2`：v1 normalize policy 将 `S1`、`scene:{slug}`、`prop:{slug}` provisional ID 直接送入严格 canonical validator，且嵌套 Django ValidationError 产生二次异常；修复为显式 v2 policy 后，用原 10 个冻结候选做外层事务回滚验证，10/10 物化、换行不变、DB 0→0；该历史 Run 保持失败，费用 0.02 USD。
3. Run `825c7fe1-76ce-4baf-b9d9-19ccd5b3004a`：`prompt.normalize` attempt `bc79cf5f-fb63-4f7e-9dfe-6ac6bff20aef` 已跨过真实 durable 提交边界，冻结请求 12 items / 7 protected tokens / 10,937 bytes / `max_tokens=4920` 均合法，但 Provider 返回非空且不是唯一合法 JSON 文档；状态 `provider_response_invalid`，费用 0.02 USD。最近同部署 smoke 连续成功，严格 parser 的 2 项定向测试通过，因此不放宽为 brace extraction；从新的空项目重跑后成功。

## 问题、根因与处理

| 问题 | 根因 / 证据 | 处理与验证 |
| --- | --- | --- |
| Ref2VA prompt 缺少 `<Video 1>` | Profile v1 漏掉 `request.reference_assets` | 通过设置页发布 Profile v2；三条正式 Run 的编译图均冻结 `<Video 1>` |
| 1 秒编译为 39 帧并在 8GB GPU OOM | 对齐函数一律向上取 `5+17n` | 改为离目标最近的合法帧数；1000ms@24fps → 22 帧/917ms；34 项定向测试与三次真实生产通过 |
| 配置定义最小值无法从 1000 改为 900 | 已有 revision 使用该结构，服务端禁止原地改 Schema | 未绕过审计改 DB；修复帧对齐根因并保留 1 秒业务事实 |
| 半自动阶段出现 `host_capability_snapshot_missing` | capability snapshot 在媒体提交前过期，刷新任务存在空窗 | 修复探测调度提前刷新与零 attempt 恢复；本轮手工正式 probe 得到新快照后重跑通过 |
| unattended 页面未显示全自动确认 | 前端错误用通用 schema 版本代替 `interaction_mode` 判断 | 改为读取 `interaction_mode=unattended`；17 项前端定向与类型检查通过 |
| 临时 Shot ID 作为 UUID 导致 500 | 前端提交临时标识，后端查询边界不足 | 前端不再提交临时 ID；后端无效 episode/shot/status 返回 400；定向测试通过 |
| v1 unattended 未标准化 / v1 canonical 无法确认 provisional | 配方闭包和 policy 语义不完整 | 当时新建 schema v2 的 13 阶段配方并将 policy 升为 v2，严格按数据库权威 provisional 精确 one-pass rewrite；该历史修复不包含后来新增的 Bible 图片闭包，12/12 定向、18/18 相关回归与真实快照回滚通过 |
| 嵌套 ValidationError 掩盖真实错误 | Django 不接受原实现的嵌套 dict error_list 形态 | 改为内层带 code/params 的 ValidationError；稳定错误码测试通过 |
| 一次 Provider 非唯一 JSON | 外部模型随机返回不合约 content；原始正文未持久化，无法进一步区分前后 prose 与截断/畸形 JSON | 保持 fail-closed 和费用审计，不放宽解析器；刷新烟测后从新空项目零人工重跑成功 |
| 全自动成片缺少人物、道具、动作，且没有跨镜一致性证据 | 旧配方无 Bible 图片生成/审核；项目的 3 个文本 Bible 实体均无 anchor/reference images；唯一 Shot 未关联人物/道具，绑定中只有动态视频；最终 PromptProfile trace 也只有 environment/camera/style/reference_assets/negative | 当前已把 Ref2VA 的 Bible 图片阶段、逐镜实体绑定和 prompt_compile 复验改为硬门禁；无人值守还拒绝未被任何 Shot 使用的 Bible 实体。历史 Run 保持 succeeded 审计，但只归类为单镜媒体链路 smoke；待额度恢复后按至少 2 镜共享实体的新口径重测 |
| 两份新增前端测试未接入统一检查 | `savedWorkflowImport.test.ts`、`taskGrouping.test.ts` 已存在，但 `package.json` 与 `scripts/check.ps1` 漏掉调用 | 补充两个测试脚本并接入统一门禁；两组 15/15 复跑通过，现有 17/17 个 `*.test.ts` 均已覆盖 |

## 代码与回归验证

- H3 frame alignment、动态 Ref2VA 与 semantic compiler：34 项定向通过；semantic compiler 全量 11 项通过。
- unattended v2 canonical / rollback：12/12；normalization + unattended 相关回归：18/18。
- 完整 Django 回归：981 项，733.396 秒，`OK (skipped=2)`；两个跳过项均为 SQLite 无法执行的 PostgreSQL `select_for_update` 并发锁测试。
- 前端：`vue-tsc -b` 与生产构建（1770 modules）通过；17 组 Node 测试合计 157/157，零失败、零跳过。
- strict JSON envelope parser：2/2。
- `manage.py check`、`manage.py check --deploy --fail-level WARNING`、`makemigrations --check --dry-run` 均通过。
- 动态 OpenAPI 生成校验无警告，且与根 `openapi.yaml` 精确一致；`git diff --check` 无空白错误。
- 当前剩余验证边界：生产 PostgreSQL 专属并发锁用例未在本次开发 SQLite 环境执行；Provider 偶发非唯一 JSON 已保持 fail-closed，并由新空项目成功重跑证明主链路可恢复。额度受限期间没有执行“至少 2 镜共享实体 + Bible 图片锚点 + 动态参考视频”的新真实一致性验收，因此不能把本报告中的旧成功 Run 宣称为该项已通过。
