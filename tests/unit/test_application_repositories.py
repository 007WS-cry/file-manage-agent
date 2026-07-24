from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.storage.database import (
    create_application_engine,
    create_session_factory,
    open_application_session,
    validate_application_database_path,
)
from app.storage.orm_models import (
    Base,
    ContextSummaryModel,
    ErrorRecoveryRecordModel,
    GovernanceRunModel,
    HumanReviewModel,
    MemoryItemModel,
    NodeExecutionRecordModel,
    ToolCallAuditModel,
)
from app.storage.repositories import (
    ErrorRecoveryRecordRepository,
    NodeExecutionRecordRepository,
    create_repository_bundle,
)

"""本文件单元测试应用数据库路径边界、短事务语义和七张表 Repository 的基础读写。"""


def prepare_database(tmp_path: Path):
    """创建单元测试专用应用数据库、表结构和 Session 工厂。

    Args:
        tmp_path: pytest 为当前测试提供的临时目录。

    Returns:
        已创建表结构的 Engine 和绑定该 Engine 的 Session 工厂。
    """
    database_path = tmp_path / "nested" / "application.sqlite3"
    engine = create_application_engine(database_path)
    Base.metadata.create_all(engine)
    return engine, create_session_factory(engine)


def make_governance_run(run_id: str = "run-001") -> GovernanceRunModel:
    """构造 Repository 测试使用的最小合法治理运行。

    Args:
        run_id: 测试治理运行唯一 ID。

    Returns:
        尚未加入 Session 的 GovernanceRunModel。
    """
    return GovernanceRunModel(
        run_id=run_id,
        thread_id="thread-001",
        status="running",
        current_stage="database_skeleton_test",
        request_summary={"root_label": "脱敏输入目录"},
    )


def test_engine_creates_parent_and_all_application_tables(tmp_path: Path) -> None:
    """Engine 应自动创建父目录，ORM 元数据应创建且只创建七张应用表。"""
    database_path = tmp_path / "new-parent" / "application.sqlite3"

    engine = create_application_engine(database_path)
    Base.metadata.create_all(engine)

    assert database_path.parent.is_dir()
    assert set(inspect(engine).get_table_names()) == {
        "context_summaries",
        "error_recovery_records",
        "governance_runs",
        "human_reviews",
        "memory_items",
        "node_execution_records",
        "tool_call_audits",
    }
    engine.dispose()


def test_repository_bundle_persists_and_reads_all_models(tmp_path: Path) -> None:
    """五个基础 Repository 应在同一短事务中写入，并在提交后稳定读回。"""
    engine, session_factory = prepare_database(tmp_path)
    with open_application_session(session_factory) as session:
        repositories = create_repository_bundle(session)
        repositories.governance_runs.add(make_governance_run())
        repositories.memory_items.add(
            MemoryItemModel(
                id="memory-001",
                namespace="workspace:example",
                scope="long_term",
                kind="governance_preference",
                summary="用户偏好保留全部版本链。",
                structured_data={"preserve_all_versions": True},
                artifact_refs=["artifact://report-001"],
                source_run_id="run-001",
                confirmed_by_human=True,
                confidence=1.0,
            )
        )
        repositories.context_summaries.add(
            ContextSummaryModel(
                id="context-001",
                run_id="run-001",
                stage="after_inventory",
                summary="已完成文件发现与内容提取。",
                artifact_refs=["artifact://inventory-001"],
                estimated_tokens=320,
                compaction_index=1,
            )
        )
        repositories.tool_call_audits.add(
            ToolCallAuditModel(
                id="tool-001",
                run_id="run-001",
                task_id="run-001:inventory",
                tool_name="discover_input_files",
                status="success",
                output_summary="发现 3 个候选文件。",
                output_ref="artifact://inventory-001",
                output_size_bytes=128,
                duration_ms=12,
                error_type=None,
                error_message=None,
            )
        )
        repositories.human_reviews.add(
            HumanReviewModel(
                id="review-001",
                run_id="run-001",
                group_id="group-001",
                selected_file_id="file-003",
                review_note="已核对客户确认记录。",
                reviewer_label="user",
            )
        )

    with open_application_session(session_factory) as session:
        repositories = create_repository_bundle(session)
        assert repositories.governance_runs.get("run-001").status == "running"
        assert (
            repositories.memory_items.list_by_namespace("workspace:example")[0].id == "memory-001"
        )
        assert repositories.context_summaries.list_by_run("run-001")[0].id == ("context-001")
        assert repositories.tool_call_audits.list_by_run("run-001")[0].id == ("tool-001")
        assert repositories.human_reviews.list_by_run("run-001")[0].id == ("review-001")
    engine.dispose()


