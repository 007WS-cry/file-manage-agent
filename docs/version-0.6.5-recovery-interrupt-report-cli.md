# 0.6.5 恢复型人工确认、部分报告和 CLI

`0.6.5` 是从 `0.6.0` 向 `0.7.0` 演进的第五批。本批不改变七个子图的业务
边界，重点补齐 Error Recovery 暂停后的用户输入、CLI 可操作提示，以及部分成功
结果的报告和 Task 语义。

## 两种 interrupt 协议

主版本人工审核继续使用 `file_governance_review`：

```json
{
  "selections": {
    "<group_id>": "<file_id>"
  },
  "review_note": "可选说明"
}
```

恢复型人工确认使用独立的 `error_recovery` kind，并只接受当前 interrupt
`allowed_actions` 中的动作：

- `retry`：人工授权重新执行固定失败节点；
- `skip_file`：跳过当前关联文件并登记安全降级；
- `provide_path`：校验并替换只读输入目录后重新执行；
- `abort`：终止恢复并进入失败报告。

`provide_path` 必须携带非空 `replacement_path`，其他动作不得携带该字段。
替换路径必须是已存在的普通目录，不能是符号链接，也不能与产物目录、报告目录、
应用数据库或 checkpoint 路径重叠。

## CLI 提示

CLI 继续输出单个最小 JSON 对象，不在标准输出混入额外自由文本。每个字典型
interrupt 保留原始受控字段，并依据 kind 增加：

- `cli_prompt`：说明当前需要提交 selections 还是 action；
- `response_example`：与当前协议兼容的最小 JSON 示例。

未知 interrupt kind 只生成通用提示，不推断动作或动态字段。CLI 仍不输出文档
正文、完整报告、Task 输入输出引用、Prompt、模型响应或 Recovery 内部状态。

## 部分成功报告

所有报告类型都可以追加两个独立章节：

- “已恢复错误”：展示节点、阶段、恢复方式、有限重试次数、Task ID 和脱敏说明；
- “降级项”：展示固定动作、影响、受影响文件 ID 和降级摘要。

`ReportState.recovered_error_ids` 与 `degradation_ids` 和正文使用同一状态来源并
保持稳定去重。存在已恢复错误且没有未解决错误时，报告摘要明确标记“结果为部分
完成”。恢复说明不包含堆栈、原始文件路径、完整输入、用户 note 或正文。

## Task 和运行状态

安全降级对应的业务 Task 保持 `partial`，Report Task 表示报告是否成功生成，
因此仍为 `completed`。`partial` 是正常终态之一：

- 不计入 CLI 的 `failed` 数量；
- 不会被报告收口节点改写为 `failed`；
- Todo 可按完成态展示；
- 最终运行在没有未解决错误时标记为 `partial`，而不是 `failed`。

只有仍为 pending、retrying、waiting_human 或 failed 的未解决错误才会使最终
运行失败。

## 验证

本批测试覆盖：

- 四种恢复型人工动作和专用 interrupt 载荷；
- 主版本 selections 协议不受 action 协议影响；
- CLI 分类型提示、响应示例和大型状态隔离；
- 损坏 DOCX 经 parse/skip_file 降级后生成部分成功报告；
- “已恢复错误”和“降级项”正文、报告索引和磁盘文件一致；
- partial 业务 Task、completed Report Task 与零 failed Task 的统计边界。
