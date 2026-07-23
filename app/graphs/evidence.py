from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graphs.routers import dispatch_pdf_match_jobs, has_pdf_match_jobs
from app.nodes.evidence import (
    collect_pdf_candidates,
    create_pdf_match_jobs,
    fanout_pdf_matching,
    join_pdf_matches,
    load_local_delivery_log,
    match_delivery_to_version,
    match_pdf_to_source_version,
    merge_external_evidence,
    validate_evidence_confidence,
)
from app.nodes.memory import capture_evidence_memory
from app.state.models import EvidenceGraphState

"""本模块构建带 START、END 和 Send 并行分发的独立 Evidence 子图。"""


def build_evidence_graph():
    """构建 PDF 来源和本地发送记录匹配子图。

    PDF 任务存在时先进入 fan-out 节点，再使用 ``Send`` 为每项任务创建隔离
    Worker；所有 Worker 输出经 reducer 汇合后继续处理本地发送日志。没有 PDF
    时直接跳过并行阶段，确保空输入仍能从 START 正常到达 END。

    Returns:
        已编译、可独立调用且不带 Checkpointer 的 Evidence LangGraph。
    """
    builder = StateGraph(EvidenceGraphState)
    builder.add_node("collect_pdf_candidates", collect_pdf_candidates)
    builder.add_node("create_pdf_match_jobs", create_pdf_match_jobs)
    builder.add_node("fanout_pdf_matching", fanout_pdf_matching)
    builder.add_node("match_pdf_to_source_version", match_pdf_to_source_version)
    builder.add_node("join_pdf_matches", join_pdf_matches)
    builder.add_node("load_local_delivery_log", load_local_delivery_log)
    builder.add_node("match_delivery_to_version", match_delivery_to_version)
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
            "done": "load_local_delivery_log",
        },
    )
    builder.add_conditional_edges(
        "fanout_pdf_matching",
        dispatch_pdf_match_jobs,
        ["match_pdf_to_source_version"],
    )
    builder.add_edge("match_pdf_to_source_version", "join_pdf_matches")
    builder.add_edge("join_pdf_matches", "load_local_delivery_log")
    builder.add_edge("load_local_delivery_log", "match_delivery_to_version")
    builder.add_edge("match_delivery_to_version", "merge_external_evidence")
    builder.add_edge("merge_external_evidence", "validate_evidence_confidence")
    builder.add_edge("validate_evidence_confidence", "capture_evidence_memory")
    builder.add_edge("capture_evidence_memory", END)
    return builder.compile()


# 已编译的独立 Evidence 子图，包含受控的证据 Memory 捕获节点。
evidence_graph = build_evidence_graph()
