# Production Deployment

## 部署原则

部署以根 \`openapi.yaml\`、Django migrations、\`scripts/restart.ps1\` 和受管存储为权威入口。不要部署 OpenAPI 副本、旧生产路由或启动时 seed 同步。

## 发布顺序

新机器全新部署与现有安装升级分开处理：新机器从干净源码和空数据库/新卷初始化，不复制旧环境密钥、数据库、媒体目录、日志、备份、`.runtime`或`.artifacts`。Windows正式安装入口支持`setup.ps1 -Fresh`只读断言无业务历史，默认不导入素材或演示项目。Docker bootstrap在迁移后初始化系统风格，但不导入素材；需要预置素材时显式运行`manage.py import_asset_library --manifest /app/assets/library/manifest.json`（先核对容器实际挂载的清单和素材路径）。重启不会恢复已删素材。现有安装清理历史/保留素材见[运行手册](operations.md#业务历史清理与素材批量删除)，不通过复制当前开发目录制作“全新”安装包。

1. 备份 PostgreSQL 与 managed media。
2. 从独立 secret manager 注入数据库连接、Django secret 与数据库加密密钥环；密钥环不得进入数据库备份。
3. 部署代码和锁定依赖。
4. 审阅 \`manage.py migrate --plan\`。
5. 执行 migration，并以 \`manage.py migrate --check\` 确认。
6. 运行 \`scripts/check.ps1\`。
7. 使用 \`scripts/restart.ps1\` 启动新 boot。
8. 显式激活 pending restart-required configuration revisions。
9. probe runtime hosts/model deployments。
10. validate、real-smoke、publish workflow versions。
11. 确认 published recipe 具备完整 content → storyboard → prompt → media → selection → assembly → delivery 闭包，再以目标环境 ProductionRun 完成 canary。
12. 验证 managed artifact、episode master 与 ReleaseBundle。
13. 验证回滚与恢复路径。

Docker 首装不会在长期 backend 容器日志中打印临时密码。迁移完成后，由能够直接看到当前终端且不会收集该输出的操作者执行一次：

\`\`\`bash
docker compose run --rm -e AUTOPROMPT_BOOTSTRAP=false backend python manage.py bootstrap_superadmin --if-missing
\`\`\`

命令只在不存在超级管理员时输出一次随机临时密码；立即交给目标用户并要求首次登录改密，不得复制到工单、聊天、配置文件或持久化日志。

## 合同回执更新的部署边界

工作流合同的只读恢复依赖 `0236_comfy_workflow_preview_receipt` 与匹配的前后端代码。该迁移只新增回执表、外键及操作人／请求键唯一约束，不回填旧请求，不修改已有合同或素材；验收与现用加载状态以[当前检查点](optimization/current.md)为准。

沿上方发布流程，在获授权的维护窗口先完成[一致备份](backup-restore.md#一致性)并审阅迁移计划；若还有其他待迁移项，先核对其范围，不能视为只应用0236。后台表结构与代码均就绪后再验证新页面，不能只更新前端便宣称回读可用。

现有 `scripts/restart.ps1` 默认应用所有待迁移项、恢复中断装配并启动 API、前端及各 worker。`-SkipComfyUI` 只跳过 ComfyUI 进程管理，不会停用生成 worker 或主机维护；启动后可能继续已有任务及主机探测。因此“更新代码”不等于授权执行该脚本，“不重启 ComfyUI”也不等于“不会调用 ComfyUI”。操作前核对在途任务与维护范围，不另写旁路启动脚本。

本更新的上线复核先检查迁移状态、服务健康及工作流页面读取；有原回执时仅核对原合同。没有回执样本不能临时生成一份并声称只读验收，新的合同生成或真实 Smoke 须另有相应授权。回读不触发自动提升。回退应保留已产生的合同与回执并沿既有备份恢复流程处理，不以反向删除新表或清除待核对请求冒充修复。

## 服务角色

除 API 和前端外，部署必须包含：

- generation worker；
- text-generation worker；
- async-media worker；
- assembly worker；
- platform-maintenance worker；
- PostgreSQL；
- managed media storage；
- 至少一个经过验证的媒体 executor（按实际路由）。

worker 并发是进程局部设置。当前没有跨节点/多 GPU scheduler，不能用固定并发值描述所有机器。

## ProductionRun canary

canary 必须从 ProductionRun 创建，并检查脚本/Bible gate、route、cost、workflow/config snapshot、async submit/poll、managed archive、\`comparison_parameters\`、逐集 master 和 ReleaseBundle。参数差异只使用归档时从冻结事实白名单生成的 \`comparison_parameters\`，不用 manifest 身份/hash。Tasks、Deferred requests、Assemblies、Releases 完全只读；创建、重试、取消与有序 fallback 只在所属 ProductionRun。

若要验收 I2V/reference image，必须先实现并验证目标 provider 的 upload contract。否则预期结果是 \`provider_upload_contract_missing\` blocked，而不是成功。

## 模型与 workflow

bootstrap catalog 是 \`needs_review\` 草案。平台不会自动下载模型、创建已部署事实、发布 workflow 或激活 recommendation。catalog 中的 H3/SDXL 等词仅是 adapter/许可审阅事实。

## 生产就绪证据

代码测试通过不等于目标环境就绪。发布记录必须包含真实 provider 授权、deployment probe、workflow real smoke、T2I/T2V E2E、artifact 归档、episode master、ReleaseBundle verify 和恢复演练。
