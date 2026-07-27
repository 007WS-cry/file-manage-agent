# 1.0.0 发布说明

## 发布摘要

1.0.0 完成从 0.8.2 到稳定交付版的第三批工作：提供覆盖前台、后台、Cron、两类人工
恢复、MCP、checkpoint 重启、双数据库拓扑、输入不变性和备份恢复的端到端验收；同时
加入可复验演示脚本、示例响应和正式架构/状态文档。

## 新增内容

- 四个 `tests/e2e` 发布验收文件；
- `generate_demo_data.py`：生成 1–500 个 DOCX、请求、证据和 SHA-256 基线；
- `run_e2e_demo.py`：迁移数据库并执行 Python 前台、API 后台和 Cron 全链路；
- `backup_restore_demo.py`：SQLite 副本闭环及 Docker PostgreSQL 临时库闭环；
- 主版本审核、错误恢复与输入清单示例；
- 架构、状态契约、演示、恢复协议和发布文档；
- 版本、镜像、Compose、环境示例和忽略规则统一到 1.0.0。

## 修复

SQLite 的 `DateTime(timezone=True)` 读回时可能丢失时区。节点执行记录从 ORM 转回状态
后再次持久化，会被严格 ISO 8601 校验拒绝。1.0.0 在 ORM→状态边界将无时区数据库
时间解释为 UTC，并统一输出带时区 ISO 字符串，保证错误恢复和 checkpoint 重启链路
可以继续执行。

## 最终验收矩阵

| 能力 | 自动化覆盖 | 演示/部署覆盖 |
| --- | --- | --- |
| CLI/Python 前台 | `test_foreground_background_cron_resume_report.py` | `run_e2e_demo.py --mode foreground` |
| API 后台运行 | 同上 | `--mode background` |
| Cron 到报告获取 | 同上 | `--mode cron` |
| 主版本人工确认 | `test_checkpoint_restart_resume.py` | `sample_human_review_response.json` |
| 错误恢复人工确认 | 同上 | `sample_recovery_response.json` |
| 恢复幂等、旧 ID、计数 | E2E 与 `test_background_resume_api.py` | `resume-and-interview.md` |
| MCP 成功与本地降级 | `test_mcp_success_and_fallback.py` | Compose 模拟 MCP |
| Worker/checkpoint 重启 | `test_checkpoint_restart_resume.py` | 同数据库与 checkpoint 重启流程 |
| SQLite 默认拓扑 | `test_compose_smoke.py` | `docker compose up --build` |
| PostgreSQL Docker 拓扑 | 三个 PostgreSQL 集成测试 | Compose override |
| 数十文件有界执行 | 32 个真实 DOCX | 生成器支持最高 500 |
| 有界并发 | 32 个 PDF，峰值限制 4 | RunnableConfig `max_concurrency` |
| 输入数量/路径/SHA 不变 | `test_readonly_input_invariance.py` | `manifest.before.json` |
| 数据库迁移 | SQLite 与 PostgreSQL migration 测试 | `run_e2e_demo.py` 自动 upgrade |
| 备份和恢复 | SQLite roundtrip E2E | SQLite/PostgreSQL 演示脚本 |
| 报告路径越界 | `test_report_retrieval_api.py` | 受控下载 API |

## 升级步骤

1. 停止新任务提交并等待正在运行的 Worker 收口或进入 `waiting_human`；
2. 备份应用数据库、LangGraph checkpoint 和报告/产物目录；
3. 更新源码和依赖；
4. 执行 `python -m alembic upgrade head`；
5. 确认 `python -c "import app; print(app.__version__)"` 输出 `1.0.0`；
6. 先启动 migrate，再启动 API、Worker、Scheduler 和可选 MCP；
7. 对暂停任务重新查询当前 interrupt，再提交恢复请求；
8. 执行 E2E 或演示脚本并核对输入清单。

0.8.2 已经包含迁移 `0005_background_resume_state`，因此 1.0.0 没有新增数据库迁移
版本；仍应执行 upgrade 以验证部署数据库处于 head。

## 兼容性

- Python 要求 3.10 及以上，容器基于 Python 3.11；
- 默认应用数据库仍为 SQLite；
- PostgreSQL 仍通过 `docker-compose.postgresql.yml` 按需启用；
- 后台 LangGraph checkpoint 仍为 SQLite；
- 0.8.2 API 路径、两类恢复 kind 和报告接口保持不变；
- 旧 0.6.0 状态兼容补齐和历史迁移回放测试继续保留。

## 已知边界

- 不提供 PostgreSQL LangGraph Checkpointer；
- 不自动删除、移动、覆盖或重命名业务文件；
- 不自动清理脏 Git Worktree；
- 不把真实 LLM、邮件系统或云数据库作为发布验收前提；
- SQLite 适合默认单机部署；高并发多 Worker 应选择 Docker PostgreSQL 应用数据库；
- 容量上限仍需结合容器 CPU、内存、进程数和超时控制。

## 发布命令

```powershell
python -m pytest
python -m ruff check app tests alembic scripts
python -m compileall -q app alembic scripts tests
docker compose config
$env:FILE_GOVERNANCE_POSTGRES_PASSWORD = "仅用于配置检查的临时值"
docker compose -f docker-compose.yml -f docker-compose.postgresql.yml config
docker build --build-arg APP_VERSION=1.0.0 -t file-manage-agent:1.0.0 .
```

PostgreSQL 集成测试需要 Docker，并通过
`FILE_GOVERNANCE_RUN_POSTGRESQL_TESTS=1` 显式启用。
