# Backup and Restore

## 备份范围

一次可恢复备份必须一致覆盖：

- PostgreSQL；
- managed media 与 generated artifacts；
- Django默认文件存储中的私有`execution-requests/`完整冻结请求，与ExecutionPreparationContract／GenerationAttempt的同源文件标识、版本、字节大小和SHA一致备份；文件及manifest均不可单独清理，不能由当前策略重新生成。恢复后核对原准备身份和完整规范字节；该前缀不能通过公共静态目录暴露；
- `execution-responses/` 下的截断响应、人工补全文本及文本Smoke原始响应，连同数据库中的所属调用、大小、SHA 与确认记录一致备份；该前缀不得作为公共静态目录暴露，正文仅经有权限的执行准备／任务详情读取。Smoke历史缺失正文不得补造；文件恢复须与原任务manifest摘要一致，不能以空文本代替；
- episode masters 和 ReleaseBundle 文件；
- 非敏感部署清单、workflow/model provenance 与许可记录；
- 恢复所需的版本、migration 和配置 revision 标识。

任务队列、claim、lease、远端 request ID 与不确定提交状态都属于数据库业务事实，必须随 PostgreSQL 一致备份。SecretVersion 的 ciphertext、nonce、algorithm 和 key id 随数据库备份；\`AUTOPROMPT_CONFIG_MASTER_KEYS\` 对应的解密密钥环必须由外部秘密管理系统或离线保管独立备份，严禁与数据库或 \`.env\` 配置包放在同一个备份中。

\`scripts/backup.ps1 -IncludeConfig\` 默认只选择项目 \`config/\` 中的非秘密配置，不会隐式打包根目录或 backend 的 \`.env\`。即使通过 \`-ConfigPath\` 显式选择，部署 \`.env\` 或包含数据库加密密钥环赋值的文件也会在 Dry-run 计划输出和创建备份目录之前被拒绝，且错误信息不会回显配置值。

## 一致性

在备份窗口停止新的 ProductionRun 提交，并等待或显式暂停 generation、text-generation、async-media、assembly、platform-maintenance worker。记录仍在 running、uncertain 或 leased 的 run/attempt，避免数据库快照与媒体文件不一致。

先确定数据库与受管存储的一致时间点，再执行各自备份。只备份数据库、只复制媒体目录或只导出队列都不足以恢复。

## 恢复流程

恢复数据库与媒体后，先从独立保管位置恢复所需 key id 对应的密钥环，再启动 readiness。缺少 key、密文/AAD 被篡改或活动 SecretBinding 不可解密时，\`secret_store\` 检查必须返回失败并阻止 ready；不得把不可解密凭据降级为空值继续运行。

1. 在隔离环境部署匹配的代码版本，但不要启动应用进程。
2. 从独立保管位置注入原数据库加密密钥环；不得生成替代 key 覆盖丢失的历史 key。
3. 恢复 PostgreSQL。
4. 恢复 managed media/artifact/master 文件。
5. 启动 readiness 并核对活动 SecretBinding 均可解密。
6. 运行 \`manage.py migrate --plan\`。
7. 按目标版本执行 migration。
8. 运行 \`manage.py migrate --check\` 和 \`scripts/check.ps1\`。
9. 使用 \`scripts/restart.ps1\` 启动新 boot。
10. 显式处理 pending restart-required config activation。
11. probe host/deployment，确认 workflow readiness。
12. 核对 ProductionRun、attempt、managed artifact、episode master 和 ReleaseBundle 引用。
13. 对恢复前 uncertain/leased 项按状态机执行恢复，不直接改成成功。

## 恢复验收

抽样验证：

- 脚本、Bible、镜头和 ProductionRun 快照；
- provider/deployment/workflow/config 引用；
- managed artifact hash、媒体类型和可读性；
- 每集 master 的 source items 与播放；
- ReleaseBundle 完整性；
- Deferred lineage 的只读历史与所属 ProductionRun 恢复/取消；
- worker lease 恢复；
- 新 ProductionRun canary。

系统交付单位是逐集 master + ReleaseBundle，不检查整季单条长视频。

## 边界

备份恢复不能证明真实 provider/GPU 可用。恢复后仍需重新验证授权、deployment probe、workflow real smoke 和 async T2I/T2V E2E。I2V/reference upload 在没有 provider-specific contract 时仍应 blocked。

全新机器安装不是恢复：使用干净源码、空库/新卷及新环境密钥，不复制原运行目录。正式入口及业务历史清理见[运行手册](operations.md#业务历史清理与素材批量删除)。历史清理只删除业务数据库记录并重置运行证明，保留素材、配置和现存文件；它不会清理旧备份或日志。素材批量删除另行管理，可能删除不再共用的受管上传文件，因此仅数据库备份不足以恢复被删素材。执行清理前自行保留需要恢复的数据与文件，不将预览或Schema0235迁移当成已经执行清理。

## 冻结请求存储维护

0194将已有最终请求按原规范字节迁入正式文件存储，先核验原摘要及生产调用同一请求关系、写入后回读，再移除旧JSON正文列；不改变任务、审核、费用或时间。执行前使用正式备份入口并保留匹配代码版本，在无活动调用的维护窗口由正式重启入口应用迁移。迁移不可直接反向执行；回退须恢复成套数据库、文件和代码，不能只回退Schema。SQLite开发验收与PostgreSQL目标迁移是不同范围，后者仍以用户明确通知为前提。

完整请求文件与所属执行历史同保留期，不自动删除。文件先写、数据库事务后提交，因此失败事务可能留下未被manifest引用的文件；它们不是任务事实，读取端不得扫描目录并认领。维护清理须在停止写入的维护窗口核对全部manifest和在途事务后另行形成精确可回滚清单，不能按文件时间或“本次失败”直接删除。当前没有自动孤儿清理器，也不会因数据库回滚而删除可能被其他调用引用的文件。
