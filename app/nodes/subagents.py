from __future__ import annotations

import hashlib
from typing import cast

from app.agents.content import (
    build_content_subagent_prompts,
    build_deterministic_content_output,
)
from app.agents.evidence import (
    build_deterministic_evidence_output,
    build_evidence_subagent_prompts,
)
from app.agents.protocol import (
    MAX_TEAM_MESSAGE_ERROR_CHARACTERS,
    create_assignment_message,
    create_error_message,
    create_result_message,
    validate_team_message,
)
from app.agents.protocol import (
    validate_content_subagent_input as validate_content_input_protocol,
)
from app.agents.protocol import (
    validate_evidence_subagent_input as validate_evidence_input_protocol,
)
from app.agents.protocol import (
    validate_version_subagent_input as validate_version_input_protocol,
)
from app.agents.registry import resolve_fixed_subagent
from app.agents.version import (
    build_deterministic_version_output,
    build_version_subagent_prompts,
)
from app.llm.client import LLMClient
from app.llm.config import create_llm_config_state
from app.llm.model_profiles import DISABLED_MODEL_PROFILE_ID
from app.llm.model_profiles import (
    resolve_model_profile as resolve_configured_model_profile,
)
from app.llm.schemas import validate_output_artifact_refs, validate_structured_output
from app.state.models import (
    ContentSubagentGraphState,
    ContentSubagentOutput,
    EvidenceSubagentGraphState,
    EvidenceSubagentOutput,
    LLMCallRecord,
    VersionSubagentGraphState,
    VersionSubagentOutput,
)
from app.utils.error_context import create_node_error
from app.utils.runtime import utc_now_iso

"""本模块只实现三个 Subagent LangGraph 中通过 add_node 明确注册的节点函数。"""

# 三个固定 Subagent 子图状态的联合类型。
SubagentGraphState = (
    ContentSubagentGraphState
    | VersionSubagentGraphState
    | EvidenceSubagentGraphState
)

# 单个 Subagent Prompt 信封允许的最大字符数，防止完整正文进入模型上下文。
MAX_SUBAGENT_PROMPT_CHARACTERS = 20_000

# 输入缺少真实 Task ID 时用于表达协议错误的保留 Task ID。
INVALID_PROTOCOL_TASK_ID = "protocol-invalid-task"


def resolve_model_profile(state: SubagentGraphState) -> dict:
    """按固定 Subagent 任务类型解析本次调用使用的模型 Profile。

    本节点同时把 0.5.1 及更早 checkpoint 中的单模型 LLM 配置规范化为 Profile
    列表。它只解析环境变量名称和模型参数，不读取 API Key、Base URL 或业务文件。

    Args:
        state: 已通过 Team Protocol 输入校验的任一固定 Subagent 状态。

    Returns:
        规范化 LLM 配置与选中的 Profile ID；配置错误时返回非致命校验错误。
    """
    input_data = state.get("input", {})
    if "document_id" in input_data:
        task_type = "content"
    elif "comparison_id" in input_data:
        task_type = "version"
    else:
        task_type = "evidence"

    try:
        normalized_llm = create_llm_config_state(state.get("llm"))
        profile = resolve_configured_model_profile(
            normalized_llm,
            task_type=task_type,
        )
        return {
            "llm": normalized_llm,
            "selected_model_profile_id": profile["id"],
        }
    except (KeyError, TypeError, ValueError) as error:
        return {
            "selected_model_profile_id": "",
            "output": None,
            "errors": [
                create_node_error(
                    state,
                    stage=f"{task_type}_subagent",
                    node_name="resolve_model_profile",
                    category="protocol",
                    message=str(error)[:MAX_TEAM_MESSAGE_ERROR_CHARACTERS],
                    fatal=False,
                )
            ],
        }


def execute_before_model_hooks(state: SubagentGraphState) -> dict:
    """在模型调用前执行固定的 Prompt 边界安全检查。

    本批只执行内置安全检查，不动态解析 Hook、模块路径或工具描述。检查确保系统
    Prompt 和用户 Prompt 均非空且保持在最小输入上限内。

    Args:
        state: 已由角色专属节点生成最小 Prompt 的 Subagent 状态。

    Returns:
        Prompt 合法时返回空更新；非法时清空输出并返回非致命校验错误。
    """
    system_prompt = state.get("system_prompt")
    user_prompt = state.get("user_prompt")
    error_message: str | None = None
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        error_message = "Subagent system_prompt 必须是非空字符串"
    elif not isinstance(user_prompt, str) or not user_prompt.strip():
        error_message = "Subagent user_prompt 必须是非空字符串"
    elif len(system_prompt) + len(user_prompt) > MAX_SUBAGENT_PROMPT_CHARACTERS:
        error_message = (
            f"Subagent Prompt 总长度不得超过 {MAX_SUBAGENT_PROMPT_CHARACTERS} 个字符"
        )

    if error_message is None:
        return {}
    return {
        "output": None,
        "errors": [
            create_node_error(
                state,
                stage="subagent",
                node_name="execute_before_model_hooks",
                category="protocol",
                message=error_message,
                fatal=False,
            )
        ],
    }


