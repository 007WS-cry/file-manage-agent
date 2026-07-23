# File Manage Agent

基于 LangGraph 的只读文件版本治理 Agent。当前版本 `0.5.5` 在安全 Memory
基础上接入 Context Compact：以确定性 Token 估算在 Inventory、Evidence 两个
安全点决定是否压缩，把后续不再参与决策的文档详情移到受控产物，同时保留
`content_ref`、内容哈希及全部版本、证据、推荐和人工审核事实。Content、Version、
Evidence 仍可分别路由 Claude、Gemini、GLM、DeepSeek、Qwen、OpenAI 及其他主流
Provider 和第三方中转站。
当前具备：

- 只读扫描、SHA-256 去重及 XLSX、DOCX、文本型 PDF 内容提取；
- 内容标准化、版本分组、文件对差异、版本边、分叉和版本链；
- 可解释主版本评分和低置信度人工确认；
- Inventory、Version Analysis、Evidence、Recommendation 四个业务子图和顶层治理图；
- Content、Version、Evidence 三个可独立调用的固定 Subagent 子图；
- 标准化内容及中间 JSON 产物的隔离、原子持久化；
- 进程内或 SQLite LangGraph checkpoint；
- 独立 SQLAlchemy 应用数据库、五张基础表和 Repository 数据访问边界；
- 当前运行短期阶段摘要、跨运行长期 Memory 召回和幂等持久化；
- 固定模板、结构化字段白名单、哈希命名空间及数据库原始字节泄漏保护；
- 独立 Context Compact 子图、确定性 Token 估算和两个阶段压缩安全点；
- 未跟踪临时压缩载荷、可重建中间产物和有界 Context Summary；
- 压缩开关对版本边、分叉、推荐和人工选择的严格不变性测试；
- 可升级、回退和重放的 Alembic SQLite 迁移；
- 可跨进程恢复 `interrupt()` 的最小 CLI；
- 成功、部分成功、无数据和失败 Markdown 报告；
- PDF 来源、本地发送记录及推荐候选的状态协议；
- 只读本地发送日志加载工具，以及不执行文件 I/O 的纯证据匹配服务；
- 带 START、END、条件跳过和 LangGraph Send 并行匹配的独立 Evidence 子图；
- 分阶段应用版本链、发送确认、PDF 来源和分叉规则的 Recommendation 子图；
- 证据化 Markdown 报告以及贯穿四个子图的端到端错误路由；
- 受版本控制的文件治理 System Prompt 资源；
- Prompt、Hooks、Hook Event 顶层状态协议和严格的初始配置校验；
- 受路径、符号链接、扩展名、UTF-8 和字节上限约束的 Prompt 加载器；
- 静态 Hook 白名单、顺序 runner、HookEvent 以及 block/ignore 失败聚合；
- 请求预检、运行状态补充、报告检查、最小审计入口和安全清理内置 Hook；
- before_run、System Prompt、after_run 顶层节点、条件路由和生命周期失败报告；
- CLI 请求信封中的可选 `prompt`、`hooks` 配置及旧 checkpoint 关闭兼容模式；
- `TaskItem`、`TodoItem`、`TaskStatusUpdate` 和 Team Orchestration 子图状态协议；
- 按 `task_id` 稳定合并且不重置已有字段的 LangGraph Task reducer；
- 六阶段固定 Task DAG、确定性 ID、拓扑排序、环检测和固定逻辑角色映射；
- 仅以 Task 为事实来源、不会读取旧 Todo 状态的用户进度纯投影；
- 同时支持 Task 状态同步和固定 Subagent 分派的 Team Orchestration 子图；
- 固定团队初始化、动态成员拒绝、实际角色选择和串行分派运行状态；
- 消费后清空且不会泄漏回顶层状态的 `task_update` 与 `dispatch_request`；
- Subagent result/error 消息校验、摘要与引用合并、Task 产物登记和协调者回退；
- 顶层规划节点和六个同步适配节点，按业务实际结果推进 Task 和 Todo；
- 无需审核时正常跳过 Human Review，interrupt 期间保持 running，恢复后完成；
- 业务失败只标记源 Task failed，下游以带原因的 skipped 阻断且报告仍可收口；
- 成功、无数据和业务失败报告统一完成 Report Task，计划前失败安全绕过 Task 同步。
- `run`、`resume` 统一输出 Todo 投影和五种 Task 状态数量；
- CLI 通过字段白名单隔离文档正文、完整报告、Task 引用和大型治理产物。
- 兼容旧单模型写法的 `profiles`、默认 Profile 和三类任务路由状态；
- 只从环境变量读取 API Key、可选 Base URL 和专有参数的 LangChain 多 Provider；
- 可注入、可模拟超时和非法输出的 Mock Provider；
- Content、Version、Evidence 三类独立 Pydantic 输出及产物引用白名单校验；
- 不记录 Prompt、响应正文、API Key 或 Base URL 的 Profile、耗时、Token 和错误审计。
- 固定 Subagent 注册表、最小输入信封、assignment/result/error Team Message；
- 模型失败、超时或引用越权时的确定性摘要回退和 fallback 审计。
- Content、Evidence 阶段后分派，以及 Version 文件对摘要的内部 Team Orchestration 调用；
- 只允许成功 Version Subagent 替换解释摘要的 `DiffRecord` 来源和消息审计字段；
- 包含关键修改摘要、来源和可选受控引用的治理报告。
- 受版本控制的四项 Skill、严格 YAML 注册表和安全的按需 Markdown 加载器；
- Task 类型、固定角色和 Agent 注册表三重约束的最小 Skill 选择；
- Team Orchestration 中显式的选择、加载、绑定、释放节点及失败协调者回退；
- 只向当前 Subagent Prompt 注入已校验 Skill，分派后恢复全部 Skill 为 available；
- 0.2.0、0.3.0、0.4.0 三条参照路径兼容测试和 0.5.0 发布验收矩阵；
- 不持久化 API Key、私有模型 Prompt 或完整解析正文的 SQLite checkpoint 边界。

四个子图既可独立测试，也已按 Inventory、Version Analysis、Evidence、
Recommendation 的顺序接入顶层 File Governance 图。当前版本提供 Python 接口
和 CLI，尚未提供 HTTP API 或后台 Worker。`0.2.3` 已接入 Prompt 和 Hooks 顶层
节点；Prompt 和 Hooks 默认仍完全关闭，并通过 0.2.0 参照图兼容测试确认业务结果
一致。旧版缺少生命周期、Task 或 Todo 字段的 checkpoint 也会自动补齐兼容默认值。
`0.5.0` 由顶层图和 Version Analysis 子图构造最小 `dispatch_request`。模型只解释
既有内容、差异和证据摘要，不改变分组、版本方向、相似度、置信度、证据匹配或推荐
结论。CLI 仍只展示用户所需的最小进度，不输出 LLM 配置、Team Message、Prompt
或调用审计。旧状态缺少 LLM、Team、消息或审计字段时会补齐安全默认值。`0.5.1`
只建立应用数据库、Repository 和迁移；`0.5.2` 在三个既有 Subagent 子图内增加
`resolve_model_profile` 节点；`0.5.3` 在 Prompt 加载与 Task 规划之间加入 Skill
元数据节点，并在 Team Orchestration 分派边界内完成按需加载和释放；`0.5.4`
在 Skill 元数据后召回长期 Memory，在 Evidence 与 Recommendation 子图捕获安全
摘要，并在报告收口后、after_run Hook 前幂等持久化；`0.5.5` 在 Inventory
同步后及 Evidence 解释分派后调用 Context Compact，不改变任何治理决策字段。

## 安全边界

- 原始业务文件始终只读，不删除、移动、重命名或覆盖文件。
- 请求必须显式设置 `workspace.input_readonly = true`。
- 输入目录拒绝符号链接；产物、报告、应用数据库和 checkpoint 不得与输入目录重叠。
- Office 解析器不执行公式、宏、嵌入对象或外部链接。
- PDF 解析器不执行 OCR，也不猜测加密密码。
- 文件大小、ZIP 声明解压大小、Excel 单元格、PDF 页数和提取字符数均有上限。
- 完整正文通过 `content_ref` 指向 `normalized/*.json`，不直接进入图状态。
- Inventory 解析期间的 `current_raw_content` 使用 LangGraph 非跟踪通道，不进入子图
  checkpoint；中断恢复只依赖已持久化的安全业务状态。
