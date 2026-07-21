# 0.3.1 确定性 Task System

## 版本目标

`0.3.1` 是从 `0.3.0` 向 `0.4.0` 开发的第一批版本。本批只建立 Team
Orchestration 所需的状态协议和确定性纯服务，不修改 Inventory、Version Analysis、
Evidence、Recommendation 四个业务子图，也不调用真实 LLM 或 Subagent。

## 固定 Task DAG

每次治理运行使用以下六个 Task：

```text
inventory
  -> version_analysis
  -> evidence
  -> recommendation
  -> human_review
  -> report
```

Task ID 由 `run_id:task_type` 确定性生成。`human_review` 始终存在于 DAG 中；后续
接入顶层流程时，如果推荐结果无需人工确认，应将该 Task 正常标记为 `skipped`，
使 Report 仍能沿固定依赖继续执行。

逻辑角色分配如下：

| Task | assigned_role |
| --- | --- |
| Inventory | `content` |
| Version Analysis | `version` |
| Evidence | `evidence` |
| Recommendation | `coordinator` |
| Human Review | `coordinator` |
| Report | `coordinator` |

角色字段只是后续固定 Subagent 的职责占位。`0.3.1` 中所有业务步骤仍由主 Agent
执行，Task System 不访问网络、不读取业务文件，也不调用工具或模型。

## 幂等与恢复边界

`create_task_dag()` 接收运行 ID、运行创建时间和可选已有 Task：

- 相同运行 ID 和创建时间生成完全相同的 DAG；
- 已有 Task 按 `task_id` 保留，不重复创建；
- 已有状态、输入输出引用、错误、创建时间和更新时间不被重置；
- 不完整 checkpoint 只补齐缺失的固定 Task；
- 不属于当前运行或依赖偏离固定模板的 Task 会被拒绝。

Task reducer 使用 `merge_by_task_id()`。它只合并字段并保持首次出现顺序，不负责
推进状态或更新时间，避免 reducer 在 LangGraph 汇合时产生隐藏业务行为。

## DAG 校验

`topologically_sort_tasks()` 使用稳定拓扑排序：同时可执行的 Task 保持输入顺序。
以下情况均抛出明确的 `ValueError`：

- DAG 为空；
- `task_id` 为空或重复；
- 同一 Task 重复声明相同依赖；
- 引用未知依赖；
- Task 依赖自身；
- 多个 Task 构成循环依赖。

## Todo 单向投影

Todo 不是执行状态。`update_todos_from_tasks()` 不接收旧 Todo，每次只根据完整 Task
DAG 创建四个用户视图：

| Todo | 关联 Task |
| --- | --- |
| 准备文件事实 | Inventory |
| 建立版本治理结论 | Version Analysis、Evidence、Recommendation |
| 完成人工确认 | Human Review |
| 输出治理报告 | Report |

状态规则：

- 任一关联 Task 为 `failed`：`blocked`；
- Task 因失败依赖而 `skipped` 且带有错误：`blocked`；
- 全部关联 Task 为 `completed` 或正常 `skipped`：`completed`；
- 任一 Task 已开始或部分完成：`in_progress`；
- 其他情况：`pending`。

Task 的 `input_refs`、`output_refs` 和 `error` 只能保存状态键、产物引用和简短错误，
不得保存完整文档正文、密钥或客户信息。

## 本批文件

新增：

- `app/services/task_system.py`
- `tests/unit/test_task_system.py`

修改：

- `app/state/models.py`
- `app/state/reducers.py`
- `app/state/factories.py`
- `app/state/__init__.py`
- `app/services/__init__.py`
- `Dockerfile`
- `.dockerignore`
- `.gitignore`
- `README.md`
- `SECURITY.md`
- `pyproject.toml`
- `app/__init__.py`

## 下一批边界

下一批再新增 Team Orchestration LangGraph 子图、状态转换器和顶层同步节点。本批不应
让 Task System 侵入四个既有业务子图，也不实现真实 Subagent、Team Protocol、
重试、Worktree、后台执行或数据库任务队列。
