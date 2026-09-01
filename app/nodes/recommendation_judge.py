from __future__ import annotations

import hashlib
from typing import cast

from app.agents.protocol import (
    MAX_TEAM_MESSAGE_ERROR_CHARACTERS,
    create_assignment_message,
    create_error_message,
    create_result_message,
    validate_team_message,
)
from app.agents.protocol import (
    validate_recommendation_judge_input as validate_judge_input_protocol,
)
from app.agents.recommendation_judge import (
    build_deterministic_recommendation_judge_output,
    build_recommendation_judge_assignment_summary,
    build_recommendation_judge_prompts,
)
from app.agents.registry import resolve_fixed_subagent
from app.llm.client import LLMClient
from app.llm.schemas import validate_output_artifact_refs, validate_structured_output
from app.services.recommendation_judge import (
    validate_recommendation_judge_output as validate_judge_business_output,
)
from app.state.models import (
    LLMCallRecord,
    RecommendationJudgeGraphState,
    RecommendationJudgeOutput,
)
from app.utils.error_context import create_node_error
from app.utils.runtime import utc_now_iso

"""本模块只实现 Recommendation Judge 子图显式注册的输入、模型、校验和回退节点。"""


def validate_recommendation_judge_input(state: RecommendationJudgeGraphState) -> dict:
    """校验 Judge 决策包并创建 coordinator 到 Judge 的 assignment 消息。

    Args:
        state: 包含待校验决策包、固定 Team 和 LLM 配置的子图状态。

    Returns:
        合法时返回规范化输入与 assignment；非法时返回非致命协议错误。
    """
    try:
        input_data = validate_judge_input_protocol(state["input"])
        assignment = create_assignment_message(
            team=state["team"],
            task_id=input_data["task_id"],
            receiver=resolve_fixed_subagent("recommendation_judge").agent_id,
            summary=build_recommendation_judge_assignment_summary(input_data),
            artifact_refs=input_data["artifact_refs"],
        )
        return {"input": input_data, "team_messages": [assignment]}
    except (KeyError, TypeError, ValueError) as error:
        return {
            "output": None,
            "errors": [
                create_node_error(
                    state,
                    stage="recommendation_judge_subagent",
                    node_name="validate_recommendation_judge_input",
                    category="protocol",
                    message=str(error)[:MAX_TEAM_MESSAGE_ERROR_CHARACTERS],
                    fatal=False,
                )
            ],
        }


def build_recommendation_judge_prompt(state: RecommendationJudgeGraphState) -> dict:
    """把已校验决策包转换为不含完整文档的 Judge Prompt。

    Args:
        state: 已通过 Recommendation Judge 输入协议校验的子图状态。

    Returns:
        固定系统 Prompt 和压缩 JSON 用户 Prompt。
    """
    try:
        system_prompt, user_prompt = build_recommendation_judge_prompts(state["input"])
        skill_blocks: list[str] = []
        for instruction in state.get("skill_context", []):
            if instruction.get("skill_id") != "recommendation-second-opinion":
                raise ValueError("Recommendation Judge 收到职责外 Skill")
            content = str(instruction.get("content", ""))
            digest = str(instruction.get("content_sha256", ""))
            if not content.strip() or hashlib.sha256(content.encode("utf-8")).hexdigest() != digest:
                raise ValueError("Recommendation Judge Skill 正文为空或摘要不一致")
            skill_blocks.append(f"### {instruction['name']} ({instruction['skill_id']})\n{content}")
        if skill_blocks:
            system_prompt = (
                system_prompt + "\n\n## 当前 Task 已绑定 Skills\n" + "\n\n".join(skill_blocks)
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
                    stage="recommendation_judge_subagent",
                    node_name="build_recommendation_judge_prompt",
                    category="protocol",
                    message=str(error)[:MAX_TEAM_MESSAGE_ERROR_CHARACTERS],
                    fatal=False,
                )
            ],
        }


