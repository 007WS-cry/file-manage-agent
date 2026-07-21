# File Manage Agent

基于 LangGraph 的只读文件版本治理 Agent。当前版本 `0.3.1` 是从 `0.3.0` 向
`0.4.0` 开发的第一批版本：保留已有 Prompt、Hook 和四个业务子图，并新增确定性
Task System 的状态协议与纯服务层。当前具备以下能力：

- 只读扫描、SHA-256 去重及 XLSX、DOCX、文本型 PDF 内容提取；
- 内容标准化、版本分组、文件对差异、版本边、分叉和版本链；
- 可解释主版本评分和低置信度人工确认；
- Inventory、Version Analysis、Evidence、Recommendation 四个子图和顶层治理图；
- 标准化内容及中间 JSON 产物的隔离、原子持久化；
- 进程内或 SQLite LangGraph checkpoint；
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
- 仅以 Task 为事实来源、不会读取旧 Todo 状态的用户进度纯投影。

四个子图既可独立测试，也已按 Inventory、Version Analysis、Evidence、
Recommendation 的顺序接入顶层 File Governance 图。当前版本提供 Python 接口
和 CLI，尚未提供 HTTP API 或后台 Worker。`0.2.3` 已接入 Prompt 和 Hooks 顶层
节点；Prompt 和 Hooks 默认仍完全关闭，并通过 0.2.0 参照图兼容测试确认业务结果
一致。旧版缺少生命周期字段的 checkpoint 也会自动补齐关闭配置。`0.3.1` 的
Task System 目前是独立纯服务层，尚未接入顶层图执行进度；该接入属于下一批。

## 安全边界

- 原始业务文件始终只读，不删除、移动、重命名或覆盖文件。
- 请求必须显式设置 `workspace.input_readonly = true`。
- 输入目录拒绝符号链接；产物、报告和 checkpoint 不得与输入目录重叠。
- Office 解析器不执行公式、宏、嵌入对象或外部链接。
- PDF 解析器不执行 OCR，也不猜测加密密码。
- 文件大小、ZIP 声明解压大小、Excel 单元格、PDF 页数和提取字符数均有上限。
- 完整正文通过 `content_ref` 指向 `normalized/*.json`，不直接进入图状态。
- 产物 ID 不允许包含路径分隔符，JSON 使用同目录临时文件和原子替换写入。
- 分叉、链不完整、候选近似并列或低置信度结果必须人工确认。
- `interrupt()` 载荷只包含文件 ID、文件名、评分和理由，不包含完整正文。
- 本地发送日志工具只读取用户明确提供的普通 UTF-8 JSON 文件，拒绝符号链接、
  超限文件和未知协议版本，不打开附件、不访问网络且不执行日志内容。
- 证据匹配遇到多个非重复候选时保留未匹配结果，不依靠排序猜测文件版本。
- Prompt 只读取显式配置的本地 `.md`/`.txt` 文件，拒绝符号链接、非 UTF-8、
  超限内容和相对路径越界，并记录实际内容 SHA-256。
- Hook 只能从静态白名单解析，不能通过请求动态导入模块或执行表达式；状态更新
  仅允许完整替换 `run` 或 `report`。
- Task 只保存状态键、产物引用和简短错误，不保存完整文档正文；Todo 只能由 Task
  单向生成，不能作为第二套可写执行状态。

## 目录

