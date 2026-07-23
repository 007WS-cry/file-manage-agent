from __future__ import annotations

import hashlib
import sysconfig
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import yaml

from app.state.models import SkillRecord, SkillRegistryState

"""本模块安全读取受控 Skill 注册表，并按需加载单个 SKILL.md 的指令正文。"""

# Skill 注册表允许使用的固定 Task 类型。
ALLOWED_SKILL_TASK_TYPES = frozenset(
    {
        "inventory",
        "version_analysis",
        "evidence",
        "recommendation",
        "human_review",
        "report",
    }
)

# Skill 注册表允许绑定的固定 Agent 角色。
ALLOWED_SKILL_ROLES = frozenset({"coordinator", "content", "version", "evidence"})

# 单个注册表文件允许的最大字节数，防止配置异常占用过多内存。
MAX_SKILL_REGISTRY_BYTES = 128 * 1024

# 单个 SKILL.md 允许的最大字节数，避免完整业务材料被误当成 Skill 加载。
MAX_SKILL_DOCUMENT_BYTES = 64 * 1024


def _resolve_default_skill_registry_path() -> str:
    """定位源码、容器或 wheel 数据目录中的默认 Skill 注册表。

    Returns:
        默认 ``resources/skills/registry.yaml`` 的绝对路径。
    """
    relative_path = Path("resources/skills/registry.yaml")
    source_path = Path(__file__).resolve().parents[2] / relative_path
    if source_path.is_file():
        return str(source_path)
    installed_path = Path(sysconfig.get_path("data")) / relative_path
    return str(installed_path)


# 默认 Skill 注册表路径，兼容源码、容器与 wheel 安装布局。
DEFAULT_SKILL_REGISTRY_PATH = _resolve_default_skill_registry_path()


def create_pending_skill_registry(
    source_path: str | Path | None = None,
) -> SkillRegistryState:
    """创建尚未读取磁盘元数据的 Skill 注册表状态。

    Args:
        source_path: 可选注册表路径；省略时使用受控默认资源。

    Returns:
        状态为 ``pending`` 且不包含 Skill 正文的注册表状态。

    Raises:
        TypeError: 路径不是字符串、Path 或 None 时抛出。
        ValueError: 路径字符串为空时抛出。
    """
    raw_path = source_path if source_path is not None else DEFAULT_SKILL_REGISTRY_PATH
    if not isinstance(raw_path, (str, Path)):
        raise TypeError("Skill 注册表路径必须是字符串、Path 或 None")
    normalized = str(raw_path).strip()
    if not normalized:
        raise ValueError("Skill 注册表路径不得为空")
    return SkillRegistryState(
        version="",
        source_path=str(Path(normalized).expanduser().resolve()),
        status="pending",
        skills=[],
    )


def _read_bounded_utf8(path: Path, *, max_bytes: int, resource_name: str) -> str:
    """在大小上限内读取一个 UTF-8 受控资源。

    Args:
        path: 已解析的本地文件路径。
        max_bytes: 允许读取的最大字节数。
        resource_name: 用于异常消息的资源名称。

    Returns:
        非空且不包含空字节的 UTF-8 文本。

    Raises:
        FileNotFoundError: 目标不是普通文件时抛出。
        ValueError: 文件为空、过大、含空字节或不是合法 UTF-8 时抛出。
    """
    if not path.is_file():
        raise FileNotFoundError(f"{resource_name} 不存在或不是普通文件：{path}")
    size = path.stat().st_size
    if size < 1:
        raise ValueError(f"{resource_name} 不得为空：{path}")
    if size > max_bytes:
        raise ValueError(f"{resource_name} 超过 {max_bytes} 字节上限：{path}")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{resource_name} 必须使用 UTF-8 编码：{path}") from error
    if "\x00" in content:
        raise ValueError(f"{resource_name} 不得包含空字节：{path}")
    if not content.strip():
        raise ValueError(f"{resource_name} 不得只包含空白：{path}")
    return content


