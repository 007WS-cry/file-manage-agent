# 1.0.4 发布说明

## Title

**File Manage Agent 1.0.4 — 受控业务证据理解与确定性决策**

## Description

1.0.4 将 Evidence Subagent 从附件元数据匹配器升级为业务证据理解器：模型可以从
有界邮件或日志摘录中识别批准、拒绝、作废、仅供参考、继续修改和最终版本等语义，
但目标文件、时间和引用必须通过白名单校验。LLM 只提出候选，固定规则负责加权、
排除和人工审核，因此模型能够产生实质性业务输入，却不能直接控制系统。

## 新增能力

- 本地发送日志与邮件 MCP 记录新增可选 `evidence_text` 受控摘录；
- 单条摘录最多 1000 字符，协议拒绝额外正文、超长文本和未知字段；
- Evidence Subagent 输出新增 `business_evidence` 结构化候选；
- 支持八种封闭证据类型以及固定的类型—状态映射；
- 模型输出必须复用输入中的目标文件、有效时间和句子级证据引用；
- 顶层状态新增 `BusinessEvidenceRecord`，规则动作和分值只由代码生成；
- Recommendation 新增业务证据加权、排除和强制人工审核节点；
- 人工审核载荷和 Markdown 报告新增业务语义证据及规则结果。

## 确定性安全规则

- `approved` 的最大权重为 `+0.18`，并避免与同一客户确认事实重复计分；
- `superseded` 与 `for_reference_only` 目标不参与主版本竞争；
- `requires_revision` 强制人工审核；
- `rejected` 排除目标并强制人工审核；
- `final_version` 使用固定 `+0.14` 最大权重；
- `sent_but_unconfirmed` 和 `ambiguous` 不触发额外自动动作；
- 正负证据冲突或全部候选被排除时强制人工处理；
- Evidence Subagent 不能输出评分、候选排除、审核动作或文件操作指令。

## 兼容性

- 发送日志和模拟邮件数据继续使用 `schema_version: "1.0"`，`evidence_text` 为可选字段；
- 旧日志未提供摘录时仍执行原有确定性匹配和推荐流程；
- Mock LLM 和模型失败回退默认返回空 `business_evidence`；
- 未新增 Alembic 迁移，SQLite、PostgreSQL 和 checkpoint 拓扑不变；
- 原始文件继续只读，完整版本链继续全部保留。

## 部署元数据

- Python 包、`app.__version__`、Dockerfile 和 Compose 镜像标签统一为 `1.0.4`；
- Docker OCI 描述已更新为业务证据语义理解能力；
- `.gitignore` 与 `.dockerignore` 新增业务证据与摘录快照规则；
- README、技术索引、状态契约、Skill 和示例数据同步更新。

## 验证命令

```powershell
python -m ruff check app tests alembic scripts
python -m pytest
docker compose config --quiet
docker build --build-arg APP_VERSION=1.0.4 -t file-manage-agent:1.0.4 .
```

PostgreSQL Docker 集成测试仍需通过 `FILE_GOVERNANCE_RUN_POSTGRESQL_TESTS=1` 显式启用。
