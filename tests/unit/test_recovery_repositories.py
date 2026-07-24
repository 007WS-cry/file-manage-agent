from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.exc import IntegrityError

from app.state.models import ErrorRecord, NodeExecutionRecord
from app.storage import (
    ErrorRecoveryRecordModel,
    ErrorRecoveryRecordRepository,
    NodeExecutionRecordModel,
    NodeExecutionRecordRepository,
)
from app.storage.database import (
    create_application_engine,
    create_session_factory,
    open_application_session,
)
from app.storage.orm_models import Base, GovernanceRunModel
from app.storage.repositories import (
    build_error_recovery_record_id,
    create_repository_bundle,
)
from app.utils.runtime import create_error_record

"""本文件验证错误恢复与节点幂等仓储的短事务、重放保护和跨运行隔离。"""


# 节点执行测试统一使用的带时区开始时间。
STARTED_AT = "2026-07-24T08:00:00+00:00"

# 节点执行测试统一使用的带时区完成时间。
FINISHED_AT = "2026-07-24T08:00:05+00:00"


def prepare_database(tmp_path: Path):
    """创建恢复仓储测试专用数据库和 Session 工厂。

    Args:
        tmp_path: pytest 为当前测试提供的临时目录。

    Returns:
        已创建七张表的 Engine 和绑定该 Engine 的 Session 工厂。
    """
    engine = create_application_engine(tmp_path / "database" / "application.sqlite3")
    Base.metadata.create_all(engine)
    return engine, create_session_factory(engine)


def make_governance_run(run_id: str) -> GovernanceRunModel:
    """创建恢复仓储外键依赖的最小治理运行。

    Args:
        run_id: 当前测试使用的治理运行 ID。

    Returns:
        尚未加入 Session 的合法治理运行 ORM 对象。
    """
    return GovernanceRunModel(
        run_id=run_id,
        thread_id=f"thread:{run_id}",
        status="recovering",
        current_stage="error_recovery",
        request_summary={},
    )


def make_node_execution(
    *,
    run_id: str = "run-recovery-001",
    status: str = "running",
    attempt_count: int = 1,
    input_digest: str = "input-digest-001",
    finished_at: str | None = None,
) -> NodeExecutionRecord:
    """创建可持久化的节点执行状态。

    Args:
        run_id: 节点执行所属治理运行 ID。
        status: 节点执行状态。
        attempt_count: 累计执行次数。
        input_digest: 幂等校验使用的输入摘要。
        finished_at: 可选执行完成时间。

    Returns:
        具有完整恢复协议字段的 NodeExecutionRecord。
    """
    return cast(
        NodeExecutionRecord,
        {
            "id": f"{run_id}:inventory:extract:001",
            "task_execution_id": f"{run_id}:inventory:execution",
            "run_id": run_id,
            "task_id": f"{run_id}:inventory",
            "stage": "inventory",
            "node_name": "extract_docx_content",
            "input_digest": input_digest,
            "status": status,
            "attempt_count": attempt_count,
            "state_update_ref": (
                "artifact://state-update-001" if status in {"succeeded", "reused"} else None
            ),
            "result_refs": (
                ["artifact://document-001"] if status in {"succeeded", "reused"} else []
            ),
            "result_digest": ("result-digest-001" if status in {"succeeded", "reused"} else None),
            "last_error_id": None,
            "started_at": STARTED_AT,
            "finished_at": finished_at,
        },
    )


def make_recovery_error(
    *,
    node_execution_id: str | None,
    task_id: str | None = "run-recovery-001:inventory",
    retry_count: int = 0,
    status: str = "pending",
) -> ErrorRecord:
    """创建与测试节点关联的超时恢复错误。

    Args:
        node_execution_id: 可选节点幂等执行 ID。
        task_id: 可选关联 Task ID。
        retry_count: 已执行的额外重试次数。
        status: 当前错误恢复状态。

    Returns:
        可由 ErrorRecoveryRecordRepository 持久化的 ErrorRecord。
    """
    return create_error_record(
        stage="inventory",
        node_name="extract_docx_content",
        category="timeout",
        message="文档提取超时",
        task_id=task_id,
        node_execution_id=node_execution_id,
        exception_type="TimeoutError",
        retryable=True,
        retry_count=retry_count,
        max_retries=2,
        requires_human=True,
        status=status,
        fatal=False,
        created_at=STARTED_AT,
    )


def test_repositories_upsert_recovery_and_reusable_execution(tmp_path: Path) -> None:
    """仓储应幂等推进节点及错误状态，并返回输入摘要一致的可复用结果。"""
    engine, session_factory = prepare_database(tmp_path)
    running = make_node_execution()
    with open_application_session(session_factory) as session:
        repositories = create_repository_bundle(session)
        repositories.governance_runs.add(make_governance_run("run-recovery-001"))
        repositories.node_execution_records.upsert_state(running)

    succeeded = make_node_execution(
        status="succeeded",
        attempt_count=1,
        finished_at=FINISHED_AT,
    )
    error = make_recovery_error(node_execution_id=succeeded["id"])
    with open_application_session(session_factory) as session:
        repositories = create_repository_bundle(session)
        repositories.node_execution_records.upsert_state(succeeded)
        repositories.error_recovery_records.upsert_state(
            "run-recovery-001",
            error,
            action="retry",
        )

    retrying_error = cast(
        ErrorRecord,
        {
            **error,
            "retry_count": 1,
            "status": "retrying",
        },
    )
    with open_application_session(session_factory) as session:
        repositories = create_repository_bundle(session)
        repositories.error_recovery_records.upsert_state(
            "run-recovery-001",
            retrying_error,
            action="retry",
        )
        reusable = repositories.node_execution_records.find_reusable(
            succeeded["id"],
            input_digest=succeeded["input_digest"],
        )
        recovery_records = repositories.error_recovery_records.list_by_run("run-recovery-001")

        assert reusable is not None
        assert reusable.result_refs == ["artifact://document-001"]
        assert len(recovery_records) == 1
        assert recovery_records[0].retry_count == 1
        assert recovery_records[0].action == "retry"
        assert recovery_records[0].status == "retrying"
    engine.dispose()


