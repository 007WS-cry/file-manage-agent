# 安全说明

## 数据处理原则

本项目的业务输入必须视为只读数据。任何调用方都不得把 `artifact_root` 或
报告目录设置到业务输入根目录内部，也不得依据自动推荐直接删除、覆盖、移动或
重命名原始文件。

文档正文只在本地解析。显式启用真实 LLM Provider 时，仅允许发送有界内容预览、
确定性差异摘要、证据摘要和受控引用；不得发送完整正文。调用方必须在启用前完成
数据授权，并核对 Provider 的区域、保留和日志策略。默认配置使用离线 Mock Provider。

人工审核使用 LangGraph `interrupt()`。暂停载荷只应包含文件 ID、文件名、评分和
推荐理由，不得加入完整正文。CLI 默认使用本地 SQLite Checkpointer 支持跨进程
恢复；数据库必须位于输入目录之外，并应配置访问控制、磁盘加密、保留期限和
安全删除策略。测试或单进程嵌入场景可以显式使用 `InMemorySaver`。

0.5.1 新增的应用数据库与 LangGraph Checkpointer 是两套独立存储。默认应用
数据库位于 `.artifacts/database/file-governance-app.sqlite3`，checkpoint 位于
`.artifacts/checkpoints/file-governance.sqlite3`，不得配置为同一个文件。应用
数据库只允许保存脱敏运行摘要、结构化 Memory、上下文摘要、工具审计和人工选择；
不得保存 API Key、完整 Prompt、完整工具输出或文档正文。Repository 不自行提交
事务，Session 必须在单个请求或图节点内创建、提交或回滚并关闭，不得跨线程共享。

0.5.4 接入的短期 Memory 只保留在当前 LangGraph 状态；长期 Memory 只允许固定
模板摘要以及版本组 ID、文件 ID、证据记录 ID、匹配类型和计数等字段白名单。
工作空间命名空间保存目录或调用方种子的 SHA-256，而不是原始路径。人工审核的
`review_note`、版本组标签、匹配信号、收件人和原始证据引用不得进入长期 Memory。
每条记录必须在创建和数据库写入前分别执行安全校验；数据库不可用或校验失败时
采用 fail-open 治理、fail-closed 持久化，即继续生成报告但拒绝不安全写入。

0.5.5 接入的 Context Compact 只在 Inventory 和 Evidence 已完成后的固定安全点
运行。压缩计划不得接收或改写 `version_edges`、`branches`、`decisions` 和
`human_review`；包含文档详情的大型临时载荷必须使用 `UntrackedValue`，不得进入
checkpoint。文档详情只能原子写入受控 `intermediate` 产物，System Prompt 正文
不得写入压缩产物；`context_summaries` 表只允许保存固定模板摘要、阶段、序号、
压缩后的 Token 估算和产物引用。数据库或产物写入失败时应记录非致命错误并继续
治理流程，不得为了持久化而恢复或复制已经释放的敏感上下文。

0.6.0 接通的应用数据库生命周期默认关闭。启用后，
`governance_runs` 只保存运行状态、真实 Checkpointer `thread_id` 和脱敏请求计数；
`human_reviews` 只保存版本组与选中文件 ID，不保存 `review_note` 自由文本。
`tool_call_audits` 不保存 Tool 参数或完整输出：目录扫描只保存计数，文档解析只
保存固定摘要、受控 `content_ref` 和字节数。产物引用必须位于配置的
`artifact_root` 内，符号链接、越界路径和缺失文件不得作为成功输出写入数据库。
数据库写入失败应保留治理报告和确定性结论，并以非致命错误或 Hook 降级事件收口。

0.6.1 新增的恢复策略只能声明固定错误类别、有限重试数值、确定性退避参数、
安全降级白名单和是否允许人工处理；不得声明动态 Python 函数、任意图节点、
Shell 命令、外部 URL 或文件写入动作。`ErrorRecord` 只允许保存脱敏异常类型、
简短消息、内部 ID 和恢复元数据，不得保存堆栈、工具参数、文档正文或凭据。
`RecoveryHumanState.note` 不得进入长期 Memory；`NodeExecutionRecord` 只保存
输入摘要及受控结果引用，不保存可直接重放的不受信任命令。本批尚未接入自动恢复，
后续恢复图必须继续遵守这些状态边界。

