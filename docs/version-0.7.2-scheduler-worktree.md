# 0.7.2 APScheduler 与 Worktree Isolation

`0.7.2` 是向 `0.8.0` 演进的第二批，建立两个彼此隔离的外围能力：

- APScheduler 从 `scheduled_jobs` 恢复 Cron 计划，触发回调只创建
  `background_jobs`，不调用 LangGraph；
- Team Orchestration 只为 `requires_repository_write=true` 的 Task 创建 Git
  Worktree，普通文件治理 Task 不启动 Git 子进程。

## Scheduler 边界

API 提供计划创建、查看、启用和停用接口。独立 Scheduler 进程定期从应用数据库
同步计划，因此 API 进程不承担 Cron 执行。每次 Cron 回调复用 0.7.1 的持久化
入队协议，创建 `trigger_source=cron` 的运行与后台任务，实际治理图仍由独立
Worker 领取执行。

计划表是唯一事实来源，APScheduler 使用可重建的进程内 Job，不引入第二套任务
持久化数据库。服务重启后会重新注册所有启用计划。

## Worktree 边界

Worktree 工具只允许 `rev-parse`、`status` 和 `worktree` 三个 Git 子命令，参数以
argv 列表传入，禁止 Shell 拼接。参数形状进一步固定为顶层/HEAD 查询、porcelain
状态读取、新分支创建和不带 `--force` 的安全移除；`prune`、`move` 等操作会在
进程启动前被拒绝。工作目录和绝对路径参数必须位于主仓库或显式临时根目录内，
Git 调用会禁用仓库 Hook，并具有固定超时和有限输出。

普通六阶段治理 Task 的 `requires_repository_write` 默认都是 `false`。显式写仓库
Task 还必须提供 `workspace.project_git_root` 和 `workspace.temporary_root`。
关闭阶段先检查 porcelain 状态：

- Worktree 干净时执行不带 `--force` 的安全移除，并保留隔离分支；
- 存在改动时保留目录与分支，状态记为 `completed`；
- 检查或移除失败时状态记为 `failed`，不执行强制删除或自动合并。

## 多进程启动

```bash
python -m alembic upgrade head
file-governance-api
file-governance-worker
file-governance-scheduler
```

也可以使用 `docker compose up --build` 同时启动迁移、API、Worker 和 Scheduler。