- 产物 ID 不允许包含路径分隔符，JSON 使用同目录临时文件和原子替换写入。
- 分叉、链不完整、候选近似并列或低置信度结果必须人工确认。
- `interrupt()` 载荷只包含文件 ID、文件名、评分和理由，不包含完整正文。
- 本地发送日志工具只读取用户明确提供的普通 UTF-8 JSON 文件，拒绝符号链接、
  超限文件和未知协议版本，不打开附件、不访问网络且不执行日志内容。
- 证据匹配遇到多个非重复候选时保留未匹配结果，不依靠排序猜测文件版本。
- Prompt 只读取显式配置的本地 `.md`/`.txt` 文件，拒绝符号链接、非 UTF-8、
  超限内容和相对路径越界，并记录实际内容 SHA-256。
- Hook 只能从静态白名单解析，不能通过请求动态导入模块或执行表达式；状态更新
  仅允许完整替换 `run` 或 `report`，并显式禁止修改 Team、Task、消息和 LLM 审计。
- Task 只保存状态键、产物引用和简短错误，不保存完整文档正文；Todo 只能由 Task
  单向生成，不能作为第二套可写执行状态。
- CLI 只输出 Todo 白名单和 Task 状态计数，不输出 `documents`、完整 Task、报告
  Markdown、Prompt、HookEvent 或 checkpoint 内容。
- LLM Profile 只能保存 API Key 和 Base URL 的环境变量名称；实际值不得进入请求
  JSON、YAML、LangGraph 状态、checkpoint、日志、Team Message 或调用审计。
- 应用数据库与 LangGraph checkpoint 必须使用不同 SQLite 文件；数据库 Session
  只在单次事务中使用，Repository 不得自行 commit 或跨线程共享 Session。
- 长期 Memory 只允许固定模板摘要及版本组、文件、证据记录 ID 和计数白名单；
  文档正文、API Key、完整模型 Prompt、审核自由文本、收件人和原始证据引用不得
  写入应用数据库。
- Context Compact 的大型临时载荷使用 `UntrackedValue`；应用数据库只保存固定
  摘要、Token 估算和受控产物引用，不保存文档详情或已释放的 Prompt 正文。
- LangChain 适配器可从 Profile 指定的 `base_url_env` 读取兼容服务地址，并从
  `options_env` 读取受限 JSON 专有参数；实际值都不会写入治理状态。
- 项目不会主动开启 LangSmith tracing；生产环境不要设置 `LANGSMITH_TRACING=true`，
  除非已明确授权把有界 Prompt 和响应元数据发送到该遥测服务。
- 关闭 `llm.enabled` 时统一 Client 强制使用 Mock Provider，即使其他字段预配置了
  真实 Provider 也不会读取密钥或发起网络调用。
- Subagent 输入拒绝未知正文型字段、超长预览、超长结构化字符串和非受控引用；
  Subagent 不会主动打开 `artifact_refs` 指向的文件。
- Team Message 的发送方和接收方必须属于固定 Team，result/error 必须返回唯一
  coordinator，模型输出引用必须属于当前输入白名单。
- Team Orchestration 拒绝动态成员、角色篡改、协调者 Task 分派和失败 Task 重放；
  分派请求、模型输出和 Team Message 三层引用必须保持一致。
- Skill 注册表只接受固定字段、Task 类型和角色；Skill 路径必须位于
  `resources/skills` 受控目录并命名为 `SKILL.md`，同时受 UTF-8 和字节上限约束。
- 顶层只持久化 Skill 元数据；SKILL.md 正文只在当前 Task 分派期间进入子图状态，
  结束后正文、SHA-256、Task 绑定和 Agent `skill_ids` 均被清空。

## 目录

```text
file-manage-agent/
├── resources/
│   ├── prompts/               # 受版本控制的 System Prompt 资源
│   └── skills/                # Skill 注册表和四项受控 SKILL.md
├── app/
│   ├── state/                 # 状态、reducer、初始状态工厂和子图状态转换
│   ├── llm/                   # Prompt、模型 Profile、统一 Client 和输出校验
│   │   └── providers/         # Provider 抽象、LangChain 多模型、Mock 与旧兼容实现
│   ├── skills/                # Skill 元数据加载、注册表状态操作和 Task 选择
│   ├── agents/                # 固定 Subagent、静态注册表和 Team Protocol
│   ├── hooks/                 # 静态 Hook 注册、顺序执行和内置生命周期 Hook
│   ├── tools/                 # 只读文件扫描、解析和本地发送日志工具
│   ├── services/              # 标准化、版本图、推荐、Memory 和 Context Compact
│   ├── storage/               # 业务产物、checkpoint、ORM 与 Repository
│   ├── utils/                 # 生命周期、Token 估算、Task 编排和状态辅助函数
│   ├── nodes/                 # 仅存放通过 add_node 显式注册的图节点函数
│   ├── graphs/                # 四业务图、Context Compact、团队图与顶层治理图
│   └── entrypoints/           # 最小 CLI
├── alembic/                   # 应用数据库迁移环境和版本脚本
├── alembic.ini                # 默认应用数据库迁移配置
├── configs/default.yaml       # 默认治理、生命周期、存储和数据库参数
├── .env                       # 本地密钥文件，必须被 Git 和 Docker 构建上下文忽略
├── .env.example               # 只声明密钥环境变量名称的安全示例
├── examples/sample_request.json # 默认关闭真实模型的安全请求
├── examples/sample_llm_request.json # 仅引用环境变量名称的真实 Provider 请求
├── examples/sample_delivery_log.json
├── examples/sample_task_progress.json # 0.4.0 CLI 安全进度摘要示例
├── docs/version-0.3-prompt-hooks.md # 0.3.0 生命周期、兼容性与交付说明
├── docs/version-0.3.1-task-system.md # 0.3.1 状态协议与确定性 Task System
├── docs/version-0.3.2-team-orchestration.md # 0.3.2 独立团队编排子图
├── docs/version-0.3.3-task-progress.md # 0.3.3 顶层 Task 进度与人工审核
├── docs/release-0.4.0-task-orchestration.md # 0.4.0 正式发布说明
├── docs/version-0.4.1-llm-foundation.md # 0.4.1 LLM 基础设施说明
├── docs/version-0.4.2-fixed-subagents.md # 0.4.2 固定 Subagent 与 Team Protocol
├── docs/version-0.4.3-team-dispatch.md # 0.4.3 固定团队分派与协调者回退
├── docs/version-0.4.4-business-stage-integration.md # 0.4.4 三业务阶段接入
├── docs/release-0.5.0-agent-team.md # 0.5.0 固定 Agent Team 正式发布说明
├── docs/version-0.5.2-langchain-multi-model.md # 0.5.2 LangChain 多模型适配
├── docs/version-0.5.4-memory.md # 0.5.4 短期与长期 Memory 说明
├── docs/version-0.5.5-context-compact.md # 0.5.5 Context Compact 说明
├── docs/version-0.4-evidence.md # 第四批证据链、评分和错误语义说明
├── tests/
│   ├── unit/                  # 分组、版本图、推荐和 Task System 单元测试
│   └── integration/           # 顶层图、SQLite 恢复和 CLI 集成测试
├── Dockerfile
├── requirements.txt           # 基础可编辑安装入口，依赖版本统一由 pyproject.toml 管理
└── pyproject.toml
```

`app/state/model.py` 仅用于兼容早期单数文件名，新代码应从
`app.state.models` 导入状态。

## 0.2.2 Prompt 与 Hook 基础设施

`0.2.1` 新增了 `PromptState`、`HookConfigState` 和 `HookEvent`，并将 `prompt`、
`hooks` 和 `hook_events` 放入顶层 `FileGovernanceState`。`0.2.2` 在该协议上实现：

- `app.llm.prompt_loader`：只读加载受信任的 `.md`/`.txt` Prompt，拒绝路径越界、
  符号链接、非 UTF-8 内容和超限文件，追加动态规则后记录 SHA-256；