0.6.2 新增的 `error_recovery_records` 和 `node_execution_records` 只能保存内部 ID、
输入摘要、受控产物引用、有限计数和脱敏状态，不得保存节点完整输入、文档正文、
堆栈或凭据。Repository 只能在单个图节点内部使用
`open_application_session()` 创建的短事务，不得把 Session、Repository 或数据库
连接写入 LangGraph 状态、checkpoint 或 `interrupt()` 载荷。旧 checkpoint 不得
回退已持久化的 `retry_count`、`attempt_count`，输入摘要不一致时不得复用结果。

0.6.3 的节点 `error_handler` 只允许把未捕获异常转换为脱敏错误记录并路由到
Error Recovery，不得在异常处理器内选择降级或接受动态目标节点。恢复重试和正常
续跑分别受固定节点白名单约束；状态中的 `resume_node` 与 `resume_after_node`
不能覆盖该白名单。节点结果只有在幂等键、输入摘要、受控产物路径和结果 SHA-256
全部校验通过时才能复用。恢复型 `interrupt()` 只公开错误 ID、类别、简短说明、
允许动作和是否需要替换路径，不得包含堆栈、完整节点输入、正文或数据库对象。
确定性退避秒数只写入状态供调度层观察，不在数据库事务内休眠，也不允许 Session
跨子图调用、条件边或中断存活。

0.6.4 要求业务节点只登记脱敏错误事实，不得自行扩展恢复动作、动态节点目标或
任意降级名称。统一错误上下文只保存内部运行、Task、逻辑执行 ID 和策略快照，
不得加入文档正文、Prompt、模型响应或凭据。同一节点重放只能继承有限重试进度，
不能降低 `retry_count`；成功执行产物仍包含当前未解决错误时禁止结果复用。
coordinator、no-memory、keep-context、default-skill 和 partial-result 等既有
确定性回退必须由 Recovery 生成恢复终态和降级审计。顶层路由只处理当前阶段的
未解决错误，`recovered` 与 `fallback_applied` 历史记录仅供审计，不得再次触发
恢复。写入错误恢复表前必须存在对应节点执行记录，Session 仍不得跨节点或
`interrupt()` 存活。

标准化内容和中间产物统一由 `app/storage/artifacts.py` 写入独立目录。产物 ID
不允许包含路径分隔符，写入使用临时文件和原子替换；调用方仍应限制产物目录的
操作系统权限，并避免将包含业务正文的 JSON 提交到源码仓库。

## Prompt 与生命周期 Hook

CLI 只从请求信封的独立 `prompt`、`hooks` 对象接收生命周期配置，不会把这些字段
混入业务 `RequestState`。System Prompt 只允许读取调用方显式指定的本地 `.md` 或
`.txt` 普通文件，并限制路径范围、符号链接、UTF-8 编码和文件大小；不会执行
Prompt 中的命令、模板或代码，也不会访问网络。

实际 Prompt 内容和 SHA-256 会进入 LangGraph 状态，并可能随 Checkpointer 持久化，
因此 Prompt 和动态规则不得包含密钥、客户正文或其他不应写入 checkpoint 的敏感
信息。Hook 只能从静态白名单按名称解析，不能通过配置动态导入 Python 模块；
HookEvent 只记录简短结果，不应保存业务正文、工具输出或凭据。生产配置应审慎为
安全校验使用 `block`，仅对允许降级的审计或清理操作使用 `ignore`。

生命周期 Hook 只能完整替换 `run` 或 `report`。`llm`、`team`、`skill_registry`、
`tasks`、`todos`、`team_messages` 和 `llm_calls` 属于固定 Agent 受保护状态，Hook
不得修改。

## Task 与 Todo 状态

`TaskItem` 是治理执行状态的唯一事实来源，`TodoItem` 只能由完整 Task DAG 重新
投影，不能接受用户提交的 Todo 状态覆盖。Task 的 `input_refs`、`output_refs` 和
`error` 只允许保存状态键、产物引用及简短错误，不得保存文档正文、密钥、客户
信息或完整工具输出。

Task ID 由 `run_id` 和固定 Task 类型确定性生成。恢复 checkpoint 时必须验证重复
ID、未知依赖和循环依赖，不能为了继续执行而静默覆盖或删除冲突 Task。本地生成的
Task DAG、Todo 和进度调试快照默认被 `.gitignore` 与 `.dockerignore` 排除。
`execution_id` 在同一逻辑 Task 的有限重试间保持不变，外部状态更新必须同时匹配
该 ID 和预期 `attempt_count`，不能借重试注入跨 Task 更新。

