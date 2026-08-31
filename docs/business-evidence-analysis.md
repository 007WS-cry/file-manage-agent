# 业务证据语义理解

## 目标

1.0.4 让 Evidence Subagent 从用户明确提供的邮件、发送记录或其他受控文本摘录中
理解业务含义，而不再只依赖附件名、哈希、时间和客户确认布尔值。LLM 只提出带
证据引用的语义候选；候选加权、排除和人工审核仍由确定性规则执行。

## 数据流

```text
本地发送日志或邮件 MCP
        ↓ 固定字段、长度和来源校验
确定性附件匹配（SHA-256 / normalized digest / 文件名）
        ↓ 只保留已匹配文件的有界 evidence_text
Evidence Subagent 业务语义分类
        ↓ Schema、目标文件、时间和引用白名单校验
BusinessEvidenceRecord
        ↓ 固定规则
候选加权 / 退出竞争 / 强制人工审核
```

完整邮件正文不会进入 Evidence Subagent。单条 `evidence_text` 最多 1000 字符，
单次分派最多 20 条、合计最多 8000 字符；未匹配到具体文件的摘录不会提交给模型。

## 输入协议

本地发送日志和邮件 MCP 记录继续使用 `schema_version: "1.0"`，新增可选字段：

```json
{
  "id": "message-982",
  "attachment_name": "contract-v4.docx",
  "attachment_sha256": null,
  "normalized_digest": null,
  "sent_at": "2026-07-12T10:30:00+08:00",
  "recipient_label": "客户甲",
  "customer_confirmed": false,
  "evidence_ref": "email-mcp://message-982:sentence-3",
  "evidence_text": "报价已确认，请以附件中的第二版为准。"
}
```

工具拒绝协议外字段、空摘录、超长摘录、无时区时间和非法引用。邮件 MCP Tool 只返回
结构化元数据及有界摘录，不提供发送、修改、下载、移动或删除邮件的能力。

## LLM 输出

`business_evidence` 使用以下封闭类型：

- `approved`
- `rejected`
- `superseded`
- `for_reference_only`
- `requires_revision`
- `final_version`
- `sent_but_unconfirmed`
- `ambiguous`

示例：

```json
{
  "evidence_type": "approved",
  "target_file_id": "contract-v4",
  "status": "approved",
  "actor_role": "customer",
  "effective_time": "2026-07-12T10:30:00+08:00",
  "reason": "摘录明确表达报价已确认。",
  "evidence_refs": [
    "email-mcp://message-982:sentence-3"
  ],
  "confidence": 0.96
}
```

模型不能输出 `rule_action` 或 `score_adjustment`。系统会校验：

- `status` 必须与 `evidence_type` 固定对应；
- `target_file_id` 必须与每条所引摘录的确定性匹配结果一致；
- `effective_time` 只能复用所引摘录已有时间；
- `evidence_refs` 必须同时属于 `evidence_snippets` 和 `artifact_refs` 白名单；
- 重复候选、虚构引用、虚构文件或虚构时间会使本次模型输出进入确定性回退。

## 确定性规则

| 业务证据 | 固定动作 |
| --- | --- |
| `approved` | 最大候选分增量 `+0.18`，实际值乘模型置信度；同一引用已由客户确认布尔事实加权时不重复计分 |
| `final_version` | 最大候选分增量 `+0.14`，实际值乘模型置信度 |
| `superseded` | 目标文件退出主版本竞争 |
| `for_reference_only` | 目标文件退出主版本竞争 |
| `rejected` | 目标文件退出主版本竞争，并强制人工审核 |
| `requires_revision` | 不直接改分，强制人工审核 |
| `sent_but_unconfirmed` | 不追加语义分值，继续使用既有发送记录规则 |
| `ambiguous` | 不执行自动动作 |

同一文件同时出现肯定证据与拒绝、作废、仅供参考或继续修改证据时，系统强制人工
审核。全部候选都被排除时不会自动选择文件。

## 状态与报告

顶层状态新增 `business_evidence`。其中只保存语义类型、目标文件、角色、时间、简短
理由、证据引用、置信度以及代码生成的规则动作，不重复保存 `evidence_text`。人工审核
载荷和 Markdown 报告会显示这些记录及规则结果，便于复核 LLM 理解与程序执行之间的
边界。

本能力不删除、移动、重命名或覆盖任何原始业务文件，也不改变完整版本链保留策略。
