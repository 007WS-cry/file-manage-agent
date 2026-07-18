# File Manage Agent

基于 LangGraph 的只读文件版本治理 Agent。当前版本 `0.1.0` 已完成能力：

- 只读扫描、SHA-256 去重及 XLSX、DOCX、文本型 PDF 内容提取；
- 内容标准化、版本分组、文件对差异、版本边、分叉和版本链；
- 可解释主版本评分和低置信度人工确认；
- Inventory、Version Analysis 子图和顶层 File Governance 图；
- 标准化内容及中间 JSON 产物的隔离、原子持久化；
- 进程内或 SQLite LangGraph checkpoint；
- 可跨进程恢复 `interrupt()` 的最小 CLI；
- 成功、部分成功、无数据和失败 Markdown 报告。

当前版本提供 Python 接口和 CLI，尚未提供 HTTP API 或后台 Worker。

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

## 目录

```text
file-manage-agent/
├── app/
│   ├── state/                 # 状态、reducer、初始状态工厂和子图状态转换
│   ├── tools/                 # 只读文件扫描与文档解析工具
│   ├── services/              # 标准化、分组、版本图、推荐和报告服务
│   ├── storage/               # 标准化/中间产物与 checkpoint
│   ├── utils/                 # 时间、错误、路径和状态记录查询辅助函数
│   ├── nodes/                 # 仅包含已注册的 LangGraph 节点函数
│   ├── graphs/                # 两个子图与顶层治理图
│   └── entrypoints/           # 最小 CLI
├── configs/default.yaml       # 默认扫描、存储和 checkpoint 参数
├── examples/sample_request.json
├── tests/
│   ├── unit/                  # 分组、版本图和推荐规则单元测试
│   └── integration/           # 顶层图、SQLite 恢复和 CLI 集成测试
├── Dockerfile
└── pyproject.toml
```

`app/state/model.py` 仅用于兼容早期单数文件名，新代码应从
`app.state.models` 导入状态。

## 图结构

顶层图：

```text
initialize_run
  -> validate_request
  -> run_inventory_subgraph
  -> run_version_analysis_subgraph
  -> [prepare_human_review -> interrupt -> apply_human_selection]
  -> generate_governance_report
  -> finalize_run
```

Inventory 子图按队列逐文件解析。单文件失败只产生非致命错误并继续处理；目录
无法访问或状态引用不一致等问题才形成致命错误。

Version Analysis 子图按队列逐文件对比较，然后统一构建版本边、分叉、版本链和
推荐结果。顶层包装节点使用 `app/state/converters.py` 显式转换状态，解析队列、
比较队列和当前草稿等子图私有字段不会泄漏回顶层状态。

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

## 准备请求

`examples/sample_request.json` 是完整请求信封。相对路径以 JSON 文件所在目录
为基准解析，因此示例中的 `../data/input` 指向仓库根目录下的 `data/input`。

```json
{
  "request": {
    "root_directory": "../data/input",
    "recursive": true,
    "allowed_extensions": [".xlsx", ".docx", ".pdf"],
    "max_files": 500,
    "grouping_similarity_threshold": 0.72,
    "auto_select_threshold": 0.82,
    "use_llm_summary": false
  },
  "workspace": {
    "input_root": "../data/input",
    "input_readonly": true,
    "artifact_root": "../.artifacts/content",
    "report_root": "../.artifacts/reports"
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
        "use_llm_summary": False,
    },
    {
        "input_root": "/data/input",
        "input_readonly": True,
        "artifact_root": "/data/artifacts/content",
        "report_root": "/data/artifacts/reports",
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
- 真实 DOCX 顶层治理及原文件字节不变；
- SQLite Checkpointer 关闭后重新打开并恢复 `interrupt()`；
- 最小 CLI 的真实请求文件调用。

## Docker

构建镜像：

```bash
docker build -t file-manage-agent:0.1.0 .
```

默认显示 CLI 帮助：

```bash
docker run --rm file-manage-agent:0.1.0
```

实际运行时必须只读挂载输入目录，单独挂载可写产物目录，并提供路径为
`/data/input`、`/data/artifacts/content`、`/data/artifacts/reports` 的请求文件：

```bash
docker run --rm \
  --mount type=bind,src=/local/business-files,dst=/data/input,readonly \
  --mount type=bind,src=/local/agent-artifacts,dst=/data/artifacts \
  --mount type=bind,src=/local/request.json,dst=/config/request.json,readonly \
  file-manage-agent:0.1.0 \
  run /config/request.json --thread-id governance-run-001 \
  --checkpoint-path /data/artifacts/checkpoints/file-governance.sqlite3
```

恢复时使用同样的产物挂载，并额外挂载人工选择 JSON：

```bash
docker run --rm \
  --mount type=bind,src=/local/business-files,dst=/data/input,readonly \
  --mount type=bind,src=/local/agent-artifacts,dst=/data/artifacts \
  --mount type=bind,src=/local/review_response.json,dst=/config/review.json,readonly \
  file-manage-agent:0.1.0 \
  resume /config/review.json --thread-id governance-run-001 \
  --checkpoint-path /data/artifacts/checkpoints/file-governance.sqlite3
```

## 当前未实现

- HTTP API、后台 Worker 和定时任务；
- PostgreSQL 等生产级 Checkpointer；
- LLM 差异摘要客户端；当前始终使用确定性摘要；
- 邮件证据、长期 Memory、Skills、Subagent 和 Worktree；
- OCR、旧版 `.doc`/`.xls`、宏文件和加密文档处理。
