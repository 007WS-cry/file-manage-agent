# 1.0.5 发布说明

## Title

**File Manage Agent 1.0.5 — 受约束推荐裁决与双通道决策**

## Description

1.0.5 新增固定 Recommendation Judge Subagent：它只读取由确定性推荐结果压缩成的
决策包，输出可弃权的结构化第二意见。固定融合规则对比两路结果：一致时记录
共识，冲突时强制人工审核，低置信场景中只允许有上限的候选优先级增量。
模型因此能影响“是否可以自动完成”的风险判断，但不能单方面改写候选分数、
版本事实或解除人工审核。

## 主要变更

- 固定团队扩展为 coordinator 与 Content、Version、Evidence、Recommendation Judge 四个 Subagent；
- 新增压缩候选输入、Pydantic Judge 输出、候选白名单、弃权协议和引用校验；
- 新增 `strong_consensus`、`weak_consensus`、`conflict_review`、
  `judge_priority_signal`、`abstained_review` 和 `deterministic_only` 融合类型；
- 人工审核载荷和最终报告展示 Judge 候选、反证、缺失信息与融合结果；
- 新增 `recommendation-second-opinion` 受控 Skill，并支持独立模型 Profile 路由；
- Python 包、Dockerfile、Compose、README 和文档索引统一为 `1.0.5`。

## 升级说明

1.0.5 没有新增应用数据库表。新的 Judge 记录由 LangGraph 状态和最终报告承载。
使用多模型配置时，可在 `llm.task_profile_ids` 中增加 `recommendation_judge`；
未配置时继承默认 Profile。真实 LLM 关闭或失败时，Judge 安全弃权。

## 验收建议

```bash
python -m pytest -q
python -m ruff check .
docker build --build-arg APP_VERSION=1.0.5 -t file-manage-agent:1.0.5 .
```

详细决策包、融合矩阵和安全边界见
[受约束 Recommendation Judge](recommendation-judge.md)。
