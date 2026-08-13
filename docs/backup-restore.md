# 备份、校验与恢复

本项目提供 Windows PowerShell 本地备份/恢复工具，覆盖：

- `DATABASE_URL=sqlite:///...`：使用 Python `sqlite3.Connection.backup()` 做在线一致性快照，再执行 `PRAGMA integrity_check`。
- `DATABASE_URL=postgresql://...`：使用 `pg_dump --format=custom`；恢复使用 `pg_restore --single-transaction --clean --if-exists`，作用域仅为明确配置的目标数据库。
- Django `backend/media`。
- `COMFYUI_OUTPUT_DIR`（Docker 宿主机可用 `COMFYUI_OUTPUT_HOST_DIR`）。
- 可选项目配置文件。配置可能含数据库密码、Django Secret Key 和外部 API Key，默认不备份，启用后必须把备份目录当作密钥库保护。

脚本不会备份模型权重、Python/Node 依赖、缓存或源代码。模型和运行时应由版本锁定清单重新部署；如业务要求离线灾备，应在独立受控流程中备份模型仓库。

## 创建备份

在仓库根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup.ps1
```

默认创建一个从不覆盖既有目录的 `backups/autoprompt-backup-时间-随机值/`，包含：

- `manifest.json`：数据类别、相对路径、字节数和逐文件 SHA-256；数据库连接字符串中的密码不会写入清单。
- `manifest.sha256`：清单自身的 SHA-256。
- `data/database/`、`data/media/`、`data/comfyui-output/`。

SQLite 使用在线备份 API，可在服务运行时取得数据库一致性快照；但数据库、媒体文件和 ComfyUI 产物之间的业务级时间点一致性仍需要短暂停止 API/worker/ComfyUI 或使用组织现有的快照协调机制。持续写入产物目录时直接复制只能保证“备份副本自身可验哈希”，不能保证多个目录属于同一业务时刻。

先做无写入检查：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup.ps1 -DryRun
```

显式包含配置（敏感）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup.ps1 `
  -IncludeConfig
```

未传入 `-ConfigPath` 时，脚本从当前仓库根目录读取 `.env`、`backend/.env` 和 `config/`；不存在的默认项按脚本参数与校验规则处理。自定义配置范围时，先由仓库根目录拼出绝对路径，再传给 `-ConfigPath`。

不需要复制体积较大的 ComfyUI 产物时，可显式使用 `-SkipComfyUIOutput`。媒体目录可用 `-SkipMedia` 跳过。完整灾备不应跳过这些目录。

PostgreSQL 环境必须在执行主机安装兼容版本的 `pg_dump`/`pg_restore`，并通过绝对目标或 `DATABASE_URL` 指定可访问的数据库：

```powershell
$env:DATABASE_URL = 'postgresql://autoprompt:***@127.0.0.1:5432/autoprompt'
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup.ps1
```

连接密码只临时放入子进程的 `PGPASSWORD`；清单仅记录脱敏地址。不要把含真实密码的命令写进共享终端命令记录，生产环境优先使用受控服务账号和系统密钥注入。

## 校验与恢复预演

恢复前必须停止 Django API、生成/装配 worker 和 ComfyUI，避免恢复期间继续写数据库或产物目录。脚本不会自行终止进程。

`-DryRun` 会校验 `manifest.sha256`、每个文件的长度和 SHA-256、相对路径防穿越、reparse point、安全绝对目标边界、数据库类型及必要工具；不会创建 pre-restore 备份，也不会改动任何目标：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\restore.ps1 `
  -BackupPath '.\backups\autoprompt-backup-YYYYMMDD-HHMMSS-xxxxxxxx' `
  -DryRun
```

对迁移到新主机的备份，显式传入绝对目标：

```powershell
$projectRoot = (Read-Host '项目根目录绝对路径').Trim()
$backupPath = (Read-Host '备份目录绝对路径').Trim()
$comfyOutput = (Read-Host 'ComfyUI output 绝对路径').Trim()
$databasePath = Join-Path $projectRoot 'backend\db.sqlite3'
$databaseUrl = 'sqlite:///' + ($databasePath -replace '\\', '/')

powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\restore.ps1 `
  -BackupPath $backupPath `
  -DatabaseUrl $databaseUrl `
  -MediaRoot (Join-Path $projectRoot 'backend\media') `
  -ComfyUIOutputDir $comfyOutput `
  -ProjectRoot $projectRoot `
  -DryRun
```

## 执行真实恢复

只有显式传入 `-ConfirmRestore` 才会执行真实恢复：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\restore.ps1 `
  -BackupPath '.\backups\autoprompt-backup-YYYYMMDD-HHMMSS-xxxxxxxx' `
  -ConfirmRestore
```

安全行为如下：

1. 先完成全部哈希、数据库类型和目标边界校验。
2. 在 `backups/pre-restore/` 自动创建当前数据库、媒体、ComfyUI 产物和所选配置的 pre-restore 备份；这一步失败则不开始恢复。
3. SQLite 和普通文件先复制到同目录 staging 文件并再次验哈希。
4. 已存在的文件或目录会移动为同级 `.pre-restore-时间-随机值`，不会删除或覆盖；新目录再切换到精确目标名。失败时脚本尽力把刚移动的目标移回。
5. PostgreSQL 只连接清单类型一致且由当前 `DATABASE_URL` 明确指定的数据库，并在单事务中恢复；其可回退副本是第 2 步生成的 custom-format dump。

恢复完成后：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\restart.ps1
```

核对登录、连续剧/分镜数量、代表性图片/视频预览和下载，以及一条低成本生成任务。只有验收完成后，才由管理员手工删除同级 `.pre-restore-*` 副本和过期备份。脚本不会自动清理它们。

## 工具自测

下面的测试解析三个 PowerShell 文件的 AST，在系统临时目录创建小型 SQLite/媒体/ComfyUI/配置样本，创建真实临时备份并执行恢复 Dry-run；它不会恢复或删除项目现有数据：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\test_backup_restore.ps1
```

正式上线仍必须在隔离验收环境用生产同版本 PostgreSQL、实际目录权限和足够磁盘空间至少演练一次完整恢复，并记录 RPO、RTO、备份加密、异地复制、保留期和责任人。
