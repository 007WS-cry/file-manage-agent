from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal

from app.utils.runtime import paths_overlap

"""本模块在只读输入目录之外安全保存和读取标准化内容及中间 JSON 产物。"""


# 产物 ID 只允许安全文件名字符，避免调用方通过 ID 注入路径分隔符。
ARTIFACT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


def validate_artifact_root(
    artifact_root: str | Path,
    input_root: str | Path | None = None,
) -> Path:
    """解析并校验允许写入的产物根目录。

    本函数不会访问或修改任何原始业务文件。提供 ``input_root`` 时，产物根目录
    必须与只读输入目录完全隔离，既不能相同，也不能互为上下级目录。

    Args:
        artifact_root: 调用方显式配置的可写产物根目录。
        input_root: 可选的只读业务文件根目录。

    Returns:
        已展开用户目录并规范化的产物根目录绝对路径。

    Raises:
        ValueError: 产物根目录与只读输入目录发生重叠时抛出。
    """
    resolved_artifact_root = Path(artifact_root).expanduser().resolve()
    if input_root is not None and paths_overlap(input_root, resolved_artifact_root):
        raise ValueError("artifact_root 与只读 input_root 不得相同或互为上下级目录")
    return resolved_artifact_root


def save_json_artifact(
    artifact_root: str | Path,
    category: Literal["normalized", "intermediate"],
    artifact_id: str,
    payload: dict[str, Any],
    *,
    input_root: str | Path | None = None,
) -> str:
    """把 JSON 产物原子写入受控类别目录并返回绝对路径。

    函数只写入调用方显式提供且通过隔离校验的 ``artifact_root``，不会根据
    ``payload`` 内容选择路径，也不会执行其中的命令、公式或代码。写入目标限定
    为 ``normalized`` 或 ``intermediate``，产物 ID 不允许包含路径分隔符。

    Args:
        artifact_root: 可写产物根目录。
        category: 固定产物类别，只能为标准化内容或中间产物。
        artifact_id: 产物的稳定安全 ID，不包含扩展名。
        payload: 可由标准 JSON 编码器序列化的对象。
        input_root: 可选只读业务文件根目录，用于强制目录隔离。

    Returns:
        已成功写入的 JSON 产物绝对路径。

    Raises:
        TypeError: ``payload`` 不是字典时抛出。
        ValueError: 类别、产物 ID 或目录隔离规则不合法时抛出。
        OSError: 目录创建、临时写入或原子替换失败时抛出。
    """
    if category not in {"normalized", "intermediate"}:
        raise ValueError(f"不支持的产物类别：{category}")
    if not ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
        raise ValueError("artifact_id 必须是 1 到 200 位的安全文件名标识")
    if not isinstance(payload, dict):
        raise TypeError("JSON 产物顶层必须是对象")

    root = validate_artifact_root(artifact_root, input_root)
    category_root = root / category
    if category_root.is_symlink():
        raise ValueError("产物类别目录不得是符号链接")
    category_root.mkdir(parents=True, exist_ok=True)
    if input_root is not None and paths_overlap(input_root, category_root.resolve()):
        raise ValueError("产物类别目录不得与只读 input_root 重叠")
    artifact_path = category_root / f"{artifact_id}.json"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".tmp",
            prefix=f"{artifact_id}.",
            dir=category_root,
            delete=False,
        ) as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            temporary_path = Path(stream.name)
        os.replace(temporary_path, artifact_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return str(artifact_path)


def save_normalized_content_artifact(
    artifact_root: str | Path,
    document_id: str,
    payload: dict[str, Any],
    *,
    input_root: str | Path | None = None,
) -> str:
    """保存一个文档的标准化内容产物。

    Args:
        artifact_root: 可写产物根目录。
        document_id: 标准化文档的稳定 ID。
        payload: 包含标准化文本、结构、关键字段和警告的 JSON 对象。
        input_root: 可选只读业务文件根目录，用于强制目录隔离。

    Returns:
        位于 ``normalized`` 子目录中的 JSON 产物绝对路径。
    """
    return save_json_artifact(
        artifact_root,
        "normalized",
        document_id,
        payload,
        input_root=input_root,
    )


def save_intermediate_artifact(
    artifact_root: str | Path,
    run_id: str,
    artifact_name: str,
    payload: dict[str, Any],
    *,
    input_root: str | Path | None = None,
) -> str:
    """保存一次治理运行产生的可追踪中间 JSON 产物。

    本函数不会把 ``run_id`` 或 ``artifact_name`` 直接当作路径，而是先校验并
    组合为安全产物 ID。中间产物只用于调试或后续处理，不会修改原始文件。

    Args:
        artifact_root: 可写产物根目录。
        run_id: 当前治理运行的稳定 ID。
        artifact_name: 中间产物名称，例如 ``comparison-summary``。
        payload: 可由标准 JSON 编码器序列化的中间结果对象。
        input_root: 可选只读业务文件根目录，用于强制目录隔离。

    Returns:
        位于 ``intermediate`` 子目录中的 JSON 产物绝对路径。

    Raises:
        ValueError: 运行 ID 或产物名称不是安全文件名标识时抛出。
    """
    if not ARTIFACT_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run_id 必须是安全文件名标识")
    if not ARTIFACT_ID_PATTERN.fullmatch(artifact_name):
        raise ValueError("artifact_name 必须是安全文件名标识")
    return save_json_artifact(
        artifact_root,
        "intermediate",
        f"{run_id}-{artifact_name}",
        {
            "schema_version": "1.0",
            "run_id": run_id,
            "artifact_name": artifact_name,
            "payload": payload,
        },
        input_root=input_root,
    )


def save_context_compaction_artifact(
    artifact_root: str | Path,
    run_id: str,
    compaction_index: int,
    payload: dict[str, Any],
    *,
    input_root: str | Path | None = None,
) -> str:
    """保存 Context Compact 从图状态移出的文档上下文。

    函数只接受正压缩序号，并复用受控中间产物的目录隔离、文件名校验和原子
    写入规则。Prompt 正文不得由调用方放入 ``payload``。

    Args:
        artifact_root: 可写产物根目录。
        run_id: 当前治理运行 ID。
        compaction_index: 当前运行内从一开始递增的压缩序号。
        payload: 只包含被移出文档预览、结构和关键字段的 JSON 对象。
        input_root: 可选只读业务输入根目录，用于强制路径隔离。

    Returns:
        位于 ``intermediate`` 子目录中的 Context Compact 产物绝对路径。

    Raises:
        TypeError: ``compaction_index`` 不是整数时抛出。
        ValueError: 压缩序号不大于零或载荷疑似包含 Prompt 正文时抛出。
    """
    if isinstance(compaction_index, bool) or not isinstance(
        compaction_index,
        int,
    ):
        raise TypeError("compaction_index 必须是整数")
    if compaction_index < 1:
        raise ValueError("compaction_index 必须大于零")
    if "prompt_content" in payload:
        raise ValueError("Context Compact 产物不得保存 Prompt 正文")
    return save_intermediate_artifact(
        artifact_root,
        run_id,
        f"context-compact-{compaction_index}",
        payload,
        input_root=input_root,
    )


def load_json_artifact(
    artifact_path: str | Path,
    *,
    max_artifact_size_bytes: int = 50 * 1024 * 1024,
    expected_root: str | Path | None = None,
) -> dict[str, Any]:
    """只读加载受大小和路径约束的本地 JSON 产物。

    函数拒绝符号链接、非 JSON 文件和超限文件；提供 ``expected_root`` 时还会
    拒绝读取根目录之外的路径。载荷只按 JSON 解析，不执行其中的任何内容。

    Args:
        artifact_path: 待读取的本地 JSON 产物路径。
        max_artifact_size_bytes: 允许读取的最大文件字节数。
        expected_root: 可选允许读取的产物根目录。

    Returns:
        JSON 顶层对象。

    Raises:
        ValueError: 大小上限、路径、扩展名或顶层结构不合法时抛出。
        OSError: 文件无法读取时由操作系统抛出。
        json.JSONDecodeError: 文件内容不是合法 JSON 时抛出。
    """
    if max_artifact_size_bytes <= 0:
        raise ValueError("max_artifact_size_bytes 必须大于零")

    original_path = Path(artifact_path).expanduser()
    if original_path.is_symlink():
        raise ValueError("拒绝从符号链接读取 JSON 产物")
    resolved_path = original_path.resolve(strict=True)
    if not resolved_path.is_file() or resolved_path.suffix.lower() != ".json":
        raise ValueError(f"产物路径必须指向 JSON 普通文件：{resolved_path}")
    if expected_root is not None:
        resolved_root = Path(expected_root).expanduser().resolve(strict=True)
        try:
            resolved_path.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError("产物路径位于 expected_root 之外") from exc
    if resolved_path.stat().st_size > max_artifact_size_bytes:
        raise ValueError("JSON 产物超过读取上限")

    with resolved_path.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError("JSON 产物顶层必须是对象")
    return payload


def load_normalized_content_artifact(
    content_ref: str | Path,
    *,
    max_artifact_size_bytes: int = 50 * 1024 * 1024,
) -> dict[str, Any]:
    """读取标准化内容 JSON 并验证版本分析依赖的基础字段。

    Args:
        content_ref: ``DocumentRecord.content_ref`` 指向的 JSON 文件。
        max_artifact_size_bytes: 允许读取的产物文件大小上限。

    Returns:
        至少包含 ``normalized_text``、``structure`` 和 ``key_fields`` 的对象。

    Raises:
        ValueError: 文件或必需字段的类型、结构不合法时抛出。
        OSError: 文件无法读取时由操作系统抛出。
        json.JSONDecodeError: 文件内容不是合法 JSON 时抛出。
    """
    payload = load_json_artifact(
        content_ref,
        max_artifact_size_bytes=max_artifact_size_bytes,
    )
    for required_key in ("normalized_text", "structure", "key_fields"):
        if required_key not in payload:
            raise ValueError(f"标准化内容产物缺少字段：{required_key}")
    if not isinstance(payload["normalized_text"], str):
        raise ValueError("标准化内容产物的 normalized_text 必须是字符串")
    if not isinstance(payload["structure"], dict):
        raise ValueError("标准化内容产物的 structure 必须是对象")
    if not isinstance(payload["key_fields"], dict):
        raise ValueError("标准化内容产物的 key_fields 必须是对象")
    return payload
