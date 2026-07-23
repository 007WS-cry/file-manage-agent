from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from docx import Document

from app.graphs.file_governance import build_file_governance_graph
from app.state.factories import create_initial_state
from app.state.models import FileGovernanceState

"""本模块验证 0.5.0 状态升级到 0.6.0 后仍保持确定性治理结论。"""


# 0.6.0 不得因扩展状态补齐而改写的治理结论字段。
GOVERNANCE_CONCLUSION_FIELDS = (
    "version_groups",
    "diffs",
    "version_edges",
    "branches",
    "version_chains",
    "pdf_exports",
    "deliveries",
    "decisions",
    "errors",
)

# 0.5.0 checkpoint 尚不包含的 0.6.0 顶层扩展字段。
POST_V050_STATE_FIELDS = (
    "skill_registry",
    "memory",
    "context_compact",
    "application_database",
)


def create_docx(path: Path, text: str) -> None:
    """创建 0.5.0/0.6.0 兼容测试使用的 DOCX。

    Args:
        path: DOCX 输出路径。
        text: 写入首个正文段落的测试内容。
    """
    document = Document()
    document.add_paragraph(text)
    document.save(path)


def create_compatibility_state(tmp_path: Path) -> FileGovernanceState:
    """创建关闭 0.6.0 持久化扩展的确定性治理状态。

    Args:
        tmp_path: pytest 提供的隔离临时目录。

    Returns:
        包含两个合同版本、固定运行 ID和隔离产物目录的顶层状态。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    create_docx(
        input_root / "agreement_v1.docx",
        "Agreement Alpha Amount CNY 1000 Clause A",
    )
    create_docx(
        input_root / "agreement_v2.docx",
        "Agreement Alpha Amount CNY 1200 Clause A",
    )
    state = create_initial_state(
        {
            "root_directory": str(input_root),
            "recursive": True,
            "allowed_extensions": [".docx"],
            "max_files": 20,
            "grouping_similarity_threshold": 0.72,
            "auto_select_threshold": 0.0,
            "pdf_match_threshold": 0.82,
            "delivery_log_path": None,
            "use_llm_summary": False,
        },
        {
            "input_root": str(input_root),
            "input_readonly": True,
            "artifact_root": str(tmp_path / "artifacts"),
            "report_root": str(tmp_path / "reports"),
        },
        prompt_config={"enabled": False},
        hook_config={"enabled": False},
        llm_config={
            "enabled": False,
            "provider": "mock",
            "model": "mock-structured-v1",
        },
    )
    state["run"]["run_id"] = "v050-v060-compatibility"
    return state


def test_v050_state_receives_safe_v060_defaults_without_changing_results(
    tmp_path: Path,
) -> None:
    """缺少四类扩展字段的旧状态应补齐关闭值，并保持治理结果完全一致。"""
    current_state = create_compatibility_state(tmp_path)
    legacy_state = deepcopy(current_state)
    legacy_state["run"].pop("thread_id", None)
    for field_name in POST_V050_STATE_FIELDS:
        legacy_state.pop(field_name, None)

    current_result = build_file_governance_graph().invoke(
        deepcopy(current_state),
        config={"configurable": {"thread_id": "v060-current-state"}},
    )
    legacy_result = build_file_governance_graph().invoke(
        legacy_state,
        config={"configurable": {"thread_id": "v050-legacy-state"}},
    )

    assert legacy_result["run"]["status"] == current_result["run"]["status"]
    for field_name in GOVERNANCE_CONCLUSION_FIELDS:
        assert legacy_result[field_name] == current_result[field_name]
    assert legacy_result["report"]["report_markdown"] == current_result["report"]["report_markdown"]
    assert legacy_result["memory"]["status"] == "disabled"
    assert legacy_result["context_compact"]["status"] == "disabled"
    assert legacy_result["application_database"]["status"] == "disabled"
    assert legacy_result["run"]["thread_id"] == legacy_result["run"]["run_id"]
    assert all(
        skill["status"] == "available" and skill["content"] == "" and skill["bound_task_id"] is None
        for skill in legacy_result["skill_registry"]["skills"]
    )
