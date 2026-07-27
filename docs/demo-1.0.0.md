# 1.0.0 演示手册

## 准备环境

在仓库根目录创建虚拟环境并安装开发依赖：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Linux/macOS 把激活命令替换为 `source .venv/bin/activate`。演示脚本不会下载模型，也
不会调用真实 LLM；默认使用本地 SQLite。

## 生成演示数据

输出目录必须不存在或为空。脚本不会清空已有目录。

```powershell
python scripts/generate_demo_data.py `
  --output-root .artifacts/demo `
  --file-count 32
```

生成内容包括：

- `input/`：成对的 DOCX 版本文件；
- `request.json` 和 `background_submission.json`；
- `delivery_log.json`：一条脱敏本地发送证据；
- `manifest.before.json`：输入数量、相对路径、大小和 SHA-256；
- 相互隔离的 `artifacts/`、`reports/`、`database/` 与 `checkpoints/`。

`--file-count` 允许 1 到 500。发布验收自动运行 32 个真实 DOCX；容量演示可以在独立
空目录生成 100 或 200 个文件。

## SQLite 全链路

以下命令先执行 Alembic 迁移，再依次运行 Python 前台、API 后台和 Cron 链路。每条
后台链路都会由真实 Worker 执行并通过报告 API 下载结果，最后重新计算输入清单。

```powershell
python scripts/run_e2e_demo.py `
  --demo-root .artifacts/demo `
  --database-backend sqlite `
  --mode all
```

成功结果写入 `.artifacts/demo/e2e-demo-output/result.json`，其中
`readonly_input.unchanged` 必须为 `true`。可以用 `--mode foreground`、
`--mode background` 或 `--mode cron` 单独执行一条链路。

## SQLite 备份恢复闭环

`roundtrip` 使用 SQLite 在线备份 API 创建一致性备份，再恢复到一个全新副本；它不会
覆盖源数据库。

```powershell
python scripts/backup_restore_demo.py `
  --backend sqlite `
  --action roundtrip `
  --work-directory .artifacts/demo/backup-restore-output `
  --database-path .artifacts/demo/database/file-governance-app.sqlite3
```

脚本对源、备份和恢复副本执行 `PRAGMA integrity_check`，并逐表比较行数。恢复到明确的
全新路径时需要：

```powershell
python scripts/backup_restore_demo.py `
  --backend sqlite `
  --action restore `
  --work-directory .artifacts/demo/manual-restore `
  --backup-path .artifacts/demo/backup-restore-output/file-governance.sqlite3.bak `
  --restore-target .artifacts/demo/manual-restore/restored.sqlite3 `
  --confirm-restore
```

脚本拒绝覆盖现有文件。生产恢复应停写、验证备份、恢复到新文件并通过受控切换替换
连接目标，不能直接覆盖仍在使用的数据库。

## Docker SQLite 拓扑

准备只读输入挂载目录并启动默认拓扑：

```powershell
New-Item -ItemType Directory -Force data/input | Out-Null
docker compose up --build -d
docker compose ps
```

默认拓扑包含 migrate、api、worker、scheduler 和 mock-email-mcp，不启动 PostgreSQL。
API 健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

## Docker PostgreSQL 拓扑

本项目只通过 Docker 演示 PostgreSQL，不要求安装本地 PostgreSQL 服务，也不使用云
数据库。复制环境示例并修改强密码：

```powershell
Copy-Item .env.example .env
```

然后启动基础文件和 PostgreSQL override：

```powershell
docker compose `
  -f docker-compose.yml `
  -f docker-compose.postgresql.yml `
  up --build -d
docker compose `
  -f docker-compose.yml `
  -f docker-compose.postgresql.yml `
  ps
```

要从宿主机运行同一 E2E 脚本，设置 Docker 映射端口 URL。下面的密码、用户名、数据库
名必须与 `.env` 一致：

```powershell
$env:FILE_GOVERNANCE_DATABASE_URL = `
  "postgresql+psycopg://file_governance:你的密码@127.0.0.1:55432/file_governance"
python scripts/run_e2e_demo.py `
  --demo-root .artifacts/demo-postgresql `
  --database-backend postgresql `
  --mode all
```

先为 `.artifacts/demo-postgresql` 单独运行数据生成器。运行器拒绝远程 PostgreSQL
主机，只接受 `127.0.0.1`、`localhost` 或 Compose 内部 `postgres` 服务。

## Docker PostgreSQL 备份恢复闭环

下面的命令调用 PostgreSQL 容器内的 `pg_dump`、`createdb`、`pg_restore` 和 `psql`，
不会依赖宿主机数据库工具。`roundtrip` 创建带
`file_governance_restore_` 前缀的临时数据库，验证 `alembic_version` 后在 `finally`
阶段删除它。

```powershell
python scripts/backup_restore_demo.py `
  --backend postgresql `
  --action roundtrip `
  --work-directory .artifacts/postgresql-backup-restore `
  --compose-project-name file-manage-agent
```

只创建备份：

```powershell
python scripts/backup_restore_demo.py `
  --backend postgresql `
  --action backup `
  --work-directory .artifacts/postgresql-backup
```

保留恢复数据库的 `restore` 动作必须提供 `--confirm-restore`，且目标名称只能使用专用
前缀，避免指向现有业务库。

## 人工确认演示

后台提交后查询：

```text
GET /runs/{run_id}
```

当 `background_job.status` 为 `waiting_human` 时，读取当前
`pending_interrupt.interrupt_id` 和 `kind`。主版本确认可参考
`examples/sample_human_review_response.json`，错误恢复可参考
`examples/sample_recovery_response.json`：

```text
POST /runs/{run_id}/resume
Content-Type: application/json
```

恢复返回 202 后等待 Worker。重复提交同一 `request_id`、`interrupt_id` 和内容应返回
相同任务状态；旧 interrupt ID 返回 409。

## MCP 成功与降级

Compose 默认让 API/Worker 使用模拟邮件 MCP。成功时报告中的发送证据来源为
`email_mcp`。停止 MCP 或配置不可达端点，并在请求中提供本地
`delivery_log_path`，Evidence 子图会记录非致命 `mcp` 错误并将来源标记为
`local_log`。

自动化用例 `tests/e2e/test_mcp_success_and_fallback.py` 会启动真实 Streamable HTTP
模拟服务，并在同一用例中验证连接失败降级。

## 停止服务

保留数据库卷：

```powershell
docker compose `
  -f docker-compose.yml `
  -f docker-compose.postgresql.yml `
  down
```

删除 named volume 会永久删除容器数据库和运行产物，只有确认数据已备份且明确需要重置
演示环境时才可以执行带 `--volumes` 的命令。
