from __future__ import annotations

import hashlib
import json
from pathlib import Path

from docx import Document
from langgraph.types import Command

from app.entrypoints.cli import main
from app.graphs.file_governance import build_file_governance_graph
from app.state.factories import create_initial_state
from app.storage.artifacts import save_intermediate_artifact
from app.storage.checkpoints import open_checkpointer

"""本文件集成测试真实 DOCX、顶层治理图、产物隔离、SQLite 恢复和最小 CLI。"""


def create_docx(path: Path, text: str) -> None:
    """创建顶层图集成测试使用的最小 DOCX 文件。

    Args:
        path: 测试 DOCX 输出路径。
        text: 写入首个正文段落的文本。
    """
    document = Document()
    document.add_paragraph(text)
    document.save(path)


def create_test_state(
    input_root: Path,
    artifact_root: Path,
    report_root: Path,
    *,
    auto_select_threshold: float,
) -> dict:
    """创建集成测试使用的完整顶层治理初始状态。

    Args:
        input_root: 只读业务文件测试目录。
        artifact_root: 标准化内容和中间产物目录。
        report_root: Markdown 报告目录。
        auto_select_threshold: 自动主版本选择阈值。

    Returns:
        可直接提交给顶层 LangGraph 的状态。
    """
    return create_initial_state(
        {
            "root_directory": str(input_root),
            "recursive": True,
            "allowed_extensions": [".docx"],
            "max_files": 20,
            "grouping_similarity_threshold": 0.72,
            "auto_select_threshold": auto_select_threshold,
            "use_llm_summary": False,
        },
        {
            "input_root": str(input_root),
            "input_readonly": True,
            "artifact_root": str(artifact_root),
            "report_root": str(report_root),
        },
    )


def test_top_graph_completes_without_modifying_source_files(tmp_path: Path) -> None:
    """自动治理应生成标准化内容和报告，同时保持全部原文件字节不变。"""
    input_root = tmp_path / "input"
    artifact_root = tmp_path / "artifacts"
    report_root = tmp_path / "reports"
    input_root.mkdir()
    create_docx(input_root / "contract_v1.docx", "Amount CNY 1000 Clause A")
    create_docx(input_root / "contract_final.docx", "Amount CNY 1200 Clause A")
    source_hashes = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in input_root.iterdir()
    }
    state = create_test_state(
        input_root,
        artifact_root,
        report_root,
        auto_select_threshold=0.82,
    )

    result = build_file_governance_graph().invoke(
        state,
        config={"configurable": {"thread_id": "automatic-path"}},
    )
    intermediate_path = save_intermediate_artifact(
        artifact_root,
        result["run"]["run_id"],
        "integration-summary",
        {"decision_count": len(result["decisions"])},
        input_root=input_root,
    )

    assert result["run"]["status"] == "completed"
    assert result["decisions"][0]["selected_by"] == "rule"
    assert Path(result["report"]["report_path"]).exists()
    assert Path(intermediate_path).parent == artifact_root / "intermediate"
    assert all(Path(item["content_ref"]).parent == artifact_root / "normalized" for item in result["documents"])
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in source_hashes.items())
    assert not any(path.suffix in {".json", ".md", ".sqlite3"} for path in input_root.rglob("*"))


def test_sqlite_checkpoint_resumes_after_reopen(tmp_path: Path) -> None:
    """关闭并重新打开 SQLite 后，应能用同一 thread_id 恢复人工审核。"""
    input_root = tmp_path / "input"
    artifact_root = tmp_path / "artifacts"
    report_root = tmp_path / "reports"
    checkpoint_path = tmp_path / "checkpoints" / "governance.sqlite3"
    input_root.mkdir()
    create_docx(input_root / "proposal_v1.docx", "Amount CNY 1000")
    create_docx(input_root / "proposal_v2.docx", "Amount CNY 1200")
    state = create_test_state(
        input_root,
        artifact_root,
        report_root,
        auto_select_threshold=1.0,
    )
    config = {"configurable": {"thread_id": "sqlite-human-review"}}

    with open_checkpointer(
        "sqlite",
        database_path=checkpoint_path,
        input_root=input_root,
    ) as checkpointer:
        paused = build_file_governance_graph(checkpointer=checkpointer).invoke(
            state,
            config=config,
        )

    assert paused["run"]["status"] == "waiting_human"
    assert paused.get("__interrupt__")
    group_id = paused["human_review"]["pending_group_ids"][0]
    selected_file_id = paused["version_groups"][0]["file_ids"][-1]

    with open_checkpointer("sqlite", database_path=checkpoint_path) as checkpointer:
        resumed = build_file_governance_graph(checkpointer=checkpointer).invoke(
            Command(
                resume={
                    "selections": {group_id: selected_file_id},
                    "review_note": "SQLite 跨进程恢复测试",
                }
            ),
            config=config,
        )

    assert checkpoint_path.exists()
    assert resumed["run"]["status"] == "completed"
    assert resumed["decisions"][0]["selected_by"] == "human"
    assert resumed["decisions"][0]["recommended_file_id"] == selected_file_id


def test_cli_runs_empty_directory_request(tmp_path: Path, capsys) -> None:
    """最小 CLI 应能读取请求 JSON 并完成空目录治理报告。"""
    input_root = tmp_path / "input"
    input_root.mkdir()
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "request": {
                    "root_directory": "input",
                    "recursive": True,
                    "allowed_extensions": [".docx"],
                    "max_files": 20,
                    "grouping_similarity_threshold": 0.72,
                    "auto_select_threshold": 0.82,
                    "use_llm_summary": False,
                },
                "workspace": {
                    "input_root": "input",
                    "input_readonly": True,
                    "artifact_root": "artifacts",
                    "report_root": "reports",
                },
                "checkpoint": {"backend": "memory"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    exit_code = main(["run", str(request_path), "--thread-id", "cli-empty"])
    captured = capsys.readouterr()
    output = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert output["thread_id"] == "cli-empty"
    assert output["status"] == "completed"
    assert Path(output["report_path"]).exists()
