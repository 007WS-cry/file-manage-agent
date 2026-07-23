from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graphs.routers import (
    route_subagent_input_validation,
    route_subagent_llm_result,
    route_subagent_output_validation,
    route_subagent_prompt_validation,
)
from app.nodes.subagents import (
    build_deterministic_version_fallback,
    build_version_result_message,
    build_version_subagent_prompt,
    execute_after_model_hooks,
    execute_before_model_hooks,
    invoke_version_structured_llm,
    persist_version_analysis_artifact,
    resolve_model_profile,
    validate_version_subagent_input,
    validate_version_subagent_output,
)
from app.state.models import VersionSubagentGraphState

"""本模块构建固定 Version Subagent 的差异解释、输出校验和协议返回子图。"""


def build_version_subagent_graph():
    """构建带条件路由和确定性回退的 Version Subagent 子图。

    Returns:
        已编译、只接收确定性比较摘要和受控引用的 Version Subagent LangGraph。
    """
    builder = StateGraph(VersionSubagentGraphState)
    builder.add_node("validate_version_subagent_input", validate_version_subagent_input)
    builder.add_node("resolve_model_profile", resolve_model_profile)
    builder.add_node("build_version_subagent_prompt", build_version_subagent_prompt)
    builder.add_node("execute_before_model_hooks", execute_before_model_hooks)
    builder.add_node("invoke_version_structured_llm", invoke_version_structured_llm)
    builder.add_node("execute_after_model_hooks", execute_after_model_hooks)
    builder.add_node("validate_version_subagent_output", validate_version_subagent_output)
    builder.add_node("persist_version_analysis_artifact", persist_version_analysis_artifact)
    builder.add_node("build_version_result_message", build_version_result_message)
    builder.add_node(
        "build_deterministic_version_fallback",
        build_deterministic_version_fallback,
    )

    builder.add_edge(START, "validate_version_subagent_input")
    builder.add_conditional_edges(
        "validate_version_subagent_input",
        route_subagent_input_validation,
        {
            "valid": "resolve_model_profile",
            "invalid": "build_version_result_message",
        },
    )
    builder.add_edge("resolve_model_profile", "build_version_subagent_prompt")
    builder.add_edge("build_version_subagent_prompt", "execute_before_model_hooks")
    builder.add_conditional_edges(
        "execute_before_model_hooks",
        route_subagent_prompt_validation,
        {
            "invoke": "invoke_version_structured_llm",
            "error": "build_version_result_message",
        },
    )
    builder.add_edge("invoke_version_structured_llm", "execute_after_model_hooks")
    builder.add_conditional_edges(
        "execute_after_model_hooks",
        route_subagent_llm_result,
        {
            "validate": "validate_version_subagent_output",
            "fallback": "build_deterministic_version_fallback",
            "error": "build_version_result_message",
        },
    )
    builder.add_conditional_edges(
        "validate_version_subagent_output",
        route_subagent_output_validation,
        {
            "persist": "persist_version_analysis_artifact",
            "fallback": "build_deterministic_version_fallback",
            "error": "build_version_result_message",
        },
    )
    builder.add_edge("persist_version_analysis_artifact", "build_version_result_message")
    builder.add_edge("build_deterministic_version_fallback", "build_version_result_message")
    builder.add_edge("build_version_result_message", END)
    return builder.compile()


# 已编译的 Version Subagent 子图，供后续 Version Analysis 包装节点调用。
version_subagent_graph = build_version_subagent_graph()