def _normalize_string_list(
    value: object,
    *,
    field_name: str,
    allowed_values: frozenset[str],
) -> list[str]:
    """校验并复制注册表中的非空字符串列表。

    Args:
        value: 等待校验的 YAML 字段值。
        field_name: 用于异常消息的完整字段名称。
        allowed_values: 当前字段允许出现的固定值。

    Returns:
        保持配置顺序、无重复且全部属于白名单的字符串列表。

    Raises:
        TypeError: 字段不是列表或元素不是字符串时抛出。
        ValueError: 列表为空、包含空值、重复值或未知值时抛出。
    """
    if not isinstance(value, list):
        raise TypeError(f"{field_name} 必须是字符串列表")
    if not value:
        raise ValueError(f"{field_name} 不得为空")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"{field_name} 的元素必须是字符串")
        text = item.strip()
        if not text:
            raise ValueError(f"{field_name} 不得包含空字符串")
        if text not in allowed_values:
            raise ValueError(f"{field_name} 包含未知值：{text}")
        if text in normalized:
            raise ValueError(f"{field_name} 包含重复值：{text}")
        normalized.append(text)
    return normalized


def _resolve_skill_document_path(registry_path: Path, relative_path: object) -> Path:
    """把注册表中的相对路径解析为受控目录内的真实 SKILL.md。

    Args:
        registry_path: 已解析的 registry.yaml 路径。
        relative_path: YAML 中声明的 Skill 文档相对路径。

    Returns:
        位于注册表目录内且文件名为 ``SKILL.md`` 的绝对路径。

    Raises:
        TypeError: 配置路径不是字符串时抛出。
        ValueError: 路径为空、为绝对路径、越界或文件名不正确时抛出。
        FileNotFoundError: 目标文件不存在时抛出。
    """
    if not isinstance(relative_path, str):
        raise TypeError("Skill path 必须是字符串")
    normalized = relative_path.strip()
    if not normalized:
        raise ValueError("Skill path 不得为空")
    configured_path = Path(normalized)
    if configured_path.is_absolute():
        raise ValueError("Skill path 必须相对于 registry.yaml 所在目录")
    base_directory = registry_path.parent.resolve()
    candidate = (base_directory / configured_path).resolve()
    if not candidate.is_relative_to(base_directory):
        raise ValueError(f"Skill path 越出受控目录：{normalized}")
    if candidate.name != "SKILL.md":
        raise ValueError(f"Skill 文档必须命名为 SKILL.md：{normalized}")
    if not candidate.is_file():
        raise FileNotFoundError(f"Skill 文档不存在：{candidate}")
    return candidate