def test_repository_bundle_exposes_recovery_repositories(tmp_path: Path) -> None:
    """RepositoryBundle 应公开两张恢复表，且模型与 Repository 类型固定。"""
    engine, session_factory = prepare_database(tmp_path)
    with open_application_session(session_factory) as session:
        repositories = create_repository_bundle(session)

        assert isinstance(
            repositories.node_execution_records,
            NodeExecutionRecordRepository,
        )
        assert isinstance(
            repositories.error_recovery_records,
            ErrorRecoveryRecordRepository,
        )
        assert repositories.node_execution_records.model_type is NodeExecutionRecordModel
        assert repositories.error_recovery_records.model_type is ErrorRecoveryRecordModel
    engine.dispose()


def test_session_context_rolls_back_all_records_on_failure(tmp_path: Path) -> None:
    """事务内出现异常时必须回滚，不能留下只写入一半的治理记录。"""
    engine, session_factory = prepare_database(tmp_path)

    with pytest.raises(RuntimeError, match="触发事务回滚"):
        with open_application_session(session_factory) as session:
            repositories = create_repository_bundle(session)
            repositories.governance_runs.add(make_governance_run())
            raise RuntimeError("触发事务回滚")

    with open_application_session(session_factory) as session:
        repositories = create_repository_bundle(session)
        assert repositories.governance_runs.get("run-001") is None
    engine.dispose()


def test_sqlite_foreign_keys_are_enabled_for_repository_flush(
    tmp_path: Path,
) -> None:
    """引用未知 run_id 的 Memory 应在 flush 时触发外键错误。"""
    engine, session_factory = prepare_database(tmp_path)

    with pytest.raises(IntegrityError):
        with open_application_session(session_factory) as session:
            repositories = create_repository_bundle(session)
            repositories.memory_items.add(
                MemoryItemModel(
                    id="memory-orphan",
                    namespace="workspace:example",
                    scope="long_term",
                    kind="stage_summary",
                    summary="不应写入的孤立 Memory。",
                    structured_data={},
                    artifact_refs=[],
                    source_run_id="missing-run",
                    confirmed_by_human=False,
                    confidence=0.8,
                )
            )
    engine.dispose()


def test_database_path_rejects_input_overlap_and_checkpoint_reuse(
    tmp_path: Path,
) -> None:
    """应用数据库不得位于输入目录，也不得复用 checkpoint 文件。"""
    input_root = tmp_path / "input"
    input_root.mkdir()
    checkpoint_path = tmp_path / "checkpoints" / "state.sqlite3"

    with pytest.raises(ValueError, match="只读输入目录"):
        validate_application_database_path(
            input_root / "application.sqlite3",
            input_root=input_root,
        )

    with pytest.raises(ValueError, match="checkpoint"):
        validate_application_database_path(
            checkpoint_path,
            checkpoint_path=checkpoint_path,
        )


def test_repository_limit_and_required_lookup_are_explicit(tmp_path: Path) -> None:
    """非法查询上限和缺失必需记录应产生明确异常。"""
    engine, session_factory = prepare_database(tmp_path)
    with open_application_session(session_factory) as session:
        repositories = create_repository_bundle(session)

        with pytest.raises(ValueError, match="1 到 1000"):
            repositories.governance_runs.list_by_thread("thread-001", limit=0)

        with pytest.raises(LookupError, match="missing-run"):
            repositories.governance_runs.get_required("missing-run")
    engine.dispose()
