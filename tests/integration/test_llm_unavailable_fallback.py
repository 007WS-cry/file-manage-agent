from __future__ import annotations

import json
from pathlib import Path

import pytest
from docx import Document

from app.graphs.file_governance import build_file_governance_graph
from app.state.factories import create_initial_state

"""本文件验证真实 Provider 不可用时三个业务阶段安全回退且治理事实保持一致。"""

# 测试专用且刻意不设置的 API Key 环境变量名称。
MISSING_API_KEY_ENV = "FILE_MANAGE_AGENT_TEST_MISSING_OPENAI_KEY"

# 不得出现在任何状态、错误、消息或报告中的伪密钥文本。
FORBIDDEN_SECRET = "sk-test-secret-must-never-enter-state"

# 模型解释不可修改的顶层确定性业务结果字段。
DETERMINISTIC_RESULT_FIELDS = (
    "version_groups",
    "diffs",
    "version_edges",
    "branches",
    "version_chains",
    "pdf_exports",
    "deliveries",
    "decisions",
)


def write_fallback_docx(path: Path, amount: int) -> None:
    """创建 LLM 不可用回退测试使用的候选合同版本。

    Args:
        path: 测试 DOCX 输出路径。
        amount: 当前版本合同金额。
    """
    document = Document()
    document.add_paragraph(f"合同金额 CNY {amount}。" + ("共同条款。" * 100))
    document.save(path)


def create_fallback_state(
    input_root: Path,
    output_root: Path,
    *,
    unavailable_openai: bool,
) -> dict:
    """创建确定性基线或缺失 API Key 的真实 Provider 顶层状态。

    Args:
        input_root: 两个合同版本所在的只读输入目录。
        output_root: 当前运行独立使用的产物和报告目录。
        unavailable_openai: True 时启用缺少密钥的 OpenAI Provider。

    Returns:
        可直接提交给顶层 File Governance 图的完整状态。
    """
    llm_config = (
        {
            "enabled": True,
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "api_key_env": MISSING_API_KEY_ENV,
            "fallback_enabled": True,
        }
        if unavailable_openai
        else {
            "enabled": False,
            "provider": "mock",
            "model": "mock-structured-v1",
        }
    )
    return create_initial_state(
        {
            "root_directory": str(input_root),
            "recursive": True,
            "allowed_extensions": [".docx"],
            "max_files": 20,
            "grouping_similarity_threshold": 0.72,
            "auto_select_threshold": 0.82,
            "pdf_match_threshold": 0.82,
            "delivery_log_path": None,
            "use_llm_summary": unavailable_openai,
        },
        {
            "input_root": str(input_root),
            "input_readonly": True,
            "artifact_root": str(output_root / "artifacts"),
            "report_root": str(output_root / "reports"),
        },
        llm_config=llm_config,
    )


def test_missing_api_key_falls_back_without_changing_governance_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺少 API Key 应产生 fallback 审计、部分成功状态和相同确定性结论。"""
    monkeypatch.delenv(MISSING_API_KEY_ENV, raising=False)
    input_root = tmp_path / "input"
    input_root.mkdir()
    write_fallback_docx(input_root / "contract_v1.docx", 1_000)
    write_fallback_docx(input_root / "contract_v2.docx", 1_200)

    baseline = build_file_governance_graph().invoke(
        create_fallback_state(
            input_root,
            tmp_path / "baseline",
            unavailable_openai=False,
        ),
        config={"configurable": {"thread_id": "llm-fallback-baseline"}},
    )
    fallback = build_file_governance_graph().invoke(
        create_fallback_state(
            input_root,
            tmp_path / "fallback",
            unavailable_openai=True,
        ),
        config={"configurable": {"thread_id": "llm-missing-key-fallback"}},
    )

    assert baseline["run"]["status"] == "completed"
    assert fallback["run"]["status"] == "partial"
    assert all(
        fallback[field_name] == baseline[field_name]
        for field_name in DETERMINISTIC_RESULT_FIELDS
    )
    assert fallback["llm_calls"]
    assert all(call["status"] == "fallback" for call in fallback["llm_calls"])
    assert all(call["fallback_used"] is True for call in fallback["llm_calls"])
    assert {call["agent_id"] for call in fallback["llm_calls"]} == {
        "content-subagent",
        "version-subagent",
        "evidence-subagent",
    }
    assert all(diff["summary_source"] == "deterministic" for diff in fallback["diffs"])
    assert all(diff["summary_message_id"] is None for diff in fallback["diffs"])
    assert not any(error["fatal"] for error in fallback["errors"])

    serialized = json.dumps(fallback, ensure_ascii=False, default=str)
    assert FORBIDDEN_SECRET not in serialized
    assert fallback["llm"]["api_key_env"] == MISSING_API_KEY_ENV
    assert not any(value == FORBIDDEN_SECRET for value in fallback["llm"].values())