def test_storage_package_exports_recovery_persistence_types() -> None:
    """storage 包应公开两张 ORM 表及其 Repository 类型。"""
    assert ErrorRecoveryRecordRepository.model_type is ErrorRecoveryRecordModel
    assert NodeExecutionRecordRepository.model_type is NodeExecutionRecordModel


def test_repositories_reject_stale_or_mutated_idempotency_state(
    tmp_path: Path,
) -> None:
    """旧 checkpoint 不得回退尝试次数或改变已保存节点的输入摘要。"""
    engine, session_factory = prepare_database(tmp_path)
    with open_application_session(session_factory) as session:
        repositories = create_repository_bundle(session)
        repositories.governance_runs.add(make_governance_run("run-recovery-001"))
        repositories.node_execution_records.upsert_state(make_node_execution(attempt_count=2))

    with pytest.raises(ValueError, match="不得小于已持久化次数"):
        with open_application_session(session_factory) as session:
            create_repository_bundle(session).node_execution_records.upsert_state(
                make_node_execution(attempt_count=1)
            )

    with pytest.raises(ValueError, match="input_digest 不得改变"):
        with open_application_session(session_factory) as session:
            create_repository_bundle(session).node_execution_records.upsert_state(
                make_node_execution(
                    attempt_count=2,
                    input_digest="different-input-digest",
                )
            )

    with open_application_session(session_factory) as session:
        create_repository_bundle(session).node_execution_records.upsert_state(
            make_node_execution(
                status="succeeded",
                attempt_count=2,
                finished_at=FINISHED_AT,
            )
        )
    with pytest.raises(ValueError, match="不允许从 succeeded 回退"):
        with open_application_session(session_factory) as session:
            create_repository_bundle(session).node_execution_records.upsert_state(
                make_node_execution(status="running", attempt_count=2)
            )
    engine.dispose()


def test_error_records_are_isolated_by_run_and_reject_retry_rollback(
    tmp_path: Path,
) -> None:
    """相同 ErrorRecord ID 可跨运行保存，但同一运行的重试次数不得倒退。"""
    engine, session_factory = prepare_database(tmp_path)
    error = make_recovery_error(
        node_execution_id=None,
        task_id=None,
        retry_count=1,
        status="retrying",
    )
    with open_application_session(session_factory) as session:
        repositories = create_repository_bundle(session)
        repositories.governance_runs.add(make_governance_run("run-recovery-001"))
        repositories.governance_runs.add(make_governance_run("run-recovery-002"))
        first = repositories.error_recovery_records.upsert_state(
            "run-recovery-001",
            error,
            action="retry",
        )
        second = repositories.error_recovery_records.upsert_state(
            "run-recovery-002",
            error,
            action="retry",
        )

        assert first.record_id != second.record_id
        assert first.record_id == build_error_recovery_record_id(
            "run-recovery-001",
            error["id"],
        )

    stale_error = cast(
        ErrorRecord,
        {
            **error,
            "retry_count": 0,
            "status": "pending",
        },
    )
    with pytest.raises(ValueError, match="不得小于已持久化次数"):
        with open_application_session(session_factory) as session:
            create_repository_bundle(session).error_recovery_records.upsert_state(
                "run-recovery-001",
                stale_error,
            )

    recovered_error = cast(
        ErrorRecord,
        {
            **error,
            "status": "recovered",
            "recovered_at": FINISHED_AT,
        },
    )
    with open_application_session(session_factory) as session:
        create_repository_bundle(session).error_recovery_records.upsert_state(
            "run-recovery-001",
            recovered_error,
        )
    reopened_error = cast(
        ErrorRecord,
        {
            **error,
            "status": "pending",
        },
    )
    with pytest.raises(ValueError, match="不允许从 recovered 回退"):
        with open_application_session(session_factory) as session:
            create_repository_bundle(session).error_recovery_records.upsert_state(
                "run-recovery-001",
                reopened_error,
            )
    engine.dispose()


def test_each_node_uses_a_distinct_short_transaction(tmp_path: Path) -> None:
    """模拟两个图节点时必须使用不同 Session，且退出后不保留活动事务。"""
    engine, session_factory = prepare_database(tmp_path)
    with open_application_session(session_factory) as first_session:
        create_repository_bundle(first_session).governance_runs.add(
            make_governance_run("run-recovery-001")
        )
    assert first_session.get_transaction() is None

    with open_application_session(session_factory) as second_session:
        create_repository_bundle(second_session).node_execution_records.upsert_state(
            make_node_execution()
        )
    assert second_session.get_transaction() is None
    assert first_session is not second_session
    engine.dispose()


def test_node_execution_requires_existing_governance_run(tmp_path: Path) -> None:
    """节点执行引用未知运行时必须由 SQLite 外键拒绝。"""
    engine, session_factory = prepare_database(tmp_path)

    with pytest.raises(IntegrityError):
        with open_application_session(session_factory) as session:
            create_repository_bundle(session).node_execution_records.upsert_state(
                make_node_execution()
            )
    engine.dispose()
