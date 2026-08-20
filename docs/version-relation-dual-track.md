# 双轨版本关系判定

## 目标

1.0.3 让 Version Subagent 不再只总结确定性比较结果，而是可以基于受控证据提出版本
关系候选。模型候选不会直接修改版本图，必须先与确定性结果及硬约束融合。

```text
确定性关系判断 ─┐
                ├─ 约束融合 ─ 版本图可消费关系
LLM 语义候选 ───┘           └─ 冲突或越权时人工审核
```

## 关系类型

| 类型 | 含义 | 是否带方向 |
| --- | --- | --- |
| `direct_revision` | 后一个文件是前一个文件的直接修订 | 是 |
| `parallel_branch` | 两个文件来自共同基础版本，但修改方向不同 | 否 |
| `derived_export` | PDF 等文件由较早的可编辑源版本导出 | 是 |
| `semantic_duplicate` | 表达和格式可能不同，但业务内容等价 | 否 |
| `unrelated` | 文件不应属于同一版本关系 | 否，不建边 |
| `uncertain` | 当前证据不足或双轨结果无法安全融合 | 否 |

## 输入证据

`VersionSubagentInput` 在原有相似度、关键修改和排序信号之外增加：

- `deterministic_relation` 与 `deterministic_relation_confidence`；
- `relation_constraints`，保存哈希、父子、导出、语义重复、并行分支和无关文件约束；
- `relation_evidence`，保存当前文件对及同组既有比较的有界证据。

关系证据使用稳定 `diff:` 引用，包括相似度、排序、文件格式、变更片段和同组比较
上下文。每项证据有数量、单项字符数和总字符数上限，不携带完整文档正文。

模型输出示例：

```json
{
  "relation": "parallel_branch",
  "reason": "两份文件均与同一基础版本相关，但修改内容不同",
  "evidence_refs": [
    "diff:proposal-a-b:context",
    "diff:proposal-a-c:context"
  ],
  "confidence": 0.87
}
```

非 `uncertain` 候选必须至少引用一项输入白名单中的关系证据。伪造引用、重复引用、
非法关系类型或 Schema 外字段会拒绝整个模型输出并进入确定性回退。

## 约束融合

融合结果通过 `relation_resolution` 说明来源：

| 决议 | 处理方式 |
| --- | --- |
| `deterministic_only` | 模型未提出可采纳候选，保留确定性结果 |
| `consensus` | 两条轨道一致，提高最终关系置信度 |
| `llm_supported` | 确定性结果不明确，约束内采纳高置信 LLM 候选 |
| `conflict_review` | 两条轨道冲突，最终关系设为 `uncertain` 并强制人工审核 |
| `constrained_rejection` | 模型候选违反硬约束，不写入版本图并强制人工审核 |

固定约束包括：

- 原始 SHA-256 完全一致时，重复事实不可由 LLM 覆盖；
- 时间、显式版本号、文件名、内容和结构不足以支持父子关系时，模型不能建立
  `direct_revision`；
- 文件格式、方向和内容不足以支持可编辑源到 PDF 时，模型不能建立
  `derived_export`；
- 语义重复、并行分支和无关文件候选也必须满足各自的确定性支持条件；
- 模型置信度低于采纳阈值时，不会用候选消解确定性不确定状态。

## 版本图与人工审核

版本图只消费 `resolved_relation`：

- `direct_revision` 转换为 `derived_from` 有向边；
- `derived_export` 保留为导出有向边；
- `parallel_branch` 和 `semantic_duplicate` 形成非拓扑关系边；
- `unrelated` 不建立版本边；
- 冲突、约束拒绝和无法判断形成 `uncertain` 边。

关系冲突优先于普通语义审核优先级进入待审核队列。人工审核载荷和 Markdown 报告会
展示确定性关系、LLM 候选、最终关系、融合方式、置信度和审核理由，但不会包含正文。

## 安全边界

- Version Subagent 只提出候选，不返回删除、移动、批准或建边动作；
- 融合规则和审核升级均为确定性程序；
- 模型关闭、失败、超时或回退时，版本关系仍由原确定性路径生成；
- 所有推荐继续保留完整版本组，任何关系结果都不构成文件修改或清理授权。
