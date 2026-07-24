from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from docx import Document

from app.graphs.file_governance import build_file_governance_graph
from app.state.factories import create_initial_state

"""本文件验证 0.6.0 checkpoint 缺少恢复字段时可安全升级，并保持原治理结论。"""


# 0.7.0 Recovery 与幂等能力不得改写的确定性业务结果字段。
GOVERNANCE_CONCLUSION_FIELDS = (
    "files",
    "documents",
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

# 0.6.0 checkpoint 尚不包含的 0.7.0 顶层恢复字段。
POST_V060_STATE_FIELDS = (
    "recovery",
    "node_executions",
    "degradations",
)


def create_docx(path: Path, text: str) -> None:
    """创建 0.6.0/0.7.0 兼容测试使用的 DOCX。

    Args:
        path: DOCX 输出路径。
        text: 写入正文的稳定测试内容。
    """
    document = Document()
    document.add_paragraph(text)
    document.save(path)


def create_v060_compatibility_state(tmp_path: Path) -> dict:
    """创建关闭数据库、Memory 与 Context Compact 的确定性双版本状态。

    Args:
        tmp_path: pytest 为当前测试提供的隔离目录。

    Returns:
        具有固定运行 ID、两个合同版本和自动选择阈值的完整状态。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    create_docx(
        input_root / "agreement_v1.docx",
        "Agreement Beta Amount CNY 1000 Clause A",
    )
    create_docx(
        input_root / "agreement_v2.docx",
        "Agreement Beta Amount CNY 1200 Clause A",
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
        thread_id="v060-v070-compatibility",
    )
    state["run"]["run_id"] = "v060-v070-compatibility-run"
    return state


def test_v060_state_receives_safe_v070_recovery_defaults(
    tmp_path: Path,
) -> None:
    """旧状态应补齐空恢复集合和报告引用，同时保持结论、Task 终态及主版本协议。"""
    current_state = create_v060_compatibility_state(tmp_path)
    legacy_state = deepcopy(current_state)
    for field_name in POST_V060_STATE_FIELDS:
        legacy_state.pop(field_name, None)
    legacy_state["report"].pop("degradation_ids", None)
    legacy_state["report"].pop("recovered_error_ids", None)

    current_result = build_file_governance_graph().invoke(
        deepcopy(current_state),
        config={"configurable": {"thread_id": "v070-current-state"}},
    )
    legacy_result = build_file_governance_graph().invoke(
        legacy_state,
        config={"configurable": {"thread_id": "v060-legacy-state"}},
    )

    assert legacy_result["run"]["status"] == current_result["run"]["status"]
    for field_name in GOVERNANCE_CONCLUSION_FIELDS:
        assert legacy_result[field_name] == current_result[field_name]
    assert (
        legacy_result["report"]["report_markdown"]
        == current_result["report"]["report_markdown"]
    )
    assert legacy_result["report"]["degradation_ids"] == []
    assert legacy_result["report"]["recovered_error_ids"] == []
    assert legacy_result["degradations"] == []
    assert legacy_result["recovery"]["action"] == "none"
    assert legacy_result["recovery"]["pending_error_ids"] == []
    assert legacy_result["node_executions"]
    assert [task["status"] for task in legacy_result["tasks"]] == [
        task["status"] for task in current_result["tasks"]
    ]
    assert all(
        decision["recommended_file_id"] is not None
        and decision["needs_human_review"] is False
        for decision in legacy_result["decisions"]
    )