def execute_after_model_hooks(state: SubagentGraphState) -> dict:
    """在模型调用后检查 LLM 审计与 Team assignment 的关联关系。

    该固定安全检查不读取模型原始响应，只确认审计记录的 Task、Agent 和触发消息
    与当前子图一致，避免跨任务结果被错误合并。

    Args:
        state: 已调用统一 LLM Client 的 Subagent 状态。

    Returns:
        审计关联正确时返回空更新；不一致时清空输出并记录校验错误。
    """
    llm_calls = state.get("llm_calls", [])
    assignment_messages = [
        message
        for message in state.get("team_messages", [])
        if message.get("message_type") == "assignment"
    ]
    input_data = state.get("input", {})
    if "document_id" in input_data:
        definition = resolve_fixed_subagent("content")
    elif "comparison_id" in input_data:
        definition = resolve_fixed_subagent("version")
    else:
        definition = resolve_fixed_subagent("evidence")

    error_message: str | None = None
    if not llm_calls:
        error_message = "Subagent 模型调用后缺少 LLMCallRecord"
    elif not assignment_messages:
        error_message = "Subagent 模型调用后缺少 assignment Team Message"
    else:
        call_record = llm_calls[-1]
        assignment = assignment_messages[-1]
        if call_record.get("task_id") != input_data.get("task_id"):
            error_message = "LLMCallRecord.task_id 与 Subagent 输入不一致"
        elif call_record.get("agent_id") != definition.agent_id:
            error_message = "LLMCallRecord.agent_id 与固定 Subagent 不一致"
        elif call_record.get("message_id") != assignment.get("message_id"):
            error_message = "LLMCallRecord.message_id 与 assignment 消息不一致"
        elif state.get("llm", {}).get("enabled") is True and call_record.get(
            "model_profile_id"
        ) != state.get("selected_model_profile_id"):
            error_message = "LLMCallRecord.model_profile_id 与任务路由结果不一致"
        elif state.get("llm", {}).get("enabled") is False and call_record.get(
            "model_profile_id"
        ) != DISABLED_MODEL_PROFILE_ID:
            error_message = "关闭真实 LLM 时必须审计为 disabled-mock Profile"

    if error_message is None:
        return {}
    return {
        "output": None,
        "errors": [
            create_node_error(
                state,
                stage="subagent",
                node_name="execute_after_model_hooks",
                category="protocol",
                message=error_message,
                fatal=False,
            )
        ],
    }


def validate_content_subagent_input(state: ContentSubagentGraphState) -> dict:
    """校验 Content 输入并创建 coordinator 到 Content 的 assignment 消息。

    Args:
        state: 包含待校验 Content 输入、固定 Team 和 LLM 配置的子图状态。

    Returns:
        合法时返回规范化输入和 assignment 消息；非法时返回协议错误。
    """
    try:
        input_data = validate_content_input_protocol(state["input"])
        assignment = create_assignment_message(
            team=state["team"],
            task_id=input_data["task_id"],
            receiver=resolve_fixed_subagent("content").agent_id,
            summary="分配内容摘要与关键字段解释任务，输入仅包含短预览和受控引用。",
            artifact_refs=input_data["artifact_refs"],
        )
        return {"input": input_data, "team_messages": [assignment]}
    except (KeyError, TypeError, ValueError) as error:
        return {
            "output": None,
            "errors": [
                create_node_error(
                    state,
                    stage="content_subagent",
                    node_name="validate_content_subagent_input",
                    category="protocol",
                    message=str(error)[:MAX_TEAM_MESSAGE_ERROR_CHARACTERS],
                    fatal=False,
                )
            ],
        }


