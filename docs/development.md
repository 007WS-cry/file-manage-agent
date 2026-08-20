# 开发与测试

本文说明 File Manage Agent 1.0.0 的源码结构、开发环境和质量检查。系统架构与状态协议
分别见[架构说明](architecture-1.0.0.md)和[状态契约](state-contracts-1.0.0.md)。

当前维护版本为 `1.0.2`；`1.0.0` 架构和状态文档继续作为稳定基线保留。

## 中文注释契约

- 每个 Python 文件必须在 import 区域之后放置中文模块 docstring，准确说明文件职责；
- 每个类必须把中文 docstring 作为类体首项，状态或 Schema 字段逐项使用中文 `#` 注释；
- 每个函数必须把中文 docstring 作为函数体首项，并按需要提供 `Args`、`Returns`、
  `Raises`；工具函数的 docstring 会作为 LLM description 时，只描述真实能力、输入、
  输出和安全边界，不承诺未实现的动作；
- 每个新增模块级常量或变量必须紧邻中文 `#` 注释，说明用途、范围或安全限制；
- 发布前运行 `test_release_structure_contract.py`，确保模块、类和函数的中文 docstring
  契约没有回退。

## 源码结构

```text
app/
├── agents/          # 固定 Subagent、注册表和 Team Protocol
├── api/             # 运行、恢复、报告和计划路由
├── entrypoints/     # CLI、API、Worker、Scheduler、模拟 MCP 入口
├── graphs/          # 业务图、团队图、恢复图和顶层图
├── hooks/           # 生命周期 Hook
├── llm/             # Profile、Provider、统一 Client 和输出校验
├── mcp_servers/     # 只读模拟邮件 MCP
├── nodes/           # 通过 add_node 注册的图节点
├── observability/   # 结构化日志
├── runtime/         # 队列、Worker 和 Scheduler
├── services/        # 版本、Memory、恢复和持久化服务
├── skills/          # Skill 注册、选择和加载
├── state/           # 状态、转换器、工厂和 reducer
├── storage/         # 产物、checkpoint、ORM 和 Repository
├── tools/           # 文件、证据、MCP 和 Worktree 工具
└── utils/           # 通用辅助逻辑

alembic/             # 应用数据库迁移
configs/             # 默认配置
examples/            # 可提交的脱敏请求与响应示例
resources/           # 受控 Prompt 和 Skills
scripts/             # 演示、验收和备份恢复脚本
tests/               # 单元、集成和端到端测试
docs/                # 技术与版本文档
```

## 开发环境

PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Linux 或 macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

实际依赖和版本约束以 `pyproject.toml` 为准；`requirements.txt` 只提供基础可编辑安装。

## 质量检查

完整检查：

```bash
python -m pytest
python -m ruff check app tests alembic scripts
python -m compileall -q app tests alembic scripts
```

发布或部署相关改动还应检查 Compose：

```bash
docker compose config
docker compose \
  -f docker-compose.yml \
  -f docker-compose.postgresql.yml \
  config
```

需要 Docker 的 PostgreSQL 集成测试应在可用的隔离环境中单独运行。测试不得依赖真实
LLM、真实邮件系统或生产数据库凭据。

## 配置来源

| 来源 | 用途 |
| --- | --- |
| `pyproject.toml` | 包版本、依赖、命令入口和工具配置 |
| `configs/default.yaml` | 治理、存储、恢复和资源限制默认值 |
| `examples/*.json` | CLI、API、Cron、人工恢复和 LLM 示例 |
| `.env.example` | 可用环境变量名称和安全示例 |
| `alembic.ini` | 默认迁移入口 |
| `docker-compose*.yml` | SQLite 与 PostgreSQL 容器拓扑 |

请求 JSON 不得包含 API Key、数据库密码或未脱敏业务正文。路径配置必须保持输入只读，
并将产物、报告、数据库和 checkpoint 隔离。

## 数据库变更

应用数据库通过 SQLAlchemy 和 Alembic 管理。修改 ORM 后应：

1. 新增可升级、可回退的迁移；
2. 验证 SQLite 和 PostgreSQL；
3. 检查 ORM 元数据与迁移 head 一致；
4. 验证旧状态、幂等键和人工恢复协议不被破坏；
5. 更新状态契约、部署文档和发布说明。

普通治理运行不会自动修改表结构；启动服务前由部署流程执行
`python -m alembic upgrade head`。

## 文档维护

- 面向使用者的安装和快速开始更新到根 `README.md`；
- 部署、API、架构、协议和开发信息更新到 `docs/`；
- 新增示例时放入 `examples/`，文档只引用，不复制维护；
- 版本行为变化同时更新发布说明；
- 所有 Markdown 相对链接应从文档自身目录解析并通过检查。
