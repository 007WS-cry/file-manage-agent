from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

from app.state.models import PromptState

"""本模块受限读取 System Prompt，校验大小和内容并生成可审计加载状态。"""

# 单个 System Prompt 资源允许读取的默认最大字节数。
DEFAULT_MAX_PROMPT_BYTES = 128 * 1024

# 单次运行允许追加的动态规则数量上限。
MAX_DYNAMIC_PROMPT_RULES = 20

# 单条动态规则允许包含的最大字符数。
MAX_DYNAMIC_RULE_CHARACTERS = 500

# 当前 Prompt 加载器允许读取的纯文本资源扩展名。
ALLOWED_PROMPT_EXTENSIONS = frozenset({".md", ".txt"})


def is_system_prompt_enabled(prompt_state: PromptState) -> bool:
    """判断本次运行是否启用了 System Prompt。

    Args:
        prompt_state: 由状态工厂创建的 Prompt 状态。

    Returns:
        仅当 ``enabled`` 为 True 时返回 True。
    """
    return prompt_state.get("enabled") is True


def mark_prompt_disabled(prompt_state: PromptState) -> PromptState:
    """返回不包含 Prompt 正文和哈希的关闭状态副本。

    Args:
        prompt_state: 等待标记为关闭的 Prompt 状态。

    Returns:
        状态为 ``disabled`` 的新 Prompt 状态，不修改调用方对象。
    """
    disabled_state = dict(prompt_state)
    disabled_state.update(
        {
            "enabled": False,
            "source_path": None,
            "content": "",
            "content_sha256": None,
            "status": "disabled",
        }
    )
    return PromptState(**disabled_state)


def _reject_symlink_components(path: Path) -> None:
    """拒绝目标路径及其现有父路径中的符号链接。

    Args:
        path: 已转换为绝对形式、等待检查的 Prompt 候选路径。

    Raises:
        ValueError: 任一路径组件是符号链接时抛出。
    """
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"System Prompt 路径不得包含符号链接：{current}")


def resolve_prompt_resource(
    source_path: str | Path,
    *,
    base_directory: str | Path | None = None,
    allowed_root: str | Path | None = None,
) -> Path:
    """解析并约束用户明确配置的本地 Prompt 资源路径。

    该函数只解析单个本地纯文本资源，不遍历目录、不访问网络、不执行文件内容，
    并拒绝符号链接和允许根目录之外的路径。

    Args:
        source_path: Prompt 资源文件路径。
        base_directory: 相对路径使用的基准目录；省略时使用当前工作目录。
        allowed_root: 允许读取的根目录；省略时与基准目录相同。

    Returns:
        位于允许根目录内的规范化绝对普通文件路径。

    Raises:
        FileNotFoundError: Prompt 文件或允许根目录不存在时抛出。
        ValueError: 路径越界、包含符号链接、扩展名不受支持或不是普通文件时抛出。
        OSError: 路径元数据无法读取时由操作系统抛出。
    """
    if not isinstance(source_path, (str, Path)):
        raise TypeError("source_path 必须是本地路径字符串或 Path")
    if isinstance(source_path, str) and not source_path.strip():
        raise ValueError("source_path 不得为空")

    base = Path(base_directory or Path.cwd()).expanduser()
    _reject_symlink_components(base.absolute())
    resolved_base = base.resolve(strict=True)
    if not resolved_base.is_dir():
        raise ValueError(f"Prompt 基准路径不是目录：{resolved_base}")

    root_candidate = Path(allowed_root).expanduser() if allowed_root else resolved_base
    if not root_candidate.is_absolute():
        root_candidate = resolved_base / root_candidate
    _reject_symlink_components(root_candidate.absolute())
    resolved_root = root_candidate.resolve(strict=True)
    if not resolved_root.is_dir():
        raise ValueError(f"Prompt 允许根路径不是目录：{resolved_root}")

    candidate = Path(source_path).expanduser()
    if not candidate.is_absolute():
        candidate = resolved_base / candidate
    _reject_symlink_components(candidate.absolute())
    resolved_path = candidate.resolve(strict=True)
    if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
        raise ValueError(f"System Prompt 必须位于允许根目录内：{resolved_root}")
    if not resolved_path.is_file():
        raise ValueError(f"System Prompt 路径不是普通文件：{resolved_path}")
    if resolved_path.suffix.casefold() not in ALLOWED_PROMPT_EXTENSIONS:
        raise ValueError("System Prompt 只允许使用 .md 或 .txt 纯文本资源")
    return resolved_path