def build_content_subagent_prompt(state: ContentSubagentGraphState) -> dict:
    """把已校验 Content 输入转换为不含完整正文的模型 Prompt。

    Args:
        state: 已通过 Content Team Protocol 输入校验的子图状态。

    Returns:
        固定系统 Prompt 和最小 JSON 用户 Prompt。
    """
    try:
        system_prompt, user_prompt = build_content_subagent_prompts(state["input"])
        skill_blocks: list[str] = []
        for instruction in state.get("skill_context", []):
            if instruction.get("skill_id") != "file-content-analysis":
                raise ValueError("Content Subagent 收到职责外 Skill")
            content = str(instruction.get("content", ""))
            digest = str(instruction.get("content_sha256", ""))
            if not content.strip() or hashlib.sha256(content.encode("utf-8")).hexdigest() != digest:
                raise ValueError("Content Skill 正文为空或摘要不一致")
            skill_blocks.append(
                f"### {instruction['name']} ({instruction['skill_id']})\n{content}"
            )
        if skill_blocks:
            system_prompt = (
                system_prompt
                + "\n\n## 当前 Task 已绑定 Skills\n"
                + "\n\n".join(skill_blocks)
            )
        return {"system_prompt": system_prompt, "user_prompt": user_prompt}
    except (KeyError, TypeError, ValueError) as error:
        return {
            "system_prompt": "",
            "user_prompt": "",
            "output": None,
            "errors": [
                create_node_error(
                    state,
                    stage="content_subagent",
                    node_name="build_content_subagent_prompt",
                    category="protocol",
                    message=str(error)[:MAX_TEAM_MESSAGE_ERROR_CHARACTERS],
                    fatal=False,
                )
            ],
        }


def invoke_content_structured_llm(state: ContentSubagentGraphState) -> dict:
    """使用统一 LLM Client 调用 Content Pydantic 结构化输出。

    Args:
        state: 已生成安全 Prompt 和 assignment 消息的 Content 子图状态。

    Returns:
        可选 Content 输出、必有 LLM 审计以及失败时的非致命错误。
    """
    assignment = next(
        (
            message
            for message in reversed(state.get("team_messages", []))
            if message.get("message_type") == "assignment"
        ),
        None,
    )
    if assignment is None:
        return {
            "output": None,
            "errors": [
                create_node_error(
                    state,
                    stage="content_subagent",
                    node_name="invoke_content_structured_llm",
                    category="protocol",
                    message="调用 Content LLM 前缺少 assignment Team Message",
                    fatal=False,
                )
            ],
        }

    definition = resolve_fixed_subagent("content")
    result = LLMClient(state["llm"]).generate_structured(
        task_id=state["input"]["task_id"],
        agent_id=definition.agent_id,
        message_id=assignment["message_id"],
        system_prompt=state["system_prompt"],
        user_prompt=state["user_prompt"],
        output_model=ContentSubagentOutput,
        model_profile_id=state["selected_model_profile_id"],
    )
    update: dict = {
        "output": cast(ContentSubagentOutput | None, result.output),
        "llm_calls": [result.call_record],
    }
    if result.output is None:
        update["errors"] = [
            create_node_error(
                state,
                stage="content_subagent",
                node_name="invoke_content_structured_llm",
                category="llm",
                message=result.call_record.get("error_message")
                or "Content Subagent 结构化模型调用失败",
                fatal=False,
            )
        ]
    return update


def validate_content_subagent_output(state: ContentSubagentGraphState) -> dict:
    """校验 Content 输出只含摘要和输入白名单中的产物引用。

    Args:
        state: 已取得可选模型输出的 Content 子图状态。

    Returns:
        合法 Pydantic 输出，或清空输出并返回非致命校验错误。
    """
    try:
        output = validate_structured_output(state.get("output"), ContentSubagentOutput)
        validate_output_artifact_refs(
            output,
            allowed_refs=state["input"]["artifact_refs"],
        )
        return {"output": output}
    except (KeyError, TypeError, ValueError) as error:
        return {
            "output": None,
            "errors": [
                create_node_error(
                    state,
                    stage="content_subagent",
                    node_name="validate_content_subagent_output",
                    category="protocol",
                    message=str(error)[:MAX_TEAM_MESSAGE_ERROR_CHARACTERS],
                    fatal=False,
                )
            ],
        }