```text
file-manage-agent/
├── resources/
│   └── prompts/               # 受版本控制的 System Prompt 资源
├── app/
│   ├── state/                 # 状态、reducer、初始状态工厂和子图状态转换
│   ├── llm/                   # System Prompt 受限加载和后续模型扩展入口
│   ├── hooks/                 # 静态 Hook 注册、顺序执行和内置生命周期 Hook
│   ├── tools/                 # 只读文件扫描、解析和本地发送日志工具
│   ├── services/              # 标准化、版本图、推荐、报告和确定性 Task System
│   ├── storage/               # 标准化/中间产物与 checkpoint
│   ├── utils/                 # 时间、错误、路径和状态记录查询辅助函数
│   ├── nodes/                 # 仅包含已注册的 LangGraph 节点函数
│   ├── graphs/                # 四个独立子图与顶层治理图
│   └── entrypoints/           # 最小 CLI
├── configs/default.yaml       # 默认治理、生命周期、存储和 checkpoint 参数
├── examples/sample_request.json
├── examples/sample_delivery_log.json
├── docs/version-0.3-prompt-hooks.md # 0.3.0 生命周期、兼容性与交付说明
├── docs/version-0.3.1-task-system.md # 0.3.1 状态协议与确定性 Task System
├── docs/version-0.4-evidence.md # 第四批证据链、评分和错误语义说明
├── tests/
│   ├── unit/                  # 分组、版本图、推荐和 Task System 单元测试
│   └── integration/           # 顶层图、SQLite 恢复和 CLI 集成测试
├── Dockerfile
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

## 图结构

顶层图：

```text
initialize_run
  -> execute_before_run_hooks
  -> validate_request
  -> load_system_prompt
  -> run_inventory_subgraph
  -> run_version_analysis_subgraph
  -> run_evidence_subgraph
  -> run_recommendation_subgraph
  -> [prepare_human_review -> interrupt -> apply_human_selection]
  -> generate_governance_report | generate_no_data_report | generate_failure_report
  -> execute_after_run_hooks
  -> [generate_lifecycle_failure_report]
  -> finalize_run
```

`0.3.1` 尚未改变以上执行顺序。确定性 Task System 已可独立调用和测试，下一批再
通过 Team Orchestration 子图把 Task 状态同步节点接入顶层图。

Inventory 子图按队列逐文件解析。单文件失败只产生非致命错误并继续处理；目录
无法访问或状态引用不一致等问题才形成致命错误。

Version Analysis 子图按队列逐文件对比较，然后统一构建版本边、分叉和版本链。
主版本推荐已完全迁移到 Recommendation 子图。顶层包装节点使用
`app/state/converters.py` 显式转换状态，解析队列、比较队列、Evidence 任务和
推荐候选集合等子图私有字段不会泄漏回顶层状态。

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
  -> END
```

没有 PDF 时直接跳到本地发送日志；单个 PDF 或日志读取失败可记录非致命错误并
继续，状态引用或证据关系不一致才产生致命错误。`run_evidence_subgraph()` 通过
白名单转换只返回 `pdf_exports`、`deliveries` 和 `errors`，候选、任务和原始日志
不会泄漏到顶层状态。顶层图在版本分析成功后调用该子图，并允许日志读取或单个
PDF 匹配等非致命错误降级后继续 Recommendation。

独立 Recommendation 子图：

```text
START
  -> find_editable_leaf_versions
  -> score_version_candidates
  -> apply_delivery_rules
  -> apply_pdf_source_rules
  -> apply_branch_rules
  -> select_main_versions
  -> explain_recommendations
  -> calculate_decision_confidence
  -> preserve_complete_version_chains
  -> mark_human_review_items
  -> validate_recommendation_results
  -> END
```

Recommendation 子图只在各自版本组内竞争主版本：客户确认和可靠发送记录增强
具体版本，PDF 来源关系优先可编辑源文件，分叉、链不完整、近似并列或低于阈值
的结果强制进入人工审核。推荐只表达主版本偏好，`preserve_file_ids` 始终保留组内
全部版本；`run_recommendation_subgraph()` 仅返回 `decisions`、`human_review` 和
`errors`，私有候选集合不会泄漏到顶层状态。Recommendation 完成后，致命错误
进入失败报告；分叉、链不完整、近似并列或低置信度结果进入人工审核；其余结果
直接生成证据化治理报告。

第四批的证据协议、匹配优先级、推荐加权和错误语义详见
[Evidence 接入与治理决策说明](docs/version-0.4-evidence.md)。

## 安装

