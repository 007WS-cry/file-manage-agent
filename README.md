# File Manage Agent

File Manage Agent 是一个基于 LangGraph 的只读文件版本治理工具。它扫描 XLSX、DOCX
和文本型 PDF，识别内容相近的文件版本，分析差异与证据，并生成可解释的主版本建议和
Markdown 报告。

当前稳定版本：`1.0.0`。

## 主要能力

- **原文件只读**：不会删除、移动、重命名或覆盖业务文件。
- **版本关系分析**：完成内容提取、相似文件分组、版本链、分叉和差异识别。
- **可解释推荐**：结合内容、PDF 来源和发送记录推荐主版本；低置信度结果交由人工确认。
- **离线可用**：默认关闭真实 LLM，使用确定性规则和本地 Mock 即可运行。
- **多种运行方式**：支持 CLI、Python、HTTP API、后台 Worker 和 Cron 调度。
- **可恢复执行**：支持 SQLite checkpoint、人工审核恢复和受控错误恢复。

## 支持范围

| 项目 | 说明 |
| --- | --- |
| Python | 3.10 及以上 |
| 文件格式 | `.xlsx`、`.docx`、文本型 `.pdf` |
| 默认应用数据库 | SQLite |
| 可选应用数据库 | PostgreSQL |
| LangGraph checkpoint | 内存或 SQLite；后台运行使用 SQLite |
| LLM | 默认关闭；可选 OpenAI、Claude、Gemini、DeepSeek、Qwen 等 |

扫描型 PDF 不会自动 OCR，加密 PDF 不会尝试猜测密码，Office 宏、公式、嵌入对象和
外部链接也不会被执行。

## 快速开始

### 1. 安装

在项目根目录创建虚拟环境并安装：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Linux 或 macOS 使用：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### 2. 准备文件

把需要治理的文件放入 `data/input/`。该目录只作为输入使用；报告、数据库和中间产物
会写入 `.artifacts/`。

示例请求 [`examples/sample_request.json`](examples/sample_request.json) 默认关闭
真实模型，可以直接用于本地运行。

### 3. 启动治理

```bash
file-governance run examples/sample_request.json \
  --thread-id governance-run-001
```

命令返回最小 JSON 摘要，其中：

- `status` 表示运行状态；
- `report_path` 指向生成的 Markdown 报告；
- `todos` 展示用户可读的处理进度；
- `interrupts` 表示仍需完成的人工确认。

当状态为 `waiting_human` 时，请保留本次运行的 `thread_id` 和 checkpoint，并按
[人工确认与恢复文档](docs/resume-and-interview.md)提交响应。

## 使用 Docker Compose

默认 Compose 拓扑使用 SQLite，并启动数据库迁移、API、Worker、Scheduler 和只读模拟
邮件 MCP：

```bash
docker compose up --build -d
docker compose ps
```

API 默认监听 `http://127.0.0.1:8000`。提交后台任务：

```bash
curl -X POST http://127.0.0.1:8000/runs \
  -H "Content-Type: application/json" \
  --data @examples/sample_background_submission.json
```

使用返回的 `run_id` 查询状态和下载报告：

```bash
curl http://127.0.0.1:8000/runs/<run_id>
curl -OJ http://127.0.0.1:8000/runs/<run_id>/report
```

PostgreSQL 部署、服务参数、Cron API 和生产检查清单见
[部署与运行](docs/operations.md)。

## 启用真实模型

真实模型是可选能力。请先安装对应 Provider 的依赖，例如：

```bash
python -m pip install -e ".[anthropic]"
python -m pip install -e ".[gemini]"
python -m pip install -e ".[deepseek]"
```

密钥只能通过环境变量或本地 `.env` 提供，不能写入请求 JSON。可以从安全模板开始：

```powershell
Copy-Item .env.example .env
```

应用不会自动加载 `.env`；本地运行时请导出所需环境变量，Docker 运行时使用
`--env-file .env`。模型 Profile 和多模型请求示例见
[`examples/sample_llm_request.json`](examples/sample_llm_request.json)及
[完整技术参考](docs/technical-reference-1.0.0.md)。

## 运行方式

| 场景 | 入口 |
| --- | --- |
| 单次本地治理 | `file-governance run` |
| 人工确认后恢复 | `file-governance resume` |
| 后台任务 | `file-governance-api` + `file-governance-worker` |
| 定时任务 | 后台服务 + `file-governance-scheduler` |
| Python 集成 | `app.graphs.file_governance` |
| 完整容器拓扑 | `docker compose` |

## 安全提示

- 请求必须设置 `workspace.input_readonly = true`。
- 输入目录不能与产物、报告、数据库或 checkpoint 目录重叠。
- 不要在请求、日志或版本库中保存 API Key、数据库密码或真实邮件正文。
- 报告应通过 CLI 返回路径或受控 HTTP 下载接口获取。
- 部署前请阅读 [安全策略](SECURITY.md)。

## 文档

- [技术文档索引](docs/README.md)
- [部署与运行](docs/operations.md)
- [1.0.0 演示手册](docs/demo-1.0.0.md)
- [人工确认与后台恢复](docs/resume-and-interview.md)
- [1.0.0 架构说明](docs/architecture-1.0.0.md)
- [开发与测试](docs/development.md)
- [1.0.0 发布说明](docs/release-1.0.0.md)
- [1.0.0 完整技术参考](docs/technical-reference-1.0.0.md)

## License

本项目使用 [MIT License](LICENSE)。
