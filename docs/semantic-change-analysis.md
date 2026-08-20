# 1.0.2 语义级变更分析

版本分析在原有文本、结构、相似度和先后关系之上，增加了“有界差异证据 → LLM
业务分类 → 确定性审核规则”链路。该能力由请求中的 `use_llm_summary` 开关控制；真实
LLM 不可用或输出校验失败时，系统保留原有确定性摘要，语义审核优先级为
`not_assessed`。

## 数据流

1. 确定性比较从标准化关键字段和发生变化的段落生成 `change_evidence`。单项、总字符数
   和数量均有上限，不把完整文档正文写入图状态或 Prompt。
2. 每项证据获得稳定 `diff:` 引用，例如
   `diff:<comparison_id>:paragraph-18-to-18`。
3. Version Subagent 只能输出封闭类型的 `semantic_changes`，包含重要性、旧值、新值、
   业务影响、证据引用和置信度。
4. 输出先通过 Pydantic Schema，再检查 `evidence_refs` 是否属于当前输入白名单。
   伪造引用、重复分类、非法类型或方向未知时断言新旧值都会触发确定性回退。
5. 规则引擎根据已校验分类计算 `review_priority`。LLM 不能输出审核动作或优先级。
6. `high` 优先级会强制对应版本组进入人工审核；待审核组按
   `high → medium → low → not_assessed` 排序。

## 受控变更类型

- `amount`
- `date_or_term`
- `responsible_party`
- `delivery_scope`
- `payment_term`
- `breach_liability`
- `approval_status`
- `contact_or_recipient`
- `wording_only`
- `formatting_only`
- `no_material_change`

金额、日期/期限、责任主体、交付范围、付款条件、违约责任和审批状态属于实质类型，
规则引擎至少映射为 `medium`；模型标记为 `high` 时映射为 `high`。纯措辞、格式和无
实质变化始终映射为 `low`，即使模型给出更高重要性也不会直接提升系统优先级。

## 输出示例

```json
{
  "change_type": "payment_term",
  "significance": "high",
  "old_value": "30天",
  "new_value": "60天",
  "business_impact": "回款周期延长",
  "evidence_refs": [
    "diff:contract-001:paragraph-18-to-18"
  ],
  "confidence": 0.94
}
```

治理报告会展示每个文件对的语义审核优先级、业务分类、影响、置信度和差异引用；人工
审核 interrupt 只携带这些有界结构化摘要，不包含完整正文。
