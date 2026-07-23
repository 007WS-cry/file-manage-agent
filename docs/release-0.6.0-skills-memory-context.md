# 0.6.0 Skills、Memory、Context Compact 正式发布

`0.6.0` 完成从 `0.5.0` 开始的六批演进：应用数据库、LangChain 多模型路由、
Task 级 Skills、安全短期与长期 Memory、Context Compact，以及发布期数据库接线
和兼容性验证已经形成一条可运行、可暂停恢复、可迁移回退的完整治理路径。

## 发布范围

- Content、Version、Evidence 三类 Task 可路由不同模型 Profile；
- Claude、Gemini、GLM、DeepSeek、Qwen、OpenAI-compatible 中转站和 LiteLLM
  均通过按需安装的 LangChain Provider 接入；
- Provider 失败、超时或结构化输出不合法时继续使用确定性回退；
- 每个 Task 只加载匹配的 `SKILL.md`，分派结束后恢复为 `available`；
- 短期 Memory 只存在于当前 LangGraph 状态；
- 长期 Memory 通过哈希命名空间跨运行召回；
- Context Compact 在 Inventory、Evidence 后的固定安全点释放无用上下文；
- `governance_runs`、`memory_items`、`context_summaries`、
  `tool_call_audits`、`human_reviews` 五张应用表已经全部接线；
- 0.5.0 状态缺少 Skills、Memory、Context Compact 或数据库字段时自动补齐
  安全关闭值。

## 运行和持久化顺序

```text
initialize_run
  -> 写入 governance_runs 初始记录
  -> before_run Hooks
  -> Inventory / Skills / 多模型 Subagents
  -> Memory / Context Compact
  -> 报告与人工审核
  -> persist_long_term_memory
  -> flush_tool_audit_hook
  -> finalize_run
       -> 更新 governance_runs
       -> 幂等写入 human_reviews
```

工具审计根据最终治理事实保存文件扫描、文档解析和本地发送日志调用。扫描输出只
保存数量；文档解析只保存固定短摘要、受控 `content_ref` 和产物字节数。完整正文、
工具参数、完整输出、API Key、完整模型 Prompt 和人工审核自由文本不会写入审计表。

## 两套 SQLite

应用数据库默认位置：

```text
.artifacts/database/file-governance-app.sqlite3
```

LangGraph Checkpointer 默认位置：

```text
.artifacts/checkpoints/file-governance.sqlite3
```

两者必须是不同文件。状态工厂和 Engine 会拒绝相同路径，也会拒绝把数据库放在
只读输入目录内部。

Engine 第一次连接时会自动创建应用数据库父目录和 SQLite 文件，但不会自动建表。
正式表结构必须由 Alembic 管理：

```bash
python -m alembic upgrade head
```

验证回退：

```bash
python -m alembic downgrade base
python -m alembic upgrade head
```

应用数据库可以在请求中启用：

```json
{
  "application_database": {
    "enabled": true,
    "backend": "sqlite",
    "database_path": "../.artifacts/database/file-governance-app.sqlite3",
    "auto_create_parent": true,
    "echo": false,
    "timeout_seconds": 30.0
  }
}
```

也可以由 CLI 覆盖：

```bash
file-governance run examples/sample_multi_model_request.json \
  --thread-id governance-release-001 \
  --checkpoint-path .artifacts/checkpoints/file-governance.sqlite3 \
  --application-database-path .artifacts/database/file-governance-app.sqlite3
```

相对请求路径以请求 JSON 所在目录为基准。环境变量
`FILE_GOVERNANCE_DATABASE_PATH` 可覆盖 Alembic 以及未显式给出路径的应用数据库
默认值。

## 发布验收

发布测试覆盖：

1. 0.5.0 状态升级后治理事实、报告和确定性结果不变；
2. 三个固定 Task 路由到不同模型 Profile；
3. 模型失败后仍产生相同确定性版本结论；
4. Task Skill 只按需加载并在结束后释放正文；
5. 数据库连接释放并重建后仍可召回长期 Memory；
6. Context Compact 开关前后的版本边、分叉、推荐和人工选择完全一致；
7. 大型文档解析输出只在产物文件保存正文，审计表只保存引用；
8. Alembic 可创建、回退并重新创建全部五张应用表；
9. 应用数据库与 Checkpointer 使用不同文件；
10. 完整运行前后的原始输入文件字节完全一致。

多模型、Skills、Memory、Context Compact 和五表接线的组合请求见
`examples/sample_multi_model_request.json`。

## 升级说明

从 `0.5.x` 升级时：

1. 安装 `0.6.0` 依赖；
2. 备份现有 `.artifacts`；
3. 执行 `python -m alembic upgrade head`；
4. 为应用数据库和 checkpoint 配置两个不同的 SQLite 路径；
5. 先保持真实模型关闭执行一次确定性验收；
6. 再按 Task 逐个启用所需 Provider Profile。

应用数据库、Memory 和 Context Compact 默认关闭，因此旧请求不增加外部模型调用
或数据库写入。启用数据库但未执行迁移时，治理流程会保留确定性报告并记录安全
降级错误，不会使用 ORM `create_all()` 静默修改生产表结构。

## 后续版本

以下能力不属于 `0.6.0`：

- 跨节点统一错误恢复策略；
- 多进程 Worker、任务队列和进程协调；
- MCP 邮件或远程证据工具；
- HTTP API、定时任务和后台服务；
- PostgreSQL 等生产级 Checkpointer。

这些能力将在后续版本独立设计，避免把进程调度、外部工具权限和错误恢复语义混入
本次只读文件治理发布。
