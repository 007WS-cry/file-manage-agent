# 0.5.2 LangChain 多模型适配

0.5.2 是从 0.5.0 向 0.6.0 演进的第二批。本批调整 LLM 配置、统一 Client 和三个
固定 Subagent 子图，不改变文件解析、版本关系、证据匹配、推荐评分或应用数据库
接入范围。

## 目标

- 由 LangChain Chat Model 统一执行 Pydantic 结构化输出；
- Content、Version、Evidence 可以选择不同 Provider、模型和调用预算；
- 原生覆盖 Claude、Gemini、DeepSeek 和 Qwen；
- GLM/ZhipuAI 和普通第三方中转站通过 OpenAI 兼容端点接入；
- OpenRouter、LiteLLM 使用专用 LangChain Provider；
- 覆盖 LangChain `init_chat_model` 当前注册的其他主流 Provider；
- 保留 0.5.1 单模型请求、Mock Provider 和确定性回退；
- API Key、Base URL、Provider 专有参数、Prompt 和原始响应不得进入调用审计；
- 默认只安装演示所需的 `langchain-openai`，其他 Provider 包按需安装。

## Provider 与依赖

基础依赖只包含 `langchain-openai`。以下可选组一次只安装部署实际需要的集成：

```bash
python -m pip install ".[anthropic]"
python -m pip install ".[gemini]"
python -m pip install ".[deepseek]"
python -m pip install ".[qwen]"
python -m pip install ".[openrouter]"
python -m pip install ".[litellm]"
```

LangChain 内置注册的 Azure、AWS Bedrock、Groq、Mistral、Cohere、xAI、Ollama、
Hugging Face、NVIDIA、Together、Fireworks 等也进入统一工厂；未安装对应包时，
调用审计只记录脱敏配置错误，并提示应安装的包名。

Qwen 使用 `langchain-qwq.ChatQwen`。GLM/ZhipuAI 暂无维护中的专用 LangChain
集成，因此使用 `langchain-openai.ChatOpenAI` 连接调用方通过
`ZHIPUAI_BASE_URL` 声明的 OpenAI 兼容端点。OpenRouter 与 LiteLLM 不降级成普通
`ChatOpenAI`，避免丢失专有路由语义。

## 状态协议

`ModelProfileState` 保存：

- 稳定 `id`；
- 规范化后的 `provider` 与 `model`；
- `api_key_env`、`base_url_env` 和 `options_env` 环境变量名称；
- `structured_output_method`；
- `temperature`、`max_output_tokens` 和 `timeout_seconds`。

状态只保存环境变量名称。`options_env` 指向的运行时 JSON 可包含
`default_headers`、`extra_body`、Azure deployment 等 Provider 专有参数，但禁止
覆盖模型、Provider、API Key、Base URL、温度、Token、超时和重试策略。

`structured_output_method` 允许：

- `auto`：使用 Provider 默认方法；
- `function_calling`：使用工具调用结构化输出；
- `json_schema`：使用 Provider 原生 JSON Schema；
- `json_mode`：使用 JSON 模式。

模型自身仍需支持所选能力。例如 DeepSeek `deepseek-chat` 支持结构化输出，
`deepseek-reasoner` 不支持；不匹配时调用失败并进入既有确定性回退。

`LLMConfigState` 保存有序 `profiles`、`default_profile_id` 和
`task_profile_ids`。任务路由键固定为 `content`、`version`、`evidence`。旧
`provider`、`model` 和生成参数继续作为默认 Profile 的兼容镜像。

0.5.1 单模型配置会转换为 ID 为 `default` 的 Profile。旧 checkpoint 进入生命周期
或 Subagent 时也会执行同一规范化，不需要离线重写数据库。

## 调用流程

三个固定 Subagent 在输入校验后增加同名节点：

```text
validate_*_subagent_input
  -> resolve_model_profile
  -> build_*_subagent_prompt
  -> execute_before_model_hooks
  -> invoke_*_structured_llm
  -> execute_after_model_hooks
  -> 输出校验或确定性回退
```

统一 Client 接收解析后的 `model_profile_id`。启用真实模型时按 Profile 延迟创建
`LangChainChatModelProvider`；关闭真实模型时忽略真实配置并使用
`disabled-mock`。`LLMCallRecord` 记录实际 Profile、Provider、模型、耗时、Token
和脱敏错误，不记录 Prompt、响应正文或任何环境变量实际值。

Provider 内部重试固定为零，避免一次图节点失败产生不可见的重复费用；失败由已有
图级回退处理。项目不主动开启 LangSmith tracing。

## 中转站边界

普通 OpenAI Chat Completions 兼容站使用：

```json
{
  "id": "relay",
  "provider": "openai_compatible",
  "model": "relay-model",
  "api_key_env": "OPENAI_COMPATIBLE_API_KEY",
  "base_url_env": "OPENAI_COMPATIBLE_BASE_URL",
  "options_env": "OPENAI_COMPATIBLE_OPTIONS",
  "structured_output_method": "function_calling"
}
```

`OPENAI_COMPATIBLE_OPTIONS` 可以是：

```json
{"default_headers":{"X-Tenant":"tenant-a"},"extra_body":{"route":"fast"}}
```

`ChatOpenAI` 只保证 OpenAI API 标准字段。需要 OpenRouter 或 LiteLLM 专有字段时，
必须选择各自 Provider 并安装对应可选组。中转站或其具体模型如果不支持
`with_structured_output`，统一 Client 会记录失败并按配置执行确定性回退。

## 兼容与验证

- 默认 YAML 和 `sample_request.json` 继续关闭真实模型；
- `sample_llm_request.json` 演示 Claude、DeepSeek、Qwen 三类任务路由，并提供
  Gemini、GLM 和通用中转站候选 Profile；
- 旧原生 `OpenAILLMProvider` 暂时保留导入兼容；
- 应用数据库仍未接入主图；
- 自动化测试不访问外部模型，不产生费用；
- 测试覆盖全部注册 Provider、别名默认值、按需依赖错误、运行时专有参数、
  Pydantic 输出、Token 提取和三个子图的跨 Provider 路由。