- `app.hooks.registry`：通过不可变静态白名单解析六个内置 Hook，不支持配置驱动的
  Python 模块导入、表达式或 `eval()`；
- `app.hooks.runner`：按配置顺序执行四个阶段，限制 Hook 只能更新 `run`、`report`，
  为每次调用生成 HookEvent，并分别聚合 `block` 与 `ignore` 失败；
- `app.hooks.builtin`：提供请求信封预检、运行状态补充、报告检查、最小工具审计入口
  和不接触原始文件的清理 Hook。

调用 `create_initial_state()` 时不提供 `prompt_config` 和 `hook_config`，即可获得
完全关闭的新功能配置。显式启用时，Prompt 加载器和 Hook runner 已可独立测试或
调用；`0.2.3` 已把它们注册为顶层 LangGraph 生命周期节点。

## 0.2.3 接入顶层 LangGraph

本批将第二批基础设施接入实际治理运行，同时保持生命周期配置和业务请求隔离：

- `execute_before_run_hooks` 在不可关闭的业务请求校验之前执行；阻断失败直接生成
  失败报告，不进入文件扫描；
- `load_system_prompt` 在请求校验后受限读取 Prompt；关闭时直接继续，加载失败时
  记录 `prompt` 类致命错误；
- 成功、无数据和业务失败报告均进入 `execute_after_run_hooks`；`ignore` 失败只保留
  HookEvent，`block` 失败追加生命周期收口失败章节后结束；
- CLI 从请求信封单独解析 `prompt`、`hooks`，再分别传给 `create_initial_state()`，
  两个对象不会进入业务 `RequestState`；
- `initialize_run` 会为 0.2.0/0.2.2 checkpoint 或手工状态补齐完全关闭的生命周期
  字段，保持原有调用兼容性。

## 0.3.0 兼容性与版本交付

正式版本保持四个既有业务子图及其节点不变，并补齐以下交付能力：

- 使用不包含生命周期节点的 0.2.0 参照图，与 Prompt、Hooks 同时关闭的 0.3.0
  路径逐项比较业务事实、版本关系、推荐、错误和报告内容；
- `pyproject.toml` 将受控 Prompt 声明为 setuptools `data-files`，wheel 安装后默认
  路径可回退到 Python 数据前缀下的 `resources/prompts`；
- Dockerfile 显式复制 `resources/`，并在安装前检查受控 Prompt 确实存在；
- `.dockerignore` 和 `.gitignore` 排除本地、私有 Prompt，同时显式保留受控版本；
- 包版本、镜像版本和 Python `__version__` 统一为 `0.3.0`。

完整的配置、路由、失败策略、兼容范围和升级步骤见
[0.3.0 Prompt 与生命周期 Hooks](docs/version-0.3-prompt-hooks.md)。

## 0.3.1 确定性 Task System 第一批

本批先建立 `0.4.0` Team Orchestration 所需的状态与纯服务边界，不修改现有四个
业务子图和顶层执行顺序：

- 固定创建 Inventory、Version Analysis、Evidence、Recommendation、Human Review
  和 Report 六个 Task，Task ID 使用 `run_id:task_type`；
- `create_task_dag()` 支持从不完整 checkpoint 补齐缺失 Task，已有 Task 的状态、
  输出、错误、`created_at` 和 `updated_at` 保持不变；
- `topologically_sort_tasks()` 和 `validate_task_dag()` 拒绝重复 ID、重复依赖、
  未知依赖、自依赖和循环依赖；
- `assign_tasks_to_roles()` 只写固定逻辑角色，不调用 LLM 或 Subagent；
- `update_todos_from_tasks()` 不接收旧 Todo，确保 Todo 只能由 Task 单向生成；
- Task 和 Todo 调试快照默认不进入 Git 或 Docker 构建上下文。

完整字段、幂等边界、Todo 推导规则和测试范围见
[0.3.1 确定性 Task System](docs/version-0.3.1-task-system.md)。

## 0.3.2 独立 Team Orchestration 子图

本批在 0.3.1 纯服务层上增加可独立调用的 LangGraph 子图：

```text
START
  -> create_task_dag
  -> validate_task_dag
  -> [invalid -> END]
  -> assign_tasks_to_roles
  -> update_task_status
  -> update_todos_from_tasks
  -> END
```

- 节点异常统一转换为 `team_orchestration` 阶段的结构化校验错误；
- DAG 创建或校验失败后直接结束，不继续分配角色和投影 Todo；
- `update_task_status()` 校验状态转换、依赖就绪、失败错误和产物引用；
- completed、failed、skipped 终态不能重新打开，相同终态更新保持幂等；
- `task_update` 无论成功或失败都会被消费并清空；
- 顶层转换器只允许 `tasks`、`todos`、`errors` 返回，私有命令不会泄漏；
- 子图节点集合不包含 LLM、Subagent、MCP 或文件工具。

完整的状态边界、转换规则和独立调用说明见
[0.3.2 独立 Team Orchestration 子图](docs/version-0.3.2-team-orchestration.md)。

## 0.3.3 顶层 Task 进度与人工审核

本批把固定 DAG 接入真实治理运行，同时保持四个业务节点文件不变：

- `plan_run_tasks()` 幂等创建六个 Task，并在请求和 Prompt 校验通过后启动 Inventory；
- 四个业务同步节点完成当前 Task，再按实际路由启动后继 Task；非致命 Evidence
  错误保留部分成功语义，不会错误地把 Evidence Task 标为 failed；
- Recommendation 无需人工确认时把 Human Review 正常标记为 skipped；需要确认时，
  Task 在 `interrupt()` 期间保持 running，恢复并应用人工选择后才完成；
- 业务子图致命失败只把对应 Task 标为 failed，下游业务和审核 Task 使用带阻断原因
  的 skipped，Todo 因而显示 blocked 而不会把下游误报为自身失败；
- 无数据路径正常跳过未执行 Task，Report Task 完成后全部 Todo 都进入终态；
- 单一失败报告节点按 DAG 是否可用分流：计划前失败直接执行 after-run hooks，业务
  失败则先完成 Report Task，避免主图出现两个同名失败报告节点；
- 0.3.0 参照图兼容测试继续逐项验证业务事实、治理结论、人工状态和报告正文。

完整的流程、状态转换表和失败收口规则见
[0.3.3 顶层 Task 进度与人工审核](docs/version-0.3.3-task-progress.md)。

## 0.4.0 CLI 展示与版本交付

正式版本在不扩大治理状态暴露面的前提下补齐进度展示：

- `run` 的正常、部分成功、失败、无数据和人工暂停结果，以及 `resume` 的恢复结果，
  均输出相同结构的 `todos` 与 `task_status_counts`；
- Todo 只公开 `id`、`title`、`status`、`related_task_ids`、`order`，并按固定顺序排列；
- Task 只统计 pending、running、completed、failed、skipped 数量，零数量状态仍保留；
- CLI 不输出完整 Task、文档记录、文件事实、证据集合、报告 Markdown、Prompt、
  HookEvent 或 checkpoint；
- 正常完成、无需审核、人工暂停恢复、无数据、业务失败、非致命警告和 checkpoint
  重放七条路径均纳入最终验收矩阵；
- `app/nodes` 严格只保留流程图中通过 `add_node()` 注册的节点函数，生命周期和 Task
  编排辅助逻辑统一迁移到 `app/utils`，并由 AST 结构测试持续约束；
- 包版本、Python `__version__` 和默认 Docker 镜像版本统一为 `0.4.0`。

完整输出协议、安全边界、升级说明和测试映射见
[0.4.0 Task Orchestration 正式发布说明](docs/release-0.4.0-task-orchestration.md)。

## 0.4.1 统一 LLM 基础设施和状态契约

本版本是从 `0.4.0` 向固定 Agent Team 演进的第一批，不修改既有业务图执行顺序：

- 新增统一 `LLMClient`，按配置选择 Mock 或 OpenAI Provider；
- 真实 Provider 只接受 `api_key_env`，调用时才读取环境变量，不保存实际密钥；
- 默认 `llm.enabled=false` 且使用 Mock，升级后不会自动产生外部请求或费用；
- 三个固定 Subagent 的输入、Pydantic 输出和内部图状态全部定义在
  `app/state/models.py`；
