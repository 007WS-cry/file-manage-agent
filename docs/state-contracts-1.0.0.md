# 1.0.0 状态契约

## 统一约束

所有 LangGraph 状态类、嵌套状态和 Worker 状态均定义在
`app/state/models.py`。状态转换位于 `app/state/converters.py`，初始值位于
`app/state/factories.py`，列表合并规则位于 `app/state/reducers.py`。

状态只保存 JSON 可序列化数据、受控产物引用和脱敏事实，不保存以下对象：

- SQLAlchemy Engine、Session 或 Repository；
- 打开的文件、锁、线程、进程或网络客户端；
- API Key、数据库密码、完整异常堆栈；
- 不受限的文档正文、完整 Tool 输出或邮件正文。

## 顶层状态

`FileGovernanceState` 是主图唯一顶层状态。核心字段分为：

| 分组 | 代表字段 | 契约 |
| --- | --- | --- |
| 运行 | `run` | 运行、线程、触发源、前后台方式和当前阶段 |
| 请求 | `request` | 扫描、分组、推荐、证据阈值和文件上限 |
| 工作区 | `workspace` | 输入只读，产物、报告和临时目录相互隔离 |
| 生命周期 | `prompt`、`hooks` | 默认关闭，只接受固定协议和白名单 |
| 任务 | `tasks`、`todos` | Task 是事实来源，Todo 仅由 Task DAG 投影 |
| 业务 | `files`、`documents`、`version_groups`、`diffs`、`decisions` | 使用稳定 ID 和受控引用 |
| 证据 | `pdf_exports`、`deliveries` | 明确匹配方法、置信度和来源 |
| 恢复 | `errors`、`recovery`、`node_executions`、`degradations` | 有限重试、固定动作、幂等记录 |
| 输出 | `report` | 报告正文只在内部状态，API 仅提供受控下载 |

主图状态必须能够从 0.6.0 兼容状态补齐恢复字段，但兼容转换不得降低重试计数、覆盖
人工选择或绕过路径安全校验。

## 运行状态

`RunState` 的 1.0.0 外围字段包括：

- `run_id`：治理运行唯一 ID；
- `thread_id`：LangGraph checkpoint 线程 ID；
- `trigger_source`：`manual` 或 `cron`；
- `execution_mode`：`foreground` 或 `background`；
- `background_job_id`：前台为 `None`；
- `worker_id`：尚未领取或前台运行时为 `None`；
- `status`：从 created/queued/running/waiting_human 收口到
  completed/partial/failed；
- `current_stage`、`started_at`、`finished_at`：用于状态查询和审计。

数据库 datetime 转回状态时统一规范化为带 UTC 时区的 ISO 8601。SQLite 返回的无时区
datetime 不得直接写回严格状态协议。

## 后台任务状态

`BackgroundJobState` 是 API、队列和 Worker 共用的持久化协议：

| 字段 | 含义 |
| --- | --- |
| `id`、`run_id`、`thread_id` | 任务、运行和 checkpoint 身份 |
| `trigger_source` | 手动提交或 Cron |
| `status` | queued、running、waiting_human、resume_queued 或终态 |
| `request_payload` | 已规范化、可 JSON 序列化且不含凭据的请求信封 |
| `attempt_count`、`max_attempts` | 普通领取次数和上限 |
| `resume_count` | 已实际应用的人工恢复次数 |
| `pending_interrupt` | 当前唯一可恢复中断的脱敏快照 |
| `resume` | 等待 Worker 应用的幂等恢复请求 |
| `report_path` | 内部路径，下载时仍需重新校验 |

`attempt_count` 与 `resume_count` 不可混用。人工等待和正常恢复不消耗异常重试预算。

## 中断状态

`PendingInterruptState` 固定包含：

- `interrupt_id`：由当前 checkpoint 中断内容确定的身份；
- `kind`：`file_governance_review` 或 `error_recovery`；
- `payload`：经过字段白名单和大小限制的中断载荷；
- `created_at`：Worker 保存中断的带时区时间。

`BackgroundResumeState` 固定包含：

