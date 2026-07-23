# 0.5.4 短期与长期 Memory

`0.5.4` 是从 `0.5.0` 向 `0.6.0` 演进的第四批，目标是在不扩大正文、凭据和
模型 Prompt 持久化范围的前提下，让已确认治理事实可以跨运行复用。

## 存储边界

- 短期 Memory：只存在于当前 `FileGovernanceState` 及其 Checkpointer 中，用于
  保存 Evidence、Recommendation 的固定模板阶段摘要。
- 长期 Memory：写入独立应用数据库的 `memory_items` 表，只允许人工确认选择和
  高置信度证据关系。
- LangGraph checkpoint 与应用数据库必须使用不同 SQLite 文件。
- 数据库父目录由 SQLAlchemy Engine 自动创建，数据表只能通过 Alembic 迁移创建。

默认位置：

```text
.artifacts/
├── checkpoints/file-governance.sqlite3
└── database/file-governance-app.sqlite3
```

## 主图接入

```text
load_skill_registry
  -> recall_long_term_memory
  -> plan_run_tasks
  -> ...
  -> sync_report_task_status
  -> persist_long_term_memory
  -> execute_after_run_hooks
```

Evidence 子图在置信度校验后捕获安全证据关系；Recommendation 子图在基础评分后
应用历史人工选择，并在结果校验后捕获短期摘要。历史选择只增加固定 `0.03` 分，
不会直接覆盖当前推荐，也不会绕过人工审核阈值。

## 启用方式

先升级应用数据库：

```bash
python -m alembic upgrade head
```

再在 CLI 请求信封中显式启用：

```json
{
  "memory": {
    "enabled": true,
    "namespace": null,
    "database_path": "../.artifacts/database/file-governance-app.sqlite3",
    "recall_limit": 50
  }
}
```

相对数据库路径以请求 JSON 所在目录为基准。`namespace=null` 时由输入根目录
计算哈希；显式命名空间同样只作为哈希种子。Memory 默认关闭，旧请求和旧
checkpoint 不会因为升级而自动访问应用数据库。

## 内容安全

长期条目只接受固定类型、固定模板短摘要、结构化字段白名单和受控引用。以下内容
明确禁止写入：

- 文档完整正文或长内容预览；
- API Key、Token、密码和认证头；
- 完整模型 Prompt 或响应；
- 人工审核自由文本、收件人和原始证据引用；
- 未声明的结构化业务字段。

测试会在持久化完成后直接读取 SQLite 原始字节，确认文档长正文、API Key 和完整
模型 Prompt 均不存在。Memory 召回或写入失败只产生非致命错误；治理图继续使用
当前运行事实完成报告，但不放宽内容安全规则。