- `TeamMessage`、`TeamState`、`LLMConfigState`、`LLMCallRecord` 进入顶层状态协议；
- 调用成功记录 Provider、模型、耗时和 Token；失败与超时只记录脱敏错误摘要；
- Pydantic 输出禁止额外字段，产物引用还必须通过调用方白名单校验；
- 旧 checkpoint 在初始化时补齐安全关闭的 LLM、固定 Team、空消息和空审计列表。

本批不会调用三个 Subagent，也不会修改版本差异摘要。业务图接入将在后续批次完成。
完整配置和安全边界见
[0.4.1 LLM 基础设施](docs/version-0.4.1-llm-foundation.md)。

## 0.4.2 三个固定 Subagent 和 Team Protocol

第二批在第一批状态与 LLM Client 契约上完成三个独立子图：

- Content Subagent 只接收短内容预览、结构摘要、关键字段和产物引用；
- Version Subagent 只接收文件安全标签、相似度、关键修改和排序信号；
- Evidence Subagent 只接收 PDF 来源摘要、发送证据摘要和产物引用；
- 固定注册表只允许 `content`、`version`、`evidence` 三个角色，不支持动态招聘；
- 每个子图先创建合法 assignment 消息，结束时创建 result 或 error 消息；
- Pydantic 输出只包含 `summary` 和 `artifact_refs`，引用必须属于输入白名单；
- 模型失败、超时或输出越权时，按配置进入角色专属确定性回退；
- 流程分支由 `graphs/routers.py` 中被 `add_conditional_edges()` 明确调用的路由实现。

三个 Subagent 当前可以独立调用，但尚未接入既有 Inventory、Version Analysis 和
Evidence 业务图，避免本批改变 `0.4.0` 的确定性治理结论。详细协议、分支语义和
测试矩阵见 [0.4.2 固定 Subagent 与 Team Protocol](docs/version-0.4.2-fixed-subagents.md)。

## 0.4.3 升级 Team Orchestration

第三批把第二批的三个独立 Subagent 接入 Team Orchestration，但暂不修改顶层业务图：

- 编排图幂等初始化并严格校验 coordinator、Content、Version、Evidence 四个成员；
- 同一次调用只能执行 `task_update` 状态同步或 `dispatch_request` 分派；
- 分派前同时校验真实 Task、`assigned_role`、最小输入协议和产物引用；
- 条件路由只允许选择 Content、Version、Evidence 三个固定角色；
- Subagent 返回后再次校验 sender、receiver、消息类型、摘要和引用白名单；
- 合法引用按稳定顺序登记到对应 `TaskItem.output_refs`；
- 模型失败、error 消息或越权引用会转为协调者确定性回退结果；
- `dispatch_request` 和 `dispatch_result` 不写回顶层状态，不实现 Skills 或 Worktree。

现有顶层图尚未创建分派请求，三个业务阶段的接入属于下一批。完整分支、状态边界和
测试矩阵见 [0.4.3 固定团队分派](docs/version-0.4.3-team-dispatch.md)。

## 0.4.4 接入三个业务阶段

第四批把固定 Team 正式接入文件治理运行，同时维持确定性事实的唯一权威来源：

- `sync_inventory_task_status` 后按文档串行分派 Content Subagent；
- Version Analysis 为每个成功比较构造不含正文的输入，并通过 Team Orchestration
  调用 Version Subagent；
- 只有审计状态为 `success` 且未使用 fallback 的 Version 输出可以替换 `summary`；
- 超时、缺少 API Key、Pydantic 非法或协议失败时保留确定性摘要；
- `sync_evidence_task_status` 后按版本组分派 Evidence Subagent，再进入 Recommendation；
- Content 和 Evidence 输出只增加解释消息与受控引用，不改变文档事实或证据评分；
- 生命周期 Hook 显式禁止修改固定 Team、Task、Todo、Team Message 和 LLM 审计；
- 最终报告展示关键修改摘要、摘要来源、Team Message ID 和可选解释引用。

流程、状态边界和回退验收见
[0.4.4 三业务阶段接入](docs/version-0.4.4-business-stage-integration.md)。

## 0.5.0 兼容性、发布和文档

第五批将前四批能力收口为首个固定 Agent Team 正式版本：

- Python 包、项目元数据、Docker 默认镜像版本统一为 `0.5.0`；
- 新增 0.4.0 确定性参照图，证明关闭真实 LLM 时版本关系、证据和推荐结论不变；
- 旧状态缺少 `llm`、`team`、`team_messages`、`llm_calls` 时自动补齐安全默认值；
- SQLite checkpoint 测试同时检查恢复状态和数据库原始字节，禁止 API Key 实际值、
  私有模型 Prompt 及完整正文尾部落盘；
- Inventory 原始解析结果改用非跟踪状态通道，只把标准化产物引用写入持久化状态；
- 默认示例继续关闭真实模型；真实 Provider 使用被忽略的本地 `.env` 提供
  `OPENAI_API_KEY`，远程仓库只提交 `.env.example`；
- 真实 Provider、Mock、超时、缺失密钥、非法 Pydantic 输出、越权引用和三个角色回退
  均映射到自动化测试或显式手工 smoke 步骤。

发布范围、升级方式和完整验收矩阵见
[0.5.0 固定 Agent Team 正式发布说明](docs/release-0.5.0-agent-team.md)。

## 0.5.1 应用数据库骨架

本版本是从 `0.5.0` 向 `0.6.0` 演进的第一批，只建立数据库基础设施，不修改
顶层 LangGraph、CLI 请求协议或现有业务结论：

- `app.storage.database` 校验应用数据库路径、自动创建父目录，并提供短生命周期
  SQLAlchemy Session 事务；
- `app.storage.orm_models` 定义 `governance_runs`、`memory_items`、
  `context_summaries`、`tool_call_audits`、`human_reviews` 五张表；
- `app.storage.repositories` 隔离五张表的数据访问，Repository 只执行查询、写入
  和 flush，由外层 Session 上下文统一提交或回滚；
- `alembic/` 和 `alembic.ini` 管理表结构升级与回退，不使用 ORM
  `create_all()` 代替正式迁移；
- 应用数据库默认使用
  `.artifacts/database/file-governance-app.sqlite3`，LangGraph checkpoint 继续使用
  `.artifacts/checkpoints/file-governance.sqlite3`，两者不得共用文件；
- 当前主图尚未读写这五张表；Memory、Context Compact、工具审计和人工审核将在
  后续批次逐步接线。

首次初始化本地应用数据库：

```bash
python -m alembic upgrade head
```

命令会自动创建 `.artifacts/database/` 父目录和 SQLite 文件。检查当前迁移版本：

```bash
python -m alembic current
```

回退首个迁移并重新升级：

```bash
python -m alembic downgrade base
python -m alembic upgrade head
```

需要改变数据库位置时，可以设置 `FILE_GOVERNANCE_DATABASE_PATH` 环境变量；
Alembic 会把它作为本地 SQLite 文件路径并自动创建父目录。0.5.1 的普通
`file-governance run` 命令尚不接收应用数据库路径，也不会在执行治理图时自动
运行迁移。

## 0.5.2 LangChain 多模型适配

本版本是向 `0.6.0` 演进的第二批。统一 Client 默认通过 LangChain
`with_structured_output(..., include_raw=True)` 调用真实模型，以继续获得经过
Pydantic 校验的输出和 Token 用量。默认依赖只安装 `langchain-openai` 演示
Provider；其他 Provider 使用可选依赖组按需安装，不会随基础安装一次性拉取。旧
`app.llm.providers.openai.OpenAILLMProvider` 暂时保留导入兼容，但业务 Client
创建所有真实模型时统一使用 `LangChainChatModelProvider`。

当前适配范围：

- LangChain 内置主流 Provider：OpenAI、Anthropic、Google Gemini/Vertex AI、
  DeepSeek、Azure OpenAI、AWS Bedrock、Groq、Mistral、Cohere、xAI、Ollama、
  Hugging Face、NVIDIA、Together、Fireworks、Perplexity、OpenRouter、LiteLLM 等；
