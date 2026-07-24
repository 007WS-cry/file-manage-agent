# 0.6.1 Error Recovery 状态协议与恢复策略

`0.6.1` 是从 `0.6.0` 向 `0.7.0` 演进的第一批。本批只建立统一错误恢复需要的
状态、配置、纯策略服务和兼容入口，不新增 Error Recovery 图节点，不修改现有
conditional edge，也不改变文件治理结论。

## 本批范围

本批新增：

- 错误重试、降级、人工恢复和生命周期字段；
- Recovery、Node Execution、Degradation 和未来 Recovery 子图状态；
- Task 稳定执行 ID、尝试次数、重试中和部分完成状态；
- 报告引用的降级记录与已恢复错误 ID；
- 十六类错误的确定性策略快照；
- 有限重试次数和指数退避计算；
- 安全降级和人工恢复动作推荐；
- 0.6.0 错误构造及旧顶层状态兼容；
- 默认 YAML 策略与代码默认值的一致性测试。

本批不实现：

- `app/graphs/error_recovery.py`；
- `app/nodes/error_recovery.py`；
- 主图错误出口改线；
- 自动重试、暂停恢复或动态 `Command(goto=...)`；
- 错误恢复和节点执行数据库表；
- 部分成功报告中的降级章节。

## 目录职责

- `app/state/models.py` 集中定义所有状态类及被状态引用的子类；
- `app/state/factories.py` 创建和复制 Recovery 状态；
- `app/services/recovery_policy.py` 只执行配置校验和纯策略计算；
- `app/utils/runtime.py` 创建兼容旧调用的结构化错误；
- `app/nodes/lifecycle.py` 中的 `initialize_run()` 仍是主图明确注册的节点，只负责
  为旧状态补齐顶层默认字段；
- `app/graphs/routers.py` 本批不修改，继续只保存被 conditional edge 明确调用的路由。

## 错误生命周期兼容

旧版调用仍可只传入：

```python
create_error_record(
    stage="inventory",
    node_name="extract_docx_content",
    category="parse",
    message="文件解析失败",
    related_file_id="file-1",
    fatal=False,
)
```

旧参数生成的错误 ID 算法保持不变。因为 0.6.0 节点已经自行应用跳过或确定性回退，
未显式提供恢复状态的非致命错误默认为 `recovered`，致命错误默认为 `failed`。
后续 Recovery 接入节点需要显式传入 `status="pending"`，或调用
`apply_recovery_policy_to_error()` 补齐类别策略。

## 默认策略

默认策略遵循以下顺序：

1. 有剩余次数且类别允许时选择 `retry`；
2. 重试不可用或耗尽后，有安全降级时选择 `fallback`；
3. 没有安全降级但允许人工处理时选择 `wait_human`；
4. 其余情况选择 `abort`；
5. 策略关闭或错误已经恢复时选择 `none`。

当前安全降级白名单为：

```text
skip_file
coordinator
no_memory
default_skill
keep_context
partial_result
```

任何配置都不能声明动态 Python 函数、任意图节点、Shell 命令或文件写入动作。

## 状态初始化

新运行通过 `create_initial_state()` 获得：

```text
recovery.policy              完整恢复策略快照
recovery.pending_error_ids   空列表
recovery.current_error_id    null
recovery.action              none
recovery.human.kind          error_recovery
node_executions              空列表
degradations                 空列表
report.degradation_ids       空列表
report.recovered_error_ids   空列表
```

0.6.0 checkpoint 缺少这些字段时，`initialize_run()` 使用相同默认值补齐。复制函数
会解除列表和嵌套策略的可变引用，并拒绝未知恢复动作。

0.6.0 Task 缺少 `execution_id` 和 `attempt_count` 时，固定 Task DAG 重建会根据
`run_id` 与 `task_type` 补齐稳定执行 ID，并将尝试次数设为零。每次从 `pending`
或 `retrying` 进入 `running` 才增加一次尝试；`partial` 是保留可用降级结果的终态，
可满足后续普通 Task 的依赖。

## 退避边界

退避公式为：

```text
min(
    initial_backoff_seconds * backoff_multiplier ** (retry_number - 1),
    max_backoff_seconds,
)
```

`retry_number` 从一开始计数，且不得超过 `max_retries`。本批不加入随机抖动，保证
相同策略和重试序号得到相同结果。

## 后续批次接入约束

后续 Error Recovery 子图必须：

- 只处理 `pending`、`retrying` 或 `waiting_human` 错误；
- 不把 0.6.0 已经处理的 `recovered` 警告重新入队；
- 使用 `NodeExecutionRecord.id` 判断是否可以复用已完成结果；
- 使用独立 `kind="error_recovery"` 的 interrupt；
- 保留现有 `kind="file_governance_review"` 主版本确认协议；
- 在修改主图之前先增加错误恢复持久化和故障注入测试。
