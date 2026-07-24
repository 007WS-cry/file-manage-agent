from __future__ import annotations

from app.services.content_normalizer import (
    normalize_document_content as normalize_document_content_service,
)
from app.services.inventory_execution import extract_current_file_with_parser
from app.state.models import FileRecord, InventoryGraphState
from app.tools.document_parsers import (
    parse_docx_document,
    parse_pdf_document,
    parse_xlsx_document,
)
from app.tools.file_scanner import (
    build_file_record,
)
from app.tools.file_scanner import (
    discover_input_files as discover_input_files_tool,
)
from app.tools.file_scanner import (
    mark_exact_duplicates as mark_exact_duplicates_tool,
)
from app.utils.error_context import create_node_error
from app.utils.state_lookup import find_file_by_id

"""本模块实现 Inventory 子图的文件发现、逐文件解析、标准化和错误隔离节点。"""


def discover_input_files(state: InventoryGraphState) -> dict:
    """按请求范围只读发现候选文件路径，并初始化 Inventory 私有字段。

    Args:
        state: 包含扫描根目录、递归规则、扩展名和文件数量上限的子图状态。

    Returns:
        发现路径和已重置的解析循环字段；目录访问失败时附加致命错误。
    """
    try:
        paths = discover_input_files_tool(
            state["request"]["root_directory"],
            state["request"]["allowed_extensions"],
            recursive=state["request"]["recursive"],
            max_files=state["request"]["max_files"],
        )
        return {
            "discovered_paths": [str(path) for path in paths],
            "parse_queue": [],
            "current_file_id": None,
            "current_raw_content": None,
            "current_document": None,
            "current_parse_error": None,
        }
    except (OSError, TypeError, ValueError) as exc:
        return {
            "discovered_paths": [],
            "parse_queue": [],
            "current_file_id": None,
            "current_raw_content": None,
            "current_document": None,
            "current_parse_error": None,
            "errors": [
                create_node_error(
                    state,
                    stage="inventory",
                    node_name="discover_input_files",
                    category="filesystem",
                    message=str(exc),
                    fatal=True,
                )
            ],
        }


def register_file_metadata(state: InventoryGraphState) -> dict:
    """逐个登记候选文件的元数据和 SHA-256，并隔离单文件读取错误。

    Args:
        state: 已包含 ``discovered_paths`` 的 Inventory 状态。

    Returns:
        成功登记的文件记录和不会中断其他文件的非致命错误列表。
    """
    files: list[FileRecord] = []
    errors = []
    for file_path in state.get("discovered_paths", []):
        try:
            files.append(build_file_record(file_path))
        except (OSError, TypeError, ValueError) as exc:
            errors.append(
                create_node_error(
                    state,
                    stage="inventory",
                    node_name="register_file_metadata",
                    category="filesystem",
                    message=f"无法登记文件元数据：{exc}",
                    fatal=False,
                )
            )
    result: dict = {"files": files}
    if errors:
        result["errors"] = errors
    return result


def mark_exact_duplicates(state: InventoryGraphState) -> dict:
    """通过 SHA-256 标记完全重复件，同时保留所有文件记录。

    Args:
        state: 已完成元数据和哈希登记的 Inventory 状态。

    Returns:
        更新了 ``duplicate_of`` 和解析状态的文件记录；内部不删除任何文件。
    """
    try:
        return {"files": mark_exact_duplicates_tool(state.get("files", []))}
    except (TypeError, ValueError) as exc:
        return {
            "errors": [
                create_node_error(
                    state,
                    stage="inventory",
                    node_name="mark_exact_duplicates",
                    category="validation",
                    message=str(exc),
                    fatal=True,
                )
            ]
        }


def build_parse_queue(state: InventoryGraphState) -> dict:
    """为非重复且尚未解析的文件建立稳定解析队列。"""
    queue = [
        item["id"]
        for item in state.get("files", [])
        if item["duplicate_of"] is None and item["parse_status"] == "pending"
    ]
    return {
        "parse_queue": queue,
        "current_file_id": None,
        "current_raw_content": None,
        "current_document": None,
        "current_parse_error": None,
    }


def load_next_parse_job(state: InventoryGraphState) -> dict:
    """从解析队列取出一个文件，并清空上一个文件的临时结果。"""
    queue = list(state.get("parse_queue", []))
    if not queue:
        return {
            "current_file_id": None,
            "current_raw_content": None,
            "current_document": None,
            "current_parse_error": "解析队列为空",
        }
    return {
        "parse_queue": queue[1:],
        "current_file_id": queue[0],
        "current_raw_content": None,
        "current_document": None,
        "current_parse_error": None,
    }


def extract_xlsx_content(state: InventoryGraphState) -> dict:
    """使用受资源限制的只读 XLSX 解析器提取当前文件内容。"""
    return extract_current_file_with_parser(state, parse_xlsx_document)


def extract_docx_content(state: InventoryGraphState) -> dict:
    """使用受资源限制的只读 DOCX 解析器提取当前文件内容。"""
    return extract_current_file_with_parser(state, parse_docx_document)


