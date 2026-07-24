# 0.6.4 六个既有子图和工具统一接入

`0.6.4` 是从 `0.6.0` 向 `0.7.0` 演进的第四批。本批不新增业务子图，重点是让
Inventory、Version Analysis、Evidence、Recommendation、Team Orchestration、
三个固定 Subagent、Skills、Memory、Context Compact、Lifecycle、Hooks 和 Task
工具产生的错误进入同一状态、策略、路由与持久化协议。

## 统一错误上下文

`ErrorContextState` 只包含 `run_id`、`task_id`、`task_execution_id` 和当前
Recovery Policy 快照。顶层转换器把该状态按值复制到业务子图和固定 Subagent，
`Send` Worker 也显式携带副本。旧版独立子图输入缺少该字段时，
`create_error_context()` 会使用稳定兼容值补齐，不要求调用方立即迁移。

业务节点统一调用 `create_node_error()`，只提供阶段、节点函数名、错误类别、脱敏
消息、关联文件和可选异常对象。该入口负责：

- 补齐非空 `task_id` 和稳定 `node_execution_id`；
- 应用当前类别的 `retryable`、`max_retries`、`fallback` 与人工恢复设置；
- 在 Recovery 启用时保持错误为待处理状态，由顶层统一决定动作；
- 同一节点执行重放时继承既有 `retry_count` 和首次捕获时间。

## 路由与既有降级

顶层路由只检查当前阶段的未解决错误。`recovered` 和 `fallback_applied` 记录保留
审计价值，但不会再次进入 Recovery。Memory 召回、两个 Context Compact 安全点
和 Memory 持久化由显式 `conditional_edge` 接入恢复入口。

原有确定性回退能力继续保留：

- LLM 或 Team Protocol：`coordinator`；
- Memory：`no_memory`；
- Context Compact：`keep_context`；
- Skill：`default_skill`；
- 比较或证据：`partial_result`。

节点仍负责产生安全的本地回退结果，Recovery 负责把错误更新为恢复终态、登记
`DegradationRecord` 并推进 Task。一个 Task 上由同一回退链产生的关联错误会一起
收敛，避免后续阶段重复触发。

## 重试、复用与持久化

重试后的业务包装节点如果再次返回同一错误，错误工厂保留已登记的重试次数。
一个表面成功的节点执行产物若仍包含当前未解决错误，则不能作为成功结果复用，
必须重新执行；真实成功时才把活动错误更新为 `recovered`。

业务节点产生的内部错误在写入 `error_recovery_records` 前，会先补建对应失败
`node_execution_records`。节点执行与错误恢复仍分别使用
`open_application_session()` 短事务，SQLAlchemy `Session` 不进入 LangGraph
状态，不跨节点、条件边、子图调用或 `interrupt()` 存活。

## 验证

本批增加统一错误身份、重试进度继承和历史恢复终态过滤测试，并更新 Evidence
与 Version Analysis 的部分结果预期。完整测试覆盖：

- 六个既有子图和固定 Subagent 的正常、失败及本地回退路径；
- 未捕获异常有限重试、恢复终态与节点结果复用；
- 无效发送日志、缺失 LLM API Key 和关联错误统一降级；
- 两张恢复表的短事务 Repository 与 Alembic 迁移；
- 旧状态、关闭 Recovery 和独立子图调用兼容。
