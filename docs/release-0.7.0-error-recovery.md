# 0.7.0 Error Recovery 正式发布

`0.7.0` 将 0.6.1 至 0.6.5 的状态协议、短事务持久化、Error Recovery 子图、
既有节点接入、人工恢复、CLI 和部分成功报告合并为一个可发布版本。本次不扩大
文件治理权限：原始输入仍保持只读，自动恢复只允许有限重试和固定安全降级。

## 发布能力

- 十六类错误使用确定性策略决定有限重试、指数退避、人工处理或固定降级；
- 第七个 Error Recovery 子图统一处理未决错误，顶层只通过固定白名单重试或续跑；
- `error_recovery_records` 保存错误恢复生命周期，
  `node_execution_records` 保存幂等键、输入摘要、结果引用和尝试次数；
- coordinator、no-memory、default-skill、keep-context、skip-file 和
  partial-result 继续保留原有确定性能力，但统一生成恢复终态和降级审计；
- 恢复型人工输入支持 `retry`、`skip_file`、`provide_path`、`abort`，并与
  主版本 `selections` 协议保持隔离；
- 报告独立展示“已恢复错误”和“降级项”，安全降级结果使用 `partial` 而不是
  `failed`。

## 故障注入验收

正式版本新增九组集成测试：

1. transient 子图超时有限重试并从固定后继继续；
2. 损坏 DOCX 只跳过关联文件，保留正常文件的推荐和报告；
3. Content Subagent 崩溃后使用 coordinator；
4. Memory 召回失败后使用 no-memory；
5. Skill 注册表失败后使用 default-skill；
6. 两个 Context Compact 安全点失败后使用 keep-context；
7. 人工恢复 checkpoint 不包含正文、Team Message 或完整报告；
8. 相同节点幂等键只执行一次，并在数据库保留唯一记录；
9. 0.6.0 checkpoint 自动补齐恢复字段且治理结论不变。

这些测试同时验证所有错误具有 `task_id`、`node_execution_id`、重试和恢复字段，
已恢复历史错误不会再次触发顶层 Recovery。

## 兼容与升级

0.6.0 状态缺少以下字段时，`initialize_run` 会补齐安全默认值：

- `recovery`；
- `node_executions`；
- `degradations`；
- `report.recovered_error_ids`；
- `report.degradation_ids`。

主版本推荐仍使用 `recommended_file_id`、`needs_human_review` 和 `selected_by`，
人工确认继续提交 `selections`。升级应用数据库前执行：

```bash
python -m alembic upgrade head
```

迁移会保留五张既有应用表，并增加恢复与节点执行两张表。应用数据库与 LangGraph
checkpoint 必须继续使用不同 SQLite 文件。

## 安全边界

- 每个图节点只使用短事务，Session、Repository 和连接不得进入状态或跨
  `interrupt()` 存活；
- 人工替换路径必须重新校验符号链接、目录类型及与输入、产物、报告、数据库、
  checkpoint 的隔离；
- 节点结果只有在幂等键、输入摘要、受控产物路径和结果摘要全部一致时允许复用；
- 恢复状态不接收文档正文、完整 Prompt、模型响应、Team Message、完整报告、
  堆栈或凭据；
- 自动恢复不得删除、移动、重命名或覆盖原始文件。

## 发布验证

发布前执行：

```bash
python -m pytest
python -m ruff check app tests
python -m compileall -q app tests
python -m pip wheel . --no-deps --no-build-isolation
```

构建命令可以重新生成 `build/`、`dist/` 和 `file_manage_agent.egg-info/`；这些目录
不是源码，不应手工修改。