def persist_content_analysis_artifact(state: ContentSubagentGraphState) -> dict:
    """把已验证 Content 输出深复制到可由 Checkpointer 持久化的图状态。

    本节点不读取或改写输入文件，也不伪造新的外部产物引用；详细内容仍由输入中的
    受控引用承载。

    Args:
        state: 已通过 Pydantic 和引用白名单校验的 Content 子图状态。

    Returns:
        与 Provider 返回对象解除可变引用关系的 Content 输出。
    """
    output = state.get("output")
    if output is None:
        return {
            "errors": [
                create_node_error(
                    state,
                    stage="content_subagent",
                    node_name="persist_content_analysis_artifact",
                    category="protocol",
                    message="没有可固化的 Content Subagent 输出",
                    fatal=False,
                )
            ]
        }
    return {"output": output.model_copy(deep=True)}


def build_content_result_message(state: ContentSubagentGraphState) -> dict:
    """把 Content 成功或失败结果转换为合法 Team Protocol 消息。

    Args:
        state: 即将结束的 Content 子图状态。

    Returns:
        只含摘要和受控引用的 result 消息，或含脱敏错误的 error 消息。
    """
    definition = resolve_fixed_subagent("content")
    raw_task_id = state.get("input", {}).get("task_id")
    task_id = raw_task_id if isinstance(raw_task_id, str) and raw_task_id.strip() else INVALID_PROTOCOL_TASK_ID
    output = state.get("output")
    if output is not None:
        message = create_result_message(
            team=state["team"],
            task_id=task_id,
            sender=definition.agent_id,
            summary=output.summary,
            artifact_refs=output.artifact_refs,
        )
        validate_team_message(
            message,
            team=state["team"],
            allowed_artifact_refs=state["input"]["artifact_refs"],
        )
    else:
        errors = state.get("errors", [])
        error_text = errors[-1]["message"] if errors else "Content Subagent 未产生结果"
        message = create_error_message(
            team=state["team"],
            task_id=task_id,
            sender=definition.agent_id,
            summary="Content Subagent 执行失败，已返回协调 Agent。",
            error=error_text[:MAX_TEAM_MESSAGE_ERROR_CHARACTERS],
        )
    return {"team_messages": [message]}


def build_deterministic_content_fallback(state: ContentSubagentGraphState) -> dict:
    """在 Content 模型失败或输出无效时生成确定性回退结果。

    Args:
        state: 允许回退且包含已校验最小输入的 Content 子图状态。

    Returns:
        确定性 Pydantic 输出、回退标记和更新后的 LLM 审计。
    """
    output = build_deterministic_content_output(state["input"])
    validate_output_artifact_refs(output, allowed_refs=state["input"]["artifact_refs"])
    llm_calls = state.get("llm_calls", [])
    if llm_calls:
        call_record = dict(llm_calls[-1])
        call_record["status"] = "fallback"
        call_record["fallback_used"] = True
    else:
        assignment = next(
            (
                message
                for message in reversed(state.get("team_messages", []))
                if message.get("message_type") == "assignment"
            ),
            None,
        )
        timestamp = utc_now_iso()
        task_id = state["input"]["task_id"]
        agent_id = resolve_fixed_subagent("content").agent_id
        call_record = LLMCallRecord(
            id="llm-fallback-" + hashlib.sha256(f"{task_id}:{agent_id}".encode()).hexdigest(),
            task_id=task_id,
            agent_id=agent_id,
            message_id=assignment["message_id"] if assignment else "missing-assignment",
            model_profile_id="deterministic-content-fallback",
            provider="deterministic",
            model="deterministic-content-fallback",
            status="fallback",
            started_at=timestamp,
            finished_at=timestamp,
            duration_ms=0,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            error_type=None,
            error_message=None,
            fallback_used=True,
        )
    return {
        "output": output,
        "fallback_used": True,
        "llm_calls": [cast(LLMCallRecord, call_record)],
    }


def validate_version_subagent_input(state: VersionSubagentGraphState) -> dict:
    """校验 Version 输入并创建 coordinator 到 Version 的 assignment 消息。

    Args:
        state: 包含待校验 Version 输入、固定 Team 和 LLM 配置的子图状态。

    Returns:
        合法时返回规范化输入和 assignment 消息；非法时返回协议错误。
    """
    try:
        input_data = validate_version_input_protocol(state["input"])
        assignment = create_assignment_message(
            team=state["team"],
            task_id=input_data["task_id"],
            receiver=resolve_fixed_subagent("version").agent_id,
            summary="分配版本差异解释任务，输入仅包含比较结果、信号和受控引用。",
            artifact_refs=input_data["artifact_refs"],
        )
        return {"input": input_data, "team_messages": [assignment]}
    except (KeyError, TypeError, ValueError) as error:
        return {
            "output": None,
            "errors": [
                create_node_error(
                    state,
                    stage="version_subagent",
                    node_name="validate_version_subagent_input",
                    category="protocol",
                    message=str(error)[:MAX_TEAM_MESSAGE_ERROR_CHARACTERS],
                    fatal=False,
                )
            ],
        }


