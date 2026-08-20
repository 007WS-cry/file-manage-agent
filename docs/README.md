# 技术文档

根目录的 [`README.md`](../README.md) 面向 File Manage Agent 的使用者，只保留产品定位、
快速开始和常用入口。本目录集中维护部署、架构、状态协议、开发测试和版本演进资料。

## 使用与运维

| 文档 | 内容 |
| --- | --- |
| [部署与运行](operations.md) | 本地服务、HTTP API、Worker、Scheduler、Docker 和 PostgreSQL |
| [1.0.0 演示手册](demo-1.0.0.md) | 演示数据、前后台链路、备份恢复和验收 |
| [后台恢复与人工确认](resume-and-interview.md) | 两类 interrupt、幂等恢复和 API 协议 |
| [完整技术参考](technical-reference-1.0.0.md) | 1.0.0 全量配置、命令、能力清单和历史技术说明 |

`technical-reference-1.0.0.md` 由标准化前的完整 README 迁入，用于保留全部技术细节。
日常使用优先阅读根 README 和上表中的专题文档。

## 架构与开发

| 文档 | 内容 |
| --- | --- |
| [1.0.0 架构说明](architecture-1.0.0.md) | 组件关系、主图、后台运行、Cron 和安全边界 |
| [1.0.0 状态契约](state-contracts-1.0.0.md) | LangGraph、后台任务、中断和持久化状态协议 |
| [语义级变更分析](semantic-change-analysis.md) | 有界差异证据、Version Subagent 分类和确定性审核规则 |
| [双轨版本关系判定](version-relation-dual-track.md) | 确定性关系、LLM 候选、硬约束融合和人工审核规则 |
| [开发与测试](development.md) | 源码目录、开发环境、检查命令和文档维护约定 |

## 发布说明

| 版本 | 文档 |
| --- | --- |
| 1.0.3 | [双轨版本关系发布说明](release-1.0.3.md) · [双轨版本关系专题](version-relation-dual-track.md) |
| 1.0.2 | [语义级变更分析发布说明](release-1.0.2.md) · [语义级变更分析专题](semantic-change-analysis.md) |
| 1.0.0 | [稳定版发布说明](release-1.0.0.md) |
| 0.8.x | [后台运行时发布](release-0.8.0-background-runtime.md) · [主图收口与后台恢复](version-0.8.1-main-closure-runtime-resume.md) |
| 0.7.x | [Error Recovery 发布](release-0.7.0-error-recovery.md) · [后台运行基础设施](version-0.7.1-background-runtime.md) · [Scheduler 与 Worktree](version-0.7.2-scheduler-worktree.md) |
| 0.6.x | [Skills、Memory、Context Compact 发布](release-0.6.0-skills-memory-context.md) · [恢复状态与策略](version-0.6.1-recovery-state-policy.md) · [恢复持久化](version-0.6.2-recovery-persistence.md) · [恢复子图](version-0.6.3-error-recovery-graph.md) · [既有子图接入](version-0.6.4-existing-subgraph-integration.md) · [恢复确认与 CLI](version-0.6.5-recovery-interrupt-report-cli.md) |
| 0.5.x | [固定 Agent Team 发布](release-0.5.0-agent-team.md) · [多模型适配](version-0.5.2-langchain-multi-model.md) · [Memory](version-0.5.4-memory.md) · [Context Compact](version-0.5.5-context-compact.md) |
| 0.4.x | [Task Orchestration 发布](release-0.4.0-task-orchestration.md) · [LLM 基础设施](version-0.4.1-llm-foundation.md) · [固定 Subagent](version-0.4.2-fixed-subagents.md) · [团队分派](version-0.4.3-team-dispatch.md) · [业务阶段接入](version-0.4.4-business-stage-integration.md) |
| 0.3.x 及更早 | [Prompt 与 Hooks](version-0.3-prompt-hooks.md) · [Task System](version-0.3.1-task-system.md) · [Team Orchestration](version-0.3.2-team-orchestration.md) · [Task 进度](version-0.3.3-task-progress.md) · [Evidence 接入](version-0.4-evidence.md) |

## 文档维护约定

- 根 README 只描述用户需要理解的目标、安装、快速开始、主要入口和安全提示。
- 实现细节、内部协议、部署拓扑、开发测试和版本历史统一写入 `docs/`。
- 示例请求统一放入 `examples/`，文档使用相对链接引用，避免复制出多个版本。
- 版本专题文档保留版本号；跨版本的通用文档使用稳定文件名。
