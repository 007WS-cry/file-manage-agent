from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graphs.routers import (
    route_subagent_input_validation,
    route_subagent_llm_result,
    route_subagent_output_validation,
    route_subagent_prompt_validation,
)
from app.nodes.recommendation_judge import (
    build_deterministic_recommendation_judge_fallback,
    build_recommendation_judge_prompt,
    build_recommendation_judge_result_message,
    invoke_recommendation_judge_structured_llm,
    persist_recommendation_judge_output,
    validate_recommendation_judge_input,
    validate_recommendation_judge_output,
)
from app.nodes.subagents import (
    execute_after_model_hooks,
    execute_before_model_hooks,
    resolve_model_profile,
)
from app.state.models import RecommendationJudgeGraphState

"""本模块构建固定 Recommendation Judge Subagent 的第二意见、输出校验和弃权回退子图。"""


def build_recommendation_judge_graph():
    """构建带候选白名单、弃权协议和确定性回退的 Judge 子图。

    Returns:
        已编译、只接收压缩决策包的 Recommendation Judge LangGraph。
    """
    builder = StateGraph(RecommendationJudgeGraphState)
    builder.add_node(
        "validate_recommendation_judge_input",
        validate_recommendation_judge_input,
    )
    builder.add_node("resolve_model_profile", resolve_model_profile)
    builder.add_node(
        "build_recommendation_judge_prompt",
        build_recommendation_judge_prompt,
    )
    builder.add_node("execute_before_model_hooks", execute_before_model_hooks)
    builder.add_node(
        "invoke_recommendation_judge_structured_llm",
        invoke_recommendation_judge_structured_llm,
    )
    builder.add_node("execute_after_model_hooks", execute_after_model_hooks)
    builder.add_node(
        "validate_recommendation_judge_output",
        validate_recommendation_judge_output,
    )
    builder.add_node(
        "persist_recommendation_judge_output",
        persist_recommendation_judge_output,
    )
    builder.add_node(
        "build_recommendation_judge_result_message",
        build_recommendation_judge_result_message,
    )
    builder.add_node(
        "build_deterministic_recommendation_judge_fallback",
        build_deterministic_recommendation_judge_fallback,
    )

    builder.add_edge(START, "validate_recommendation_judge_input")
    builder.add_conditional_edges(
        "validate_recommendation_judge_input",
        route_subagent_input_validation,
        {
            "valid": "resolve_model_profile",
            "invalid": "build_recommendation_judge_result_message",
        },
    )
    builder.add_edge("resolve_model_profile", "build_recommendation_judge_prompt")
    builder.add_edge("build_recommendation_judge_prompt", "execute_before_model_hooks")
    builder.add_conditional_edges(
        "execute_before_model_hooks",
        route_subagent_prompt_validation,
        {
            "invoke": "invoke_recommendation_judge_structured_llm",
            "error": "build_recommendation_judge_result_message",
        },
    )
    builder.add_edge(
        "invoke_recommendation_judge_structured_llm",
        "execute_after_model_hooks",
    )
    builder.add_conditional_edges(
        "execute_after_model_hooks",
        route_subagent_llm_result,
        {
            "validate": "validate_recommendation_judge_output",
            "fallback": "build_deterministic_recommendation_judge_fallback",
            "error": "build_recommendation_judge_result_message",
        },
    )
    builder.add_conditional_edges(
        "validate_recommendation_judge_output",
        route_subagent_output_validation,
        {
            "persist": "persist_recommendation_judge_output",
            "fallback": "build_deterministic_recommendation_judge_fallback",
            "error": "build_recommendation_judge_result_message",
        },
    )
    builder.add_edge(
        "persist_recommendation_judge_output",
        "build_recommendation_judge_result_message",
    )
    builder.add_edge(
        "build_deterministic_recommendation_judge_fallback",
        "build_recommendation_judge_result_message",
    )
    builder.add_edge("build_recommendation_judge_result_message", END)
    return builder.compile()


# 已编译的 Recommendation Judge 子图，供 Team Orchestration 同步调用。
recommendation_judge_graph = build_recommendation_judge_graph()
