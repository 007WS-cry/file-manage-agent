# 受约束 Recommendation Judge

## 目标

1.0.5 在确定性 Recommendation 之后增加固定
`Recommendation Judge Subagent`。它为主版本推荐提供一个结构化第二意见，
但不能直接改写版本事实、审核阈值或系统动作。

## 压缩决策包

Judge 不接收完整 DOCX、XLSX、PDF、邮件或正文。每个版本组只传入：

- 确定性候选分数；
- 版本链位置；
- 有界语义变更摘要；
- 有界发送、PDF 和业务证据摘要；
- 分支、关系冲突、高重要性变更和证据审核风险；
- 允许返回的受控证据引用。

决策包最多保留八个高分候选；每类候选摘要最多三条、每条最多 160 字符，
且整体 JSON 还要通过 16000 字符上限校验。确定性候选即使不在高分截断范围内，
也会被保留在白名单中供两路结果比较。

被作废、拒绝或标记为仅供参考的文件会先被确定性证据规则移出 Judge
候选白名单，模型不能把它们重新加入竞争。

## 输出协议

Judge 只能返回以下结构：

```json
{
  "recommended_file_id": "v4",
  "confidence": 0.78,
  "supporting_reasons": [],
  "counterarguments": [],
  "missing_information": [],
  "should_abstain": false
}
```

Team Protocol 另外保留 `summary` 和 `artifact_refs` 用于消息与审计。
`recommended_file_id` 必须属于输入候选；弃权时必须为 `null`。
`artifact_refs` 必须属于决策包白名单。默认关闭真实 LLM 或模型输出失败时，
回退结果始终弃权，不伪造模型推荐。

## 确定性融合矩阵

| 确定性结果 | Judge 结果 | 固定动作 |
| --- | --- | --- |
| 已达自动条件 A | 高置信 A | 自动通过，记录 `strong_consensus` |
| 已达自动条件 A | 低置信 A | 保留自动结果，记录 `weak_consensus` |
| A | B | 强制人工审核，不自动切换到 B |
| 低置信或未决 | 高置信 A | 为 A 增加最多 0.04 的有界优先级，仍人工审核 |
| 低置信或未决 | 弃权 | 保持人工审核 |
| 已达自动条件 A | 弃权 | 保留确定性结果，记录 `deterministic_only` |

有界优先级增量为 `0.04 × Judge confidence`。该增量可改变组内候选的审核排序，
但不会把已标记的人工审核改为自动通过。

## 审计与报告

顶层状态保存 `recommendation_judgments`，其中同时记录 Judge 介入前的
确定性候选、Judge 候选、两路置信度、融合类型、是否要求审核以及实际加分。
人工审核载荷和最终 Markdown 报告都展示这份记录，便于重放和解释。

## 安全边界

- Judge 不配置工具，不写入原文件，不修改版本图。
- 哈希重复、确定性证据排除和已有强制审核均不能被 Judge 覆盖。
- Judge 输出是结构化意见，不是执行命令。
- 模型故障、越界候选、错误引用或弃权协议冲突都会进入安全回退。
