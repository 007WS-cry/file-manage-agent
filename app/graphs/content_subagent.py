from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graphs.routers import (
    route_subagent_input_validation,
    route_subagent_llm_result,
    route_subagent_output_validation,
    route_subagent_prompt_validation,
)
from app.nodes.subagents import (
    build_content_result_message,
    build_content_subagent_prompt,
    build_deterministic_content_fallback,
    execute_after_model_hooks,
    execute_before_model_hooks,
    invoke_content_structured_llm,
    persist_content_analysis_artifact,
    resolve_model_profile,
    validate_content_subagent_input,
    validate_content_subagent_output,
)
from app.state.models import ContentSubagentGraphState

"""本模块构建固定 Content Subagent 的输入校验、结构化调用和协议返回子图。"""


def build_content_subagent_graph():
    """构建带条件路由和确定性回退的 Content Subagent 子图。

    Returns:
        已编译、只接收短预览和受控引用的 Content Subagent LangGraph。
    """
    builder = StateGraph(ContentSubagentGraphState)
    builder.add_node("validate_content_subagent_input", validate_content_subagent_input)
    builder.add_node("resolve_model_profile", resolve_model_profile)
    builder.add_node("build_content_subagent_prompt", build_content_subagent_prompt)
    builder.add_node("execute_before_model_hooks", execute_before_model_hooks)
    builder.add_node("invoke_content_structured_llm", invoke_content_structured_llm)
    builder.add_node("execute_after_model_hooks", execute_after_model_hooks)
    builder.add_node("validate_content_subagent_output", validate_content_subagent_output)
    builder.add_node("persist_content_analysis_artifact", persist_content_analysis_artifact)
    builder.add_node("build_content_result_message", build_content_result_message)
    builder.add_node(
        "build_deterministic_content_fallback",
        build_deterministic_content_fallback,
    )

    builder.add_edge(START, "validate_content_subagent_input")
    builder.add_conditional_edges(
        "validate_content_subagent_input",
        route_subagent_input_validation,
        {
            "valid": "resolve_model_profile",
            "invalid": "build_content_result_message",
        },
    )
    builder.add_edge("resolve_model_profile", "build_content_subagent_prompt")
    builder.add_edge("build_content_subagent_prompt", "execute_before_model_hooks")
    builder.add_conditional_edges(
        "execute_before_model_hooks",
        route_subagent_prompt_validation,
        {
            "invoke": "invoke_content_structured_llm",
            "error": "build_content_result_message",
        },
    )
    builder.add_edge("invoke_content_structured_llm", "execute_after_model_hooks")
    builder.add_conditional_edges(
        "execute_after_model_hooks",
        route_subagent_llm_result,
        {
            "validate": "validate_content_subagent_output",
            "fallback": "build_deterministic_content_fallback",
            "error": "build_content_result_message",
        },
    )
    builder.add_conditional_edges(
        "validate_content_subagent_output",
        route_subagent_output_validation,
        {
            "persist": "persist_content_analysis_artifact",
            "fallback": "build_deterministic_content_fallback",
            "error": "build_content_result_message",
        },
    )
    builder.add_edge("persist_content_analysis_artifact", "build_content_result_message")
    builder.add_edge("build_deterministic_content_fallback", "build_content_result_message")
    builder.add_edge("build_content_result_message", END)
    return builder.compile()


# 已编译的 Content Subagent 子图，供后续 Team Orchestration 包装节点调用。
content_subagent_graph = build_content_subagent_graph()
