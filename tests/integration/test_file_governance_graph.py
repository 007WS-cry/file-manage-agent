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

"""本文件集成测试真实 DOCX、阶段分派顶层图、产物隔离、SQLite 恢复和 CLI。"""


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
    delivery_log_path: str | None = None,
) -> dict:
    """创建集成测试使用的完整顶层治理初始状态。

    Args:
        input_root: 只读业务文件测试目录。
        artifact_root: 标准化内容和中间产物目录。
        report_root: Markdown 报告目录。
        auto_select_threshold: 自动主版本选择阈值。
        delivery_log_path: 可选的本地发送记录 JSON 路径。

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
            "pdf_match_threshold": 0.82,
            "delivery_log_path": delivery_log_path,
            "use_llm_summary": False,
        },
        {
            "input_root": str(input_root),
            "input_readonly": True,
            "artifact_root": str(artifact_root),
            "report_root": str(report_root),
        },
    )


def write_delivery_log(
    path: Path,
    *,
    attachment_name: str,
    attachment_sha256: str,
) -> None:
    """写入端到端测试使用的单条客户确认发送记录。

    Args:
        path: 本地发送记录 JSON 输出路径。
        attachment_name: 发送时使用的附件文件名。
        attachment_sha256: 用于精确匹配文件版本的原始附件摘要。
    """
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "deliveries": [
                    {
                        "id": "delivery-confirmed",
                        "attachment_name": attachment_name,
                        "attachment_sha256": attachment_sha256,
                        "normalized_digest": None,
                        "sent_at": "2026-07-19T10:00:00+08:00",
                        "recipient_label": "客户甲",
                        "customer_confirmed": True,
                        "evidence_ref": "local-log://delivery-confirmed",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_top_graph_registers_task_tracking_around_four_business_subgraphs() -> None:
    """0.5.4 顶层图必须在 Skill 后召回 Memory，再规划和执行固定 Task。"""
    graph = build_file_governance_graph().get_graph()
    edges = {(edge.source, edge.target) for edge in graph.edges}

    assert ("initialize_run", "execute_before_run_hooks") in edges
    assert ("validate_request", "load_system_prompt") in edges
    assert ("load_system_prompt", "load_skill_registry") in edges
    assert ("load_skill_registry", "recall_long_term_memory") in edges
    assert ("recall_long_term_memory", "plan_run_tasks") in edges
    assert ("plan_run_tasks", "run_inventory_subgraph") in edges
    assert ("run_inventory_subgraph", "sync_inventory_task_status") in edges
    assert (
        "sync_inventory_task_status",
        "dispatch_content_subagent_task",
    ) in edges
    assert (
        "dispatch_content_subagent_task",
        "run_version_analysis_subgraph",
    ) in edges
    assert (
        "run_version_analysis_subgraph",
        "sync_version_task_status",
    ) in edges
    assert (
        "sync_version_task_status",
        "run_evidence_subgraph",
    ) in edges
    assert ("run_evidence_subgraph", "sync_evidence_task_status") in edges
    assert (
        "sync_evidence_task_status",
        "dispatch_evidence_subagent_task",
    ) in edges
    assert (
        "dispatch_evidence_subagent_task",
        "run_recommendation_subgraph",
    ) in edges
    assert (
        "run_recommendation_subgraph",
        "sync_recommendation_task_status",
    ) in edges
    assert ("sync_report_task_status", "persist_long_term_memory") in edges
    assert ("persist_long_term_memory", "execute_after_run_hooks") in edges
    assert ("apply_human_selection", "sync_human_review_task_status") in edges
    assert (
        "sync_human_review_task_status",
        "generate_governance_report",
    ) in edges
    assert (
        "sync_human_review_task_status",
        "generate_failure_report",
    ) in edges
    assert ("generate_no_data_report", "sync_report_task_status") in edges
    assert ("generate_governance_report", "sync_report_task_status") in edges
    assert ("generate_failure_report", "sync_report_task_status") in edges
    assert ("generate_failure_report", "persist_long_term_memory") in edges
    assert ("execute_after_run_hooks", "finalize_run") in edges
    assert (
        "execute_after_run_hooks",
        "generate_lifecycle_failure_report",
    ) in edges


def test_top_graph_uses_delivery_evidence_in_recommendation_and_report(
    tmp_path: Path,
) -> None:
    """本地客户确认应贯穿 Evidence、Recommendation 和最终治理报告。"""
    input_root = tmp_path / "input"
    artifact_root = tmp_path / "artifacts"
    report_root = tmp_path / "reports"
    delivery_log_path = tmp_path / "delivery_log.json"
    input_root.mkdir()
    source_path = input_root / "contract_final.docx"
    create_docx(source_path, "Amount CNY 1200 Clause A")
    source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    write_delivery_log(
        delivery_log_path,
        attachment_name=source_path.name,
        attachment_sha256=source_sha256,
    )
    state = create_test_state(
        input_root,
        artifact_root,
        report_root,
        auto_select_threshold=0.82,
        delivery_log_path=str(delivery_log_path),
    )

    result = build_file_governance_graph().invoke(
        state,
        config={"configurable": {"thread_id": "evidence-recommendation-report"}},
    )

    assert result["run"]["status"] == "completed"
    assert len(result["deliveries"]) == 1
    assert result["deliveries"][0]["match_method"] == "sha256"
    assert result["deliveries"][0]["customer_confirmed"] is True
    assert result["decisions"][0]["selected_by"] == "rule"
    assert any("客户已确认" in reason for reason in result["decisions"][0]["reasons"])
    assert "### PDF 来源证据" in result["report"]["report_markdown"]
    assert "### 发送与确认记录" in result["report"]["report_markdown"]
    assert "local-log://delivery-confirmed" in result["report"]["report_markdown"]
    assert "1 条发送证据" in result["report"]["summary"]


def test_invalid_delivery_log_is_nonfatal_and_reaches_recommendation(
    tmp_path: Path,
) -> None:
    """非法发送日志应降级为部分成功，并继续生成推荐和带警告报告。"""
    input_root = tmp_path / "input"
    artifact_root = tmp_path / "artifacts"
    report_root = tmp_path / "reports"
    delivery_log_path = tmp_path / "delivery_log.json"
    input_root.mkdir()
    create_docx(input_root / "contract_final.docx", "Amount CNY 1200 Clause A")
    delivery_log_path.write_text("{not-json", encoding="utf-8")
    state = create_test_state(
        input_root,
        artifact_root,
        report_root,
        auto_select_threshold=0.82,
        delivery_log_path=str(delivery_log_path),
    )

    result = build_file_governance_graph().invoke(
        state,
        config={"configurable": {"thread_id": "invalid-evidence-degrades"}},
    )

    assert result["run"]["status"] == "partial"
    assert len(result["decisions"]) == 1
    assert any(error["stage"] == "evidence" and not error["fatal"] for error in result["errors"])
    task_statuses = {task["task_type"]: task["status"] for task in result["tasks"]}
    assert task_statuses["evidence"] == "completed"
    assert task_statuses["report"] == "completed"
    assert "## 运行警告" in result["report"]["report_markdown"]


def test_top_graph_completes_without_modifying_source_files(tmp_path: Path) -> None:
    """自动治理应生成标准化内容和报告，同时保持全部原文件字节不变。"""
    input_root = tmp_path / "input"
    artifact_root = tmp_path / "artifacts"
    report_root = tmp_path / "reports"
    input_root.mkdir()
    create_docx(input_root / "contract_v1.docx", "Amount CNY 1000 Clause A")
    create_docx(input_root / "contract_final.docx", "Amount CNY 1200 Clause A")
    source_hashes = {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in input_root.iterdir()
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
    assert all(
        Path(item["content_ref"]).parent == artifact_root / "normalized"
        for item in result["documents"]
    )
    assert all(
        hashlib.sha256(path.read_bytes()).hexdigest() == digest
        for path, digest in source_hashes.items()
    )
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
    paused_task_by_type = {task["task_type"]: dict(task) for task in paused["tasks"]}
    assert len(paused_task_by_type) == 6
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
    resumed_task_by_type = {task["task_type"]: task for task in resumed["tasks"]}
    assert len(resumed_task_by_type) == 6
    assert len({task["task_id"] for task in resumed["tasks"]}) == 6
    for task_type in ("inventory", "version_analysis", "evidence", "recommendation"):
        assert resumed_task_by_type[task_type] == paused_task_by_type[task_type]
    assert resumed_task_by_type["human_review"]["status"] == "completed"
    assert resumed_task_by_type["report"]["status"] == "completed"


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
                "prompt": {"enabled": False},
                "hooks": {"enabled": False},
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
    assert [todo["status"] for todo in output["todos"]] == ["completed"] * 4
    assert output["task_status_counts"] == {
        "pending": 0,
        "running": 0,
        "completed": 2,
        "failed": 0,
        "skipped": 4,
    }
    assert "documents" not in output
    assert "tasks" not in output
    assert "report_markdown" not in output
    assert Path(output["report_path"]).exists()


def test_cli_outputs_task_progress_during_pause_and_after_resume(
    tmp_path: Path,
    capsys,
) -> None:
    """CLI 应在人工暂停和 SQLite 恢复后输出安全 Todo 与 Task 计数摘要。"""
    input_root = tmp_path / "input"
    input_root.mkdir()
    create_docx(input_root / "proposal_v1.docx", "Amount CNY 1000")
    create_docx(input_root / "proposal_v2.docx", "Amount CNY 1200")
    request_path = tmp_path / "request.json"
    checkpoint_path = tmp_path / "checkpoints" / "governance.sqlite3"
    response_path = tmp_path / "review_response.json"
    request_path.write_text(
        json.dumps(
            {
                "request": {
                    "root_directory": "input",
                    "recursive": True,
                    "allowed_extensions": [".docx"],
                    "max_files": 20,
                    "grouping_similarity_threshold": 0.72,
                    "auto_select_threshold": 1.0,
                    "use_llm_summary": False,
                },
                "workspace": {
                    "input_root": "input",
                    "input_readonly": True,
                    "artifact_root": "artifacts",
                    "report_root": "reports",
                },
                "checkpoint": {
                    "backend": "sqlite",
                    "database_path": "checkpoints/governance.sqlite3",
                },
                "prompt": {"enabled": False},
                "hooks": {"enabled": False},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    exit_code = main(["run", str(request_path), "--thread-id", "cli-task-progress-human"])
    paused_capture = capsys.readouterr()
    paused_output = json.loads(paused_capture.out)

    assert exit_code == 0
    assert paused_capture.err == ""
    assert paused_output["status"] == "waiting_human"
    assert [todo["status"] for todo in paused_output["todos"]] == [
        "completed",
        "completed",
        "in_progress",
        "pending",
    ]
    assert paused_output["task_status_counts"] == {
        "pending": 1,
        "running": 1,
        "completed": 4,
        "failed": 0,
        "skipped": 0,
    }
    assert len(paused_output["interrupts"]) == 1
    assert "documents" not in paused_output
    assert "tasks" not in paused_output
    review_group = paused_output["interrupts"][0]["groups"][0]
    group_id = review_group["group_id"]
    selected_file_id = review_group["candidates"][-1]["file_id"]
    response_path.write_text(
        json.dumps(
            {
                "selections": {group_id: selected_file_id},
                "review_note": "CLI 0.4.0 checkpoint 恢复测试",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    resume_exit_code = main(
        [
            "resume",
            str(response_path),
            "--thread-id",
            "cli-task-progress-human",
            "--checkpoint-path",
            str(checkpoint_path),
        ]
    )
    resumed_capture = capsys.readouterr()
    resumed_output = json.loads(resumed_capture.out)

    assert resume_exit_code == 0
    assert resumed_capture.err == ""
    assert resumed_output["status"] == "completed"
    assert [todo["status"] for todo in resumed_output["todos"]] == ["completed"] * 4
    assert resumed_output["task_status_counts"] == {
        "pending": 0,
        "running": 0,
        "completed": 6,
        "failed": 0,
        "skipped": 0,
    }
    assert resumed_output["interrupts"] == []
    assert "documents" not in resumed_output
    assert "tasks" not in resumed_output
    assert "report_markdown" not in resumed_output
    assert Path(resumed_output["report_path"]).exists()
