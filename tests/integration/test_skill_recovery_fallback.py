from __future__ import annotations

from pathlib import Path

import pytest

from app.graphs.error_recovery import build_error_recovery_graph
from app.nodes.skills import load_skill_registry
from app.state.converters import file_governance_to_recovery_state
from app.state.factories import create_initial_state

"""本文件验证 Skill 注册表故障耗尽重试后由 Recovery 统一登记 default_skill 降级。"""


def create_skill_failure_state(tmp_path: Path) -> dict:
    """创建具有稳定运行身份、但不访问应用数据库的最小顶层状态。

    Args:
        tmp_path: pytest 为当前测试提供的隔离目录。

    Returns:
        可供 Skill 节点和 Error Recovery 子图依次调用的完整状态。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    state = create_initial_state(
        {
            "root_directory": str(input_root),
            "recursive": True,
            "allowed_extensions": [".docx"],
            "max_files": 20,
            "grouping_similarity_threshold": 0.72,
            "auto_select_threshold": 0.82,
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
        thread_id="skill-recovery-fallback",
    )
    state["run"].update(
        {
            "run_id": "skill-recovery-fallback-run",
            "thread_id": "skill-recovery-fallback",
            "status": "running",
            "current_stage": "skills",
            "started_at": "2026-07-24T08:00:00+00:00",
        }
    )
    return state


def test_skill_failure_uses_default_skill_after_retry_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 Skill 节点产生的错误应由策略子图决定 default_skill，而非节点自行决定。"""
    state = create_skill_failure_state(tmp_path)

    def raise_registry_failure(configured_path=None):
        """模拟受控 Skill 注册表读取失败。"""
        del configured_path
        raise OSError("injected skill registry failure")

    monkeypatch.setattr(
        "app.nodes.skills.load_skill_registry_metadata",
        raise_registry_failure,
    )
    node_update = load_skill_registry(state)
    error = dict(node_update["errors"][0])
    error["retry_count"] = 1
    state["skill_registry"] = node_update["skill_registry"]
    state["errors"] = [error]

    result = build_error_recovery_graph().invoke(
        file_governance_to_recovery_state(state)
    )
    recovered_error = next(
        item for item in result["errors"] if item["id"] == error["id"]
    )

    assert error["category"] == "skill"
    assert error["fallback"] == "default_skill"
    assert recovered_error["status"] == "fallback_applied"
    assert recovered_error["fallback"] == "default_skill"
    assert recovered_error["retry_count"] == 1
    assert result["recovery"]["action"] == "fallback"
    assert result["recovery"]["resume_node"] == "load_skill_registry"
    assert result["recovery"]["resume_after_node"] == "recall_long_term_memory"
    assert result["degradations"][0]["action"] == "default_skill"
