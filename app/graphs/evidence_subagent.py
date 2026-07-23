from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graphs.routers import (
    route_subagent_input_validation,
    route_subagent_llm_result,
    route_subagent_output_validation,
    route_subagent_prompt_validation,
)
from app.nodes.subagents import (
    build_deterministic_evidence_fallback,
    build_evidence_result_message,
    build_evidence_subagent_prompt,
    execute_after_model_hooks,
    execute_before_model_hooks,
    invoke_evidence_structured_llm,
    persist_evidence_analysis_artifact,
    resolve_model_profile,
    validate_evidence_subagent_input,
    validate_evidence_subagent_output,
)
from app.state.models import EvidenceSubagentGraphState

"""本模块构建固定 Evidence Subagent 的证据解释、输出校验和协议返回子图。"""


def build_evidence_subagent_graph():
    """构建带条件路由和确定性回退的 Evidence Subagent 子图。

    Returns:
        已编译、只接收证据摘要和受控引用的 Evidence Subagent LangGraph。
    """
    builder = StateGraph(EvidenceSubagentGraphState)
    builder.add_node("validate_evidence_subagent_input", validate_evidence_subagent_input)
    builder.add_node("resolve_model_profile", resolve_model_profile)
    builder.add_node("build_evidence_subagent_prompt", build_evidence_subagent_prompt)
    builder.add_node("execute_before_model_hooks", execute_before_model_hooks)
    builder.add_node("invoke_evidence_structured_llm", invoke_evidence_structured_llm)
    builder.add_node("execute_after_model_hooks", execute_after_model_hooks)
    builder.add_node("validate_evidence_subagent_output", validate_evidence_subagent_output)
    builder.add_node("persist_evidence_analysis_artifact", persist_evidence_analysis_artifact)
    builder.add_node("build_evidence_result_message", build_evidence_result_message)
    builder.add_node(
        "build_deterministic_evidence_fallback",
        build_deterministic_evidence_fallback,
    )

    builder.add_edge(START, "validate_evidence_subagent_input")
    builder.add_conditional_edges(
        "validate_evidence_subagent_input",
        route_subagent_input_validation,
        {
            "valid": "resolve_model_profile",
            "invalid": "build_evidence_result_message",
        },
    )
    builder.add_edge("resolve_model_profile", "build_evidence_subagent_prompt")
    builder.add_edge("build_evidence_subagent_prompt", "execute_before_model_hooks")
    builder.add_conditional_edges(
        "execute_before_model_hooks",
        route_subagent_prompt_validation,
        {
            "invoke": "invoke_evidence_structured_llm",
            "error": "build_evidence_result_message",
        },
    )
    builder.add_edge("invoke_evidence_structured_llm", "execute_after_model_hooks")
    builder.add_conditional_edges(
        "execute_after_model_hooks",
        route_subagent_llm_result,
        {
            "validate": "validate_evidence_subagent_output",
            "fallback": "build_deterministic_evidence_fallback",
            "error": "build_evidence_result_message",
        },
    )
    builder.add_conditional_edges(
        "validate_evidence_subagent_output",
        route_subagent_output_validation,
        {
            "persist": "persist_evidence_analysis_artifact",
            "fallback": "build_deterministic_evidence_fallback",
            "error": "build_evidence_result_message",
        },
    )
    builder.add_edge("persist_evidence_analysis_artifact", "build_evidence_result_message")
    builder.add_edge("build_deterministic_evidence_fallback", "build_evidence_result_message")
    builder.add_edge("build_evidence_result_message", END)
    return builder.compile()


# 已编译的 Evidence Subagent 子图，供后续 Evidence 包装节点调用。
evidence_subagent_graph = build_evidence_subagent_graph()