def invoke_recommendation_judge_structured_llm(
    state: RecommendationJudgeGraphState,
) -> dict:
    """使用统一 LLM Client 调用 Recommendation Judge Pydantic 结构化输出。

    Args:
        state: 已生成安全 Prompt 和 assignment 消息的 Judge 子图状态。

    Returns:
        可选 Judge 输出、必有 LLM 审计和调用失败时的非致命错误。
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
                    stage="recommendation_judge_subagent",
                    node_name="invoke_recommendation_judge_structured_llm",
                    category="protocol",
                    message="调用 Judge LLM 前缺少 assignment Team Message",
                    fatal=False,
                )
            ],
        }
    definition = resolve_fixed_subagent("recommendation_judge")
    result = LLMClient(state["llm"]).generate_structured(
        task_id=state["input"]["task_id"],
        agent_id=definition.agent_id,
        message_id=assignment["message_id"],
        system_prompt=state["system_prompt"],
        user_prompt=state["user_prompt"],
        output_model=RecommendationJudgeOutput,
        model_profile_id=state["selected_model_profile_id"],
    )
    update: dict = {
        "output": cast(RecommendationJudgeOutput | None, result.output),
        "llm_calls": [result.call_record],
    }
    if result.output is None:
        update["errors"] = [
            create_node_error(
                state,
                stage="recommendation_judge_subagent",
                node_name="invoke_recommendation_judge_structured_llm",
                category="llm",
                message=result.call_record.get("error_message")
                or "Recommendation Judge 结构化模型调用失败",
                fatal=False,
            )
        ]
    return update


def validate_recommendation_judge_output(state: RecommendationJudgeGraphState) -> dict:
    """校验 Judge 输出的结构、弃权协议、候选和引用白名单。

    Args:
        state: 已取得可选模型输出的 Judge 子图状态。

    Returns:
        合法输出，或清空输出并返回非致命协议错误。
    """
    try:
        output = validate_structured_output(
            state.get("output"),
            RecommendationJudgeOutput,
        )
        validate_output_artifact_refs(output, allowed_refs=state["input"]["artifact_refs"])
        output = validate_judge_business_output(state["input"], output)
        return {"output": output}
    except (KeyError, TypeError, ValueError) as error:
        return {
            "output": None,
            "errors": [
                create_node_error(
                    state,
                    stage="recommendation_judge_subagent",
                    node_name="validate_recommendation_judge_output",
                    category="protocol",
                    message=str(error)[:MAX_TEAM_MESSAGE_ERROR_CHARACTERS],
                    fatal=False,
                )
            ],
        }


def persist_recommendation_judge_output(state: RecommendationJudgeGraphState) -> dict:
    """把已校验 Judge 输出深复制到可由 Checkpointer 持久化的子图状态。

    Args:
        state: 已通过 Pydantic、候选和引用白名单校验的 Judge 状态。

    Returns:
        与 Provider 返回对象解除可变引用的 Judge 输出。
    """
    output = state.get("output")
    if output is None:
        return {
            "errors": [
                create_node_error(
                    state,
                    stage="recommendation_judge_subagent",
                    node_name="persist_recommendation_judge_output",
                    category="protocol",
                    message="没有可固化的 Recommendation Judge 输出",
                    fatal=False,
                )
            ]
        }
    return {"output": output.model_copy(deep=True)}


def build_recommendation_judge_result_message(
    state: RecommendationJudgeGraphState,
) -> dict:
    """把 Judge 成功或失败结果转换为合法 Team Protocol 消息。

    Args:
        state: 即将结束的 Recommendation Judge 子图状态。

    Returns:
        只含摘要和受控引用的 result 消息，或含脱敏错误的 error 消息。
    """
    definition = resolve_fixed_subagent("recommendation_judge")
    raw_task_id = state.get("input", {}).get("task_id")
    task_id = (
        raw_task_id
        if isinstance(raw_task_id, str) and raw_task_id.strip()
        else "protocol-invalid-task"
    )
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
        error_text = errors[-1]["message"] if errors else "Recommendation Judge 未产生结果"
        message = create_error_message(
            team=state["team"],
            task_id=task_id,
            sender=definition.agent_id,
            summary="Recommendation Judge 执行失败，已返回协调 Agent。",
            error=error_text[:MAX_TEAM_MESSAGE_ERROR_CHARACTERS],
        )
    return {"team_messages": [message]}


def build_deterministic_recommendation_judge_fallback(
    state: RecommendationJudgeGraphState,
) -> dict:
    """在 Judge 模型失败或输出无效时生成确定性弃权回退。

    Args:
        state: 允许回退且包含已校验压缩决策包的 Judge 子图状态。

    Returns:
        弃权 Pydantic 输出、回退标记和更新后的 LLM 审计。
    """
    output = build_deterministic_recommendation_judge_output(state["input"])
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
        agent_id = resolve_fixed_subagent("recommendation_judge").agent_id
        call_record = LLMCallRecord(
            id="llm-fallback-" + hashlib.sha256(f"{task_id}:{agent_id}".encode()).hexdigest(),
            task_id=task_id,
            agent_id=agent_id,
            message_id=assignment["message_id"] if assignment else "missing-assignment",
            model_profile_id="deterministic-recommendation-judge-fallback",
            provider="deterministic",
            model="deterministic-recommendation-judge-fallback",
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
