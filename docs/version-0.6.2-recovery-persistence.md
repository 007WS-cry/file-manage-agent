# 0.6.2 恢复与幂等持久化

`0.6.2` 是从 `0.6.0` 向 `0.7.0` 演进的第二批。本批把 0.6.1 已定义的
`ErrorRecord` 和 `NodeExecutionRecord` 映射到独立应用数据库，提供可重放、
可回退且不会跨图节点持有 Session 的持久化基础。

## 本批范围

- 新增 `error_recovery_records` 和 `node_execution_records` 两张表；
- 新增两个只执行查询、写入和 `flush` 的 Repository；
- 新增 Alembic `0002_error_recovery_tables` 可逆迁移；
- 将 `governance_runs.status` 的数据库白名单与 `RunState.recovering` 对齐；
- 拒绝旧 checkpoint 回退已持久化的尝试次数；
- 根据幂等键和输入摘要查询可复用节点结果；
- 验证每个模拟图节点使用独立短事务。

本批不实现：

- Error Recovery 子图及其节点；
- 主图错误出口改线；
- 自动重试、退避等待或节点跳转；
- 恢复型 `interrupt()`；
- `DegradationRecord` 持久化；
- 多进程 Worker 锁竞争和任务领取协议。

## 表结构

### error_recovery_records

一条记录对应某次治理运行中的一个 `ErrorRecord`：

- `record_id`：根据 `run_id` 和 `error_id` 计算的持久化主键；
- `run_id + error_id`：唯一约束，允许相同错误 ID 在不同运行中分别保存；
- `task_id`、`node_execution_id`：关联逻辑 Task 和节点执行；
- `stage`、`node_name`、`category`、`message`：不可变错误事实；
- `retryable`、`retry_count`、`max_retries`：有限重试策略与进度；
- `action`、`fallback`、`requires_human`：恢复决策；
- `status`、`fatal`、`created_at`、`recovered_at`：恢复生命周期。

`node_execution_id` 使用 `ON DELETE SET NULL`，避免删除节点审计记录时连带删除错误
事实；两张恢复表都通过 `run_id` 对 `governance_runs` 使用级联删除。

### node_execution_records

一条记录对应一个确定性节点幂等键：

- `idempotency_key`：主键，与 `NodeExecutionRecord.id` 一致；
- `run_id`、`task_id`、`task_execution_id`：运行和逻辑 Task 归属；
- `stage`、`node_name`、`input_digest`：不可变执行身份；
- `status`、`attempt_count`：执行状态与累计尝试次数；
- `state_update_ref`、`result_refs`、`result_digest`：受控结果引用及完整性摘要；
- `last_error_id`、`started_at`、`finished_at`：最近错误与执行时间。

只有状态为 `succeeded` 或 `reused` 且输入摘要完全一致的记录可以由
`find_reusable()` 返回。

## 幂等更新规则

Repository 更新遵守以下约束：

1. 同一 `idempotency_key` 的运行、Task、阶段、节点名和输入摘要不得改变；
2. `attempt_count` 不得小于数据库已保存值；
3. 同一 `run_id + error_id` 的 Task、节点、类别、消息和文件关联不得改变；
4. `retry_count` 不得小于数据库已保存值，也不得大于 `max_retries`；
5. 同一次节点尝试不得从成功回退到运行，已恢复或最终失败的错误不得重新打开；
6. 已成功节点的状态引用、结果引用和结果摘要不得被后续重放改写；
7. 恢复动作必须来自固定白名单，不能保存函数名、Shell 命令或任意节点名称。

这些规则用于阻止较旧 checkpoint 在进程恢复后覆盖较新的数据库事实。本批不负责
决定何时重试或复用，只提供后续恢复子图可以安全调用的确定性存储接口。

## 短事务边界

Repository 不创建、不提交也不关闭 Session。调用方必须在单个图节点内部使用：

```python
with open_application_session(session_factory) as session:
    repositories = create_repository_bundle(session)
    repositories.node_execution_records.upsert_state(execution)
```

上下文正常退出时提交，异常时回滚，并始终关闭 Session。禁止：

- 把 Session、Repository 或 RepositoryBundle 写入 LangGraph 状态；
- 在一个节点创建 Session、在另一个节点提交；
- 让 Session 跨越 `interrupt()`；
- 在 Repository 内部调用 `commit()`；
- 使用同一 Session 处理多个并行 Worker 的节点执行。

## 迁移与回退

升级到最新结构：

```bash
python -m alembic upgrade head
```

只回退第二批并保留原五张基础表：

```bash
python -m alembic downgrade 0001_application_tables
```

正式部署必须先备份应用数据库。应用数据库仍不得与 LangGraph checkpoint 共用同一
SQLite 文件，也不得位于只读业务输入目录中。

## 后续批次接入约束

后续 Error Recovery 图接入时必须：

- 每个持久化节点独立打开短事务；
- 先查询 `find_reusable()`，再决定是否执行节点；
- 只复用输入摘要一致且结果引用通过完整性校验的记录；
- 在重试前持久化新的 `attempt_count`，失败后关联 `last_error_id`；
- 不因数据库暂时不可用而修改业务文件或跳过输入只读边界；
- 为 SQLite 锁竞争、多进程重复领取和进程崩溃补充故障注入测试。
