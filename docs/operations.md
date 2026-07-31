# 部署与运行

本文面向部署和运维 File Manage Agent 1.0.0 的人员。第一次使用请先阅读根目录
[`README.md`](../README.md)。

## 运行组件

| 组件 | 命令 | 职责 |
| --- | --- | --- |
| CLI | `file-governance` | 启动或恢复前台治理任务 |
| API | `file-governance-api` | 接收后台任务、查询状态、恢复任务和下载报告 |
| Worker | `file-governance-worker` | 从应用数据库领取任务并执行治理图 |
| Scheduler | `file-governance-scheduler` | 将持久化 Cron 计划转换为后台任务 |
| 模拟邮件 MCP | `file-governance-mock-email-mcp` | 提供只读、脱敏的发送证据演示数据 |

后台拓扑中，API、Worker 和 Scheduler 必须共享同一个应用数据库和 checkpoint 存储。
应用数据库与 LangGraph checkpoint 是两套存储，不能指向同一个 SQLite 文件。

## 本地安装

项目要求 Python 3.10 或更高版本：

```bash
python -m pip install -r requirements.txt
```

开发与测试依赖：

```bash
python -m pip install -e ".[dev]"
```

首次启动应用服务前执行数据库迁移：

```bash
python -m alembic upgrade head
```

## 前台 CLI

[`../examples/sample_request.json`](../examples/sample_request.json) 默认关闭真实 LLM，
使用 SQLite checkpoint：

```bash
file-governance run examples/sample_request.json \
  --thread-id governance-run-001
```

常用覆盖参数：

```text
--checkpoint-backend {sqlite,memory}
--checkpoint-path PATH
--application-database-path PATH
```

跨进程人工恢复必须使用 SQLite checkpoint，并提供启动时相同的 `thread_id`：

```bash
file-governance resume review_response.json \
  --thread-id governance-run-001 \
  --checkpoint-path .artifacts/checkpoints/file-governance-0.5.sqlite3
```

响应格式和错误恢复动作见[后台恢复与人工确认](resume-and-interview.md)。

## 本地后台服务

启动 API：

```bash
file-governance-api \
  --host 127.0.0.1 \
  --port 8000 \
  --database-path .artifacts/database/file-governance-app.sqlite3 \
  --checkpoint-path .artifacts/checkpoints/file-governance-background.sqlite3
```

另一个终端启动 Worker：

```bash
file-governance-worker \
  --database-path .artifacts/database/file-governance-app.sqlite3
```

需要 Cron 时再启动 Scheduler：

```bash
file-governance-scheduler \
  --database-path .artifacts/database/file-governance-app.sqlite3 \
  --checkpoint-path .artifacts/checkpoints/file-governance-background.sqlite3 \
  --timezone Asia/Shanghai
```

需要演示邮件证据时启动模拟 MCP：

```bash
file-governance-mock-email-mcp \
  --host 127.0.0.1 \
  --port 8001 \
  --data-path examples/mock_email_data.json
```

随后为 CLI、API 或 Worker 设置：

```dotenv
FILE_GOVERNANCE_EMAIL_MCP_ENABLED=true
FILE_GOVERNANCE_EMAIL_MCP_URL=http://127.0.0.1:8001/mcp
```

## HTTP API

| 方法与路径 | 用途 |
| --- | --- |
| `POST /runs` | 创建后台治理任务 |
| `GET /runs/{run_id}` | 查询运行及当前人工中断 |
| `GET /runs/jobs/{job_id}` | 按后台任务 ID 查询 |
| `POST /runs/{run_id}/resume` | 幂等提交人工恢复 |
| `GET /runs/{run_id}/report` | 受控下载 Markdown 报告 |
| `POST /schedules` | 创建 Cron 计划 |
| `GET /schedules` | 查询计划列表 |
| `GET /schedules/{schedule_id}` | 查询单个计划 |
| `POST /schedules/{schedule_id}/enable` | 启用计划 |
| `POST /schedules/{schedule_id}/disable` | 停用计划 |