def build_version_subagent_prompt(state: VersionSubagentGraphState) -> dict:
    """把已校验 Version 输入转换为不含完整正文的模型 Prompt。

    Args:
        state: 已通过 Version Team Protocol 输入校验的子图状态。

    Returns:
        固定系统 Prompt 和最小 JSON 用户 Prompt。
    """
    try:
        system_prompt, user_prompt = build_version_subagent_prompts(state["input"])
        skill_blocks: list[str] = []
        for instruction in state.get("skill_context", []):
            if instruction.get("skill_id") != "version-relation":
                raise ValueError("Version Subagent 收到职责外 Skill")
            content = str(instruction.get("content", ""))
            digest = str(instruction.get("content_sha256", ""))
            if not content.strip() or hashlib.sha256(content.encode("utf-8")).hexdigest() != digest:
                raise ValueError("Version Skill 正文为空或摘要不一致")
            skill_blocks.append(
                f"### {instruction['name']} ({instruction['skill_id']})\n{content}"
            )
        if skill_blocks:
            system_prompt = (
                system_prompt
                + "\n\n## 当前 Task 已绑定 Skills\n"
                + "\n\n".join(skill_blocks)
            )
        return {"system_prompt": system_prompt, "user_prompt": user_prompt}
    except (KeyError, TypeError, ValueError) as error:
        return {
            "system_prompt": "",
            "user_prompt": "",
            "output": None,
            "errors": [
                create_node_error(
                    state,
                    stage="version_subagent",
                    node_name="build_version_subagent_prompt",
                    category="protocol",
                    message=str(error)[:MAX_TEAM_MESSAGE_ERROR_CHARACTERS],
                    fatal=False,
                )
            ],
        }


def invoke_version_structured_llm(state: VersionSubagentGraphState) -> dict:
    """使用统一 LLM Client 调用 Version Pydantic 结构化输出。

    Args:
        state: 已生成安全 Prompt 和 assignment 消息的 Version 子图状态。

    Returns:
        可选 Version 输出、必有 LLM 审计以及失败时的非致命错误。
    """
    assignment = next(
        (
            message
            for message in reversed(state.get("team_messages", []))
            if message.get("message_type") == "assignment"
        ),
        None,
    )
    if assignment is None:
        return {
            "output": None,
            "errors": [
                create_node_error(
                    state,
                    stage="version_subagent",
                    node_name="invoke_version_structured_llm",
                    category="protocol",
                    message="调用 Version LLM 前缺少 assignment Team Message",
                    fatal=False,
                )
            ],
        }
    definition = resolve_fixed_subagent("version")
    result = LLMClient(state["llm"]).generate_structured(
        task_id=state["input"]["task_id"],
        agent_id=definition.agent_id,
        message_id=assignment["message_id"],
        system_prompt=state["system_prompt"],
        user_prompt=state["user_prompt"],
        output_model=VersionSubagentOutput,
        model_profile_id=state["selected_model_profile_id"],
    )
    update: dict = {
        "output": cast(VersionSubagentOutput | None, result.output),
        "llm_calls": [result.call_record],
    }
    if result.output is None:
        update["errors"] = [
            create_node_error(
                state,
                stage="version_subagent",
                node_name="invoke_version_structured_llm",
                category="llm",
                message=result.call_record.get("error_message")
                or "Version Subagent 结构化模型调用失败",
                fatal=False,
            )
        ]
    return update


def validate_version_subagent_output(state: VersionSubagentGraphState) -> dict:
    """校验 Version 输出只含摘要和输入白名单中的产物引用。

    Args:
        state: 已取得可选模型输出的 Version 子图状态。

    Returns:
        合法 Pydantic 输出，或清空输出并返回非致命校验错误。
    """
    try:
        output = validate_structured_output(state.get("output"), VersionSubagentOutput)
        validate_output_artifact_refs(output, allowed_refs=state["input"]["artifact_refs"])
        return {"output": output}
    except (KeyError, TypeError, ValueError) as error:
        return {
            "output": None,
            "errors": [
                create_node_error(
                    state,
                    stage="version_subagent",
                    node_name="validate_version_subagent_output",
                    category="protocol",
                    message=str(error)[:MAX_TEAM_MESSAGE_ERROR_CHARACTERS],
                    fatal=False,
                )
            ],
        }


