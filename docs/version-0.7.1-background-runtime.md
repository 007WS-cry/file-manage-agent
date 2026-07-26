# 0.7.1 持久化队列、HTTP API 与 Background Worker

本版本是从 `0.7.0` 向 `0.8.0` 演进的第一批，只增加 LangGraph 外层运行能力，
不改变 Inventory、Version Analysis、Evidence、Recommendation、Team
Orchestration、Context Compact 和 Error Recovery 七个子图的业务连线。

## 数据库结构

第三个 Alembic 迁移新增：

- `background_jobs`：保存 `run_id`、`thread_id`、规范化请求信封、生命周期、
  Worker 归属、有限尝试次数、报告路径和脱敏错误摘要；
- `scheduled_jobs`：为下一批 APScheduler 保存 Cron、时区、启用状态和请求模板；
- `worker_leases`：每个后台任务保存当前或最近一次租约、Worker、心跳和到期时间。

应用数据库由七张表扩展为十张表。普通 API、Worker 和图节点仍不会自动调用
`Base.metadata.create_all()`，必须显式执行：

```bash
python -m alembic upgrade head
```

## 事务与进程边界

API 在一个短事务中创建 `governance_runs` 和 `background_jobs`，成功提交后才返回
`run_id` 与 `job_id`。Worker 领取任务时使用带 `queued` 状态条件的 UPDATE，
随后在同一事务建立 active 租约。多个 Worker 读到同一候选时只有一个能够成功
推进状态。

LangGraph 执行期间不持有应用数据库 Session。心跳线程每次创建独立短 Session，
只更新当前任务租约。Worker 正常完成、部分完成、失败或等待人工输入时，在一个
短事务中收口后台任务、治理运行并释放租约。

Worker 进程异常退出后不再产生心跳。任一 Worker 下一次领取前扫描过期租约：

- `attempt_count < max_attempts`：任务重新进入 queued；
- `attempt_count >= max_attempts`：后台任务与治理运行进入 failed。

## HTTP 边界

第一批提供：

- `POST /runs`：持久化提交后台任务并返回 HTTP 202；
- `GET /runs/{run_id}`：查询治理运行和关联后台任务；
- `GET /runs/jobs/{job_id}`：直接查询后台任务；
- `GET /health`：返回进程版本和固定存活状态。

响应不会包含持久化请求信封、原始业务文件、文档正文、完整报告、Prompt、
Team Message、Task 引用、模型配置或 checkpoint 内容。

## 启动

```bash
file-governance-api \
  --database-path .artifacts/database/file-governance-app.sqlite3 \
  --checkpoint-path .artifacts/checkpoints/file-governance-background.sqlite3
```

```bash
file-governance-worker \
  --database-path .artifacts/database/file-governance-app.sqlite3
```

Worker 从任务信封读取 API 强制写入的 SQLite checkpoint 路径，因此 API 和所有
Worker 必须挂载同一个可写 checkpoint 文件。