Team Orchestration 子图不接收完整正文、工作空间写操作或业务工具，只处理运行标识、
Task、Todo、最小 Subagent 输入和结构化错误。单次 `task_update`、`dispatch_request`
和 `dispatch_result` 是子图私有字段，必须在使用后清空或由转换白名单阻止其写回顶层。
三个固定 Subagent 可以通过统一 Client 调用 Mock 或显式启用的真实 Provider，但不能
主动打开 `artifact_refs`、调用 MCP、本地文件工具或继续递归委派。

## Task 级 Skills

`registry.yaml` 只能声明固定字段、Task 类型和 Agent 角色，Skill ID 必须唯一。
其中的文档路径必须是注册表目录内的相对路径，并最终指向命名为 `SKILL.md` 的
普通 UTF-8 文件；注册表和文档都受字节上限约束。Skill 文本是模型指令而不是
工具代码，加载器不会执行其中的命令、脚本或模板，也不会因 Skill 内容访问网络。

顶层图只读取注册表元数据。Team Orchestration 必须根据真实 Task、固定角色和
Agent 注册表选择最小 Skill 集合，只有选择项可以在当前分派期间读取正文并绑定。
Subagent 必须校验 Skill ID 和正文 SHA-256，结束后无论使用模型还是协调者回退，
都要把 Skill 恢复为 `available`，并清空正文、摘要、Task 绑定和成员 `skill_ids`。
本地或私有 Skill 变体不得进入 Git 或 Docker 构建上下文。

LLM Profile 只允许保存 API Key、可选 Base URL 和 Provider 专有参数的环境变量
名称。实际值由真实 Provider 在调用时读取，不得进入请求 JSON、YAML、LangGraph
状态、checkpoint、错误、报告或调用审计。专有参数 JSON 不得覆盖模型、凭据、
端点、超时、重试或 Token 预算。任务路由只能引用已声明 Profile；关闭真实模型时必须强制
使用 `disabled-mock`。模型失败、超时、非法 Pydantic 输出和越权引用必须回退到
确定性结果；Version Subagent 只有在审计状态为成功且未回退时才允许替换解释摘要，
不能修改版本方向或推荐事实。

LangChain 的各个可选 Provider 包只在显式启用对应真实 Profile 时延迟加载。项目不主动开启
LangSmith tracing；生产环境不得无意设置 `LANGSMITH_TRACING=true`，否则有界 Prompt
和响应元数据可能发送到外部遥测服务。确需 tracing 时必须先完成数据授权、访问控制、
保留期限和脱敏审查。

## CLI 进度输出

CLI 不得直接序列化 `FileGovernanceState`。`run` 和 `resume` 的标准输出只允许包含
运行 ID、状态、短摘要、报告路径、Todo 白名单字段、七种 Task 状态数量和经过约束
的 interrupt 载荷。Todo 只公开 ID、标题、状态、关联 Task ID 和顺序；Task 本身的
输入输出引用、错误详情、时间字段和角色信息均不进入 CLI JSON。

完整 `documents`、文件事实、差异、版本关系、证据记录、Prompt、Skill 指令、
HookEvent、报告 Markdown 和 checkpoint 内容不得为了展示进度而加入标准输出。
即使以后扩展 Todo 或 Task 协议，也必须显式更新 CLI 白名单和泄漏测试，不能使用
`dict(result)` 或其他全状态透传方式。

`report_path`、Todo ID、Task ID 和人工候选文件名仍可能暴露本地目录结构或业务命名，
调用方应把 CLI 输出视为受控运行日志，不应上传到公开 CI 日志或遥测系统。本地生成
的 CLI 输出快照默认由 `.gitignore` 与 `.dockerignore` 排除；公开示例必须使用虚构、
脱敏的 ID、路径和文件名。

## 不受信任文档

解析来自未知来源的 Office/PDF 文件仍然存在第三方解析库漏洞风险。生产环境应：

- 使用非 root 容器用户运行；
- 以只读方式挂载输入目录；
- 对容器设置 CPU、内存、进程数和执行时间上限；
- 定期更新并扫描 `openpyxl`、`python-docx`、`pypdf` 及其依赖；
- 不为解析容器配置业务网络访问能力；
- 对异常或加密文档转入人工处理，不绕过安全上限。

## 报告安全问题

发现安全漏洞时，请不要在公开 Issue 中附加真实业务文档、绝对路径、客户名称、
邮件内容或密钥。请只提供经过脱敏的最小复现样本和依赖版本信息。