def persist_version_analysis_artifact(state: VersionSubagentGraphState) -> dict:
    """把已验证 Version 输出深复制到可由 Checkpointer 持久化的图状态。

    Args:
        state: 已通过 Pydantic 和引用白名单校验的 Version 子图状态。

    Returns:
        与 Provider 返回对象解除可变引用关系的 Version 输出。
    """
    output = state.get("output")
    if output is None:
        return {
            "errors": [
                create_node_error(
                    state,
                    stage="version_subagent",
                    node_name="persist_version_analysis_artifact",
                    category="protocol",
                    message="没有可固化的 Version Subagent 输出",
                    fatal=False,
                )
            ]
        }
    return {"output": output.model_copy(deep=True)}


def build_version_result_message(state: VersionSubagentGraphState) -> dict:
    """把 Version 成功或失败结果转换为合法 Team Protocol 消息。

    Args:
        state: 即将结束的 Version 子图状态。

    Returns:
        只含摘要和受控引用的 result 消息，或含脱敏错误的 error 消息。
    """
    definition = resolve_fixed_subagent("version")
    raw_task_id = state.get("input", {}).get("task_id")
    task_id = raw_task_id if isinstance(raw_task_id, str) and raw_task_id.strip() else INVALID_PROTOCOL_TASK_ID
    output = state.get("output")
    if output is not None:
        message = create_result_message(
            team=state["team"],
            task_id=task_id,
            sender=definition.agent_id,
            summary=output.summary,
            artifact_refs=output.artifact_refs,
        )
        validate_team_message(
            message,
            team=state["team"],
            allowed_artifact_refs=state["input"]["artifact_refs"],
        )
    else:
        errors = state.get("errors", [])
        error_text = errors[-1]["message"] if errors else "Version Subagent 未产生结果"
        message = create_error_message(
            team=state["team"],
            task_id=task_id,
            sender=definition.agent_id,
            summary="Version Subagent 执行失败，已返回协调 Agent。",
            error=error_text[:MAX_TEAM_MESSAGE_ERROR_CHARACTERS],
        )
    return {"team_messages": [message]}


def build_deterministic_version_fallback(state: VersionSubagentGraphState) -> dict:
    """在 Version 模型失败或输出无效时生成确定性回退结果。

    Args:
        state: 允许回退且包含已校验最小输入的 Version 子图状态。

    Returns:
        确定性 Pydantic 输出、回退标记和更新后的 LLM 审计。
    """
    output = build_deterministic_version_output(state["input"])
    validate_output_artifact_refs(output, allowed_refs=state["input"]["artifact_refs"])
    llm_calls = state.get("llm_calls", [])
    if llm_calls:
        call_record = dict(llm_calls[-1])
        call_record["status"] = "fallback"
        call_record["fallback_used"] = True
    else:
        assignment = next(
            (
                message
                for message in reversed(state.get("team_messages", []))
                if message.get("message_type") == "assignment"
            ),
            None,
        )
        timestamp = utc_now_iso()
        task_id = state["input"]["task_id"]
        agent_id = resolve_fixed_subagent("version").agent_id
        call_record = LLMCallRecord(
            id="llm-fallback-" + hashlib.sha256(f"{task_id}:{agent_id}".encode()).hexdigest(),
            task_id=task_id,
            agent_id=agent_id,
            message_id=assignment["message_id"] if assignment else "missing-assignment",
            model_profile_id="deterministic-version-fallback",
            provider="deterministic",
            model="deterministic-version-fallback",
            status="fallback",
            started_at=timestamp,
            finished_at=timestamp,
            duration_ms=0,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            error_type=None,
            error_message=None,
            fallback_used=True,
        )
    return {
        "output": output,
        "fallback_used": True,
        "llm_calls": [cast(LLMCallRecord, call_record)],
    }


def validate_evidence_subagent_input(state: EvidenceSubagentGraphState) -> dict:
    """校验 Evidence 输入并创建 coordinator 到 Evidence 的 assignment 消息。

    Args:
        state: 包含待校验 Evidence 输入、固定 Team 和 LLM 配置的子图状态。

    Returns:
        合法时返回规范化输入和 assignment 消息；非法时返回协议错误。
    """
    try:
        input_data = validate_evidence_input_protocol(state["input"])
        assignment = create_assignment_message(
            team=state["team"],
            task_id=input_data["task_id"],
            receiver=resolve_fixed_subagent("evidence").agent_id,
            summary="分配外部证据解释任务，输入仅包含 PDF、发送摘要和受控引用。",
            artifact_refs=input_data["artifact_refs"],
        )
        return {"input": input_data, "team_messages": [assignment]}
    except (KeyError, TypeError, ValueError) as error:
        return {
            "output": None,
            "errors": [
                create_node_error(
                    state,
                    stage="evidence_subagent",
                    node_name="validate_evidence_subagent_input",
                    category="protocol",
                    message=str(error)[:MAX_TEAM_MESSAGE_ERROR_CHARACTERS],
                    fatal=False,
                )
            ],
        }