要求 Python 3.10+。

```bash
python -m pip install -e .
```

安装测试和静态检查依赖：

```bash
python -m pip install -e ".[dev]"
```

安装后会提供 `file-governance` 命令，也可以使用
`python -m app.entrypoints.cli`。

构建 wheel 时，受控 Prompt 会随分发包进入安装数据目录：

```bash
python -m pip wheel . --no-deps --no-build-isolation
```

## 准备请求

`examples/sample_request.json` 是完整请求信封。相对路径以 JSON 文件所在目录
为基准解析，因此示例中的 `../data/input` 指向仓库根目录下的 `data/input`。
`delivery_log_path` 同样相对请求文件解析；设为 `null` 可跳过本地发送记录。
示例中的 `prompt` 和 `hooks` 是可选的请求信封对象，目前显式关闭。CLI 会单独解析
这两个对象并传给状态工厂，不会把它们合并进业务 `request`。启用 Prompt 时，
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
  "checkpoint": {
    "backend": "sqlite",
    "database_path": "../.artifacts/checkpoints/file-governance.sqlite3"
  }
}
```

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

CLI 输出固定为 JSON 摘要。自动完成时包含报告路径；需要人工确认时，
`status` 为 `waiting_human`，并在 `interrupts` 中列出版本组和候选文件。

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
    # 0.3.1 默认值仍为关闭；这里显式写出便于说明生命周期配置。
    prompt_config={"enabled": False},
    hook_config={"enabled": False},
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
- `.artifacts/content/normalized` 和 `intermediate` 产物布局；
- Markdown 报告目录；
- SQLite checkpoint 后端及数据库路径。

当前 CLI 以请求 JSON 为直接运行配置；YAML 用于记录统一部署默认值。

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

## Docker

构建镜像：

```bash
docker build --build-arg APP_VERSION=0.3.1 -t file-manage-agent:0.3.1 .
```

默认显示 CLI 帮助：

```bash
docker run --rm file-manage-agent:0.3.1
```

实际运行时必须只读挂载输入目录和可选发送日志，单独挂载可写产物目录。
请求中的 `delivery_log_path` 应指向 `/data/evidence/delivery_log.json`；不使用
本地证据时应设为 `null`：

```bash
docker run --rm \
  --mount type=bind,src=/local/business-files,dst=/data/input,readonly \
  --mount type=bind,src=/local/agent-artifacts,dst=/data/artifacts \
  --mount type=bind,src=/local/delivery_log.json,dst=/data/evidence/delivery_log.json,readonly \
  --mount type=bind,src=/local/request.json,dst=/config/request.json,readonly \
  file-manage-agent:0.3.1 \
  run /config/request.json --thread-id governance-run-001 \
  --checkpoint-path /data/artifacts/checkpoints/file-governance.sqlite3
```

恢复时使用同样的产物挂载，并额外挂载人工选择 JSON：

```bash
docker run --rm \
  --mount type=bind,src=/local/business-files,dst=/data/input,readonly \
  --mount type=bind,src=/local/agent-artifacts,dst=/data/artifacts \
  --mount type=bind,src=/local/review_response.json,dst=/config/review.json,readonly \
  file-manage-agent:0.3.1 \
  resume /config/review.json --thread-id governance-run-001 \
  --checkpoint-path /data/artifacts/checkpoints/file-governance.sqlite3
```

## 当前未实现

- HTTP API、后台 Worker 和定时任务；
- PostgreSQL 等生产级 Checkpointer；
- LLM 差异摘要客户端；当前始终使用确定性摘要；
- before_model、after_model 与真实 LLM 调用的节点接入；
- 持久化工具调用审计；当前只记录最小 HookEvent；
- Team Orchestration 子图及 Task 状态与顶层业务流程的同步节点；
- 邮件 MCP 证据、长期 Memory、Skills、Subagent 和 Worktree；
- OCR、旧版 `.doc`/`.xls`、宏文件和加密文档处理。