def read_prompt_resource(
    prompt_path: str | Path,
    *,
    max_bytes: int = DEFAULT_MAX_PROMPT_BYTES,
) -> str:
    """受大小限制地只读加载 UTF-8 Prompt 资源。

    该函数不会修改 Prompt 文件，不执行模板、脚本或文件内命令，也不会自动猜测
    其他编码。读取前后都会限制字节数，以减少文件变化导致的越界读取风险。

    Args:
        prompt_path: 已通过范围校验的本地 Prompt 普通文件路径。
        max_bytes: 最大允许读取字节数，必须大于零。

    Returns:
        严格按 UTF-8 解码的 Prompt 原始文本。

    Raises:
        TypeError: ``max_bytes`` 不是整数时抛出。
        ValueError: 大小上限非法、路径不安全、文件超限或内容不是 UTF-8 时抛出。
        OSError: 文件无法读取时由操作系统抛出。
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise TypeError("max_bytes 必须是整数")
    if max_bytes <= 0:
        raise ValueError("max_bytes 必须大于零")

    path = Path(prompt_path).expanduser()
    _reject_symlink_components(path.absolute())
    resolved_path = path.resolve(strict=True)
    if not resolved_path.is_file():
        raise ValueError(f"System Prompt 路径不是普通文件：{resolved_path}")
    if resolved_path.stat().st_size > max_bytes:
        raise ValueError(f"System Prompt 超过 {max_bytes} 字节读取上限")

    with resolved_path.open("rb") as stream:
        payload = stream.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError(f"System Prompt 超过 {max_bytes} 字节读取上限")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("System Prompt 必须使用 UTF-8 编码") from exc


def verify_prompt_content(content: str) -> str:
    """规范换行并验证 Prompt 内容非空且不含空字符。

    Args:
        content: 从受限资源读取的 Prompt 文本。

    Returns:
        使用 LF 换行、移除 UTF-8 BOM 和首尾空白并以换行结尾的文本。

    Raises:
        TypeError: Prompt 内容不是字符串时抛出。
        ValueError: Prompt 为空或包含空字符时抛出。
    """
    if not isinstance(content, str):
        raise TypeError("Prompt 内容必须是字符串")
    normalized = content.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.strip()
    if not normalized:
        raise ValueError("System Prompt 内容不得为空")
    if "\x00" in normalized:
        raise ValueError("System Prompt 内容不得包含空字符")
    return f"{normalized}\n"


def build_dynamic_prompt_rules(
    prompt_content: str,
    dynamic_rules: Iterable[str],
) -> str:
    """校验并把简短动态规则追加到基础 System Prompt。

    动态规则只作为纯文本追加，不执行模板替换、表达式或代码。调用方不得通过该
    参数传入完整业务正文；数量和单条长度均受到固定上限约束。

    Args:
        prompt_content: 已通过内容校验的基础 Prompt。
        dynamic_rules: 根据本次治理请求生成的简短规则序列。

    Returns:
        未提供规则时返回基础 Prompt；否则返回追加动态规则章节的完整 Prompt。

    Raises:
        TypeError: 规则不是字符串时抛出。
        ValueError: 规则为空、重复、包含空字符或超过数量、长度上限时抛出。
    """
    if isinstance(dynamic_rules, (str, bytes)):
        raise TypeError("dynamic_rules 必须是字符串序列，不能是单个字符串")
    base_content = verify_prompt_content(prompt_content)
    rules: list[str] = []
    for raw_rule in dynamic_rules:
        if not isinstance(raw_rule, str):
            raise TypeError("dynamic_rules 的元素必须是字符串")
        rule = raw_rule.strip()
        if not rule:
            raise ValueError("dynamic_rules 不得包含空字符串")
        if "\x00" in rule:
            raise ValueError("dynamic_rules 不得包含空字符")
        if "\n" in rule or "\r" in rule:
            raise ValueError("每条动态规则必须是单行文本")
        if len(rule) > MAX_DYNAMIC_RULE_CHARACTERS:
            raise ValueError(
                f"单条动态规则不得超过 {MAX_DYNAMIC_RULE_CHARACTERS} 个字符"
            )
        if rule in rules:
            raise ValueError(f"dynamic_rules 不得包含重复规则：{rule}")
        rules.append(rule)
        if len(rules) > MAX_DYNAMIC_PROMPT_RULES:
            raise ValueError(
                f"dynamic_rules 不得超过 {MAX_DYNAMIC_PROMPT_RULES} 条"
            )

    if not rules:
        return base_content
    rule_lines = "\n".join(f"{index}. {rule}" for index, rule in enumerate(rules, 1))
    return f"{base_content}\n## 本次运行动态规则\n\n{rule_lines}\n"


def record_loaded_prompt(
    prompt_state: PromptState,
    *,
    content: str,
    source_path: str | Path,
) -> PromptState:
    """记录已加载 Prompt 的规范路径、完整内容和 SHA-256。

    Args:
        prompt_state: 原始 Prompt 配置状态。
        content: 已完成基础校验和动态规则追加的完整 Prompt。
        source_path: 实际读取的规范化资源路径。

    Returns:
        状态为 ``loaded`` 的新 Prompt 状态，不修改调用方对象。
    """
    loaded_state = dict(prompt_state)
    loaded_state.update(
        {
            "enabled": True,
            "source_path": str(Path(source_path).resolve(strict=True)),
            "content": content,
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "status": "loaded",
        }
    )
    return PromptState(**loaded_state)


def record_prompt_load_error(prompt_state: PromptState) -> PromptState:
    """清除未完成内容并返回显式的 Prompt 加载失败状态。

    Args:
        prompt_state: 加载过程中发生异常的 Prompt 状态。

    Returns:
        保留版本和源路径、状态为 ``failed`` 的新 Prompt 状态。
    """
    failed_state = dict(prompt_state)
    failed_state.update(
        {
            "content": "",
            "content_sha256": None,
            "status": "failed",
        }
    )
    return PromptState(**failed_state)


def load_system_prompt(
    prompt_state: PromptState,
    *,
    base_directory: str | Path | None = None,
    allowed_root: str | Path | None = None,
    max_bytes: int = DEFAULT_MAX_PROMPT_BYTES,
) -> PromptState:
    """按受限路径、UTF-8 和大小规则加载本次 System Prompt。

    本函数不访问网络、不执行模板或代码，也不会吞掉路径、编码和内容异常。调用方
    应捕获异常、调用 ``record_prompt_load_error``，并按照顶层错误策略决定是否阻断。

    Args:
        prompt_state: 由状态工厂创建的 Prompt 状态。
        base_directory: 相对资源路径的基准目录。
        allowed_root: Prompt 允许读取的根目录。
        max_bytes: Prompt 资源最大读取字节数。

    Returns:
        关闭时返回 ``disabled`` 状态；启用时返回包含内容和哈希的 ``loaded`` 状态。

    Raises:
        OSError: Prompt 路径或文件无法访问时抛出。
        TypeError: Prompt 状态或读取参数类型不正确时抛出。
        ValueError: Prompt 路径越界、文件超限、编码或内容不合法时抛出。
    """
    if not is_system_prompt_enabled(prompt_state):
        return mark_prompt_disabled(prompt_state)
    source_path = prompt_state.get("source_path")
    if not source_path:
        raise ValueError("启用 System Prompt 时必须提供 source_path")

    resolved_path = resolve_prompt_resource(
        source_path,
        base_directory=base_directory,
        allowed_root=allowed_root,
    )
    raw_content = read_prompt_resource(resolved_path, max_bytes=max_bytes)
    verified_content = verify_prompt_content(raw_content)
    complete_content = build_dynamic_prompt_rules(
        verified_content,
        prompt_state.get("dynamic_rules", []),
    )
    return record_loaded_prompt(
        prompt_state,
        content=complete_content,
        source_path=resolved_path,
    )