- Qwen 使用 `langchain-qwq` 的 `ChatQwen`；
- GLM/ZhipuAI 使用维护中的 `langchain-openai` 连接其 OpenAI 兼容端点，必须设置
  `ZHIPUAI_BASE_URL`；
- 任意标准 OpenAI Chat Completions 中转站使用 `openai_compatible`；OpenRouter
  和 LiteLLM 使用专用 Provider，以保留其结构化输出和路由语义。

只安装实际使用的原生集成：

```bash
python -m pip install ".[anthropic]"
python -m pip install ".[gemini]"
python -m pip install ".[deepseek]"
python -m pip install ".[qwen]"
python -m pip install ".[openrouter]"
python -m pip install ".[litellm]"
```

其他 LangChain 内置 Provider 按运行时报错给出的包名单独安装，例如
`langchain-aws`、`langchain-groq` 或 `langchain-ollama`。

多模型配置使用有序 `profiles` 列表、`default_profile_id` 和固定任务路由：

```json
{
  "enabled": true,
  "profiles": [
    {
      "id": "content-claude",
      "provider": "anthropic",
      "model": "claude-sonnet-4-6",
      "api_key_env": "ANTHROPIC_API_KEY",
      "temperature": 0.0,
      "max_output_tokens": 800,
      "timeout_seconds": 30.0
    },
    {
      "id": "version-deepseek",
      "provider": "deepseek",
      "model": "deepseek-chat",
      "api_key_env": "DEEPSEEK_API_KEY",
      "structured_output_method": "function_calling",
      "temperature": 0.0,
      "max_output_tokens": 1600,
      "timeout_seconds": 45.0
    },
    {
      "id": "evidence-qwen",
      "provider": "qwen",
      "model": "qwen-flash",
      "api_key_env": "DASHSCOPE_API_KEY",
      "base_url_env": "DASHSCOPE_API_BASE",
      "structured_output_method": "function_calling",
      "temperature": 0.0,
      "max_output_tokens": 800,
      "timeout_seconds": 30.0
    }
  ],
  "default_profile_id": "content-claude",
  "task_profile_ids": {
    "content": "content-claude",
    "version": "version-deepseek",
    "evidence": "evidence-qwen"
  },
  "fallback_enabled": true
}
```

`task_profile_ids` 只接受 `content`、`version`、`evidence` 三个固定任务类型，
且目标 ID 必须存在；Profile ID 重复、未知 Provider、直接 `api_key`/`base_url`
字段和非法生成边界都会在创建初始状态时被拒绝。省略任务路由时使用
`default_profile_id`。旧 `provider/model/api_key_env` 单模型配置继续有效，会被
转换成 ID 为 `default` 的单一 Profile；顶层兼容字段镜像默认 Profile，便于旧
checkpoint 和调用方平滑升级。

`structured_output_method` 支持 `auto`、`function_calling`、`json_schema` 和
`json_mode`。默认 `auto` 由 Provider 选择；模型本身必须支持所选能力，例如
DeepSeek 的 `deepseek-chat` 可用于本项目结构化摘要，而 `deepseek-reasoner`
不支持结构化输出。`options_env` 可指向只在运行时读取的 JSON 对象，用于
Azure deployment、`default_headers`、`extra_body` 等 Provider 专有参数，但不得
覆盖模型、API Key、Base URL、超时或 Token 预算。

三个 Subagent 在构造 Prompt 前执行 `resolve_model_profile`，模型调用审计新增
`model_profile_id`。关闭 `llm.enabled` 时，即使 Profile 配置为真实 Provider，也会审计
并执行 `disabled-mock`，不读取环境变量；模型失败、超时、结构化解析失败和越权
引用仍沿用既有确定性回退。

## 图结构

顶层图：

```text
initialize_run
  -> execute_before_run_hooks
  -> validate_request
  -> load_system_prompt
  -> plan_run_tasks
  -> run_inventory_subgraph -> sync_inventory_task_status
  -> dispatch_content_subagent_task
  -> run_version_analysis_subgraph -> sync_version_task_status
  -> run_evidence_subgraph -> sync_evidence_task_status
  -> dispatch_evidence_subagent_task
  -> run_recommendation_subgraph -> sync_recommendation_task_status
  -> [prepare_human_review -> interrupt -> apply_human_selection
      -> sync_human_review_task_status]
  -> generate_governance_report | generate_no_data_report | generate_failure_report
  -> [具有合法 Task DAG 时 sync_report_task_status]
  -> execute_after_run_hooks
  -> [generate_lifecycle_failure_report]
  -> finalize_run
```

`generate_failure_report` 始终只有一个节点。请求、Prompt 或 Task 规划前后没有合法
DAG 的失败直接进入 after-run hooks；四个业务阶段失败则先由同步节点更新失败和
阻断状态，再由报告同步节点完成 Report Task。

Inventory 子图按队列逐文件解析。单文件失败只产生非致命错误并继续处理；目录
无法访问或状态引用不一致等问题才形成致命错误。

Version Analysis 子图按队列逐文件对比较，然后统一构建版本边、分叉和版本链。
主版本推荐已完全迁移到 Recommendation 子图。顶层包装节点使用
`app/state/converters.py` 显式转换状态，解析队列、比较队列、Evidence 任务和
推荐候选集合等子图私有字段不会泄漏回顶层状态。

三个固定 Subagent 子图共享以下模型路由前缀，后续输出校验与回退路径保持不变：

```text
validate_*_subagent_input
  -> resolve_model_profile
  -> build_*_subagent_prompt
  -> execute_before_model_hooks
  -> invoke_*_structured_llm
  -> execute_after_model_hooks
  -> validate_*_subagent_output | build_deterministic_*_fallback
```

独立 Evidence 子图：

```text
START
  -> collect_pdf_candidates
  -> create_pdf_match_jobs
  -> [fanout_pdf_matching -> Send(match_pdf_to_source_version) -> join_pdf_matches]
  -> load_local_delivery_log
  -> match_delivery_to_version
  -> merge_external_evidence
  -> validate_evidence_confidence
  -> capture_evidence_memory
  -> END
```

没有 PDF 时直接跳到本地发送日志；单个 PDF 或日志读取失败可记录非致命错误并
继续，状态引用或证据关系不一致才产生致命错误。`run_evidence_subgraph()` 通过
白名单转换只返回 `memory`、`pdf_exports`、`deliveries` 和 `errors`，候选、任务和原始日志
不会泄漏到顶层状态。顶层图在版本分析成功后调用该子图，并允许日志读取或单个
PDF 匹配等非致命错误降级后继续 Recommendation。

独立 Recommendation 子图：

```text
START
  -> find_editable_leaf_versions
  -> score_version_candidates
  -> apply_recalled_memory
  -> apply_delivery_rules
  -> apply_pdf_source_rules
  -> apply_branch_rules
  -> select_main_versions
  -> explain_recommendations
  -> calculate_decision_confidence
  -> preserve_complete_version_chains
  -> mark_human_review_items
  -> validate_recommendation_results
  -> capture_recommendation_memory
  -> END
```

Recommendation 子图只在各自版本组内竞争主版本：客户确认和可靠发送记录增强
具体版本，PDF 来源关系优先可编辑源文件，分叉、链不完整、近似并列或低于阈值
的结果强制进入人工审核。推荐只表达主版本偏好，`preserve_file_ids` 始终保留组内
全部版本；`run_recommendation_subgraph()` 仅返回 `memory`、`decisions`、
`human_review` 和 `errors`，私有候选集合不会泄漏到顶层状态。Recommendation
完成后，致命错误
进入失败报告；分叉、链不完整、近似并列或低置信度结果进入人工审核；其余结果
直接生成证据化治理报告。

第四批的证据协议、匹配优先级、推荐加权和错误语义详见
[Evidence 接入与治理决策说明](docs/version-0.4-evidence.md)。

## 0.5.3 Task 级 Skills

本版本是向 `0.6.0` 演进的第三批。Skill 资源由
`resources/skills/registry.yaml` 统一登记，当前包含：

- `file-content-analysis`：Inventory Task 的 Content 内容说明；
- `version-relation`：Version Analysis Task 的版本关系解释；
- `evidence-confidence`：Evidence Task 的证据强弱与限制说明；
- `governance-report`：Recommendation、Human Review 和 Report 的协调者报告规则。

