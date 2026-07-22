# 0.4.1 统一 LLM 基础设施和状态契约

`0.4.1` 是从 `0.4.0` 向固定 Agent Team 演进的第一批版本。本批建立可测试、
可审计且默认不产生外部请求的模型基础设施，不改变 Inventory、Version Analysis、
Evidence、Recommendation 和顶层治理图的确定性执行顺序。

## 完成内容

- `app.llm.client.LLMClient` 统一选择 Provider、传递结构化参数并生成调用审计；
- `MockLLMProvider` 支持预设 Pydantic 返回、模拟 Token、耗时、超时和非法输出；
- `OpenAILLMProvider` 支持 Responses API，并兼容具有 `parse` 接口的 Chat
  Completions Client；
- 三个 Subagent 的输入、输出和内部子图状态集中定义在 `app/state/models.py`；
- Pydantic 输出禁止协议外字段，产物引用必须通过调用方提供的白名单；
- `LLMCallRecord` 记录 Provider、模型、状态、耗时、Token 和脱敏错误；
- `TeamMessage` 使用 `message_id` 作为稳定 reducer 键；
- CLI 请求信封支持独立的 `llm` 配置对象；
- 旧 checkpoint 会补齐关闭状态的 LLM、固定 Team 和空审计集合。

## 安全默认值

默认配置如下：

```yaml
llm:
  enabled: false
  provider: mock
  model: mock-structured-v1
  api_key_env: null
  temperature: 0.0
  max_output_tokens: 800
  timeout_seconds: 30.0
  fallback_enabled: true
```

`enabled: false` 时，统一 Client 强制使用 Mock Provider，不读取任何密钥，也不会
创建真实 Provider 或访问网络。因此升级到 `0.4.1` 不会自动产生模型费用。

## 启用 OpenAI Provider

请求 JSON 中只允许配置环境变量名称：

```json
{
  "llm": {
    "enabled": true,
    "provider": "openai",
    "model": "由部署环境选择的模型名称",
    "api_key_env": "OPENAI_API_KEY",
    "temperature": 0.0,
    "max_output_tokens": 800,
    "timeout_seconds": 30.0,
    "fallback_enabled": true
  }
}
```

本地环境可以参考 `.env.example`，但程序不会自动读取或解析 `.env` 文件。调用方应
通过进程环境、容器密钥或其他密钥管理设施设置 `OPENAI_API_KEY`。请求中的
`api_key`、密钥实际值或未知字段都会被拒绝。

Docker 运行时可以显式传入环境变量：

```bash
docker run --rm \
  --env OPENAI_API_KEY \
  --mount type=bind,src=/local/business-files,dst=/data/input,readonly \
  --mount type=bind,src=/local/agent-artifacts,dst=/data/artifacts \
  --mount type=bind,src=/local/request.json,dst=/config/request.json,readonly \
  file-manage-agent:0.4.1 \
  run /config/request.json
```

镜像构建参数、Dockerfile、镜像层和请求文件中都不得写入真实 API Key。

## 调用结果和审计

统一 Client 成功时返回 Pydantic 输出和一条 `LLMCallRecord`；失败或超时时返回
`output=None` 和失败审计。审计明确不保存：

- System Prompt；
- 用户 Prompt；
- 完整模型响应；
- 文档正文；
- API Key；
- Provider 原始响应体。

`fallback_enabled` 只声明后续业务节点是否允许降级。本批没有在 Client 内隐式执行
业务回退，以免模型基础层擅自改变文件治理结论。

## Provider 边界

Provider 代码只负责模型协议和 SDK 适配，位于 `app/llm/providers/`。它们不是
LangGraph 节点，因此不会放入 `app/nodes/`；也不会在 `app/graphs/routers.py` 中
注册任何函数。`routers.py` 仍只保存被 `add_conditional_edges()` 明确调用的路由。

所有顶层状态、子图状态以及状态引用的输入/输出类均位于 `app/state/models.py`。
状态初始化、兼容补齐和子图边界分别由 `factories.py`、`utils/lifecycle.py` 和
`converters.py` 负责。

## 本批不包含

- 三个固定 Subagent 的 LangGraph 节点；
- before_model/after_model Hooks 的实际模型节点接入；
- Version Analysis 的 LLM 差异摘要；
- 动态 Agent 招聘、递归委派、Skills、Memory 或 Worktree；
- 应用数据库或持久化 LLM 调用表。

这些内容将在后续批次基于当前状态和 Client 契约接入。

## 验证命令

```bash
python -m pytest
python -m ruff check app tests
python -m compileall -q app tests
```

OpenAI 适配器单元测试使用注入的 Fake SDK Client，不访问网络，也不要求测试环境
提供真实 API Key。
