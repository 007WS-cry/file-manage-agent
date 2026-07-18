from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graphs.routers import has_pending_parse_jobs, parse_succeeded, route_parser
from app.nodes.inventory import (
    build_parse_queue,
    discover_input_files,
    extract_docx_content,
    extract_pdf_content,
    extract_xlsx_content,
    load_next_parse_job,
    mark_exact_duplicates,
    normalize_document_content,
    record_document_result,
    record_parse_error,
    record_unsupported_file,
    register_file_metadata,
    validate_inventory_results,
)
from app.state.models import InventoryGraphState

"""本模块构建并编译文件发现、逐文件解析和内容标准化 Inventory 子图。"""


def build_inventory_graph():
    """构建 Inventory 子图并校验所有节点名称和条件路由目标。"""
    builder = StateGraph(InventoryGraphState)
    builder.add_node("discover_input_files", discover_input_files)
    builder.add_node("register_file_metadata", register_file_metadata)
    builder.add_node("mark_exact_duplicates", mark_exact_duplicates)
    builder.add_node("build_parse_queue", build_parse_queue)
    builder.add_node("load_next_parse_job", load_next_parse_job)
    builder.add_node("extract_xlsx_content", extract_xlsx_content)
    builder.add_node("extract_docx_content", extract_docx_content)
    builder.add_node("extract_pdf_content", extract_pdf_content)
    builder.add_node("record_unsupported_file", record_unsupported_file)
    builder.add_node("normalize_document_content", normalize_document_content)
    builder.add_node("record_document_result", record_document_result)
    builder.add_node("record_parse_error", record_parse_error)
    builder.add_node("validate_inventory_results", validate_inventory_results)

    builder.add_edge(START, "discover_input_files")
    builder.add_edge("discover_input_files", "register_file_metadata")
    builder.add_edge("register_file_metadata", "mark_exact_duplicates")
    builder.add_edge("mark_exact_duplicates", "build_parse_queue")
    builder.add_conditional_edges(
        "build_parse_queue",
        has_pending_parse_jobs,
        {"pending": "load_next_parse_job", "done": "validate_inventory_results"},
    )
    builder.add_conditional_edges(
        "load_next_parse_job",
        route_parser,
        {
            "xlsx": "extract_xlsx_content",
            "docx": "extract_docx_content",
            "pdf": "extract_pdf_content",
            "unsupported": "record_unsupported_file",
        },
    )
    builder.add_edge("extract_xlsx_content", "normalize_document_content")
    builder.add_edge("extract_docx_content", "normalize_document_content")
    builder.add_edge("extract_pdf_content", "normalize_document_content")
    builder.add_conditional_edges(
        "normalize_document_content",
        parse_succeeded,
        {"success": "record_document_result", "failure": "record_parse_error"},
    )
    builder.add_conditional_edges(
        "record_document_result",
        has_pending_parse_jobs,
        {"pending": "load_next_parse_job", "done": "validate_inventory_results"},
    )
    builder.add_conditional_edges(
        "record_parse_error",
        has_pending_parse_jobs,
        {"pending": "load_next_parse_job", "done": "validate_inventory_results"},
    )
    builder.add_conditional_edges(
        "record_unsupported_file",
        has_pending_parse_jobs,
        {"pending": "load_next_parse_job", "done": "validate_inventory_results"},
    )
    builder.add_edge("validate_inventory_results", END)
    return builder.compile()


# 已编译的 Inventory 子图，供顶层治理图直接作为子图节点接入。
inventory_graph = build_inventory_graph()