提交示例任务：

```bash
curl -X POST http://127.0.0.1:8000/runs \
  -H "Content-Type: application/json" \
  --data @examples/sample_background_submission.json
```

API 返回 HTTP 202 和 `run_id`、`job_id`、`thread_id`。Worker 异步执行后，可查询状态
并下载报告：

```bash
curl http://127.0.0.1:8000/runs/<run_id>
curl -OJ http://127.0.0.1:8000/runs/<run_id>/report
```

创建 Cron 计划：

```bash
curl -X POST http://127.0.0.1:8000/schedules \
  -H "Content-Type: application/json" \
  --data @examples/sample_schedule.json
```

API 不在请求进程执行治理图或 Scheduler。Cron 到点后只创建队列任务，仍需 Worker
完成执行。

## Docker Compose：SQLite

默认拓扑使用 SQLite 应用数据库和独立 SQLite checkpoint。输入目录以只读方式挂载：

```bash
docker compose up --build -d
docker compose ps
```

查看服务日志：

```bash
docker compose logs -f api worker scheduler
```

停止服务但保留 named volume：

```bash
docker compose down
```

不要添加 `--volumes`，除非明确需要删除演示数据库和产物卷。

## Docker Compose：PostgreSQL

PostgreSQL 只替换应用数据库；LangGraph checkpoint 仍使用共享产物卷中的 SQLite 文件。
先创建本地环境文件并修改密码：

```bash
cp .env.example .env
docker compose \
  -f docker-compose.yml \
  -f docker-compose.postgresql.yml \
  up --build -d
```

Compose URL 插值不负责转义，示例拓扑中的数据库名、用户名和密码应只使用
`A-Z/a-z/0-9/._~-`。生产环境应改用受管 Secret。

## 真实 LLM

默认安装包含 OpenAI 集成。其他 Provider 使用可选依赖：

```bash
python -m pip install -e ".[anthropic]"
python -m pip install -e ".[gemini]"
python -m pip install -e ".[deepseek]"
python -m pip install -e ".[qwen]"
python -m pip install -e ".[openrouter]"
python -m pip install -e ".[litellm]"
```

请求只能保存环境变量名称，不能保存密钥值。根据
[`../.env.example`](../.env.example)创建本地 `.env`，并只填写实际启用的 Provider。
应用不会自动加载 dotenv；容器运行时显式传入：

```bash
docker run --env-file .env ...
```

公开示例见
[`../examples/sample_llm_request.json`](../examples/sample_llm_request.json)和
[`../examples/sample_multi_model_request.json`](../examples/sample_multi_model_request.json)。

## 存储与挂载

推荐布局：

```text
data/input/                       # 只读业务输入
.artifacts/content/               # 标准化内容和中间产物
.artifacts/reports/               # Markdown 报告
.artifacts/database/              # 应用数据库
.artifacts/checkpoints/           # LangGraph checkpoint
.artifacts/worktrees/             # 可选隔离 Worktree
```

必须满足：

- 输入目录与所有可写目录互不重叠；
- 应用数据库与 checkpoint 使用不同文件；
- 后台服务共享相同数据库、checkpoint 和产物挂载；
- 报告通过受控 API 获取，不根据数据库字段自行拼接路径；
- 邮件日志和请求文件以只读方式挂载。

## 生产检查清单

- 已执行 `alembic upgrade head`；
- 输入目录只读，产物目录可写；
- 数据库、checkpoint、报告和产物已纳入备份；
- `.env`、数据库凭据和真实邮件记录未进入镜像或版本库；
- API、Worker、Scheduler 使用一致的数据库和 checkpoint 配置；
- 已根据负载设置 Worker 数量、超时、租约和文件上限；
- 已验证人工中断查询、恢复和报告下载；
- 已执行[演示手册](demo-1.0.0.md)中的端到端检查。
