from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graphs.routers import route_context_compaction
from app.nodes.context_compact import (
    compact_context,
    estimate_context_tokens,
    mark_context_compaction_skipped,
    persist_context_compaction_artifact,
    persist_context_summary,
)
from app.state.models import ContextCompactGraphState

"""本模块构建 Token 估算、条件压缩、产物保存和摘要持久化的独立子图。"""


def build_context_compact_graph():
    """构建可在 Inventory 和 Evidence 后调用的 Context Compact 子图。

    Returns:
        已编译、无 Checkpointer 且通过条件边决定是否压缩的 LangGraph。
    """
    builder = StateGraph(ContextCompactGraphState)
    builder.add_node("estimate_context_tokens", estimate_context_tokens)
    builder.add_node("compact_context", compact_context)
    builder.add_node(
        "persist_context_compaction_artifact",
        persist_context_compaction_artifact,
    )
    builder.add_node("persist_context_summary", persist_context_summary)
    builder.add_node(
        "mark_context_compaction_skipped",
        mark_context_compaction_skipped,
    )

    builder.add_edge(START, "estimate_context_tokens")
    builder.add_conditional_edges(
        "estimate_context_tokens",
        route_context_compaction,
        {
            "compact": "compact_context",
            "skip": "mark_context_compaction_skipped",
        },
    )
    builder.add_edge(
        "compact_context",
        "persist_context_compaction_artifact",
    )
    builder.add_edge(
        "persist_context_compaction_artifact",
        "persist_context_summary",
    )
    builder.add_edge("persist_context_summary", END)
    builder.add_edge("mark_context_compaction_skipped", END)
    return builder.compile()


# 已编译的独立 Context Compact 子图，供两个顶层包装节点同步调用。
context_compact_graph = build_context_compact_graph()
