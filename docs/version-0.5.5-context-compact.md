# 0.5.5 Context Compact

`0.5.5` 是从 `0.5.0` 向 `0.6.0` 演进的第五批。本批通过独立 LangGraph 子图
压缩已经不再参与后续计算的大型上下文，同时明确禁止改写版本、证据、推荐和
人工审核事实。

## 触发条件

Context Compact 默认关闭。启用后，`estimate_context_tokens` 使用本地确定性
规则估算 Prompt 与文档状态：

- ASCII 字符按每四字符一个 Token 近似；
- 中文等非 ASCII 字符按一字符一个 Token 保守估算；
- 不调用模型、远程 tokenizer 或第三方服务；
- 只有估算值超过阈值且当前阶段存在可回收字段时才执行压缩。

独立子图通过 `route_context_compaction` 条件边选择压缩或跳过：

```text
START
  -> estimate_context_tokens
  -> [compact_context | mark_context_compaction_skipped]
  -> persist_context_compaction_artifact
  -> persist_context_summary
  -> END
```

## 两个安全点

### after_inventory

Inventory 已完成后，只清空已加载的 System Prompt 正文和动态规则。Prompt
版本、来源、SHA-256 和加载状态继续保留，全部 `DocumentRecord` 保持不变，因此
Content、Version Analysis 和 Evidence 仍接收相同文档事实。

### after_evidence

Evidence 和固定 Evidence Subagent 均完成后，Recommendation 不再读取文档详情。
此时可移出：

- `content_preview`
- `structure_summary`
- `key_fields`

压缩后的文档仍保留：

- `id`、`file_id`
- `parser_name`
- `content_ref`
- `normalized_digest`
- `warnings`

完整标准化内容可以继续通过 `content_ref` 重建。

## 产物和数据库

文档详情先进入 `UntrackedValue` 临时字段，随后原子写入：

```text
.artifacts/content/intermediate/
  <run_id>-context-compact-<index>.json
```

临时载荷不会进入 checkpoint。Prompt 正文不会写入该产物。

应用数据库的 `context_summaries` 表只保存：

- 固定模板摘要；
- 压缩阶段和序号；
- 压缩后的 Token 估算；
- 受控产物引用。

该表已由 `0001_create_application_tables` 创建，因此没有新增迁移版本。启用摘要
持久化前仍须执行：

```bash
python -m alembic upgrade head
```

## 配置

```json
{
  "context_compact": {
    "enabled": true,
    "trigger_token_threshold": 12000,
    "retained_preview_characters": 0,
    "persist_summaries": true,
    "database_path": "../.artifacts/database/file-governance-app.sqlite3"
  }
}
```

数据库父目录由 Engine 自动创建，表结构由 Alembic 管理。应用数据库不得位于
只读输入目录内，也不得与 SQLite Checkpointer 共用文件。

## 决策不变性

压缩服务和子图状态不接收以下字段：

- `version_edges`
- `branches`
- `decisions`
- `human_review`

集成测试使用同一组真实 DOCX 分别运行启用和关闭压缩的完整主图，并在人工暂停
及恢复后逐值比较上述字段。任何差异都会使版本验收失败。