主图中的 `load_skill_registry` 只读取 YAML 元数据并验证四个 `SKILL.md` 的受控
路径，不读取指令正文。固定 Subagent 分派进入 Team Orchestration 后按以下顺序
执行：

1. `select_task_skills` 根据真实 Task、`assigned_role` 和固定 Agent 注册表选择最小集合；
2. `load_task_skills` 只读取选择项，并确保其他 Skill 没有正文；
3. `bind_task_skills` 把正文和 SHA-256 绑定到当前 Task 与固定 Agent；
4. Subagent Prompt 只追加本次绑定且摘要一致的 Skill 指令；
5. `release_task_skills` 在成功或协调者回退收口后清空正文、摘要和绑定，恢复
   `available`。

Skill 不是工具，不会自行读取业务文件、访问网络或执行命令。当前三个固定
Subagent 已实际消费各自 Skill；`governance-report` 已登记 Coordinator 的三个
Task 范围，供后续报告 Agent 化批次使用，现有确定性 Recommendation、Human
Review 和 Report 节点不改为模型调用。

## 0.5.4 短期与长期 Memory

本版本是向 `0.6.0` 演进的第四批。Memory 默认关闭，只有请求信封显式设置
`memory.enabled=true` 后才访问应用数据库：

- `recall_long_term_memory` 在 Skill 注册表加载完成后、Task 规划前读取当前
  工作空间的最近长期治理事实；数据库不可用时记录非致命错误并继续当前运行；
- Evidence 子图只捕获阶段计数、高置信度 PDF 来源关系及客户确认关系；
- Recommendation 子图只把同组历史人工选择作为 `0.03` 的有界评分信号，仍由
  当前文件事实、版本链和外部证据决定主版本；
- 人工审核节点只保存版本组 ID 与所选文件 ID，明确忽略 `review_note`；
- `persist_long_term_memory` 在报告 Task 收口后、`after_run` Hook 前幂等写入，
  短期阶段摘要不会写入应用数据库；
- 所有长期条目在创建和写入前都经过摘要长度、凭据模式、结构化字段白名单、
  引用数量、命名空间和来源运行一致性复验。

正式运行前需要执行一次迁移，随后在请求信封中启用 Memory：

```bash
python -m alembic upgrade head
```

```json
{
  "memory": {
    "enabled": true,
    "namespace": null,
    "database_path": "../.artifacts/database/file-governance-app.sqlite3",
    "recall_limit": 50
  }
}
```

`namespace=null` 时使用规范化输入根目录的 SHA-256 哈希；显式提供的命名空间
也只作为哈希种子，不会原样写入数据库。数据库父目录会在首次建立 Engine 时
自动创建，但应用不会用 ORM `create_all()` 替代正式迁移。省略
`memory.database_path` 时可通过 `FILE_GOVERNANCE_DATABASE_PATH` 覆盖默认位置；
请求中显式路径的优先级更高。

## 0.5.5 Context Compact

本版本是向 `0.6.0` 演进的第五批。Context Compact 默认关闭，启用后在两个固定
安全点运行：

1. `after_inventory`：只释放已经完成加载校验、且后续业务节点不再读取的
   System Prompt 正文；文档记录保持原样。
2. `after_evidence`：Version Analysis、Evidence 和 Evidence Subagent 已完成，
   此时可把 Recommendation 不再消费的 `content_preview`、`structure_summary`
   和 `key_fields` 移到 `intermediate` 产物。

压缩后的文档仍保留 `id`、`file_id`、`content_ref`、`normalized_digest`、
解析器和警告，完整标准化内容可由 `content_ref` 重建。压缩计划完全不接收
`version_edges`、`branches`、`decisions` 或 `human_review`，集成测试还会对
启用和关闭路径逐值比较这些字段。

```text
sync_inventory_task_status
  -> run_context_compact_after_inventory
  -> dispatch_content_subagent_task
  -> ...
dispatch_evidence_subagent_task
  -> run_context_compact_after_evidence
  -> run_recommendation_subgraph
```

请求信封示例：

```json
{
  "context_compact": {
    "enabled": true,
    "trigger_token_threshold": 12000,
    "retained_preview_characters": 0,
    "persist_summaries": true,
    "database_path": "../.artifacts/database/file-governance-app.sqlite3"
  }
}
```

Token 估算不调用模型或外部分词服务：ASCII 文本按每四字符一个 Token 近似，
中文等非 ASCII 字符按一字符一个 Token 保守估算。压缩详情写入受控中间产物；
`context_summaries` 表只保存固定模板摘要、压缩后估算、序号和产物引用。启用
数据库摘要前仍需执行 `python -m alembic upgrade head`。

## 安装

要求 Python 3.10+。

```bash
python -m pip install -r requirements.txt
```

`requirements.txt` 会以可编辑模式安装本项目；实际依赖及版本约束仍统一维护在
`pyproject.toml` 中。Claude、Gemini、DeepSeek、Qwen、OpenRouter 和 LiteLLM
等可选 Provider 请按上文的 extras 安装命令按需安装。

安装测试和静态检查依赖：

```bash
python -m pip install -e ".[dev]"
```

安装后会提供 `file-governance` 命令，也可以使用
`python -m app.entrypoints.cli`。

构建 wheel 时，受控 Prompt、Skill 注册表和四个 `SKILL.md` 会随分发包进入安装
数据目录：

```bash
python -m pip wheel . --no-deps --no-build-isolation
```

## 准备请求

`examples/sample_request.json` 是默认关闭真实模型的完整请求信封；
`examples/sample_llm_request.json` 则通过 Claude、DeepSeek 和 Qwen Profile
启用三个阶段的模型摘要，并额外声明 Gemini、GLM 和通用中转站候选 Profile。
相对路径以 JSON 文件所在目录
为基准解析，因此示例中的 `../data/input` 指向仓库根目录下的 `data/input`。
`delivery_log_path` 同样相对请求文件解析；设为 `null` 可跳过本地发送记录。
示例中的 `prompt`、`hooks`、`memory` 和 `context_compact` 是可选请求信封对象，
目前均显式关闭。
CLI 会单独解析这些对象并传给状态工厂，不会把它们合并进业务 `request`。启用 Prompt 时，
`source_path` 的相对路径同样以请求 JSON 所在目录为基准。

```json
{
  "request": {
    "root_directory": "../data/input",
    "recursive": true,
    "allowed_extensions": [".xlsx", ".docx", ".pdf"],
    "max_files": 500,
    "grouping_similarity_threshold": 0.72,
    "auto_select_threshold": 0.82,
    "pdf_match_threshold": 0.82,
    "delivery_log_path": "sample_delivery_log.json",
    "use_llm_summary": false
  },
  "workspace": {
    "input_root": "../data/input",
    "input_readonly": true,
    "artifact_root": "../.artifacts/content",
    "report_root": "../.artifacts/reports"
  },
  "prompt": {
    "enabled": false,
    "version": "file-governance-v1",
    "source_path": "../resources/prompts/file_governance_system_v1.md",
    "dynamic_rules": []
  },
  "hooks": {
    "enabled": false,
    "before_run": [
      "validate_request_envelope_hook",
      "enrich_run_state_hook",
      "initialize_tool_audit_hook"
    ],
    "before_model": [],
    "after_model": [],
    "after_run": [
      "validate_report_result_hook",
      "flush_tool_audit_hook",
      "cleanup_run_resources_hook"
    ],
    "default_failure_policy": "block",
    "failure_policies": {
      "initialize_tool_audit_hook": "ignore",
      "flush_tool_audit_hook": "ignore",
      "cleanup_run_resources_hook": "ignore"
    }
  },
  "llm": {
    "enabled": false,
    "provider": "mock",
    "model": "mock-structured-v1",
    "api_key_env": null,
    "base_url_env": null,
    "options_env": null,
    "structured_output_method": "auto",
    "temperature": 0.0,
    "max_output_tokens": 800,
    "timeout_seconds": 30.0,
    "fallback_enabled": true
  },
  "checkpoint": {
    "backend": "sqlite",
    "database_path": "../.artifacts/checkpoints/file-governance-0.5.sqlite3"
  }
}
```

