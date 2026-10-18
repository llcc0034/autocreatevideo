# 项目字段翻译与创作辅助

本文说明当前项目字段的独立翻译入口，不把翻译候选当作短剧生成、媒体执行或生产就绪证据。实施记录与未完成验收见[统一工作包](optimization/records/execution-unification/2026-09-25.md#current-work-packages)。

## 唯一调用路径

在原项目/分镜/Bible内容审核入口选择已经保存的字段，读取服务端目录并明确选择已发布的 `prompt.normalize` 执行实例版本。请求提交精确实例与字段来源，不再直接挑选裸Deployment UUID，也不使用模型名固定枚举或隐藏默认部署。

`ProjectTranslationRequest/Source`关联原`AssetGenerationJob/ExecutionPreparation`，沿原文本worker异步执行；保存、字段确认、继续执行和结果采用分别是明确动作。没有第二套翻译队列，保存Bible不自动调用模型。

本地/外部连接、能力、语言、输入和输出格式由所选精确合同决定；外部调用必须明确授权，资源提醒沿系统配置和原准备确认，不沿用旧“仅loopback、页面无外发授权”的独立路径说明。目录或准入失败如实提示，不回退其他模型或旧缓存。

## 操作与结果

1. 选择已保存的字段，核对原文、范围和当前候选修订；超过批次数量由用户调整，不自动拆批外呼。
2. 选择执行实例并按其合同填写必要参数/语言与外发授权；切换实例须重新核对输入和授权。
3. 保存后从“查看翻译任务”恢复原准备，确认字段及资源提醒，再明确继续。
4. 成功后阅读实际候选与原文差异，明确采用；采用进入原审核流程，不自动把内容标为已确认。
5. 名称候选经人工“确认并写入术语表”后才更新正式术语。来源修订改变时需重新审核，不静默覆盖新原文。
6. 剧本/Bible及短剧生产仍走项目ProductionRun，独立翻译不能选生产路线或直接创建媒体任务。

## 安全与失败

凭据只通过受管SecretBinding使用，不进入正文、普通日志或文档。保留来源、修订、请求身份、诊断和真实费用；失败、超额待确认、取消和不确定结果不伪装成功。未知提交先核对原远端结果，不能用新请求绕过。

本地模型安装与进程生命周期仍由运维承担；平台不下载或守护推理进程，相关延期项为OPT-LOCAL-RUNTIME-001。语言规则由具体合同解释，不恢复全局“所有输入必须英文”的门禁。

## 实现与验证边界

- 页面：[ProjectTranslationRequests.vue](../frontend/src/components/ProjectTranslationRequests.vue)。
- 请求与目录适配：[projectTranslationRequests.ts](../frontend/src/services/projectTranslationRequests.ts)。
- 服务与来源：[project_translation_requests.py](../backend/studio/services/project_translation_requests.py)。
- 定向用例：[前端测试](../frontend/tests/projectTranslationRequests.test.ts)、[后端测试](../backend/studio/tests/test_project_translation_requests.py)。

上述是当前代码入口和已有测试文件，不表示本轮执行过测试或真实翻译。目标环境仍需验证授权、精确组合、来源漂移、错误恢复、候选采用与人工审核。
