# 文档索引

最近整理：2026-10-10。本文只负责导航与职责，不保存第二份功能、待办或验收状态。

## 从哪里开始

1. 使用平台：先读[项目入口](../README.md)与[操作员手册](production-operator-manual.md)，页面内手册提供逐步操作。
2. 配置模型与工作流：读[系统设置指南](system-settings-beginner-guide.md)、[素材生成配置](asset-generation-model-configuration.md)、[工作流纳管手册](comfyui-saved-workflow-validation.md)。
3. 接续开发：从[优化索引](optimization/README.md)进入[当前工作包](optimization/current.md)，再读命中模块的唯一设计与清单。原模块设计和日期化证据已分开，不按历史快照重新从M1启动；范围与延期决定见[分模块总览](optimization/backlog-review-2026-09-28.md#module-plan)。
4. 判断能否投产：按[生产就绪检查](production-readiness.md)收集当前目标环境证据；历史成功、源码存在或配置已发布都不能替代真实验收。

## 当前正式文档

| 文档 | 职责 |
| --- | --- |
| [MVP范围](mvp-requirements.md) | 产品目标与范围；不是全项已验收声明 |
| [架构](architecture.md) | 当前模块、事实来源与执行链；Schema以[models.py](../backend/studio/models.py)和[迁移](../backend/studio/migrations)为准 |
| [API](api.md) | 本地接口说明；唯一机器契约为[openapi.yaml](../openapi.yaml) |
| [运行与运维](operations.md) | 正式服务、配置、恢复与维护边界；日常入口为restart脚本 |
| [部署](production-deployment.md)、[备份恢复](backup-restore.md) | 目标环境部署和完整恢复要求 |
| [生产操作员手册](production-operator-manual.md) | 页面操作边界；逐步正文源为[productionManual.ts](../frontend/src/content/productionManual.ts) |
| [系统设置指南](system-settings-beginner-guide.md) | 由[systemSettingsGuide.json](../frontend/src/content/systemSettingsGuide.json)生成；禁止只改生成文档 |
| [素材生成配置](asset-generation-model-configuration.md) | 统一执行实例在素材库的配置与使用 |
| [工作流纳管](comfyui-saved-workflow-validation.md)、[工作流审阅](comfyui-audit.md) | candidate/draft、Projector、Validate、Smoke与发布的职责 |
| [节点监测](execution-monitoring-contract.md) | 长节点静默时限、任务时限与恢复边界 |
| [独立翻译](local-prompt-translation.md) | 项目字段异步翻译、来源修订与明确采用 |
| [最小表单标准](form-input-standard.md) | 目录/系统/业务字段所有权；规范目标不等于所有表单已覆盖 |
| [生产就绪检查](production-readiness.md) | 目标环境验收条件，不保存全局完成状态 |
| [决策记录](decisions/README.md) | 决策记录维护规则；模板不是当前待办 |

## 待办与改造

- [待办优化与分模块开发计划](optimization/backlog-review-2026-09-28.md#module-plan)：M1–M9范围、顺序、双入口素材链及延期/待定决定的唯一总览，A组仅来源索引。
- [平台模块化改造详细设计索引](optimization/module-development-detailed-design-2026-09-28.md)：原DESIGN-MODULE-20260928编号不变，各模块设计文件分别维护自己的任务、依赖、验收和勾选，不复制清单。新增范围仍须确认。
- [优化工作索引](optimization/README.md)：当前续接、模块设计、原OPT登记及日期证据的唯一总入口。
- [统一执行实例计划](optimization/execution-instance-unification-plan.md)：历史设计、简短检查点与日期化证据；最新施工细则引用独立详细设计，历史“下一步”不自动恢复。
- 暂不做、观察中与待验收分别管理；未完成验收不能直接改写成“尚未开发”，配置重建也不能继承旧验收通过。

## 历史证据（不能作为当前操作步骤）

| 记录 | 范围 |
| --- | --- |
| [8月24日实操记录](e2e-production-tutorial.md)、[8月23–24日发现与复测](e2e-production-findings-20260823.md) | 旧阶段/配置下的成功和失败，不代表当前统一实例或系统额度合同 |
| [8月25日Ref2VA](e2e-short-drama-ref2va-dual-clock-test-2026-08-25.md)、[8月26日](e2e-short-drama-ref2va-dual-clock-test-2026-08-26.md) | 当时链路与一致性样本 |
| [8月27日Ref2VA](e2e-short-drama-ref2va-dual-clock-test-2026-08-27.md)、[8月28日](e2e-short-drama-ref2va-dual-clock-test-2026-08-28.md) | 当时三线实测与质量缺口；未闭环部分由现台账承接，不继续按旧版本重跑 |
| [配置边界审计](optimization/configurable-boundary-audit-2026-08-28.md) | 0116–0127阶段证据；已撤下对象/配置只解释历史，不是当前Schema说明 |

## 维护要求

- 主文档描述当前合同，目标方案进入台账，运行记录写日期与范围；每种职责保留一个入口。
- 修改指南时更新结构化源并生成；修改API时同步唯一机器契约。
- 文档审核不能替代稳定源码回归、浏览器、真实Smoke、媒体效果和恢复验证；缺证据明确标为待验收。
