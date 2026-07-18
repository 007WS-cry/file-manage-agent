from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from app.services.document_grouping import normalize_filename_stem
from app.state.models import FileRecord

"""本模块提供受范围约束的只读文件发现、元数据登记和哈希计算工具。"""


# 计算文件 SHA-256 时每次从磁盘读取的默认字节数。
DEFAULT_HASH_CHUNK_SIZE = 1024 * 1024


def normalize_extensions(extensions: Iterable[str]) -> set[str]:
    """把扩展名集合规范化为带点号的小写形式。

    Args:
        extensions: 用户或配置提供的扩展名，例如 ``xlsx`` 或 ``.DOCX``。

    Returns:
        去除空值后的规范化扩展名集合。

    Raises:
        ValueError: 没有得到任何有效扩展名时抛出。
    """
    normalized = {
        value if value.startswith(".") else f".{value}"
        for raw_value in extensions
        if (value := raw_value.strip().lower())
    }
    if not normalized:
        raise ValueError("allowed_extensions 至少需要包含一个有效扩展名")
    return normalized


def calculate_sha256(
    file_path: str | Path,
    chunk_size: int = DEFAULT_HASH_CHUNK_SIZE,
) -> str:
    """只读计算指定普通文件的 SHA-256，不修改文件及其元数据。

    该函数只能读取调用方明确提供的单个文件，不会遍历目录、跟随符号链接，
    也不会上传或缓存文件内容。适合作为 LLM 工具时用于验证文件是否完全一致。

    Args:
        file_path: 需要计算哈希的本地文件路径。
        chunk_size: 每次读取的字节数，必须大于零。

    Returns:
        64 个小写十六进制字符组成的 SHA-256。

    Raises:
        ValueError: 路径是符号链接、不是普通文件或 chunk_size 非法时抛出。
        OSError: 文件无法读取时由操作系统抛出。
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于零")

    original_path = Path(file_path).expanduser()
    if original_path.is_symlink():
        raise ValueError(f"为保证扫描边界，拒绝读取符号链接：{original_path}")

    resolved_path = original_path.resolve(strict=True)
    if not resolved_path.is_file():
        raise ValueError(f"目标不是普通文件：{resolved_path}")

    digest = hashlib.sha256()
    with resolved_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_input_files(
    root_directory: str | Path,
    allowed_extensions: Iterable[str],
    *,
    recursive: bool = True,
    max_files: int = 500,
    include_hidden: bool = False,
) -> list[Path]:
    """在指定根目录内只读发现满足扩展名要求的普通文件。

    该函数不会读取文件正文，不会修改目录内容，也不会跟随符号链接。返回路径
    始终位于调用方提供的根目录内；文件数量超过上限时会明确失败而不是静默截断。

    Args:
        root_directory: 调用方授权扫描的本地根目录。
        allowed_extensions: 允许发现的文件扩展名集合。
        recursive: 是否递归检查子目录。
        max_files: 单次最多返回的文件数量，必须大于零。
        include_hidden: 是否包含名称以点号开头的文件或目录。

    Returns:
        按不区分大小写的绝对路径稳定排序的文件路径列表。

    Raises:
        FileNotFoundError: 根目录不存在时抛出。
        NotADirectoryError: 根路径不是目录时抛出。
        ValueError: 参数无效或匹配文件数超过上限时抛出。
        OSError: 目录无法读取时由操作系统抛出。
    """
    if max_files <= 0:
        raise ValueError("max_files 必须大于零")

    original_root = Path(root_directory).expanduser()
    if original_root.is_symlink():
        raise ValueError(f"为保证扫描边界，拒绝扫描符号链接目录：{original_root}")
    root = original_root.resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(f"扫描根路径不是目录：{root}")

    normalized_extensions = normalize_extensions(allowed_extensions)
    iterator = root.rglob("*") if recursive else root.glob("*")
    discovered: list[Path] = []

    for candidate in iterator:
        if candidate.is_symlink() or not candidate.is_file():
            continue

        relative_parts = candidate.relative_to(root).parts
        if not include_hidden and any(part.startswith(".") for part in relative_parts):
            continue
        if candidate.suffix.lower() not in normalized_extensions:
            continue

        discovered.append(candidate.resolve(strict=True))
        if len(discovered) > max_files:
            raise ValueError(
                f"匹配文件数量超过 max_files={max_files}，请缩小目录或提高上限"
            )

    return sorted(discovered, key=lambda path: str(path).casefold())


def build_file_record(file_path: str | Path) -> FileRecord:
    """只读提取单个文件的路径、大小、时间和 SHA-256 元数据。

    该函数不会解析文档正文，不会修改文件，也不会根据文件名推断主版本。
    ``normalized_stem`` 仅供后续分组使用，不能被视为版本结论。

    Args:
        file_path: 已由扫描范围校验过的普通文件路径。

    Returns:
        初始解析状态为 ``pending`` 的文件记录。

    Raises:
        ValueError: 路径是符号链接或不是普通文件时抛出。
        OSError: 文件元数据或内容无法读取时由操作系统抛出。
    """
    original_path = Path(file_path).expanduser()
    if original_path.is_symlink():
        raise ValueError(f"为保证扫描边界，拒绝登记符号链接：{original_path}")

    path = original_path.resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"目标不是普通文件：{path}")

    stat = path.stat()
    canonical_path = str(path).casefold()
    file_id = hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()

    return FileRecord(
        id=file_id,
        absolute_path=str(path),
        file_name=path.name,
        normalized_stem=normalize_filename_stem(path.name),
        extension=path.suffix.lower(),
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(
            stat.st_mtime,
            tz=timezone.utc,
        ).isoformat(),
        sha256=calculate_sha256(path),
        duplicate_of=None,
        parse_status="pending",
        parse_error=None,
    )


def mark_exact_duplicates(files: Iterable[FileRecord]) -> list[FileRecord]:
    """依据 SHA-256 标记完全重复文件，并保留所有原始文件记录。

    每个哈希组会选择“修改时间最早、路径字典序最小”的文件作为规范文件，
    其余文件只被标记为 ``duplicate``，不会被删除、移动或重命名。

    Args:
        files: 已完成 SHA-256 计算的文件记录。

    Returns:
        新的文件记录列表；输入记录不会被原地修改。

    Raises:
        ValueError: 文件记录缺少 SHA-256 或出现重复文件 ID 时抛出。
    """
    records = [dict(item) for item in files]
    if len({item["id"] for item in records}) != len(records):
        raise ValueError("文件记录中存在重复 id")

    by_hash: dict[str, list[dict[str, object]]] = {}
    for item in records:
        sha256 = item.get("sha256")
        if not isinstance(sha256, str) or not sha256:
            raise ValueError("每个文件记录都必须包含有效 SHA-256")
        by_hash.setdefault(sha256, []).append(item)

    for duplicate_group in by_hash.values():
        canonical = min(
            duplicate_group,
            key=lambda item: (
                str(item["modified_at"]),
                str(item["absolute_path"]).casefold(),
            ),
        )
        for item in duplicate_group:
            if item["id"] == canonical["id"]:
                continue
            item["duplicate_of"] = canonical["id"]
            item["parse_status"] = "duplicate"

    return [FileRecord(**item) for item in records]


def scan_files(
    root_directory: str | Path,
    allowed_extensions: Iterable[str],
    *,
    recursive: bool = True,
    max_files: int = 500,
    include_hidden: bool = False,
) -> list[FileRecord]:
    """只读扫描授权目录并返回已完成哈希去重标记的文件记录。

    这是面向 Agent 的高层扫描工具。它只访问明确给出的根目录，拒绝符号链接，
    不读取目录范围外的内容，不修改任何业务文件，并在超过文件上限时停止执行。

    Args:
        root_directory: 调用方明确授权扫描的本地根目录。
        allowed_extensions: 允许处理的文件扩展名。
        recursive: 是否递归扫描子目录。
        max_files: 单次扫描允许处理的最大文件数量。
        include_hidden: 是否允许扫描点号开头的隐藏路径。

    Returns:
        路径顺序稳定且已标记完全重复项的文件记录列表。

    Raises:
        OSError: 目录或文件不可访问时抛出。
        ValueError: 参数无效、出现符号链接或文件数超过限制时抛出。
    """
    paths = discover_input_files(
        root_directory,
        allowed_extensions,
        recursive=recursive,
        max_files=max_files,
        include_hidden=include_hidden,
    )
    return mark_exact_duplicates(build_file_record(path) for path in paths)