真实 Provider 请求不得在 JSON 中写入 API Key。本地开发先从可提交模板创建被忽略的
`.env`，再只在本地填写真实值：

```powershell
if (-not (Test-Path -LiteralPath ".env" -PathType Leaf)) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env" -ErrorAction Stop
}
# 使用本地编辑器，只填写实际启用 Provider 对应的密钥和端点。
```

`.env` 已被 `.gitignore` 和 `.dockerignore` 排除，远程仓库只保留 `.env.example`。
应用本身不会自动加载 dotenv 文件；Docker 运行真实 Provider 时必须显式传入
`--env-file .env`。不要使用 `docker build --secret` 之外的方式把 `.env` 复制进镜像。

使用官方 OpenAI API 时只填写：

```dotenv
OPENAI_API_KEY=你的官方API密钥
```

使用第三方 OpenAI 兼容中转站时，在 Profile 中选择
`provider: "openai_compatible"`，并在本地 `.env` 填写：

```dotenv
OPENAI_COMPATIBLE_API_KEY=中转站提供的API密钥
OPENAI_COMPATIBLE_BASE_URL=https://你的中转站地址/v1
# 可选：中转站要求的 Header 或 extra_body，只在运行时读取。
OPENAI_COMPATIBLE_OPTIONS={"default_headers":{"X-Tenant":"tenant-a"}}
```

LangChain `ChatOpenAI` 只保证兼容官方 OpenAI API 规范；中转站必须支持其
Chat Model 请求和结构化输出能力。OpenRouter 和 LiteLLM 应分别使用
`provider: "openrouter"` 与 `provider: "litellm"` 及其可选依赖，避免把专有
路由字段当成普通 OpenAI 响应丢弃。其他中转站可通过 `options_env` 注入受控
`default_headers` 或 `extra_body`；模型名称必须替换为中转站实际支持的名称。

Profile、模型名称和任务路由由 `sample_llm_request.json` 配置，可按部署环境支持
的结构化输出模型调整。自动化测试使用注入的 LangChain Chat Model 和离线 Mock
验证真实适配与多模型路由，不会在 CI 中产生外部费用；真实模型 smoke 应使用下方
Docker 命令完成。

创建输入目录并放入待治理文件：

```bash
mkdir -p data/input
```

## 本地发送记录协议

本地发送记录使用 `schema_version: "1.0"` 和 `deliveries` 数组。完整脱敏示例见
`examples/sample_delivery_log.json`。每条记录包含：

- `id`：发送记录稳定唯一 ID；
- `attachment_name`：发送时的附件名称；
- `attachment_sha256`：可选原始附件 SHA-256；
- `normalized_digest`：可选标准化内容 SHA-256；
- `sent_at`：可选、必须带时区的 ISO 8601 发送时间；
- `recipient_label`：脱敏收件人标签；
- `customer_confirmed`：是否存在客户确认或批准记录；
- `evidence_ref`：指向原始记录的稳定引用，不应包含正文或凭据。

真实发送日志默认被 `.gitignore` 和 `.dockerignore` 排除，只允许通过用户明确
提供的路径或只读挂载进入运行环境。当前 Evidence 子图会消费该协议，
Recommendation 使用可靠匹配结果加权，最终治理报告展示脱敏记录和证据引用。

## CLI 启动治理

```bash
file-governance run examples/sample_request.json \
  --thread-id governance-run-001
```

也可以临时覆盖 checkpoint：

```bash
file-governance run examples/sample_request.json \
  --thread-id governance-run-001 \
  --checkpoint-backend memory
```

CLI 输出固定为最小 JSON 摘要。自动完成时包含报告路径；需要人工确认时，
`status` 为 `waiting_human`，并在 `interrupts` 中列出版本组和候选文件。所有路径
都会同时输出：

- `todos`：按 `order` 排列的用户进度，只含 ID、标题、状态和关联 Task ID；
- `task_status_counts`：固定包含 pending、running、completed、failed、skipped；
- 原有 `thread_id`、`status`、`summary`、`report_path` 和 `interrupts`。

CLI 不输出完整 Task、文档正文、报告 Markdown 或大型产物。完整脱敏示例见
[`examples/sample_task_progress.json`](examples/sample_task_progress.json)。

## CLI 恢复人工审核

把选择保存为 JSON，例如 `review_response.json`：

```json
{
  "selections": {
    "<group_id>": "<selected_file_id>"
  },
  "review_note": "已核对业务内容"
}
```

使用启动时完全相同的 `thread_id` 和 SQLite 数据库恢复：

```bash
file-governance resume review_response.json \
  --thread-id governance-run-001 \
  --checkpoint-path .artifacts/checkpoints/file-governance.sqlite3
```

`selections` 必须恰好覆盖全部待审核版本组，且每个文件 ID 必须属于对应版本组。
`memory` 后端只适合同一 Python 进程，不能用于两个独立 CLI 进程之间的恢复。

## Python 调用

不需要跨进程恢复时，可以直接使用默认的内存 Checkpointer：

```python
from app.graphs.file_governance import file_governance_graph
from app.state.factories import create_initial_state

state = create_initial_state(
    {
        "root_directory": "/data/input",
        "recursive": True,
        "allowed_extensions": [".xlsx", ".docx", ".pdf"],
        "max_files": 500,
        "grouping_similarity_threshold": 0.72,
        "auto_select_threshold": 0.82,
        "pdf_match_threshold": 0.82,
        "delivery_log_path": None,
        "use_llm_summary": False,
    },
    {
        "input_root": "/data/input",
        "input_readonly": True,
        "artifact_root": "/data/artifacts/content",
        "report_root": "/data/artifacts/reports",
    },
    # 0.5.2 仍默认关闭真实模型；旧单模型配置会规范化为离线 Mock Profile。
    prompt_config={"enabled": False},
    hook_config={"enabled": False},
    llm_config={
        "enabled": False,
        "provider": "mock",
        "model": "mock-structured-v1",
    },
)

config = {"configurable": {"thread_id": "governance-run-001"}}
result = file_governance_graph.invoke(state, config=config)
```

需要持久化时，由调用方管理 Checkpointer 生命周期：

```python
from app.graphs.file_governance import build_file_governance_graph
from app.storage.checkpoints import open_checkpointer

with open_checkpointer(
    "sqlite",
    database_path="/data/artifacts/checkpoints/file-governance.sqlite3",
    input_root="/data/input",
) as checkpointer:
    graph = build_file_governance_graph(checkpointer=checkpointer)
    result = graph.invoke(state, config=config)
```

## 默认配置

`configs/default.yaml` 记录部署默认值，包括：

- 扫描扩展名、最大文件数和解析资源上限；
- 文档分组及自动选择阈值；
- PDF 来源匹配阈值、本地发送日志读取上限和歧义分差；
- 默认关闭的 Prompt、Hooks、执行顺序和失败策略；
- 默认关闭真实模型的旧单模型兼容写法、Profile 规范化边界和回退配置；
- `.artifacts/content/normalized` 和 `intermediate` 产物布局；
- Markdown 报告目录；
- SQLite checkpoint 后端及数据库路径；
- 默认关闭的 Memory、哈希命名空间、召回上限和独立应用数据库路径；
- 默认关闭的 Context Compact、Token 阈值、预览保留量和摘要持久化配置；
- 应用数据库父目录自动创建、SQL 日志和文件锁等待配置。

当前 CLI 以请求 JSON 为直接运行配置；YAML 用于记录统一部署默认值。0.5.5 的
Memory 与 Context Compact 数据库路径均已接入 CLI 和主图；迁移命令继续读取 `alembic.ini` 或
`FILE_GOVERNANCE_DATABASE_PATH`，不会在普通治理运行中静默修改表结构。

## 测试

```bash
python -m pytest
python -m ruff check app tests
python -m compileall -q app tests
```

新的测试结构覆盖：

