# 1.0.2 发布说明

## 发布摘要

1.0.2 在既有只读版本治理、固定 Agent Team 和人工恢复能力上增加语义级变更分析。
确定性比较负责生成有界差异证据，Version Subagent 负责输出受控业务分类，最终人工
审核优先级和强制审核动作仍由规则引擎决定。

## 新增能力

- 从关键字段、标准化段落和文档结构生成带稳定 `diff:` 引用的有界证据；
- 支持金额、日期或期限、责任主体、交付范围、付款条件、违约责任、审批状态、联系人
  或收件人、纯措辞、格式和无实质变化共 11 类业务语义分类；
- `SemanticChangeAnalysis` 输出包含重要性、旧值、新值、业务影响、证据引用和置信度；
- 对模型证据引用、comparison 归属和 old/new 值执行严格复核；
- 由确定性规则计算 `not_assessed`、`low`、`medium`、`high` 审核优先级；
- 高优先级变更强制进入人工审核，待审核版本组按优先级稳定排序；
- 治理报告和人工审核载荷展示语义分类、业务影响、置信度及受控证据引用。

## 安全与回退

- Version Subagent 不接收完整正文，只接收总长度受限的差异片段；
- LLM 不能输出系统动作或审核优先级；
- 伪造 `diff:` 引用、虚构 old/new 值、非法 Schema 或方向未知时断言新旧值均触发
  确定性回退；
- 模型关闭、超时或回退时保留原有摘要，语义审核状态为 `not_assessed`；
- 语义分类不会修改版本方向、相似度、版本边、证据评分或业务文件。

## 状态与兼容性

- `DiffRecord` 新增 `change_evidence`、`semantic_changes`、`review_priority` 和
  `review_reasons`；
- `VersionSubagentInput` 新增 `version_order_known` 和 `change_evidence`；
- `VersionSubagentOutput` 新增 `semantic_changes`；
- 没有新增 Alembic 迁移，SQLite/PostgreSQL 应用数据库和 SQLite checkpoint 拓扑不变；
- `use_llm_summary=false` 时保持原有确定性版本分析路径。

## 部署元数据

- Python 包、Dockerfile、Compose 镜像标签和 README 当前版本统一为 `1.0.2`；
- `.gitignore` 与 `.dockerignore` 增加语义证据、分类和审核优先级快照；
- 容器仍使用非 root 用户，输入目录仍只读挂载。

## 验证命令

```powershell
python -m ruff check app tests alembic scripts
python -m pytest
docker compose config
docker build --build-arg APP_VERSION=1.0.2 -t file-manage-agent:1.0.2 .
```

PostgreSQL Docker 集成测试仍需通过 `FILE_GOVERNANCE_RUN_POSTGRESQL_TESTS=1` 显式启用。
