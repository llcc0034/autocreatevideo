# Production Deployment

## 部署原则

部署以根 \`openapi.yaml\`、Django migrations、\`scripts/restart.ps1\` 和受管存储为权威入口。不要部署 OpenAPI 副本、旧生产路由或启动时 seed 同步。

## 发布顺序

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