- 文件名归一化、内容支持的合组和无关文档隔离；
- 候选对、差异、重复边、分叉和线性版本链；
- 可解释候选评分、自动推荐和人工选择限制；
- 本地发送日志协议、只读边界、大小限制和时间字段校验；
- PDF 来源及发送记录的哈希、内容摘要、名称和歧义匹配规则；
- Evidence 子图的 Send 并行汇合、空分支、日志降级和包装字段隔离；
- Recommendation 子图的证据加权、分叉审核、空输入和包装字段隔离；
- 四子图端到端顺序、发送证据加权、报告展示和非致命 Evidence 降级；
- 真实 DOCX 顶层治理及原文件字节不变；
- SQLite Checkpointer 关闭后重新打开并恢复 `interrupt()`；
- 最小 CLI 的真实请求文件调用；
- Prompt 资源中的只读、证据和人工确认规则；
- Prompt/Hook 默认关闭、显式配置复制和非法失败策略拒绝；
- Prompt 路径范围、符号链接、UTF-8、大小、动态规则和 SHA-256；
- 静态 Hook 注册、顺序执行、跳过事件、block/ignore 和状态写入白名单；
- 请求预检、状态补充、报告检查、最小审计与只读清理内置 Hook。
- Prompt/Hook 顶层节点顺序、旧状态关闭兼容和 CLI 请求信封字段隔离；
- before_run 阻断、Prompt 加载失败、after_run ignore/block 及生命周期失败报告。
- Prompt 和 Hooks 同时关闭时与 0.2.0 参照顶层路径的业务结果兼容性。
- 固定 Task DAG 的确定性创建、幂等补齐、角色映射和已有状态保护；
- 重复 ID、重复依赖、未知依赖、自依赖和循环依赖拒绝；
- Todo 对 Task 状态的确定性纯投影、正常跳过和失败阻断语义。
- Team Orchestration 的状态同步/分派双路径、无效 DAG 截断和固定团队初始化；
- Task 更新的依赖检查、终态幂等、产物引用合并和错误收口；
- 私有 task_update/dispatch_request 消费、转换器白名单和顶层包装字段隔离；
- 重复调用子图时 Task、Todo 和时间字段不重复、不重置。
- Content、Version、Evidence 唯一角色选择及 assignment/result/error 消息往返；
- 模型失败与伪造引用的协调者回退、fallback 审计和 Task 引用登记；
- 动态团队成员、角色篡改、协调者 Task 分派和 Worktree 节点缺失检查；
- 顶层四业务 Task 的顺序推进、无需人工审核时的正常跳过和报告完成；
- interrupt 期间 Human Review running、恢复后的审核与报告 Task 完成；
- 业务失败源 Task、下游阻断跳过、失败报告收口和 Todo blocked 语义；
- 无数据报告不会遗留 pending Todo，以及 0.3.0 业务与报告内容兼容性。
- CLI 最终、人工暂停和恢复输出中的 Todo 顺序与五状态 Task 计数；
- CLI 字段白名单对文档正文、完整报告和 Task 产物引用的隔离；
- nodes 目录函数与所有 LangGraph `add_node()` 注册关系的一致性；
- LLM 配置未知字段、直接密钥、非法范围和环境变量名称拒绝；
- 旧单模型配置转换、重复 Profile、未知任务路由和不存在的 Profile 引用拒绝；
- LangChain 主流 Provider 注册、别名、按需依赖报错、专有参数和统一 Token 用量提取；
- Content、Version、Evidence 三个子图跨 Claude、DeepSeek、Qwen Profile 的离线路由；
- Mock 结构化调用、Token 记录、确定性超时和非法 Pydantic 输出失败审计；
- OpenAI 兼容中转站的结构化参数、Header、extra_body 和缺失环境变量拒绝；
- 三个 Subagent 输出的额外字段拒绝和产物引用白名单；
- Content、Evidence 阶段后分派以及 Version Analysis 内部摘要升级；
- Version Subagent 成功来源登记与模型不可用时的确定性事实一致性；
- LangChain 多 Provider 适配成功、Mock 成功、确定性超时、缺失 API Key 和非法
  Pydantic 输出；
- Subagent 越权产物引用拒绝，以及 Content、Version、Evidence 分别失败后的安全回退；
- 关闭真实 LLM 时与 0.4.0 确定性参照图的治理结论一致性；
- SQLite checkpoint 恢复状态和原始数据库字节均不包含 API Key 实际值、私有 Prompt
  或长正文尾部；
- 应用数据库路径与 checkpoint 文件隔离、五个 Repository 的事务提交和异常回滚；
- Alembic `upgrade head`、`downgrade base`、再次升级及 ORM 元数据一致性；
- Memory 策略的长正文/凭据拒绝、有界历史偏好和自由文本隔离；
- 释放并重建数据库连接后的长期 Memory 召回；
- 应用数据库原始字节不包含文档长正文、API Key 或完整模型 Prompt；
- Token 估算的中英文确定性、阈值跳过和两个阶段字段压缩边界；
- Context Compact 条件子图、未跟踪临时载荷、中间产物和数据库有界摘要；
- 启用与关闭压缩时 `version_edges`、`branches`、`decisions` 和人工选择完全一致；
- Skill 注册表未知字段、路径越界、重复 ID、职责不匹配和正文摘要校验；
- Content、Version、Evidence 分派期间只加载当前 Task Skill，并在收口后恢复
  全部 Skill 为 `available`；
- 正常完成、无需审核、人工暂停恢复、无数据、业务失败、非致命警告和 checkpoint
  重放七条 0.4.0 发布验收路径。

## Docker

构建镜像：

```bash
docker build --build-arg APP_VERSION=0.5.5 -t file-manage-agent:0.5.5 .
```

默认镜像只安装 OpenAI 演示集成。按需构建其他 Provider，例如：

```bash
docker build \
  --build-arg APP_VERSION=0.5.5 \
  --build-arg LLM_EXTRAS=anthropic,deepseek,qwen \
  -t file-manage-agent:0.5.5-mainstream .
```

默认显示 CLI 帮助：

```bash
docker run --rm file-manage-agent:0.5.5
```

容器首次使用应用数据库时，可在同一个可写产物卷中执行迁移。镜像内默认通过
`FILE_GOVERNANCE_DATABASE_PATH` 把数据库放到
`/data/artifacts/database/file-governance-app.sqlite3`：

```bash
docker run --rm \
  --mount type=bind,src=/local/agent-artifacts,dst=/data/artifacts \
  --entrypoint python \
  file-manage-agent:0.5.5 \
  -m alembic upgrade head
```

实际运行时必须只读挂载输入目录和可选发送日志，单独挂载可写产物目录。
请求中的 `delivery_log_path` 应指向 `/data/evidence/delivery_log.json`；不使用
本地证据时应设为 `null`。当请求启用任一真实 Provider 时，必须通过
`--env-file` 显式加载项目根目录下被忽略的本地 `.env`：

```bash
docker run --rm \
  --env-file /local/file-manage-agent/.env \
  --mount type=bind,src=/local/business-files,dst=/data/input,readonly \
  --mount type=bind,src=/local/agent-artifacts,dst=/data/artifacts \
  --mount type=bind,src=/local/delivery_log.json,dst=/data/evidence/delivery_log.json,readonly \
  --mount type=bind,src=/local/request.json,dst=/config/request.json,readonly \
  file-manage-agent:0.5.5 \
  run /config/request.json --thread-id governance-run-001 \
  --checkpoint-path /data/artifacts/checkpoints/file-governance.sqlite3
```

恢复时使用同样的产物挂载，并额外挂载人工选择 JSON：

```bash
docker run --rm \
  --mount type=bind,src=/local/business-files,dst=/data/input,readonly \
  --mount type=bind,src=/local/agent-artifacts,dst=/data/artifacts \
  --mount type=bind,src=/local/review_response.json,dst=/config/review.json,readonly \
  file-manage-agent:0.5.5 \
  resume /config/review.json --thread-id governance-run-001 \
  --checkpoint-path /data/artifacts/checkpoints/file-governance.sqlite3
```

## 当前未实现

- HTTP API、后台 Worker 和定时任务；
- PostgreSQL 等生产级 Checkpointer；
- 配置驱动的 before_model、after_model Hook；本批只有固定 Prompt/审计安全检查；
- 主图持久化工具调用审计；0.5.5 已接入 Memory 与 Context Summary；
- 未安装的可选 LangChain Provider 包；基础安装不会一次性包含全部模型 SDK；
- 邮件 MCP 证据和 Worktree；
- OCR、旧版 `.doc`/`.xls`、宏文件和加密文档处理。
