from __future__ import annotations

from pathlib import Path

import pytest

from app.skills.loader import (
    load_skill_document,
    load_skill_registry_metadata,
)

"""本文件验证 Skill 注册表只加载元数据，并安全按需读取单个受控 SKILL.md。"""


def test_default_registry_loads_metadata_without_skill_content() -> None:
    """默认注册表应登记四个 available Skill，但不得预读任何正文。"""
    registry = load_skill_registry_metadata()

    assert registry["version"] == "skill-registry-v1"
    assert registry["status"] == "ready"
    assert [skill["skill_id"] for skill in registry["skills"]] == [
        "file-content-analysis",
        "version-relation",
        "evidence-confidence",
        "governance-report",
    ]
    assert all(skill["status"] == "available" for skill in registry["skills"])
    assert all(skill["content"] == "" for skill in registry["skills"])
    assert all(skill["content_sha256"] is None for skill in registry["skills"])


def test_loader_reads_only_explicit_skill_and_calculates_digest() -> None:
    """单个加载接口应只返回目标正文及其 SHA-256，不修改其他注册记录。"""
    registry = load_skill_registry_metadata()
    selected = registry["skills"][1]

    loaded = load_skill_document(
        selected,
        registry_source_path=registry["source_path"],
    )

    assert loaded["skill_id"] == "version-relation"
    assert loaded["status"] == "loaded"
    assert "# 版本关系解释 Skill" in loaded["content"]
    assert len(loaded["content_sha256"] or "") == 64
    assert registry["skills"][1]["status"] == "available"
    assert registry["skills"][1]["content"] == ""


def test_registry_rejects_skill_path_outside_controlled_directory(
    tmp_path: Path,
) -> None:
    """registry.yaml 不得通过相对路径越界读取受控目录外的 Markdown。"""
    registry_root = tmp_path / "skills"
    registry_root.mkdir()
    outside_document = tmp_path / "SKILL.md"
    outside_document.write_text("# 越界 Skill", encoding="utf-8")
    registry_path = registry_root / "registry.yaml"
    registry_path.write_text(
        "\n".join(
            [
                "version: skill-registry-v1",
                "skills:",
                "  - id: escaped",
                "    name: 越界",
                "    description: 不应被加载",
                "    path: ../SKILL.md",
                "    task_types: [inventory]",
                "    roles: [content]",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="越出受控目录"):
        load_skill_registry_metadata(registry_path)
