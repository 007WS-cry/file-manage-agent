from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graphs.routers import (
    dispatch_pdf_match_jobs,
    has_pdf_match_jobs,
    route_email_evidence_source,
)
from app.nodes.evidence import (
    collect_pdf_candidates,
    create_pdf_match_jobs,
    fanout_pdf_matching,
    join_pdf_matches,
    load_email_mcp_evidence,
    load_local_delivery_log,
    match_delivery_to_version,
    match_email_delivery_to_version,
    match_pdf_to_source_version,
    merge_external_evidence,
    validate_evidence_confidence,
)
from app.nodes.memory import capture_evidence_memory
from app.state.models import EvidenceGraphState

"""本模块构建包含邮件 MCP 优先与本地降级分支的独立 Evidence 子图。"""


def build_evidence_graph():
    """构建 PDF 来源、邮件 MCP 与本地发送记录匹配子图。

    PDF 任务存在时先进入 fan-out 节点，再使用 ``Send`` 为每项任务创建隔离
    Worker；全部 Worker 输出汇合后优先调用邮件 MCP，只有关闭或失败时才读取
    本地发送日志。没有 PDF 时直接跳过并行阶段。

    Returns:
        已编译、可独立调用且不带 Checkpointer 的 Evidence LangGraph。
    """
    builder = StateGraph(EvidenceGraphState)
    builder.add_node("collect_pdf_candidates", collect_pdf_candidates)
    builder.add_node("create_pdf_match_jobs", create_pdf_match_jobs)
    builder.add_node("fanout_pdf_matching", fanout_pdf_matching)
    builder.add_node("match_pdf_to_source_version", match_pdf_to_source_version)
    builder.add_node("join_pdf_matches", join_pdf_matches)
    builder.add_node("load_email_mcp_evidence", load_email_mcp_evidence)
    builder.add_node("load_local_delivery_log", load_local_delivery_log)
    builder.add_node("match_delivery_to_version", match_delivery_to_version)
    builder.add_node(
        "match_email_delivery_to_version",
        match_email_delivery_to_version,
    )
    builder.add_node("merge_external_evidence", merge_external_evidence)
    builder.add_node("validate_evidence_confidence", validate_evidence_confidence)
    builder.add_node("capture_evidence_memory", capture_evidence_memory)

    builder.add_edge(START, "collect_pdf_candidates")
    builder.add_edge("collect_pdf_candidates", "create_pdf_match_jobs")
    builder.add_conditional_edges(
        "create_pdf_match_jobs",
        has_pdf_match_jobs,
        {
            "pdf_match": "fanout_pdf_matching",
            "done": "load_email_mcp_evidence",
        },
    )
    builder.add_conditional_edges(
        "fanout_pdf_matching",
        dispatch_pdf_match_jobs,
        ["match_pdf_to_source_version"],
    )
    builder.add_edge("match_pdf_to_source_version", "join_pdf_matches")
    builder.add_edge("join_pdf_matches", "load_email_mcp_evidence")
    builder.add_conditional_edges(
        "load_email_mcp_evidence",
        route_email_evidence_source,
        {
            "mcp": "match_email_delivery_to_version",
            "local": "load_local_delivery_log",
        },
    )
    builder.add_edge("match_email_delivery_to_version", "merge_external_evidence")
    builder.add_edge("load_local_delivery_log", "match_delivery_to_version")
    builder.add_edge("match_delivery_to_version", "merge_external_evidence")
    builder.add_edge("merge_external_evidence", "validate_evidence_confidence")
    builder.add_edge("validate_evidence_confidence", "capture_evidence_memory")
    builder.add_edge("capture_evidence_memory", END)
    return builder.compile()


# 已编译的独立 Evidence 子图，包含受控的证据 Memory 捕获节点。
evidence_graph = build_evidence_graph()