def build_evidence_subagent_prompt(state: EvidenceSubagentGraphState) -> dict:
    """把已校验 Evidence 输入转换为不含完整正文的模型 Prompt。

    Args:
        state: 已通过 Evidence Team Protocol 输入校验的子图状态。

    Returns:
        固定系统 Prompt 和最小 JSON 用户 Prompt。
    """
    try:
        system_prompt, user_prompt = build_evidence_subagent_prompts(state["input"])
        skill_blocks: list[str] = []
        for instruction in state.get("skill_context", []):
            if instruction.get("skill_id") != "evidence-confidence":
                raise ValueError("Evidence Subagent 收到职责外 Skill")
            content = str(instruction.get("content", ""))
            digest = str(instruction.get("content_sha256", ""))
            if not content.strip() or hashlib.sha256(content.encode("utf-8")).hexdigest() != digest:
                raise ValueError("Evidence Skill 正文为空或摘要不一致")
            skill_blocks.append(
                f"### {instruction['name']} ({instruction['skill_id']})\n{content}"
            )
        if skill_blocks:
            system_prompt = (
                system_prompt
                + "\n\n## 当前 Task 已绑定 Skills\n"
                + "\n\n".join(skill_blocks)
            )
        return {"system_prompt": system_prompt, "user_prompt": user_prompt}
    except (KeyError, TypeError, ValueError) as error:
        return {
            "system_prompt": "",
            "user_prompt": "",
            "output": None,
            "errors": [
                create_node_error(
                    state,
                    stage="evidence_subagent",
                    node_name="build_evidence_subagent_prompt",
                    category="protocol",
                    message=str(error)[:MAX_TEAM_MESSAGE_ERROR_CHARACTERS],
                    fatal=False,
                )
            ],
        }


def invoke_evidence_structured_llm(state: EvidenceSubagentGraphState) -> dict:
    """使用统一 LLM Client 调用 Evidence Pydantic 结构化输出。

    Args:
        state: 已生成安全 Prompt 和 assignment 消息的 Evidence 子图状态。

    Returns:
        可选 Evidence 输出、必有 LLM 审计以及失败时的非致命错误。
    """
    assignment = next(
        (
            message
            for message in reversed(state.get("team_messages", []))
            if message.get("message_type") == "assignment"
        ),
        None,
    )
    if assignment is None:
        return {
            "output": None,
            "errors": [
                create_node_error(
                    state,
                    stage="evidence_subagent",
                    node_name="invoke_evidence_structured_llm",
                    category="protocol",
                    message="调用 Evidence LLM 前缺少 assignment Team Message",
                    fatal=False,
                )
            ],
        }
    definition = resolve_fixed_subagent("evidence")
    result = LLMClient(state["llm"]).generate_structured(
        task_id=state["input"]["task_id"],
        agent_id=definition.agent_id,
        message_id=assignment["message_id"],
        system_prompt=state["system_prompt"],
        user_prompt=state["user_prompt"],
        output_model=EvidenceSubagentOutput,
        model_profile_id=state["selected_model_profile_id"],
    )
    update: dict = {
        "output": cast(EvidenceSubagentOutput | None, result.output),
        "llm_calls": [result.call_record],
    }
    if result.output is None:
        update["errors"] = [
            create_node_error(
                state,
                stage="evidence_subagent",
                node_name="invoke_evidence_structured_llm",
                category="llm",
                message=result.call_record.get("error_message")
                or "Evidence Subagent 结构化模型调用失败",
                fatal=False,
            )
        ]
    return update


