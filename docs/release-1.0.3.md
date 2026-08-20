# 1.0.3 发布说明

## Title

**File Manage Agent 1.0.3 — 受约束的双轨版本关系判定**

## Description

1.0.3 在确定性版本关系算法之外加入 Version Subagent 的语义关系候选，通过证据
白名单和硬约束完成融合：一致时提高置信度，不确定时可受限采纳，冲突或越权时转入
人工审核。LLM 能影响版本图，但不能无约束修改版本事实。

## 新增能力

- 支持 `direct_revision`、`parallel_branch`、`derived_export`、
  `semantic_duplicate`、`unrelated` 和 `uncertain` 六种封闭关系类型；
- Version Subagent 输入新增确定性关系、关系置信度、硬约束和有界关系证据；
- Version Subagent 输出新增可选 `relation_assessment`，包含理由、证据引用和置信度；
- 关系证据可以引用当前文件对和同组既有比较，但必须使用输入白名单内的 `diff:` 引用；
- 双轨融合支持确定性保留、共识增强、约束内采纳、冲突审核和约束拒绝五种决议；
- 版本图只消费融合后的关系，无关文件不建边，冲突和越权候选不建立父子边；
- 人工审核载荷和 Markdown 报告展示双轨关系的完整解释。

## 确定性安全规则

- 原始 SHA-256 完全一致时固定为重复事实，LLM 不能覆盖；
- 时间、版本名、内容和结构不支持父子关系时，LLM 不能建立直接修订边；
- 格式、方向和内容不支持导出关系时，LLM 不能建立导出边；
- LLM 与确定性算法一致时提高关系置信度；
- 两条轨道冲突或候选违反硬约束时，最终关系设为不确定并强制人工审核；
- Version Subagent 仍不能输出审核优先级、系统动作或文件修改指令。

## 状态与兼容性

- `DiffRecord` 新增确定性关系、约束、LLM 候选、融合结果和关系审核字段；
- `VersionSubagentInput` 新增关系判断所需的最小结构化输入；
- `VersionSubagentOutput` 新增可选关系候选，旧 Mock 或回退结果缺少该字段时默认为
  `None`；
- 未新增 Alembic 迁移，SQLite/PostgreSQL 应用数据库及 SQLite checkpoint 拓扑不变；
- `use_llm_summary=false` 时完全保留确定性关系路径。

## 部署元数据

- Python 包、Dockerfile、Compose 镜像标签和 README 当前版本统一为 `1.0.3`；
- Docker OCI 描述已更新为受约束双轨版本关系能力；
- `.gitignore` 与 `.dockerignore` 新增关系证据、候选和融合快照规则；
- 容器继续使用非 root 用户，输入目录继续只读挂载。

## 验证命令

```powershell
python -m ruff check app tests alembic scripts
python -m pytest
docker compose config --quiet
docker build --build-arg APP_VERSION=1.0.3 -t file-manage-agent:1.0.3 .
```

PostgreSQL Docker 集成测试仍需通过 `FILE_GOVERNANCE_RUN_POSTGRESQL_TESTS=1` 显式启用。