def load_skill_registry_metadata(
    source_path: str | Path | None = None,
) -> SkillRegistryState:
    """读取并严格校验 Skill 注册表，但不加载任何 SKILL.md 正文。

    Args:
        source_path: 可选 registry.yaml 路径；省略时使用受控默认资源。

    Returns:
        状态为 ``ready``、全部 Skill 均为 ``available`` 的注册表状态。

    Raises:
        TypeError: YAML 根对象、字段或列表元素类型不符合协议时抛出。
        ValueError: YAML 字段未知、为空、重复、越界或不在固定白名单时抛出。
        FileNotFoundError: 注册表或其中声明的 SKILL.md 不存在时抛出。
    """
    pending = create_pending_skill_registry(source_path)
    registry_path = Path(pending["source_path"])
    raw_text = _read_bounded_utf8(
        registry_path,
        max_bytes=MAX_SKILL_REGISTRY_BYTES,
        resource_name="Skill 注册表",
    )
    try:
        raw_config = yaml.safe_load(raw_text)
    except yaml.YAMLError as error:
        raise ValueError(f"Skill 注册表 YAML 无法解析：{error}") from error
    if not isinstance(raw_config, Mapping):
        raise TypeError("Skill 注册表根对象必须是映射")
    unknown_root_fields = sorted(set(raw_config) - {"version", "skills"})
    if unknown_root_fields:
        raise ValueError(
            "Skill 注册表包含未知字段：" + ", ".join(unknown_root_fields)
        )

    version = raw_config.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("Skill 注册表 version 必须是非空字符串")
    raw_skills = raw_config.get("skills")
    if not isinstance(raw_skills, list) or not raw_skills:
        raise ValueError("Skill 注册表 skills 必须是非空列表")

    skills: list[SkillRecord] = []
    seen_ids: set[str] = set()
    for index, raw_skill in enumerate(raw_skills):
        field_prefix = f"skills[{index}]"
        if not isinstance(raw_skill, Mapping):
            raise TypeError(f"{field_prefix} 必须是映射")
        allowed_fields = {
            "id",
            "name",
            "description",
            "path",
            "task_types",
            "roles",
        }
        unknown_fields = sorted(set(raw_skill) - allowed_fields)
        if unknown_fields:
            raise ValueError(
                f"{field_prefix} 包含未知字段：" + ", ".join(unknown_fields)
            )

        normalized_text: dict[str, str] = {}
        for field_name in ("id", "name", "description"):
            value = raw_skill.get(field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_prefix}.{field_name} 必须是非空字符串")
            normalized_text[field_name] = value.strip()
        skill_id = normalized_text["id"]
        if skill_id in seen_ids:
            raise ValueError(f"Skill 注册表包含重复 ID：{skill_id}")
        seen_ids.add(skill_id)

        document_path = _resolve_skill_document_path(
            registry_path,
            raw_skill.get("path"),
        )
        task_types = _normalize_string_list(
            raw_skill.get("task_types"),
            field_name=f"{field_prefix}.task_types",
            allowed_values=ALLOWED_SKILL_TASK_TYPES,
        )
        roles = _normalize_string_list(
            raw_skill.get("roles"),
            field_name=f"{field_prefix}.roles",
            allowed_values=ALLOWED_SKILL_ROLES,
        )
        skills.append(
            SkillRecord(
                skill_id=skill_id,
                name=normalized_text["name"],
                description=normalized_text["description"],
                source_path=str(document_path),
                task_types=task_types,
                roles=roles,
                status="available",
                bound_task_id=None,
                content="",
                content_sha256=None,
            )
        )

    return SkillRegistryState(
        version=version.strip(),
        source_path=str(registry_path),
        status="ready",
        skills=skills,
    )


def load_skill_document(
    skill: SkillRecord,
    *,
    registry_source_path: str | Path,
) -> SkillRecord:
    """按需读取一个 Skill 的受控 SKILL.md 并生成内容摘要。

    Args:
        skill: 注册表中状态为 ``available`` 的 Skill 记录。
        registry_source_path: 当前 Skill 所属 registry.yaml 的受控路径。

    Returns:
        状态为 ``loaded`` 且包含正文和 SHA-256 的独立 Skill 记录。

    Raises:
        ValueError: Skill 当前不可加载、路径非法或正文不符合约束时抛出。
        FileNotFoundError: SKILL.md 不存在时抛出。
    """
    if skill.get("status") != "available":
        raise ValueError(
            f"Skill {skill.get('skill_id', '<unknown>')} 只有 available 状态可以加载"
        )
    source_path = Path(str(skill.get("source_path", ""))).resolve()
    registry_root = Path(registry_source_path).resolve().parent
    if not source_path.is_relative_to(registry_root):
        raise ValueError(f"Skill source_path 越出受控目录：{source_path}")
    if source_path.name != "SKILL.md":
        raise ValueError(f"Skill 文档必须命名为 SKILL.md：{source_path}")
    content = _read_bounded_utf8(
        source_path,
        max_bytes=MAX_SKILL_DOCUMENT_BYTES,
        resource_name=f"Skill {skill.get('skill_id', '<unknown>')}",
    )
    updated = dict(skill)
    updated.update(
        {
            "status": "loaded",
            "bound_task_id": None,
            "content": content,
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
    )
    return cast(SkillRecord, updated)