def extract_pdf_content(state: InventoryGraphState) -> dict:
    """使用不执行 OCR 的只读 PDF 解析器提取当前文件文本。"""
    return extract_current_file_with_parser(state, parse_pdf_document)


def record_unsupported_file(state: InventoryGraphState) -> dict:
    """把当前未知扩展名文件标记为不支持，并继续处理后续文件。"""
    file_record = find_file_by_id(
        state.get("files", []),
        state.get("current_file_id"),
    )
    if file_record is None:
        error_message = "当前解析任务引用的文件不存在"
        related_file_id = state.get("current_file_id")
        files = []
    else:
        updated = dict(file_record)
        error_message = f"暂不支持扩展名：{file_record['extension']}"
        updated.update({"parse_status": "unsupported", "parse_error": error_message})
        related_file_id = file_record["id"]
        files = [FileRecord(**updated)]
    return {
        "files": files,
        "errors": [
            create_node_error(
                state,
                stage="inventory",
                node_name="record_unsupported_file",
                category="parse",
                message=error_message,
                related_file_id=related_file_id,
                fatal=False,
            )
        ],
        "current_file_id": None,
        "current_raw_content": None,
        "current_document": None,
        "current_parse_error": None,
    }


def normalize_document_content(state: InventoryGraphState) -> dict:
    """标准化当前解析结果并写入输入目录之外的内容产物。

    Args:
        state: 包含当前文件、解析器结果和隔离工作空间的 Inventory 状态。

    Returns:
        等待提交的 ``current_document``，或可由路由识别的解析错误。
    """
    if state.get("current_parse_error") is not None:
        return {}
    file_record = find_file_by_id(
        state.get("files", []),
        state.get("current_file_id"),
    )
    raw_content = state.get("current_raw_content")
    if file_record is None or raw_content is None:
        return {
            "current_document": None,
            "current_parse_error": "当前文件或解析结果不存在",
        }
    try:
        document = normalize_document_content_service(
            file_record,
            raw_content,
            state["workspace"]["artifact_root"],
            input_root=state["workspace"]["input_root"],
        )
        return {"current_document": document, "current_parse_error": None}
    except (OSError, TypeError, ValueError) as exc:
        return {"current_document": None, "current_parse_error": str(exc)}


def record_document_result(state: InventoryGraphState) -> dict:
    """提交当前标准化文档，并把对应文件标记为解析成功。"""
    file_record = find_file_by_id(
        state.get("files", []),
        state.get("current_file_id"),
    )
    document = state.get("current_document")
    if file_record is None or document is None:
        return record_parse_error(
            {
                **state,
                "current_parse_error": "无法提交缺失的文件或标准化文档",
            }
        )
    updated = dict(file_record)
    updated.update({"parse_status": "parsed", "parse_error": None})
    return {
        "files": [FileRecord(**updated)],
        "documents": [document],
        "current_file_id": None,
        "current_raw_content": None,
        "current_document": None,
        "current_parse_error": None,
    }


def record_parse_error(state: InventoryGraphState) -> dict:
    """记录当前文件的非致命解析错误，并清理逐文件临时状态。"""
    file_record = find_file_by_id(
        state.get("files", []),
        state.get("current_file_id"),
    )
    error_message = state.get("current_parse_error") or "未知文档解析错误"
    if file_record is None:
        related_file_id = state.get("current_file_id")
        files = []
    else:
        updated = dict(file_record)
        updated.update({"parse_status": "failed", "parse_error": error_message})
        related_file_id = file_record["id"]
        files = [FileRecord(**updated)]
    return {
        "files": files,
        "errors": [
            create_node_error(
                state,
                stage="inventory",
                node_name="record_parse_error",
                category="parse",
                message=error_message,
                related_file_id=related_file_id,
                fatal=False,
            )
        ],
        "current_file_id": None,
        "current_raw_content": None,
        "current_document": None,
        "current_parse_error": None,
    }


def validate_inventory_results(state: InventoryGraphState) -> dict:
    """校验成功解析文件与标准化文档的一一对应关系。"""
    document_file_ids = [item["file_id"] for item in state.get("documents", [])]
    parsed_file_ids = [
        item["id"]
        for item in state.get("files", [])
        if item["parse_status"] == "parsed"
    ]
    validation_messages = []
    if len(document_file_ids) != len(set(document_file_ids)):
        validation_messages.append("同一文件存在多条标准化文档记录")
    missing_documents = set(parsed_file_ids) - set(document_file_ids)
    if missing_documents:
        validation_messages.append(
            f"{len(missing_documents)} 个 parsed 文件缺少标准化文档记录"
        )
    unknown_documents = set(document_file_ids) - {item["id"] for item in state.get("files", [])}
    if unknown_documents:
        validation_messages.append(
            f"{len(unknown_documents)} 条标准化文档引用未知文件"
        )
    if not validation_messages:
        return {}
    return {
        "errors": [
            create_node_error(
                state,
                stage="inventory",
                node_name="validate_inventory_results",
                category="validation",
                message="；".join(validation_messages),
                fatal=True,
            )
        ]
    }