- `request_id`：调用方生成的幂等键；
- `interrupt_id`：必须等于当前 pending interrupt；
- `kind`：必须与中断协议一致；
- `value`：经分类型校验的人工输入；
- `status`：pending、applied 或 failed；
- 创建、应用时间和脱敏错误摘要。

相同 `request_id`、中断和值重复提交返回同一结果。相同 `request_id` 配不同内容、旧
`interrupt_id`、错误 `kind` 或终态上的新恢复请求均被拒绝。

## 主版本审核协议

`file_governance_review` 的恢复值为：

```json
{
  "selections": {
    "<group_id>": "<该组候选 file_id>"
  },
  "review_note": "可选说明"
}
```

每个 pending group 必须且只能选择组内候选。自由文本说明不会进入长期 Memory，也不应
出现在公开日志。

### 1.0.2 语义审核扩展

1.0.2 为 `DiffRecord` 增加以下字段，不改变原有版本方向、相似度和版本边事实：

| 字段 | 含义 |
| --- | --- |
| `change_evidence` | 有数量和字符上限的关键字段、段落或结构差异，使用稳定 `diff:` 引用 |
| `semantic_changes` | 经 Pydantic、引用白名单及 old/new 原文复核后的业务语义分类 |
| `review_priority` | 规则引擎计算的 `not_assessed`、`low`、`medium` 或 `high` |
| `review_reasons` | 规则提高审核优先级的有界解释 |

`VersionSubagentOutput` 只允许 `summary`、`semantic_changes` 和 `artifact_refs`。
LLM 不能返回审核动作或审核优先级。高重要性语义变更是否强制人工审核，由
Recommendation 子图中的确定性规则决定。人工审核中断可展示截断后的 old/new 值、
业务影响、置信度和 `diff:` 引用，但不得携带完整文档正文。

## 错误恢复协议

`error_recovery` 的恢复值为：

```json
{
  "action": "retry | skip_file | provide_path | abort",
  "replacement_path": "仅 provide_path 必需",
  "note": "可选说明"
}
```

实际允许动作由中断载荷 `allowed_actions` 决定。`provide_path` 必须重新执行目录类型、
符号链接、只读输入、数据库和输出目录隔离校验；`skip_file` 只登记降级，不删除文件；
`abort` 安全收口为失败。

## 数据库引用状态

应用数据库状态只允许保存：

- `backend`：sqlite 或 postgresql；
- SQLite `database_path`，或 PostgreSQL `database_url_env`；
- 连接状态、SQL 日志开关和超时。

PostgreSQL URL 的实际值只从固定环境变量读取，禁止进入 `request_payload` 和
checkpoint。后台任务无论使用哪种应用数据库，`checkpoint.backend` 都固定为 sqlite。

## Reducer 与幂等

具有稳定 `id` 的列表使用 `merge_by_id` 合并。更新同一 ID 时必须保留协议一致性，不能
通过 checkpoint 重放降低 attempt/retry 计数。`NodeExecutionRecord` 只有在
idempotency key、输入摘要、受控产物引用和结果摘要全部匹配时才允许复用。

PDF `Send` Worker 使用 `PdfMatchWorkerState`，每个 Worker 只接收一个 `job` 和只读文件、
文档快照，返回通过 reducer 合并的 `pdf_match_jobs`、`pdf_exports` 与 `errors`。

## API 输出边界

API Schema 不直接返回 `FileGovernanceState`：

- 运行查询仅返回运行摘要、后台任务摘要、当前 pending interrupt 和报告可用性；
- 报告正文通过 `GET /runs/{run_id}/report` 下载；
- API 不返回请求信封、文档正文、Prompt、Task 输入输出引用或 checkpoint；
- CLI 仅输出 Todo 白名单字段、Task 状态计数、报告路径和分类型中断提示。

## 版本兼容

1.0.2 的语义字段由版本比较重新生成，不新增数据库迁移；既有 1.0.0 状态扩展仍保持
向后补齐，不更改 0.8.2 数据库迁移链。升级前应备份应用数据库和
checkpoint；升级后执行 `alembic upgrade head`。正在等待人工输入的后台任务必须保留
原数据库、checkpoint、`run_id` 和 `thread_id`，然后由 1.0.2 API 使用当前
`interrupt_id` 恢复。
