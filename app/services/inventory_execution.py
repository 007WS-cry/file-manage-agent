from __future__ import annotations

from collections.abc import Callable

from app.state.models import InventoryGraphState, RawExtractedContent
from app.utils.state_lookup import find_file_by_id

"""本模块封装 Inventory 节点共用的受控文档解析执行逻辑。"""


def extract_current_file_with_parser(
    state: InventoryGraphState,
    parser: Callable[[str], RawExtractedContent],
) -> dict:
    """使用调用方提供的受信任只读解析器提取当前已登记文件。

    本函数只把状态中已登记文件的 ``absolute_path`` 传给解析器，不接受或执行
    LLM 生成的路径、命令或代码。解析器应保持只读并实施自身的资源限制；可预期
    的依赖、文件、类型和内容错误会转换为子图临时错误字段。

    Args:
        state: 包含当前文件 ID 和已登记文件记录的 Inventory 子图状态。
        parser: 由代码显式选择的 XLSX、DOCX 或 PDF 只读解析函数。

    Returns:
        当前原始提取内容，或供错误路由处理的 ``current_parse_error``。
    """
    file_record = find_file_by_id(
        state.get("files", []),
        state.get("current_file_id"),
    )
    if file_record is None:
        return {
            "current_raw_content": None,
            "current_document": None,
            "current_parse_error": "当前解析任务引用的文件不存在",
        }
    try:
        raw_content = parser(file_record["absolute_path"])
        return {
            "current_raw_content": raw_content,
            "current_document": None,
            "current_parse_error": None,
        }
    except (ImportError, OSError, TypeError, ValueError) as exc:
        return {
            "current_raw_content": None,
            "current_document": None,
            "current_parse_error": str(exc),
        }