def validate_evidence_subagent_output(state: EvidenceSubagentGraphState) -> dict:
    """校验 Evidence 输出只含摘要和输入白名单中的产物引用。

    Args:
        state: 已取得可选模型输出的 Evidence 子图状态。

    Returns:
        合法 Pydantic 输出，或清空输出并返回非致命校验错误。
    """
    try:
        output = validate_structured_output(state.get("output"), EvidenceSubagentOutput)
        validate_output_artifact_refs(output, allowed_refs=state["input"]["artifact_refs"])
        return {"output": output}
    except (KeyError, TypeError, ValueError) as error:
        return {
            "output": None,
            "errors": [
                create_node_error(
                    state,
                    stage="evidence_subagent",
                    node_name="validate_evidence_subagent_output",
                    category="protocol",
                    message=str(error)[:MAX_TEAM_MESSAGE_ERROR_CHARACTERS],
                    fatal=False,
                )
            ],
        }


def persist_evidence_analysis_artifact(state: EvidenceSubagentGraphState) -> dict:
    """把已验证 Evidence 输出深复制到可由 Checkpointer 持久化的图状态。

    Args:
        state: 已通过 Pydantic 和引用白名单校验的 Evidence 子图状态。

    Returns:
        与 Provider 返回对象解除可变引用关系的 Evidence 输出。
    """
    output = state.get("output")
    if output is None:
        return {
            "errors": [
                create_node_error(
                    state,
                    stage="evidence_subagent",
                    node_name="persist_evidence_analysis_artifact",
                    category="protocol",
                    message="没有可固化的 Evidence Subagent 输出",
                    fatal=False,
                )
            ]
        }
    return {"output": output.model_copy(deep=True)}


def build_evidence_result_message(state: EvidenceSubagentGraphState) -> dict:
    """把 Evidence 成功或失败结果转换为合法 Team Protocol 消息。

    Args:
        state: 即将结束的 Evidence 子图状态。

    Returns:
        只含摘要和受控引用的 result 消息，或含脱敏错误的 error 消息。
    """
    definition = resolve_fixed_subagent("evidence")
    raw_task_id = state.get("input", {}).get("task_id")
    task_id = raw_task_id if isinstance(raw_task_id, str) and raw_task_id.strip() else INVALID_PROTOCOL_TASK_ID
    output = state.get("output")
    if output is not None:
        message = create_result_message(
            team=state["team"],
            task_id=task_id,
            sender=definition.agent_id,
            summary=output.summary,
            artifact_refs=output.artifact_refs,
        )
        validate_team_message(
            message,
            team=state["team"],
            allowed_artifact_refs=state["input"]["artifact_refs"],
        )
    else:
        errors = state.get("errors", [])
        error_text = errors[-1]["message"] if errors else "Evidence Subagent 未产生结果"
        message = create_error_message(
            team=state["team"],
            task_id=task_id,
            sender=definition.agent_id,
            summary="Evidence Subagent 执行失败，已返回协调 Agent。",
            error=error_text[:MAX_TEAM_MESSAGE_ERROR_CHARACTERS],
        )
    return {"team_messages": [message]}


def build_deterministic_evidence_fallback(state: EvidenceSubagentGraphState) -> dict:
    """在 Evidence 模型失败或输出无效时生成确定性回退结果。

    Args:
        state: 允许回退且包含已校验最小输入的 Evidence 子图状态。

    Returns:
        确定性 Pydantic 输出、回退标记和更新后的 LLM 审计。
    """
    output = build_deterministic_evidence_output(state["input"])
    validate_output_artifact_refs(output, allowed_refs=state["input"]["artifact_refs"])
    llm_calls = state.get("llm_calls", [])
    if llm_calls:
        call_record = dict(llm_calls[-1])
        call_record["status"] = "fallback"
        call_record["fallback_used"] = True
    else:
        assignment = next(
            (
                message
                for message in reversed(state.get("team_messages", []))
                if message.get("message_type") == "assignment"
            ),
            None,
        )
        timestamp = utc_now_iso()
        task_id = state["input"]["task_id"]
        agent_id = resolve_fixed_subagent("evidence").agent_id
        call_record = LLMCallRecord(
            id="llm-fallback-" + hashlib.sha256(f"{task_id}:{agent_id}".encode()).hexdigest(),
            task_id=task_id,
            agent_id=agent_id,
            message_id=assignment["message_id"] if assignment else "missing-assignment",
            model_profile_id="deterministic-evidence-fallback",
            provider="deterministic",
            model="deterministic-evidence-fallback",
            status="fallback",
            started_at=timestamp,
            finished_at=timestamp,
            duration_ms=0,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            error_type=None,
            error_message=None,
            fallback_used=True,
        )
    return {
        "output": output,
        "fallback_used": True,
        "llm_calls": [cast(LLMCallRecord, call_record)],
    }
